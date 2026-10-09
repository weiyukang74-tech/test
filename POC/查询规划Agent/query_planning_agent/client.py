from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import Settings


@dataclass(frozen=True)
class GenerationMetrics:
    elapsed_seconds: float
    first_token_seconds: float | None
    output_tokens: int
    tokens_per_second: float | None
    tokens_estimated: bool


class OpenAICompatibleClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.metrics_history: list[GenerationMetrics] = []
        self.last_metrics: GenerationMetrics | None = None

    def generate_record_supplement(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        return self.generate(system_prompt, user_prompt, allow_empty_list=True)

    def generate(
        self, system_prompt: str, user_prompt: str, *, allow_empty_list: bool = False
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
        if self.settings.response_format == "json_object":
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
                f"模型请求第{attempt + 1}/{self.settings.max_retries + 1}次开始；"
                f"阶段={request_stage}；读取超时={self.settings.timeout_seconds}秒",
                file=sys.stderr,
                flush=True,
            )
            try:
                with urllib.request.urlopen(request, timeout=self.settings.timeout_seconds) as response:
                    request_stage = "接收模型流式响应"
                    print(
                        "模型请求已收到响应头；"
                        f"用时={time.perf_counter() - started_at:.2f}秒；等待首个内容",
                        file=sys.stderr,
                        flush=True,
                    )
                    content, output_tokens, first_token_at = self._read_response(
                        response,
                        started_at,
                    )
                elapsed_seconds = time.perf_counter() - started_at
                print(
                    f"模型响应接收结束；用时={elapsed_seconds:.2f}秒；"
                    f"内容长度={len(content)}字符；开始解析JSON",
                    file=sys.stderr,
                    flush=True,
                )
                tokens_estimated = output_tokens is None
                if output_tokens is None:
                    output_tokens = self._estimate_tokens(content)
                first_token_seconds = (
                    first_token_at - started_at
                    if first_token_at is not None
                    else None
                )
                tokens_per_second = (
                    output_tokens / elapsed_seconds if elapsed_seconds > 0 else None
                )
                metrics = GenerationMetrics(
                    elapsed_seconds=elapsed_seconds,
                    first_token_seconds=first_token_seconds,
                    output_tokens=output_tokens,
                    tokens_per_second=tokens_per_second,
                    tokens_estimated=tokens_estimated,
                )
                request_stage = "解析模型JSON输出"
                parsed_content = self._parse_json(content, allow_empty_list=allow_empty_list)
                self.last_metrics = metrics
                self.metrics_history.append(metrics)
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
                attempt_elapsed = time.perf_counter() - started_at
                retry_delay = min(2**attempt, 4)
                will_retry = attempt < self.settings.max_retries
                print(
                    self._format_attempt_failure(
                        attempt + 1,
                        self.settings.max_retries + 1,
                        request_stage,
                        attempt_elapsed,
                        exc,
                        retry_delay if will_retry else None,
                    ),
                    file=sys.stderr,
                    flush=True,
                )
                if attempt >= self.settings.max_retries:
                    break
                time.sleep(retry_delay)
        raise RuntimeError(
            f"模型调用失败，已尝试{self.settings.max_retries + 1}次: {last_error}"
        ) from last_error

    @staticmethod
    def _format_attempt_failure(
        attempt_number: int,
        max_attempts: int,
        request_stage: str,
        elapsed_seconds: float,
        error: Exception,
        retry_delay: int | None,
    ) -> str:
        details = [
            f"模型请求第{attempt_number}/{max_attempts}次失败",
            f"阶段={request_stage}",
            f"用时={elapsed_seconds:.2f}秒",
            f"异常={type(error).__name__}",
        ]
        if isinstance(error, urllib.error.HTTPError):
            details.append(f"HTTP状态={error.code}")
            try:
                response_text = error.read(2000).decode("utf-8", errors="replace").strip()
            except Exception:
                response_text = ""
            if response_text:
                details.append(f"响应={response_text}")
        elif isinstance(error, urllib.error.URLError):
            reason = getattr(error, "reason", None)
            details.append(f"原因={reason or error}")
        else:
            details.append(f"原因={error}")
        if retry_delay is not None:
            details.append(f"{retry_delay}秒后重试")
        else:
            details.append("不再重试")
        return "；".join(details)

    @classmethod
    def _read_response(
        cls,
        response: Any,
        started_at: float,
    ) -> tuple[str, int | None, float | None]:
        first_line = response.readline()
        while first_line and not first_line.strip():
            first_line = response.readline()
        decoded_first_line = first_line.decode("utf-8", errors="replace").lstrip()
        if not decoded_first_line.startswith("data:"):
            raw_payload = first_line + response.read()
            payload = json.loads(raw_payload.decode("utf-8"))
            content = payload["choices"][0]["message"]["content"]
            usage = payload.get("usage") or {}
            first_token_at = time.perf_counter()
            print(
                "模型已收到完整非流式内容；"
                f"用时={first_token_at - started_at:.2f}秒",
                file=sys.stderr,
                flush=True,
            )
            return content, usage.get("completion_tokens"), first_token_at

        content_parts: list[str] = []
        output_tokens: int | None = None
        first_token_at: float | None = None
        pending_line = first_line
        while pending_line:
            line = pending_line.decode("utf-8", errors="replace").strip()
            if line.startswith("data:"):
                event_text = line[5:].strip()
                if event_text and event_text != "[DONE]":
                    event = json.loads(event_text)
                    usage = event.get("usage") or {}
                    if usage.get("completion_tokens") is not None:
                        output_tokens = int(usage["completion_tokens"])
                    choices = event.get("choices") or []
                    if choices:
                        delta = choices[0].get("delta") or {}
                        piece = delta.get("content")
                        if isinstance(piece, str) and piece:
                            if first_token_at is None:
                                first_token_at = time.perf_counter()
                                print(
                                    "模型流式响应已收到首个内容；"
                                    f"用时={first_token_at - started_at:.2f}秒",
                                    file=sys.stderr,
                                    flush=True,
                                )
                            content_parts.append(piece)
            if line == "data: [DONE]":
                break
            pending_line = response.readline()
        return "".join(content_parts), output_tokens, first_token_at

    @staticmethod
    def _estimate_tokens(content: str) -> int:
        return max(1, round(len(content) / 2))

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        return headers

    @staticmethod
    def _parse_json(content: Any, *, allow_empty_list: bool = False) -> dict[str, Any]:
        if not isinstance(content, str):
            raise ValueError("模型返回content不是字符串")
        text = content.strip()
        if text.startswith("```"):
            lines = text.splitlines()[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        payload = json.loads(text)
        if allow_empty_list and isinstance(payload, list) and not payload:
            return {"added_record_search_terms": []}
        if not isinstance(payload, dict):
            preview = json.dumps(content[:800], ensure_ascii=False)
            if len(content) > 800:
                preview += "（已截断，仅显示前800字符）"
            raise ValueError(
                "模型输出必须是JSON对象；"
                f"实际类型={type(payload).__name__}；原始输出={preview}"
            )
        return payload
