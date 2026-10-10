from __future__ import annotations

from copy import deepcopy
from typing import Any


REFERENCE_FIELDS = ("document_refs", "structured_data_refs", "expense_summary_refs")


def primary_source_context(context: dict[str, Any]) -> dict[str, Any]:
    selected = deepcopy(context)
    references: dict[str, set[str]] = {field: set() for field in REFERENCE_FIELDS}
    document_sources: dict[str, list[str]] = {}
    for proposition in selected["propositions"]:
        for fact in proposition["evidence"]:
            query = fact["query_result"]
            groups = [group for group in query["source_groups"] if group["source_level"] == "PRIMARY"]
            primary_query: dict[str, Any] = {
                "source_groups": groups,
                "source": "、".join(dict.fromkeys(group["source"] for group in groups)) or None,
            }
            for field in REFERENCE_FIELDS:
                values = list(dict.fromkeys(value for group in groups for value in group.get(field, [])))
                if values or any(field in group for group in groups):
                    primary_query[field] = values
                references[field].update(values)
            has_data = any(primary_query.get(field) for field in REFERENCE_FIELDS)
            unresolved = [group for group in groups if group.get("query_scope_status") == "QUERY_SCOPE_UNRESOLVED"]
            primary_query["status"] = (
                "RETRIEVED" if has_data else "QUERY_SCOPE_UNRESOLVED" if unresolved else "NOT_RETRIEVED"
            )
            if unresolved:
                primary_query["query_scope_status"] = "QUERY_SCOPE_UNRESOLVED"
                primary_query["query_scope_reason"] = "；".join(dict.fromkeys(
                    str(group.get("query_scope_reason", "查询范围未确定")) for group in unresolved
                ))
            for group in groups:
                for document_id in group.get("document_refs", []):
                    sources = document_sources.setdefault(document_id, [])
                    if group["source"] not in sources:
                        sources.append(group["source"])
            fact["query_result"] = primary_query
    for pool, field in (("structured_data_pool", "structured_data_refs"), ("expense_summary_pool", "expense_summary_refs")):
        selected[pool] = {
            reference: value for reference, value in selected.get(pool, {}).items()
            if reference in references[field]
        }
    selected["medical_documents"] = [
        document for document in selected["medical_documents"]
        if document["document_id"] in references["document_refs"]
    ]
    for document in selected["medical_documents"]:
        document["source"] = "、".join(document_sources[document["document_id"]])
        for field in ("matched_propositions", "matched_evidence_tags", "matched_terms"):
            document.pop(field, None)
    groups = []
    for group in selected.get("medical_document_groups", []):
        records = [record for record in group["records"] if record["document_id"] in references["document_refs"]]
        if records:
            group["records"] = records
            group["source"] = "、".join(dict.fromkeys(
                source for record in records for source in document_sources[record["document_id"]]
            ))
            groups.append(group)
    selected["medical_document_groups"] = groups
    fragment_refs: set[str] = set()

    def collect_fragment_refs(value: Any) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("ref"), str):
                fragment_refs.add(value["ref"])
            for item in value.values():
                collect_fragment_refs(item)
        elif isinstance(value, list):
            for item in value:
                collect_fragment_refs(item)

    collect_fragment_refs(selected["medical_documents"])
    collect_fragment_refs(groups)
    if "medical_fragment_pool" in selected:
        selected["medical_fragment_pool"] = {
            reference: fragment for reference, fragment in selected["medical_fragment_pool"].items()
            if reference in fragment_refs
        }
    selected.pop("medical_record_filter", None)
    selected["review_source_stage"] = "PRIMARY"
    return selected
