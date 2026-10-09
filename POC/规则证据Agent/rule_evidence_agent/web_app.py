from __future__ import annotations

import argparse
import json
import re
import threading
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .catalog import load_tags
from .client import OpenAICompatibleClient
from .config import Settings, load_env_file
from .excel_reader import read_published_rules
from .prompt import PROMPT_PATH, load_system_prompt
from .service import RuleDecompositionService


AGENT_ROOT = Path(__file__).resolve().parents[1]
POC_ROOT = AGENT_ROOT.parent
INPUT_PATH = POC_ROOT / "负面清单" / "负面清单.xlsx"
TAG_CATALOG_PATH = AGENT_ROOT / "标签目录.xlsx"
OUTPUT_DIR = POC_ROOT / "规则证据结果"
ENV_PATH = POC_ROOT / "查询规划Agent" / ".env"
WEB_ROOT = AGENT_ROOT / "web_ui" / "dist"
OUTPUT_PATTERN = re.compile(r"^(?:(?P<legacy_class>[YCN])_)?(?P<rule_id>RULE-\d+)\.json$")

_state_lock = threading.Lock()
_run_state: dict[str, Any] = {
    "status": "idle",
    "message": "等待运行",
    "started_at": None,
    "finished_at": None,
    "elapsed_seconds": None,
    "summary": None,
}


def _set_run_state(**values: Any) -> None:
    with _state_lock:
        _run_state.update(values)


def _get_run_state() -> dict[str, Any]:
    with _state_lock:
        return dict(_run_state)


def _read_default_thinking() -> bool:
    if not ENV_PATH.exists():
        return False
    for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "MODEL_ENABLE_THINKING":
            return value.strip().strip('"').strip("'").lower() in {"true", "1", "yes"}
    return False


def _read_rule_outputs() -> dict[str, dict[str, Any]]:
    outputs: dict[str, dict[str, Any]] = {}
    priorities: dict[str, int] = {}
    if not OUTPUT_DIR.exists():
        return outputs
    for path in sorted(OUTPUT_DIR.glob("*.json")):
        match = OUTPUT_PATTERN.fullmatch(path.name)
        if not match:
            continue
        rule_id = match.group("rule_id")
        priority = 1 if match.group("legacy_class") is None else 0
        if priority < priorities.get(rule_id, -1):
            continue
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            outputs[rule_id] = {
                "state": "done",
                "filename": path.name,
                "result": result,
            }
            priorities[rule_id] = priority
        except (OSError, json.JSONDecodeError) as exc:
            outputs[rule_id] = {
                "state": "error",
                "filename": path.name,
                "error": str(exc),
            }
            priorities[rule_id] = priority
    return outputs


def _build_rules_payload() -> list[dict[str, Any]]:
    records = read_published_rules(INPUT_PATH)
    outputs = _read_rule_outputs()
    rules: list[dict[str, Any]] = []
    for record in records:
        output = outputs.get(record.rule_id)
        item: dict[str, Any] = {
            "rule_id": record.rule_id,
            "rule_text": record.rule_text,
            "state": "pending",
            "filename": None,
            "result": None,
        }
        if output:
            item.update(output)
        rules.append(item)
    return rules


def _build_state_payload() -> dict[str, Any]:
    rules = _build_rules_payload()
    counts = {"total": len(rules), "done": 0, "pending": 0, "error": 0}
    for rule in rules:
        state = rule["state"]
        counts[state] = counts.get(state, 0) + 1
    return {
        "prompt": load_system_prompt(),
        "rules": rules,
        "counts": counts,
        "run": _get_run_state(),
        "defaults": {"thinking": _read_default_thinking(), "write_mode": "append"},
    }


def _save_prompt(prompt: str) -> None:
    value = prompt.strip()
    if not value:
        raise ValueError("系统提示词不能为空")
    temp_path = PROMPT_PATH.with_suffix(".txt.tmp")
    temp_path.write_text(value + "\n", encoding="utf-8")
    temp_path.replace(PROMPT_PATH)


def _save_rule_result(rule_id: str, result: dict[str, Any]) -> str:
    if not re.fullmatch(r"RULE-\d+", rule_id):
        raise ValueError("rule_id 格式不合法")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    target_path = OUTPUT_DIR / f"{rule_id}.json"
    temp_path = OUTPUT_DIR / f".{rule_id}.json.tmp"
    temp_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(target_path)
    for prefix in ("Y", "C", "N"):
        stale_path = OUTPUT_DIR / f"{prefix}_{rule_id}.json"
        if stale_path.exists():
            stale_path.unlink()
    return target_path.name


def _run_agent_job(thinking: bool, write_mode: str) -> None:
    started_at = datetime.now().isoformat(timespec="seconds")
    started_counter = time.perf_counter()
    _set_run_state(
        status="running",
        message="规则拆解正在运行",
        started_at=started_at,
        finished_at=None,
        elapsed_seconds=None,
        summary=None,
    )
    try:
        records = read_published_rules(INPUT_PATH)
        load_env_file(ENV_PATH)
        settings = Settings.from_env()
        service = RuleDecompositionService(OpenAICompatibleClient(settings), load_tags(TAG_CATALOG_PATH))
        summary = service.run(records, OUTPUT_DIR, overwrite_existing=write_mode == "overwrite")
        elapsed = time.perf_counter() - started_counter
        _set_run_state(
            status="completed",
            message="规则拆解完成",
            finished_at=datetime.now().isoformat(timespec="seconds"),
            elapsed_seconds=round(elapsed, 2),
            summary={
                "created": summary.created,
                "updated": summary.updated,
                "skipped": summary.skipped,
                "processed": summary.total,
            },
        )
    except Exception as exc:
        _set_run_state(
            status="failed",
            message=str(exc),
            finished_at=datetime.now().isoformat(timespec="seconds"),
            elapsed_seconds=round(time.perf_counter() - started_counter, 2),
            summary=None,
        )


class RuleAgentHandler(BaseHTTPRequestHandler):
    server_version = "RuleAgentUI/0.1"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/state":
            try:
                self._send_json(HTTPStatus.OK, _build_state_payload())
            except Exception as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        self._serve_static(path)

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        if _get_run_state()["status"] == "running":
            self._send_json(HTTPStatus.CONFLICT, {"error": "Agent 运行期间不能修改配置或结果"})
            return
        try:
            payload = self._read_json_body()
            if path == "/api/prompt":
                prompt = payload.get("prompt")
                if not isinstance(prompt, str):
                    raise ValueError("prompt 必须是字符串")
                _save_prompt(prompt)
                self._send_json(HTTPStatus.OK, {"message": "提示词已保存"})
                return
            rule_match = re.fullmatch(r"/api/rules/(?P<rule_id>RULE-\d+)", path)
            if rule_match:
                result = payload.get("result")
                if not isinstance(result, dict):
                    raise ValueError("规则结果必须包含 result 对象")
                filename = _save_rule_result(rule_match.group("rule_id"), result)
                self._send_json(HTTPStatus.OK, {"message": "规则结果已保存", "filename": filename})
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/api/run":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})
            return
        if _get_run_state()["status"] == "running":
            self._send_json(HTTPStatus.CONFLICT, {"error": "Agent 已在运行"})
            return
        try:
            payload = self._read_json_body()
            thinking = payload.get("thinking")
            write_mode = payload.get("write_mode")
            if not isinstance(thinking, bool):
                raise ValueError("thinking 必须是布尔值")
            if write_mode not in {"append", "overwrite"}:
                raise ValueError("write_mode 必须是 append 或 overwrite")
            worker = threading.Thread(
                target=_run_agent_job,
                args=(thinking, write_mode),
                daemon=True,
                name="rule-agent-runner",
            )
            worker.start()
            self._send_json(HTTPStatus.ACCEPTED, {"message": "Agent 已启动"})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _read_json_body(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Content-Length 不合法") from exc
        if length <= 0 or length > 1_000_000:
            raise ValueError("请求正文为空或过大")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求正文必须是 JSON 对象")
        return payload

    def _serve_static(self, path: str) -> None:
        static_files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        }
        static_file = static_files.get(path)
        if not static_file:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        filename, content_type = static_file
        file_path = WEB_ROOT / filename
        if not file_path.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动规则拆解 Agent 本地可视化工作台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), RuleAgentHandler)
    print(f"规则拆解工作台：http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
