from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from typing import Any


_SEGMENT_PATTERN = re.compile(r".*?(?:[。！？；;\n]|$)", re.DOTALL)
_MIN_SHARED_LENGTH = 20


def _split_text(value: str) -> list[str]:
    segments = [match.group(0) for match in _SEGMENT_PATTERN.finditer(value)]
    return [segment for segment in segments if segment]


def _fragment_id(field_name: str, text: str) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {"field": field_name, "text": text},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:12]
    return f"MF-{digest}"


def _content_fields(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        return {"全文": content}
    return {"全文": content}


def compress_medical_documents(
    documents: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]]]:
    """将病历中的完全相同长文本片段提取到共享池，保留每条记录的顺序和差异。"""
    occurrences: Counter[tuple[str, str]] = Counter()
    prepared: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for document in documents:
        fields = _content_fields(document.get("recordContent"))
        prepared.append((document, fields))
        seen_in_document: set[tuple[str, str]] = set()
        for field_name, value in fields.items():
            if not isinstance(value, str):
                continue
            for segment in _split_text(value):
                key = (str(field_name), segment)
                if len(segment.strip()) >= _MIN_SHARED_LENGTH:
                    seen_in_document.add(key)
        for key in seen_in_document:
            occurrences[key] += 1

    shared_keys = {key for key, count in occurrences.items() if count >= 2}
    fragment_pool: dict[str, dict[str, str]] = {}
    for field_name, text in sorted(shared_keys):
        fragment_pool[_fragment_id(field_name, text)] = {
            "field": field_name,
            "text": text,
        }

    compressed_documents: list[dict[str, Any]] = []
    for document, fields in prepared:
        compressed_fields: dict[str, Any] = {}
        for field_name, value in fields.items():
            if not isinstance(value, str):
                compressed_fields[field_name] = {"value": value}
                continue
            segments: list[dict[str, str]] = []
            for segment in _split_text(value):
                fragment_key = (str(field_name), segment)
                if fragment_key in shared_keys:
                    segments.append(
                        {"ref": _fragment_id(str(field_name), segment)}
                    )
                else:
                    segments.append({"text": segment})
            compressed_fields[field_name] = {"segments": segments}

        compressed = dict(document)
        compressed["recordContent"] = {
            "__compressed__": True,
            "fields": compressed_fields,
        }
        compressed_documents.append(compressed)

    return compressed_documents, fragment_pool


def expand_medical_record_content(
    content: Any,
    fragment_pool: dict[str, dict[str, str]] | None,
) -> Any:
    """恢复压缩病历的原文字段，供引用校验使用。"""
    if not isinstance(content, dict) or content.get("__compressed__") is not True:
        return content
    pool = fragment_pool or {}
    fields = content.get("fields", {})
    if not isinstance(fields, dict):
        return content
    expanded: dict[str, Any] = {}
    for field_name, value in fields.items():
        if not isinstance(value, dict):
            expanded[field_name] = value
            continue
        if "value" in value:
            expanded[field_name] = value["value"]
            continue
        segments = value.get("segments", [])
        if not isinstance(segments, list):
            expanded[field_name] = value
            continue
        parts: list[str] = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            if "text" in segment:
                parts.append(str(segment["text"]))
                continue
            reference = segment.get("ref")
            fragment = pool.get(str(reference))
            if isinstance(fragment, dict):
                parts.append(str(fragment.get("text", "")))
        expanded[field_name] = "".join(parts)
    return expanded


def group_medical_documents(
    documents: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按相同文书类型分组，公共原文只展示一次，记录保留差异内容。"""
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for document in documents:
        key = (
            str(document.get("source", "")),
            str(document.get("recordName", "")),
            str(document.get("recordType", "")),
        )
        grouped[key].append(document)

    index: list[dict[str, Any]] = []
    result_groups: list[dict[str, Any]] = []
    for group_index, ((source, record_name, record_type), group_documents) in enumerate(
        grouped.items(),
        start=1,
    ):
        group_id = f"MG-{group_index:03d}"
        prepared = [
            (document, _content_fields(document.get("recordContent")))
            for document in group_documents
        ]
        occurrences: Counter[tuple[str, str]] = Counter()
        for _, fields in prepared:
            seen: set[tuple[str, str]] = set()
            for field_name, value in fields.items():
                if not isinstance(value, str):
                    continue
                for segment in _split_text(value):
                    if len(segment.strip()) >= _MIN_SHARED_LENGTH:
                        seen.add((str(field_name), segment))
            for key in seen:
                occurrences[key] += 1

        common_keys = {
            key
            for key, count in occurrences.items()
            if len(group_documents) >= 2 and count == len(group_documents)
        }
        common_content: dict[str, str] = defaultdict(str)
        if common_keys:
            first_fields = prepared[0][1]
            for field_name, value in first_fields.items():
                if not isinstance(value, str):
                    continue
                common_content[str(field_name)] = "".join(
                    segment
                    for segment in _split_text(value)
                    if (str(field_name), segment) in common_keys
                )
        common_set = set(common_keys)
        group_records: list[dict[str, Any]] = []
        for document, fields in prepared:
            unique_content: dict[str, Any] = {}
            for field_name, value in fields.items():
                if isinstance(value, str):
                    unique_text = "".join(
                        segment
                        for segment in _split_text(value)
                        if (str(field_name), segment) not in common_set
                    )
                    if unique_text:
                        unique_content[str(field_name)] = unique_text
                else:
                    unique_content[str(field_name)] = value
            document_id = str(document["document_id"])
            index.append(
                {
                    "document_id": document_id,
                    "source": document.get("source"),
                    "recordName": document.get("recordName"),
                    "recordType": document.get("recordType"),
                    "updateTime": document.get("updateTime"),
                    "group_id": group_id,
                }
            )
            group_records.append(
                {
                    "document_id": document_id,
                    "updateTime": document.get("updateTime"),
                    "common_content_ref": f"{group_id}.common_content",
                    "unique_content": unique_content,
                }
            )
        result_groups.append(
            {
                "group_id": group_id,
                "source": source,
                "recordName": record_name,
                "recordType": record_type,
                "common_content": dict(common_content),
                "records": group_records,
            }
        )
    return index, result_groups


def expand_group_document(
    group: dict[str, Any],
    document_id: str,
) -> dict[str, Any] | None:
    """为引用校验构造一条分组病历的字段内容。"""
    record = next(
        (
            item
            for item in group.get("records", [])
            if isinstance(item, dict) and str(item.get("document_id")) == document_id
        ),
        None,
    )
    if record is None:
        return None
    values: dict[str, Any] = {}
    common_content = group.get("common_content", {})
    if isinstance(common_content, dict):
        values.update(common_content)
    elif isinstance(common_content, list):
        for item in common_content:
            if isinstance(item, dict) and "field" in item:
                values[str(item["field"])] = values.get(str(item["field"]), "") + str(
                    item.get("text", "")
                )
    unique_content = record.get("unique_content", {})
    if isinstance(unique_content, dict):
        for field, value in unique_content.items():
            if field in values and isinstance(values[field], str) and isinstance(value, str):
                values[field] = f"{values[field]}\n{value}"
            else:
                values[field] = value
    elif isinstance(unique_content, list):
        for item in unique_content:
            if not isinstance(item, dict) or "field" not in item:
                continue
            field = str(item["field"])
            value = item.get("text") if "text" in item else item.get("value")
            values[field] = values.get(field, "") + str(value or "")
    return values
