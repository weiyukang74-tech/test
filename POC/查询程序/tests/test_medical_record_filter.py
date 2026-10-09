from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from evidence_retriever.medical_record_filter import (
    filter_rule_medical_records,
    match_record_content,
    medical_record_identity,
)


POC_ROOT = Path(__file__).resolve().parents[2]
RULE_009_UNFILTERED_RESULT = (
    POC_ROOT / "对比验证" / "病程关键词筛选" / "A_查询结果_RULE-009.json"
)
P03_KEY = ("P03", "病理", "病理结果", "确认患者雌激素受体状态")
P04_KEY = ("P04", "用药", "既往治疗", "确认患者既往使用他莫昔芬")


def medical_plan(
    key: tuple[str, str, str, str],
    terms: list[str],
    *,
    necessity: str = "REQUIRED",
    sources: list[str] | None = None,
) -> dict:
    proposition_id, primary_domain, secondary_tag, target_fact = key
    return {
        "query_id": f"QP-{proposition_id}-01",
        "supports_facts": [
            {
                "proposition_id": proposition_id,
                "一级证据域": primary_domain,
                "二级证据标签": secondary_tag,
                "target_fact": target_fact,
                "necessity": necessity,
            }
        ],
        "query_type": "MEDICAL_RECORD_KEYWORD_FILTER",
        "record_search_terms": terms,
        "medical_record_sources": sources or ["DS-M-IP-004 日常病程记录"],
    }


def progress_record(name: str, content: object) -> dict:
    return {
        "mdtrtId": "CASE-1",
        "recordName": name,
        "recordType": "病程记录",
        "updateTime": "2026-01-01 08:00:00",
        "recordContent": content,
    }


class MedicalRecordTermMatchingTests(unittest.TestCase):
    def test_er_matches_clinical_forms_but_not_her2(self) -> None:
        self.assertEqual(match_record_content("ER+，HER2阴性", ["ER"]), ["ER"])
        self.assertEqual(match_record_content("仅记录HER2阳性", ["ER"]), [])
        self.assertEqual(match_record_content("ER（90%）", ["ER"]), ["ER"])

    def test_matching_is_case_insensitive_and_reads_nested_values(self) -> None:
        content = {
            "病理": {
                "免疫组化": ["er 阳性", "孕激素受体阳性"],
            }
        }

        self.assertEqual(
            match_record_content(content, ["ER", "雌激素"]),
            ["ER"],
        )

    def test_regex_characters_are_matched_as_literal_text(self) -> None:
        self.assertEqual(match_record_content("检测项目C++结果", ["C++"]), ["C++"])


class MedicalRecordRuleFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.er_record = progress_record(
            "首次病程记录",
            {"病理结果": "ER+，HER2阴性"},
        )
        self.tamoxifen_record = progress_record(
            "日常病程记录",
            "患者既往接受他莫昔芬辅助治疗。",
        )
        self.unrelated_record = progress_record(
            "日常病程记录-2",
            "患者一般情况稳定。",
        )
        self.admission_record = {
            "mdtrtId": "CASE-1",
            "recordName": "入院记录",
            "recordType": "入院记录",
            "recordContent": "入院诊断为乳腺癌。",
        }
        duplicate_er_record = copy.deepcopy(self.er_record)
        duplicate_er_record["unused_transport_field"] = "ignored"
        self.rule_result = {
            "rule_id": "RULE-056",
            "propositions": [
                {
                    "proposition_id": "P03",
                    "evidence": [
                        {
                            "一级证据域": "病理",
                            "二级证据标签": "病理结果",
                            "target_fact": "确认患者雌激素受体状态",
                            "query_result": {
                                "status": "RETRIEVED",
                                "source": "DS-M-IP-004 日常病程记录",
                                "data": [
                                    self.er_record,
                                    duplicate_er_record,
                                    self.tamoxifen_record,
                                    self.unrelated_record,
                                    self.admission_record,
                                ],
                            },
                        }
                    ],
                },
                {
                    "proposition_id": "P04",
                    "evidence": [
                        {
                            "一级证据域": "用药",
                            "二级证据标签": "既往治疗",
                            "target_fact": "确认患者既往使用他莫昔芬",
                            "query_result": {
                                "status": "RETRIEVED",
                                "source": "DS-M-IP-004 日常病程记录",
                                "data": [
                                    copy.deepcopy(self.er_record),
                                    copy.deepcopy(self.tamoxifen_record),
                                    copy.deepcopy(self.unrelated_record),
                                ],
                            },
                        }
                    ],
                },
            ],
        }

    def test_filter_uses_rule_level_or_and_deduplicates_candidates(self) -> None:
        plan_index = {
            P03_KEY: medical_plan(P03_KEY, ["雌激素", "ER"]),
            P04_KEY: medical_plan(P04_KEY, ["他莫昔芬"]),
        }

        stats = filter_rule_medical_records(
            self.rule_result,
            plan_index,
            [P03_KEY, P04_KEY],
            [P03_KEY, P04_KEY],
        )

        self.assertEqual(
            stats,
            {
                "enabled": True,
                "filter_applied": True,
                "candidate_document_count": 3,
                "selected_document_count": 2,
                "filter_fallback": False,
                "fallback_reason": None,
                "planned_evidence_count": 2,
                "matched_evidence_count": 2,
                "required_planned_evidence_count": 2,
                "required_matched_evidence_count": 2,
            },
        )
        first_data = self.rule_result["propositions"][0]["evidence"][0][
            "query_result"
        ]["data"]
        progress = [record for record in first_data if record["recordType"] == "病程记录"]
        self.assertEqual(len(progress), 2)
        self.assertEqual(first_data[-1], self.admission_record)
        self.assertEqual(progress[0]["recordContent"], {"病理结果": "ER+，HER2阴性"})
        self.assertEqual(
            progress[0]["_medical_record_filter_match"]["matched_terms"],
            ["ER"],
        )
        self.assertEqual(
            progress[1]["_medical_record_filter_match"]["matched_propositions"],
            ["P04"],
        )

    def test_zero_matches_falls_back_to_all_progress_records(self) -> None:
        original = copy.deepcopy(self.rule_result)
        plan_index = {
            P03_KEY: medical_plan(P03_KEY, ["孕激素受体"]),
            P04_KEY: medical_plan(P04_KEY, ["芳香化酶"]),
        }

        stats = filter_rule_medical_records(
            self.rule_result,
            plan_index,
            [P03_KEY, P04_KEY],
            [P03_KEY, P04_KEY],
        )

        self.assertTrue(stats["filter_fallback"])
        self.assertEqual(stats["fallback_reason"], "NO_KEYWORD_MATCH")
        self.assertTrue(stats["enabled"])
        self.assertFalse(stats["filter_applied"])
        self.assertEqual(stats["selected_document_count"], 3)
        self.assertEqual(self.rule_result, original)

    def test_required_plan_without_match_falls_back_to_all_records(self) -> None:
        original = copy.deepcopy(self.rule_result)
        plan_index = {
            P03_KEY: medical_plan(P03_KEY, ["ER"]),
            P04_KEY: medical_plan(P04_KEY, ["芳香化酶"]),
        }

        stats = filter_rule_medical_records(
            self.rule_result,
            plan_index,
            [P03_KEY, P04_KEY],
            [P03_KEY, P04_KEY],
        )

        self.assertTrue(stats["filter_fallback"])
        self.assertEqual(stats["fallback_reason"], "INCOMPLETE_FILTER_COVERAGE")
        self.assertTrue(stats["enabled"])
        self.assertFalse(stats["filter_applied"])
        self.assertEqual(stats["matched_evidence_count"], 1)
        self.assertEqual(stats["required_planned_evidence_count"], 2)
        self.assertEqual(stats["required_matched_evidence_count"], 1)
        self.assertEqual(self.rule_result, original)

    def test_unmatched_supplementary_plan_keeps_its_own_progress_records(self) -> None:
        plan_index = {
            P03_KEY: medical_plan(P03_KEY, ["ER"]),
            P04_KEY: medical_plan(
                P04_KEY,
                ["芳香化酶"],
                necessity="SUPPLEMENTARY",
            ),
        }

        stats = filter_rule_medical_records(
            self.rule_result,
            plan_index,
            [P03_KEY, P04_KEY],
            [P03_KEY],
        )

        self.assertFalse(stats["filter_fallback"])
        self.assertTrue(stats["filter_applied"])
        self.assertEqual(stats["selected_document_count"], 3)
        self.assertEqual(stats["required_planned_evidence_count"], 1)
        self.assertEqual(stats["required_matched_evidence_count"], 1)
        self.assertEqual(
            stats["supplementary_evidence_fallbacks"],
            [
                {
                    "proposition_id": "P04",
                    "一级证据域": "用药",
                    "二级证据标签": "既往治疗",
                    "fallback_reason": "NO_KEYWORD_MATCH",
                    "retained_document_count": 3,
                }
            ],
        )
        required_data = self.rule_result["propositions"][0]["evidence"][0][
            "query_result"
        ]["data"]
        self.assertEqual(
            [record["recordName"] for record in required_data],
            ["首次病程记录", "入院记录"],
        )
        supplementary_data = self.rule_result["propositions"][1]["evidence"][0][
            "query_result"
        ]["data"]
        self.assertEqual(
            [record["recordContent"] for record in supplementary_data],
            [
                {"病理结果": "ER+，HER2阴性"},
                "患者既往接受他莫昔芬辅助治疗。",
                "患者一般情况稳定。",
            ],
        )
        self.assertEqual(
            supplementary_data[0]["_medical_record_filter_match"]["matched_terms"],
            ["ER"],
        )
        self.assertNotIn("_medical_record_filter_match", supplementary_data[1])

    def test_evidence_tag_label_term_cannot_satisfy_required_coverage(self) -> None:
        key = ("P02", "治疗", "治疗方案", "查看康复治疗方案")
        rehabilitation_record = progress_record(
            "会诊记录",
            "康复科会诊：继续床边康复训练。",
        )
        rule_result = {
            "propositions": [
                {
                    "proposition_id": "P02",
                    "evidence": [
                        {
                            "一级证据域": "治疗",
                            "二级证据标签": "治疗方案",
                            "target_fact": "查看康复治疗方案",
                            "query_result": {
                                "source": "DS-M-IP-007 会诊记录",
                                "data": [
                                    progress_record(
                                        "会诊记录",
                                        "为调整治疗方案，特请神经外科会诊。",
                                    ),
                                    rehabilitation_record,
                                ],
                            },
                        }
                    ],
                }
            ]
        }
        original = copy.deepcopy(rule_result)
        plan_index = {
            key: medical_plan(
                key,
                ["康复计划", "治疗方案"],
                sources=["DS-M-IP-007 会诊记录"],
            )
        }

        stats = filter_rule_medical_records(rule_result, plan_index, [key], [key])

        self.assertTrue(stats["filter_fallback"])
        self.assertEqual(stats["fallback_reason"], "NO_KEYWORD_MATCH")
        self.assertEqual(
            stats["ignored_search_terms"],
            [
                {
                    "proposition_id": "P02",
                    "一级证据域": "治疗",
                    "二级证据标签": "治疗方案",
                    "terms": ["治疗方案"],
                }
            ],
        )
        self.assertEqual(rule_result, original)

    def test_plan_with_only_label_or_generic_terms_is_incomplete(self) -> None:
        original = copy.deepcopy(self.rule_result)
        plan_index = {
            P03_KEY: medical_plan(P03_KEY, ["ER"]),
            P04_KEY: medical_plan(P04_KEY, ["既往治疗", "病理结果", "患者"]),
        }

        stats = filter_rule_medical_records(
            self.rule_result,
            plan_index,
            [P03_KEY, P04_KEY],
            [P03_KEY, P04_KEY],
        )

        self.assertFalse(stats["enabled"])
        self.assertEqual(stats["fallback_reason"], "INCOMPLETE_FILTER_PLAN")
        self.assertEqual(stats["planned_evidence_count"], 1)
        self.assertEqual(
            stats["ignored_search_terms"][0]["terms"],
            ["既往治疗", "病理结果", "患者"],
        )
        self.assertEqual(self.rule_result, original)

    def test_missing_plan_falls_back_to_all_progress_records(self) -> None:
        original = copy.deepcopy(self.rule_result)
        plan_index = {P03_KEY: medical_plan(P03_KEY, ["ER"])}

        stats = filter_rule_medical_records(
            self.rule_result,
            plan_index,
            [P03_KEY, P04_KEY],
            [P03_KEY, P04_KEY],
        )

        self.assertTrue(stats["filter_fallback"])
        self.assertEqual(stats["fallback_reason"], "INCOMPLETE_FILTER_PLAN")
        self.assertFalse(stats["enabled"])
        self.assertFalse(stats["filter_applied"])
        self.assertEqual(stats["planned_evidence_count"], 1)
        self.assertEqual(self.rule_result, original)

    def test_plan_terms_match_progress_records_from_other_evidence(self) -> None:
        activity_key = ("P01", "病情", "活动进展", "确认乙肝病毒活动状态")
        treatment_key = ("P01", "治疗", "治疗过程", "确认抗病毒治疗过程")
        rule_result = {
            "propositions": [
                {
                    "proposition_id": "P01",
                    "evidence": [
                        {
                            "一级证据域": "病情",
                            "二级证据标签": "活动进展",
                            "target_fact": "确认乙肝病毒活动状态",
                            "query_result": {
                                "source": "DS-M-IP-003 首次病程记录",
                                "data": [],
                            },
                        },
                        {
                            "一级证据域": "治疗",
                            "二级证据标签": "治疗过程",
                            "target_fact": "确认抗病毒治疗过程",
                            "query_result": {
                                "source": "DS-M-IP-004 日常病程记录",
                                "data": [
                                    progress_record(
                                        "日常病程记录",
                                        "复查HBV-DNA阳性，乙肝病毒复制活跃。",
                                    ),
                                    progress_record(
                                        "日常病程记录-2",
                                        "继续恩替卡韦抗病毒治疗。",
                                    ),
                                    progress_record(
                                        "日常病程记录-3",
                                        "患者一般情况稳定。",
                                    ),
                                ],
                            },
                        },
                    ],
                }
            ]
        }
        plan_index = {
            activity_key: medical_plan(
                activity_key,
                ["乙肝", "HBV"],
                sources=["DS-M-IP-003 首次病程记录"],
            ),
            treatment_key: medical_plan(treatment_key, ["恩替卡韦"]),
        }

        stats = filter_rule_medical_records(
            rule_result,
            plan_index,
            [activity_key, treatment_key],
            [activity_key, treatment_key],
        )

        self.assertTrue(stats["filter_applied"])
        self.assertEqual(stats["matched_evidence_count"], 2)
        self.assertEqual(stats["selected_document_count"], 2)
        treatment_data = rule_result["propositions"][0]["evidence"][1][
            "query_result"
        ]["data"]
        self.assertEqual(
            [record["recordName"] for record in treatment_data],
            ["日常病程记录", "日常病程记录-2"],
        )
        self.assertEqual(
            treatment_data[0]["_medical_record_filter_match"][
                "matched_evidence_tags"
            ][0]["二级证据标签"],
            "活动进展",
        )

    def test_current_rule_requiredness_overrides_stale_plan_necessity(self) -> None:
        original = copy.deepcopy(self.rule_result)
        plan_index = {
            P03_KEY: medical_plan(P03_KEY, ["ER"]),
            P04_KEY: medical_plan(
                P04_KEY,
                ["芳香化酶"],
                necessity="SUPPLEMENTARY",
            ),
        }

        stats = filter_rule_medical_records(
            self.rule_result,
            plan_index,
            [P03_KEY, P04_KEY],
            [P03_KEY, P04_KEY],
        )

        self.assertTrue(stats["filter_fallback"])
        self.assertEqual(stats["fallback_reason"], "INCOMPLETE_FILTER_COVERAGE")
        self.assertEqual(stats["required_planned_evidence_count"], 2)
        self.assertEqual(stats["required_matched_evidence_count"], 1)
        self.assertEqual(self.rule_result, original)

    def test_plan_without_medical_record_sources_is_incomplete(self) -> None:
        original = copy.deepcopy(self.rule_result)
        plan = medical_plan(P03_KEY, ["ER"])
        plan.pop("medical_record_sources")

        stats = filter_rule_medical_records(
            self.rule_result,
            {P03_KEY: plan},
            [P03_KEY],
            [P03_KEY],
        )

        self.assertFalse(stats["enabled"])
        self.assertFalse(stats["filter_applied"])
        self.assertTrue(stats["filter_fallback"])
        self.assertEqual(stats["fallback_reason"], "INCOMPLETE_FILTER_PLAN")
        self.assertEqual(self.rule_result, original)


class Rule009RegressionTests(unittest.TestCase):
    """RULE-009 旧计划靠标签名“治疗方案”命中无关会诊，筛掉了全部康复病程。"""

    def setUp(self) -> None:
        self.rule_result = json.loads(
            RULE_009_UNFILTERED_RESULT.read_text(encoding="utf-8")
        )
        self.evidence = {
            (proposition["proposition_id"], evidence["二级证据标签"]): evidence
            for proposition in self.rule_result["propositions"]
            for evidence in proposition["evidence"]
        }
        self.process_key = (
            "P01",
            "治疗",
            "治疗过程",
            self.evidence[("P01", "治疗过程")]["target_fact"],
        )
        self.plan_key = (
            "P02",
            "治疗",
            "治疗方案",
            self.evidence[("P02", "治疗方案")]["target_fact"],
        )
        self.plan_index = {
            self.process_key: medical_plan(
                self.process_key,
                ["大关节松动", "关节松动", "松动训练", "关节活动度", "ROM"],
                necessity="SUPPLEMENTARY",
                sources=["DS-M-IP-004日常病程记录", "DS-M-IP-006阶段小结"],
            ),
            self.plan_key: medical_plan(
                self.plan_key,
                ["大关节松动", "关节松动", "松动训练", "康复计划", "治疗方案"],
                sources=[
                    "DS-M-IP-003首次病程记录",
                    "DS-M-IP-007会诊记录",
                    "DS-M-IP-009术前小结",
                ],
            ),
        }
        self.rehabilitation_identities = self._progress_identities(containing="康复")

    def _progress_identities(
        self,
        evidence_items: list[dict] | None = None,
        containing: str | None = None,
    ) -> set[str]:
        if evidence_items is None:
            evidence_items = list(self.evidence.values())
        return {
            medical_record_identity(record)
            for evidence in evidence_items
            for record in evidence["query_result"]["data"]
            if record.get("recordType") == "病程记录"
            and (
                containing is None
                or match_record_content(record["recordContent"], [containing])
            )
        }

    def _filter(self) -> dict:
        return filter_rule_medical_records(
            self.rule_result,
            self.plan_index,
            [self.process_key, self.plan_key],
            [self.plan_key],
        )

    def test_recorded_plan_no_longer_drops_rehabilitation_records(self) -> None:
        stats = self._filter()

        self.assertEqual(len(self.rehabilitation_identities), 7)
        self.assertFalse(stats["filter_applied"])
        self.assertEqual(stats["fallback_reason"], "NO_KEYWORD_MATCH")
        self.assertEqual(stats["selected_document_count"], 67)
        self.assertEqual(
            stats["ignored_search_terms"],
            [
                {
                    "proposition_id": "P02",
                    "一级证据域": "治疗",
                    "二级证据标签": "治疗方案",
                    "terms": ["治疗方案"],
                }
            ],
        )
        self.assertLessEqual(
            self.rehabilitation_identities,
            self._progress_identities(),
        )

    def test_unmatched_supplementary_lane_keeps_all_progress_records(self) -> None:
        process_evidence = [self.evidence[("P01", "治疗过程")]]
        process_identities = self._progress_identities(process_evidence)
        self.plan_index[self.plan_key]["record_search_terms"] = ["康复"]

        stats = self._filter()

        self.assertTrue(stats["filter_applied"])
        self.assertEqual(stats["candidate_document_count"], 67)
        self.assertEqual(stats["selected_document_count"], 28)
        self.assertEqual(
            stats["supplementary_evidence_fallbacks"],
            [
                {
                    "proposition_id": "P01",
                    "一级证据域": "治疗",
                    "二级证据标签": "治疗过程",
                    "fallback_reason": "NO_KEYWORD_MATCH",
                    "retained_document_count": 23,
                }
            ],
        )
        self.assertEqual(
            self._progress_identities(process_evidence),
            process_identities,
        )
        self.assertLessEqual(
            self.rehabilitation_identities,
            self._progress_identities(),
        )


if __name__ == "__main__":
    unittest.main()
