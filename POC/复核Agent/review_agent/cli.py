from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .client import OpenAICompatibleClient
from .config import Settings, load_env_file
from .service import ReviewService


PROGRAM_ROOT = Path(__file__).resolve().parents[1]
POC_ROOT = PROGRAM_ROOT.parent
DEFAULT_CONTEXT = POC_ROOT / "查询结果" / "复核上下文.json"
DEFAULT_OUTPUT = POC_ROOT / "复核结果" / "复核结果.json"
SHARED_ENV = POC_ROOT / "查询规划Agent" / ".env"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="根据复核上下文执行医保规则证据复核")
    parser.add_argument("--context", type=Path, default=DEFAULT_CONTEXT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--review-round", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true", help="只校验输入，不调用模型")
    parser.add_argument(
        "--no-thinking",
        action="store_true",
        help="兼容参数；模型已固定关闭思考模式",
    )
    return parser


def _resolve_env_path(explicit_path: Path | None) -> Path:
    if explicit_path is not None:
        return explicit_path.resolve()
    return SHARED_ENV


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        context_path = args.context.resolve()
        context = ReviewService.load_context(context_path)
        if args.dry_run:
            print(f"复核上下文校验通过：{context_path}")
            print(f"规则：{context['rule_id']}，命题：{len(context['propositions'])} 个")
            return 0

        env_path = _resolve_env_path(args.env_file)
        load_env_file(env_path)
        settings = Settings.from_env()
        print("思考模式：关闭（固定）")
        service = ReviewService(OpenAICompatibleClient(settings))
        model_started = time.perf_counter()
        result = service.review(context, review_round=args.review_round)
        model_elapsed_seconds = time.perf_counter() - model_started
        output_path = args.output.resolve()
        service.write_result(output_path, result)

        print(f"复核完成：{result['rule_id']}，第 {result['review_round']} 轮")
        print(f"模型：{settings.model_name}（{settings.model_param_scale}）")
        print(f"复核状态：{result['review_status']}")
        print(f"案件状态：{result['case_status']}")
        print(f"模型整体输出时间：{model_elapsed_seconds:.2f} 秒")
        for index, metrics in enumerate(service.metrics, start=1):
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
                f"模型调用{index}：首Token={first_token}，"
                f"Token/s={token_speed}（{token_label}），"
                f"用时={metrics.elapsed_seconds:.2f} 秒，"
                f"输入Token={metrics.input_tokens if metrics.input_tokens is not None else '未获取'}，"
                f"输出Token={metrics.output_tokens if metrics.output_tokens is not None else '未获取'}，"
                f"总Token={metrics.total_tokens if metrics.total_tokens is not None else '未获取'}"
            )
        if service.citation_repair_attempted:
            print("引用专项修复：已执行 1 次")
        if service.citation_degraded_fact_count:
            print(
                "引用安全降级："
                f"{service.citation_degraded_fact_count} 个事实项已降级为UNKNOWN"
            )
        if service.citation_repair_error:
            print(f"引用专项修复异常：{service.citation_repair_error}")
        print(
            "最终结论："
            f"{result['final_conclusion']['result']}"
            f"（is_final={str(result['final_conclusion']['is_final']).lower()}）"
        )
        print(f"输出：{output_path}")
        return 0
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
