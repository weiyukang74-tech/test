from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any

from .field_dictionary import StandardViewFieldDictionary


SUMMARY_TRIGGER_TERMS = (
    "天数", "日期", "疗程", "频次", "重复收费", "重复计费", "退费", "作废",
    "实际发生", "有效治疗", "总数量", "总金额",
)
DATE_FIELDS = ("executeDate", "billingDate", "drordBegnDate")
SUMMARY_RECORD_THRESHOLD = 20


def _text(value: object) -> str:
    return str(value or "").strip()


def _date_and_minute(record: dict[str, Any]) -> tuple[str, str, str] | None:
    for field_name in DATE_FIELDS:
        value = _text(record.get(field_name))
        if not value:
            continue
        normalized = value.replace("T", " ").replace("/", "-")
        date_value = normalized[:10] if len(normalized) >= 10 else normalized
        minute_value = normalized[:16] if len(normalized) >= 16 else normalized
        if date_value:
            return field_name, date_value, minute_value
    return None


def _is_refund(record: dict[str, Any]) -> bool:
    value = _text(record.get("refundFlag", record.get("refund_flag", ""))).casefold()
    return value in {"1", "true", "yes", "y", "退费", "作废"}


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(_text(value).replace(",", "") or "0")
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _quantity(value: object) -> Decimal:
    if not _text(value):
        return Decimal("1")
    return _decimal(value)


def _number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


def _identity(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def should_summarize(records: list[dict[str, Any]], target_fact: str) -> bool:
    if len(records) < SUMMARY_RECORD_THRESHOLD:
        return False
    service_dates = {
        date_info[1]
        for record in records
        if (date_info := _date_and_minute(record)) is not None
    }
    return len(service_dates) >= 2 or any(
        term in target_fact for term in SUMMARY_TRIGGER_TERMS
    )


def build_expense_summary(
    records: list[dict[str, Any]],
    target_fact: str,
    field_dictionary: StandardViewFieldDictionary,
) -> dict[str, Any]:
    duplicate_counts: defaultdict[str, int] = defaultdict(int)
    for record in records:
        duplicate_counts[_identity(record)] += 1
    exact_duplicate_count = sum(
        count - 1 for count in duplicate_counts.values() if count > 1
    )

    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[
            (
                _text(record.get("hosplistName")),
                _text(record.get("hilistCode")),
                _text(record.get("hosplistCode")),
            )
        ].append(record)

    project_groups: list[dict[str, Any]] = []
    total_quantity = Decimal("0")
    total_amount = Decimal("0")
    total_refund_count = 0
    for (hosplist_name, hilist_code, hosplist_code), group_records in groups.items():
        service_dates: set[str] = set()
        service_minutes: list[str] = []
        quantity = Decimal("0")
        amount = Decimal("0")
        refund_count = 0
        group_identity_counts: defaultdict[str, int] = defaultdict(int)
        for record in group_records:
            date_info = _date_and_minute(record)
            if date_info is not None:
                service_dates.add(date_info[1])
                service_minutes.append(date_info[2])
            quantity += _quantity(record.get("cnt"))
            amount += _decimal(record.get("sumamt"))
            if _is_refund(record):
                refund_count += 1
            group_identity_counts[_identity(record)] += 1
        group_duplicate_count = sum(
            count - 1 for count in group_identity_counts.values() if count > 1
        )
        total_quantity += quantity
        total_amount += amount
        total_refund_count += refund_count
        project_groups.append(
            {
                "hosplist_name": hosplist_name,
                "hilist_code": hilist_code,
                "hosplist_code": hosplist_code,
                "record_count": len(group_records),
                "distinct_service_days": len(service_dates),
                "total_service_times": _number(quantity),
                "exact_duplicate_count": group_duplicate_count,
                "first_service_minute": min(service_minutes) if service_minutes else None,
                "last_service_minute": max(service_minutes) if service_minutes else None,
                "refund_record_count": refund_count,
                "total_amount": _number(amount),
            }
        )

    project_groups.sort(
        key=lambda group: (-int(group["record_count"]), str(group["hosplist_name"]))
    )
    return {
        "mode": "EXPENSE_SUMMARY",
        "target_fact": target_fact,
        "record_count": len(records),
        "unique_record_count": len(duplicate_counts),
        "exact_duplicate_count": exact_duplicate_count,
        "total_service_times": _number(total_quantity),
        "total_amount": _number(total_amount),
        "refund_record_count": total_refund_count,
        "time_precision": "minute",
        "time_field_priority": [
            {"field": field_name, "label": field_dictionary.describe(field_name)["description"]}
            for field_name in DATE_FIELDS
        ],
        "field_labels": field_dictionary.labels_for(
            [
                "hilistCode", "hilistName", "hosplistCode", "hosplistName",
                "executeDate", "billingDate", "drordBegnDate", "cnt", "sumamt", "refundFlag",
            ]
        ),
        "project_groups": project_groups,
    }
