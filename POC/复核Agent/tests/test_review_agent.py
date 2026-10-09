from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from review_agent.service import ReviewService
from review_agent.validation import (
    ExpressionParser,
    ReviewValidationError,
    evaluate_expression,
    _medical_quote_matches_source,
    validate_context,
    validate_result,
)


PROGRAM_ROOT = Path(__file__).resolve().parents[1]
POC_ROOT = PROGRAM_ROOT.parent
CONTEXT_PATH = POC_ROOT / "查询结果" / "复核上下文.json"


def build_unknown_result(context: dict, review_round: int = 1) -> dict:
    proposition_ids = [item["proposition_id"] for item in context["propositions"]]
    fact_reviews = []
    for proposition in context["propositions"]:
        for fact in proposition["evidence"]:
            fact_reviews.append(
                {
                    "proposition_id": proposition["proposition_id"],
                    "一级证据域": fact["一级证据域"],
                    "二级证据标签": fact["二级证据标签"],
                    "target_fact": fact["target_fact"],
                    "necessity": fact["necessity"],
                    "status": "UNKNOWN",
                    "evidence": [],
                    "reason": "当前证据不足以判断该事实。",
                }
            )
    return {
        "rule_id": context["rule_id"],
        "review_round": review_round,
        "proposition_relation": {
            "status": "CONFIRMED",
            "expression": " AND ".join(proposition_ids),
            "basis_quotes": [context["review_basis"]["appeal_objective"]],
        },
        "fact_reviews": fact_reviews,
        "proposition_reviews": [
            {
                "proposition_id": proposition_id,
                "status": "UNKNOWN",
                "reason": "存在必要事实无法判断。",
            }
            for proposition_id in proposition_ids
        ],
        "overall_logic_status": "UNKNOWN",
        "review_status": "BLOCKED",
        "case_status": "数据阻断",
        "final_conclusion": {
            "result": "暂无法判定",
            "is_final": False,
            "reason": "关键事实缺少可用证据。",
        },
        "supplemental_evidence_requests": [],
        "manual_review_reason": None,
    }


class FakeModelClient:
    def __init__(self, result: dict):
        self.result = result
        self.system_prompt = ""
        self.user_prompt = ""
        self.schema: dict = {}

    def generate(self, system_prompt: str, user_prompt: str, schema: dict) -> dict:
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        self.schema = schema
        return copy.deepcopy(self.result)


class RepairingFakeModelClient:
    def __init__(self, invalid_result: dict, repaired_result: dict):
        self.invalid_result = invalid_result
        self.repaired_result = repaired_result
        self.calls = 0
        self.user_prompts: list[str] = []

    def generate(self, system_prompt: str, user_prompt: str, schema: dict) -> dict:
        self.calls += 1
        self.user_prompts.append(user_prompt)
        if self.calls == 1:
            return copy.deepcopy(self.invalid_result)
        return copy.deepcopy(self.repaired_result)


class ReviewAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = json.loads(CONTEXT_PATH.read_text(encoding="utf-8"))

    def test_context_is_valid(self) -> None:
        validate_context(self.context)

    def test_service_builds_prompt_and_validates_result(self) -> None:
        result = build_unknown_result(self.context)
        client = FakeModelClient(result)
        service = ReviewService(client)

        actual = service.review(self.context, review_round=1)

        self.assertEqual(actual, result)
        self.assertIn("复核 Agent", client.system_prompt)
        self.assertIn('"review_context"', client.user_prompt)
        self.assertIn('"logic_basis_quote_sources"', client.user_prompt)
        self.assertEqual(client.schema["type"], "object")

    def test_service_repairs_non_verbatim_logic_quote_once(self) -> None:
        repaired_result = build_unknown_result(self.context)
        invalid_result = copy.deepcopy(repaired_result)
        invalid_result["proposition_relation"]["basis_quotes"] = [
            "患者本次就诊具有明确的胰岛素治疗适应症...这是核心路径。"
        ]
        client = RepairingFakeModelClient(invalid_result, repaired_result)
        service = ReviewService(client)

        actual = service.review(self.context, review_round=1)

        self.assertEqual(actual, repaired_result)
        self.assertEqual(client.calls, 2)
        self.assertIn("validation_error", client.user_prompts[1])
        self.assertIn("不得把statement与logic_notes拼接", client.user_prompts[1])

    def test_structured_quote_must_exist(self) -> None:
        result = build_unknown_result(self.context)
        context_facts = [
            fact
            for proposition in self.context["propositions"]
            for fact in proposition["evidence"]
        ]
        fact_index = next(
            index
            for index, fact in enumerate(context_facts)
            if fact["query_result"].get("structured_data")
            or fact["query_result"].get("structured_data_refs")
        )
        context_fact = context_facts[fact_index]
        structured_refs = context_fact["query_result"].get("structured_data_refs", [])
        if structured_refs:
            first_record = self.context["structured_data_pool"][structured_refs[0]]
        else:
            first_record = context_fact["query_result"]["structured_data"][0]
        location, value = next(iter(first_record.items()))
        fact_review = result["fact_reviews"][fact_index]
        fact_review["status"] = "SUPPORTED"
        fact_review["evidence"] = [
            {
                "source": context_fact["query_result"]["source"].split("、", 1)[0],
                "evidence_type": "STRUCTURED",
                "document_id": None,
                "location": location,
                "quote": str(value),
            }
        ]
        validate_result(result, self.context, 1)

        fact_review["evidence"][0]["quote"] = "上下文中不存在的结构化值"
        with self.assertRaises(ReviewValidationError):
            validate_result(result, self.context, 1)

    def test_supported_fact_requires_evidence(self) -> None:
        result = build_unknown_result(self.context)
        result["fact_reviews"][0]["status"] = "SUPPORTED"
        with self.assertRaises(ReviewValidationError):
            validate_result(result, self.context, 1)

    def test_medical_document_can_support_another_tag(self) -> None:
        result = build_unknown_result(self.context)
        first_fact = result["fact_reviews"][0]
        first_fact["status"] = "SUPPORTED"
        first_fact["evidence"] = [
            {
                "source": "DS-M-IP-002 入院记录",
                "evidence_type": "MEDICAL_DOCUMENT",
                "document_id": "DOC-DS-M-IP-002-119797913-01",
                "location": "患者基本信息",
                "quote": "性别：女",
            }
        ]
        validate_result(result, self.context, 1)

    def test_expression_is_computed_by_program(self) -> None:
        expression = ExpressionParser("P01 AND (P02 OR P03)").parse()
        self.assertEqual(
            evaluate_expression(
                expression,
                {
                    "P01": "SUPPORTED",
                    "P02": "NOT_SUPPORTED",
                    "P03": "SUPPORTED",
                },
            ),
            "SUPPORTED",
        )
        self.assertEqual(
            evaluate_expression(
                expression,
                {
                    "P01": "SUPPORTED",
                    "P02": "UNKNOWN",
                    "P03": "NOT_SUPPORTED",
                },
            ),
            "UNKNOWN",
        )

    def test_logic_basis_allows_ordered_ellipsis_segments(self) -> None:
        result = build_unknown_result(self.context)
        source_text = self.context["review_basis"]["violation_description"]
        result["proposition_relation"]["basis_quotes"] = [
            source_text[:12] + "..." + source_text[-12:]
        ]
        validate_result(result, self.context, 1)

        result["proposition_relation"]["basis_quotes"] = [
            source_text[:12] + "...上下文中不存在的条件..." + source_text[-12:]
        ]
        with self.assertRaises(ReviewValidationError):
            validate_result(result, self.context, 1)

    def test_medical_quote_ignores_whitespace_differences(self) -> None:
        source = "患者 4 天前出现发热，体温最高达 39.2  ℃。"
        quote = "患者 4 天前出现发热，体温最高达 39.2 ℃。"
        self.assertTrue(_medical_quote_matches_source(quote, source))

    def test_medical_quote_allows_trailing_ellipsis(self) -> None:
        source = (
            "予以对症处理，完善微创手术治疗前准备"
            "（术前感染筛查如HIV，梅毒，乙肝，丙肝，血型筛查等）"
        )
        self.assertTrue(
            _medical_quote_matches_source(
                "完善微创手术治疗前准备（术前感染筛查如HIV...",
                source,
            )
        )
        self.assertFalse(
            _medical_quote_matches_source("不存在的手术准备内容...", source)
        )


if __name__ == "__main__":
    unittest.main()
