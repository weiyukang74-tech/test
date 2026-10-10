from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .expense_summary import build_expense_summary, should_summarize
from .field_dictionary import StandardViewFieldDictionary
from .medical_compression import group_medical_documents
from .medical_record_filter import medical_record_identity


TagKey = tuple[str, str, str]


def _remove_empty_values(value: Any) -> Any:
    """Remove empty values from review-context structured data only."""
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            cleaned_item = _remove_empty_values(item)
            if cleaned_item is None:
                continue
            if isinstance(cleaned_item, str) and not cleaned_item.strip():
                continue
            if isinstance(cleaned_item, (dict, list)) and not cleaned_item:
                continue
            cleaned[key] = cleaned_item
        return cleaned
    if isinstance(value, list):
        cleaned_list: list[Any] = []
        for item in value:
            cleaned_item = _remove_empty_values(item)
            if cleaned_item is None:
                continue
            if isinstance(cleaned_item, str) and not cleaned_item.strip():
                continue
            if isinstance(cleaned_item, (dict, list)) and not cleaned_item:
                continue
            cleaned_list.append(cleaned_item)
        return cleaned_list
    return value


def _remove_evidence_labels(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _remove_evidence_labels(item)
            for key, item in value.items()
            if key not in {"一级证据域", "二级证据标签"}
        }
    if isinstance(value, list):
        return [_remove_evidence_labels(item) for item in value]
    return value


class MedicalDocumentCollector:
    def __init__(self) -> None:
        self.documents: list[dict[str, Any]] = []
        self.document_ids: dict[str, str] = {}
        self.documents_by_identity: dict[str, dict[str, Any]] = {}
        self.source_counts: defaultdict[tuple[str, str], int] = defaultdict(int)

    def add(self, source: str, record: dict[str, Any]) -> str:
        filter_match = record.get("_medical_record_filter_match")
        identity = medical_record_identity(record)
        existing_id = self.document_ids.get(identity)
        if existing_id is not None:
            existing_document = self.documents_by_identity[identity]
            document_source = _select_record_source(source, record)
            existing_document["source"] = _merge_sources(
                str(existing_document.get("source", "")),
                document_source,
            )
            if isinstance(filter_match, dict):
                _merge_filter_match(existing_document, filter_match)
            return existing_id

        document_source = _select_record_source(source, record)
        source_code = document_source.split(maxsplit=1)[0] if document_source else "UNKNOWN"
        mdtrt_id = str(record.get("mdtrtId", "UNKNOWN"))
        counter_key = (source_code, mdtrt_id)
        self.source_counts[counter_key] += 1
        document_id = (
            f"DOC-{source_code}-{mdtrt_id}-{self.source_counts[counter_key]:02d}"
        )
        document = {
            "document_id": document_id,
            "source": document_source,
            "recordName": record.get("recordName"),
            "recordType": record.get("recordType"),
            "updateTime": record.get("updateTime"),
            "recordContent": record.get("recordContent"),
        }
        if isinstance(filter_match, dict):
            _merge_filter_match(document, filter_match)
        self.document_ids[identity] = document_id
        self.documents_by_identity[identity] = document
        self.documents.append(document)
        return document_id


def _merge_sources(existing: str, incoming: str) -> str:
    sources: list[str] = []
    for value in (existing, incoming):
        for source in (part.strip() for part in value.split("、")):
            if source and source not in sources:
                sources.append(source)
    return "、".join(sources)


def _merge_filter_match(
    document: dict[str, Any],
    filter_match: dict[str, Any],
) -> None:
    for field in (
        "matched_propositions",
        "matched_evidence_tags",
        "matched_terms",
    ):
        incoming = filter_match.get(field)
        if not isinstance(incoming, list):
            continue
        existing = document.setdefault(field, [])
        if not isinstance(existing, list):
            existing = []
            document[field] = existing
        for item in incoming:
            if item not in existing:
                existing.append(item)


class StructuredDataCollector:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.record_ids: dict[str, str] = {}

    def add(self, source: str, record: dict[str, Any]) -> str:
        compact_record = _remove_empty_values(record)
        identity = json.dumps(compact_record, ensure_ascii=False, sort_keys=True)
        existing_id = self.record_ids.get(identity)
        if existing_id is not None:
            return existing_id

        source_code = source.strip().split(maxsplit=1)[0] if source.strip() else "UNKNOWN"
        mdtrt_id = str(compact_record.get("mdtrtId", compact_record.get("mdtrt_id", "UNKNOWN")))
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
        record_id = f"SD-{source_code}-{mdtrt_id}-{digest}"
        self.record_ids[identity] = record_id
        self.records[record_id] = compact_record
        return record_id


class ExpenseSummaryCollector:
    def __init__(self) -> None:
        self.summaries: dict[str, dict[str, Any]] = {}
        self.summary_ids: dict[str, str] = {}

    def add(
        self,
        source: str,
        summary: dict[str, Any],
        records: list[dict[str, Any]],
    ) -> str:
        canonical_summary = _remove_empty_values(summary)
        canonical_summary.pop("target_fact", None)
        identity = json.dumps(
            {"source": source, "summary": canonical_summary},
            ensure_ascii=False,
            sort_keys=True,
        )
        existing_id = self.summary_ids.get(identity)
        if existing_id is not None:
            return existing_id
        source_code = source.strip().split(maxsplit=1)[0] if source.strip() else "UNKNOWN"
        mdtrt_id = str(records[0].get("mdtrtId", records[0].get("mdtrt_id", "UNKNOWN"))) if records else "UNKNOWN"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
        summary_id = f"ES-{source_code}-{mdtrt_id}-{digest}"
        stored_summary = _remove_empty_values(summary)
        stored_summary.pop("target_fact", None)
        self.summary_ids[identity] = summary_id
        self.summaries[summary_id] = stored_summary
        return summary_id


def _select_record_source(source: str, record: dict[str, Any]) -> str:
    candidates = [part.strip() for part in source.split("、") if part.strip()]
    if len(candidates) <= 1:
        return candidates[0] if candidates else source

    record_name = str(record.get("recordName", "")).strip()
    for candidate in candidates:
        parts = candidate.split(maxsplit=1)
        source_name = parts[1] if len(parts) == 2 else parts[0]
        source_names = [name.strip() for name in source_name.split("或") if name.strip()]
        if any(
            name == record_name or name in record_name or record_name in name
            for name in source_names
        ):
            return candidate
    return source


def _tag_key(tag: dict[str, Any]) -> TagKey:
    return (
        str(tag.get("一级证据域", "")).strip(),
        str(tag.get("二级证据标签", "")).strip(),
        str(tag.get("target_fact", "")).strip(),
    )


def _tag_metadata(proposition: dict[str, Any]) -> dict[TagKey, dict[str, Any]]:
    metadata: dict[TagKey, dict[str, Any]] = {}
    for group_name, default_necessity in (
        ("required_tags", "REQUIRED"),
        ("supplementary_tags", "SUPPLEMENTARY"),
    ):
        for tag in proposition.get(group_name, []):
            metadata[_tag_key(tag)] = {
                "necessity": tag.get("necessity") or default_necessity,
            }
    return metadata


def _source_is_medical_document(source: str | None) -> bool:
    if not source:
        return False
    source_codes = [part.strip().split(maxsplit=1)[0] for part in source.split("、")]
    return bool(source_codes) and all(code.startswith("DS-M-") for code in source_codes)


def _source_contains_expense(source: str | None) -> bool:
    if not source:
        return False
    return any(part.strip().startswith("DS-S-002") for part in source.split("、"))


def _compact_query_result(
    query_result: dict[str, Any],
    target_fact: str,
    document_collector: MedicalDocumentCollector,
    structured_collector: StructuredDataCollector,
    summary_collector: ExpenseSummaryCollector,
    field_dictionary: StandardViewFieldDictionary,
) -> dict[str, Any]:
    source = query_result.get("source")
    compact: dict[str, Any] = {
        "status": query_result.get("status"),
        "source": source,
    }
    if query_result.get("query_scope_status"):
        compact["query_scope_status"] = query_result["query_scope_status"]
    if query_result.get("query_scope_reason"):
        compact["query_scope_reason"] = query_result["query_scope_reason"]
    records = query_result.get("data", [])
    document_records = [
        record
        for record in records
        if isinstance(record, dict) and "recordContent" in record
    ]
    structured_records = [record for record in records if record not in document_records]

    if (
        structured_records
        and _source_contains_expense(str(source or ""))
        and should_summarize(structured_records, target_fact)
    ):
        summary = build_expense_summary(
            structured_records,
            target_fact,
            field_dictionary,
        )
        compact["expense_summary_refs"] = [
            summary_collector.add(str(source or ""), summary, structured_records)
        ]
        compact["raw_record_count"] = len(structured_records)
        compact["raw_data_in_query_result"] = True
        structured_records = []

    if document_records:
        compact["document_refs"] = [
            document_collector.add(str(source or ""), record)
            for record in document_records
        ]
    if structured_records:
        compact["structured_data_refs"] = list(
            dict.fromkeys(
                structured_collector.add(str(source or ""), record)
                for record in structured_records
            )
        )
    if not records:
        if _source_is_medical_document(source):
            compact["document_refs"] = []
        else:
            compact["structured_data_refs"] = []
    return compact


def _build_rule_context(
    source_rule: dict[str, Any],
    query_rule: dict[str, Any],
    field_dictionary: StandardViewFieldDictionary,
) -> dict[str, Any]:
    source_result = source_rule.get("result", {})
    source_propositions = {
        str(proposition.get("proposition_id", "")): proposition
        for proposition in source_result.get("appeal_propositions", [])
    }
    document_collector = MedicalDocumentCollector()
    structured_collector = StructuredDataCollector()
    summary_collector = ExpenseSummaryCollector()
    propositions: list[dict[str, Any]] = []

    for query_proposition in query_rule.get("propositions", []):
        proposition_id = str(query_proposition.get("proposition_id", ""))
        source_proposition = source_propositions.get(proposition_id)
        if source_proposition is None:
            raise ValueError(f"规则证据中不存在申诉命题：{proposition_id}")
        metadata = _tag_metadata(source_proposition)
        evidence: list[dict[str, Any]] = []
        for fact_index, query_evidence in enumerate(
            query_proposition.get("evidence", []), start=1
        ):
            key = _tag_key(query_evidence)
            if key not in metadata:
                raise ValueError(
                    "规则证据中不存在查询结果对应的标签："
                    f"{key[0]}/{key[1]}/{key[2]}"
                )
            evidence.append(
                {
                    "fact_id": f"{proposition_id}-F{fact_index:02d}",
                    "target_fact": key[2],
                    "necessity": metadata[key]["necessity"],
                    "query_result": _compact_query_result(
                        query_evidence.get("query_result", {}),
                        key[2],
                        document_collector,
                        structured_collector,
                        summary_collector,
                        field_dictionary,
                    ),
                }
            )
        propositions.append(
            {
                "proposition_id": proposition_id,
                "statement": source_proposition.get("statement"),
                "logic_notes": source_proposition.get("logic_notes"),
                "missing_required_facts": source_proposition.get(
                    "missing_required_facts", []
                ),
                "evidence": evidence,
            }
        )

    source = source_result.get("source", {})
    regulatory_claim = source_result.get("regulatory_claim", {})
    medical_filter = query_rule.get("medical_record_filter")
    if isinstance(medical_filter, dict) and medical_filter.get("enabled") is True:
        progress_documents = [
            document
            for document in document_collector.documents
            if str(document.get("recordType", "")).strip() == "病程记录"
        ]
        other_documents = [
            document
            for document in document_collector.documents
            if str(document.get("recordType", "")).strip() != "病程记录"
        ]
        grouped_documents, medical_document_groups = group_medical_documents(
            other_documents
        )
        medical_documents = [*progress_documents, *grouped_documents]
    else:
        medical_documents, medical_document_groups = group_medical_documents(
            document_collector.documents
        )
    context = {
        "rule_id": query_rule.get("rule_id"),
        "mdtrt_id": query_rule.get("mdtrt_id"),
        "review_basis": {
            "violation_item": source.get("violation_item"),
            "violation_description": source.get("violation_description"),
            "regulatory_claim": regulatory_claim.get("statement"),
            "appeal_objective": source_result.get("appeal_objective"),
        },
        "logical_expression": source_result.get("logical_expression"),
        "propositions": propositions,
        "structured_data_pool": structured_collector.records,
        "expense_summary_pool": summary_collector.summaries,
        "medical_documents": medical_documents,
        "medical_document_groups": medical_document_groups,
        "program_checks": source_result.get("program_checks", []),
    }
    if isinstance(medical_filter, dict):
        context["medical_record_filter"] = medical_filter
    return _remove_evidence_labels(context)


def build_review_context(
    rule_evidence: list[dict[str, Any]],
    query_result: dict[str, Any],
    standard_view_dictionary_path: Path | None = None,
) -> dict[str, Any]:
    source_rules = {
        str(rule.get("rule_id", "")): rule
        for rule in rule_evidence
    }
    query_rules = query_result.get("rules", [query_result])
    contexts: list[dict[str, Any]] = []
    field_dictionary = StandardViewFieldDictionary(standard_view_dictionary_path)
    for query_rule in query_rules:
        rule_id = str(query_rule.get("rule_id", ""))
        source_rule = source_rules.get(rule_id)
        if source_rule is None:
            raise ValueError(f"规则证据中不存在规则：{rule_id}")
        contexts.append(_build_rule_context(source_rule, query_rule, field_dictionary))

    if len(contexts) == 1:
        return contexts[0]
    return {"rules": contexts}
