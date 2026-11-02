"""Collector readiness validation (session-bound, no stale status reuse)."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_HEARTBEAT_MS = 2000.0
MAX_TELEMETRY_MS = 2000.0
WIN_REPLACE_RETRY_ERRNOS = frozenset({5, 32})
MAX_WIN_REPLACE_RETRIES = 12
WIN_REPLACE_RETRY_DELAY_SEC = 0.025


def read_worker_status(worker_id: int, *, repo_root: Path | None = None) -> dict[str, Any]:
    root = repo_root or Path(__file__).resolve().parents[1]
    path = root / "ac_workers" / "status" / ("worker_{0}.json".format(int(worker_id)))
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}


def _replace_errno(exc: BaseException) -> int | None:
    if not isinstance(exc, OSError):
        return None
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        return int(winerror)
    return exc.errno


def _is_retriable_windows_replace_error(exc: BaseException) -> bool:
    if os.name != "nt":
        return False
    code = _replace_errno(exc)
    return code in WIN_REPLACE_RETRY_ERRNOS


def _sanitize_json(obj: Any) -> Any:
    """Convert non-finite floats (NaN/±Infinity) to None.

    Python's json.dumps writes -inf/NaN which are NOT valid JSON. The desktop
    app parses the status with .NET JsonDocument (strict), so a single -inf
    (e.g. neuroevolution_score before any evaluation) makes the whole file
    unreadable and every worker shows "absent".
    """
    if isinstance(obj, float):
        return obj if obj == obj and obj not in (float("inf"), float("-inf")) else None
    if isinstance(obj, dict):
        return {k: _sanitize_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_json(v) for v in obj]
    return obj


def atomic_write_status_json(
    path: Path,
    payload: dict,
    lock: threading.RLock | None = None,
) -> None:
    """Atomically write JSON; optional lock serializes concurrent disk writers only."""
    ctx = lock if lock is not None else nullcontext()
    with ctx:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: Path | None = None
        try:
            fd, tmp_name = tempfile.mkstemp(
                suffix=".tmp",
                prefix="{0}_".format(path.stem),
                dir=str(path.parent),
            )
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps(_sanitize_json(payload), indent=2))
                    handle.flush()
                    try:
                        os.fsync(handle.fileno())
                    except OSError as fsync_exc:
                        if os.name == "nt" and _replace_errno(fsync_exc) == 22:
                            pass
                        else:
                            raise
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise

            replace_retries = 0
            last_exc: OSError | None = None
            for attempt in range(MAX_WIN_REPLACE_RETRIES):
                try:
                    os.replace(str(tmp_path), str(path))
                    tmp_path = None
                    if replace_retries > 0:
                        logger.warning(
                            "status write recovered after %s replace retries path=%s",
                            replace_retries,
                            path,
                        )
                    return
                except OSError as exc:
                    if not _is_retriable_windows_replace_error(exc):
                        raise
                    last_exc = exc
                    replace_retries += 1
                    if attempt + 1 >= MAX_WIN_REPLACE_RETRIES:
                        break
                    time.sleep(WIN_REPLACE_RETRY_DELAY_SEC)
            if last_exc is not None:
                raise last_exc
            raise OSError("status replace failed without exception path={0}".format(path))
        finally:
            if tmp_path is not None:
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except OSError:
                    pass


def safe_write_status_json(
    path: Path,
    payload: dict,
    lock: threading.RLock | None = None,
) -> bool:
    """Best-effort status write. A locked file must not stop the collector."""
    try:
        atomic_write_status_json(path, payload, lock)
        return True
    except OSError as exc:
        logger.warning("status write deferred path=%s: %s", path, exc)
        return False


def observation_complete(snap: dict[str, Any]) -> bool:
    if not bool(snap.get("observation_complete")):
        return False
    obs = snap.get("last_observation") or []
    ts = snap.get("telemetry_snapshot") or snap.get("telemetry_summary") or {}
    # Compact diagnostics deliberately omit the full observation array.
    length = len(obs) or int(snap.get("observation_length") or 0)
    expected = int(snap.get("observation_expected_length") or 8)
    return length == expected and bool(ts)


def validate_snapshot(
    snap: dict[str, Any],
    *,
    session_id: str | None = None,
    collector_pid: int | None = None,
    require_transitions: bool = True,
) -> tuple[bool, list[str], dict[str, Any]]:
    errors: list[str] = []
    details: dict[str, Any] = {
        "sessionMatch": True,
        "collectorPidMatch": True,
        "processAlive": True,
        "diag": True,
        "heartbeatAgeMs": None,
        "telemetryAgeMs": float(snap.get("telemetry_age_ms", 999999.0)),
        "observationComplete": observation_complete(snap),
        "management": bool(snap.get("management_connected")),
        "vjoy": bool(snap.get("vjoy_ready")),
        "transitionServer": bool(
            snap.get("transition_server_connected") or snap.get("transition_server_ok")
        ),
        "transitions": int(snap.get("transition_count", 0)),
        "phase": str(snap.get("phase", "")),
    }

    if session_id:
        match = str(snap.get("session_id", "")) == str(session_id)
        details["sessionMatch"] = match
        if not match:
            errors.append("session_id mismatch (got {0})".format(snap.get("session_id")))

    if collector_pid is not None:
        snap_pid = int(snap.get("collector_pid", -1))
        match = snap_pid == int(collector_pid)
        details["collectorPidMatch"] = match
        if not match:
            errors.append("collector_pid mismatch (got {0}, expected {1})".format(snap_pid, collector_pid))

    hb_mono = snap.get("heartbeat_monotonic")
    if hb_mono is not None:
        hb_age = max(0.0, (time.monotonic() - float(hb_mono)) * 1000.0)
    else:
        hb_age = float(snap.get("heartbeat_age_ms", 999999.0))
    details["heartbeatAgeMs"] = hb_age
    if hb_age > MAX_HEARTBEAT_MS:
        errors.append("heartbeat stale ms={0:.0f}".format(hb_age))

    telem = details["telemetryAgeMs"]
    if telem > MAX_TELEMETRY_MS:
        errors.append("telemetry stale ms={0:.0f}".format(telem))

    if not details["observationComplete"]:
        errors.append("observation incomplete")

    if not details["management"]:
        errors.append("management_connected=false")

    if not details["vjoy"]:
        errors.append("vjoy_ready=false")

    if not details["transitionServer"]:
        errors.append("transition_server not connected")

    tc = details["transitions"]
    if require_transitions and tc <= 0:
        errors.append("transition_count=0")

    phase = str(snap.get("phase", ""))
    details["phase"] = phase
    allowed_phases = ("running",) if require_transitions else ("running", "idle")
    if phase and phase not in allowed_phases:
        errors.append("phase={0} (need running)".format(phase))

    return len(errors) == 0, errors, details


def format_not_ready_line(worker_id: int, details: dict[str, Any]) -> str:
    return (
        "collector worker {0} not ready: processAlive={1} diag={2} sessionMatch={3} "
        "heartbeatAgeMs={4} telemetryAgeMs={5} observationComplete={6} "
        "management={7} vjoy={8} transitionServer={9} transitions={10} phase={11}"
    ).format(
        worker_id,
        details.get("processAlive", True),
        details.get("diag", True),
        details.get("sessionMatch", True),
        details.get("heartbeatAgeMs"),
        details.get("telemetryAgeMs"),
        details.get("observationComplete", False),
        details.get("management", False),
        details.get("vjoy", False),
        details.get("transitionServer", False),
        details.get("transitions", 0),
        details.get("phase", ""),
    )
