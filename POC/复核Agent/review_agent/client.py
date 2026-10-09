from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from .config import Settings


class ModelClient(Protocol):
    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class GenerationMetrics:
    elapsed_seconds: float
    first_token_seconds: float | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    tokens_per_second: float | None
    tokens_estimated: bool


class OpenAICompatibleClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.metrics_history: list[GenerationMetrics] = []
        self.last_metrics: GenerationMetrics | None = None

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.settings.model_name,
            "temperature": 0,
            "enable_thinking": False,
            "stream": True,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if self.settings.response_format == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "review_result",
                    "strict": True,
                    "schema": schema,
                },
            }
        elif self.settings.response_format == "json_object":
            body["response_format"] = {"type": "json_object"}

        request = urllib.request.Request(
            url=f"{self.settings.base_url}/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            started_at = time.perf_counter()
            request_stage = "连接接口并等待响应头"
            print(
                f"复核模型请求第{attempt + 1}/{self.settings.max_retries + 1}次开始；"
                f"阶段={request_stage}；读取超时={self.settings.timeout_seconds}秒",
                file=sys.stderr,
                flush=True,
            )
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self.settings.timeout_seconds,
                ) as response:
                    request_stage = "接收模型响应内容"
                    print(
                        "复核模型已收到响应头；"
                        f"用时={time.perf_counter() - started_at:.2f}秒；等待首个内容",
                        file=sys.stderr,
                        flush=True,
                    )
                    (
                        content,
                        output_tokens,
                        input_tokens,
                        total_tokens,
                        first_token_at,
                        streamed,
                    ) = (
                        self._read_response(response, started_at)
                    )
                elapsed_seconds = time.perf_counter() - started_at
                print(
                    f"复核模型响应接收结束；用时={elapsed_seconds:.2f}秒；"
                    f"内容长度={len(content)}字符；开始解析JSON",
                    file=sys.stderr,
                    flush=True,
                )
                has_usage_tokens = (
                    input_tokens is not None and output_tokens is not None
                )
                if input_tokens is None:
                    input_tokens = self._estimate_tokens(system_prompt + user_prompt)
                if output_tokens is None:
                    output_tokens = self._estimate_tokens(content)
                if total_tokens is None:
                    total_tokens = input_tokens + output_tokens
                first_token_seconds = (
                    first_token_at - started_at
                    if first_token_at is not None
                    else None
                )
                tokens_per_second = (
                    output_tokens / elapsed_seconds
                    if output_tokens is not None and elapsed_seconds > 0
                    else None
                )
                metrics = GenerationMetrics(
                    elapsed_seconds=elapsed_seconds,
                    first_token_seconds=first_token_seconds,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                    tokens_per_second=tokens_per_second,
                    tokens_estimated=not has_usage_tokens,
                )
                self.last_metrics = metrics
                self.metrics_history.append(metrics)
                request_stage = "解析模型JSON输出"
                parsed_content = self._parse_json_content(content)
                print("复核模型JSON解析完成", file=sys.stderr, flush=True)
                return parsed_content
            except (
                urllib.error.URLError,
                urllib.error.HTTPError,
                TimeoutError,
                KeyError,
                IndexError,
                json.JSONDecodeError,
                ValueError,
            ) as exc:
                last_error = exc
                print(
                    f"复核模型请求第{attempt + 1}/{self.settings.max_retries + 1}次失败；"
                    f"阶段={request_stage}；用时={time.perf_counter() - started_at:.2f}秒；"
                    f"异常={type(exc).__name__}；原因={exc}",
                    file=sys.stderr,
                    flush=True,
                )
                if attempt >= self.settings.max_retries:
                    break
                retry_delay = min(0.5 * (2**attempt), 1.0)
                print(
                    f"{retry_delay:g}秒后重试复核模型请求",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(retry_delay)
        raise RuntimeError(
            f"模型调用失败，已尝试 {self.settings.max_retries + 1} 次: {last_error}"
        ) from last_error

    @classmethod
    def _read_response(
        cls,
        response: Any,
        started_at: float,
    ) -> tuple[str, int | None, int | None, int | None, float | None, bool]:
        first_line = response.readline()
        while first_line and not first_line.strip():
            first_line = response.readline()
        stripped_first_line = first_line.decode("utf-8", errors="replace").lstrip()
        if not stripped_first_line.startswith("data:"):
            raw_payload = first_line + response.read()
            payload = json.loads(raw_payload.decode("utf-8"))
            content = payload["choices"][0]["message"]["content"]
            usage = payload.get("usage") or {}
            output_tokens = cls._usage_number(usage, "completion_tokens", "output_tokens")
            input_tokens = cls._usage_number(usage, "prompt_tokens", "input_tokens")
            total_tokens = cls._usage_number(usage, "total_tokens")
            first_token_at = time.perf_counter()
            print(
                "复核模型已收到完整非流式内容；"
                f"用时={first_token_at - started_at:.2f}秒",
                file=sys.stderr,
                flush=True,
            )
            return (
                content,
                output_tokens,
                input_tokens,
                total_tokens,
                time.perf_counter(),
                False,
            )

        content_parts: list[str] = []
        completion_tokens: int | None = None
        prompt_tokens: int | None = None
        total_tokens: int | None = None
        first_token_at: float | None = None
        pending_line = first_line
        while pending_line:
            line = pending_line.decode("utf-8", errors="replace").strip()
            if line.startswith("data:"):
                event_text = line[5:].strip()
                if event_text and event_text != "[DONE]":
                    event = json.loads(event_text)
                    usage = event.get("usage") or {}
                    usage_output = cls._usage_number(
                        usage, "completion_tokens", "output_tokens"
                    )
                    usage_input = cls._usage_number(
                        usage, "prompt_tokens", "input_tokens"
                    )
                    usage_total = cls._usage_number(usage, "total_tokens")
                    if usage_output is not None:
                        completion_tokens = usage_output
                    if usage_input is not None:
                        prompt_tokens = usage_input
                    if usage_total is not None:
                        total_tokens = usage_total
                    choices = event.get("choices") or []
                    if choices:
                        delta = choices[0].get("delta") or {}
                        piece = delta.get("content")
                        if piece is None:
                            piece = choices[0].get("text")
                        if isinstance(piece, str) and piece:
                            if first_token_at is None:
                                first_token_at = time.perf_counter()
                                print(
                                    "复核模型流式响应已收到首个内容；"
                                    f"用时={first_token_at - started_at:.2f}秒",
                                    file=sys.stderr,
                                    flush=True,
                                )
                            content_parts.append(piece)
            if line == "data: [DONE]":
                print(
                    "复核模型收到流结束标记；"
                    f"用时={time.perf_counter() - started_at:.2f}秒",
                    file=sys.stderr,
                    flush=True,
                )
                break
            pending_line = response.readline()
        return (
            "".join(content_parts),
            completion_tokens,
            prompt_tokens,
            total_tokens,
            first_token_at,
            True,
        )

    @staticmethod
    def _usage_number(usage: Any, *names: str) -> int | None:
        if not isinstance(usage, dict):
            return None
        for name in names:
            value = usage.get(name)
            if isinstance(value, (int, float)):
                return int(value)
        return None

    @staticmethod
    def _estimate_tokens(content: str) -> int:
        return max(1, round(len(content) / 2))

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        return headers

    @staticmethod
    def _parse_json_content(content: Any) -> dict[str, Any]:
        if not isinstance(content, str):
            raise ValueError("模型返回的 message.content 不是字符串")
        text = content.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            snippet_start = max(0, exc.pos - 120)
            snippet_end = min(len(text), exc.pos + 120)
            snippet = text[snippet_start:snippet_end].replace("\n", "\\n")
            raise ValueError(
                "模型JSON解析失败，"
                f"位置=line {exc.lineno}, column {exc.colno}, char {exc.pos}; "
                f"附近原文={snippet!r}"
            ) from exc
        if not isinstance(parsed, dict):
            raise ValueError("模型输出必须是 JSON 对象")
        return parsed
