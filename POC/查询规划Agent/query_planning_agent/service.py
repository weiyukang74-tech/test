from __future__ import annotations

import json
import unicodedata
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Any

from .client import OpenAICompatibleClient
from .prompt import (
    FALLBACK_SEARCH_FIELDS,
    LAB_FALLBACK_SEARCH_FIELDS,
    LAB_SEARCH_FIELDS,
    MEDICAL_RECORD_RETRY_SYSTEM_PROMPT,
    MEDICAL_RECORD_SYSTEM_PROMPT,
    SEARCH_FIELDS,
    SYSTEM_PROMPT,
    build_medical_record_repair_prompt,
    build_medical_record_retry_prompt,
    build_medical_record_user_prompt,
    build_repair_prompt,
    build_user_prompt,
)
from .record_feedback import RecordFeedbackSource, build_record_excerpts


class PlanningValidationError(ValueError):
    pass


EXPENSE_TRIGGER_DOMAINS = {"费用", "手术操作"}
LAB_TRIGGER_DOMAINS = {"检验"}

FALLBACK_STRATEGY = [
    "ALL_CHRG_TYPE_SOURCE_EXACT",
    "ALL_CHRG_TYPE_CORE_QUALIFIER",
    "ALL_CHRG_TYPE_EXPANDED",
]
LAB_FALLBACK_STRATEGY: list[str] = []

GENERIC_MEDICAL_RECORD_TERMS = {
    "患者",
    "病历",
    "是否",
    "相关",
    "本次",
    "检查",
}

# 查询程序要求每个证据点的病程词根不超过5个
MAX_RECORD_SEARCH_TERMS = 5
# 命中不超过这个篇数就按原文补充：0篇为完全没命中，1篇也可能是写法没覆盖到
RECORD_FEEDBACK_TRIGGER_MAX_HITS = 1
# 每个证据点最多补3个词根，每条规则最多补3个证据点
RECORD_FEEDBACK_MAX_ADDED_TERMS = 3
RECORD_FEEDBACK_MAX_CALLS_PER_RULE = 3


def _normalize_medical_term(term: str) -> str:
    return unicodedata.normalize("NFKC", term).casefold()


def _evidence_tag_labels(facts: list[dict[str, Any]]) -> set[str]:
    return {
        _normalize_medical_term(label)
        for label in (
            str(fact.get("二级证据标签", "")).strip() for fact in facts
        )
        if label
    }


def _medical_term_rejection(term: str, evidence_tag_labels: set[str]) -> str | None:
    normalized = _normalize_medical_term(term)
    if normalized in GENERIC_MEDICAL_RECORD_TERMS:
        return "包含无区分度词语"
    if normalized in evidence_tag_labels:
        return "不能直接使用证据标签名称"
    if len(normalized) < 2 or normalized.isdigit():
        return "不能是单个字符或纯数字"
    return None


def load_rule_evidence(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("规则证据JSON顶层必须是数组")
    return payload


def collect_rule_facts(
    rule: dict[str, Any],
    expense_tags: set[tuple[str, str]] | None = None,
    medical_record_sources: dict[tuple[str, str], list[str]] | None = None,
) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    expense_tags = expense_tags or set()
    medical_record_sources = medical_record_sources or {}
    for proposition in rule.get("result", {}).get("appeal_propositions", []):
        proposition_id = str(proposition.get("proposition_id", "")).strip()
        proposition_statement = str(proposition.get("statement", "")).strip()
        for group_name in ("required_tags", "supplementary_tags"):
            for tag in proposition.get(group_name, []):
                primary = str(tag.get("一级证据域", "")).strip()
                secondary = str(tag.get("二级证据标签", "")).strip()
                mapped_to_expense = (primary, secondary) in expense_tags
                facts.append(
                    {
                        "proposition_id": proposition_id,
                        "proposition_statement": proposition_statement,
                        "一级证据域": primary,
                        "二级证据标签": secondary,
                        "target_fact": str(tag.get("target_fact", "")).strip(),
                        "necessity": str(tag.get("necessity", "")).strip(),
                        "mapped_to_expense": mapped_to_expense,
                        "triggers_expense": (
                            mapped_to_expense or primary in EXPENSE_TRIGGER_DOMAINS
                        ),
                        "triggers_lab": primary in LAB_TRIGGER_DOMAINS,
                        "query_domain": (
                            "检验报告明细"
                            if primary in LAB_TRIGGER_DOMAINS
                            else "费用明细"
                            if primary in EXPENSE_TRIGGER_DOMAINS
                            else None
                        ),
                        "medical_record_sources": list(
                            medical_record_sources.get((primary, secondary), [])
                        ),
                        "proposition_logic_notes": str(
                            proposition.get("logic_notes", "")
                        ).strip(),
                    }
                )
    return facts


def _copy_fact(fact: dict[str, Any]) -> dict[str, str]:
    return {
        "proposition_id": str(fact.get("proposition_id", "")).strip(),
        "一级证据域": str(fact.get("一级证据域", "")).strip(),
        "二级证据标签": str(fact.get("二级证据标签", "")).strip(),
        "target_fact": str(fact.get("target_fact", "")).strip(),
        "necessity": str(fact.get("necessity", "")).strip(),
    }


class QueryPlanningService:
    def __init__(
        self,
        client: OpenAICompatibleClient,
        record_feedback: RecordFeedbackSource | None = None,
    ):
        self.client = client
        self.record_feedback = record_feedback

    def plan(
        self,
        rule: dict[str, Any],
        candidate_facts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        expense_facts = [
            fact
            for fact in candidate_facts
            if fact.get("triggers_expense") is True
            or fact.get("triggers_lab") is True
        ]
        medical_facts = [
            fact
            for fact in candidate_facts
            if fact.get("medical_record_sources")
        ]
        if not expense_facts and not medical_facts:
            return self.empty_plan(rule)

        rule_id = str(rule.get("rule_id", "")).strip()
        violation_item = str(
            rule.get("result", {}).get("source", {}).get("violation_item", "")
        ).strip()
        violation_description = str(
            rule.get("result", {}).get("source", {}).get("violation_description", "")
        ).strip()
        rule_text = "\n".join(
            part for part in (violation_item, violation_description) if part
        )
        expense_planning_facts = [
            {
                "fact_id": f"E{index:02d}",
                **_copy_fact(fact),
                "triggers_expense": fact.get("triggers_expense") is True,
                "triggers_lab": fact.get("triggers_lab") is True,
                "query_domain": fact.get("query_domain"),
                "proposition_statement": fact.get("proposition_statement", ""),
                "proposition_logic_notes": fact.get("proposition_logic_notes", ""),
            }
            for index, fact in enumerate(expense_facts, start=1)
        ]
        expense_keyword_groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
        if expense_planning_facts:
            user_prompt = build_user_prompt(
                rule_id,
                violation_item,
                violation_description,
                expense_planning_facts,
            )
            model_result = self.client.generate(SYSTEM_PROMPT, user_prompt)
            try:
                validated_groups = self._validate_model_result(
                    model_result,
                    rule_id,
                    violation_item,
                    violation_description,
                    expense_planning_facts,
                )
            except PlanningValidationError as validation_error:
                print(
                    f"[{rule_id}] 首次费用查询规划未通过校验，自动修正："
                    f"{validation_error}",
                    flush=True,
                )
                repaired_result = self.client.generate(
                    SYSTEM_PROMPT,
                    build_repair_prompt(
                        rule_id,
                        violation_item,
                        violation_description,
                        expense_planning_facts,
                        model_result,
                        str(validation_error),
                    ),
                )
                validated_groups = self._validate_model_result(
                    repaired_result,
                    rule_id,
                    violation_item,
                    violation_description,
                    expense_planning_facts,
                )
            for fact, planning_fact in zip(
                expense_facts,
                expense_planning_facts,
                strict=True,
            ):
                expense_keyword_groups[self._fact_key(fact)] = validated_groups[
                    planning_fact["fact_id"]
                ]

        medical_planning_facts = [
            {
                "fact_id": f"M{index:02d}",
                **_copy_fact(fact),
                "proposition_statement": str(
                    fact.get("proposition_statement", "")
                ).strip(),
                "medical_record_sources": list(fact["medical_record_sources"]),
            }
            for index, fact in enumerate(medical_facts, start=1)
        ]
        medical_terms: dict[tuple[str, str, str, str], list[str]] = {}
        if medical_planning_facts:
            medical_prompt = build_medical_record_user_prompt(
                rule_id,
                rule_text,
                medical_planning_facts,
            )
            medical_result = self.client.generate(
                MEDICAL_RECORD_SYSTEM_PROMPT,
                medical_prompt,
            )
            medical_result = self._clean_medical_record_result(
                medical_result, medical_planning_facts, rule_id
            )
            try:
                validated_terms = self._validate_medical_record_result(
                    medical_result,
                    rule_id,
                    medical_planning_facts,
                )
            except PlanningValidationError as validation_error:
                retained_facts, repair_facts, repair_errors = self._medical_record_repair_scope(
                    medical_result, rule_id, medical_planning_facts
                )
                print(
                    f"[{rule_id}] 首次病程查询规划未通过校验，自动修正："
                    f"{validation_error}",
                    flush=True,
                )
                repaired_result = self.client.generate(
                    MEDICAL_RECORD_SYSTEM_PROMPT,
                    build_medical_record_repair_prompt(
                        rule_id,
                        rule_text,
                        repair_facts,
                        {
                            "rule_id": rule_id,
                            "facts": [
                                fact for fact in medical_result.get("facts", [])
                                if isinstance(fact, dict)
                                and fact.get("fact_id") in {item["fact_id"] for item in repair_facts}
                            ] if isinstance(medical_result.get("facts"), list) else [],
                        },
                        "；".join(repair_errors),
                        forbidden_record_search_terms=sorted(
                            _evidence_tag_labels(medical_planning_facts)
                        ),
                    ),
                )
                repaired_result = self._clean_medical_record_result(
                    repaired_result, medical_planning_facts, rule_id
                )
                validated_terms = self._validate_medical_record_result(
                    repaired_result,
                    rule_id,
                    repair_facts,
                )
                validated_terms = {**retained_facts, **validated_terms}
            for fact, planning_fact in zip(
                medical_facts,
                medical_planning_facts,
                strict=True,
            ):
                medical_terms[self._fact_key(fact)] = validated_terms[
                    planning_fact["fact_id"]
                ]

        record_feedback_notes: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        if medical_terms and self.record_feedback is not None:
            record_feedback_notes = self._supplement_record_terms(
                rule_id,
                rule_text,
                medical_facts,
                medical_terms,
            )

        proposition_counters: defaultdict[str, int] = defaultdict(int)
        plans: list[dict[str, Any]] = []
        for fact in candidate_facts:
            fact_key = self._fact_key(fact)
            has_expense = fact_key in expense_keyword_groups
            has_medical = fact_key in medical_terms
            if not has_expense and not has_medical:
                continue
            proposition_id = fact["proposition_id"]
            proposition_counters[proposition_id] += 1
            if has_expense:
                plan = self._build_plan(
                    fact,
                    expense_keyword_groups[fact_key],
                    proposition_counters[proposition_id],
                )
            else:
                plan = {
                    "query_id": (
                        f"QP-{proposition_id}-"
                        f"{proposition_counters[proposition_id]:02d}"
                    ),
                    "supports_facts": [_copy_fact(fact)],
                    "source": "、".join(fact["medical_record_sources"]),
                    "view_name": "病历信息",
                    "query_type": "MEDICAL_RECORD_KEYWORD_FILTER",
                    "plan_origin": "TAG_MAPPED",
                    "supplement_reason": None,
                }
            if has_medical:
                plan["record_search_terms"] = medical_terms[fact_key]
                plan["medical_record_sources"] = list(
                    fact["medical_record_sources"]
                )
                if fact_key in record_feedback_notes:
                    plan["record_search_retry"] = record_feedback_notes[fact_key]
                if has_expense:
                    plan["query_type"] = "MULTI_SOURCE"
                    plan["source"] = "、".join(
                        [plan["source"], *fact["medical_record_sources"]]
                    )
                    plan["view_name"] = "费用明细、病历信息"
            plans.append(plan)

        result = self.empty_plan(rule)
        result["plans"] = plans
        return result

    def _supplement_record_terms(
        self,
        rule_id: str,
        rule_text: str,
        facts: list[dict[str, Any]],
        terms_by_key: dict[tuple[str, str, str, str], list[str]],
    ) -> dict[tuple[str, str, str, str], dict[str, Any]]:
        """对命中很少的病程证据点，用病历原文片段向模型补充一次词根。

        命中为0或1篇时触发；补充的词根必须在候选病程里真的命中，否则丢弃。
        命中不减少：原词根与新词根取并集，超出5个时优先舍弃没有命中的原词根。
        """
        if self.record_feedback is None:
            return {}
        tag_keys = [
            (
                str(fact.get("一级证据域", "")).strip(),
                str(fact.get("二级证据标签", "")).strip(),
            )
            for fact in facts
        ]
        try:
            candidates = self.record_feedback.collect(tag_keys)
        except Exception as exc:  # 预检失败只跳过反馈，不影响正常规划
            print(
                f"[{rule_id}] 病程原文预检失败，跳过关键词反馈：{exc}",
                flush=True,
            )
            return {}
        evidence_tag_labels = _evidence_tag_labels(facts)

        pending: list[
            tuple[
                tuple[str, str, str, str],
                dict[str, Any],
                list[str],
                list[dict[str, Any]],
                int,
                str,
            ]
        ] = []
        for fact, tag_key in zip(facts, tag_keys, strict=True):
            key = self._fact_key(fact)
            terms = list(terms_by_key.get(key) or [])
            records = candidates.get(tag_key) or []
            if not terms or not records:
                continue
            matched_documents = sum(
                1
                for record in records
                if self.record_feedback.match(record.get("recordContent"), terms)
            )
            if matched_documents > RECORD_FEEDBACK_TRIGGER_MAX_HITS:
                continue
            pending.append(
                (
                    key,
                    fact,
                    terms,
                    records,
                    matched_documents,
                    "NO_KEYWORD_MATCH"
                    if matched_documents == 0
                    else "LOW_KEYWORD_COVERAGE",
                )
            )
        pending.sort(
            key=lambda item: 0
            if str(item[1].get("necessity", "")).strip() == "REQUIRED"
            else 1
        )

        notes: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for key, fact, terms, records, matched_documents, reason in pending[
            :RECORD_FEEDBACK_MAX_CALLS_PER_RULE
        ]:
            excerpts, excerpt_stats = build_record_excerpts(records)
            generate_supplement = getattr(
                self.client, "generate_record_supplement", self.client.generate
            )
            answer = generate_supplement(
                MEDICAL_RECORD_RETRY_SYSTEM_PROMPT,
                build_medical_record_retry_prompt(
                    rule_id,
                    rule_text,
                    _copy_fact(fact),
                    terms,
                    excerpts,
                ),
            )
            original_hits: dict[str, int] = {}
            for term in terms:
                matched_count = sum(
                    1
                    for record in records
                    if self.record_feedback.match(record.get("recordContent"), [term])
                )
                if matched_count:
                    original_hits[term] = matched_count
            added: list[str] = []
            added_hits: dict[str, int] = {}
            rejected: list[dict[str, str]] = []
            raw_added = answer.get("added_record_search_terms")
            if isinstance(raw_added, list):
                for raw_term in raw_added:
                    term = str(raw_term).strip()
                    if not term or term in terms or term in added:
                        continue
                    rejection = _medical_term_rejection(
                        term, evidence_tag_labels
                    )
                    if rejection is not None:
                        rejected.append({"term": term, "reason": rejection})
                        continue
                    matched = [
                        record
                        for record in records
                        if self.record_feedback.match(
                            record.get("recordContent"), [term]
                        )
                    ]
                    if not matched:
                        rejected.append(
                            {"term": term, "reason": "候选病程中没有命中"}
                        )
                        continue
                    added.append(term)
                    added_hits[term] = len(matched)
                    if len(added) >= RECORD_FEEDBACK_MAX_ADDED_TERMS:
                        break

            note: dict[str, Any] = {
                "triggered": True,
                "reason": reason,
                "candidate_documents": excerpt_stats["candidate_documents"],
                "matched_documents": matched_documents,
                "exposed_documents": excerpt_stats["exposed_documents"],
                "exposed_chars": excerpt_stats["exposed_chars"],
                "original_terms": terms,
                "original_term_hits": original_hits,
                "added_terms": added,
                "added_term_hits": added_hits,
            }
            if not added:
                note["note"] = "病历片段中没有找到与该证据点对应的写法"
            if rejected:
                note["rejected_terms"] = rejected
            if added:
                # 优先保留有命中的新词根，其次是有命中的原词根，最后才是没命中的原词根
                ranked = [
                    *added,
                    *[term for term in terms if term in original_hits],
                    *[term for term in terms if term not in original_hits],
                ]
                covered = ranked[:MAX_RECORD_SEARCH_TERMS]
                replaced = [term for term in terms if term not in covered]
                if replaced:
                    note["replaced_terms"] = replaced
                terms_by_key[key] = covered
            notes[key] = note
            print(
                f"[{rule_id}] {key[0]}{key[2]} 词根命中{matched_documents}篇，"
                f"按原文补充：{added or '无'}",
                flush=True,
            )
        return notes

    @staticmethod
    def _fact_key(fact: dict[str, Any]) -> tuple[str, str, str, str]:
        return (
            str(fact.get("proposition_id", "")).strip(),
            str(fact.get("一级证据域", "")).strip(),
            str(fact.get("二级证据标签", "")).strip(),
            str(fact.get("target_fact", "")).strip(),
        )

    @staticmethod
    def empty_plan(rule: dict[str, Any]) -> dict[str, Any]:
        return {
            "rule_id": str(rule.get("rule_id", "")),
            "case_parameters": {
                "mdtrt_id": str(rule.get("mdtrt_id", "") or ""),
                "medins_id": str(rule.get("medins_id", "") or ""),
                "visit_no": str(rule.get("visit_no", "") or ""),
            },
            "plans": [],
        }

    @staticmethod
    def _validate_model_result(
        result: dict[str, Any],
        rule_id: str,
        violation_item: str,
        violation_description: str,
        planning_facts: list[dict[str, str]],
    ) -> dict[str, list[dict[str, Any]]]:
        if str(result.get("rule_id", "")).strip() != rule_id:
            raise PlanningValidationError("模型输出rule_id与输入不一致")
        model_facts = result.get("facts")
        if not isinstance(model_facts, list):
            raise PlanningValidationError("模型输出facts必须是数组")

        facts_by_id = {fact["fact_id"]: fact for fact in planning_facts}
        indexed: dict[str, list[dict[str, Any]]] = {}
        for model_fact in model_facts:
            if not isinstance(model_fact, dict):
                raise PlanningValidationError("模型输出的事实必须是对象")
            fact_id = str(model_fact.get("fact_id", "")).strip()
            if fact_id not in facts_by_id:
                raise PlanningValidationError(f"模型输出了不存在的fact_id: {fact_id}")
            if fact_id in indexed:
                raise PlanningValidationError(f"模型重复输出fact_id: {fact_id}")

            groups = model_fact.get("keyword_groups")
            if not isinstance(groups, list) or not groups:
                raise PlanningValidationError(f"{fact_id}.keyword_groups不能为空")
            if len(groups) > 6:
                raise PlanningValidationError(f"{fact_id}.keyword_groups不能超过6组")

            target_fact = facts_by_id[fact_id]["target_fact"]
            seen_phrases: set[str] = set()
            validated_groups: list[dict[str, Any]] = []
            for group in groups:
                if not isinstance(group, dict):
                    raise PlanningValidationError(f"{fact_id}的关键词组必须是对象")
                source_phrase = str(group.get("source_phrase", "")).strip()
                if not source_phrase or source_phrase in seen_phrases:
                    raise PlanningValidationError(
                        f"{fact_id}.source_phrase为空或重复"
                    )
                if source_phrase in target_fact:
                    source_path = "target_fact"
                elif source_phrase in facts_by_id[fact_id].get(
                    "proposition_statement", ""
                ):
                    source_path = "proposition.statement"
                elif source_phrase in facts_by_id[fact_id].get(
                    "proposition_logic_notes", ""
                ):
                    source_path = "proposition.logic_notes"
                elif source_phrase in violation_item:
                    source_path = "result.source.violation_item"
                elif source_phrase in violation_description:
                    source_path = "result.source.violation_description"
                else:
                    source_path = "query_planning_agent.expansion"
                seen_phrases.add(source_phrase)
                for field in ("required_terms", "alternative_terms"):
                    values = group.get(field)
                    if not isinstance(values, list) or any(
                        not isinstance(value, str) or not value.strip()
                        for value in values
                    ):
                        raise PlanningValidationError(
                            f"{fact_id}.{field}必须是字符串数组"
                        )
                if not group["required_terms"]:
                    raise PlanningValidationError(
                        f"{fact_id}.required_terms不能为空"
                    )
                validated_group = dict(group)
                validated_group["source_path"] = source_path
                validated_groups.append(validated_group)
            indexed[fact_id] = validated_groups

        missing = sorted(set(facts_by_id) - set(indexed))
        if missing:
            raise PlanningValidationError(f"模型遗漏事实: {missing}")
        return indexed

    @staticmethod
    def _clean_medical_record_result(
        result: dict[str, Any],
        planning_facts: list[dict[str, Any]],
        rule_id: str,
    ) -> dict[str, Any]:
        cleaned = deepcopy(result)
        labels = _evidence_tag_labels(planning_facts)
        removed: list[str] = []
        model_facts = cleaned.get("facts")
        if not isinstance(model_facts, list):
            return cleaned
        for fact in model_facts:
            if not isinstance(fact, dict) or not isinstance(fact.get("record_search_terms"), list):
                continue
            terms: list[str] = []
            seen: set[str] = set()
            for raw_term in fact["record_search_terms"]:
                term = raw_term.strip() if isinstance(raw_term, str) else ""
                rejection = _medical_term_rejection(term, labels) if term else "不是非空字符串"
                if rejection:
                    removed.append(f"{fact.get('fact_id', '')}: {raw_term!r}（{rejection}）")
                    continue
                normalized = _normalize_medical_term(term)
                if normalized not in seen:
                    seen.add(normalized)
                    terms.append(term)
            fact["record_search_terms"] = terms
        if removed:
            print(f"[{rule_id}] 已清理非法病程词根：{'；'.join(removed)}", flush=True)
        return cleaned

    @classmethod
    def _medical_record_repair_scope(
        cls,
        result: dict[str, Any],
        rule_id: str,
        planning_facts: list[dict[str, Any]],
    ) -> tuple[dict[str, list[str]], list[dict[str, Any]], list[str]]:
        model_facts = result.get("facts")
        expected_ids = {fact["fact_id"] for fact in planning_facts}
        if (
            str(result.get("rule_id", "")).strip() != rule_id
            or not isinstance(model_facts, list)
            or any(not isinstance(fact, dict) for fact in model_facts)
        ):
            return {}, planning_facts, ["返回的rule_id或facts结构无效，请重新输出全部事实"]
        identifiers = [str(fact.get("fact_id", "")).strip() for fact in model_facts]
        if len(set(identifiers)) != len(identifiers) or set(identifiers) - expected_ids:
            return {}, planning_facts, ["事实编号重复或包含未定义编号，请重新输出全部事实"]
        indexed = dict(zip(identifiers, model_facts, strict=True))
        retained: dict[str, list[str]] = {}
        repair_facts: list[dict[str, Any]] = []
        errors: list[str] = []
        for fact in planning_facts:
            fact_id = fact["fact_id"]
            try:
                retained.update(cls._validate_medical_record_result(
                    {"rule_id": rule_id, "facts": [indexed[fact_id]] if fact_id in indexed else []},
                    rule_id,
                    [fact],
                ))
            except PlanningValidationError as error:
                repair_facts.append(fact)
                errors.append(str(error))
        return retained, repair_facts, errors

    @staticmethod
    def _validate_medical_record_result(
        result: dict[str, Any],
        rule_id: str,
        planning_facts: list[dict[str, Any]],
    ) -> dict[str, list[str]]:
        if str(result.get("rule_id", "")).strip() != rule_id:
            raise PlanningValidationError("模型输出rule_id与输入不一致")
        model_facts = result.get("facts")
        if not isinstance(model_facts, list):
            raise PlanningValidationError("模型输出facts必须是数组")

        facts_by_id = {fact["fact_id"]: fact for fact in planning_facts}
        evidence_tag_labels = _evidence_tag_labels(planning_facts)
        indexed: dict[str, list[str]] = {}
        for model_fact in model_facts:
            if not isinstance(model_fact, dict):
                raise PlanningValidationError("模型输出的事实必须是对象")
            fact_id = str(model_fact.get("fact_id", "")).strip()
            if fact_id not in facts_by_id:
                raise PlanningValidationError(
                    f"模型输出了不存在的fact_id: {fact_id}"
                )
            if fact_id in indexed:
                raise PlanningValidationError(f"模型重复输出fact_id: {fact_id}")
            raw_terms = model_fact.get("record_search_terms")
            if not isinstance(raw_terms, list) or not raw_terms:
                raise PlanningValidationError(
                    f"{fact_id}.record_search_terms不能为空"
                )
            if any(not isinstance(term, str) or not term.strip() for term in raw_terms):
                raise PlanningValidationError(
                    f"{fact_id}.record_search_terms必须是非空字符串数组"
                )
            terms: list[str] = []
            normalized_terms: set[str] = set()
            for raw_term in raw_terms:
                term = raw_term.strip()
                rejection = _medical_term_rejection(term, evidence_tag_labels)
                if rejection is not None:
                    raise PlanningValidationError(
                        f"{fact_id}.record_search_terms{rejection}: {term}"
                    )
                normalized = _normalize_medical_term(term)
                if normalized in normalized_terms:
                    continue
                normalized_terms.add(normalized)
                terms.append(term)
            if not terms:
                raise PlanningValidationError(
                    f"{fact_id}.record_search_terms规范化后不能为空"
                )
            if len(terms) > 5:
                raise PlanningValidationError(
                    f"{fact_id}.record_search_terms去重后不能超过5个"
                )
            indexed[fact_id] = terms

        missing = sorted(set(facts_by_id) - set(indexed))
        if missing:
            raise PlanningValidationError(f"模型遗漏事实: {missing}")
        return indexed

    @staticmethod
    def _build_plan(
        fact: dict[str, str],
        keyword_groups: list[dict[str, Any]],
        proposition_sequence: int,
    ) -> dict[str, Any]:
        entities: list[dict[str, Any]] = []
        for index, group in enumerate(keyword_groups, start=1):
            source_phrase = str(group["source_phrase"]).strip()
            required_terms = list(
                dict.fromkeys(str(term).strip() for term in group["required_terms"])
            )
            alternative_terms = list(
                dict.fromkeys(
                    str(term).strip() for term in group["alternative_terms"]
                )
            )
            entities.append(
                {
                    "entity_id": f"E{index:02d}",
                    "source_exact": source_phrase,
                    "source_path": str(group["source_path"]),
                    "core_terms": required_terms[:1],
                    "qualifiers": required_terms[1:],
                    "normalized_terms": alternative_terms,
                    "alias_terms": [],
                }
            )

        proposition_id = fact["proposition_id"]
        query_domain = fact.get("query_domain") or "费用明细"
        is_lab = query_domain == "检验报告明细"
        return {
            "query_id": f"QP-{proposition_id}-{proposition_sequence:02d}",
            "supports_facts": [_copy_fact(fact)],
            "source": (
                "DS-S-004 检验报告"
                if is_lab
                else "DS-S-002 费用明细"
            ),
            "view_name": query_domain,
            "query_type": "STRUCTURED",
            "plan_origin": "TAG_MAPPED",
            "supplement_reason": None,
            "entities": entities,
            "search_fields": LAB_SEARCH_FIELDS if is_lab else SEARCH_FIELDS,
            "fallback_search_fields": (
                LAB_FALLBACK_SEARCH_FIELDS if is_lab else FALLBACK_SEARCH_FIELDS
            ),
            "fallback_strategy": (
                LAB_FALLBACK_STRATEGY if is_lab else FALLBACK_STRATEGY
            ),
        }

    @staticmethod
    def write(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
