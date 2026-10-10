from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from evidence_retriever.context_builder import build_review_context


class ContextFactIdTests(unittest.TestCase):
    def test_context_replaces_labels_with_ids_and_preserves_other_data(self) -> None:
        tags = [
            {
                "一级证据域": "诊断",
                "二级证据标签": "标准诊断",
                "target_fact": "本次诊断",
                "necessity": "REQUIRED",
            },
            {
                "一级证据域": "病史",
                "二级证据标签": "既往史",
                "target_fact": "既往病史",
                "necessity": "SUPPLEMENTARY",
            },
        ]
        rule = {
            "rule_id": "RULE-TEST",
            "result": {
                "source": {"violation_item": "测试项目", "violation_description": "测试规则"},
                "logical_expression": "NOT P01",
                "appeal_propositions": [
                    {
                        "proposition_id": "P01",
                        "statement": "测试命题",
                        "logic_notes": "测试说明",
                        "required_tags": tags[:1],
                        "supplementary_tags": tags[1:],
                    }
                ],
                "program_checks": [{"check": "测试检查"}],
            },
        }
        query_result = {
            "rule_id": "RULE-TEST",
            "mdtrt_id": "TEST",
            "propositions": [
                {
                    "proposition_id": "P01",
                    "evidence": [
                        {
                            **tag,
                            "query_result": {
                                "status": "RETRIEVED",
                                "source": "DS-S-003 诊断信息",
                                "query_scope_status": "QUERY_SCOPE_UNRESOLVED",
                                "query_scope_reason": "测试范围缺口",
                                "data": [{"diseName": "测试诊断"}],
                            },
                        }
                        for tag in tags
                    ],
                }
            ],
            "medical_record_filter": {
                "enabled": False,
                "matched_evidence_tags": [tags[0]],
            },
        }
        original = copy.deepcopy((rule, query_result))
        with patch("evidence_retriever.context_builder.StandardViewFieldDictionary"):
            context = build_review_context([rule], query_result)
        self.assertEqual((rule, query_result), original)
        serialized = json.dumps(context, ensure_ascii=False)
        self.assertNotIn("一级证据域", serialized)
        self.assertNotIn("二级证据标签", serialized)
        self.assertEqual(context["logical_expression"], "NOT P01")
        self.assertEqual(context["program_checks"], rule["result"]["program_checks"])
        facts = context["propositions"][0]["evidence"]
        self.assertEqual([fact["fact_id"] for fact in facts], ["P01-F01", "P01-F02"])
        for fact, tag in zip(facts, tags):
            self.assertEqual(fact["target_fact"], tag["target_fact"])
            self.assertEqual(fact["necessity"], tag["necessity"])
            self.assertEqual(fact["query_result"]["status"], "RETRIEVED")
            self.assertEqual(fact["query_result"]["query_scope_status"], "QUERY_SCOPE_UNRESOLVED")
            self.assertEqual(fact["query_result"]["query_scope_reason"], "测试范围缺口")
            reference = fact["query_result"]["structured_data_refs"][0]
            self.assertEqual(context["structured_data_pool"][reference], {"diseName": "测试诊断"})


if __name__ == "__main__":
    unittest.main()
