from __future__ import annotations

import unittest
from pathlib import Path

from evidence_retriever.expense_summary import build_expense_summary, should_summarize
from evidence_retriever.field_dictionary import StandardViewFieldDictionary


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DICTIONARY = PROJECT_ROOT / "datat" / "标准病历视图.xlsx"


class ExpenseSummaryTests(unittest.TestCase):
    def test_summary_separates_days_times_and_exact_duplicates(self) -> None:
        records = []
        for index, service_date in enumerate(("2024-12-31", "2025-01-01")):
            records.append(
                {
                    "rxId": str(index),
                    "hosplistName": "大关节松动训练",
                    "hilistCode": "H1",
                    "hosplistCode": "P1",
                    "executeDate": f"{service_date} 16:18:07",
                    "cnt": "1",
                    "sumamt": "30",
                    "refundFlag": None,
                }
            )
        records.append(dict(records[0]))
        summary = build_expense_summary(
            records,
            "实际有效治疗天数",
            StandardViewFieldDictionary(DICTIONARY),
        )
        group = summary["project_groups"][0]
        self.assertEqual(group["distinct_service_days"], 2)
        self.assertEqual(group["total_service_times"], 3)
        self.assertEqual(summary["exact_duplicate_count"], 1)

    def test_summary_trigger_requires_volume_or_summary_intent(self) -> None:
        record = {
            "hosplistName": "项目",
            "executeDate": "2025-01-01 08:00:00",
            "cnt": "1",
        }
        self.assertFalse(should_summarize([record], "实际有效治疗天数"))


if __name__ == "__main__":
    unittest.main()
