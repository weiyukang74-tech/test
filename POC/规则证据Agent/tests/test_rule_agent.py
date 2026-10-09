from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from rule_evidence_agent.catalog import EvidenceTag, load_tags
from rule_evidence_agent.config import Settings
from rule_evidence_agent.excel_reader import RuleRecord, read_published_rules
from rule_evidence_agent.service import RuleDecompositionService
from rule_evidence_agent.validation import validate_result


class FakeModelClient:
    def generate(self, system_prompt: str, user_prompt: str, schema: dict) -> dict:
        return {
            "status": "READY",
            "source": {"violation_item": "碳酸镧", "violation_description": "碳酸镧仅限透析患者且存在高磷血症时使用。"},
            "regulatory_claim": {"statement": "当前记录可能缺少限定条件依据。"},
            "appealability": {"explanation": "可通过患者事实核验。"},
            "appeal_objective": "证明患者满足限定条件。",
            "appeal_propositions": [
                {
                    "proposition_id": "P01",
                    "statement": "患者属于透析患者。",
                    "logic_notes": "该事实是限定条件之一。",
                    "mapping_status": "matched",
                    "required_tags": [
                        {
                            "一级证据域": "治疗史",
                            "二级证据标签": "既往治疗",
                            "target_fact": "患者接受透析治疗的记录",
                            "necessity": "REQUIRED",
                        }
                    ],
                    "supplementary_tags": [],
                    "missing_required_facts": [],
                }
            ],
            "evidence_tasks": [{"task_id": "E01", "supports_propositions": ["P01"], "question": "是否存在透析治疗记录？"}],
            "program_checks": [],
        }


class RepairingFakeModelClient(FakeModelClient):
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, system_prompt: str, user_prompt: str, schema: dict) -> dict:
        self.calls += 1
        if self.calls == 1:
            result = super().generate(system_prompt, user_prompt, schema)
            result["appeal_propositions"][0]["required_tags"][0]["二级证据标签"] = "病程时间"
            return result
        return super().generate(system_prompt, user_prompt, schema)


TAGS = [EvidenceTag("治疗史", "既往治疗")]


class RuleAgentTests(unittest.TestCase):
    def test_thinking_mode_is_allowed(self) -> None:
        settings = Settings(
            base_url="https://example.invalid/v1",
            api_key="",
            model_name="qwen3.6-27b",
            model_param_scale="27B",
            response_format="json_object",
            enable_thinking=True,
            timeout_seconds=120,
            max_retries=0,
        )
        settings.validate()

    def test_hospital_export_rules_are_loaded_dynamically(self) -> None:
        input_path = Path(__file__).resolve().parents[3] / "datat" / "南山人医-数据导出" / "住院负面清单_处理.xlsx"
        records = read_published_rules(input_path)
        self.assertGreater(len(records), 0)
        self.assertEqual(len({record.rule_id for record in records}), len(records))
        self.assertTrue(all(record.rule_text for record in records))

    def test_actual_tag_catalog_uses_two_fields(self) -> None:
        tags = load_tags(Path(__file__).resolve().parents[1] / "标签目录.xlsx")
        self.assertEqual(len(tags), 85)
        self.assertEqual(set(tags[0].to_prompt_dict()), {"一级证据域", "二级证据标签"})

    def test_validation_accepts_tags_without_sources(self) -> None:
        validate_result(FakeModelClient().generate("", "", {}), TAGS)

    def test_service_writes_only_rule_evidence_json(self) -> None:
        record = RuleRecord("RULE-001", "规则一", "ITEM-001", "项目一", "规则一原文", date(2026, 1, 1), "V1")
        service = RuleDecompositionService(FakeModelClient(), TAGS)
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            summary = service.run([record], output_dir)
            self.assertEqual(summary.created, 1)
            self.assertEqual(sorted(path.name for path in output_dir.glob("*.json")), ["规则证据结果.json"])
            saved = json.loads((output_dir / "规则证据结果.json").read_text(encoding="utf-8"))
            self.assertNotIn("evidence_tasks", saved[0]["result"])
            tag = saved[0]["result"]["appeal_propositions"][0]["required_tags"][0]
            self.assertNotIn("主要来源", tag)
            self.assertNotIn("次要来源", tag)

    def test_service_repairs_invalid_tag_pair_once(self) -> None:
        record = RuleRecord("RULE-001", "规则一", "ITEM-001", "项目一", "规则一原文", date(2026, 1, 1), "V1")
        client = RepairingFakeModelClient()
        service = RuleDecompositionService(client, TAGS)
        result = service.decompose(record)
        tag = result["appeal_propositions"][0]["required_tags"][0]
        self.assertEqual(client.calls, 2)
        self.assertEqual((tag["一级证据域"], tag["二级证据标签"]), ("治疗史", "既往治疗"))

    def test_append_mode_skips_existing_rule(self) -> None:
        record = RuleRecord("RULE-001", "规则一", "ITEM-001", "项目一", "规则一原文", date(2026, 1, 1), "V1")
        service = RuleDecompositionService(FakeModelClient(), TAGS)
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            service.run([record], output_dir)
            before = (output_dir / "规则证据结果.json").read_text(encoding="utf-8")
            summary = service.run([record], output_dir)
            self.assertEqual((summary.created, summary.updated, summary.skipped), (0, 0, 1))
            self.assertEqual((output_dir / "规则证据结果.json").read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
