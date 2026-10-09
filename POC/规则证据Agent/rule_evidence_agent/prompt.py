from __future__ import annotations

import json
from pathlib import Path

from .catalog import EvidenceTag
from .excel_reader import RuleRecord


PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "system_prompt_分阶段保留路径版.txt"


def load_system_prompt() -> str:
    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"系统提示词文件不存在: {PROMPT_PATH}")
    prompt = PROMPT_PATH.read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError("系统提示词不能为空")
    return prompt


def build_user_prompt(record: RuleRecord, tags: list[EvidenceTag]) -> str:
    payload = {
        "source_rule": {
            "rule_id": record.rule_id,
            "source_rule_name": record.source_rule_name,
            "case_parameters": {
                "mdtrt_id": record.mdtrt_id,
                "medins_id": record.medins_id,
                "visit_no": record.visit_no,
            },
            "item": {"code": record.item_code, "name": record.item_name},
            "rule_text": record.rule_text,
        },
        "tag_catalog": [tag.to_prompt_dict() for tag in tags],
    }
    return json.dumps(payload, ensure_ascii=False)


def build_repair_prompt(
    record: RuleRecord,
    tags: list[EvidenceTag],
    validation_error: str,
) -> str:
    payload = json.loads(build_user_prompt(record, tags))
    payload["validation_feedback"] = {
        "error": validation_error,
        "instruction": (
            "上一次输出未通过程序校验。请根据error修正对应字段，重新输出符合系统提示词的完整合法JSON。"
            "必须包含顶层非空字符串logical_expression，使用实际定义的proposition_id、AND、OR、NOT和圆括号，"
            "覆盖全部命题编号；只有一个命题时直接填写该编号。不得遗漏该字段，不得根据患者证据倒推逻辑。"
            "一级证据域和二级证据标签必须来自tag_catalog中的同一条完整记录，禁止跨记录拼接。"
            "不要新增系统提示词未定义的字段，不要改变既有JSON结构。"
        ),
    }
    return json.dumps(payload, ensure_ascii=False)
