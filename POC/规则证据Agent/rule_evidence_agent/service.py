from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .catalog import EvidenceTag
from .client import ModelClient
from .excel_reader import RuleRecord
from .prompt import build_repair_prompt, build_user_prompt, load_system_prompt
from .schema import load_schema
from .validation import validate_result


AGGREGATE_FILENAME = "规则证据结果.json"
SEPARATOR = "-----------------"


@dataclass(frozen=True)
class RuleTiming:
    rule_id: str
    output_filename: str
    elapsed_seconds: float
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class RunSummary:
    created: int
    updated: int
    skipped: int
    timings: tuple[RuleTiming, ...]
    elapsed_seconds: float

    @property
    def total(self) -> int:
        return self.created + self.updated


class RuleDecompositionService:
    def __init__(self, client: ModelClient, tags: list[EvidenceTag]):
        self.client = client
        self.tags = tags
        self.schema = load_schema()
        self._token_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    def _generate(self, system_prompt: str, user_prompt: str) -> dict:
        result = self.client.generate(system_prompt, user_prompt, self.schema)
        usage = getattr(self.client, "last_usage", {})
        if isinstance(usage, dict):
            for key in self._token_usage:
                value = usage.get(key, 0)
                if isinstance(value, (int, float)):
                    self._token_usage[key] += int(value)
        return result

    def decompose(self, record: RuleRecord) -> dict:
        system_prompt = load_system_prompt()
        self._token_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        try:
            result = self._generate(
                system_prompt,
                build_user_prompt(record, self.tags),
            )
            result.pop("evidence_tasks", None)
            validate_result(result, self.tags)
            return result
        except ValueError as validation_error:
            repaired_result = self._generate(
                system_prompt,
                build_repair_prompt(record, self.tags, str(validation_error)),
            )
            repaired_result.pop("evidence_tasks", None)
            validate_result(repaired_result, self.tags)
            return repaired_result

    @staticmethod
    def _load_entries(path: Path) -> list[dict]:
        if not path.exists():
            return []
        raw_text = path.read_text(encoding="utf-8")
        if not raw_text.strip():
            return []
        payload = json.loads(raw_text)
        if not isinstance(payload, list):
            raise ValueError(f"汇总结果必须是 JSON 数组: {path}")
        for index, entry in enumerate(payload, start=1):
            if not isinstance(entry, dict) or not isinstance(entry.get("rule_id"), str) or not isinstance(entry.get("result"), dict):
                raise ValueError(f"汇总结果第 {index} 项必须包含 rule_id 和 result 对象")
        return payload

    @staticmethod
    def _write_entries(path: Path, entries: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    def run(self, records: Iterable[RuleRecord], output_dir: Path, *, overwrite_existing: bool = False) -> RunSummary:
        del overwrite_existing
        started = time.perf_counter()
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / AGGREGATE_FILENAME
        entries = self._load_entries(output_path)
        existing_ids = {entry["rule_id"] for entry in entries}
        created = skipped = 0
        timings: list[RuleTiming] = []

        for record in records:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", record.rule_id):
                raise ValueError(f"规则编号不能安全写入结果: {record.rule_id}")
            if record.rule_id in existing_ids:
                existing_entry = next(
                    entry for entry in entries if entry["rule_id"] == record.rule_id
                )
                metadata = {
                    "mdtrt_id": record.mdtrt_id,
                    "medins_id": record.medins_id,
                    "visit_no": record.visit_no,
                }
                if any(existing_entry.get(key) != value for key, value in metadata.items()):
                    existing_entry.update(metadata)
                    self._write_entries(output_path, entries)
                skipped += 1
                continue
            rule_started = time.perf_counter()
            result = self.decompose(record)
            entries.append(
                {
                    "_comment": f"{SEPARATOR} {record.rule_id} {SEPARATOR}",
                    "rule_id": record.rule_id,
                    "mdtrt_id": record.mdtrt_id,
                    "medins_id": record.medins_id,
                    "visit_no": record.visit_no,
                    "result": result,
                }
            )
            self._write_entries(output_path, entries)
            existing_ids.add(record.rule_id)
            created += 1
            timings.append(
                RuleTiming(
                    record.rule_id,
                    AGGREGATE_FILENAME,
                    time.perf_counter() - rule_started,
                    self._token_usage["input_tokens"],
                    self._token_usage["output_tokens"],
                    self._token_usage["total_tokens"],
                )
            )

        return RunSummary(created, 0, skipped, tuple(timings), time.perf_counter() - started)
