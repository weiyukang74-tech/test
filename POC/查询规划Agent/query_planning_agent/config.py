from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_env_file(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    base_url: str
    api_key: str
    model_name: str
    response_format: str
    enable_thinking: bool | None
    timeout_seconds: int
    max_retries: int

    @classmethod
    def from_env(cls) -> "Settings":
        enable_thinking = False
        settings = cls(
            base_url=os.getenv("MODEL_BASE_URL", "http://127.0.0.1:8000/v1").rstrip("/"),
            api_key=os.getenv("MODEL_API_KEY", ""),
            model_name=os.getenv("MODEL_NAME", "").strip(),
            response_format=os.getenv("MODEL_RESPONSE_FORMAT", "json_object").lower(),
            enable_thinking=enable_thinking,
            timeout_seconds=int(
                os.getenv(
                    "QUERY_PLANNING_MODEL_TIMEOUT_SECONDS",
                    os.getenv("MODEL_TIMEOUT_SECONDS", "120"),
                )
            ),
            max_retries=int(os.getenv("MODEL_MAX_RETRIES", "2")),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.model_name:
            raise ValueError("必须设置MODEL_NAME")
        if self.response_format not in {"json_object", "prompt_only"}:
            raise ValueError("查询规划Agent仅支持json_object或prompt_only")
        if self.timeout_seconds <= 0 or self.max_retries < 0:
            raise ValueError("模型超时必须大于0，重试次数不能小于0")
