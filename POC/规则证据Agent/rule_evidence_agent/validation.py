from __future__ import annotations

import re
from typing import Any

from .catalog import EvidenceTag


MAPPING_STATUSES = {"matched", "evidence_insufficient"}
LOGIC_TOKEN_PATTERN = re.compile(r"\s*(AND|OR|NOT|\(|\)|[A-Za-z0-9][A-Za-z0-9_-]*)")


def _require_nonempty_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    return value.strip()


def _expression_identifiers(expression: str) -> list[str]:
    tokens: list[str] = []
    position = 0
    while position < len(expression):
        match = LOGIC_TOKEN_PATTERN.match(expression, position)
        if not match:
            raise ValueError(f"logical_expression包含非法内容: {expression[position:]}")
        tokens.append(match.group(1))
        position = match.end()

    cursor = 0

    def parse_or() -> None:
        nonlocal cursor
        parse_and()
        while cursor < len(tokens) and tokens[cursor] == "OR":
            cursor += 1
            parse_and()

    def parse_and() -> None:
        nonlocal cursor
        parse_not()
        while cursor < len(tokens) and tokens[cursor] == "AND":
            cursor += 1
            parse_not()

    def parse_not() -> None:
        nonlocal cursor
        if cursor < len(tokens) and tokens[cursor] == "NOT":
            cursor += 1
            parse_not()
            return
        parse_primary()

    def parse_primary() -> None:
        nonlocal cursor
        if cursor >= len(tokens):
            raise ValueError("logical_expression缺少命题编号")
        token = tokens[cursor]
        if token == "(":
            cursor += 1
            parse_or()
            if cursor >= len(tokens) or tokens[cursor] != ")":
                raise ValueError("logical_expression缺少右括号")
            cursor += 1
            return
        if token in {"AND", "OR", "NOT", ")"}:
            raise ValueError("logical_expression语法无效")
        cursor += 1

    if not tokens:
        raise ValueError("logical_expression不能为空")
    parse_or()
    if cursor != len(tokens):
        raise ValueError("logical_expression存在未解析内容")
    return [token for token in tokens if token not in {"AND", "OR", "NOT", "(", ")"}]


def validate_result(result: dict[str, Any], tags: list[EvidenceTag]) -> None:
    """校验命题与标签映射结构；不替代模型进行医学或医保语义判断。"""
    propositions = result.get("appeal_propositions")
    if not isinstance(propositions, list) or not propositions:
        raise ValueError("appeal_propositions 必须是非空数组")

    catalog_by_pair = {tag.pair: tag for tag in tags}
    proposition_ids: set[str] = set()
    for index, proposition in enumerate(propositions, start=1):
        prefix = f"appeal_propositions[{index}]"
        if not isinstance(proposition, dict):
            raise ValueError(f"{prefix} 必须是对象")

        proposition_id = _require_nonempty_text(proposition.get("proposition_id"), f"{prefix}.proposition_id")
        if proposition_id in proposition_ids:
            raise ValueError(f"proposition_id 重复: {proposition_id}")
        proposition_ids.add(proposition_id)
        _require_nonempty_text(proposition.get("statement"), f"{prefix}.statement")

        mapping_status = proposition.get("mapping_status")
        if mapping_status not in MAPPING_STATUSES:
            raise ValueError(f"{prefix}.mapping_status 只能是 matched 或 evidence_insufficient")

        missing = proposition.get("missing_required_facts")
        if not isinstance(missing, list) or any(not isinstance(item, str) or not item.strip() for item in missing):
            raise ValueError(f"{prefix}.missing_required_facts 必须是字符串数组")
        if mapping_status == "matched" and missing:
            raise ValueError(f"{prefix} 为 matched 时 missing_required_facts 必须为空")
        if mapping_status == "evidence_insufficient" and not missing:
            raise ValueError(f"{prefix} 为 evidence_insufficient 时必须说明标签目录无法覆盖的患者侧事实")

        seen_pairs: set[tuple[str, str]] = set()
        for field, expected_necessity in (
            ("required_tags", "REQUIRED"),
            ("supplementary_tags", "SUPPLEMENTARY"),
        ):
            mapped_tags = proposition.get(field)
            if not isinstance(mapped_tags, list):
                raise ValueError(f"{prefix}.{field} 必须是数组")
            for tag_index, mapped_tag in enumerate(mapped_tags, start=1):
                tag_prefix = f"{prefix}.{field}[{tag_index}]"
                if not isinstance(mapped_tag, dict):
                    raise ValueError(f"{tag_prefix} 必须是对象")
                primary = _require_nonempty_text(mapped_tag.get("一级证据域"), f"{tag_prefix}.一级证据域")
                secondary = _require_nonempty_text(mapped_tag.get("二级证据标签"), f"{tag_prefix}.二级证据标签")
                pair = (primary, secondary)
                if pair not in catalog_by_pair:
                    raise ValueError(f"{tag_prefix} 使用了标签目录中不存在的标签: {primary}—{secondary}")
                if pair in seen_pairs:
                    raise ValueError(f"{prefix} 重复使用标签: {primary}—{secondary}")
                seen_pairs.add(pair)
                _require_nonempty_text(mapped_tag.get("target_fact"), f"{tag_prefix}.target_fact")
                if mapped_tag.get("necessity") != expected_necessity:
                    raise ValueError(f"{tag_prefix}.necessity 必须是 {expected_necessity}")

    expression = _require_nonempty_text(result.get("logical_expression"), "logical_expression")
    identifiers = _expression_identifiers(expression)
    if set(identifiers) != proposition_ids:
        raise ValueError(
            "logical_expression 引用范围不完整，"
            f"实际={sorted(set(identifiers))}，应为={sorted(proposition_ids)}"
        )
    repeated = sorted(identifier for identifier in proposition_ids if identifiers.count(identifier) != 1)
    if repeated:
        raise ValueError(f"logical_expression 中命题编号必须且只能出现一次: {repeated}")
