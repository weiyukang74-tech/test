from __future__ import annotations

import unittest

from evidence_retriever.planning import (
    index_query_plans,
    resolve_medical_record_plan,
    resolve_query_plan,
    safe_index_query_plans,
)


class QueryPlanResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = {
            "query_id": "QP-P01-01",
            "supports_facts": [
                {
                    "proposition_id": "P01",
                    "一级证据域": "手术操作",
                    "二级证据标签": "操作记录",
                    "target_fact": "确认胸腹水穿刺操作",
                }
            ],
            "entities": [
                {
                    "entity_id": "E01",
                    "source_exact": "胸腹水穿刺",
                    "core_terms": ["胸腹水"],
                    "qualifiers": ["穿刺"],
                    "normalized_terms": ["胸腔穿刺", "腹腔穿刺"],
                    "alias_terms": [],
                }
            ],
            "search_fields": ["hilist_name", "hosplist_name"],
            "fallback_search_fields": ["set_name"],
            "fallback_strategy": ["ALL_CHRG_TYPE_EXPANDED"],
        }
        self.index = index_query_plans({"plans": [self.plan]})

    def test_exact_fact_uses_own_plan(self) -> None:
        resolved = resolve_query_plan(
            self.index,
            ("P01", "手术操作", "操作记录", "确认胸腹水穿刺操作"),
        )
        self.assertIs(resolved, self.plan)

    def test_missing_fact_inherits_same_proposition_entities(self) -> None:
        resolved = resolve_query_plan(
            self.index,
            ("P01", "结算", "结算单据", "确认操作类费用构成"),
        )
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["plan_origin"], "INHERITED_SAME_PROPOSITION")
        self.assertEqual(resolved["entities"][0]["source_exact"], "胸腹水穿刺")
        self.assertEqual(resolved["inherited_from_query_ids"], ["QP-P01-01"])

    def test_missing_fact_does_not_inherit_other_proposition(self) -> None:
        resolved = resolve_query_plan(
            self.index,
            ("P02", "结算", "结算单据", "确认操作类费用构成"),
        )
        self.assertIsNone(resolved)

    def test_medical_record_plan_requires_exact_fact(self) -> None:
        medical_plan = {
            "query_id": "QP-P01-02",
            "supports_facts": [
                {
                    "proposition_id": "P01",
                    "一级证据域": "病理",
                    "二级证据标签": "病理结果",
                    "target_fact": "确认患者雌激素受体状态",
                }
            ],
            "record_search_terms": ["雌激素", "ER"],
            "medical_record_sources": ["DS-M-IP-004 日常病程记录"],
        }
        index = index_query_plans({"plans": [medical_plan]})

        self.assertIs(
            resolve_medical_record_plan(
                index,
                ("P01", "病理", "病理结果", "确认患者雌激素受体状态"),
            ),
            medical_plan,
        )
        self.assertIsNone(
            resolve_medical_record_plan(
                index,
                ("P01", "治疗", "既往治疗", "确认患者既往治疗"),
            )
        )

    def test_medical_record_plan_rejects_invalid_terms(self) -> None:
        invalid_plan = {
            "query_id": "QP-P01-02",
            "supports_facts": [
                {
                    "proposition_id": "P01",
                    "一级证据域": "病理",
                    "二级证据标签": "病理结果",
                    "target_fact": "确认患者雌激素受体状态",
                }
            ],
            "record_search_terms": [],
            "medical_record_sources": ["DS-M-IP-004 日常病程记录"],
        }
        index = index_query_plans({"plans": [invalid_plan]})

        self.assertIsNone(
            resolve_medical_record_plan(
                index,
                ("P01", "病理", "病理结果", "确认患者雌激素受体状态"),
            )
        )

    def test_medical_record_plan_rejects_missing_sources(self) -> None:
        plan = {
            "query_id": "QP-P01-02",
            "supports_facts": [
                {
                    "proposition_id": "P01",
                    "一级证据域": "病理",
                    "二级证据标签": "病理结果",
                    "target_fact": "确认患者雌激素受体状态",
                }
            ],
            "record_search_terms": ["ER"],
        }
        index = index_query_plans({"plans": [plan]})

        self.assertIsNone(
            resolve_medical_record_plan(
                index,
                ("P01", "病理", "病理结果", "确认患者雌激素受体状态"),
            )
        )

    def test_safe_index_removes_duplicate_fact_instead_of_raising(self) -> None:
        duplicate = {
            "plans": [self.plan, dict(self.plan, query_id="QP-P01-02")]
        }

        index, errors = safe_index_query_plans(duplicate)

        self.assertEqual(index, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("重复覆盖事实", errors[0])

    def test_safe_index_skips_plan_without_fact_locator(self) -> None:
        invalid = {
            "plans": [
                {
                    "query_id": "QP-P01-02",
                    "supports_facts": [{"proposition_id": "P01"}],
                    "record_search_terms": ["ER"],
                }
            ]
        }

        index, errors = safe_index_query_plans(invalid)

        self.assertEqual(index, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("缺少事实定位字段", errors[0])


if __name__ == "__main__":
    unittest.main()
