from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .web_app import WEB_ROOT, _build_state_payload


def export_snapshot() -> Path:
    payload = _build_state_payload()
    payload["mode"] = "static"
    payload["generated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    payload["run"] = {
        "status": "idle",
        "message": "云端审阅模式",
        "started_at": None,
        "finished_at": None,
        "elapsed_seconds": None,
        "summary": None,
    }

    target_dir = WEB_ROOT / "data"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / "state.json"
    temp_path = target_path.with_suffix(".json.tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(target_path)
    return target_path


def main() -> None:
    target_path = export_snapshot()
    print(f"静态数据快照已生成：{target_path}")


if __name__ == "__main__":
    main()
