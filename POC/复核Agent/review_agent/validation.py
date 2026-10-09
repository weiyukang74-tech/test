from __future__ import annotations

import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_QUERY_PROGRAM_ROOT = Path(__file__).resolve().parents[2] / "查询程序"
if str(_QUERY_PROGRAM_ROOT) not in sys.path:
    sys.path.insert(0, str(_QUERY_PROGRAM_ROOT))
from evidence_retriever.medical_compression import (
    expand_group_document,
    expand_medical_record_content,
)


FACT_STATUSES = {"SUPPORTED", "NOT_SUPPORTED", "UNKNOWN", "CONFLICTED"}
REVIEW_STATUSES = {"CLOSED", "OPEN", "BLOCKED", "CONFLICTED"}
CASE_STATUSES = {"复核完成", "待补充证据", "数据阻断", "证据冲突", "待人工终审"}
FINAL_RESULTS = {"合规", "违规", "暂无法判定", "待人工终审"}
NECESSITIES = {"REQUIRED", "SUPPLEMENTARY"}
EVIDENCE_TYPES = {"STRUCTURED", "MEDICAL_DOCUMENT"}

ROOT_KEYS = {
    "rule_id",
    "mdtrt_id",
    "review_round",
    "proposition_relation",
    "fact_reviews",
    "proposition_reviews",
    "overall_logic_status",
    "review_status",
    "case_status",
    "final_conclusion",
    "supplemental_evidence_requests",
}

OPTIONAL_ROOT_KEYS = {"manual_review_reason"}

TOKEN_PATTERN = re.compile(r"\s*(AND|OR|NOT|\(|\)|[A-Za-z0-9][A-Za-z0-9_-]*)")


class ReviewValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ExpressionNode:
    kind: str
    value: str | None = None
    left: "ExpressionNode | None" = None
    right: "ExpressionNode | None" = None


class ExpressionParser:
    def __init__(self, expression: str):
        self.tokens = self._tokenize(expression)
        self.position = 0

    @staticmethod
    def _tokenize(expression: str) -> list[str]:
        tokens: list[str] = []
        position = 0
        while position < len(expression):
            match = TOKEN_PATTERN.match(expression, position)
            if match is None:
                raise ReviewValidationError(
                    f"命题逻辑包含非法内容: {expression[position:position + 20]}"
                )
            tokens.append(match.group(1))
            position = match.end()
        if not tokens:
            raise ReviewValidationError("命题逻辑不能为空")
        return tokens

    def parse(self) -> ExpressionNode:
        node = self._parse_or()
        if self.position != len(self.tokens):
            raise ReviewValidationError(
                f"命题逻辑存在未解析内容: {self.tokens[self.position]}"
            )
        return node

    def _parse_or(self) -> ExpressionNode:
        node = self._parse_and()
        while self._accept("OR"):
            node = ExpressionNode("OR", left=node, right=self._parse_and())
        return node

    def _parse_and(self) -> ExpressionNode:
        node = self._parse_not()
        while self._accept("AND"):
            node = ExpressionNode("AND", left=node, right=self._parse_not())
        return node

    def _parse_not(self) -> ExpressionNode:
        if self._accept("NOT"):
            return ExpressionNode("NOT", left=self._parse_not())
        return self._parse_atom()

    def _parse_atom(self) -> ExpressionNode:
        if self._accept("("):
            node = self._parse_or()
            if not self._accept(")"):
                raise ReviewValidationError("命题逻辑缺少右括号")
            return node
        token = self._peek()
        if token is None or token in {"AND", "OR", "NOT", ")"}:
            raise ReviewValidationError("命题逻辑缺少命题编号")
        self.position += 1
        return ExpressionNode("ID", value=token)

    def _peek(self) -> str | None:
        if self.position >= len(self.tokens):
            return None
        return self.tokens[self.position]

    def _accept(self, token: str) -> bool:
        if self._peek() == token:
            self.position += 1
            return True
        return False


def expression_ids(node: ExpressionNode) -> set[str]:
    if node.kind == "ID":
        return {str(node.value)}
    identifiers: set[str] = set()
    if node.left is not None:
        identifiers.update(expression_ids(node.left))
    if node.right is not None:
        identifiers.update(expression_ids(node.right))
    return identifiers


def evaluate_expression(node: ExpressionNode, statuses: dict[str, str]) -> str:
    if node.kind == "ID":
        return statuses[str(node.value)]
    if node.kind == "NOT":
        status = evaluate_expression(_required_node(node.left), statuses)
        return {
            "SUPPORTED": "NOT_SUPPORTED",
            "NOT_SUPPORTED": "SUPPORTED",
            "UNKNOWN": "UNKNOWN",
            "CONFLICTED": "CONFLICTED",
        }[status]

    left = evaluate_expression(_required_node(node.left), statuses)
    right = evaluate_expression(_required_node(node.right), statuses)
    if node.kind == "AND":
        if "NOT_SUPPORTED" in {left, right}:
            return "NOT_SUPPORTED"
        if left == right == "SUPPORTED":
            return "SUPPORTED"
        if "CONFLICTED" in {left, right}:
            return "CONFLICTED"
        return "UNKNOWN"
    if node.kind == "OR":
        if "SUPPORTED" in {left, right}:
            return "SUPPORTED"
        if left == right == "NOT_SUPPORTED":
            return "NOT_SUPPORTED"
        if "CONFLICTED" in {left, right}:
            return "CONFLICTED"
        return "UNKNOWN"
    raise ReviewValidationError(f"未知逻辑节点: {node.kind}")


def _required_node(node: ExpressionNode | None) -> ExpressionNode:
    if node is None:
        raise ReviewValidationError("命题逻辑节点不完整")
    return node


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReviewValidationError(f"{label}必须是对象")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ReviewValidationError(f"{label}必须是数组")
    return value


def _require_nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewValidationError(f"{label}必须是非空字符串")
    return value


def _require_exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ReviewValidationError(f"{label}字段不正确，缺少={missing}，多余={extra}")


def _fact_key(proposition_id: str, fact: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        proposition_id,
        str(fact.get("一级证据域", "")).strip(),
        str(fact.get("二级证据标签", "")).strip(),
        str(fact.get("target_fact", "")).strip(),
    )


def validate_context(context: dict[str, Any]) -> None:
    _require_nonempty_string(context.get("rule_id"), "rule_id")
    _require_nonempty_string(context.get("mdtrt_id"), "mdtrt_id")
    _require_object(context.get("review_basis"), "review_basis")
    propositions = _require_list(context.get("propositions"), "propositions")
    if not propositions:
        raise ReviewValidationError("propositions不能为空")

    proposition_ids: set[str] = set()
    fact_keys: set[tuple[str, str, str, str]] = set()
    all_document_refs: set[str] = set()
    all_summary_refs: set[str] = set()
    all_structured_refs: set[str] = set()
    for index, raw_proposition in enumerate(propositions, start=1):
        proposition = _require_object(raw_proposition, f"propositions[{index}]")
        proposition_id = _require_nonempty_string(
            proposition.get("proposition_id"),
            f"propositions[{index}].proposition_id",
        )
        if proposition_id in proposition_ids:
            raise ReviewValidationError(f"proposition_id重复: {proposition_id}")
        proposition_ids.add(proposition_id)
        evidence_items = _require_list(
            proposition.get("evidence"),
            f"{proposition_id}.evidence",
        )
        for raw_evidence in evidence_items:
            evidence = _require_object(raw_evidence, f"{proposition_id}.evidence项")
            key = _fact_key(proposition_id, evidence)
            if not all(key):
                raise ReviewValidationError(f"{proposition_id}存在标签或target_fact为空")
            if key in fact_keys:
                raise ReviewValidationError(f"事实项重复: {key}")
            fact_keys.add(key)
            if evidence.get("necessity") not in NECESSITIES:
                raise ReviewValidationError(f"{key}的necessity无效")
            query_result = _require_object(evidence.get("query_result"), f"{key}.query_result")
            for document_ref in query_result.get("document_refs", []):
                all_document_refs.add(_require_nonempty_string(document_ref, "document_ref"))
            summary_refs = query_result.get("expense_summary_refs", [])
            if not isinstance(summary_refs, list):
                raise ReviewValidationError(f"{key}.query_result.expense_summary_refs必须是数组")
            for summary_ref in summary_refs:
                all_summary_refs.add(
                    _require_nonempty_string(summary_ref, "expense_summary_ref")
                )
            for structured_ref in query_result.get("structured_data_refs", []):
                all_structured_refs.add(
                    _require_nonempty_string(structured_ref, "structured_data_ref")
                )

    expression = _require_nonempty_string(context.get("logical_expression"), "logical_expression")
    identifiers = expression_ids(ExpressionParser(expression).parse())
    if identifiers != proposition_ids:
        raise ReviewValidationError(
            f"命题逻辑引用范围不完整，实际={sorted(identifiers)}，应为={sorted(proposition_ids)}"
        )

    structured_pool_raw = context.get("structured_data_pool", {})
    structured_pool = _require_object(structured_pool_raw, "structured_data_pool")
    for record_id, raw_record in structured_pool.items():
        _require_nonempty_string(record_id, "structured_data_pool记录ID")
        _require_object(raw_record, f"structured_data_pool.{record_id}")
    missing_structured = sorted(all_structured_refs - set(structured_pool))
    if missing_structured:
        raise ReviewValidationError(
            f"structured_data_refs找不到对应记录: {missing_structured}"
        )

    documents = _require_list(context.get("medical_documents"), "medical_documents")
    document_groups = _require_list(
        context.get("medical_document_groups", []),
        "medical_document_groups",
    )
    grouped_document_ids: set[str] = set()
    for raw_group in document_groups:
        group = _require_object(raw_group, "medical_document_groups项")
        _require_nonempty_string(group.get("group_id"), "病历分组ID")
        _require_nonempty_string(group.get("recordName"), "病历分组recordName")
        common_content = group.get("common_content", {})
        if not isinstance(common_content, (dict, list)):
            raise ReviewValidationError("病历分组common_content必须是对象或数组")
        records = _require_list(group.get("records"), "病历分组records")
        for raw_record in records:
            record = _require_object(raw_record, "病历分组records项")
            document_id = _require_nonempty_string(
                record.get("document_id"), "病历分组document_id"
            )
            if document_id in grouped_document_ids:
                raise ReviewValidationError(f"病历分组document_id重复: {document_id}")
            grouped_document_ids.add(document_id)
            unique_content = record.get("unique_content", {})
            if not isinstance(unique_content, (dict, list)):
                raise ReviewValidationError("病历差异内容必须是对象或数组")
    fragment_pool_raw = context.get("medical_fragment_pool", {})
    fragment_pool = _require_object(fragment_pool_raw, "medical_fragment_pool")
    for fragment_id, raw_fragment in fragment_pool.items():
        _require_nonempty_string(fragment_id, "medical_fragment_pool片段ID")
        fragment = _require_object(raw_fragment, f"medical_fragment_pool.{fragment_id}")
        _require_nonempty_string(fragment.get("field"), "medical_fragment_pool.field")
        _require_nonempty_string(fragment.get("text"), "medical_fragment_pool.text")
    document_ids: set[str] = set()
    for raw_document in documents:
        document = _require_object(raw_document, "medical_documents项")
        document_id = _require_nonempty_string(document.get("document_id"), "document_id")
        if document_id in document_ids:
            raise ReviewValidationError(f"document_id重复: {document_id}")
        document_ids.add(document_id)
        record_content = document.get("recordContent")
        if record_content is None and document_id in grouped_document_ids:
            continue
        if not isinstance(record_content, (dict, str)):
            raise ReviewValidationError(
                f"{document_id}.recordContent必须是对象或字符串"
            )
        if isinstance(record_content, dict) and record_content.get("__compressed__") is True:
            fields = record_content.get("fields")
            if not isinstance(fields, dict):
                raise ReviewValidationError(f"{document_id}.recordContent.fields必须是对象")
            for field_name, field_value in fields.items():
                if not isinstance(field_value, dict):
                    raise ReviewValidationError(
                        f"{document_id}.recordContent.fields.{field_name}必须是对象"
                    )
                if "segments" in field_value:
                    segments = field_value["segments"]
                    if not isinstance(segments, list):
                        raise ReviewValidationError(
                            f"{document_id}.recordContent.fields.{field_name}.segments必须是数组"
                        )
                    for segment in segments:
                        segment = _require_object(segment, "病历压缩片段")
                        if "ref" in segment and str(segment["ref"]) not in fragment_pool:
                            raise ReviewValidationError(
                                f"病历压缩片段引用不存在: {segment['ref']}"
                            )
                        if "text" not in segment and "ref" not in segment:
                            raise ReviewValidationError("病历压缩片段必须包含text或ref")
        if isinstance(record_content, str) and not record_content.strip():
            raise ReviewValidationError(f"{document_id}.recordContent不能为空")
    missing_documents = sorted(all_document_refs - document_ids)
    if missing_documents:
        raise ReviewValidationError(f"document_refs找不到对应病历: {missing_documents}")

    summary_pool_raw = context.get("expense_summary_pool", {})
    summary_pool = _require_object(summary_pool_raw, "expense_summary_pool")
    for summary_id, raw_summary in summary_pool.items():
        _require_nonempty_string(summary_id, "expense_summary_pool摘要ID")
        _require_object(raw_summary, f"expense_summary_pool.{summary_id}")
    missing_summaries = sorted(all_summary_refs - set(summary_pool))
    if missing_summaries:
        raise ReviewValidationError(
            f"expense_summary_refs找不到对应摘要: {missing_summaries}"
        )

    _require_list(context.get("program_checks"), "program_checks")


def _context_indexes(context: dict[str, Any]) -> tuple[
    dict[str, dict[str, Any]],
    dict[tuple[str, str, str, str], dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, str]],
]:
    propositions: dict[str, dict[str, Any]] = {}
    facts: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for proposition in context["propositions"]:
        proposition_id = proposition["proposition_id"]
        propositions[proposition_id] = proposition
        for fact in proposition["evidence"]:
            facts[_fact_key(proposition_id, fact)] = fact
    documents = {
        document["document_id"]: document
        for document in context["medical_documents"]
    }
    groups = context.get("medical_document_groups", [])
    groups_by_id = {
        str(group.get("group_id")): group
        for group in groups
        if isinstance(group, dict)
    }
    for document_id, document in list(documents.items()):
        if document.get("recordContent") is not None:
            continue
        group_id = document.get("group_id")
        group = groups_by_id.get(str(group_id))
        if group is not None:
            expanded = expand_group_document(group, document_id)
            if expanded is not None:
                document["recordContent"] = expanded
    structured_pool = context.get("structured_data_pool", {})
    summary_pool = context.get("expense_summary_pool", {})
    fragment_pool = context.get("medical_fragment_pool", {})
    return propositions, facts, documents, structured_pool, summary_pool, fragment_pool


def _logic_source_texts(context: dict[str, Any]) -> list[str]:
    texts = [
        value
        for value in context.get("review_basis", {}).values()
        if isinstance(value, str)
    ]
    for proposition in context["propositions"]:
        for field in ("statement", "logic_notes"):
            value = proposition.get(field)
            if isinstance(value, str):
                texts.append(value)
    return texts


def _logic_quote_matches_source(quote: str, source_text: str) -> bool:
    if quote in source_text:
        return True
    segments = [
        segment.strip()
        for segment in re.split(r"(?:\.\.\.|…+)", quote)
        if segment.strip()
    ]
    if len(segments) < 2:
        return False
    position = 0
    for segment in segments:
        match_position = source_text.find(segment, position)
        if match_position < 0:
            return False
        position = match_position + len(segment)
    return True


def _medical_quote_matches_source(quote: str, source_text: str) -> bool:
    """Match an exact quote or verbatim source fragments separated by ellipses.

    Medical quotes may compact several independently selected facts into one
    quote.  Prefer the original order, but accept a different fragment order
    as long as every fragment appears verbatim in the same source field.
    """
    if quote in source_text:
        return True
    normalized_quote = _normalize_medical_text(quote)
    normalized_source = _normalize_medical_text(source_text)
    if normalized_quote in normalized_source:
        return True
    if not re.search(r"(?:\.\.\.|…+)", quote):
        return False
    segments = [
        _normalize_medical_text(segment)
        for segment in re.split(r"(?:\.\.\.|…+)", quote)
        if _normalize_medical_text(segment)
    ]
    if not segments:
        return False
    if len(segments) == 1:
        return segments[0] in normalized_source

    # Keep the stricter, semantically safer path when the model preserved the
    # source order.
    position = 0
    ordered = True
    for segment in segments:
        match_position = normalized_source.find(segment, position)
        if match_position < 0:
            ordered = False
            break
        position = match_position + len(segment)
    if ordered:
        return True

    # A medical quote can also be a compact collection of facts rather than a
    # reconstructed sentence.  In that case order is not evidence; exact
    # membership of every fragment in the same field is still required.
    return all(segment in normalized_source for segment in segments)


def _unmatched_medical_quote_segments(quote: str, source_values: list[Any]) -> list[str]:
    normalized_sources = [_normalize_medical_text(str(value)) for value in source_values]
    if re.search(r"(?:\.\.\.|…+)", quote):
        segments = [
            _normalize_medical_text(segment)
            for segment in re.split(r"(?:\.\.\.|…+)", quote)
            if _normalize_medical_text(segment)
        ]
    else:
        segments = [_normalize_medical_text(quote)]
    return [
        segment
        for segment in segments
        if not any(segment in source for source in normalized_sources)
    ]


def _medical_quote_locations(node: Any, quote: str) -> list[str]:
    """Find field names containing a verifiable medical quote."""
    matches: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and _medical_quote_matches_source(quote, value):
                matches.append(str(key))
            elif isinstance(value, (dict, list)):
                matches.extend(_medical_quote_locations(value, quote))
    elif isinstance(node, list):
        for value in node:
            matches.extend(_medical_quote_locations(value, quote))
    return list(dict.fromkeys(matches))


def _normalize_medical_text(text: str) -> str:
    """Normalize harmless Unicode and whitespace differences in medical text."""
    normalized = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", normalized).strip()


def _find_location_values(node: Any, location: str) -> list[Any]:
    matches: list[Any] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == location:
                matches.append(value)
            matches.extend(_find_location_values(value, location))
    elif isinstance(node, list):
        for value in node:
            matches.extend(_find_location_values(value, location))
    return matches


_SOURCE_SEPARATOR_PATTERN = re.compile(r"[、,，;；|\n]+")
_SOURCE_CODE_PATTERN = re.compile(r"^(DS-[SM]-\d{3})", re.IGNORECASE)


def _source_parts(value: Any) -> list[str]:
    """将新旧版本中的来源字符串、数组或对象统一拆成来源项。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        parts: list[str] = []
        for item in value:
            parts.extend(_source_parts(item))
        return parts
    if isinstance(value, dict):
        parts: list[str] = []
        for key in ("label", "source", "name", "source_name", "code", "source_code"):
            if key in value:
                parts.extend(_source_parts(value[key]))
        return parts
    text = str(value).strip()
    if not text:
        return []
    return [part.strip() for part in _SOURCE_SEPARATOR_PATTERN.split(text) if part.strip()]


def _canonical_source(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", "", normalized).strip().casefold()


def _source_identities(value: str) -> set[tuple[str, str]]:
    canonical = _canonical_source(value)
    code_match = _SOURCE_CODE_PATTERN.match(canonical)
    if code_match:
        identities = {("code", code_match.group(1).casefold())}
        name = canonical[code_match.end():]
        if name:
            identities.add(("name", name))
        return identities
    return {("name", canonical)}


def _source_matches(candidate: str, available: Any) -> bool:
    candidate_parts = _source_parts(candidate)
    available_parts = _source_parts(available)
    if not candidate_parts or not available_parts:
        return False

    available_identities = {
        identity
        for part in available_parts
        for identity in _source_identities(part)
    }
    for part in candidate_parts:
        for identity in _source_identities(part):
            if identity in available_identities:
                return True
    return False


def _query_sources(query_result: dict[str, Any]) -> Any:
    """兼容 source、sources 及结构化来源对象三种上下文格式。"""
    for key in ("source", "sources", "source_list"):
        if query_result.get(key) is not None:
            return query_result[key]
    return None


def _source_codes(value: Any) -> set[str]:
    codes: set[str] = set()
    for part in _source_parts(value):
        codes.update(
            match.casefold()
            for match in re.findall(r"DS-[SM]-\d{3}", part, re.IGNORECASE)
        )
    return codes


def _shared_structured_data_for_source(
    source: str,
    structured_pool: dict[str, dict[str, Any]],
    summary_pool: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return shared case records whose IDs prove they came from source."""
    source_codes = _source_codes(source)
    if not source_codes:
        return []
    records: list[dict[str, Any]] = []
    for pool in (structured_pool, summary_pool):
        for record_id, record in pool.items():
            record_codes = {
                match.casefold()
                for match in re.findall(
                    r"DS-[SM]-\d{3}",
                    str(record_id),
                    re.IGNORECASE,
                )
            }
            if source_codes & record_codes:
                records.append(record)
    return records


def _structured_data(
    query_result: dict[str, Any],
    structured_pool: dict[str, dict[str, Any]],
    summary_pool: dict[str, dict[str, Any]],
) -> Any:
    """解析共享结构化数据、费用摘要引用，并兼容旧版内联记录。"""
    data: list[Any] = []
    references = query_result.get("structured_data_refs")
    if isinstance(references, list):
        data.extend(
            structured_pool[reference]
            for reference in references
            if reference in structured_pool
        )
    summary_references = query_result.get("expense_summary_refs")
    if isinstance(summary_references, list):
        data.extend(
            summary_pool[reference]
            for reference in summary_references
            if reference in summary_pool
        )
    if data:
        return data
    expense_summary = query_result.get("expense_summary")
    if isinstance(expense_summary, dict):
        return [expense_summary]
    for key in ("structured_data", "data", "records"):
        value = query_result.get(key)
        if isinstance(value, (list, dict)):
            return value
    return []


def _validate_evidence_quote(
    evidence: dict[str, Any],
    fact_context: dict[str, Any],
    documents: dict[str, dict[str, Any]],
    structured_pool: dict[str, dict[str, Any]],
    summary_pool: dict[str, dict[str, Any]],
    fragment_pool: dict[str, dict[str, str]],
) -> None:
    evidence_type = evidence.get("evidence_type")
    if evidence_type not in EVIDENCE_TYPES:
        raise ReviewValidationError(f"evidence_type无效: {evidence_type}")
    location = _require_nonempty_string(evidence.get("location"), "evidence.location")
    quote = _require_nonempty_string(evidence.get("quote"), "evidence.quote")

    query_result = fact_context["query_result"]
    if evidence_type == "MEDICAL_DOCUMENT":
        document_id = _require_nonempty_string(evidence.get("document_id"), "document_id")
        document = documents.get(document_id)
        if document is None:
            raise ReviewValidationError(f"引用了不存在的document_id: {document_id}")
        record_content = expand_medical_record_content(
            document.get("recordContent"),
            fragment_pool,
        )
        if isinstance(record_content, str):
            values = [record_content] if location in {"recordContent", "全文"} else []
        else:
            values = _find_location_values(record_content, location)
        if not any(
            _medical_quote_matches_source(quote, str(value))
            for value in values
        ):
            unmatched_segments = _unmatched_medical_quote_segments(quote, values)
            candidate_locations = _medical_quote_locations(record_content, quote)
            raise ReviewValidationError(
                f"病历原文中找不到引用: {document_id}/{location}/{quote}; "
                f"未匹配片段={unmatched_segments}; 候选字段={candidate_locations}"
            )
        return

    source = _require_nonempty_string(evidence.get("source"), "evidence.source")
    if evidence.get("document_id") is not None:
        raise ReviewValidationError("STRUCTURED证据不得填写document_id")
    available_sources = _query_sources(query_result)
    structured_data = _structured_data(query_result, structured_pool, summary_pool)
    if _source_matches(source, available_sources):
        values = _find_location_values(structured_data, location)
        if any(quote == str(value) for value in values):
            return

    shared_data = _shared_structured_data_for_source(
        source,
        structured_pool,
        summary_pool,
    )
    shared_values = _find_location_values(shared_data, location)
    if any(quote == str(value) for value in shared_values):
        return

    if not _source_matches(source, available_sources):
        fact_label = (
            f"{fact_context.get('一级证据域')}/"
            f"{fact_context.get('二级证据标签')}/"
            f"{fact_context.get('target_fact')}"
        )
        raise ReviewValidationError(
            f"{fact_label}的STRUCTURED证据既不属于当前查询结果，"
            f"也无法在本案共享结构化证据池中验证: "
            f"模型={source}，当前可用={available_sources}"
        )
    else:
        raise ReviewValidationError(
            f"结构化结果中找不到引用: {location}={quote}"
        )


def validate_result(
    result: dict[str, Any],
    context: dict[str, Any],
    review_round: int,
) -> None:
    validate_context(context)
    actual_root_keys = set(result)
    allowed_root_keys = ROOT_KEYS | OPTIONAL_ROOT_KEYS
    missing_root_keys = sorted(ROOT_KEYS - actual_root_keys)
    extra_root_keys = sorted(actual_root_keys - allowed_root_keys)
    if missing_root_keys or extra_root_keys:
        raise ReviewValidationError(
            f"模型输出字段不正确，缺少={missing_root_keys}，多余={extra_root_keys}"
        )
    if result.get("rule_id") != context["rule_id"]:
        raise ReviewValidationError("模型输出rule_id与复核上下文不一致")
    if result.get("mdtrt_id") != context["mdtrt_id"]:
        raise ReviewValidationError("模型输出mdtrt_id与复核上下文不一致")
    if result.get("review_round") != review_round:
        raise ReviewValidationError("模型输出review_round不正确")

    (
        propositions,
        facts,
        documents,
        structured_pool,
        summary_pool,
        fragment_pool,
    ) = _context_indexes(context)
    relation = _require_object(result.get("proposition_relation"), "proposition_relation")
    _require_exact_keys(relation, {"status", "expression", "basis_quotes"}, "proposition_relation")
    relation_status = relation.get("status")
    if relation_status not in {"CONFIRMED", "AMBIGUOUS"}:
        raise ReviewValidationError("proposition_relation.status无效")
    expression = relation.get("expression")
    if not isinstance(expression, str):
        raise ReviewValidationError("proposition_relation.expression必须是字符串")
    basis_quotes = _require_list(relation.get("basis_quotes"), "basis_quotes")
    if not basis_quotes:
        raise ReviewValidationError("basis_quotes不能为空")
    logic_texts = _logic_source_texts(context)
    for quote in basis_quotes:
        quote_text = _require_nonempty_string(quote, "basis_quote")
        if not any(
            _logic_quote_matches_source(quote_text, source_text)
            for source_text in logic_texts
        ):
            raise ReviewValidationError(f"逻辑依据不是上下文原文: {quote_text}")

    parsed_expression: ExpressionNode | None = None
    if relation_status == "CONFIRMED":
        parsed_expression = ExpressionParser(expression).parse()
        identifiers = expression_ids(parsed_expression)
        if identifiers != set(propositions):
            raise ReviewValidationError(
                f"命题逻辑引用范围不完整，实际={sorted(identifiers)}，应为={sorted(propositions)}"
            )
    elif expression:
        raise ReviewValidationError("AMBIGUOUS时expression必须为空字符串")

    fact_reviews = _require_list(result.get("fact_reviews"), "fact_reviews")
    reviewed_fact_keys: set[tuple[str, str, str, str]] = set()
    for raw_review in fact_reviews:
        review = _require_object(raw_review, "fact_reviews项")
        expected_keys = {
            "proposition_id",
            "一级证据域",
            "二级证据标签",
            "target_fact",
            "necessity",
            "status",
            "evidence",
            "reason",
        }
        _require_exact_keys(review, expected_keys, "fact_reviews项")
        proposition_id = _require_nonempty_string(review.get("proposition_id"), "proposition_id")
        key = _fact_key(proposition_id, review)
        fact_context = facts.get(key)
        if fact_context is None:
            raise ReviewValidationError(f"模型输出了不存在的事实项: {key}")
        if key in reviewed_fact_keys:
            raise ReviewValidationError(f"模型重复输出事实项: {key}")
        reviewed_fact_keys.add(key)
        if review.get("necessity") != fact_context.get("necessity"):
            raise ReviewValidationError(f"事实项necessity被修改: {key}")
        fact_status = review.get("status")
        if fact_status not in FACT_STATUSES:
            raise ReviewValidationError(f"事实状态无效: {fact_status}")
        evidence_items = _require_list(review.get("evidence"), f"{key}.evidence")
        if fact_status in {"SUPPORTED", "NOT_SUPPORTED", "CONFLICTED"} and not evidence_items:
            raise ReviewValidationError(f"{fact_status}事实必须引用证据: {key}")
        if fact_status == "CONFLICTED" and len(evidence_items) < 2:
            raise ReviewValidationError(f"CONFLICTED事实至少需要两项证据: {key}")
        for evidence in evidence_items:
            _validate_evidence_quote(
                _require_object(evidence, f"{key}.evidence项"),
                fact_context,
                documents,
                structured_pool,
                summary_pool,
                fragment_pool,
            )
        _require_nonempty_string(review.get("reason"), f"{key}.reason")

    if reviewed_fact_keys != set(facts):
        missing = sorted(set(facts) - reviewed_fact_keys)
        raise ReviewValidationError(f"模型遗漏事实项: {missing}")

    proposition_reviews = _require_list(
        result.get("proposition_reviews"),
        "proposition_reviews",
    )
    proposition_statuses: dict[str, str] = {}
    for raw_review in proposition_reviews:
        review = _require_object(raw_review, "proposition_reviews项")
        _require_exact_keys(review, {"proposition_id", "status", "reason"}, "proposition_reviews项")
        proposition_id = _require_nonempty_string(review.get("proposition_id"), "proposition_id")
        if proposition_id not in propositions:
            raise ReviewValidationError(f"模型输出了不存在的proposition_id: {proposition_id}")
        if proposition_id in proposition_statuses:
            raise ReviewValidationError(f"proposition_id重复: {proposition_id}")
        status = review.get("status")
        if status not in FACT_STATUSES:
            raise ReviewValidationError(f"命题状态无效: {status}")
        proposition_statuses[proposition_id] = status
        _require_nonempty_string(review.get("reason"), f"{proposition_id}.reason")
    if set(proposition_statuses) != set(propositions):
        raise ReviewValidationError("proposition_reviews未覆盖全部命题")

    overall_status = result.get("overall_logic_status")
    if overall_status not in FACT_STATUSES:
        raise ReviewValidationError("overall_logic_status无效")
    if parsed_expression is not None:
        computed_status = evaluate_expression(parsed_expression, proposition_statuses)
        if overall_status != computed_status:
            raise ReviewValidationError(
                "overall_logic_status计算错误，"
                f"expression={expression}，"
                f"命题状态={proposition_statuses}，"
                f"模型={overall_status}，程序={computed_status}"
            )
    elif overall_status != "UNKNOWN":
        raise ReviewValidationError("逻辑关系AMBIGUOUS时overall_logic_status必须为UNKNOWN")

    review_status = result.get("review_status")
    case_status = result.get("case_status")
    if review_status not in REVIEW_STATUSES:
        raise ReviewValidationError("review_status无效")
    if case_status not in CASE_STATUSES:
        raise ReviewValidationError("case_status无效")

    final_conclusion = _require_object(result.get("final_conclusion"), "final_conclusion")
    _require_exact_keys(final_conclusion, {"result", "is_final", "reason"}, "final_conclusion")
    final_result = final_conclusion.get("result")
    is_final = final_conclusion.get("is_final")
    if final_result not in FINAL_RESULTS or not isinstance(is_final, bool):
        raise ReviewValidationError("final_conclusion字段无效")
    _require_nonempty_string(final_conclusion.get("reason"), "final_conclusion.reason")

    supplemental_requests = _require_list(
        result.get("supplemental_evidence_requests"),
        "supplemental_evidence_requests",
    )
    if review_status == "OPEN" and not supplemental_requests:
        raise ReviewValidationError("OPEN时必须输出补充证据请求")
    if review_status != "OPEN" and supplemental_requests:
        raise ReviewValidationError("非OPEN状态不得输出补充证据请求")
    for raw_request in supplemental_requests:
        request = _require_object(raw_request, "supplemental_evidence_requests项")
        _require_exact_keys(
            request,
            {"proposition_id", "target_fact", "requested_data", "reason"}
            | ({"requested_source"} if "requested_source" in request else set()),
            "supplemental_evidence_requests项",
        )
        proposition_id = _require_nonempty_string(request.get("proposition_id"), "request.proposition_id")
        target_fact = _require_nonempty_string(request.get("target_fact"), "request.target_fact")
        if not any(key[0] == proposition_id and key[3] == target_fact for key in facts):
            raise ReviewValidationError("补充证据请求没有对应事实项")
        _require_nonempty_string(request.get("requested_data"), "requested_data")
        requested_source = request.get("requested_source")
        if requested_source is not None:
            _require_nonempty_string(requested_source, "requested_source")
        _require_nonempty_string(request.get("reason"), "request.reason")

    manual_review_reason = result.get("manual_review_reason")
    if manual_review_reason is not None:
        _require_nonempty_string(manual_review_reason, "manual_review_reason")

    if relation_status == "AMBIGUOUS":
        if review_status != "BLOCKED":
            raise ReviewValidationError("逻辑关系AMBIGUOUS时review_status必须为BLOCKED")
        if case_status != "待人工终审":
            raise ReviewValidationError("逻辑关系AMBIGUOUS时case_status必须为待人工终审")
        if final_result != "待人工终审" or is_final:
            raise ReviewValidationError("逻辑关系AMBIGUOUS时最终结论必须为非最终的待人工终审")
        if manual_review_reason is None:
            raise ReviewValidationError("逻辑关系AMBIGUOUS时必须说明人工复核原因")
        return

    expected_case_status = {
        "CLOSED": "复核完成",
        "OPEN": "待补充证据",
        "BLOCKED": "数据阻断",
        "CONFLICTED": "证据冲突",
    }[review_status]
    if case_status != expected_case_status:
        raise ReviewValidationError(
            f"case_status与review_status不一致，应为{expected_case_status}"
        )

    if review_status == "CLOSED":
        if overall_status not in {"SUPPORTED", "NOT_SUPPORTED"}:
            raise ReviewValidationError("CLOSED时整体逻辑必须为SUPPORTED或NOT_SUPPORTED")
        expected_final = "合规" if overall_status == "SUPPORTED" else "违规"
        if final_result != expected_final or not is_final:
            raise ReviewValidationError(f"CLOSED时最终结论应为最终的{expected_final}")
    elif review_status in {"OPEN", "BLOCKED"}:
        if overall_status != "UNKNOWN":
            raise ReviewValidationError(f"{review_status}时整体逻辑必须为UNKNOWN")
        if final_result != "暂无法判定" or is_final:
            raise ReviewValidationError(f"{review_status}时最终结论必须为非最终的暂无法判定")
    elif review_status == "CONFLICTED":
        if overall_status != "CONFLICTED":
            raise ReviewValidationError("CONFLICTED时整体逻辑必须为CONFLICTED")
        if final_result != "待人工终审" or is_final:
            raise ReviewValidationError("CONFLICTED时最终结论必须为非最终的待人工终审")
        if manual_review_reason is None:
            raise ReviewValidationError("CONFLICTED时必须说明人工复核原因")
