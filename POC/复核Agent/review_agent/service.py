from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .client import ModelClient
from .prompt import (
    CITATION_REPAIR_SCHEMA,
    CITATION_REPAIR_SYSTEM_PROMPT,
    build_citation_repair_prompt,
    build_user_prompt,
    load_system_prompt,
)
from .schema import load_model_schema
from .validation import (
    ExpressionParser,
    ReviewValidationError,
    _find_location_values,
    _context_indexes,
    _medical_quote_locations,
    _medical_quote_matches_source,
    _query_sources,
    _shared_structured_data_for_source,
    _source_matches,
    _source_parts,
    _structured_data,
    _validate_evidence_quote,
    evaluate_expression,
    expand_medical_record_content,
    validate_context,
    validate_result,
)


def _structured_quote_locations(node: Any, quote: str) -> list[str]:
    """Return field names whose scalar value exactly matches a structured quote."""
    matches: list[str] = []
    expected = quote.strip()
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, (str, int, float, bool)):
                if str(value).strip() == expected:
                    matches.append(str(key))
            elif isinstance(value, (dict, list)):
                matches.extend(_structured_quote_locations(value, quote))
    elif isinstance(node, list):
        for value in node:
            matches.extend(_structured_quote_locations(value, quote))
    return list(dict.fromkeys(matches))


class ReviewService:
    def __init__(self, client: ModelClient):
        self.client = client
        self.schema = load_model_schema()
        self.metrics = []
        self.citation_repair_attempted = False
        self.citation_degraded_fact_count = 0
        self.citation_repair_error: str | None = None

    @staticmethod
    def load_context(path: Path) -> dict[str, Any]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("复核上下文必须是JSON对象")
        validate_context(payload)
        return payload

    def review(
        self,
        context: dict[str, Any],
        review_round: int = 1,
    ) -> dict[str, Any]:
        if review_round < 1:
            raise ValueError("review_round必须大于等于1")
        validate_context(context)
        self.metrics = []
        self.citation_repair_attempted = False
        self.citation_degraded_fact_count = 0
        self.citation_repair_error = None
        system_prompt = load_system_prompt()
        print("复核上下文校验完成，开始首次复核模型调用", flush=True)
        model_result = self.client.generate(
            system_prompt,
            build_user_prompt(context, review_round),
            self.schema,
        )
        self._capture_metrics()
        print("首次复核模型调用完成，开始整理模型结果", flush=True)
        result = self._expand_model_result(model_result, context, review_round)
        self._fill_medical_document_sources(result, context)
        self._normalize_medical_document_locations(result, context)
        result = self._remove_null_fields(result)
        self._normalize_fact_evidence(result)
        self._normalize_structured_evidence(result, context)
        print("模型结果整理完成，开始证据引用校验", flush=True)
        self._repair_invalid_citations(result, context)
        print("证据引用处理完成，开始按上游表达式计算整体状态", flush=True)
        self._set_computed_overall_status(result)
        self._complete_deterministic_fields(result)
        print("整体状态计算完成，开始最终结果校验", flush=True)
        validate_result(result, context, review_round)
        print("最终结果校验完成", flush=True)
        return result

    @staticmethod
    def _fact_definitions(context: dict[str, Any]) -> dict[str, dict[str, Any]]:
        definitions: dict[str, dict[str, Any]] = {}
        for proposition in context.get("propositions", []):
            if not isinstance(proposition, dict):
                continue
            proposition_id = str(proposition.get("proposition_id", ""))
            for fact_index, fact in enumerate(
                proposition.get("evidence", []),
                start=1,
            ):
                if isinstance(fact, dict):
                    definitions[f"{proposition_id}-F{fact_index:02d}"] = {
                        "proposition_id": proposition_id,
                        "一级证据域": fact.get("一级证据域"),
                        "二级证据标签": fact.get("二级证据标签"),
                        "target_fact": fact.get("target_fact"),
                        "necessity": fact.get("necessity"),
                    }
        return definitions

    @classmethod
    def _expand_model_result(
        cls,
        model_result: dict[str, Any],
        context: dict[str, Any],
        review_round: int,
    ) -> dict[str, Any]:
        definitions = cls._fact_definitions(context)
        decisions: dict[str, dict[str, Any]] = {}
        for decision in model_result.get("fact_reviews", []):
            if not isinstance(decision, dict):
                raise ReviewValidationError("模型fact_reviews项必须是对象")
            fact_id = str(decision.get("fact_id", "")).strip()
            if fact_id not in definitions:
                raise ReviewValidationError(f"模型输出了不存在的fact_id: {fact_id}")
            if fact_id in decisions:
                raise ReviewValidationError(f"模型重复输出fact_id: {fact_id}")
            decisions[fact_id] = decision
        missing_fact_ids = sorted(set(definitions) - set(decisions))
        if missing_fact_ids:
            raise ReviewValidationError(f"模型遗漏fact_id: {missing_fact_ids}")

        fact_reviews = []
        for fact_id, definition in definitions.items():
            decision = decisions[fact_id]
            fact_reviews.append(
                {
                    **definition,
                    "status": decision.get("status"),
                    "evidence": decision.get("evidence", []),
                    "reason": decision.get("reason"),
                }
            )

        supplemental_requests = []
        for request in model_result.get("supplemental_evidence_requests", []):
            if not isinstance(request, dict):
                raise ReviewValidationError("补充证据请求必须是对象")
            fact_id = str(request.get("fact_id", "")).strip()
            definition = definitions.get(fact_id)
            if definition is None:
                raise ReviewValidationError(
                    f"补充证据请求引用了不存在的fact_id: {fact_id}"
                )
            expanded_request = {
                "proposition_id": definition["proposition_id"],
                "target_fact": definition["target_fact"],
                "requested_data": request.get("requested_data"),
                "reason": request.get("reason"),
            }
            if request.get("requested_source"):
                expanded_request["requested_source"] = request["requested_source"]
            supplemental_requests.append(expanded_request)

        logic_basis_quote = next(
            (
                value
                for value in context.get("review_basis", {}).values()
                if isinstance(value, str) and value.strip()
            ),
            "命题逻辑由规则证据Agent提供。",
        )
        result = {
            "rule_id": context["rule_id"],
            "mdtrt_id": context["mdtrt_id"],
            "review_round": review_round,
            "proposition_relation": {
                "status": "CONFIRMED",
                "expression": context.get("logical_expression"),
                "basis_quotes": [logic_basis_quote],
            },
            "fact_reviews": fact_reviews,
            "proposition_reviews": model_result.get("proposition_reviews"),
            "overall_logic_status": "UNKNOWN",
            "review_status": model_result.get("review_status"),
            "case_status": "数据阻断",
            "final_conclusion": {
                "result": "暂无法判定",
                "is_final": False,
                "reason": model_result.get("final_reason"),
            },
            "supplemental_evidence_requests": supplemental_requests,
        }
        if model_result.get("manual_review_reason"):
            result["manual_review_reason"] = model_result["manual_review_reason"]
        return result

    @staticmethod
    def _complete_deterministic_fields(result: dict[str, Any]) -> None:
        relation = result.get("proposition_relation", {})
        relation_status = (
            relation.get("status") if isinstance(relation, dict) else None
        )
        review_status = result.get("review_status")
        overall_status = result.get("overall_logic_status")
        conclusion = result.get("final_conclusion")
        reason = conclusion.get("reason") if isinstance(conclusion, dict) else ""

        if relation_status == "AMBIGUOUS":
            result["review_status"] = "BLOCKED"
            result["case_status"] = "待人工终审"
            result["final_conclusion"] = {
                "result": "待人工终审",
                "is_final": False,
                "reason": reason,
            }
            return

        case_statuses = {
            "CLOSED": "复核完成",
            "OPEN": "待补充证据",
            "BLOCKED": "数据阻断",
            "CONFLICTED": "证据冲突",
        }
        result["case_status"] = case_statuses.get(review_status, "数据阻断")
        if review_status == "CLOSED" and overall_status == "SUPPORTED":
            final_result, is_final = "合规", True
        elif review_status == "CLOSED" and overall_status == "NOT_SUPPORTED":
            final_result, is_final = "违规", True
        elif review_status == "CONFLICTED":
            final_result, is_final = "待人工终审", False
        else:
            final_result, is_final = "暂无法判定", False
        result["final_conclusion"] = {
            "result": final_result,
            "is_final": is_final,
            "reason": reason,
        }

    def _repair_invalid_citations(
        self,
        result: dict[str, Any],
        context: dict[str, Any],
    ) -> None:
        issues = self._collect_citation_issues(result, context)
        if not issues:
            return

        self.citation_repair_attempted = True
        print(
            f"发现{len(issues)}个无法校验的引用，开始引用专项修复模型调用",
            flush=True,
        )
        prompt_issues = [
            self._build_citation_issue_prompt(issue, context)
            for issue in issues
        ]
        try:
            repaired = self.client.generate(
                CITATION_REPAIR_SYSTEM_PROMPT,
                build_citation_repair_prompt(prompt_issues),
                CITATION_REPAIR_SCHEMA,
            )
            self._capture_metrics()
            print("引用专项修复模型调用完成，继续校验引用", flush=True)
            self._apply_citation_repairs(result, issues, repaired)
            self._fill_medical_document_sources(result, context)
            self._normalize_medical_document_locations(result, context)
            self._normalize_structured_evidence(result, context)
            remaining_issues = self._collect_citation_issues(result, context)
        except Exception as exc:
            self.citation_repair_error = str(exc)
            remaining_issues = issues

        if remaining_issues:
            print(
                f"引用专项修复后仍有{len(remaining_issues)}个问题，执行安全降级",
                flush=True,
            )
            self._degrade_invalid_citations(result, remaining_issues)

    @staticmethod
    def _collect_citation_issues(
        result: dict[str, Any],
        context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        (
            _,
            facts,
            documents,
            structured_pool,
            summary_pool,
            fragment_pool,
        ) = _context_indexes(context)
        issues: list[dict[str, Any]] = []
        fact_reviews = result.get("fact_reviews")
        if not isinstance(fact_reviews, list):
            return issues
        for fact_index, fact_review in enumerate(fact_reviews):
            if not isinstance(fact_review, dict):
                continue
            fact_key = (
                str(fact_review.get("proposition_id", "")).strip(),
                str(fact_review.get("一级证据域", "")).strip(),
                str(fact_review.get("二级证据标签", "")).strip(),
                str(fact_review.get("target_fact", "")).strip(),
            )
            fact_context = facts.get(fact_key)
            evidence_items = fact_review.get("evidence")
            if fact_context is None or not isinstance(evidence_items, list):
                continue
            for evidence_index, evidence in enumerate(evidence_items):
                if not isinstance(evidence, dict):
                    continue
                try:
                    _validate_evidence_quote(
                        evidence,
                        fact_context,
                        documents,
                        structured_pool,
                        summary_pool,
                        fragment_pool,
                    )
                except ReviewValidationError as exc:
                    issues.append(
                        {
                            "issue_id": f"CIT-{fact_index + 1:03d}-{evidence_index + 1:03d}",
                            "fact_index": fact_index,
                            "evidence_index": evidence_index,
                            "fact_key": fact_key,
                            "fact_status": fact_review.get("status"),
                            "fact_reason": fact_review.get("reason"),
                            "error": str(exc),
                            "evidence": dict(evidence),
                        }
                    )
        return issues

    @classmethod
    def _build_citation_issue_prompt(
        cls,
        issue: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        _, facts, documents, structured_pool, summary_pool, fragment_pool = (
            _context_indexes(context)
        )
        fact_context = facts[issue["fact_key"]]
        query_result = fact_context.get("query_result", {})
        if not isinstance(query_result, dict):
            query_result = {}
        evidence = issue["evidence"]
        evidence_type = evidence.get("evidence_type")
        if evidence_type == "MEDICAL_DOCUMENT":
            candidates: dict[str, Any] = {
                "medical_documents": cls._medical_citation_candidates(
                    evidence,
                    query_result,
                    documents,
                    fragment_pool,
                )
            }
        else:
            candidates = {
                "structured_sources": _source_parts(_query_sources(query_result)),
                "structured_records": cls._structured_citation_candidates(
                    evidence,
                    query_result,
                    structured_pool,
                    summary_pool,
                ),
            }
        return {
            "issue_id": issue["issue_id"],
            "fact": {
                "proposition_id": issue["fact_key"][0],
                "一级证据域": issue["fact_key"][1],
                "二级证据标签": issue["fact_key"][2],
                "target_fact": issue["fact_key"][3],
                "status": issue.get("fact_status"),
                "reason": issue.get("fact_reason"),
            },
            "validation_error": issue["error"],
            "invalid_evidence": evidence,
            "candidates": candidates,
        }

    @staticmethod
    def _scalar_fields(node: Any) -> list[dict[str, str]]:
        fields: list[dict[str, str]] = []
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, (str, int, float, bool)):
                    fields.append({"location": str(key), "text": str(value)})
                elif isinstance(value, (dict, list)):
                    fields.extend(ReviewService._scalar_fields(value))
        elif isinstance(node, list):
            for value in node:
                fields.extend(ReviewService._scalar_fields(value))
        elif isinstance(node, (str, int, float, bool)):
            fields.append({"location": "recordContent", "text": str(node)})
        return fields

    @classmethod
    def _medical_citation_candidates(
        cls,
        evidence: dict[str, Any],
        query_result: dict[str, Any],
        documents: dict[str, dict[str, Any]],
        fragment_pool: dict[str, dict[str, str]],
    ) -> list[dict[str, Any]]:
        quote = str(evidence.get("quote", "")).strip()
        location = str(evidence.get("location", "")).strip()
        segments = [
            segment.strip()
            for segment in re.split(r"(?:\.\.\.|…+)", quote)
            if segment.strip()
        ]
        preferred_ids = [
            str(document_id)
            for document_id in query_result.get("document_refs", [])
            if str(document_id) in documents
        ]
        original_id = str(evidence.get("document_id", ""))
        if original_id in documents:
            preferred_ids.insert(0, original_id)
        matching_ids: list[str] = []
        for document_id, document in documents.items():
            content = expand_medical_record_content(
                document.get("recordContent"),
                fragment_pool,
            )
            if any(_medical_quote_locations(content, segment) for segment in segments):
                matching_ids.append(document_id)
        candidate_ids = list(dict.fromkeys(preferred_ids + matching_ids))[:8]
        candidates: list[dict[str, Any]] = []
        for document_id in candidate_ids:
            document = documents[document_id]
            content = expand_medical_record_content(
                document.get("recordContent"),
                fragment_pool,
            )
            fields = cls._scalar_fields(content)
            prioritized = [
                field
                for field in fields
                if field["location"] == location
                or any(segment in field["text"] for segment in segments)
            ]
            selected_fields = list(
                {
                    (field["location"], field["text"]): field
                    for field in (prioritized or fields)
                }.values()
            )[:6]
            candidates.append(
                {
                    "document_id": document_id,
                    "source": document.get("source"),
                    "fields": selected_fields,
                }
            )
        return candidates

    @classmethod
    def _structured_citation_candidates(
        cls,
        evidence: dict[str, Any],
        query_result: dict[str, Any],
        structured_pool: dict[str, dict[str, Any]],
        summary_pool: dict[str, dict[str, Any]],
    ) -> list[Any]:
        records = _structured_data(query_result, structured_pool, summary_pool)
        if isinstance(records, dict):
            records = [records]
        if not isinstance(records, list):
            records = []
        for source in _source_parts(evidence.get("source")):
            records.extend(
                _shared_structured_data_for_source(
                    source,
                    structured_pool,
                    summary_pool,
                )
            )
        quote = str(evidence.get("quote", "")).strip()
        location = str(evidence.get("location", "")).strip()

        def priority(record: Any) -> tuple[int, str]:
            fields = cls._scalar_fields(record)
            if any(field["text"] == quote for field in fields):
                return (0, json.dumps(record, ensure_ascii=False, sort_keys=True))
            if any(field["location"] == location for field in fields):
                return (1, json.dumps(record, ensure_ascii=False, sort_keys=True))
            return (2, json.dumps(record, ensure_ascii=False, sort_keys=True))

        unique_records: dict[str, Any] = {}
        for record in records:
            if not isinstance(record, (dict, list)):
                continue
            compact_record = cls._remove_null_fields(record)
            key = json.dumps(compact_record, ensure_ascii=False, sort_keys=True)
            unique_records[key] = compact_record
        return sorted(unique_records.values(), key=priority)[:40]

    @staticmethod
    def _apply_citation_repairs(
        result: dict[str, Any],
        issues: list[dict[str, Any]],
        repaired: dict[str, Any],
    ) -> None:
        repairs = repaired.get("repairs")
        if not isinstance(repairs, list):
            raise ReviewValidationError("引用专项修复未返回repairs数组")
        allowed_issue_ids = {issue["issue_id"] for issue in issues}
        replacements: dict[str, list[dict[str, Any]]] = {}
        for repair in repairs:
            if not isinstance(repair, dict):
                raise ReviewValidationError("引用专项修复项必须是对象")
            issue_id = repair.get("issue_id")
            evidence_items = repair.get("evidence")
            if issue_id not in allowed_issue_ids or issue_id in replacements:
                raise ReviewValidationError(f"引用专项修复issue_id无效或重复: {issue_id}")
            if not isinstance(evidence_items, list) or not all(
                isinstance(item, dict) for item in evidence_items
            ):
                raise ReviewValidationError(f"{issue_id}.evidence必须是对象数组")
            replacements[issue_id] = [dict(item) for item in evidence_items]

        issue_by_position = {
            (issue["fact_index"], issue["evidence_index"]): issue
            for issue in issues
        }
        for fact_index, fact_review in enumerate(result.get("fact_reviews", [])):
            if not isinstance(fact_review, dict):
                continue
            evidence_items = fact_review.get("evidence")
            if not isinstance(evidence_items, list):
                continue
            normalized_items: list[Any] = []
            for evidence_index, evidence in enumerate(evidence_items):
                issue = issue_by_position.get((fact_index, evidence_index))
                if issue is None:
                    normalized_items.append(evidence)
                    continue
                replacement_items = replacements.get(issue["issue_id"], [])
                if replacement_items:
                    normalized_items.extend(replacement_items)
                else:
                    normalized_items.append(evidence)
            fact_review["evidence"] = normalized_items

    def _degrade_invalid_citations(
        self,
        result: dict[str, Any],
        issues: list[dict[str, Any]],
    ) -> None:
        affected_fact_indexes = {
            int(issue["fact_index"])
            for issue in issues
            if isinstance(issue.get("fact_index"), int)
        }
        affected_proposition_ids: set[str] = set()
        fact_reviews = result.get("fact_reviews", [])
        for fact_index in affected_fact_indexes:
            if not (0 <= fact_index < len(fact_reviews)):
                continue
            fact_review = fact_reviews[fact_index]
            if not isinstance(fact_review, dict):
                continue
            proposition_id = fact_review.get("proposition_id")
            if isinstance(proposition_id, str):
                affected_proposition_ids.add(proposition_id)
            fact_review["status"] = "UNKNOWN"
            fact_review["evidence"] = []
            fact_review["reason"] = "引用无法通过原始数据校验，当前证据不足。"

        for proposition_review in result.get("proposition_reviews", []):
            if not isinstance(proposition_review, dict):
                continue
            if proposition_review.get("proposition_id") in affected_proposition_ids:
                proposition_review["status"] = "UNKNOWN"
                proposition_review["reason"] = (
                    "该命题存在关键证据引用无法通过原始数据校验，暂无法判定。"
                )

        self.citation_degraded_fact_count = len(affected_fact_indexes)
        self._set_computed_overall_status(result)
        self._align_result_after_citation_degradation(result)

    @staticmethod
    def _align_result_after_citation_degradation(result: dict[str, Any]) -> None:
        relation = result.get("proposition_relation", {})
        if isinstance(relation, dict) and relation.get("status") == "AMBIGUOUS":
            result["review_status"] = "BLOCKED"
            result["case_status"] = "待人工终审"
            result["final_conclusion"] = {
                "result": "待人工终审",
                "is_final": False,
                "reason": "命题关系不明确，且部分证据引用无法通过原始数据校验。",
            }
            result["manual_review_reason"] = "命题关系或证据引用需人工核验。"
            result["supplemental_evidence_requests"] = []
            return

        overall_status = result.get("overall_logic_status")
        if overall_status == "SUPPORTED":
            result["review_status"] = "CLOSED"
            result["case_status"] = "复核完成"
            result["final_conclusion"] = {
                "result": "合规",
                "is_final": True,
                "reason": "部分证据引用已降级，但其余命题仍足以确定整体逻辑为支持。",
            }
        elif overall_status == "NOT_SUPPORTED":
            result["review_status"] = "CLOSED"
            result["case_status"] = "复核完成"
            result["final_conclusion"] = {
                "result": "违规",
                "is_final": True,
                "reason": "部分证据引用已降级，但其余命题仍足以确定整体逻辑为不支持。",
            }
        elif overall_status == "CONFLICTED":
            result["review_status"] = "CONFLICTED"
            result["case_status"] = "证据冲突"
            result["final_conclusion"] = {
                "result": "待人工终审",
                "is_final": False,
                "reason": "现有命题仍存在证据冲突，需人工终审。",
            }
            result["manual_review_reason"] = "证据冲突且部分引用无法通过校验。"
        else:
            result["review_status"] = "BLOCKED"
            result["case_status"] = "数据阻断"
            result["final_conclusion"] = {
                "result": "暂无法判定",
                "is_final": False,
                "reason": "关键证据引用无法通过原始数据校验，已安全降级。",
            }
        result["supplemental_evidence_requests"] = []

    @classmethod
    def _remove_null_fields(cls, value: Any) -> Any:
        """Omit empty JSON fields without changing non-null review content."""
        if isinstance(value, dict):
            return {
                key: cls._remove_null_fields(item)
                for key, item in value.items()
                if item is not None or key == "document_id"
            }
        if isinstance(value, list):
            return [cls._remove_null_fields(item) for item in value if item is not None]
        return value

    @staticmethod
    def _normalize_fact_evidence(result: dict[str, Any]) -> None:
        """Treat an omitted evidence list as empty; validation still enforces evidence by status."""
        fact_reviews = result.get("fact_reviews")
        if not isinstance(fact_reviews, list):
            return
        for fact_review in fact_reviews:
            if isinstance(fact_review, dict) and "evidence" not in fact_review:
                fact_review["evidence"] = []

    @staticmethod
    def _normalize_structured_evidence(
        result: dict[str, Any],
        context: dict[str, Any],
    ) -> None:
        """Split combined structured quotes into real field values when safe."""
        fact_index: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for proposition in context.get("propositions", []):
            if not isinstance(proposition, dict):
                continue
            proposition_id = str(proposition.get("proposition_id", ""))
            for fact in proposition.get("evidence", []):
                if not isinstance(fact, dict):
                    continue
                key = (
                    proposition_id,
                    str(fact.get("一级证据域", "")),
                    str(fact.get("二级证据标签", "")),
                    str(fact.get("target_fact", "")),
                )
                fact_index[key] = fact

        structured_pool = context.get("structured_data_pool", {})
        summary_pool = context.get("expense_summary_pool", {})
        if not isinstance(structured_pool, dict):
            structured_pool = {}
        if not isinstance(summary_pool, dict):
            summary_pool = {}

        for fact_review in result.get("fact_reviews", []):
            if not isinstance(fact_review, dict):
                continue
            key = (
                str(fact_review.get("proposition_id", "")),
                str(fact_review.get("一级证据域", "")),
                str(fact_review.get("二级证据标签", "")),
                str(fact_review.get("target_fact", "")),
            )
            fact_context = fact_index.get(key)
            evidence_items = fact_review.get("evidence")
            if fact_context is None or not isinstance(evidence_items, list):
                continue
            query_result = fact_context.get("query_result", {})
            if not isinstance(query_result, dict):
                continue
            normalized_items: list[Any] = []
            structured_data = _structured_data(query_result, structured_pool, summary_pool)
            for evidence in evidence_items:
                if not isinstance(evidence, dict) or evidence.get("evidence_type") != "STRUCTURED":
                    normalized_items.append(evidence)
                    continue
                source = evidence.get("source")
                location = evidence.get("location")
                quote = evidence.get("quote")
                if not all(isinstance(value, str) and value.strip() for value in (source, location, quote)):
                    normalized_items.append(evidence)
                    continue
                values: list[Any] = []
                source_data: list[Any] = []
                if _source_matches(source, _query_sources(query_result)):
                    values.extend(_find_location_values(structured_data, location))
                    source_data.append(structured_data)
                shared_source_data = _shared_structured_data_for_source(
                    source,
                    structured_pool,
                    summary_pool,
                )
                values.extend(_find_location_values(shared_source_data, location))
                source_data.append(shared_source_data)
                value_set = {str(value).strip() for value in values if value is not None}
                if quote.strip() in value_set:
                    normalized_items.append(evidence)
                    continue
                candidate_locations = _structured_quote_locations(source_data, quote)
                if len(candidate_locations) == 1:
                    normalized_evidence = dict(evidence)
                    normalized_evidence["location"] = candidate_locations[0]
                    normalized_items.append(normalized_evidence)
                    continue
                parts = [
                    part.strip()
                    for part in re.split(r"[,，、;；|\n]+", quote)
                    if part.strip()
                ]
                if len(parts) > 1 and all(part in value_set for part in parts):
                    for part in parts:
                        split_evidence = dict(evidence)
                        split_evidence["quote"] = part
                        normalized_items.append(split_evidence)
                else:
                    normalized_items.append(evidence)
            fact_review["evidence"] = normalized_items

    @staticmethod
    def _fill_medical_document_sources(
        result: dict[str, Any],
        context: dict[str, Any],
    ) -> None:
        """Derive medical evidence sources from their document IDs."""
        documents = {
            str(document.get("document_id")): document
            for document in context.get("medical_documents", [])
            if isinstance(document, dict) and document.get("document_id")
        }
        fact_reviews = result.get("fact_reviews")
        if not isinstance(fact_reviews, list):
            return
        for fact_review in fact_reviews:
            if not isinstance(fact_review, dict):
                continue
            evidence_items = fact_review.get("evidence")
            if not isinstance(evidence_items, list):
                continue
            for evidence in evidence_items:
                if not isinstance(evidence, dict):
                    continue
                if evidence.get("evidence_type") != "MEDICAL_DOCUMENT":
                    continue
                document = documents.get(str(evidence.get("document_id", "")))
                if document is not None and document.get("source"):
                    evidence["source"] = document["source"]

    @staticmethod
    def _normalize_medical_document_locations(
        result: dict[str, Any],
        context: dict[str, Any],
    ) -> None:
        """Correct or split medical evidence only when every quote is verifiable."""
        _, _, documents, _, _, fragment_pool = _context_indexes(context)
        fact_index: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for proposition in context.get("propositions", []):
            if not isinstance(proposition, dict):
                continue
            proposition_id = str(proposition.get("proposition_id", ""))
            for fact in proposition.get("evidence", []):
                if not isinstance(fact, dict):
                    continue
                fact_index[
                    (
                        proposition_id,
                        str(fact.get("一级证据域", "")),
                        str(fact.get("二级证据标签", "")),
                        str(fact.get("target_fact", "")),
                    )
                ] = fact
        fact_reviews = result.get("fact_reviews")
        if not isinstance(fact_reviews, list):
            return
        for fact_review in fact_reviews:
            if not isinstance(fact_review, dict):
                continue
            fact_context = fact_index.get(
                (
                    str(fact_review.get("proposition_id", "")),
                    str(fact_review.get("一级证据域", "")),
                    str(fact_review.get("二级证据标签", "")),
                    str(fact_review.get("target_fact", "")),
                )
            )
            query_result = (
                fact_context.get("query_result", {})
                if isinstance(fact_context, dict)
                else {}
            )
            document_refs = (
                query_result.get("document_refs", [])
                if isinstance(query_result, dict)
                else []
            )
            preferred_document_ids = [
                str(document_id)
                for document_id in document_refs
                if str(document_id) in documents
            ]
            allowed_document_ids = preferred_document_ids + [
                document_id
                for document_id in documents
                if document_id not in preferred_document_ids
            ]
            evidence_items = fact_review.get("evidence")
            if not isinstance(evidence_items, list):
                continue
            normalized_items: list[Any] = []
            for evidence in evidence_items:
                if not isinstance(evidence, dict):
                    normalized_items.append(evidence)
                    continue
                if evidence.get("evidence_type") != "MEDICAL_DOCUMENT":
                    normalized_items.append(evidence)
                    continue
                document = documents.get(str(evidence.get("document_id", "")))
                quote = evidence.get("quote")
                if document is None or not isinstance(quote, str) or not quote.strip():
                    normalized_items.append(evidence)
                    continue
                record_content = expand_medical_record_content(
                    document.get("recordContent"),
                    fragment_pool,
                )
                location = evidence.get("location")
                current_values = (
                    _find_location_values(record_content, location)
                    if isinstance(location, str) and location
                    else []
                )
                if any(
                    _medical_quote_matches_source(quote, str(value))
                    for value in current_values
                ):
                    normalized_items.append(evidence)
                    continue
                candidate_locations = _medical_quote_locations(record_content, quote)
                if len(candidate_locations) == 1:
                    normalized_evidence = dict(evidence)
                    normalized_evidence["location"] = candidate_locations[0]
                    normalized_items.append(normalized_evidence)
                    continue

                whole_quote_candidates: list[tuple[str, str]] = []
                for candidate_document_id in allowed_document_ids:
                    candidate_document = documents[candidate_document_id]
                    candidate_content = expand_medical_record_content(
                        candidate_document.get("recordContent"),
                        fragment_pool,
                    )
                    whole_quote_candidates.extend(
                        (candidate_document_id, candidate_location)
                        for candidate_location in _medical_quote_locations(
                            candidate_content,
                            quote,
                        )
                    )
                whole_quote_candidates = list(dict.fromkeys(whole_quote_candidates))
                if len(whole_quote_candidates) == 1:
                    candidate_document_id, candidate_location = whole_quote_candidates[0]
                    normalized_evidence = dict(evidence)
                    normalized_evidence["document_id"] = candidate_document_id
                    normalized_evidence["location"] = candidate_location
                    candidate_source = documents[candidate_document_id].get("source")
                    if candidate_source:
                        normalized_evidence["source"] = candidate_source
                    normalized_items.append(normalized_evidence)
                    continue

                segments = [
                    segment.strip()
                    for segment in re.split(r"(?:\.\.\.|…+)", quote)
                    if segment.strip()
                ]
                if len(segments) < 2:
                    normalized_items.append(evidence)
                    continue

                current_document_id = str(evidence.get("document_id", ""))
                grouped_segments: dict[tuple[str, str], list[str]] = {}
                can_split = True
                for segment in segments:
                    segment_candidates: list[tuple[str, str]] = []
                    for candidate_document_id in allowed_document_ids:
                        candidate_document = documents[candidate_document_id]
                        candidate_content = expand_medical_record_content(
                            candidate_document.get("recordContent"),
                            fragment_pool,
                        )
                        segment_candidates.extend(
                            (candidate_document_id, candidate_location)
                            for candidate_location in _medical_quote_locations(
                                candidate_content,
                                segment,
                            )
                        )
                    segment_candidates = list(dict.fromkeys(segment_candidates))
                    current_pair = (
                        current_document_id,
                        location if isinstance(location, str) else "",
                    )
                    current_document_candidates = [
                        candidate
                        for candidate in segment_candidates
                        if candidate[0] == current_document_id
                    ]
                    if current_pair in segment_candidates:
                        selected_pair = current_pair
                    elif len(current_document_candidates) == 1:
                        selected_pair = current_document_candidates[0]
                    elif len(segment_candidates) == 1:
                        selected_pair = segment_candidates[0]
                    else:
                        can_split = False
                        break
                    grouped_segments.setdefault(selected_pair, []).append(segment)

                if not can_split or len(grouped_segments) < 2:
                    normalized_items.append(evidence)
                    continue

                split_items: list[dict[str, Any]] = []
                for (
                    selected_document_id,
                    selected_location,
                ), selected_segments in grouped_segments.items():
                    split_quote = "...".join(selected_segments)
                    selected_document = documents[selected_document_id]
                    selected_content = expand_medical_record_content(
                        selected_document.get("recordContent"),
                        fragment_pool,
                    )
                    source_values = _find_location_values(
                        selected_content,
                        selected_location,
                    )
                    if not any(
                        _medical_quote_matches_source(split_quote, str(value))
                        for value in source_values
                    ):
                        can_split = False
                        break
                    split_evidence = dict(evidence)
                    split_evidence["document_id"] = selected_document_id
                    split_evidence["location"] = selected_location
                    split_evidence["quote"] = split_quote
                    selected_source = selected_document.get("source")
                    if selected_source:
                        split_evidence["source"] = selected_source
                    split_items.append(split_evidence)

                normalized_items.extend(split_items if can_split else [evidence])
            fact_review["evidence"] = normalized_items

    @staticmethod
    def _set_computed_overall_status(result: dict[str, Any]) -> None:
        """Use deterministic program logic for the overall proposition result."""
        relation = result.get("proposition_relation")
        if not isinstance(relation, dict):
            return
        if relation.get("status") != "CONFIRMED":
            result["overall_logic_status"] = "UNKNOWN"
            return
        expression = relation.get("expression")
        reviews = result.get("proposition_reviews")
        if not isinstance(expression, str) or not isinstance(reviews, list):
            return
        proposition_statuses: dict[str, str] = {}
        for review in reviews:
            if not isinstance(review, dict):
                return
            proposition_id = review.get("proposition_id")
            status = review.get("status")
            if not isinstance(proposition_id, str) or not isinstance(status, str):
                return
            proposition_statuses[proposition_id] = status
        parsed_expression = ExpressionParser(expression).parse()
        result["overall_logic_status"] = evaluate_expression(
            parsed_expression,
            proposition_statuses,
        )

    def _capture_metrics(self) -> None:
        metrics = getattr(self.client, "last_metrics", None)
        if metrics is not None:
            self.metrics.append(metrics)

    @staticmethod
    def write_result(path: Path, result: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_suffix(path.suffix + ".tmp")
        result = ReviewService._remove_null_fields(result)
        temporary_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(path)
