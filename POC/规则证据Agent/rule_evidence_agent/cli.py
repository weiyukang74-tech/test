from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .catalog import load_tags
from .client import OpenAICompatibleClient
from .config import Settings, load_env_file
from .excel_reader import read_published_rules
from .service import RuleDecompositionService


AGENT_ROOT = Path(__file__).resolve().parents[1]
POC_ROOT = AGENT_ROOT.parent
WORKSPACE_ROOT = POC_ROOT.parent
DEFAULT_INPUT = WORKSPACE_ROOT / "datat" / "南山人医-数据导出" / "住院负面清单_处理.xlsx"
DEFAULT_TAG_CATALOG = AGENT_ROOT / "标签目录.xlsx"
DEFAULT_OUTPUT_DIR = POC_ROOT / "规则证据结果"
DEFAULT_ENV = POC_ROOT / "查询规划Agent" / ".env"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="理解医保违规描述、生成申诉命题并匹配证据标签")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="负面清单 Excel 路径")
    parser.add_argument("--tag-catalog", type=Path, default=DEFAULT_TAG_CATALOG, help="独立维护的证据标签目录 Excel 路径")
    parser.add_argument("--output-dir", "--output", dest="output_dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="规则拆解结果目录")
    parser.add_argument("--env-file", type=Path, help="可选的 .env 配置文件")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--limit", type=int, help="只处理前 N 条已发布规则")
    selection.add_argument(
        "--rule-number",
        type=int,
        help="只处理按有效规则顺序计数的第 N 条，序号从 1 开始",
    )
    parser.add_argument(
        "--write-mode",
        choices=("append",),
        default="append",
        help="结果写入方式；当前固定为 append，只追加尚未存在的 rule_id",
    )
    parser.add_argument("--no-thinking", action="store_true", help="兼容参数；模型已固定关闭思考模式")
    parser.add_argument("--dry-run", action="store_true", help="只校验和读取 Excel，不调用模型")
    return parser


def print_decomposition_summary(timings: tuple, output_dir: Path) -> None:
    print("模型输出摘要：")
    aggregate = json.loads((output_dir / "规则证据结果.json").read_text(encoding="utf-8"))
    by_rule_id = {entry["rule_id"]: entry["result"] for entry in aggregate}
    for timing in timings:
        result = by_rule_id[timing.rule_id]
        print(f"[{timing.rule_id}]")
        print(json.dumps(result, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        records = read_published_rules(args.input.resolve())
        tags = load_tags(args.tag_catalog.resolve())
        if args.rule_number is not None:
            if args.rule_number <= 0:
                raise ValueError("--rule-number 必须大于 0")
            if args.rule_number > len(records):
                raise ValueError(
                    f"--rule-number 超出范围：共 {len(records)} 条有效规则，"
                    f"请求第 {args.rule_number} 条"
                )
            records = [records[args.rule_number - 1]]
        elif args.limit is not None:
            if args.limit <= 0:
                raise ValueError("--limit 必须大于 0")
            records = records[: args.limit]
        if args.dry_run:
            print(f"输入校验通过，共读取 {len(records)} 条规则、{len(tags)} 个证据标签：")
            for record in records:
                print(f"- {record.rule_id} | {record.source_rule_name} | 源版本 {record.source_version}")
            return 0

        legacy_local_env = (AGENT_ROOT / ".env").resolve()
        requested_env = args.env_file.resolve() if args.env_file else None
        env_path = (
            DEFAULT_ENV
            if requested_env is None or requested_env == legacy_local_env
            else requested_env
        )
        load_env_file(env_path)
        settings = Settings.from_env()
        client = OpenAICompatibleClient(settings)
        service = RuleDecompositionService(client, tags)
        output_dir = args.output_dir.resolve()
        print("思考模式：关闭（固定）")
        print(f"标签目录：{args.tag_catalog.resolve()}（{len(tags)} 个标签）")
        print("写入模式：单文件追加（已有 rule_id 跳过，不覆盖）")
        summary = service.run(
            records,
            output_dir,
        )
        print(
            f"规则拆解完成：成功处理 {summary.total} 条"
            f"（新增 {summary.created}，更新 {summary.updated}，跳过 {summary.skipped}）"
        )
        total_input_tokens = 0
        total_output_tokens = 0
        total_tokens = 0
        for timing in summary.timings:
            total_input_tokens += timing.input_tokens
            total_output_tokens += timing.output_tokens
            total_tokens += timing.total_tokens
            print(
                f"[{timing.rule_id}] 拆解耗时：{timing.elapsed_seconds:.2f} 秒，"
                f"输入Token：{timing.input_tokens}，"
                f"输出Token：{timing.output_tokens}，"
                f"总Token：{timing.total_tokens}"
            )
        print(f"本次总耗时：{summary.elapsed_seconds:.2f} 秒")
        print(
            f"本次Token总计：输入Token={total_input_tokens}，"
            f"输出Token={total_output_tokens}，总Token={total_tokens}"
        )
        print(f"模型：{settings.model_name}（{settings.model_param_scale}）")
        print(f"输出目录：{output_dir}")
        if settings.enable_thinking is True:
            print_decomposition_summary(summary.timings, output_dir)
        return 0
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
