from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "review_result.schema.json"
MODEL_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "schemas" / "review_decision.schema.json"
)


def load_schema() -> dict[str, Any]:
    payload = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("输出 Schema 必须是 JSON 对象")
    return payload


def load_model_schema() -> dict[str, Any]:
    payload = json.loads(MODEL_SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("模型输出 Schema 必须是 JSON 对象")
    return payload
