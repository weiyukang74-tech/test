from __future__ import annotations

import json
import re
import unicodedata
from typing import Any


PlanKey = tuple[str, str, str, str]

GENERIC_MEDICAL_RECORD_TERMS = {
    "患者",
    "病历",
    "是否",
    "相关",
    "本次",
    "检查",
}


def is_progress_record(record: dict[str, object]) -> bool:
    return str(record.get("recordType", "")).strip() == "病程记录"


def _flatten_content_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        flattened: list[str] = []
        for item in value.values():
            flattened.extend(_flatten_content_values(item))
        return flattened
    if isinstance(value, (list, tuple, set)):
        flattened = []
        for item in value:
            flattened.extend(_flatten_content_values(item))
        return flattened
    return []


def _normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _term_matches(text: str, term: str) -> bool:
    normalized_term = _normalize_text(term.strip())
    if not normalized_term:
        return False
    if re.fullmatch(r"[a-z0-9]+", normalized_term):
        pattern = rf"(?<![a-z0-9]){re.escape(normalized_term)}(?![a-z0-9])"
        return re.search(pattern, text) is not None
    return normalized_term in text


def match_record_content(record_content: object, terms: list[str]) -> list[str]:
    normalized_content = _normalize_text(
        "\n".join(_flatten_content_values(record_content))
    )
    matched: list[str] = []
    for raw_term in terms:
        term = str(raw_term).strip()
        if term and term not in matched and _term_matches(normalized_content, term):
            matched.append(term)
    return matched


def medical_record_identity(record: dict[str, object]) -> str:
    canonical = {
        "medinsId": record.get("medinsId"),
        "mdtrtId": record.get("mdtrtId"),
        "recordName": record.get("recordName"),
        "recordType": record.get("recordType"),
        "updateTime": record.get("updateTime"),
        "recordContent": record.get("recordContent"),
    }
    return json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _source_codes(value: object) -> set[str]:
    return set(
        re.findall(
            r"DS-(?:S|M-(?:IP|OP))-\d{3}",
            str(value or ""),
            flags=re.IGNORECASE,
        )
    )


def _valid_plan_terms(plan: dict[str, Any] | None) -> list[str] | None:
    if not isinstance(plan, dict):
        return None
    raw_terms = plan.get("record_search_terms")
    if not isinstance(raw_terms, list) or not 1 <= len(raw_terms) <= 5:
        return None
    if any(not isinstance(term, str) or not term.strip() for term in raw_terms):
        return None
    terms = list(dict.fromkeys(term.strip() for term in raw_terms))
    return terms or None


def _valid_plan_sources(plan: dict[str, Any] | None) -> set[str] | None:
    if not isinstance(plan, dict):
        return None
    raw_sources = plan.get("medical_record_sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        return None
    source_codes: set[str] = set()
    for raw_source in raw_sources:
        if not isinstance(raw_source, str) or not raw_source.strip():
            return None
        current_codes = _source_codes(raw_source)
        if not current_codes:
            return None
        source_codes.update(current_codes)
    return source_codes or None


def is_usable_medical_record_plan(plan: dict[str, Any] | None) -> bool:
    return _valid_plan_terms(plan) is not None and _valid_plan_sources(plan) is not None


def _match_metadata(
    matches: list[tuple[PlanKey, dict[str, Any], list[str]]],
) -> dict[str, Any]:
    matched_propositions: list[str] = []
    matched_evidence_tags: list[dict[str, str]] = []
    matched_terms: list[str] = []
    for key, plan, terms in matches:
        proposition_id, primary_domain, secondary_tag, target_fact = key
        if proposition_id not in matched_propositions:
            matched_propositions.append(proposition_id)
        fact = {}
        supports_facts = plan.get("supports_facts")
        if isinstance(supports_facts, list) and supports_facts:
            fact = supports_facts[0] if isinstance(supports_facts[0], dict) else {}
        evidence_tag = {
            "proposition_id": str(
                fact.get("proposition_id", proposition_id)
            ).strip(),
            "一级证据域": str(fact.get("一级证据域", primary_domain)).strip(),
            "二级证据标签": str(
                fact.get("二级证据标签", secondary_tag)
            ).strip(),
            "target_fact": str(fact.get("target_fact", target_fact)).strip(),
        }
        if evidence_tag not in matched_evidence_tags:
            matched_evidence_tags.append(evidence_tag)
        for term in terms:
            if term not in matched_terms:
                matched_terms.append(term)
    return {
        "matched_propositions": matched_propositions,
        "matched_evidence_tags": matched_evidence_tags,
        "matched_terms": matched_terms,
    }


def filter_rule_medical_records(
    rule_result: dict[str, Any],
    plan_index: dict[PlanKey, dict[str, Any]],
    medical_filter_keys: list[PlanKey],
    required_evidence_keys: list[PlanKey],
) -> dict[str, Any]:
    candidate_records: dict[str, dict[str, object]] = {}
    for proposition in rule_result.get("propositions", []):
        for evidence in proposition.get("evidence", []):
            query_result = evidence.get("query_result", {})
            for record in query_result.get("data", []):
                if isinstance(record, dict) and is_progress_record(record):
                    candidate_records.setdefault(
                        medical_record_identity(record),
                        record,
                    )

    blocked_terms = {
        _normalize_text(term)
        for term in (
            *GENERIC_MEDICAL_RECORD_TERMS,
            *(key[2] for key in medical_filter_keys),
        )
        if term
    }
    valid_plans: list[tuple[PlanKey, dict[str, Any], list[str]]] = []
    ignored_search_terms: list[dict[str, Any]] = []
    for key in medical_filter_keys:
        plan = plan_index.get(key)
        terms = _valid_plan_terms(plan)
        if terms is None or plan is None or _valid_plan_sources(plan) is None:
            continue
        usable_terms: list[str] = []
        ignored_terms: list[str] = []
        for term in terms:
            if _normalize_text(term) in blocked_terms:
                ignored_terms.append(term)
            else:
                usable_terms.append(term)
        if ignored_terms:
            ignored_search_terms.append(
                {
                    "proposition_id": key[0],
                    "一级证据域": key[1],
                    "二级证据标签": key[2],
                    "terms": ignored_terms,
                }
            )
        if usable_terms:
            valid_plans.append((key, plan, usable_terms))
    required_matching_keys = set(required_evidence_keys)

    base_stats = {
        "enabled": False,
        "filter_applied": False,
        "candidate_document_count": len(candidate_records),
        "selected_document_count": len(candidate_records),
        "filter_fallback": False,
        "fallback_reason": None,
        "planned_evidence_count": len(valid_plans),
        "matched_evidence_count": 0,
        "required_planned_evidence_count": len(required_matching_keys),
        "required_matched_evidence_count": 0,
    }
    if ignored_search_terms:
        base_stats["ignored_search_terms"] = ignored_search_terms
    if len(valid_plans) != len(medical_filter_keys):
        base_stats["filter_fallback"] = True
        base_stats["fallback_reason"] = "INCOMPLETE_FILTER_PLAN"
        return base_stats
    base_stats["enabled"] = True
    if not candidate_records:
        base_stats["selected_document_count"] = 0
        return base_stats

    matches_by_identity: dict[
        str,
        list[tuple[PlanKey, dict[str, Any], list[str]]],
    ] = {}
    matched_plan_keys: set[PlanKey] = set()
    for identity, record in candidate_records.items():
        record_matches: list[tuple[PlanKey, dict[str, Any], list[str]]] = []
        for key, plan, terms in valid_plans:
            matched_terms = match_record_content(record.get("recordContent"), terms)
            if not matched_terms:
                continue
            record_matches.append((key, plan, matched_terms))
            matched_plan_keys.add(key)
        if record_matches:
            matches_by_identity[identity] = record_matches

    if not matches_by_identity:
        base_stats["filter_fallback"] = True
        base_stats["fallback_reason"] = "NO_KEYWORD_MATCH"
        return base_stats


    matched_required_plan_keys = matched_plan_keys & required_matching_keys
    base_stats["matched_evidence_count"] = len(matched_plan_keys)
    base_stats["required_matched_evidence_count"] = len(
        matched_required_plan_keys
    )
    if matched_required_plan_keys != required_matching_keys:
        base_stats["filter_fallback"] = True
        base_stats["fallback_reason"] = "INCOMPLETE_FILTER_COVERAGE"
        return base_stats

    unmatched_supplementary_keys = {
        key
        for key, _, _ in valid_plans
        if key not in matched_plan_keys and key not in required_matching_keys
    }
    retained_identities: set[str] = set()
    supplementary_evidence_fallbacks: list[dict[str, Any]] = []
    for proposition in rule_result.get("propositions", []):
        proposition_id = str(proposition.get("proposition_id", ""))
        for evidence in proposition.get("evidence", []):
            evidence_key = (
                proposition_id,
                str(evidence.get("一级证据域", "")).strip(),
                str(evidence.get("二级证据标签", "")).strip(),
                str(evidence.get("target_fact", "")).strip(),
            )
            keep_unmatched = evidence_key in unmatched_supplementary_keys
            query_result = evidence.get("query_result", {})
            filtered_records: list[dict[str, object]] = []
            selected_identities: set[str] = set()
            selected_records: dict[str, dict[str, object]] = {}
            for record in query_result.get("data", []):
                if not isinstance(record, dict) or not is_progress_record(record):
                    filtered_records.append(record)
                    continue
                identity = medical_record_identity(record)
                record_matches = matches_by_identity.get(identity)
                if identity in selected_identities:
                    previous = selected_records[identity]
                    for source_entry in record.get("_query_sources", []):
                        source_entries = previous.setdefault("_query_sources", [])
                        if source_entry not in source_entries:
                            source_entries.append(source_entry)
                    continue
                if record_matches is None and not keep_unmatched:
                    continue
                selected_identities.add(identity)
                selected_record = dict(record)
                if "_query_sources" in record:
                    selected_record["_query_sources"] = list(record["_query_sources"])
                selected_records[identity] = selected_record
                if record_matches is None:
                    filtered_records.append(selected_record)
                    continue
                selected_record["_medical_record_filter_match"] = _match_metadata(
                    record_matches
                )
                filtered_records.append(selected_record)
            query_result["data"] = filtered_records
            retained_identities.update(selected_identities)
            if keep_unmatched and selected_identities:
                supplementary_evidence_fallbacks.append(
                    {
                        "proposition_id": evidence_key[0],
                        "一级证据域": evidence_key[1],
                        "二级证据标签": evidence_key[2],
                        "fallback_reason": "NO_KEYWORD_MATCH",
                        "retained_document_count": len(selected_identities),
                    }
                )

    base_stats["selected_document_count"] = len(retained_identities)
    base_stats["filter_applied"] = True
    if supplementary_evidence_fallbacks:
        base_stats["supplementary_evidence_fallbacks"] = (
            supplementary_evidence_fallbacks
        )
    return base_stats
