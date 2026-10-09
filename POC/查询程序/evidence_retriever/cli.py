from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .context_builder import build_review_context
from .mapping import load_routes
from .planning import load_query_plan
from .service import RetrievalService


PROGRAM_ROOT = Path(__file__).resolve().parents[1]
POC_ROOT = PROGRAM_ROOT.parent
WORKSPACE_ROOT = POC_ROOT.parent
DEFAULT_RULE_EVIDENCE = POC_ROOT / "规则证据结果" / "规则证据结果.json"
DEFAULT_MAPPING = POC_ROOT / "标签视图映射" / "规则证据标签关系映射表_完整版.xlsx"
DEFAULT_MEDICAL_DATA = WORKSPACE_ROOT / "datat" / "南山人医-数据导出"
DEFAULT_OUTPUT = POC_ROOT / "查询结果" / "查询结果.json"
DEFAULT_REVIEW_CONTEXT_OUTPUT = POC_ROOT / "查询结果" / "复核上下文.json"
DEFAULT_INDEX_OUTPUT = POC_ROOT / "查询结果" / "查询结果索引.json"
DEFAULT_QUERY_PLAN_DIR = POC_ROOT / "查询规划结果"
DEFAULT_STANDARD_VIEW_DICTIONARY = WORKSPACE_ROOT / "datat" / "标准病历视图.xlsx"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="根据规则证据标签、映射表和南山人医视图数据查询原始证据")
    parser.add_argument("--rule-evidence", type=Path, default=DEFAULT_RULE_EVIDENCE)
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--medical-data", type=Path, default=DEFAULT_MEDICAL_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--review-context-output",
        type=Path,
        default=DEFAULT_REVIEW_CONTEXT_OUTPUT,
    )
    parser.add_argument("--index-output", type=Path, default=DEFAULT_INDEX_OUTPUT)
    parser.add_argument("--query-plan-dir", type=Path, default=DEFAULT_QUERY_PLAN_DIR)
    parser.add_argument(
        "--standard-view-dictionary",
        type=Path,
        default=DEFAULT_STANDARD_VIEW_DICTIONARY,
        help="标准病历视图字段字典Excel路径",
    )
    parser.add_argument("--medins-id")
    parser.add_argument("--mdtrt-id")
    parser.add_argument("--rule-id")
    parser.add_argument("--proposition-id")
    parser.add_argument("--required-only", action="store_true", help="只查询 required_tags")
    parser.add_argument(
        "--secondary-mode",
        choices=("auto", "deferred", "always"),
        default="auto",
        help="auto=主要来源缺失时自动补查；deferred=暂不查次要来源；always=始终查询",
    )
    return parser


def _rule_case_parameters(
    rule_evidence: list[dict],
    rule_id: str | None,
) -> tuple[str, str]:
    if not rule_id and len(rule_evidence) == 1:
        rule_id = str(rule_evidence[0].get("rule_id", ""))
    if not rule_id:
        return "", ""
    for entry in rule_evidence:
        if str(entry.get("rule_id", "")) != rule_id:
            continue
        case = entry.get("case_parameters")
        if not isinstance(case, dict):
            case = entry
        return str(case.get("medins_id", case.get("medinsId", "")) or "").strip(), str(
            case.get("mdtrt_id", case.get("mdtrtId", "")) or ""
        ).strip()
    raise ValueError(f"规则证据结果中找不到规则: {rule_id}")


def _rule_ids(rule_evidence: list[dict], requested_rule_id: str | None) -> list[str]:
    if requested_rule_id:
        _rule_case_parameters(rule_evidence, requested_rule_id)
        return [requested_rule_id]
    rule_ids = [str(entry.get("rule_id", "")).strip() for entry in rule_evidence]
    rule_ids = list(dict.fromkeys(rule_id for rule_id in rule_ids if rule_id))
    if not rule_ids:
        raise ValueError("规则证据结果中没有有效rule_id")
    return rule_ids


def _rule_output_path(base_path: Path, rule_id: str) -> Path:
    if base_path.suffix:
        return base_path.with_name(f"{base_path.stem}_{rule_id}{base_path.suffix}")
    return base_path / f"{rule_id}.json"


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _is_runtime_placeholder(value: str | None) -> bool:
    return not value or value.strip() in {"运行时传入", "runtime", "RUNTIME"}


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    query_started_at = time.perf_counter()
    try:
        mapping_path = args.mapping.resolve()
        routes = load_routes(mapping_path)
        service = RetrievalService(mapping_path, args.medical_data.resolve())
        rule_evidence = service.load_rule_evidence(args.rule_evidence.resolve())
        rule_ids = _rule_ids(rule_evidence, args.rule_id)
        batch_mode = args.rule_id is None
        index_entries: list[dict[str, object]] = []
        completed = 0
        for current_rule_id in rule_ids:
            evidence_medins_id, evidence_mdtrt_id = _rule_case_parameters(
                rule_evidence,
                current_rule_id,
            )
            medins_id = args.medins_id or evidence_medins_id
            mdtrt_id = args.mdtrt_id or evidence_mdtrt_id
            if not mdtrt_id:
                index_entries.append(
                    {
                        "rule_id": current_rule_id,
                        "status": "SKIPPED",
                        "reason": "规则证据结果缺少mdtrt_id",
                    }
                )
                print(f"[{current_rule_id}] 跳过：规则证据结果缺少mdtrt_id")
                continue
            if _is_runtime_placeholder(medins_id):
                medins_id = service.medical_data.resolve_medins_id(mdtrt_id) or ""
            if _is_runtime_placeholder(medins_id):
                medins_id = routes[0].medins_id
            if _is_runtime_placeholder(medins_id):
                index_entries.append(
                    {
                        "rule_id": current_rule_id,
                        "status": "SKIPPED",
                        "mdtrt_id": mdtrt_id,
                        "reason": "无法根据mdtrt_id解析真实medinsId",
                    }
                )
                print(f"[{current_rule_id}] 跳过：无法根据mdtrt_id解析真实medinsId")
                continue
            try:
                query_plan_path = (
                    args.query_plan_dir.resolve()
                    / f"查询规划结果_{current_rule_id}.json"
                )
                query_plan = (
                    load_query_plan(query_plan_path)
                    if query_plan_path.exists()
                    else None
                )
                if query_plan is None:
                    print(
                        f"[{current_rule_id}] 未找到查询规划文件，"
                        "费用明细沿用原全量查询逻辑"
                    )
                result = service.run(
                    rule_evidence,
                    medins_id=medins_id,
                    mdtrt_id=mdtrt_id,
                    rule_id=current_rule_id,
                    proposition_id=args.proposition_id,
                    include_supplementary=not args.required_only,
                    secondary_mode=args.secondary_mode,
                    query_plan=query_plan,
                )
                use_default_output = args.output.resolve() == DEFAULT_OUTPUT.resolve()
                use_default_context_output = (
                    args.review_context_output.resolve()
                    == DEFAULT_REVIEW_CONTEXT_OUTPUT.resolve()
                )
                output_path = (
                    _rule_output_path(args.output.resolve(), current_rule_id)
                    if batch_mode or use_default_output
                    else args.output.resolve()
                )
                review_context_path = (
                    _rule_output_path(args.review_context_output.resolve(), current_rule_id)
                    if batch_mode or use_default_context_output
                    else args.review_context_output.resolve()
                )
                _write_json(output_path, result)
                _write_json(
                    review_context_path,
                    build_review_context(
                        rule_evidence,
                        result,
                        args.standard_view_dictionary.resolve(),
                    ),
                )
                rules = result.get("rules", [result])
                proposition_count = sum(len(rule["propositions"]) for rule in rules)
                evidence_count = sum(
                    len(proposition["evidence"])
                    for rule in rules
                    for proposition in rule["propositions"]
                )
                index_entries.append(
                    {
                        "rule_id": current_rule_id,
                        "status": "COMPLETED",
                        "medins_id": medins_id,
                        "mdtrt_id": mdtrt_id,
                        "proposition_count": proposition_count,
                        "evidence_count": evidence_count,
                        "query_result": str(output_path),
                        "review_context": str(review_context_path),
                    }
                )
                completed += 1
                print(
                    f"[{current_rule_id}] 查询完成：{proposition_count} 个申诉命题，"
                    f"{evidence_count} 个标签结果"
                )
                print(f"[{current_rule_id}] 查询结果：{output_path}")
                print(f"[{current_rule_id}] 复核上下文：{review_context_path}")
            except Exception as exc:
                index_entries.append(
                    {
                        "rule_id": current_rule_id,
                        "status": "ERROR",
                        "medins_id": medins_id,
                        "mdtrt_id": mdtrt_id,
                        "error": str(exc),
                    }
                )
                print(f"[{current_rule_id}] 查询失败：{exc}", file=sys.stderr)

        if batch_mode:
            index_path = args.index_output.resolve()
            _write_json(
                index_path,
                {
                    "rule_evidence": str(args.rule_evidence.resolve()),
                    "count": len(index_entries),
                    "completed": completed,
                    "entries": index_entries,
                },
            )
            print(f"批量查询索引：{index_path}")
        print(
            f"查询程序整体用时："
            f"{time.perf_counter() - query_started_at:.2f} 秒"
        )
        return 0 if completed else 1
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
