from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from query_planning_agent.mapping import load_medical_record_sources
from query_planning_agent.service import (
    PlanningValidationError,
    QueryPlanningService,
    collect_rule_facts,
)


class FakeClient:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.user_prompts: list[str] = []

    def generate(self, system_prompt: str, user_prompt: str) -> dict:
        if not self.responses:
            raise AssertionError("模型调用次数超出预期")
        self.user_prompts.append(user_prompt)
        return self.responses.pop(0)


class FakeFeedbackSource:
    """用子串匹配代替查询程序的匹配器，便于测试反馈逻辑。"""

    def __init__(self, records_by_tag: dict) -> None:
        self.records_by_tag = records_by_tag

    def collect(self, tags):
        return {tag: list(self.records_by_tag.get(tag, [])) for tag in tags}

    @staticmethod
    def match(record_content: object, terms: list[str]) -> list[str]:
        text = str(record_content)
        return [term for term in terms if term in text]


def progress_record(content: str, name: str = "张三主任医师查房记录") -> dict:
    return {
        "recordName": name,
        "recordType": "病程记录",
        "updateTime": "2026-01-01 08:00:00",
        "recordContent": content,
    }


class MedicalRecordMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.mapping_path = Path(self.temp_dir.name) / "mapping.xlsx"
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "拆分映射"
        worksheet.append(
            [
                "mapping_id",
                "来源级别",
                "主要来源",
                "次要来源",
                "对应的视图",
                "一级标签名",
                "二级标签名",
                "recordType",
            ]
        )
        worksheet.append(
            [
                "M01",
                "PRIMARY",
                "DS-M-IP-004 日常病程记录",
                "—",
                "病历信息",
                "病理",
                "病理结果",
                "病程记录",
            ]
        )
        worksheet.append(
            [
                "M02",
                "SECONDARY",
                "DS-S-001 诊断信息",
                "DS-M-IP-004 日常病程记录",
                "病历信息",
                "病理",
                "病理结果",
                "病程记录",
            ]
        )
        worksheet.append(
            [
                "M03",
                "PRIMARY",
                "DS-M-IP-002 入院记录",
                "—",
                "病历信息",
                "患者",
                "生育状态",
                "入院记录",
            ]
        )
        workbook.save(self.mapping_path)
        workbook.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_load_medical_record_sources_only_returns_progress_notes(self) -> None:
        sources = load_medical_record_sources(self.mapping_path)

        self.assertEqual(
            sources[("病理", "病理结果")],
            ["DS-M-IP-004 日常病程记录"],
        )
        self.assertNotIn(("患者", "生育状态"), sources)


class QueryPlanningMedicalRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rule = {
            "rule_id": "RULE-056",
            "result": {
                "source": {
                    "violation_item": "依西美坦片",
                    "violation_description": "患者开具此药属于超适应症用药",
                },
                "appeal_propositions": [
                    {
                        "proposition_id": "P03",
                        "statement": "患者为雌激素受体阳性的乳腺癌患者",
                        "required_tags": [
                            {
                                "一级证据域": "病理",
                                "二级证据标签": "病理结果",
                                "target_fact": "确认患者雌激素受体状态",
                                "necessity": "REQUIRED",
                            }
                        ],
                        "supplementary_tags": [],
                    }
                ],
            },
        }

    def test_only_mapped_progress_facts_receive_record_terms(self) -> None:
        facts = collect_rule_facts(
            self.rule,
            medical_record_sources={
                ("病理", "病理结果"): ["DS-M-IP-004 日常病程记录"]
            },
        )
        client = FakeClient(
            [
                {
                    "rule_id": "RULE-056",
                    "facts": [
                        {
                            "fact_id": "M01",
                            "record_search_terms": [" 雌激素 ", "ER", "ER"],
                        }
                    ],
                }
            ]
        )

        result = QueryPlanningService(client).plan(self.rule, facts)

        self.assertEqual(len(result["plans"]), 1)
        plan = result["plans"][0]
        self.assertEqual(plan["query_type"], "MEDICAL_RECORD_KEYWORD_FILTER")
        self.assertEqual(plan["record_search_terms"], ["雌激素", "ER"])
        self.assertEqual(
            plan["medical_record_sources"],
            ["DS-M-IP-004 日常病程记录"],
        )
        self.assertNotIn("entities", plan)
        self.assertEqual(client.responses, [])

    def test_unmapped_fact_does_not_call_model_or_create_plan(self) -> None:
        facts = collect_rule_facts(self.rule, medical_record_sources={})
        client = FakeClient([])

        result = QueryPlanningService(client).plan(self.rule, facts)

        self.assertEqual(result["plans"], [])

    def test_expense_and_progress_fields_share_one_fact_plan(self) -> None:
        overlap_rule = {
            "rule_id": "RULE-OVERLAP",
            "result": {
                "source": {
                    "violation_item": "他莫昔芬",
                    "violation_description": "核实既往治疗",
                },
                "appeal_propositions": [
                    {
                        "proposition_id": "P01",
                        "statement": "患者既往接受过他莫昔芬治疗",
                        "required_tags": [
                            {
                                "一级证据域": "用药",
                                "二级证据标签": "用药执行",
                                "target_fact": "确认既往是否使用他莫昔芬",
                                "necessity": "REQUIRED",
                            }
                        ],
                        "supplementary_tags": [],
                    }
                ],
            },
        }
        facts = collect_rule_facts(
            overlap_rule,
            expense_tags={("用药", "用药执行")},
            medical_record_sources={
                ("用药", "用药执行"): ["DS-M-IP-004 日常病程记录"]
            },
        )
        client = FakeClient(
            [
                {
                    "rule_id": "RULE-OVERLAP",
                    "facts": [
                        {
                            "fact_id": "E01",
                            "keyword_groups": [
                                {
                                    "source_phrase": "他莫昔芬",
                                    "required_terms": ["他莫昔芬"],
                                    "alternative_terms": [],
                                }
                            ],
                        }
                    ],
                },
                {
                    "rule_id": "RULE-OVERLAP",
                    "facts": [
                        {
                            "fact_id": "M01",
                            "record_search_terms": ["他莫昔芬"],
                        }
                    ],
                },
            ]
        )

        result = QueryPlanningService(client).plan(overlap_rule, facts)

        self.assertEqual(len(result["plans"]), 1)
        plan = result["plans"][0]
        self.assertEqual(plan["query_type"], "MULTI_SOURCE")
        self.assertEqual(plan["entities"][0]["source_exact"], "他莫昔芬")
        self.assertEqual(plan["record_search_terms"], ["他莫昔芬"])
        self.assertEqual(client.responses, [])

    def test_medical_terms_limit_is_checked_after_normalized_deduplication(self) -> None:
        planning_facts = [
            {
                "fact_id": "M01",
                "proposition_id": "P03",
                "一级证据域": "病理",
                "二级证据标签": "病理结果",
                "target_fact": "确认患者雌激素受体状态",
            }
        ]

        result = QueryPlanningService._validate_medical_record_result(
            {
                "rule_id": "RULE-056",
                "facts": [
                    {
                        "fact_id": "M01",
                        "record_search_terms": [
                            "ER",
                            " er ",
                            "ＥＲ",
                            "雌激素",
                            " 雌激素 ",
                            "ER",
                        ],
                    }
                ],
            },
            "RULE-056",
            planning_facts,
        )

        self.assertEqual(result["M01"], ["ER", "雌激素"])

    def test_medical_terms_reject_generic_words(self) -> None:
        planning_facts = [
            {
                "fact_id": "M01",
                "proposition_id": "P03",
                "一级证据域": "病理",
                "二级证据标签": "病理结果",
                "target_fact": "确认患者雌激素受体状态",
            }
        ]

        with self.assertRaisesRegex(PlanningValidationError, "无区分度词语"):
            QueryPlanningService._validate_medical_record_result(
                {
                    "rule_id": "RULE-056",
                    "facts": [
                        {
                            "fact_id": "M01",
                            "record_search_terms": ["患者"],
                        }
                    ],
                },
                "RULE-056",
                planning_facts,
            )

    def test_medical_terms_reject_single_characters_and_numbers(self) -> None:
        planning_facts = [
            {
                "fact_id": "M01",
                "proposition_id": "P03",
                "一级证据域": "病理",
                "二级证据标签": "病理结果",
                "target_fact": "确认患者雌激素受体状态",
            }
        ]

        for term in ("片", "R", "28"):
            with self.subTest(term=term), self.assertRaisesRegex(
                PlanningValidationError,
                "单个字符或纯数字",
            ):
                QueryPlanningService._validate_medical_record_result(
                    {
                        "rule_id": "RULE-056",
                        "facts": [
                            {
                                "fact_id": "M01",
                                "record_search_terms": ["雌激素", term],
                            }
                        ],
                    },
                    "RULE-056",
                    planning_facts,
                )

    def test_medical_terms_reject_evidence_tag_labels(self) -> None:
        planning_facts = [
            {
                "fact_id": "M01",
                "proposition_id": "P03",
                "一级证据域": "病理",
                "二级证据标签": "病理结果",
                "target_fact": "确认患者雌激素受体状态",
            }
        ]

        with self.assertRaisesRegex(PlanningValidationError, "证据标签名称"):
            QueryPlanningService._validate_medical_record_result(
                {
                    "rule_id": "RULE-056",
                    "facts": [
                        {
                            "fact_id": "M01",
                            "record_search_terms": ["ER", " 病理结果 "],
                        }
                    ],
                },
                "RULE-056",
                planning_facts,
            )

    def test_rule_009_label_term_is_repaired(self) -> None:
        rule = {
            "rule_id": "RULE-009",
            "result": {
                "source": {
                    "violation_item": "大关节松动训练",
                    "violation_description": "大关节松动训练计费天数超过住院天数",
                },
                "appeal_propositions": [
                    {
                        "proposition_id": "P01",
                        "statement": "计费天数与实际执行一致",
                        "required_tags": [],
                        "supplementary_tags": [
                            {
                                "一级证据域": "治疗",
                                "二级证据标签": "治疗过程",
                                "target_fact": "核对每日实际执行的治疗次数和部位",
                                "necessity": "SUPPLEMENTARY",
                            }
                        ],
                    },
                    {
                        "proposition_id": "P02",
                        "statement": "不同部位制定了独立治疗计划",
                        "required_tags": [
                            {
                                "一级证据域": "治疗",
                                "二级证据标签": "治疗方案",
                                "target_fact": "查看康复治疗方案",
                                "necessity": "REQUIRED",
                            }
                        ],
                        "supplementary_tags": [],
                    },
                ],
            },
        }
        facts = collect_rule_facts(
            rule,
            medical_record_sources={
                ("治疗", "治疗过程"): ["DS-M-IP-004日常病程记录"],
                ("治疗", "治疗方案"): ["DS-M-IP-007会诊记录"],
            },
        )
        client = FakeClient(
            [
                {
                    "rule_id": "RULE-009",
                    "facts": [
                        {
                            "fact_id": "M01",
                            "record_search_terms": [
                                "大关节松动",
                                "关节松动",
                                "松动训练",
                                "关节活动度",
                                "ROM",
                            ],
                        },
                        {
                            "fact_id": "M02",
                            "record_search_terms": [
                                "大关节松动",
                                "关节松动",
                                "松动训练",
                                "康复计划",
                                "治疗方案",
                            ],
                        },
                    ],
                },
                {
                    "rule_id": "RULE-009",
                    "facts": [
                        {
                            "fact_id": "M01",
                            "record_search_terms": ["关节松动", "康复"],
                        },
                        {
                            "fact_id": "M02",
                            "record_search_terms": ["关节松动", "康复"],
                        },
                    ],
                },
            ]
        )

        with contextlib.redirect_stdout(io.StringIO()):
            result = QueryPlanningService(client).plan(rule, facts)

        self.assertEqual(client.responses, [])
        self.assertIn("证据标签名称: 治疗方案", client.user_prompts[1])
        terms_by_tag = {
            plan["supports_facts"][0]["二级证据标签"]: plan["record_search_terms"]
            for plan in result["plans"]
        }
        self.assertEqual(terms_by_tag["治疗方案"], ["关节松动", "康复"])


class RecordFeedbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rule = {
            "rule_id": "RULE-009",
            "result": {
                "source": {
                    "violation_item": "大关节松动训练",
                    "violation_description": "计费天数超过实际执行天数",
                },
                "appeal_propositions": [
                    {
                        "proposition_id": "P02",
                        "statement": "康复治疗方案与计费一致",
                        "required_tags": [
                            {
                                "一级证据域": "治疗",
                                "二级证据标签": "治疗方案",
                                "target_fact": "查看康复治疗方案",
                                "necessity": "REQUIRED",
                            }
                        ],
                        "supplementary_tags": [],
                    }
                ],
            },
        }
        self.tag = ("治疗", "治疗方案")
        self.records = [
            progress_record("请康复科会诊，进行床边康复治疗。"),
            progress_record("今日继续床边康复训练。", "阶段小结"),
        ]
        self.facts = collect_rule_facts(
            self.rule,
            medical_record_sources={self.tag: ["DS-M-IP-005上级医师查房记录"]},
        )

    def build_service(self, client, records=None):
        return QueryPlanningService(
            client,
            record_feedback=FakeFeedbackSource(
                {self.tag: self.records if records is None else records}
            ),
        )

    def test_zero_hit_terms_are_supplemented_from_record_text(self) -> None:
        client = FakeClient(
            [
                {
                    "rule_id": "RULE-009",
                    "facts": [
                        {
                            "fact_id": "M01",
                            "record_search_terms": ["关节松动", "松动训练"],
                        }
                    ],
                },
                {"added_record_search_terms": ["康复", "床边康复", "无关词"]},
            ]
        )

        with contextlib.redirect_stdout(io.StringIO()):
            result = self.build_service(client).plan(self.rule, self.facts)

        plan = result["plans"][0]
        self.assertEqual(
            plan["record_search_terms"],
            ["康复", "床边康复", "关节松动", "松动训练"],
        )
        note = plan["record_search_retry"]
        self.assertTrue(note["triggered"])
        self.assertEqual(note["reason"], "NO_KEYWORD_MATCH")
        self.assertEqual(note["original_terms"], ["关节松动", "松动训练"])
        self.assertEqual(note["added_terms"], ["康复", "床边康复"])
        self.assertEqual(note["added_term_hits"], {"康复": 2, "床边康复": 2})
        self.assertEqual(
            note["rejected_terms"],
            [{"term": "无关词", "reason": "候选病程中没有命中"}],
        )
        self.assertEqual(note["candidate_documents"], 2)
        self.assertIn("请康复科会诊", client.user_prompts[1])
        self.assertEqual(client.responses, [])

    def test_terms_with_hits_do_not_trigger_feedback(self) -> None:
        client = FakeClient(
            [
                {
                    "rule_id": "RULE-009",
                    "facts": [
                        {
                            "fact_id": "M01",
                            "record_search_terms": ["康复"],
                        }
                    ],
                }
            ]
        )

        with contextlib.redirect_stdout(io.StringIO()):
            result = self.build_service(client).plan(self.rule, self.facts)

        plan = result["plans"][0]
        self.assertEqual(plan["record_search_terms"], ["康复"])
        self.assertNotIn("record_search_retry", plan)
        self.assertEqual(client.responses, [])

    def test_retry_without_findings_keeps_original_terms(self) -> None:
        client = FakeClient(
            [
                {
                    "rule_id": "RULE-009",
                    "facts": [
                        {
                            "fact_id": "M01",
                            "record_search_terms": ["关节松动"],
                        }
                    ],
                },
                {"added_record_search_terms": []},
            ]
        )

        with contextlib.redirect_stdout(io.StringIO()):
            result = self.build_service(client).plan(self.rule, self.facts)

        plan = result["plans"][0]
        self.assertEqual(plan["record_search_terms"], ["关节松动"])
        note = plan["record_search_retry"]
        self.assertEqual(note["added_terms"], [])
        self.assertIn("没有找到", note["note"])

    def test_invalid_added_terms_are_rejected(self) -> None:
        client = FakeClient(
            [
                {
                    "rule_id": "RULE-009",
                    "facts": [
                        {
                            "fact_id": "M01",
                            "record_search_terms": ["关节松动"],
                        }
                    ],
                },
                {"added_record_search_terms": ["康复", "治疗方案", "片"]},
            ]
        )

        with contextlib.redirect_stdout(io.StringIO()):
            result = self.build_service(client).plan(self.rule, self.facts)

        note = result["plans"][0]["record_search_retry"]
        self.assertEqual(note["added_terms"], ["康复"])
        self.assertEqual(
            note["rejected_terms"],
            [
                {"term": "治疗方案", "reason": "不能直接使用证据标签名称"},
                {"term": "片", "reason": "不能是单个字符或纯数字"},
            ],
        )

    def test_added_terms_are_capped_and_kept_within_five(self) -> None:
        client = FakeClient(
            [
                {
                    "rule_id": "RULE-009",
                    "facts": [
                        {
                            "fact_id": "M01",
                            "record_search_terms": [
                                "甲词",
                                "乙词",
                                "丙词",
                                "丁词",
                                "戊词",
                            ],
                        }
                    ],
                },
                {
                    "added_record_search_terms": [
                        "康复",
                        "床边康复",
                        "康复科",
                        "继续训练",
                    ]
                },
            ]
        )

        with contextlib.redirect_stdout(io.StringIO()):
            result = self.build_service(client).plan(self.rule, self.facts)

        plan = result["plans"][0]
        self.assertEqual(
            plan["record_search_terms"],
            ["康复", "床边康复", "康复科", "甲词", "乙词"],
        )
        note = plan["record_search_retry"]
        self.assertEqual(note["added_terms"], ["康复", "床边康复", "康复科"])
        self.assertEqual(note["replaced_terms"], ["丙词", "丁词", "戊词"])

    def test_low_coverage_terms_trigger_feedback_and_keep_hitting_terms(self) -> None:
        client = FakeClient(
            [
                {
                    "rule_id": "RULE-009",
                    "facts": [
                        {
                            "fact_id": "M01",
                            "record_search_terms": [
                                "康复",
                                "甲词",
                                "乙词",
                                "丙词",
                                "丁词",
                            ],
                        }
                    ],
                },
                {"added_record_search_terms": ["康复科", "床边康复"]},
            ]
        )
        single_hit = [progress_record("请康复科会诊。")]

        with contextlib.redirect_stdout(io.StringIO()):
            result = self.build_service(client, records=single_hit).plan(
                self.rule, self.facts
            )

        plan = result["plans"][0]
        note = plan["record_search_retry"]
        self.assertEqual(note["reason"], "LOW_KEYWORD_COVERAGE")
        self.assertEqual(note["matched_documents"], 1)
        self.assertEqual(note["original_term_hits"], {"康复": 1})
        self.assertEqual(note["added_terms"], ["康复科"])
        self.assertEqual(
            note["rejected_terms"],
            [{"term": "床边康复", "reason": "候选病程中没有命中"}],
        )
        self.assertEqual(
            plan["record_search_terms"],
            ["康复科", "康复", "甲词", "乙词", "丙词"],
        )
        self.assertEqual(note["replaced_terms"], ["丁词"])

    def test_no_candidates_skips_feedback(self) -> None:
        client = FakeClient(
            [
                {
                    "rule_id": "RULE-009",
                    "facts": [
                        {
                            "fact_id": "M01",
                            "record_search_terms": ["关节松动"],
                        }
                    ],
                }
            ]
        )

        with contextlib.redirect_stdout(io.StringIO()):
            result = self.build_service(client, records=[]).plan(
                self.rule, self.facts
            )

        self.assertNotIn("record_search_retry", result["plans"][0])
        self.assertEqual(client.responses, [])


if __name__ == "__main__":
    unittest.main()