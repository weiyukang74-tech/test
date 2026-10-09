from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_env_file(path: Path) -> None:
    """加载简单 KEY=VALUE 配置；已有环境变量优先。"""
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
    model_param_scale: str
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
            model_param_scale=os.getenv("MODEL_PARAM_SCALE", "32B").upper(),
            response_format=os.getenv("MODEL_RESPONSE_FORMAT", "json_schema").lower(),
            enable_thinking=enable_thinking,
            timeout_seconds=int(os.getenv("MODEL_TIMEOUT_SECONDS", "120")),
            max_retries=int(os.getenv("MODEL_MAX_RETRIES", "2")),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.model_name:
            raise ValueError("必须设置 MODEL_NAME，填写实际部署的 27B 或 32B 模型 ID")
        if self.model_param_scale not in {"27B", "32B"}:
            raise ValueError("MODEL_PARAM_SCALE 只能是 27B 或 32B")
        if self.response_format not in {"json_schema", "json_object", "prompt_only"}:
            raise ValueError("MODEL_RESPONSE_FORMAT 只能是 json_schema、json_object 或 prompt_only")
        if self.timeout_seconds <= 0 or self.max_retries < 0:
            raise ValueError("模型超时必须大于 0，重试次数不能小于 0")
