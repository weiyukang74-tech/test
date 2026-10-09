from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from .client import OpenAICompatibleClient
from .config import Settings, load_env_file
from .mapping import load_expense_tags, load_medical_record_sources
from .record_feedback import build_record_feedback_source
from .service import QueryPlanningService, collect_rule_facts, load_rule_evidence


PROGRAM_ROOT = Path(__file__).resolve().parents[1]
POC_ROOT = PROGRAM_ROOT.parent
WORKSPACE_ROOT = POC_ROOT.parent
DEFAULT_RULE_EVIDENCE = POC_ROOT / "规则证据结果" / "规则证据结果.json"
DEFAULT_MAPPING = POC_ROOT / "标签视图映射" / "规则证据标签关系映射表_完整版.xlsx"
DEFAULT_MEDICAL_DATA = WORKSPACE_ROOT / "datat" / "南山人医-数据导出"
DEFAULT_OUTPUT_DIR = POC_ROOT / "查询规划结果"
RETRIEVER_ROOT = POC_ROOT / "查询程序"
LOCAL_ENV = PROGRAM_ROOT / ".env"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="为费用明细、检验报告和病例文书生成关键词查询计划")
    parser.add_argument("--rule-evidence", type=Path, default=DEFAULT_RULE_EVIDENCE)
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--medical-data", type=Path, default=DEFAULT_MEDICAL_DATA)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--rule-id")
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="只生成查询规划，不自动运行查询程序",
    )
    parser.add_argument(
        "--no-record-feedback",
        action="store_true",
        help="关闭病程关键词零命中时按病历原文补充词根",
    )
    return parser


def _run_retriever(args: argparse.Namespace) -> float:
    command = [
        sys.executable,
        "-m",
        "evidence_retriever",
        "--rule-evidence",
        str(args.rule_evidence.resolve()),
        "--mapping",
        str(args.mapping.resolve()),
        "--query-plan-dir",
        str(args.output_dir.resolve()),
    ]
    if args.rule_id:
        command.extend(["--rule-id", args.rule_id])
    print("查询规划已完成，开始执行查询程序……")
    started_at = time.perf_counter()
    completed = subprocess.run(command, cwd=RETRIEVER_ROOT, check=False)
    elapsed_seconds = time.perf_counter() - started_at
    if completed.returncode != 0:
        raise RuntimeError(f"查询程序执行失败，退出码={completed.returncode}")
    return elapsed_seconds


def _print_model_metrics(rule_id: str, metrics: object) -> None:
    first_token = (
        f"{metrics.first_token_seconds:.2f} 秒"
        if metrics.first_token_seconds is not None
        else "未获取"
    )
    token_speed = (
        f"{metrics.tokens_per_second:.2f} token/s"
        if metrics.tokens_per_second is not None
        else "未获取"
    )
    token_label = "估算" if metrics.tokens_estimated else "接口统计"
    print(
        f"[{rule_id}] 模型指标：首Token={first_token}，"
        f"Token/s={token_speed}（{token_label}），"
        f"模型用时={metrics.elapsed_seconds:.2f} 秒，"
        f"输出Token={metrics.output_tokens}"
    )


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    overall_started_at = time.perf_counter()
    try:
        env_path = args.env_file.resolve() if args.env_file else LOCAL_ENV
        load_env_file(env_path)
        settings = Settings.from_env()
        print("思考模式：关闭（固定）")
        client = OpenAICompatibleClient(settings)
        rules = load_rule_evidence(args.rule_evidence.resolve())
        mapping_path = args.mapping.resolve()
        medical_data_path = args.medical_data.resolve()
        expense_tags = load_expense_tags(mapping_path)
        medical_record_sources = load_medical_record_sources(mapping_path)
        selected = [
            rule
            for rule in rules
            if not args.rule_id or str(rule.get("rule_id", "")) == args.rule_id
        ]
        if not selected:
            raise ValueError("没有找到指定规则")
        for rule in selected:
            rule_id = str(rule.get("rule_id", "")).strip()
            record_feedback = None
            if not args.no_record_feedback:
                record_feedback = build_record_feedback_source(
                    mapping_path,
                    medical_data_path,
                    str(rule.get("medins_id", "") or ""),
                    str(rule.get("mdtrt_id", "") or ""),
                )
                if record_feedback is None:
                    print(
                        f"[{rule_id}] 未找到查询程序，跳过病程原文关键词反馈",
                        flush=True,
                    )
            service = QueryPlanningService(client, record_feedback=record_feedback)
            facts = collect_rule_facts(
                rule,
                expense_tags=expense_tags,
                medical_record_sources=medical_record_sources,
            )
            metrics_count_before = len(client.metrics_history)
            result = service.plan(rule, facts)
            output = args.output_dir.resolve() / f"查询规划结果_{rule_id}.json"
            service.write(output, result)
            print(
                f"[{rule_id}] 查询规划完成：{len(result['plans'])}个查询计划，"
                f"模型={settings.model_name}"
            )
            print(f"[{rule_id}] 输出：{output}")
            for metrics in client.metrics_history[metrics_count_before:]:
                _print_model_metrics(rule_id, metrics)
        if not args.plan_only:
            query_elapsed_seconds = _run_retriever(args)
            print(f"查询程序调用用时：{query_elapsed_seconds:.2f} 秒")
        print(
            f"规划与查询全链路总用时："
            f"{time.perf_counter() - overall_started_at:.2f} 秒"
        )
        return 0
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
