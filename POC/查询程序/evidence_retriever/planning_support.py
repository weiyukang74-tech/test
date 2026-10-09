"""供查询规划阶段使用的只读取数函数。

查询规划在为某个证据点生成病程检索词根后，需要知道这些词根在该患者病程里是否命中，
才能在零命中时用病历原文向模型补充一次词根。这里只做“按标签映射取回病程记录”，
不做筛选、不写文件、不判断证据，筛选逻辑仍在 medical_record_filter 中。
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .mapping import RECORD_NAME_CONTAINS, index_routes, load_routes
from .medical_data import MarkdownMedicalData
from .medical_record_filter import is_progress_record, medical_record_identity


RUNTIME_PLACEHOLDERS = {"运行时传入", "runtime"}


def collect_progress_records(
    mapping_path: Path,
    medical_data_path: Path,
    medins_id: str,
    mdtrt_id: str,
    tags: Iterable[tuple[str, str]],
) -> dict[tuple[str, str], list[dict[str, object]]]:
    """按标签映射取回该次就诊的病程记录，标签含中文名（一级证据域、二级证据标签）。

    - 只返回 `recordType=病程记录` 的文书，与查询程序筛选时的候选范围一致；
    - recordName 按映射表的“匹配方式”执行，recordType 支持“；”分隔的多个类型；
    - medinsId 为“运行时传入”等占位值时，按 mdtrtId 从数据中解析；
    - 标签没有映射到病程记录、或该标签取不到数据时返回空列表。
    """
    route_index = index_routes(load_routes(mapping_path))
    medical_data = MarkdownMedicalData(medical_data_path)
    resolved_medins_id = str(medins_id or "").strip()
    if not resolved_medins_id or resolved_medins_id.lower() in RUNTIME_PLACEHOLDERS:
        resolved_medins_id = medical_data.resolve_medins_id(mdtrt_id) or ""

    collected: dict[tuple[str, str], list[dict[str, object]]] = {}
    for tag in dict.fromkeys(tags):
        records: list[dict[str, object]] = []
        identities: set[str] = set()
        groups = route_index.get(tag, {"PRIMARY": [], "SECONDARY": []})
        for level in ("PRIMARY", "SECONDARY"):
            for route in groups[level]:
                if route.record_name is None and route.record_type is None:
                    continue
                for record in medical_data.query_records(
                    resolved_medins_id,
                    mdtrt_id,
                    route.record_name,
                    route.record_type,
                    record_name_contains=route.match_mode == RECORD_NAME_CONTAINS,
                ):
                    if not is_progress_record(record):
                        continue
                    identity = medical_record_identity(record)
                    if identity in identities:
                        continue
                    identities.add(identity)
                    records.append(record)
        collected[tag] = records
    return collected
