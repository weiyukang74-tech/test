from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .medical_record_filter import is_usable_medical_record_plan


PlanKey = tuple[str, str, str, str]


def load_query_plan(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("plans"), list):
        raise ValueError(f"查询规划文件格式无效: {path}")
    return payload


def index_query_plans(payload: dict[str, Any] | None) -> dict[PlanKey, dict[str, Any]]:
    if payload is None:
        return {}
    result: dict[PlanKey, dict[str, Any]] = {}
    for plan in payload.get("plans", []):
        supports_facts = plan.get("supports_facts")
        if not isinstance(supports_facts, list):
            supports_facts = [plan]
        for fact in supports_facts:
            key = (
                str(fact.get("proposition_id", "")).strip(),
                str(fact.get("一级证据域", "")).strip(),
                str(fact.get("二级证据标签", "")).strip(),
                str(fact.get("target_fact", "")).strip(),
            )
            if not all(key):
                raise ValueError(f"查询计划缺少事实定位字段: {plan}")
            if key in result:
                raise ValueError(f"查询计划重复覆盖事实: {key}")
            result[key] = plan
    return result


def safe_index_query_plans(
    payload: dict[str, Any] | None,
) -> tuple[dict[PlanKey, dict[str, Any]], list[str]]:
    if payload is None:
        return {}, []
    plans = payload.get("plans")
    if not isinstance(plans, list):
        return {}, ["查询规划的plans必须是数组"]

    result: dict[PlanKey, dict[str, Any]] = {}
    invalid_keys: set[PlanKey] = set()
    errors: list[str] = []
    for plan_index, plan in enumerate(plans, start=1):
        if not isinstance(plan, dict):
            errors.append(f"第{plan_index}个查询计划必须是对象")
            continue
        supports_facts = plan.get("supports_facts")
        if not isinstance(supports_facts, list):
            supports_facts = [plan]
        for fact_index, fact in enumerate(supports_facts, start=1):
            if not isinstance(fact, dict):
                errors.append(
                    f"第{plan_index}个查询计划的第{fact_index}个事实必须是对象"
                )
                continue
            key = (
                str(fact.get("proposition_id", "")).strip(),
                str(fact.get("一级证据域", "")).strip(),
                str(fact.get("二级证据标签", "")).strip(),
                str(fact.get("target_fact", "")).strip(),
            )
            if not all(key):
                errors.append(f"查询计划缺少事实定位字段: {plan}")
                continue
            if key in result or key in invalid_keys:
                result.pop(key, None)
                invalid_keys.add(key)
                errors.append(f"查询计划重复覆盖事实: {key}")
                continue
            result[key] = plan
    return result, errors


def _has_usable_entities(plan: dict[str, Any]) -> bool:
    entities = plan.get("entities")
    if not isinstance(entities, list):
        return False
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        values = [entity.get("source_exact")]
        for field in ("core_terms", "qualifiers", "normalized_terms", "alias_terms"):
            terms = entity.get(field)
            if isinstance(terms, list):
                values.extend(terms)
        if any(str(value or "").strip() for value in values):
            return True
    return False


def resolve_query_plan(
    plan_index: dict[PlanKey, dict[str, Any]],
    key: PlanKey,
) -> dict[str, Any] | None:
    exact_plan = plan_index.get(key)
    if exact_plan is not None and _has_usable_entities(exact_plan):
        return exact_plan

    proposition_id = key[0]
    inherited_plans: list[dict[str, Any]] = []
    seen_plans: set[int] = set()
    for indexed_key, plan in plan_index.items():
        if indexed_key[0] != proposition_id or id(plan) in seen_plans:
            continue
        seen_plans.add(id(plan))
        if _has_usable_entities(plan):
            inherited_plans.append(plan)
    if not inherited_plans:
        return None

    inherited = deepcopy(inherited_plans[0])
    query_ids = [
        str(plan.get("query_id", "")).strip()
        for plan in inherited_plans
        if str(plan.get("query_id", "")).strip()
    ]
    merged_entities: list[dict[str, Any]] = []
    entity_signatures: set[str] = set()
    for plan in inherited_plans:
        for entity in plan.get("entities", []):
            signature = json.dumps(entity, ensure_ascii=False, sort_keys=True)
            if signature in entity_signatures:
                continue
            entity_signatures.add(signature)
            merged_entity = deepcopy(entity)
            merged_entity["entity_id"] = f"E{len(merged_entities) + 1:02d}"
            merged_entities.append(merged_entity)

    def merged_list(field: str) -> list[Any]:
        values: list[Any] = []
        for plan in inherited_plans:
            current = plan.get(field)
            if not isinstance(current, list):
                continue
            for value in current:
                if value not in values:
                    values.append(value)
        return values

    inherited["query_id"] = f"INHERITED-{proposition_id}"
    inherited["supports_facts"] = []
    inherited["entities"] = merged_entities
    inherited["search_fields"] = merged_list("search_fields")
    inherited["fallback_search_fields"] = merged_list("fallback_search_fields")
    inherited["fallback_strategy"] = merged_list("fallback_strategy")
    inherited["plan_origin"] = "INHERITED_SAME_PROPOSITION"
    inherited["supplement_reason"] = "当前事实无专属计划，继承同一申诉命题的争议实体"
    inherited["inherited_from_query_ids"] = query_ids
    return inherited


def resolve_medical_record_plan(
    plan_index: dict[PlanKey, dict[str, Any]],
    key: PlanKey,
) -> dict[str, Any] | None:
    plan = plan_index.get(key)
    if plan is None:
        return None
    if not is_usable_medical_record_plan(plan):
        return None
    return plan
