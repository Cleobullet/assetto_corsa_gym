"""Let the six workers drive during a reward search without training."""
from __future__ import annotations

import json
import time
from pathlib import Path

STATUS_DIR = Path(__file__).resolve().parents[1] / "ac_workers" / "status"
_CACHE = {"mtime": None, "active": False, "checked": 0.0}


def search_drive_path() -> Path:
    return STATUS_DIR / "reward_search_drive.json"


def set_search_drive(active: bool) -> None:
    path = search_drive_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if active:
        path.write_text(json.dumps({"active": True}), encoding="utf-8")
    else:
        try:
            path.unlink()
        except OSError:
            pass
    _CACHE["checked"] = 0.0


def search_drive_active(now: float | None = None) -> bool:
    stamp = time.monotonic() if now is None else now
    if stamp - float(_CACHE["checked"]) < 0.4:
        return bool(_CACHE["active"])
    _CACHE["checked"] = stamp
    path = search_drive_path()
    active = False
    try:
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            active = bool(isinstance(payload, dict) and payload.get("active"))
    except (OSError, ValueError, TypeError):
        active = False
    _CACHE["active"] = active
    return active
