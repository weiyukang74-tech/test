"""病程原文关键词反馈：零命中时用病历真实写法补充词根。

查询规划只负责生成词根；这里提供两件事：
1. 把候选病程整理成给模型看的原文片段（跨文书类型、跨时间抽样并限长）；
2. 通过查询程序的只读取数函数拿到候选病程，并用查询程序同一套匹配逻辑判断命中。

预检或模型调用失败都不影响正常规划，只是不补充词根。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Iterable, Protocol


PROGRAM_ROOT = Path(__file__).resolve().parents[1]
RETRIEVER_ROOT = PROGRAM_ROOT.parent / "查询程序"

EXCERPT_CHARS = 300
EXCERPT_BUDGET = 15_000
# 同一类型内依次取该文书的不同位置，避免只看开头（查房记录开头多是查体和生命体征）
WINDOW_RATIOS = (0.0, 0.35, 0.7)


def _ensure_retriever_importable() -> bool:
    """让查询程序作为库可导入；不可用时返回 False，调用方跳过反馈。"""
    if importlib.util.find_spec("evidence_retriever") is None:
        if str(RETRIEVER_ROOT) not in sys.path:
            sys.path.insert(0, str(RETRIEVER_ROOT))
    return importlib.util.find_spec("evidence_retriever") is not None


def _flatten_content_values(value: object) -> list[str]:
    """直接复用查询程序的取值方式，保证片段与筛选看到的内容一致。"""
    if not _ensure_retriever_importable():
        return [str(value)] if isinstance(value, str) else []
    from evidence_retriever.medical_record_filter import (
        _flatten_content_values as flatten,
    )

    return flatten(value)


class RecordFeedbackSource(Protocol):
    def collect(
        self, tags: Iterable[tuple[str, str]]
    ) -> dict[tuple[str, str], list[dict[str, Any]]]:
        ...

    def match(self, record_content: object, terms: list[str]) -> list[str]:
        ...


def record_kind(name: object) -> str:
    text = str(name)
    return "查房记录" if "查房记录" in text else text


def _evenly_spaced(items: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    """在时间顺序上均匀取 count 篇，覆盖整段住院期间而不是只取开头。"""
    if count >= len(items):
        return list(items)
    step = len(items) / count
    return [items[min(int(index * step), len(items) - 1)] for index in range(count)]


def _window(text: str, ratio: float, limit: int) -> str:
    if len(text) <= limit:
        return text
    start = min(int(len(text) * ratio), len(text) - limit)
    return text[start:start + limit]


def build_record_excerpts(
    records: list[dict[str, Any]],
    budget: int = EXCERPT_BUDGET,
    excerpt_chars: int = EXCERPT_CHARS,
) -> tuple[str, dict[str, Any]]:
    """按文书类型分配抽样篇数，类型内按时间均匀取，并轮流取文书的前/中/后段。

    大类型（如查房记录）按占比分到更多片段，避免整个反馈材料被少数长类型占满；
    总长度不超过 budget，超出时按类型从多到少依次截断。
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        groups.setdefault(record_kind(record.get("recordName")), []).append(record)
    for items in groups.values():
        items.sort(key=lambda record: str(record.get("updateTime", "")))

    order = sorted(groups, key=lambda kind: -len(groups[kind]))
    document_budget = max(1, budget // (excerpt_chars + 40))
    total_documents = len(records) or 1
    allocations = {
        kind: max(1, round(document_budget * len(groups[kind]) / total_documents))
        for kind in order
    }

    excerpts: list[str] = []
    used: list[dict[str, Any]] = []
    total = 0
    for kind in order:
        for index, record in enumerate(
            _evenly_spaced(groups[kind], allocations[kind])
        ):
            text = "".join(_flatten_content_values(record.get("recordContent")))
            snippet = _window(
                text, WINDOW_RATIOS[index % len(WINDOW_RATIOS)], excerpt_chars
            )
            block = (
                f"【{record.get('recordName')} "
                f"{str(record.get('updateTime', ''))[:10]}】{snippet}"
            )
            if total + len(block) > budget:
                return "\n\n".join(excerpts), _excerpt_stats(
                    records, used, total, order
                )
            excerpts.append(block)
            used.append(record)
            total += len(block)
    return "\n\n".join(excerpts), _excerpt_stats(records, used, total, order)


def _excerpt_stats(
    records: list[dict[str, Any]],
    used: list[dict[str, Any]],
    total: int,
    order: list[str],
) -> dict[str, Any]:
    return {
        "candidate_documents": len(records),
        "exposed_documents": len(used),
        "exposed_chars": total,
        "exposed_kinds": {
            kind: sum(1 for record in used if record_kind(record.get("recordName")) == kind)
            for kind in order
        },
    }


class EvidenceRetrieverFeedbackSource:
    def __init__(
        self,
        mapping_path: Path,
        medical_data_path: Path,
        medins_id: str,
        mdtrt_id: str,
    ) -> None:
        self.mapping_path = mapping_path
        self.medical_data_path = medical_data_path
        self.medins_id = medins_id
        self.mdtrt_id = mdtrt_id

    def collect(
        self, tags: Iterable[tuple[str, str]]
    ) -> dict[tuple[str, str], list[dict[str, Any]]]:
        from evidence_retriever.planning_support import collect_progress_records

        return collect_progress_records(
            self.mapping_path,
            self.medical_data_path,
            self.medins_id,
            self.mdtrt_id,
            tags,
        )

    @staticmethod
    def match(record_content: object, terms: list[str]) -> list[str]:
        if not _ensure_retriever_importable():
            return []
        from evidence_retriever.medical_record_filter import match_record_content

        return match_record_content(record_content, terms)


def build_record_feedback_source(
    mapping_path: Path,
    medical_data_path: Path,
    medins_id: str,
    mdtrt_id: str,
) -> EvidenceRetrieverFeedbackSource | None:
    """查询程序不可用时返回 None，规划流程继续按原逻辑运行。"""
    if not _ensure_retriever_importable():
        return None
    return EvidenceRetrieverFeedbackSource(
        mapping_path,
        medical_data_path,
        medins_id,
        mdtrt_id,
    )
