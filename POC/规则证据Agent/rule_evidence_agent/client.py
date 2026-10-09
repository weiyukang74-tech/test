from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Protocol

from .config import Settings


class ModelClient(Protocol):
    def generate(self, system_prompt: str, user_prompt: str, schema: dict[str, Any]) -> dict[str, Any]: ...


class OpenAICompatibleClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.last_usage: dict[str, int] = {}

    def generate(self, system_prompt: str, user_prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.last_usage = {}
        body: dict[str, Any] = {
            "model": self.settings.model_name,
            "temperature": 0,
            "enable_thinking": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if self.settings.response_format == "json_schema":
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "rule_profile", "strict": True, "schema": schema},
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
            try:
                with urllib.request.urlopen(request, timeout=self.settings.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.last_usage = self._extract_usage(payload.get("usage"))
                # 思考模式下接口可能同时返回 reasoning_content。原始推理不落盘，
                # 仅使用 content 中的正式结构化答案。
                content = payload["choices"][0]["message"]["content"]
                return self._parse_json_content(content)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, KeyError, IndexError, json.JSONDecodeError, ValueError) as exc:
                last_error = exc
                if attempt >= self.settings.max_retries:
                    break
                time.sleep(min(2**attempt, 4))
        raise RuntimeError(f"模型调用失败，已尝试 {self.settings.max_retries + 1} 次: {last_error}") from last_error

    @staticmethod
    def _extract_usage(usage: Any) -> dict[str, int]:
        if not isinstance(usage, dict):
            return {}

        def number(*names: str) -> int:
            for name in names:
                value = usage.get(name)
                if isinstance(value, (int, float)):
                    return int(value)
            return 0

        input_tokens = number("prompt_tokens", "input_tokens")
        output_tokens = number("completion_tokens", "output_tokens")
        total_tokens = number("total_tokens") or input_tokens + output_tokens
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }

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
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("模型输出必须是 JSON 对象")
        return parsed
