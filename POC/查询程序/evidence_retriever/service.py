from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from .mapping import RECORD_NAME_CONTAINS, SourceRoute, index_routes, load_routes
from .medical_data import MarkdownMedicalData
from .medical_record_filter import filter_rule_medical_records
from .planning import PlanKey, resolve_query_plan, safe_index_query_plans


EXPECTED_FIELDS: dict[tuple[str, str, str], tuple[str, ...]] = {
    ("患者", "性别", "患者信息"): ("gend",),
    ("患者", "生育状态", "患者信息"): ("matnStas",),
    ("诊断", "标准诊断", "诊断信息"): ("diseName",),
    ("评估", "疾病分期", "诊断信息"): ("diseName",),
    ("病情", "活动进展", "诊断信息"): ("diseName",),
}

DIAGNOSIS_RESULT_FIELDS = (
    "diseName",
    "diseCodg",
    "maindiseFlag",
    "inoutDiseType",
)

DIAGNOSIS_SUFFIXES = (
    "术后",
    "待查",
    "可能",
    "复发",
    "转移",
)

EXPENSE_FIELD_ALIASES = {
    "chrg_type": "chrgType",
    "hilist_name": "hilistName",
    "hosplist_name": "hosplistName",
    "set_name": "setName",
}

LAB_SEARCH_FIELDS = ("itemName", "indexName")
LAB_RESULT_FIELDS = (
    "itemName",
    "indexName",
    "quantRslt",
    "indexRslt",
    "resultStatus",
    "refVal",
    "examUnt",
    "examRsltVal",
    "itemCode",
    "indexCode",
)
LAB_VIEW_NAMES = {"检验报告", "检验报告明细"}
KEYWORD_VIEWS = {"费用明细", *LAB_VIEW_NAMES}


def _is_lab_view(view_name: str) -> bool:
    return view_name in LAB_VIEW_NAMES


def _canonical_view_name(view_name: str) -> str:
    return "检验报告明细" if _is_lab_view(view_name) else view_name


class RetrievalService:
    def __init__(self, mapping_path: Path, medical_data_path: Path):
        self.mapping_path = mapping_path
        self.medical_data_path = medical_data_path
        self.routes = load_routes(mapping_path)
        self.route_index = index_routes(self.routes)
        self.medical_data = MarkdownMedicalData(medical_data_path)
        self.query_cache: dict[str, dict[str, Any]] = {}

    @staticmethod
    def load_rule_evidence(path: Path) -> list[dict[str, Any]]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("规则证据 JSON 顶层必须是数组")
        return payload

    @staticmethod
    def _query_key(route: SourceRoute, medins_id: str, mdtrt_id: str) -> str:
        return json.dumps(
            {
                "source_code": route.source_code,
                "view_name": route.view_name,
                "medinsId": medins_id,
                "mdtrtId": mdtrt_id,
                "recordName": route.record_name,
                "recordType": route.record_type,
                "matchMode": route.match_mode,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @staticmethod
    def _evidence_id(query_key: str) -> str:
        digest = hashlib.sha1(query_key.encode("utf-8")).hexdigest()[:10].upper()
        return f"EV-{digest}"

    def _execute_route(
        self,
        route: SourceRoute,
        medins_id: str,
        mdtrt_id: str,
    ) -> dict[str, Any]:
        query_key = self._query_key(route, medins_id, mdtrt_id)
        if query_key in self.query_cache:
            return self.query_cache[query_key]

        records: list[dict[str, object]] = []
        if "当前六视图无对应" in route.view_name:
            status = "VIEW_UNAVAILABLE"
        elif route.document_kind == "病历":
            if route.record_name is None and route.record_type is None:
                status = "RECORD_NAME_NOT_MAPPED"
            else:
                records = self.medical_data.query_records(
                    medins_id,
                    mdtrt_id,
                    route.record_name,
                    route.record_type,
                    record_name_contains=route.match_mode == RECORD_NAME_CONTAINS,
                )
                status = "FOUND" if records else "NOT_FOUND"
        else:
            records = self.medical_data.query_structured(
                _canonical_view_name(route.view_name),
                medins_id,
                mdtrt_id,
            )
            status = "FOUND" if records else "NOT_FOUND"

        evidence = {
            "evidence_id": self._evidence_id(query_key),
            "route": route,
            "status": status,
            "records": records,
        }
        self.query_cache[query_key] = evidence
        return evidence

    @staticmethod
    def _missing_expected_fields(
        records: list[dict[str, object]],
        expected_fields: tuple[str, ...],
    ) -> list[str]:
        return [
            field
            for field in expected_fields
            if all(field not in record or record[field] in (None, "") for record in records)
        ]

    @staticmethod
    def _needs_secondary(attempts: list[dict[str, Any]]) -> bool:
        if not attempts:
            return True
        statuses = {attempt["status"] for attempt in attempts}
        return bool(
            statuses <= {"NOT_FOUND", "VIEW_UNAVAILABLE", "RECORD_NAME_NOT_MAPPED"}
            or "EXPECTED_FIELD_MISSING" in statuses
        )

    @staticmethod
    def _source_label(route: SourceRoute) -> str:
        return f"{route.source_code} {route.source_name}"

    @staticmethod
    def _diagnosis_keyword(
        target_fact: str,
        records: list[dict[str, object]],
    ) -> str | None:
        candidates: set[str] = set()
        for record in records:
            diagnosis_name = str(record.get("diseName", "")).strip()
            if not diagnosis_name:
                continue
            candidates.add(diagnosis_name)
            normalized = diagnosis_name
            for suffix in DIAGNOSIS_SUFFIXES:
                if normalized.endswith(suffix):
                    normalized = normalized[: -len(suffix)].strip()
            if len(normalized) >= 2:
                candidates.add(normalized)
        matches = [candidate for candidate in candidates if candidate in target_fact]
        return max(matches, key=len) if matches else None

    def _format_structured_records(
        self,
        route: SourceRoute,
        primary_domain: str,
        secondary_tag: str,
        target_fact: str,
        records: list[dict[str, object]],
        query_plan: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, object]], dict[str, Any] | None]:
        if route.view_name == "费用明细":
            if query_plan is None:
                return [], {
                    "status": "QUERY_SCOPE_UNRESOLVED",
                    "reason": "当前事实无专属计划，且同一申诉命题中没有可继承的争议实体",
                }
            return self._filter_expense_records(records, query_plan)
        if _is_lab_view(route.view_name):
            if query_plan is None:
                return [], {
                    "status": "QUERY_SCOPE_UNRESOLVED",
                    "reason": "检验结果需要按itemName或indexName生成关键词计划",
                }
            return self._filter_lab_records(records, query_plan)
        if route.view_name == "诊断信息":
            keyword = self._diagnosis_keyword(target_fact, records)
            if keyword:
                matched_records = [
                    record
                    for record in records
                    if keyword in str(record.get("diseName", ""))
                ]
                projected = [
                    {
                        field: record[field]
                        for field in DIAGNOSIS_RESULT_FIELDS
                        if field in record
                    }
                    for record in matched_records
                ]
                return projected, {
                    "field": "diseName",
                    "operator": "contains",
                    "value": keyword,
                    "value_source": "target_fact",
                }

        expected_fields = EXPECTED_FIELDS.get(
            (primary_domain, secondary_tag, route.view_name),
            (),
        )
        if expected_fields:
            return [
                {
                    field: record[field]
                    for field in expected_fields
                    if field in record
                }
                for record in records
                if any(record.get(field) not in (None, "") for field in expected_fields)
            ], None
        return records, None

    @classmethod
    def _filter_lab_records(
        cls,
        records: list[dict[str, object]],
        query_plan: dict[str, Any],
    ) -> tuple[list[dict[str, object]], dict[str, Any]]:
        search_fields = [
            str(field).strip()
            for field in query_plan.get("search_fields", LAB_SEARCH_FIELDS)
            if str(field).strip()
        ] or list(LAB_SEARCH_FIELDS)
        entities = query_plan.get("entities")
        if not isinstance(entities, list) or not entities:
            return [], {
                "query_id": query_plan.get("query_id"),
                "supports_facts": query_plan.get("supports_facts"),
                "plan_origin": query_plan.get("plan_origin"),
                "entity_results": [],
                "search_fields": search_fields,
                "search_terms": [],
                "record_count": 0,
                "fields_trimmed": False,
            }

        def normalized(value: object) -> str:
            text = cls._normalize_search_text(value)
            return text.replace("特异性", "特异")

        def field_values(record: dict[str, object]) -> dict[str, str]:
            return {
                field: normalized(record.get(field, ""))
                for field in search_fields
            }

        def contains_any(values: dict[str, str], terms: list[str]) -> bool:
            normalized_terms = [normalized(term) for term in terms if str(term).strip()]
            return any(
                term and term in value
                for term in normalized_terms
                for value in values.values()
            )

        def contains_all_one_field(values: dict[str, str], terms: list[str]) -> bool:
            normalized_terms = [normalized(term) for term in terms if str(term).strip()]
            return bool(normalized_terms) and any(
                all(term in value for term in normalized_terms)
                for value in values.values()
            )

        all_matched: list[dict[str, object]] = []
        entity_results: list[dict[str, Any]] = []
        all_terms: list[str] = []
        for entity in entities:
            source_exact = str(entity.get("source_exact", "")).strip()
            core_terms = [
                str(term).strip()
                for term in entity.get("core_terms", [])
                if str(term).strip()
            ]
            qualifiers = [
                str(term).strip()
                for term in entity.get("qualifiers", [])
                if str(term).strip()
            ]
            expanded_terms = list(
                dict.fromkeys(
                    str(term).strip()
                    for term in list(entity.get("normalized_terms", []))
                    + list(entity.get("alias_terms", []))
                    if str(term).strip() and str(term).strip() != source_exact
                )
            )
            all_terms.extend([source_exact, *core_terms, *qualifiers, *expanded_terms])
            stages = [
                ("SOURCE_EXACT", [source_exact], False),
                ("CORE_QUALIFIER", [*core_terms, *qualifiers], True),
                ("EXPANDED", expanded_terms, False),
            ]
            entity_matches: list[dict[str, object]] = []
            matched_stage = "NO_MATCH"
            matched_terms: list[str] = []
            matched_fields: set[str] = set()
            for stage_name, terms, require_all in stages:
                if not terms or not any(str(term).strip() for term in terms):
                    continue
                current_matches: list[dict[str, object]] = []
                current_fields: set[str] = set()
                for record in records:
                    values = field_values(record)
                    if require_all:
                        matched = contains_all_one_field(values, terms)
                    else:
                        matched = contains_any(values, terms)
                    if not matched:
                        continue
                    current_matches.append(record)
                    current_fields.update(
                        field for field, value in values.items()
                        if any(normalized(term) and normalized(term) in value for term in terms)
                    )
                if current_matches:
                    entity_matches = current_matches
                    matched_stage = stage_name
                    matched_terms = terms
                    matched_fields = current_fields
                    break
            all_matched.extend(entity_matches)
            entity_results.append(
                {
                    "entity_id": entity.get("entity_id"),
                    "source_exact": source_exact,
                    "stage": matched_stage,
                    "matched_terms": matched_terms,
                    "matched_fields": sorted(matched_fields),
                    "record_count": len(entity_matches),
                }
            )

        projected = [
            {
                field: record[field]
                for field in LAB_RESULT_FIELDS
                if field in record
            }
            for record in all_matched
        ]
        return projected, {
            "query_id": query_plan.get("query_id"),
            "supports_facts": query_plan.get("supports_facts"),
            "plan_origin": query_plan.get("plan_origin"),
            "entity_results": entity_results,
            "search_fields": search_fields,
            "search_terms": list(dict.fromkeys(term for term in all_terms if term)),
            "record_count": len(projected),
            "fields_trimmed": True,
        }

    @staticmethod
    def _normalize_search_text(value: object) -> str:
        text = unicodedata.normalize("NFKC", str(value or "")).casefold()
        return re.sub(r"[\s\-_/\\,，。;；:：()（）\[\]【】]+", "", text)

    @classmethod
    def _filter_expense_records(
        cls,
        records: list[dict[str, object]],
        query_plan: dict[str, Any],
    ) -> tuple[list[dict[str, object]], dict[str, Any]]:
        charge_type = query_plan.get("charge_type") or {}
        primary_codes = {str(code).zfill(2) for code in charge_type.get("primary", [])}
        fallback_codes = {str(code).zfill(2) for code in charge_type.get("fallback", [])}
        search_fields = [
            EXPENSE_FIELD_ALIASES.get(field, field)
            for field in query_plan.get("search_fields", [])
        ]
        fallback_fields = [
            EXPENSE_FIELD_ALIASES.get(field, field)
            for field in query_plan.get("fallback_search_fields", [])
        ]

        def charge_code(record: dict[str, object]) -> str:
            return str(record.get("chrgType", record.get("chrg_type", "")) or "").zfill(2)

        def matches_name(
            record: dict[str, object],
            fields: list[str],
            terms: list[str],
        ) -> bool:
            normalized_terms = [cls._normalize_search_text(term) for term in terms]
            values = [cls._normalize_search_text(record.get(field, "")) for field in fields]
            return any(term and term in value for term in normalized_terms for value in values)

        def matches_core_qualifiers(
            record: dict[str, object],
            fields: list[str],
            core_terms: list[str],
            qualifiers: list[str],
        ) -> bool:
            required_terms = [
                cls._normalize_search_text(term)
                for term in [*core_terms, *qualifiers]
                if str(term).strip()
            ]
            if not required_terms:
                return False
            values = [cls._normalize_search_text(record.get(field, "")) for field in fields]
            return any(all(term in value for term in required_terms) for value in values)

        entities = query_plan.get("entities")
        if not isinstance(entities, list) or not entities:
            legacy_terms = [
                str(term.get("value", "")).strip()
                for term in query_plan.get("search_terms", [])
                if str(term.get("value", "")).strip()
            ]
            entities = [
                {
                    "entity_id": "E01",
                    "source_exact": legacy_terms[0] if legacy_terms else "",
                    "core_terms": legacy_terms[:1],
                    "qualifiers": [],
                    "normalized_terms": legacy_terms[1:],
                    "alias_terms": [],
                }
            ]

        all_matched: list[dict[str, object]] = []
        entity_results: list[dict[str, Any]] = []
        all_terms: list[str] = []
        for entity in entities:
            source_exact = str(entity.get("source_exact", "")).strip()
            core_terms = [
                str(term).strip()
                for term in entity.get("core_terms", [])
                if str(term).strip()
            ]
            qualifiers = [
                str(term).strip()
                for term in entity.get("qualifiers", [])
                if str(term).strip()
            ]
            expanded_terms = list(
                dict.fromkeys(
                    str(term).strip()
                    for term in list(entity.get("normalized_terms", []))
                    + list(entity.get("alias_terms", []))
                    if str(term).strip() and str(term).strip() != source_exact
                )
            )
            all_terms.extend([source_exact, *core_terms, *qualifiers, *expanded_terms])
            stages = [
                (
                    "PRIMARY_CHRG_TYPE_SOURCE_EXACT",
                    lambda record: charge_code(record) in primary_codes,
                    search_fields,
                    [source_exact],
                ),
                (
                    "PRIMARY_CHRG_TYPE_CORE_QUALIFIER",
                    lambda record: charge_code(record) in primary_codes,
                    search_fields,
                    None,
                ),
                (
                    "PRIMARY_CHRG_TYPE_EXPANDED",
                    lambda record: charge_code(record) in primary_codes,
                    search_fields,
                    expanded_terms,
                ),
                (
                    "FALLBACK_CHRG_TYPE_SOURCE_EXACT",
                    lambda record: charge_code(record) in fallback_codes,
                    search_fields,
                    [source_exact],
                ),
                (
                    "FALLBACK_CHRG_TYPE_CORE_QUALIFIER",
                    lambda record: charge_code(record) in fallback_codes,
                    search_fields,
                    None,
                ),
                (
                    "FALLBACK_CHRG_TYPE_EXPANDED",
                    lambda record: charge_code(record) in fallback_codes,
                    search_fields,
                    expanded_terms,
                ),
                (
                    "ALL_CHRG_TYPE_SOURCE_EXACT",
                    lambda record: True,
                    search_fields,
                    [source_exact],
                ),
                (
                    "ALL_CHRG_TYPE_CORE_QUALIFIER",
                    lambda record: True,
                    search_fields,
                    None,
                ),
                (
                    "ALL_CHRG_TYPE_EXPANDED",
                    lambda record: True,
                    search_fields,
                    expanded_terms,
                ),
                (
                    "PRIMARY_AND_FALLBACK_SET_NAME_SOURCE_EXACT",
                    lambda record: charge_code(record) in primary_codes | fallback_codes,
                    fallback_fields,
                    [source_exact],
                ),
                (
                    "PRIMARY_AND_FALLBACK_SET_NAME_EXPANDED",
                    lambda record: charge_code(record) in primary_codes | fallback_codes,
                    fallback_fields,
                    expanded_terms,
                ),
                (
                    "ALL_CHRG_TYPE_SET_NAME_SOURCE_EXACT",
                    lambda record: True,
                    fallback_fields,
                    [source_exact],
                ),
                (
                    "ALL_CHRG_TYPE_SET_NAME_EXPANDED",
                    lambda record: True,
                    fallback_fields,
                    expanded_terms,
                ),
            ]
            entity_matches: list[dict[str, object]] = []
            matched_stage = "NO_MATCH"
            matched_terms: list[str] = []
            for stage_name, charge_predicate, fields, stage_terms in stages:
                if not fields:
                    continue
                if stage_terms is None:
                    entity_matches = [
                        record
                        for record in records
                        if charge_predicate(record)
                        and matches_core_qualifiers(
                            record,
                            fields,
                            core_terms,
                            qualifiers,
                        )
                    ]
                    current_matched_terms = [*core_terms, *qualifiers]
                else:
                    if not stage_terms:
                        continue
                    entity_matches = [
                        record
                        for record in records
                        if charge_predicate(record)
                        and matches_name(record, fields, stage_terms)
                    ]
                    current_matched_terms = stage_terms
                if entity_matches:
                    matched_stage = stage_name
                    matched_terms = current_matched_terms
                    break
            all_matched.extend(entity_matches)
            entity_results.append(
                {
                    "entity_id": entity.get("entity_id"),
                    "source_exact": source_exact,
                    "stage": matched_stage,
                    "matched_terms": matched_terms,
                    "record_count": len(entity_matches),
                }
            )

        return all_matched, {
            "query_id": query_plan.get("query_id"),
            "supports_facts": query_plan.get("supports_facts"),
            "plan_origin": query_plan.get("plan_origin"),
            "supplement_reason": query_plan.get("supplement_reason"),
            "entity_results": entity_results,
            "search_terms": list(dict.fromkeys(term for term in all_terms if term)),
            "record_count": len(all_matched),
            "deduplicated": False,
            "fields_trimmed": False,
        }

    @staticmethod
    def _planned_route(
        primary_domain: str,
        secondary_tag: str,
        view_name: str,
    ) -> SourceRoute:
        if view_name == "检验报告明细":
            source_code = "DS-S-004"
            source_name = "检验报告"
        else:
            source_code = "DS-S-002"
            source_name = "费用明细"
        return SourceRoute(
            mapping_id=f"PLAN-{primary_domain}-{secondary_tag}",
            medins_id="运行时传入",
            mdtrt_id="运行时传入",
            primary_domain=primary_domain,
            secondary_tag=secondary_tag,
            source_level="PRIMARY",
            source_code=source_code,
            source_name=source_name,
            view_name=view_name,
            document_kind="具体信息",
            record_name=None,
            record_type=None,
            mapping_status="PLANNED",
        )

    def _run_routes(
        self,
        routes: list[SourceRoute],
        primary_domain: str,
        secondary_tag: str,
        medins_id: str,
        mdtrt_id: str,
    ) -> list[dict[str, Any]]:
        attempts: list[dict[str, Any]] = []
        for route in routes:
            evidence = self._execute_route(route, medins_id, mdtrt_id)
            expected_fields = EXPECTED_FIELDS.get(
                (primary_domain, secondary_tag, route.view_name),
                (),
            )
            missing_fields = []
            if evidence["status"] == "FOUND" and expected_fields:
                missing_fields = self._missing_expected_fields(
                    evidence["records"],
                    expected_fields,
                )
            attempts.append(
                {
                    "route": route,
                    "status": "EXPECTED_FIELD_MISSING" if missing_fields else evidence["status"],
                    "records": evidence["records"],
                }
            )
        return attempts

    def _retrieve_tag(
        self,
        proposition_id: str,
        tag: dict[str, Any],
        medins_id: str,
        mdtrt_id: str,
        secondary_mode: str,
        query_plan_index: dict[PlanKey, dict[str, Any]],
    ) -> dict[str, Any]:
        primary_domain = str(tag.get("一级证据域", "")).strip()
        secondary_tag = str(tag.get("二级证据标签", "")).strip()
        target_fact = str(tag.get("target_fact", "")).strip()
        plan_key = (proposition_id, primary_domain, secondary_tag, target_fact)
        route_groups = self.route_index.get(
            (primary_domain, secondary_tag),
            {"PRIMARY": [], "SECONDARY": []},
        )
        mapped_to_keyword_view = any(
            route.view_name in KEYWORD_VIEWS
            for route in [*route_groups["PRIMARY"], *route_groups["SECONDARY"]]
        )
        exact_plan = query_plan_index.get(plan_key)
        has_exact_plan = bool(
            exact_plan
            and isinstance(exact_plan.get("entities"), list)
            and exact_plan["entities"]
        )
        query_plan = (
            resolve_query_plan(query_plan_index, plan_key)
            if has_exact_plan or mapped_to_keyword_view
            else None
        )
        primary_routes = list(route_groups["PRIMARY"])
        if query_plan is not None:
            planned_view = str(query_plan.get("view_name", "")).strip()
            if planned_view in KEYWORD_VIEWS and not any(
                route.view_name == planned_view
                or _canonical_view_name(route.view_name)
                == _canonical_view_name(planned_view)
                for route in primary_routes
            ):
                primary_routes.insert(0, self._planned_route(
                    primary_domain,
                    secondary_tag,
                    planned_view,
                ))

        primary_attempts = self._run_routes(
            primary_routes,
            primary_domain,
            secondary_tag,
            medins_id,
            mdtrt_id,
        )
        execute_secondary = secondary_mode != "deferred"
        keyword_scope_unresolved = query_plan is None and any(
            route.view_name in KEYWORD_VIEWS
            for route in [*primary_routes, *route_groups["SECONDARY"]]
        )
        secondary_attempts = []
        if execute_secondary:
            secondary_attempts = self._run_routes(
                route_groups["SECONDARY"],
                primary_domain,
                secondary_tag,
                medins_id,
                mdtrt_id,
            )

        def attempt_has_relevant_data(attempt: dict[str, Any]) -> bool:
            if attempt["status"] != "FOUND":
                return False
            route = attempt["route"]
            if route.view_name != "费用明细":
                return True
            formatted, _ = self._format_structured_records(
                route,
                primary_domain,
                secondary_tag,
                target_fact,
                attempt["records"],
                query_plan,
            )
            return bool(formatted)

        successful_primary = [
            attempt for attempt in primary_attempts if attempt_has_relevant_data(attempt)
        ]
        successful_secondary = [
            attempt for attempt in secondary_attempts if attempt_has_relevant_data(attempt)
        ]
        successful_attempts = successful_primary + successful_secondary
        all_attempts = primary_attempts + secondary_attempts

        source_attempts = successful_attempts or all_attempts
        source = "、".join(
            dict.fromkeys(
                self._source_label(attempt["route"])
                for attempt in source_attempts
            )
        ) or None

        data: list[dict[str, object]] = []
        source_groups: list[dict[str, Any]] = []
        source_group_index: dict[tuple[str, str], dict[str, Any]] = {}
        matched_by: dict[str, Any] | None = None

        for attempt in all_attempts:
            route = attempt["route"]
            formatted: list[dict[str, object]] = []
            current_match = None
            if attempt_has_relevant_data(attempt):
                formatted, current_match = self._format_structured_records(
                    route,
                    primary_domain,
                    secondary_tag,
                    target_fact,
                    attempt["records"],
                    query_plan,
                )
            source_entry = {
                "source": self._source_label(route),
                "source_level": route.source_level,
            }
            group_scope_unresolved = query_plan is None and route.view_name in KEYWORD_VIEWS
            source_group = {
                **source_entry,
                "status": (
                    "RETRIEVED" if formatted
                    else "QUERY_SCOPE_UNRESOLVED" if group_scope_unresolved
                    else "NOT_RETRIEVED"
                ),
            }
            if group_scope_unresolved:
                source_group["query_scope_status"] = "QUERY_SCOPE_UNRESOLVED"
                source_group["query_scope_reason"] = "当前事实没有可安全执行的关键词计划，未返回该来源全量数据"
            source_group_key = (source_entry["source"], source_entry["source_level"])
            existing_group = source_group_index.get(source_group_key)
            if existing_group is None:
                source_group_index[source_group_key] = source_group
                source_groups.append(source_group)
            else:
                if source_group["status"] == "RETRIEVED":
                    existing_group["status"] = "RETRIEVED"
                elif (
                    source_group["status"] == "QUERY_SCOPE_UNRESOLVED"
                    and existing_group["status"] == "NOT_RETRIEVED"
                ):
                    existing_group["status"] = "QUERY_SCOPE_UNRESOLVED"
                if source_group.get("query_scope_status") == "QUERY_SCOPE_UNRESOLVED":
                    existing_group["query_scope_status"] = "QUERY_SCOPE_UNRESOLVED"
                    reasons = [
                        str(existing_group.get("query_scope_reason", "")),
                        str(source_group.get("query_scope_reason", "")),
                    ]
                    existing_group["query_scope_reason"] = "；".join(
                        dict.fromkeys(reason for reason in reasons if reason)
                    )
            data.extend({**record, "_query_sources": [source_entry]} for record in formatted)
            if current_match is not None:
                matched_by = current_match

        query_result: dict[str, Any] = {
            "status": (
                "RETRIEVED"
                if data
                else "QUERY_SCOPE_UNRESOLVED"
                if keyword_scope_unresolved
                else "NOT_RETRIEVED"
            ),
            "source": source,
            "source_groups": source_groups,
        }
        if keyword_scope_unresolved:
            query_result["query_scope_status"] = "QUERY_SCOPE_UNRESOLVED"
            query_result["query_scope_reason"] = (
                "当前事实无专属计划，且同一申诉命题中没有可继承的争议实体；"
                "费用明细或检验报告未执行默认全量返回"
            )
        if matched_by is not None:
            query_result["matched_by"] = matched_by
        query_result["data"] = data

        return {
            "一级证据域": primary_domain,
            "二级证据标签": secondary_tag,
            "target_fact": target_fact,
            "query_result": query_result,
        }

    def run(
        self,
        rule_evidence: list[dict[str, Any]],
        medins_id: str,
        mdtrt_id: str,
        rule_id: str | None = None,
        proposition_id: str | None = None,
        include_supplementary: bool = True,
        secondary_mode: str = "always",
        query_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.query_cache.clear()
        query_plan_index, query_plan_errors = safe_index_query_plans(query_plan)
        rule_results: list[dict[str, Any]] = []
        for rule in rule_evidence:
            current_rule_id = str(rule.get("rule_id", ""))
            if rule_id and current_rule_id != rule_id:
                continue

            proposition_results: list[dict[str, Any]] = []
            medical_filter_keys: list[PlanKey] = []
            required_medical_filter_keys: list[PlanKey] = []
            propositions = rule.get("result", {}).get("appeal_propositions", [])
            for proposition in propositions:
                current_proposition_id = str(proposition.get("proposition_id", ""))
                if proposition_id and current_proposition_id != proposition_id:
                    continue

                required_tags = list(proposition.get("required_tags", []))
                tags = list(required_tags)
                if include_supplementary:
                    tags.extend(proposition.get("supplementary_tags", []))
                for tag_index, tag in enumerate(tags):
                    primary_domain = str(tag.get("一级证据域", "")).strip()
                    secondary_tag = str(tag.get("二级证据标签", "")).strip()
                    target_fact = str(tag.get("target_fact", "")).strip()
                    route_groups = self.route_index.get(
                        (primary_domain, secondary_tag),
                        {"PRIMARY": [], "SECONDARY": []},
                    )
                    if any(
                        route.record_type == "病程记录"
                        for route in [
                            *route_groups["PRIMARY"],
                            *route_groups["SECONDARY"],
                        ]
                    ):
                        medical_filter_key = (
                            current_proposition_id,
                            primary_domain,
                            secondary_tag,
                            target_fact,
                        )
                        medical_filter_keys.append(medical_filter_key)
                        if tag_index < len(required_tags):
                            required_medical_filter_keys.append(
                                medical_filter_key
                            )
                proposition_results.append(
                    {
                        "proposition_id": current_proposition_id,
                        "evidence": [
                            self._retrieve_tag(
                                current_proposition_id,
                                tag,
                                medins_id,
                                mdtrt_id,
                                secondary_mode,
                                query_plan_index,
                            )
                            for tag in tags
                        ],
                    }
                )

            if proposition_results:
                rule_result = {
                    "rule_id": current_rule_id,
                    "mdtrt_id": mdtrt_id,
                    "propositions": proposition_results,
                }
                if medical_filter_keys:
                    rule_result["medical_record_filter"] = (
                        filter_rule_medical_records(
                            rule_result,
                            query_plan_index,
                            list(dict.fromkeys(medical_filter_keys)),
                            list(dict.fromkeys(required_medical_filter_keys)),
                        )
                    )
                    if query_plan_errors:
                        rule_result["medical_record_filter"][
                            "plan_validation_errors"
                        ] = query_plan_errors
                rule_results.append(rule_result)

        if not rule_results:
            raise ValueError("没有找到符合筛选条件的规则或申诉命题")
        if len(rule_results) == 1:
            return rule_results[0]
        return {"rules": rule_results}
