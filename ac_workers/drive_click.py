"""C4D toggle: automatic Drive-button mouse clicks at worker start."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
PREF_PATH = REPO / "ac_workers" / "status" / "drive_click.json"


def read_pref() -> dict[str, Any]:
    try:
        data = json.loads(PREF_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {"enabled": True}


def is_enabled() -> bool:
    return bool(read_pref().get("enabled", True))


def write_pref(enabled: bool) -> None:
    PREF_PATH.parent.mkdir(parents=True, exist_ok=True)
    PREF_PATH.write_text(
        json.dumps({"enabled": bool(enabled)}, indent=2),
        encoding="utf-8",
    )


def drive_click_status() -> str:
    return "drive-click {0}  (drive-click on | drive-click off)".format(
        "on" if is_enabled() else "off"
    )


def handle_drive_click_command(arg: str) -> str:
    token = (arg or "").strip().lower()
    if token in ("", "status"):
        return drive_click_status()
    if token in ("on", "1", "allume", "oui"):
        write_pref(True)
        return "drive-click allume: clic auto sur le volant au demarrage"
    if token in ("off", "0", "coupe", "non"):
        write_pref(False)
        return "drive-click eteint: pas de clic auto (clique le volant a la main)"
    return "usage: drive-click | drive-click on | drive-click off"
