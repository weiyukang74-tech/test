from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "system_prompt.txt"

CITATION_REPAIR_SYSTEM_PROMPT = (
    "你只负责修复医保复核结果中无法通过原始数据校验的证据引用。"
    "只能依据每个issue提供的真实候选数据修复evidence，不得补写、概括或猜测原文。"
    "不得修改事实状态、命题状态、命题逻辑、理由或最终结论。"
    "找不到可验证证据时返回空evidence数组。只输出合法JSON对象。"
)

_CITATION_EVIDENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["evidence_type", "location", "quote"],
    "properties": {
        "source": {"type": "string", "minLength": 1},
        "evidence_type": {
            "type": "string",
            "enum": ["STRUCTURED", "MEDICAL_DOCUMENT"],
        },
        "document_id": {
            "anyOf": [
                {"type": "string", "minLength": 1},
                {"type": "null"},
            ]
        },
        "location": {"type": "string", "minLength": 1},
        "quote": {"type": "string", "minLength": 1},
    },
}

CITATION_REPAIR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["repairs"],
    "properties": {
        "repairs": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["issue_id", "evidence"],
                "properties": {
                    "issue_id": {"type": "string", "minLength": 1},
                    "evidence": {
                        "type": "array",
                        "items": _CITATION_EVIDENCE_SCHEMA,
                    },
                },
            },
        }
    },
}


def load_system_prompt() -> str:
    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"系统提示词文件不存在: {PROMPT_PATH}")
    prompt = PROMPT_PATH.read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError("系统提示词不能为空")
    return prompt


def _model_review_context(context: dict[str, Any]) -> dict[str, Any]:
    model_context = deepcopy(context)
    for proposition in model_context.get("propositions", []):
        if not isinstance(proposition, dict):
            continue
        proposition_id = str(proposition.get("proposition_id", ""))
        for fact_index, fact in enumerate(proposition.get("evidence", []), start=1):
            if isinstance(fact, dict):
                fact["fact_id"] = f"{proposition_id}-F{fact_index:02d}"
    return model_context


def build_user_prompt(context: dict[str, Any], review_round: int) -> str:
    model_context = _model_review_context(context)
    fact_source_constraints = []
    for proposition in model_context.get("propositions", []):
        proposition_id = proposition.get("proposition_id", "")
        for fact in proposition.get("evidence", []):
            query_result = fact.get("query_result", {})
            if not isinstance(query_result, dict):
                continue
            source = query_result.get("source")
            if source is None:
                source = query_result.get("sources", query_result.get("source_list"))
            has_documents = bool(query_result.get("document_refs"))
            if not has_documents:
                records = query_result.get("data", query_result.get("records", []))
                if isinstance(records, list):
                    has_documents = any(
                        isinstance(record, dict) and "recordContent" in record
                        for record in records
                    )
            fact_source_constraints.append(
                {
                    "fact_id": fact.get("fact_id"),
                    "proposition_id": proposition_id,
                    "一级证据域": fact.get("一级证据域"),
                    "二级证据标签": fact.get("二级证据标签"),
                    "target_fact": fact.get("target_fact"),
                    "available_sources": source,
                    "allowed_evidence_type": (
                        "MEDICAL_DOCUMENT" if has_documents else "STRUCTURED"
                    ),
                }
            )
    payload = {
        "review_round": review_round,
        "review_context": model_context,
        "fact_source_constraints": fact_source_constraints,
    }
    return (
        "请严格依据系统提示词复核以下输入，并且只输出合法JSON对象。\n"
        "本次复核必须以review_context.review_basis.violation_description中的原始规则为准，"
        "不得让后续proposition或target_fact新增原规则没有要求的‘诊断名称必须直接填写’条件。"
        "旧版target_fact即使只询问诊断名称、编码或字段是否存在，其缺失也只代表记录缺失；没有直接临床反证时，"
        "不得据此把疾病或临床命题判NOT_SUPPORTED，应判UNKNOWN。替代病因不等于排除目标病因。"
        "病原学明确阳性并存在相符症状时，应综合支持对应临床疾病；诊断字段缺名仅表示记录不完整。"
        "轻症等严重程度应结合生命体征、心肺检查、并发症、治疗场所和患者去向综合判断，"
        "不得仅因缺少同名文字而判NOT_SUPPORTED。\n"
        "review_context.logical_expression由规则证据Agent生成并固定提供，只能读取并用于组合命题状态，不得重新判断、生成或修改，也不要在输出中返回该字段。"
        "使用精简决策结构输出：fact_reviews只填写review_context中已有的fact_id、status、evidence和reason，不要重复proposition_id、证据域、证据标签、target_fact或necessity；程序会恢复这些字段。"
        "只填写review_status和final_reason，不要输出rule_id、mdtrt_id、review_round、overall_logic_status、case_status或final_conclusion，程序会确定性生成。"
        "补充证据请求使用fact_id关联事实；没有请求时省略supplemental_evidence_requests。"
        "除STRUCTURED证据的document_id外，不要输出null；其他可选字段没有值时直接省略。每个fact_reviews项都必须输出evidence数组，UNKNOWN且无证据时输出空数组。STRUCTURED证据必须填写真实查询结果source，每条证据只引用一个真实字段值，不要把多个值拼成一条quote；MEDICAL_DOCUMENT证据必须填写真实非空document_id且省略source。每个proposition_reviews项必须保留reason。\n"
        "以下为待复核输入：\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def build_repair_prompt(
    context: dict[str, Any],
    review_round: int,
    previous_result: dict[str, Any],
    validation_error: str,
) -> str:
    return (
        build_user_prompt(context, review_round)
        + "\n\n上一次输出未通过程序校验。请根据以下反馈修正后，重新输出完整合法JSON对象。"
        + "不要解释错误，不要新增字段，不要改变输出结构。"
        + "病历证据quote默认逐字复制同一字段的连续原文；原文过长时可以用...或……省略中间内容，"
        + "也可以在开头或结尾使用省略号表示省略前文或后文；所有非省略片段必须来自同一字段且逐字存在。"
        + "空格、换行或全角形式的差异可以忽略，文字、数字和符号不得改写；优先保持原文顺序，无法保持顺序时也不得改写或猜测。\n"
        + "MEDICAL_DOCUMENT证据只填写真实document_id、location和quote，source由程序根据document_id补齐；STRUCTURED证据必须填写当前真实查询结果中的source，每条证据只引用一个真实字段值，不要把多个值拼成一条quote；"
        + "同一事实可以引用当前query_result或本案共享structured_data_pool、expense_summary_pool中的真实结构化证据；"
        + "跨事实引用共享记录时，source必须与记录ID中的数据源编码一致，location和quote必须真实存在。\n"
        + "证据优先使用当前事实的查询来源；也可以使用本案共享证据池中能够直接支持当前临床事实的真实记录，"
        + "但必须保持来源、字段和值可验证，不得仅凭标签名称推测或借用无关证据。\n"
        + "完成各事实判断后，必须综合同一命题内全部已返回证据建立支持链。"
        + "SUPPLEMENTARY表示取证必要性，不代表证据证明力较弱；病原学阳性等直接结果不得因标签为SUPPLEMENTARY而忽略。"
        + "诊断名称缺失不等于疾病不存在，典型症状列表也不得在无明确限定时解释为全部必须满足。\n"
        + "原始违规描述的规则语义高于后续生成的proposition和target_fact；后两者不得新增原规则没有要求的"
        + "‘诊断名称必须直接填写’条件。病原学阳性可直接支持相应感染，若仅分型或严重程度缺失，应判UNKNOWN而非否定疾病。\n"
        + "除STRUCTURED证据的document_id外，不要输出null；其他可选字段没有值时直接省略。每个fact_reviews项都必须输出evidence数组，UNKNOWN且无证据时输出空数组。STRUCTURED证据必须填写source，每条证据只引用一个真实字段值，不要把多个值拼成一条quote；MEDICAL_DOCUMENT证据必须填写真实非空document_id且省略source。每个proposition_reviews项必须保留reason。\n"
        + json.dumps(
            {
                "validation_error": validation_error,
                "previous_result": previous_result,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def build_citation_repair_prompt(issues: list[dict[str, Any]]) -> str:
    return (
        "请逐项修复下列引用。每个issue_id必须原样返回且最多出现一次。"
        "只能从该issue的candidates中复制真实值：MEDICAL_DOCUMENT填写真实document_id、"
        "location和逐字quote，省略source；STRUCTURED填写真实source、location和单个字段值quote，"
        "document_id填写null。不能可靠修复时返回空evidence数组。"
        "不得输出事实状态、命题状态、理由、逻辑关系或最终结论。\n"
        + json.dumps({"issues": issues}, ensure_ascii=False, separators=(",", ":"))
    )
