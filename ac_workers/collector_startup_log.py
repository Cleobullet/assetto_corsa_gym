"""Collector startup logging that also appends to the redirected stderr log file."""
from __future__ import annotations

import os
import sys
from pathlib import Path

_LOG_PATH: Path | None = None


def configure_startup_log(path: str | None = None) -> None:
    global _LOG_PATH
    raw = path if path is not None else os.environ.get("AC_WORKER_STDERR_LOG", "")
    text = str(raw or "").strip()
    _LOG_PATH = Path(text) if text else None


def startup_log(message: str) -> None:
    if _LOG_PATH is not None:
        try:
            with _LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(message + "\n")
        except OSError:
            pass
    try:
        print(message, file=sys.stderr, flush=True)
    except OSError:
        pass


def normalize_loopback_host(host: str | None) -> str:
    text = str(host or "127.0.0.1").strip()
    if text.lower() == "localhost":
        return "127.0.0.1"
    return text
