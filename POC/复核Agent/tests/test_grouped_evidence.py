from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from review_agent.service import ReviewService
from review_agent.prompt import build_user_prompt
from review_agent.validation import (
    ReviewValidationError,
    expand_grouped_evidence,
    validate_context,
    validate_result,
)


class GroupedEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.values = ["带状疱疹后遗症", "带状疱疹性神经痛", "痛性周围神经病"]
        self.group = {
            "source": "DS-S-003 诊断信息",
            "evidence_type": "STRUCTURED",
            "location": "diseName",
            "quotes": self.values.copy(),
        }
        self.context = {
            "rule_id": "RULE-TEST",
            "mdtrt_id": "TEST",
            "logical_expression": "P03",
            "review_basis": {"violation_description": "测试规则"},
            "medical_documents": [],
            "program_checks": [],
            "propositions": [
                {
                    "proposition_id": "P03",
                    "evidence": [
                        {
                            "fact_id": "P03-F01",
                            "target_fact": "本次诊断",
                            "necessity": "REQUIRED",
                            "query_result": {
                                "source": self.group["source"],
                                "structured_data": [
                                    {"diseName": value} for value in self.values
                                ],
                            },
                        }
                    ],
                }
            ],
        }
        self.model_result = {
            "fact_reviews": [
                {
                    "fact_id": "P03-F01",
                    "status": "SUPPORTED",
                    "evidence": [self.group],
                    "reason": "测试引用",
                }
            ],
            "proposition_reviews": [
                {"proposition_id": "P03", "status": "SUPPORTED", "reason": "测试"}
            ],
            "review_status": "CLOSED",
            "final_reason": "测试结论",
        }

    def test_expansion_preserves_values_metadata_and_input(self) -> None:
        original = copy.deepcopy(self.group)
        expanded = expand_grouped_evidence([self.group])
        self.assertEqual([item["quote"] for item in expanded], self.values)
        for item in expanded:
            self.assertNotIn("quotes", item)
            self.assertEqual(item["source"], self.group["source"])
            self.assertEqual(item["location"], "diseName")
        self.assertEqual(self.group, original)

    def test_single_quotes_medical_evidence_and_empty_arrays_remain_compatible(self) -> None:
        items = [
            {
                "source": self.group["source"],
                "evidence_type": "STRUCTURED",
                "document_id": None,
                "location": "diseName",
                "quote": self.values[0],
            },
            {
                "evidence_type": "MEDICAL_DOCUMENT",
                "document_id": "DOC-TEST",
                "location": "主诉",
                "quote": "测试原文",
            },
        ]
        self.assertEqual(expand_grouped_evidence(items), items)
        self.assertEqual(expand_grouped_evidence([]), [])

    def test_invalid_group_formats_are_rejected(self) -> None:
        invalid_fields = [
            {"quotes": []},
            {"quotes": "诊断"},
            {"quotes": [""]},
            {"quotes": [" "]},
            {"quotes": [1]},
            {"quotes": [None]},
            {"quotes": [self.values[0], {"quote": self.values[1]}]},
            {"quote": self.values[0]},
            {"evidence_type": "MEDICAL_DOCUMENT", "document_id": "DOC-TEST"},
            {"source": ""},
            {"location": ""},
            {"document_id": "DOC-TEST"},
        ]
        for fields in invalid_fields:
            with self.subTest(fields=fields):
                with self.assertRaises(ReviewValidationError):
                    expand_grouped_evidence([{**self.group, **fields}])
        for items in (None, {}, [None]):
            with self.subTest(items=items):
                with self.assertRaises(ReviewValidationError):
                    expand_grouped_evidence(items)

    def test_model_groups_expand_before_individual_citation_validation(self) -> None:
        result = ReviewService._expand_model_result(self.model_result, self.context, 1)
        self.assertEqual(ReviewService._collect_citation_issues(result, self.context), [])
        self.assertEqual(result["proposition_relation"]["expression"], "P03")
        self.assertEqual(result["proposition_reviews"], self.model_result["proposition_reviews"])
        self.assertEqual(result["fact_reviews"][0]["status"], "SUPPORTED")
        self.assertEqual(len(result["fact_reviews"][0]["evidence"]), 3)

    def test_each_grouped_value_must_match_the_original_data(self) -> None:
        self.group["quotes"][1] = "不存在的诊断"
        result = ReviewService._expand_model_result(self.model_result, self.context, 1)
        issues = ReviewService._collect_citation_issues(result, self.context)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["evidence_index"], 1)
        self.assertEqual(issues[0]["evidence"]["quote"], "不存在的诊断")

    def test_wrong_source_and_field_are_rejected_for_every_value(self) -> None:
        for fields in ({"source": "DS-S-999 不存在的来源"}, {"location": "missing"}):
            with self.subTest(fields=fields):
                model_result = copy.deepcopy(self.model_result)
                model_result["fact_reviews"][0]["evidence"][0].update(fields)
                result = ReviewService._expand_model_result(model_result, self.context, 1)
                self.assertEqual(len(ReviewService._collect_citation_issues(result, self.context)), 3)

    def test_citation_repairs_use_the_same_group_expansion_and_validation(self) -> None:
        result = ReviewService._expand_model_result(self.model_result, self.context, 1)
        result["fact_reviews"][0]["evidence"] = [
            {**result["fact_reviews"][0]["evidence"][0], "quote": "不存在的诊断"}
        ]
        issues = ReviewService._collect_citation_issues(result, self.context)
        repaired = {
            "repairs": [{"issue_id": issues[0]["issue_id"], "evidence": [self.group]}]
        }
        ReviewService._apply_citation_repairs(result, issues, repaired)
        self.assertEqual(len(result["fact_reviews"][0]["evidence"]), 3)
        self.assertEqual(ReviewService._collect_citation_issues(result, self.context), [])


    def test_grouping_after_validation_is_saved_and_revalidated(self) -> None:
        result = ReviewService._expand_model_result(self.model_result, self.context, 1)
        ReviewService._set_computed_overall_status(result)
        ReviewService._complete_deterministic_fields(result)
        validate_result(result, self.context, 1)
        ReviewService._group_structured_evidence(result)
        validate_result(result, self.context, 1)
        evidence = result["fact_reviews"][0]["evidence"]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["quotes"], self.values)
        self.assertNotIn("quote", evidence[0])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            ReviewService.write_result(path, result)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["fact_reviews"][0]["evidence"], evidence)
            validate_result(saved, self.context, 1)
        result["fact_reviews"][0]["evidence"][0]["quotes"][1] = "不存在的诊断"
        with self.assertRaises(ReviewValidationError):
            validate_result(result, self.context, 1)

    def test_grouping_preserves_fact_source_field_and_medical_boundaries(self) -> None:
        evidence = expand_grouped_evidence([self.group])
        evidence.extend(
            [
                {**evidence[0], "source": "DS-S-004 其他来源"},
                {**evidence[0], "location": "diseCode"},
                {
                    "evidence_type": "MEDICAL_DOCUMENT",
                    "document_id": "DOC-TEST",
                    "location": "主诉",
                    "quote": "测试原文",
                },
            ]
        )
        result = {"fact_reviews": [{"evidence": evidence}, {"evidence": evidence[:1]}]}
        original = copy.deepcopy(result)
        ReviewService._group_structured_evidence(result)
        self.assertEqual(len(result["fact_reviews"][0]["evidence"]), 4)
        self.assertEqual(result["fact_reviews"][1], original["fact_reviews"][1])
        for index, fact in enumerate(result["fact_reviews"]):
            self.assertEqual(
                expand_grouped_evidence(fact["evidence"]),
                original["fact_reviews"][index]["evidence"],
            )
        grouped = copy.deepcopy(result)
        ReviewService._group_structured_evidence(result)
        self.assertEqual(result, grouped)

    def test_grouped_conflicted_fact_counts_individual_quotes(self) -> None:
        result = ReviewService._expand_model_result(self.model_result, self.context, 1)
        result["fact_reviews"][0]["status"] = "CONFLICTED"
        result["proposition_reviews"][0]["status"] = "CONFLICTED"
        result["review_status"] = "CONFLICTED"
        result["manual_review_reason"] = "测试冲突"
        ReviewService._set_computed_overall_status(result)
        ReviewService._complete_deterministic_fields(result)
        ReviewService._group_structured_evidence(result)
        validate_result(result, self.context, 1)
        result["fact_reviews"][0]["evidence"][0]["quotes"] = self.values[:1]
        with self.assertRaises(ReviewValidationError):
            validate_result(result, self.context, 1)

    def test_fact_ids_replace_labels_without_changing_decisions(self) -> None:
        validate_context(self.context)
        self.context["propositions"][0]["evidence"][0]["fact_id"] = "P03-F09"
        self.model_result["fact_reviews"][0]["fact_id"] = "P03-F09"
        original_context = copy.deepcopy(self.context)
        prompt = build_user_prompt(self.context, 1)
        payload = json.loads(prompt.split("以下为待复核输入：\n", 1)[1])
        self.assertEqual(payload["review_context"]["propositions"][0]["evidence"][0]["fact_id"], "P03-F09")
        self.assertEqual(self.context, original_context)
        result = ReviewService._expand_model_result(self.model_result, self.context, 1)
        ReviewService._set_computed_overall_status(result)
        ReviewService._complete_deterministic_fields(result)
        validate_result(result, self.context, 1)
        self.assertEqual(result["fact_reviews"][0]["fact_id"], "P03-F09")
        self.assertEqual(result["fact_reviews"][0]["target_fact"], "本次诊断")
        self.assertEqual(result["fact_reviews"][0]["necessity"], "REQUIRED")
        self.assertEqual(result["proposition_relation"]["expression"], "P03")
        self.assertEqual(result["final_conclusion"]["result"], "合规")
        self.assertNotIn("一级证据域", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("二级证据标签", json.dumps(result, ensure_ascii=False))
        result["fact_reviews"][0]["target_fact"] = "被修改的事实"
        with self.assertRaises(ReviewValidationError):
            validate_result(result, self.context, 1)

    def test_legacy_context_is_accepted_but_result_has_no_labels(self) -> None:
        fact = self.context["propositions"][0]["evidence"][0]
        fact.pop("fact_id")
        fact["一级证据域"] = "诊断"
        fact["二级证据标签"] = "标准诊断"
        original = copy.deepcopy(self.context)
        validate_context(self.context)
        result = ReviewService._expand_model_result(self.model_result, self.context, 1)
        ReviewService._set_computed_overall_status(result)
        ReviewService._complete_deterministic_fields(result)
        validate_result(result, self.context, 1)
        self.assertEqual(result["fact_reviews"][0]["fact_id"], "P03-F01")
        self.assertNotIn("一级证据域", result["fact_reviews"][0])
        self.assertNotIn("二级证据标签", result["fact_reviews"][0])
        self.assertEqual(self.context, original)

    def test_invalid_and_duplicate_fact_ids_are_rejected(self) -> None:
        for fact_id in (None, "", " ", " P03-F01", 1):
            with self.subTest(fact_id=fact_id):
                context = copy.deepcopy(self.context)
                context["propositions"][0]["evidence"][0]["fact_id"] = fact_id
                with self.assertRaises(ReviewValidationError):
                    validate_context(context)
        context = copy.deepcopy(self.context)
        context["propositions"][0]["evidence"].append(
            copy.deepcopy(context["propositions"][0]["evidence"][0])
        )
        with self.assertRaises(ReviewValidationError):
            validate_context(context)
        result = ReviewService._expand_model_result(self.model_result, self.context, 1)
        ReviewService._set_computed_overall_status(result)
        ReviewService._complete_deterministic_fields(result)
        result["fact_reviews"][0]["fact_id"] = "P03-F99"
        with self.assertRaises(ReviewValidationError):
            validate_result(result, self.context, 1)


if __name__ == "__main__":
    unittest.main()
