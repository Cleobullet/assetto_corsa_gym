"""C4D Control Center — coordinate up to 6 local worker agents (terminal UI)."""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
MAX_WORKERS = 6
TITLE = "C4D Control Center"
LEARNER_WORK_DIR = REPO / "outputs" / "multi_sac_cpu_v3"
LEARNER_STATUS = LEARNER_WORK_DIR / "learner_status.json"
LEARNER_STOP = LEARNER_WORK_DIR / "learner_stop.request"
LEARNER_SAVE = LEARNER_WORK_DIR / "learner_save.request"
TRAINER_STATE = LEARNER_WORK_DIR / "trainer_state.json"
LEARNER_RESUME_CKPT = LEARNER_WORK_DIR / "model" / "checkpoints" / "resume"
LEARNER_PUBLISH_DIR = LEARNER_WORK_DIR / "model" / "publish"
LEARNER_STOP_TIMEOUT_SEC = 30.0
LEARNER_START_LOG = REPO / "ac_workers" / "logs" / "learner_start_last.log"
MANIFEST = REPO / "ac_workers" / "manifest.json"
STATUS_DIR = REPO / "ac_workers" / "status"
SESSION_DECISION_LOG = REPO / "ac_workers" / "logs" / "session_decisions.log"
WAITING_STATUS_REASONS = frozenset(
    {"waiting_current_worker_status", "session synchronization pending"}
)


def _drive_click_enabled() -> bool:
    from ac_workers.drive_click import is_enabled

    return is_enabled()


def _subprocess_no_window() -> dict[str, int]:
    """Hide extra python/powershell consoles spawned by C4D (Windows)."""
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
    if not flags:
        return {}
    return {"creationflags": flags}

ANSI_HOME = "\x1b[H"
ANSI_CLEAR_FROM_CURSOR = "\x1b[J"
ANSI_CLEAR_LINE = "\x1b[2K"
ANSI_HIDE_CURSOR = "\x1b[?25l"
ANSI_SHOW_CURSOR = "\x1b[?25h"

TEST_START_TIMEOUT_SEC = 60.0
REFRESH_INTERVAL_SEC = 0.5
WORKER_START_READY_TIMEOUT_SEC = 60.0
COLLECTOR_FAILED_PHASES = frozenset(
    {
        "stopped",
        "failed",
        "gear_initialization_failed",
        "vjoy_startup_failed",
        "attaching_live_session_failed",
        "attach_failed",
        "waiting_for_drive_failed",
        "diagnostic_failed",
        "management_startup_failed",
        "reset_failed",
    }
)
# Multi-instance CSP contention recovery (scale-test).
# After a worker reaches ready, let it stabilize before launching the next heavy
# AC instance, otherwise the new load can starve/freeze the one that just came up
# (observed: "AC process exited" + telemetry going stale on the prior worker).
# The launcher itself takes ~10s so the real gap between launches is settle + ~10s.
SCALE_SETTLE_SEC = 5.0
HARD_CLEAN_SETTLE_SEC = 1.0
# During startup, if telemetry was once fresh then climbs past this, the AC
# instance has frozen (window/render died) even though its PID is still alive --
# invisible to PID-based liveness checks.
STARTUP_FREEZE_TELEMETRY_AGE_MS = 300000.0
# Bounded auto-restart for transient/contention startup failures. Avoids an
# infinite restart loop while recovering flaky multi-instance launches.
MAX_STARTUP_AUTO_RESTARTS = 1
# Collector phases that are transient under load and worth one auto-retry.
STARTUP_AUTO_RESTART_PHASES = frozenset(
    {"attach_failed", "attaching_live_session_failed", "waiting_for_drive_failed"}
)
# Substrings in a launcher/start failure that indicate a retryable contention
# failure rather than a hard misconfiguration.
STARTUP_RETRYABLE_MARKERS = (
    "drive input not delivered",
    "ac process exited",
    "no telemetry",
    "telemetry timeout",
    "attach_failed",
    "waiting_for_drive_failed",
    "10048",
    "diagnostic port",
    "diag port",
    "already running: diagnostic port",
)
KEY_POLL_SLEEP_SEC = 0.015
# Collector liveness window. The collector heartbeat thread persists a fresh
# status every 0.5s, so a live collector stays well under this even when its
# main loop stalls behind env.step()/network probes under heavy multi-instance
# CPU load. Kept generous (aligned with STARTUP_FREEZE_TELEMETRY_AGE_MS) to
# avoid declaring a starved-but-alive collector heartbeat_stale.
MAX_HEARTBEAT_MS = 15000.0
_DASHBOARD_DIAG_TIMEOUT_SEC = 0.2
_DASHBOARD_DIAG_TTL_SEC = 2.0
_DIAG_PROBE_LOCK = threading.Lock()
_DIAG_PROBE_CACHE: dict[int, dict[str, Any]] = {}
_DIAG_PROBE_INFLIGHT: set[int] = set()


def _diag_cache_peek(worker_id: int) -> tuple[bool, str, dict[str, Any], float]:
    """Non-blocking read of the last diag probe result."""
    with _DIAG_PROBE_LOCK:
        entry = _DIAG_PROBE_CACHE.get(worker_id)
        if not entry or entry.get("ts", 0.0) <= 0.0:
            return False, "diag_pending", {}, 0.0
        age_ms = (time.time() - float(entry["ts"])) * 1000.0
        return (
            bool(entry.get("ok")),
            str(entry.get("reason") or ""),
            dict(entry.get("snap") or {}),
            age_ms,
        )


def _diag_probe_schedule(
    worker_id: int,
    file_status: dict[str, Any] | None,
    *,
    timeout: float = _DASHBOARD_DIAG_TIMEOUT_SEC,
) -> None:
    """Kick off a background diag ping; never blocks the caller."""
    now = time.time()
    with _DIAG_PROBE_LOCK:
        entry = _DIAG_PROBE_CACHE.get(worker_id) or {}
        if worker_id in _DIAG_PROBE_INFLIGHT:
            return
        if entry.get("ts") and (now - float(entry["ts"])) < _DASHBOARD_DIAG_TTL_SEC:
            return
        _DIAG_PROBE_INFLIGHT.add(worker_id)

    status = dict(file_status or {})

    def _run() -> None:
        ok = False
        reason = "diag_unreachable"
        snap: dict[str, Any] = {}
        try:
            from ac_workers.worker_handshake import probe_collector_diag_ping

            json_ok, ping_ok, ping_reason, ping_snap = probe_collector_diag_ping(
                worker_id, timeout=timeout
            )
            snap = dict(ping_snap or {})
            if json_ok and ping_ok:
                ok, reason = True, "ok"
            elif json_ok:
                ok, reason = False, ping_reason or "ping_not_ok"
            else:
                ok, reason = False, ping_reason or "diag_unreachable"
        except Exception as exc:
            reason = str(exc)
        finally:
            with _DIAG_PROBE_LOCK:
                _DIAG_PROBE_INFLIGHT.discard(worker_id)
                _DIAG_PROBE_CACHE[worker_id] = {
                    "ts": time.time(),
                    "ok": ok,
                    "reason": reason,
                    "snap": snap,
                }

    threading.Thread(target=_run, daemon=True, name="diag-probe-{0}".format(worker_id)).start()


def _diag_probe_blocking(
    worker_id: int,
    file_status: dict[str, Any] | None,
    *,
    timeout: float = 2.0,
) -> tuple[bool, str, dict[str, Any], float]:
    """Synchronous diag ping for heavy commands (diag-check, test-health, …)."""
    from ac_workers.worker_handshake import probe_collector_diag_ping

    json_ok, ping_ok, reason, snap = probe_collector_diag_ping(worker_id, timeout=timeout)
    ok = bool(json_ok and ping_ok)
    if not json_ok:
        ok = False
    elif not ping_ok:
        ok = False
        reason = reason or "ping_not_ok"
    else:
        reason = "ok"
    with _DIAG_PROBE_LOCK:
        _DIAG_PROBE_CACHE[worker_id] = {
            "ts": time.time(),
            "ok": ok,
            "reason": reason,
            "snap": dict(snap or {}),
        }
    return ok, reason, dict(snap or {}), 0.0


def _cached_diag_probe(
    worker_id: int,
    status: dict[str, Any],
    *,
    ttl_sec: float = _DASHBOARD_DIAG_TTL_SEC,
    blocking: bool = False,
    timeout: float | None = None,
) -> tuple[bool, str, dict[str, Any], float]:
    """Dashboard path: cache-only + background schedule. Heavy path: blocking ping."""
    if blocking:
        return _diag_probe_blocking(
            worker_id, status, timeout=timeout if timeout is not None else 2.0
        )
    _diag_probe_schedule(worker_id, status, timeout=timeout or _DASHBOARD_DIAG_TIMEOUT_SEC)
    ok, reason, snap, age_ms = _diag_cache_peek(worker_id)
    if age_ms > ttl_sec * 1000.0 and reason != "diag_pending":
        reason = "{0} (stale)".format(reason)
    return ok, reason, snap, age_ms


WORKER_OP_OFFLINE = "offline"
WORKER_OP_STARTING = "starting"
WORKER_OP_WAITING_READY = "waiting_worker_ready"
WORKER_OP_START_TIMEOUT = "worker_start_timeout"
WORKER_OP_RUNNING = "running"
WORKER_OP_STOPPING = "stopping"
WORKER_OP_FAILED = "failed"
WORKER_OP_CANCELLED = "cancelled"
WORKER_OP_STOPPED = "stopped"
STARTUP_OPS = frozenset(
    {WORKER_OP_STARTING, WORKER_OP_WAITING_READY, WORKER_OP_START_TIMEOUT}
)
# Phases of a worker that has attached and is in the live episode/reset cycle.
# A healthy worker spends much of its time in transient reset phases between
# episodes, so the learner-readiness check must accept these in addition to
# "running" — otherwise a momentary "resetting" snapshot makes a working worker
# look not ready and blocks the whole learner start.
LEARNER_LIVE_PHASES = frozenset(
    {
        "idle",
        "running",
        "resetting",
        "reset_ready",
        "reset_waiting_for_session",
        "reset_vjoy_validation",
    }
)
TABLE_COLUMNS_NORMAL: tuple[tuple[str, int], ...] = (
    ("W", 1),
    ("On", 3),
    ("Phase", 14),
    ("AC", 6),
    ("Coll", 6),
    ("vJoy", 4),
    ("Tel", 5),
    ("vJoyAC", 6),
    ("Trans", 7),
    ("T/min", 6),
    ("Eps", 5),
    ("E/min", 5),
    ("Reset/m", 7),
    ("Reward", 7),
    ("VJD", 3),
)
TABLE_COLUMNS_COMPACT: tuple[tuple[str, int], ...] = (
    ("W", 1),
    ("On", 3),
    ("Phase", 10),
    ("AC", 5),
    ("Col", 5),
    ("Joy", 4),
    ("Tel", 5),
    ("vJAC", 4),
    ("Tr", 6),
    ("T/m", 5),
    ("Ep", 4),
    ("E/m", 4),
    ("R/m", 5),
    ("Rw", 6),
    ("VJ", 3),
)
TABLE_COLUMNS_MICRO: tuple[tuple[str, int], ...] = (
    ("W", 1),
    ("On", 3),
    ("Phase", 10),
    ("Tr", 6),
    ("Ep", 4),
    ("T/m", 5),
    ("VJ", 3),
)


def parse_worker_spec(spec: str) -> list[int]:
    """Parse '0', '0,2,4', '0-5' into sorted unique worker ids 0..5."""
    spec = (spec or "").strip()
    if not spec:
        return []
    out: set[int] = set()
    for part in re.split(r"[\s,]+", spec):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            left, right = part.split("-", 1)
            lo, hi = int(left), int(right)
            if lo > hi:
                lo, hi = hi, lo
            for wid in range(lo, hi + 1):
                if 0 <= wid <= 5:
                    out.add(wid)
            continue
        wid = int(part)
        if 0 <= wid <= 5:
            out.add(wid)
    return sorted(out)


def fit_cell(value: Any, width: int, align: str = "left") -> str:
    """Fixed-width cell; truncates with ellipsis, never wraps."""
    if width <= 0:
        return ""
    text = str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")
    if len(text) > width:
        text = (text[: max(0, width - 1)] + "…") if width > 1 else "…"
    if align == "right":
        return text.rjust(width)
    return text.ljust(width)


def terminal_columns(fallback: int = 140) -> int:
    return int(shutil.get_terminal_size(fallback=(fallback, 40)).columns)


def select_table_layout(term_width: int) -> tuple[tuple[tuple[str, int], ...], int]:
    """Pick column set and compute Error column width from terminal width."""
    for cols in (TABLE_COLUMNS_NORMAL, TABLE_COLUMNS_COMPACT, TABLE_COLUMNS_MICRO):
        fixed = sum(w for _, w in cols)
        gaps = len(cols)
        error_w = term_width - fixed - gaps
        if error_w >= 6:
            return cols, error_w
    cols = TABLE_COLUMNS_MICRO
    error_w = max(6, term_width - sum(w for _, w in cols) - len(cols))
    return cols, error_w


def _row_width(widths: list[int]) -> int:
    if not widths:
        return 0
    return sum(widths) + len(widths) - 1


def _format_row(values: list[Any], widths: list[int], aligns: list[str] | None = None) -> str:
    aligns = aligns or ["left"] * len(widths)
    return " ".join(fit_cell(v, w, a) for v, w, a in zip(values, widths, aligns))


def _clip_line(text: str, width: int) -> str:
    return fit_cell(text, width, "left")


def enable_ansi_stdout() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        handle = ctypes.windll.kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint()
        ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


class TerminalRenderer:
    """Single-writer ANSI frame painter (no cls / Clear-Host)."""

    def __init__(self, stream: Any = None) -> None:
        self._stream = stream or sys.stdout
        self._last_frame = ""

    def paint(self, frame: str) -> None:
        if frame == self._last_frame:
            return
        self._stream.write(ANSI_HIDE_CURSOR)
        try:
            payload = ANSI_HOME + frame
            if not payload.endswith("\n"):
                payload += "\n"
            payload += ANSI_CLEAR_FROM_CURSOR
            self._stream.write(payload)
            self._stream.flush()
            self._last_frame = frame
        finally:
            self._stream.write(ANSI_SHOW_CURSOR)
            self._stream.flush()

    def reset_cache(self) -> None:
        self._last_frame = ""


def _worker_row_values(w: WorkerView, cols: tuple[tuple[str, int], ...]) -> list[Any]:
    # Prefer the real error: a benign reset reason (e.g. "episode_done") must not
    # mask a fatal last_error (e.g. "AcquireVJD failed status=1 (FREE)") that
    # explains why a worker stopped.
    err = w.last_error or w.last_reset_reason or "-"
    if cols == TABLE_COLUMNS_NORMAL:
        return [
            w.worker_id,
            "yes" if w.active else "no",
            w.phase or "-",
            w.ac_pid or "-",
            w.collector_pid or "-",
            w.vjoy_device,
            "ok" if w.telemetry_fresh else "stale",
            "yes" if w.vjoy_connected else "no",
            w.transition_count,
            int(w.transitions_per_min),
            w.episode_count,
            int(w.episodes_per_min),
            "{0:.1f}".format(w.resets_per_min),
            "{0:.2f}".format(w.last_reward),
            "ok" if w.canonical_update_vjd_result else "-",
            err,
        ]
    if cols == TABLE_COLUMNS_COMPACT:
        return [
            w.worker_id,
            "yes" if w.active else "no",
            w.phase or "-",
            w.ac_pid or "-",
            w.collector_pid or "-",
            w.vjoy_device,
            "ok" if w.telemetry_fresh else "stale",
            "yes" if w.vjoy_connected else "no",
            w.transition_count,
            int(w.transitions_per_min),
            w.episode_count,
            int(w.episodes_per_min),
            "{0:.1f}".format(w.resets_per_min),
            "{0:.2f}".format(w.last_reward),
            "ok" if w.canonical_update_vjd_result else "-",
            err,
        ]
    return [
        w.worker_id,
        "yes" if w.active else "no",
        w.phase or "-",
        w.transition_count,
        w.episode_count,
        int(w.transitions_per_min),
        "ok" if w.canonical_update_vjd_result else "-",
        err,
    ]


def _row_aligns(n: int) -> list[str]:
    if n <= 1:
        return ["left"]
    aligns = ["left", "left", "left"]
    aligns += ["right"] * max(0, n - 4)
    aligns.append("left")
    return aligns[:n]


def _split_message_lines(message: str, width: int, limit: int = 8) -> list[str]:
    out: list[str] = []
    for para in message.splitlines():
        chunk = para.replace("\t", " ").replace("\r", "")
        while chunk and len(out) < limit:
            out.append(chunk[:width])
            chunk = chunk[width:]
    return out


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _process_command_line(pid: int) -> str:
    try:
        from ac_workers.transition_server_process import get_process_command_line

        return get_process_command_line(pid) or ""
    except Exception:
        return ""


def is_collector_process(pid: int, worker_id: int) -> bool:
    cmd = _process_command_line(pid).lower()
    if "worker_collector" not in cmd:
        return False
    return ("--worker-id {0}".format(worker_id) in cmd) or ("--worker-id={0}".format(worker_id) in cmd)


def is_ac_process(pid: int) -> bool:
    cmd = _process_command_line(pid).lower()
    return ("acs.exe" in cmd) or ("assettocorsa" in cmd and "python" not in cmd)


def startup_ac_living(ac_pid: int) -> bool:
    if ac_pid <= 0 or not pid_alive(ac_pid):
        return False
    cmd = _process_command_line(ac_pid)
    if not cmd:
        return True
    return is_ac_process(ac_pid)


def startup_ac_gone(ac_pid: int) -> bool:
    if ac_pid <= 0:
        return False
    if not pid_alive(ac_pid):
        return True
    cmd = _process_command_line(ac_pid)
    if cmd:
        return not is_ac_process(ac_pid)
    return False


def status_age_ms(status: dict[str, Any], now: float | None = None) -> float:
    for key in ("status_written_at", "collector_started_at"):
        raw = status.get(key)
        if not raw:
            continue
        try:
            text = str(raw).replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() * 1000.0)
        except Exception:
            continue
    hb_sec = status.get("heartbeat_age_sec")
    if hb_sec is not None:
        return float(hb_sec) * 1000.0
    return float(status.get("heartbeat_age_ms", 99999.0))


def learner_heartbeat_age_sec(learner: dict[str, Any]) -> float:
    for key in ("ts", "heartbeat", "started_at"):
        raw = learner.get(key)
        if not raw:
            continue
        try:
            text = str(raw).replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
        except Exception:
            continue
    return float(learner.get("heartbeat_age_sec", 99999.0))


def is_learner_process(pid: int) -> bool:
    cmd = _process_command_line(pid).lower()
    return "multi_sac_trainer" in cmd


@dataclass
class WorkerActivity:
    active: bool
    reason: str = ""
    phase: str = "offline"
    collector_pid: int = 0
    ac_pid: int = 0


def live_diagnostic(status: dict[str, Any]) -> bool:
    """A diagnostic owns its collector even during resets; never restart it."""
    return (bool((status.get("control_smoke_test") or {}).get("enabled"))
            and pid_alive(int(status.get("collector_pid") or 0)))


def historical_worker_ready(status: dict[str, Any]) -> bool:
    if not status:
        return False
    # "idle" is the post-attach ready state: the worker is attached with a loaded
    # policy and held at neutral until the learner starts it. It is as "ready" as
    # "running" for orchestration (scale-test advance, active detection).
    return (
        str(status.get("phase") or "") in ("running", "idle")
        and bool(status.get("initial_session_attached"))
        and bool(status.get("policy_ready"))
    )


def startup_policy_error(status: dict[str, Any]) -> str:
    """An incompatible saved policy cannot recover by waiting for game startup."""
    if status.get("policy_ready"):
        return ""
    for key in ("policy_load_error", "last_error"):
        reason = str(status.get(key) or "")
        if "schema_mismatch_start_new_training" in reason:
            return (reason + " : modele sauvegarde incompatible avec le code actuel. "
                    "Utilisez 'train new <nom>' (ancien entrainement sauvegarde automatiquement), "
                    "puis relancez scale-test.")
    return ""


def startup_status_ready(status: dict[str, Any]) -> bool:
    """Ready for scale-test advance — status fields plus a cached live diag ping."""
    if not historical_worker_ready(status):
        return False
    col_pid = int(status.get("collector_pid") or 0)
    if col_pid > 0 and not pid_alive(col_pid):
        return False
    if float(status.get("heartbeat_age_ms", 0.0)) > MAX_HEARTBEAT_MS:
        return False
    worker_id = int(status.get("worker_id", -1))
    if worker_id < 0:
        return False
    ok, _reason, _snap, _age = _cached_diag_probe(worker_id, status)
    return ok


def worker_profile_missing_message(wid: int, *, repo_root: Path) -> str:
    profile_root = repo_root / "ac_profiles" / "workers" / ("worker_{0}".format(wid))
    if profile_root.is_dir():
        vjoy = profile_root / "cfg" / "controllers" / "savedsetups" / ("Vjoy_W{0}.ini".format(wid))
        if vjoy.is_file():
            return ""
        return (
            "worker {0}: {1} absent — configurez vJoy device {2} puis "
            "scripts/Capture-AI-Worker-Settings.ps1 -WorkerId {0}".format(wid, vjoy.name, wid + 1)
        )
    return (
        "worker {0}: profil absent ({1}) — scripts/Capture-AI-Worker-Settings.ps1 -WorkerId {0}".format(
            wid, profile_root
        )
    )


def worker_documents_released(status: dict[str, Any]) -> bool:
    """True once a worker's collector has attached and AC is running.

    At that point the shared Assetto Corsa Documents config has already been read
    by the collector (gear mapping cached) and AC is live with its own config, so
    the next worker may safely apply its profile and launch without contention.
    """
    if not status:
        return False
    if str(status.get("phase") or "") not in ("running", "idle"):
        return False
    return bool(status.get("initial_session_attached"))


def worker_runtime_busy_message(
    wid: int,
    *,
    status: dict[str, Any],
    manifest_entry: dict[str, Any],
) -> str:
    """Non-empty when a live collector or ready session already owns this worker."""
    try:
        from ac_workers.collector_pid import canonical_collector_pid

        col_pid = canonical_collector_pid(wid, manifest_entry, status)
    except Exception:
        col_pid = int(status.get("collector_pid") or manifest_entry.get("collectorPid") or 0)
    live_collector = col_pid > 0 and pid_alive(col_pid) and is_collector_process(col_pid, wid)
    if historical_worker_ready(status) and live_collector:
        return "worker {0} already running (ready)".format(wid)
    if col_pid > 0 and pid_alive(col_pid) and is_collector_process(col_pid, wid):
        return "worker {0} already running: collector pid={1}".format(wid, col_pid)
    from ac_workers.worker_handshake import probe_collector_diag_ping, tcp_open
    from ac_workers.worker_ports import worker_ports

    ports = worker_ports(wid)
    if not tcp_open("127.0.0.1", ports.diag, timeout=0.05):
        return ""
    json_ok, ping_ok, _reason, _snap = probe_collector_diag_ping(wid, timeout=0.15)
    if json_ok and ping_ok:
        return "worker {0} already running: diag ping ok on port {1}".format(wid, ports.diag)
    if json_ok:
        return "worker {0} diag port {1} in use — stop or abort first".format(wid, ports.diag)
    return ""


def startup_display_phase(status: dict[str, Any], op_state: str) -> str:
    if historical_worker_ready(status):
        return str(status.get("phase") or "idle")
    if op_state == WORKER_OP_START_TIMEOUT:
        return WORKER_OP_START_TIMEOUT
    if not status:
        return "waiting_worker_status"
    phase = str(status.get("phase") or "")
    if phase in (
        "creating_environment",
        "environment_loading",
        "environment_construct_begin",
        "environment_construct_complete",
    ):
        return "environment_loading" if phase in ("creating_environment", "environment_loading") else phase
    if phase:
        return phase
    if op_state == WORKER_OP_WAITING_READY:
        return WORKER_OP_WAITING_READY
    return WORKER_OP_STARTING


def _read_ipc_token(repo_root: Path | None) -> str:
    path = (repo_root or REPO) / "ac_workers" / ".c4d_server" / "session.json"
    if not path.is_file():
        return ""
    try:
        return str(json.loads(path.read_text(encoding="utf-8-sig")).get("token") or "")
    except (OSError, json.JSONDecodeError):
        return ""


def log_session_decision(
    repo_root: Path | None,
    *,
    worker_id: int,
    reason: str,
    manifest_session_id: str = "",
    pre_start_manifest_session_id: str = "",
    operation_worker_session_id: str = "",
    status_session_id: str = "",
    diagnostic_session_id: str = "",
    ac_pid: int = 0,
    collector_pid: int = 0,
) -> None:
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "worker_id": worker_id,
        "reason": reason,
        "control_center_token": _read_ipc_token(repo_root),
        "manifest_session_id": manifest_session_id,
        "pre_start_manifest_session_id": pre_start_manifest_session_id,
        "operation_worker_session_id": operation_worker_session_id,
        "status_session_id": status_session_id,
        "diagnostic_session_id": diagnostic_session_id,
        "ac_pid": ac_pid,
        "collector_pid": collector_pid,
    }
    path = (repo_root or REPO) / "ac_workers" / "logs" / "session_decisions.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def is_worker_really_active(
    worker_id: int,
    *,
    manifest: dict[str, Any],
    status: dict[str, Any],
    manifest_entry: dict[str, Any],
    worker_session_id: str = "",
    op_state: str = WORKER_OP_OFFLINE,
    pre_start_manifest_sid: str = "",
    repo_root: Path | None = None,
    lightweight: bool = False,
) -> WorkerActivity:
    from ac_workers.worker_handshake import tcp_open
    from ac_workers.worker_ports import worker_ports

    ports = worker_ports(worker_id)
    manifest_session_id = str(manifest.get("sessionId") or "")
    status_session_id = str(status.get("session_id") or "")
    operation_worker_session_id = worker_session_id or manifest_session_id
    col_pid = int(status.get("collector_pid") or manifest_entry.get("collectorPid") or 0)
    try:
        from ac_workers.collector_pid import canonical_collector_pid

        col_pid = canonical_collector_pid(worker_id, manifest_entry, status)
    except Exception:
        pass
    ac_pid = int(manifest_entry.get("acPid") or status.get("ac_pid") or 0)
    launcher_pid = int(manifest_entry.get("collectorLauncherPid") or 0)
    json_phase = str(status.get("phase") or "")

    if op_state in STARTUP_OPS and not worker_session_id:
        if not manifest_session_id or manifest_session_id == pre_start_manifest_sid:
            return WorkerActivity(
                False, "waiting_current_worker_status", "starting_waiting_session", col_pid, ac_pid
            )

    status_current = status
    if (
        operation_worker_session_id
        and status_session_id
        and status_session_id != operation_worker_session_id
    ):
        log_session_decision(
            repo_root,
            worker_id=worker_id,
            reason="status_session_mismatch",
            manifest_session_id=manifest_session_id,
            pre_start_manifest_session_id=pre_start_manifest_sid,
            operation_worker_session_id=operation_worker_session_id,
            status_session_id=status_session_id,
            ac_pid=ac_pid,
            collector_pid=col_pid,
        )
        if op_state in STARTUP_OPS:
            return WorkerActivity(
                False, "waiting_current_worker_status", "starting_waiting_session", col_pid, ac_pid
            )
        if (ac_pid > 0 and pid_alive(ac_pid)) or (col_pid > 0 and pid_alive(col_pid)):
            return WorkerActivity(
                False,
                "session synchronization pending",
                "waiting_session_sync",
                col_pid,
                ac_pid,
            )
        return WorkerActivity(False, "status_session_mismatch", "stale", col_pid, ac_pid)
    if op_state in STARTUP_OPS and not status_session_id:
        status_current = {}

    if not operation_worker_session_id and op_state not in STARTUP_OPS:
        return WorkerActivity(False, "no_worker_session", "offline", col_pid, ac_pid)

    if op_state in STARTUP_OPS:
        from ac_workers.collector_pid import INIT_COLLECTOR_PHASES

        json_phase = str(status_current.get("phase") or json_phase)
        if historical_worker_ready(status_current):
            diag_ok, diag_reason, diag_snap, _probe_age = _cached_diag_probe(
                worker_id, status_current
            )
            if diag_ok:
                live_pid = int(diag_snap.get("collector_pid") or col_pid or 0)
                live_phase = str(diag_snap.get("phase") or json_phase or "running")
                return WorkerActivity(True, "ok", live_phase, live_pid, ac_pid)
            return WorkerActivity(
                False,
                "WORKER_NOT_READY:{0}".format(diag_reason),
                json_phase or "stale",
                col_pid,
                ac_pid,
            )
        if json_phase in COLLECTOR_FAILED_PHASES or (
            json_phase == "stopped" and status_current.get("last_error")
        ):
            return WorkerActivity(
                False,
                str(status_current.get("last_error") or json_phase),
                WORKER_OP_FAILED,
                col_pid,
                ac_pid,
            )
        if json_phase in INIT_COLLECTOR_PHASES:
            return WorkerActivity(False, "initializing", json_phase, col_pid, ac_pid)
        if not status_current:
            return WorkerActivity(
                False,
                "waiting_current_worker_status",
                startup_display_phase({}, op_state),
                col_pid,
                ac_pid,
            )
        return WorkerActivity(
            False,
            "waiting_current_worker_status",
            json_phase or op_state,
            col_pid,
            ac_pid,
        )

    from ac_workers.collector_pid import INIT_COLLECTOR_PHASES, GRACE_SEC, collector_evidence

    json_phase = str(status_current.get("phase") or json_phase)
    age_ms = status_age_ms(status_current)
    if age_ms > MAX_HEARTBEAT_MS and json_phase not in INIT_COLLECTOR_PHASES:
        if lightweight or col_pid <= 0 or not pid_alive(col_pid):
            return WorkerActivity(False, "heartbeat_stale", "stale", col_pid, ac_pid)

    alive, col_pid, reason, _ev = collector_evidence(
        worker_id,
        operation_worker_session_id,
        col_pid,
        launcher_pid,
        grace_sec=GRACE_SEC if not lightweight else 0.0,
        deep_scan=not lightweight,
        repo_root=repo_root,
    )
    json_phase = str(status_current.get("phase") or json_phase)
    if json_phase in INIT_COLLECTOR_PHASES and alive:
        return WorkerActivity(False, reason or "initializing", json_phase, col_pid, ac_pid)

    age_ms = status_age_ms(status_current)
    if age_ms > MAX_HEARTBEAT_MS and json_phase not in INIT_COLLECTOR_PHASES:
        return WorkerActivity(False, "heartbeat_stale", "stale", col_pid, ac_pid)

    if not alive:
        return WorkerActivity(False, reason, "offline", col_pid, ac_pid)

    if ac_pid > 0:
        if not pid_alive(ac_pid):
            return WorkerActivity(False, "ac_dead", "stale", col_pid, ac_pid)
        if not lightweight and not is_ac_process(ac_pid):
            return WorkerActivity(False, "ac_pid_recycled", "offline", col_pid, ac_pid)

    diag_ok, diag_reason, diag_snap, _probe_age = _cached_diag_probe(
        worker_id, status_current, blocking=not lightweight, timeout=1.5 if not lightweight else None
    )
    if lightweight and diag_reason == "diag_pending":
        if alive and col_pid > 0 and pid_alive(col_pid):
            phase = json_phase if json_phase and json_phase not in ("stopped",) else "running"
            return WorkerActivity(
                False,
                "WORKER_NOT_READY:diag_pending",
                phase,
                col_pid,
                ac_pid,
            )
    if not diag_ok:
        if alive and col_pid > 0 and pid_alive(col_pid):
            return WorkerActivity(
                False,
                "WORKER_NOT_READY:{0}".format(diag_reason),
                json_phase or "stale",
                col_pid,
                ac_pid,
            )
        return WorkerActivity(
            False,
            diag_reason or "diag_port_closed",
            "stale",
            col_pid,
            ac_pid,
        )
    if diag_snap.get("collector_pid"):
        col_pid = int(diag_snap["collector_pid"])

    phase = json_phase if json_phase and json_phase not in ("stopped",) else "running"
    return WorkerActivity(True, "ok", phase, col_pid, ac_pid)


def is_learner_really_active(learner: dict[str, Any], manifest_session_id: str) -> bool:
    pid = int(learner.get("learner_pid") or 0)
    if pid <= 0 or not pid_alive(pid):
        return False
    if not is_learner_process(pid):
        return False
    if learner_heartbeat_age_sec(learner) > 10.0:
        return False
    sid = str(learner.get("session_id") or "")
    # Learner and worker launches have independent session IDs.
    return bool(learner.get("learner_running", True))


class PerMinuteRate:
    def __init__(self, window_sec: float = 60.0) -> None:
        self.window = window_sec
        self._samples: list[tuple[float, float]] = []

    def add(self, value: float, ts: float | None = None) -> None:
        ts = time.time() if ts is None else ts
        self._samples.append((ts, float(value)))
        cutoff = ts - self.window
        self._samples = [(t, v) for t, v in self._samples if t >= cutoff]

    def per_minute(self) -> float:
        if len(self._samples) < 2:
            return 0.0
        t0, v0 = self._samples[0]
        t1, v1 = self._samples[-1]
        dt = t1 - t0
        if dt < 1.0:
            return 0.0
        return max(0.0, (v1 - v0) / dt * 60.0)


@dataclass
class WorkerView:
    worker_id: int
    active: bool = False
    phase: str = ""
    ac_pid: int = 0
    collector_pid: int = 0
    vjoy_device: int = 0
    diag_port: int = 0
    mgmt_port: int = 0
    telemetry_fresh: bool = False
    gear_engaged: bool = False
    vjoy_connected: bool = False
    transition_count: int = 0
    transitions_per_min: float = 0.0
    episode_count: int = 0
    episodes_per_min: float = 0.0
    avg_episode_sec: float = 0.0
    resets_per_min: float = 0.0
    last_reset_reason: str = ""
    last_reward: float = 0.0
    canonical_update_vjd_result: bool = False
    last_error: str = ""
    policy_paused: bool = False
    diag_tcp_reachable: bool = False
    diag_probe_error: str = ""
    diag_probe_age_ms: float = 999999.0
    diag_ready: bool = False
    process_alive: bool = False


@dataclass
class GlobalView:
    active_workers: int = 0
    diag_ready_workers: int = 0
    process_alive_workers: int = 0
    transitions_per_min: float = 0.0
    replay_size: int = 0
    global_steps: int = 0
    warmup_done: int = 0
    warmup_required: int = 1500
    sac_updates: int = 0
    learner_pid: int = 0
    learner_active: bool = False
    learner_error: str = ""
    learner_saved_steps: int = 0
    learner_saved_replay: int = 0
    session_sec: float = 0.0
    transition_queue: int = 0
    training_session: str = ""


def validate_worker_start(
    worker_ids: list[int],
    active_by_id: dict[int, bool],
    active_detail: dict[int, WorkerActivity] | None = None,
) -> list[str]:
    errors: list[str] = []
    seen_vjoy: dict[int, int] = {}
    seen_diag: dict[int, int] = {}
    active_detail = active_detail or {}
    for wid in worker_ids:
        if wid < 0 or wid > 5:
            errors.append("worker {0} hors plage 0-5".format(wid))
            continue
        if active_by_id.get(wid):
            info = active_detail.get(wid)
            if info and info.active:
                errors.append(
                    "worker {0} already active: AC pid={1}, collector pid={2}".format(
                        wid, info.ac_pid or "-", info.collector_pid or "-"
                    )
                )
            else:
                errors.append("worker {0} deja actif".format(wid))
        from ac_workers.worker_ports import worker_ports

        ports = worker_ports(wid)
        if ports.vjoy in seen_vjoy:
            errors.append(
                "conflit vJoy device {0}: workers {1} et {2}".format(
                    ports.vjoy, seen_vjoy[ports.vjoy], wid
                )
            )
        else:
            seen_vjoy[ports.vjoy] = wid
        if ports.diag in seen_diag:
            errors.append(
                "conflit port diag {0}: workers {1} et {2}".format(
                    ports.diag, seen_diag[ports.diag], wid
                )
            )
        else:
            seen_diag[ports.diag] = wid
    return errors


@dataclass
class _BgResult:
    key: str
    operation: str
    code: int
    output: str
    worker_id: int | None = None
    duration_sec: float = 0.0
    chain: str = ""


class ControlCenter:
    def __init__(self, repo_root: Path | None = None) -> None:
        self.repo = repo_root or REPO
        self.scripts = self.repo / "scripts"
        self.venv_python = self.repo / ".venv-acgym" / "Scripts" / "python.exe"
        self.session_started = time.time()
        self._refresh_interval_sec = REFRESH_INTERVAL_SEC
        self._quit = False
        self._message = ""
        self._worker_op_session_id: dict[int, str] = {}
        self._worker_pre_start_manifest_sid: dict[int, str] = {}
        self._worker_collector_validated: dict[int, bool] = {}
        self._transition_rates: dict[int, PerMinuteRate] = {
            i: PerMinuteRate() for i in range(MAX_WORKERS)
        }
        self._episode_rates: dict[int, PerMinuteRate] = {i: PerMinuteRate() for i in range(MAX_WORKERS)}
        self._reset_rates: dict[int, PerMinuteRate] = {i: PerMinuteRate() for i in range(MAX_WORKERS)}
        self._worker_activity: dict[int, WorkerActivity] = {}
        self._metrics_workers: list[WorkerView] | None = None
        self._metrics_global: GlobalView | None = None
        self._metrics_ts = 0.0
        self._worker_op_state: dict[int, str] = {i: WORKER_OP_OFFLINE for i in range(MAX_WORKERS)}
        self._worker_op_errors: dict[int, str] = {}
        self._learner_op_state = WORKER_OP_OFFLINE
        self._learner_last_start_error = ""
        self._bg_queue: queue.Queue[_BgResult] = queue.Queue()
        self._bg_procs: dict[str, subprocess.Popen[str]] = {}
        self._worker_op_started_at: dict[int, float] = {}
        self._worker_seen_ac_pid: dict[int, int] = {}
        self._worker_ac_was_alive: dict[int, bool] = {}
        self._worker_ac_dead_since: dict[int, float] = {}
        self._worker_running_ac_pid: dict[int, int] = {}
        self._scale_queue: list[int] = []
        self._scale_targets: list[int] = []
        self._scale_boot_paused = False
        # Scale-test pacing + startup recovery state.
        self._scale_last_ready_ts = 0.0
        self._worker_startup_restart_count: dict[int, int] = {}
        self._worker_startup_telemetry_seen: dict[int, bool] = {}
        self._start_op_lock = threading.Lock()
        # vJoy garbage collection (reaper) + privileged janitor coordination.
        self._vjoy_reaper_last_ts = 0.0
        self._vjoy_reaper_thread: threading.Thread | None = None
        self._vjoy_reaper_status: dict[str, Any] = {}
        self._vjoy_prep_thread: threading.Thread | None = None
        self._vjoy_prep_pending = False
        self._vjoy_prep_result: dict[str, Any] = {}
        self._skip_vjoy_index_probe = False
        self._bg_reconcile_ts = 0.0
        self._test_start_collector_pid: dict[int, int] = {}
        # Async mono-worker test bench (test-start/status/log/cancel). Generic per worker_id.
        self._test_jobs: dict[int, dict[str, Any]] = {}
        self._test_jobs_lock = threading.Lock()
        from ac_workers.session_cleanup import cleanup_stale_state

        cleanup_stale_state(self.repo, audit_all=True)

    def _manifest(self) -> dict[str, Any]:
        return _read_json(MANIFEST)

    def _worker_status(self, wid: int) -> dict[str, Any]:
        return _read_json(STATUS_DIR / "worker_{0}.json".format(wid))

    def _manifest_worker(self, wid: int) -> dict[str, Any]:
        for entry in self._manifest().get("workers", []):
            if int(entry.get("workerId", -1)) == wid:
                return entry
        return {}

    def _manifest_session_id(self) -> str:
        return str(self._manifest().get("sessionId") or "")

    def _invalidate_metrics_cache(self) -> None:
        self._metrics_workers = None
        self._metrics_ts = 0.0

    def _sync_worker_op_session(self, wid: int) -> str:
        bound = self._worker_op_session_id.get(wid, "")
        if self._worker_op_state.get(wid) in STARTUP_OPS:
            manifest_sid = self._manifest_session_id()
            pre = self._worker_pre_start_manifest_sid.get(wid, "")
            if not bound and manifest_sid and manifest_sid != pre:
                self._worker_op_session_id[wid] = manifest_sid
                self._invalidate_metrics_cache()
                return manifest_sid
        # The global manifest is replaced by each sequential launch. A ready
        # worker keeps its own collector session, not the last worker's session.
        if self._worker_op_state.get(wid) not in STARTUP_OPS:
            status = self._worker_status(wid)
            current = str(status.get("session_id") or "")
            if current and current != bound and startup_status_ready(status):
                self._worker_op_session_id[wid] = current
                self._invalidate_metrics_cache()
                return current
        return bound or self._manifest_session_id()

    def _reload_session_from_manifest(self) -> None:
        self._invalidate_metrics_cache()

    def _evaluate_worker_activity(self, wid: int, *, lightweight: bool = False) -> WorkerActivity:
        op_sid = self._sync_worker_op_session(wid)
        activity = is_worker_really_active(
            wid,
            manifest=self._manifest(),
            status=self._worker_status(wid),
            manifest_entry=self._manifest_worker(wid),
            worker_session_id=op_sid,
            op_state=self._worker_op_state.get(wid, WORKER_OP_OFFLINE),
            pre_start_manifest_sid=self._worker_pre_start_manifest_sid.get(wid, ""),
            repo_root=self.repo,
            lightweight=lightweight,
        )
        if activity.active or activity.reason in ("ok", "diag_alive", "initializing"):
            if op_sid:
                self._worker_collector_validated[wid] = True
        self._worker_activity[wid] = activity
        return activity

    def worker_active(self, wid: int, *, lightweight: bool = True) -> bool:
        return self._evaluate_worker_activity(wid, lightweight=lightweight).active

    def _worker_runtime_busy(self, wid: int) -> str:
        return worker_runtime_busy_message(
            wid,
            status=self._worker_status(wid),
            manifest_entry=self._manifest_worker(wid),
        )

    def _stale_startup_op_error(self, st: dict[str, Any], op_error: str) -> str:
        """Drop C4D op errors that contradict a healthy idle/running collector status."""
        if not op_error or not st:
            return op_error
        phase = str(st.get("phase") or "")
        if phase not in ("idle", "running"):
            return op_error
        if not historical_worker_ready(st):
            return op_error
        if op_error == "worker_start_timeout" or op_error.startswith("worker_start_timeout"):
            return ""
        tel_age = float(st.get("telemetry_age_ms", 99999.0))
        if op_error.startswith("ac_frozen") and tel_age < STARTUP_FREEZE_TELEMETRY_AGE_MS:
            return ""
        return op_error

    def _attach_diag_probe_fields(self, wid: int, st: dict[str, Any], view: WorkerView) -> WorkerView:
        col_pid = int(st.get("collector_pid") or view.collector_pid or 0)
        view.process_alive = col_pid > 0 and pid_alive(col_pid)
        diag_ok, diag_reason, snap, probe_age = _cached_diag_probe(wid, st)
        # Never tcp_open() the diag port here — empty connects flood the single-threaded
        # diag server and cause ping timeouts / WinError 10053/10054 on real probes.
        view.diag_tcp_reachable = diag_ok or bool(snap) or (
            "port_closed" not in diag_reason and "connection_refused" not in diag_reason
        )
        view.diag_ready = diag_ok
        view.diag_probe_error = "" if diag_ok else diag_reason
        view.diag_probe_age_ms = probe_age
        return view

    def format_diag_check(self, worker_ids: list[int] | None = None) -> str:
        from ac_workers.worker_ports import worker_ports

        ids = worker_ids if worker_ids is not None else list(range(MAX_WORKERS))
        lines = [
            "diag-check (blocking ping probe; status file alone not trusted):",
            "  W  file_pid  pid_alive  port   ping  boot_id      error",
        ]
        any_bad = False
        for wid in ids:
            st = self._worker_status(wid)
            ports = worker_ports(wid)
            file_pid = int(st.get("collector_pid") or 0)
            alive = file_pid > 0 and pid_alive(file_pid)
            ping_ok, reason, snap, _age = _diag_probe_blocking(wid, st, timeout=2.5)
            boot_id = "-"
            err = ""
            if ping_ok:
                boot_id = str(snap.get("boot_id") or "-")[:12]
            else:
                err = str(snap.get("error") or reason or "ping not ok")
            if not ping_ok:
                any_bad = True
            lines.append(
                "  {0}  {1:<8} {2:<9} {3}  {4:<4} {5:<12} {6}".format(
                    wid,
                    file_pid or "-",
                    "yes" if alive else "no",
                    ports.diag,
                    "yes" if ping_ok else "NO",
                    boot_id,
                    err or "-",
                )
            )
            if alive and not ping_ok:
                file_diag = st.get("diag_listener_ready")
                if file_diag is True:
                    lines.append(
                        "       ! status file claims diag_listener_ready=true but live ping failed"
                    )
                req_err = str(st.get("diag_last_request_error") or "").strip()
                if req_err and not err:
                    lines.append("       ! last diag error: {0}".format(req_err[:80]))
        if any_bad:
            lines.append("")
            lines.append(
                "VERDICT: workers process-alive but diag unavailable — "
                "C4D actifs count uses ping-verified diag only; run stop-all then clean-start after code update."
            )
        else:
            lines.append("")
            lines.append("VERDICT: all probed workers diag-ready (tcp yes response yes ping yes boot_id).")
        return "\n".join(lines)

    def format_diag_ping(self, worker_ids: list[int] | None = None) -> str:
        """Raw ping probe — step-by-step TCP debug for each worker."""
        from ac_workers.collector_diag_ping import step_by_step_ping

        ids = worker_ids if worker_ids is not None else list(range(MAX_WORKERS))
        lines = ["diag-ping (raw TCP + JSON ping probe, step-by-step):"]
        ok_count = 0
        for wid in ids:
            from ac_workers.worker_ports import worker_ports

            port = worker_ports(wid).diag
            lines.append("  W{0} port={1}:".format(wid, port))
            r = step_by_step_ping(port, cmd="ping", timeout=2.5)
            tcp = "yes" if r["connect_ok"] else "NO"
            send = "yes" if r["send_ok"] else "NO"
            recv = "yes ({0}B)".format(r["recv_bytes"]) if r["recv_bytes"] > 0 else "NO"
            json_ok = "yes" if r["json_parsed"] else "NO"
            if r["json_parsed"] and r.get("json_response", {}).get("ok"):
                boot = str(r["json_response"].get("boot_id", ""))[:12]
                lines.append("    tcp={0} send={1} recv={2} json={3} ok=true boot_id={4}".format(
                    tcp, send, recv, json_ok, boot
                ))
                ok_count += 1
            else:
                err = r["error"][:60] if r["error"] else "unknown"
                lines.append("    tcp={0} send={1} recv={2} json={3} FAILED: {4}".format(
                    tcp, send, recv, json_ok, err
                ))
                if r["json_parsed"] and r.get("json_response"):
                    resp = r["json_response"]
                    lines.append("    server response: ok={0} error={1}".format(
                        resp.get("ok"), str(resp.get("error", ""))[:50]
                    ))
            lines.append("    elapsed={0:.0f}ms".format(r["elapsed_ms"]))
        lines.append("")
        lines.append("diag-ping: {0}/{1} workers responded ok=true to ping".format(ok_count, len(ids)))
        return "\n".join(lines)

    def _test_pid_sources(self, wid: int, job: dict[str, Any] | None = None) -> dict[str, Any]:
        """Gather PIDs from each source separately (no merging)."""
        since_ts = 0.0
        if job is not None:
            since_ts = float(job.get("launch_ts") or job.get("started_at") or 0.0)
        st_raw = self._worker_status(wid)
        manifest_raw = self._manifest_worker(wid)
        diag_col = 0
        diag_boot = ""
        if job is not None and job.get("last_ping_snap"):
            snap = job["last_ping_snap"]
            diag_col = int(snap.get("collector_pid") or 0)
            diag_boot = str(snap.get("boot_id") or "")
        return {
            "ac_pid_from_launcher": self._read_launch_ac_pid_from_log(wid, since_ts=since_ts),
            "ac_pid_from_manifest": int(manifest_raw.get("acPid") or 0),
            "ac_pid_from_status": int(st_raw.get("ac_pid") or 0),
            "ac_pid_from_seen": int(self._worker_seen_ac_pid.get(wid, 0)),
            "collector_pid_from_manifest": int(manifest_raw.get("collectorPid") or 0),
            "collector_pid_from_status": int(st_raw.get("collector_pid") or 0),
            "collector_pid_from_diag": diag_col,
            "status_session_id": str(st_raw.get("session_id") or ""),
            "manifest_session_id": str(manifest_raw.get("sessionId") or ""),
            "status_boot_id": str(st_raw.get("boot_id") or ""),
            "diag_boot_id": diag_boot,
        }

    def _test_session_scope(
        self, wid: int, job: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
        """Filter status/manifest to the current test-start session."""
        st_raw = self._worker_status(wid)
        manifest_raw = self._manifest_worker(wid)
        notes: list[str] = []
        expected_sid = str(job.get("expected_session_id") or "")
        st = dict(st_raw)
        manifest = dict(manifest_raw)
        status_sid = str(st_raw.get("session_id") or "")
        manifest_sid = str(manifest_raw.get("sessionId") or "")
        if expected_sid:
            if status_sid and status_sid != expected_sid:
                notes.append(
                    "stale_state_ignored status_session={0} expected={1}".format(
                        status_sid[:12], expected_sid[:12]
                    )
                )
                st = {}
            if manifest_sid and manifest_sid != expected_sid:
                notes.append(
                    "stale_state_ignored manifest_session={0} expected={1}".format(
                        manifest_sid[:12], expected_sid[:12]
                    )
                )
                manifest = {}
        expected_boot = str(job.get("expected_boot_id") or "")
        status_boot = str(st_raw.get("boot_id") or "")
        if expected_boot and status_boot and status_boot != expected_boot:
            notes.append(
                "stale_state_ignored status_boot={0} expected={1}".format(
                    status_boot[:12], expected_boot[:12]
                )
            )
            st = {}
        return st, manifest, notes

    def _test_bind_session(self, wid: int, job: dict[str, Any]) -> None:
        """Bind expected_session_id once manifest moves past pre-start sid."""
        manifest_sid = self._manifest_session_id()
        pre_sid = str(self._worker_pre_start_manifest_sid.get(wid) or "")
        if manifest_sid and manifest_sid != pre_sid and not job.get("expected_session_id"):
            job["expected_session_id"] = manifest_sid
            self._worker_op_session_id[wid] = manifest_sid
            self._test_job_log(
                job,
                "session bound run_id={0} sid={1}".format(
                    job.get("startup_run_id", "?"), manifest_sid[:12]
                ),
            )

    def _test_ping_snap_trusted(self, job: dict[str, Any], ping_snap: dict[str, Any]) -> bool:
        if not ping_snap:
            return False
        expected_boot = str(job.get("expected_boot_id") or "")
        diag_boot = str(ping_snap.get("boot_id") or "")
        if expected_boot and diag_boot and diag_boot != expected_boot:
            return False
        return True

    def _test_bind_pids(
        self,
        wid: int,
        job: dict[str, Any],
        sources: dict[str, Any],
        ping_snap: dict[str, Any],
        *,
        ping_trusted: bool,
    ) -> list[str]:
        """Bind sticky AC/collector PIDs for this startup_run; log mismatches."""
        notes: list[str] = []
        if ping_trusted and ping_snap:
            diag_boot = str(ping_snap.get("boot_id") or "")
            if diag_boot and not job.get("expected_boot_id"):
                job["expected_boot_id"] = diag_boot
                job["bound_boot_id"] = diag_boot
                self._test_job_log(job, "boot_id bound {0}".format(diag_boot[:12]))
            diag_col = int(ping_snap.get("collector_pid") or 0)
            if diag_col > 0:
                bound_col = int(job.get("bound_collector_pid") or 0)
                if bound_col <= 0:
                    job["bound_collector_pid"] = diag_col
                    self._test_start_collector_pid.setdefault(wid, diag_col)
                elif diag_col != bound_col:
                    notes.append(
                        "COLLECTOR_PID_MISMATCH old={0} new={1} source=diag".format(
                            bound_col, diag_col
                        )
                    )
        ac_candidates = [
            ("launcher", int(sources.get("ac_pid_from_launcher") or 0)),
            ("manifest", int(sources.get("ac_pid_from_manifest") or 0)),
            ("status", int(sources.get("ac_pid_from_status") or 0)),
        ]
        for source, pid in ac_candidates:
            if pid <= 0:
                continue
            bound_ac = int(job.get("bound_ac_pid") or 0)
            if bound_ac <= 0:
                if pid_alive(pid):
                    job["bound_ac_pid"] = pid
                    self._worker_seen_ac_pid[wid] = pid
            elif pid != bound_ac and pid_alive(pid):
                notes.append(
                    "PID_MISMATCH old_ac_pid={0} new_ac_pid={1} source={2}".format(
                        bound_ac, pid, source
                    )
                )
        if not job.get("bound_collector_pid"):
            for source, key in (
                ("status", "collector_pid_from_status"),
                ("manifest", "collector_pid_from_manifest"),
            ):
                pid = int(sources.get(key) or 0)
                if pid <= 0:
                    continue
                if pid_alive(pid):
                    job["bound_collector_pid"] = pid
                    self._test_start_collector_pid.setdefault(wid, pid)
                    break
        else:
            bound_col = int(job["bound_collector_pid"])
            for source, key in (
                ("status", "collector_pid_from_status"),
                ("manifest", "collector_pid_from_manifest"),
            ):
                pid = int(sources.get(key) or 0)
                if pid > 0 and pid != bound_col and pid_alive(pid):
                    notes.append(
                        "COLLECTOR_PID_MISMATCH old={0} new={1} source={2}".format(
                            bound_col, pid, source
                        )
                    )
        job["last_sources"] = dict(sources)
        return notes

    def _test_final_validation(self, wid: int, job: dict[str, Any]) -> tuple[bool, str]:
        """Validate READY against bound collector/boot_id/session — not stale files."""
        bound_col = int(job.get("bound_collector_pid") or 0)
        bound_boot = str(job.get("expected_boot_id") or job.get("bound_boot_id") or "")
        expected_sid = str(job.get("expected_session_id") or "")
        if bound_col <= 0 or not bound_boot:
            return False, "no_bound_identity"
        if not pid_alive(bound_col):
            return False, "bound_collector_dead"
        st, manifest, _ = self._test_session_scope(wid, job)
        diag_ok, ping_reason, ping_snap, _ = _diag_probe_blocking(
            wid, st, timeout=0.5
        )
        job["last_ping_snap"] = dict(ping_snap or {})
        if not diag_ok or not self._test_ping_snap_trusted(job, ping_snap):
            return False, "STALE_OR_WRONG_SESSION diag={0}".format(ping_reason)
        if int(ping_snap.get("collector_pid") or 0) != bound_col:
            return False, "STALE_OR_WRONG_SESSION collector_pid={0} expected={1}".format(
                ping_snap.get("collector_pid"), bound_col
            )
        if str(ping_snap.get("boot_id") or "") != bound_boot:
            return False, "STALE_OR_WRONG_SESSION boot_id mismatch"
        if int(ping_snap.get("worker_id") or wid) != wid:
            return False, "STALE_OR_WRONG_SESSION worker_id mismatch"
        from ac_workers.worker_ports import worker_ports

        if int(ping_snap.get("diag_port") or 0) not in (0, worker_ports(wid).diag):
            return False, "STALE_OR_WRONG_SESSION diag_port mismatch"
        if expected_sid:
            status_sid = str(st.get("session_id") or manifest.get("sessionId") or "")
            if status_sid and status_sid != expected_sid:
                return False, "STALE_OR_WRONG_SESSION session mismatch"
        return True, "ok"

    def _test_health_dict(
        self, wid: int, *, diag_timeout: float = 2.0, job: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Collect worker health fields for test-health / test-start (no side effects)."""
        from ac_workers.controls_snapshot import load_meta
        from ac_workers.worker_ports import worker_ports

        if job is not None:
            self._test_bind_session(wid, job)
            sources = self._test_pid_sources(wid, job)
            st, manifest, stale_notes = self._test_session_scope(wid, job)
            scoped_sources = dict(sources)
            scoped_sources["ac_pid_from_manifest"] = int(manifest.get("acPid") or 0)
            scoped_sources["ac_pid_from_status"] = int(st.get("ac_pid") or 0)
            scoped_sources["collector_pid_from_manifest"] = int(manifest.get("collectorPid") or 0)
            scoped_sources["collector_pid_from_status"] = int(st.get("collector_pid") or 0)
            for note in stale_notes:
                if note not in job.get("stale_notes_seen", set()):
                    job.setdefault("stale_notes_seen", set()).add(note)
                    self._test_job_log(job, note)
        else:
            op_sid = self._sync_worker_op_session(wid)
            st_raw = self._worker_status(wid)
            status_sid = str(st_raw.get("session_id") or "")
            if op_sid and status_sid and status_sid != op_sid:
                st = {}
            else:
                st = st_raw
            manifest = self._manifest_worker(wid)
            sources = self._test_pid_sources(wid, None)

        ports = worker_ports(wid)
        op = self._worker_op_state.get(wid, WORKER_OP_OFFLINE)
        op_err = str(self._worker_op_errors.get(wid) or st.get("last_error") or "")
        phase = str(st.get("phase") or "")

        col_pid = int(
            (job.get("bound_collector_pid") if job else 0)
            or manifest.get("collectorPid")
            or st.get("collector_pid")
            or 0
        )
        ac_pid = int(
            (job.get("bound_ac_pid") if job else 0)
            or manifest.get("acPid")
            or st.get("ac_pid")
            or self._worker_seen_ac_pid.get(wid, 0)
            or sources.get("ac_pid_from_launcher")
            or 0
        )

        col_alive = col_pid > 0 and pid_alive(col_pid)
        ping_timeout = 0.35 if not col_alive else diag_timeout
        diag_ok, ping_reason, ping_snap, _age = _diag_probe_blocking(
            wid, st, timeout=ping_timeout
        )
        if job is not None:
            job["last_ping_snap"] = dict(ping_snap or {})
            ping_trusted = diag_ok and self._test_ping_snap_trusted(job, ping_snap)
            if not ping_trusted and diag_ok:
                self._test_job_log(
                    job,
                    "stale_state_ignored diag_boot={0} expected={1}".format(
                        str(ping_snap.get("boot_id") or "")[:12],
                        str(job.get("expected_boot_id") or "")[:12],
                    ),
                )
            bind_notes = self._test_bind_pids(
                wid, job, scoped_sources, ping_snap, ping_trusted=ping_trusted
            )
            for note in bind_notes:
                self._test_job_log(job, note)
            ac_pid = int(job.get("bound_ac_pid") or ac_pid)
            col_pid = int(job.get("bound_collector_pid") or col_pid)
            col_alive = col_pid > 0 and pid_alive(col_pid)
            ping_ok = ping_trusted and diag_ok
        else:
            ping_ok = diag_ok

        snap_meta = load_meta(wid)
        vjoy_acquired = bool(st.get("vjoy_device_acquired") or st.get("vjoy_ready"))
        initial_col = int(self._test_start_collector_pid.get(wid, 0))
        blob = (phase + " " + op_err).lower()
        collector_lost = bool(initial_col > 0 and col_pid > 0 and col_pid == initial_col and not col_alive)
        if job and initial_col > 0 and col_pid <= 0 and not col_alive:
            collector_lost = True
        failed = (
            op in (WORKER_OP_FAILED, WORKER_OP_CANCELLED)
            or phase in COLLECTOR_FAILED_PHASES
            or op == WORKER_OP_START_TIMEOUT
            or "ac_frozen" in blob
            or "collector_terminated" in blob
            or collector_lost
        )
        ready = (
            historical_worker_ready(st)
            and ping_ok
            and vjoy_acquired
            and col_alive
        )
        failed_step = self._test_failed_step(st, op, op_err, ping_ok=ping_ok)
        if collector_lost and failed_step in ("waiting_drive", "session_attach", "diag_ping"):
            failed_step = "collector_died"

        if job and job.get("reached_idle_ready") and not ready and failed_step == "diag_ping":
            final_ok, final_reason = self._test_final_validation(wid, job)
            if final_ok:
                ready = True
                failed = False
                ping_ok = True
            else:
                failed = True
                failed_step = "STALE_OR_WRONG_SESSION"
                ping_reason = final_reason

        if job and ready:
            job["reached_idle_ready"] = True

        verdict = "READY" if ready else ("FAILED" if failed else "PENDING")
        return {
            "worker_id": wid,
            "verdict": verdict,
            "ac_pid": ac_pid,
            "ac_alive": ac_pid > 0 and pid_alive(ac_pid),
            "collector_pid": col_pid,
            "collector_pid_initial": initial_col,
            "collector_alive": col_alive,
            "collector_lost": collector_lost,
            "diag_ping_ok": ping_ok,
            "diag_json_ok": bool(ping_snap),
            "diag_ping_reason": ping_reason,
            "boot_id": str(ping_snap.get("boot_id") or st.get("boot_id") or ""),
            "diag_port": ports.diag,
            "phase": phase,
            "op_state": op,
            "telemetry_age_ms": float(st.get("telemetry_age_ms", 99999.0)),
            "vjoy_device": int(st.get("vjoy_device") or ports.vjoy),
            "vjoy_acquired": vjoy_acquired,
            "joy_index_written": snap_meta.get("joy_index_written"),
            "detected_joy_index": snap_meta.get("detected_joy_index"),
            "controls_snapshot_verdict": snap_meta.get("verdict", ""),
            "startup_error": op_err[:300] if op_err else str(st.get("last_error") or "")[:300],
            "failed_step": failed_step,
            "reason": ping_reason if not ping_ok else (op_err or str(st.get("last_error") or "")),
            "initial_session_attached": bool(st.get("initial_session_attached")),
            "policy_ready": bool(st.get("policy_ready")),
            "diag_listener_ready": st.get("diag_listener_ready"),
            "diag_thread_error": str(st.get("diag_thread_error") or ""),
            "pid_sources": sources,
            "startup_run_id": str(job.get("startup_run_id") or "") if job else "",
        }

    def _test_failed_step(
        self, st: dict[str, Any], op: str, op_err: str, *, ping_ok: bool = False
    ) -> str:
        phase = str(st.get("phase") or "")
        blob = (phase + " " + op_err).lower()
        if op in (WORKER_OP_STARTING, WORKER_OP_WAITING_READY) and not st:
            return "waiting_status_file"
        if op == WORKER_OP_START_TIMEOUT:
            return "startup_timeout"
        if phase == "diagnostic_failed" or "diagnostic" in blob:
            return "diag_listener"
        if phase in ("vjoy_startup_failed",) or ("vjoy" in blob and "acquire" in blob):
            return "vjoy_acquire"
        if phase in ("gear_mapping_invalid", "gear_initialization_failed"):
            return "controls_ini_gear"
        if phase in ("attaching_live_session_failed", "attach_failed"):
            return "telemetry_attach"
        if phase in ("waiting_for_drive_failed",):
            return "drive_input"
        if phase in ("management_startup_failed",):
            return "management_tcp"
        if phase in ("creating_environment", "environment_loading", "starting"):
            return "collector_boot"
        if phase == "waiting_for_management":
            return "waiting_management"
        if phase == "waiting_for_drive" or "waiting_for_driv" in phase:
            return "waiting_drive"
        if not bool(st.get("vjoy_device_acquired")) and phase not in ("", "starting"):
            return "vjoy_acquire"
        if not bool(st.get("initial_session_attached")):
            return "session_attach"
        if not ping_ok and int(st.get("collector_pid") or 0) > 0:
            return "diag_ping"
        if op == WORKER_OP_FAILED:
            return phase or "orchestrator_failed"
        return phase or op or "unknown"

    def _verify_job_pids_dead(self, job: dict[str, Any]) -> tuple[bool, str]:
        """Verify only PIDs bound to this test-start run are dead."""
        for key, label in (("bound_ac_pid", "ac_pid"), ("bound_collector_pid", "collector_pid")):
            pid = int(job.get(key) or 0)
            if pid > 0 and pid_alive(pid):
                return False, "{0} {1} still alive".format(label, pid)
        since = float(job.get("launch_ts") or job.get("started_at") or 0)
        launch_ac = self._read_launch_ac_pid_from_log(
            int(job.get("worker_id", 0)), since_ts=since
        )
        if launch_ac > 0 and pid_alive(launch_ac):
            return False, "launch_ac_pid {0} still alive".format(launch_ac)
        return True, "OK"

    def _verify_worker_clean(self, wid: int) -> tuple[bool, str]:
        st = self._worker_status(wid)
        manifest = self._manifest_worker(wid)
        ac_candidates = [
            int(manifest.get("acPid") or 0),
            int(st.get("ac_pid") or 0),
            int(self._worker_seen_ac_pid.get(wid, 0)),
            self._read_launch_ac_pid_from_log(wid),
        ]
        col_candidates = [
            int(manifest.get("collectorPid") or 0),
            int(st.get("collector_pid") or 0),
        ]
        for pid in ac_candidates:
            if pid > 0 and pid_alive(pid):
                return False, "ac_pid {0} still alive".format(pid)
        for pid in col_candidates:
            if pid > 0 and pid_alive(pid):
                return False, "collector_pid {0} still alive".format(pid)
        return True, "OK"

    def _collector_forensics(self, wid: int) -> str:
        st = self._worker_status(wid)
        initial = int(self._test_start_collector_pid.get(wid, 0))
        col_pid = int(st.get("collector_pid") or self._manifest_worker(wid).get("collectorPid") or 0)
        lines = [
            "collector forensics W{0}:".format(wid),
            "  collector_pid_initial={0}".format(initial or "-"),
            "  collector_pid_last={0} alive={1}".format(
                col_pid or "-",
                "yes" if col_pid > 0 and pid_alive(col_pid) else "no",
            ),
            "  last_phase={0}".format(st.get("phase") or "-"),
            "  diag_listener_ready={0}".format(st.get("diag_listener_ready")),
            "  diag_thread_error={0}".format(str(st.get("diag_thread_error") or "")[:120] or "-"),
            "  diag_bind_error={0}".format(str(st.get("diag_bind_error") or "")[:120] or "-"),
        ]
        stderr = self._latest_collector_stderr_tail(wid, limit=2500)
        if stderr.strip():
            lines.append("  collector_stderr_tail:")
            for ln in stderr.strip().splitlines()[-10:]:
                lines.append("    {0}".format(ln[:180]))
        launcher = self._latest_launcher_log_tail(wid, limit=1500)
        if launcher.strip():
            lines.append("  launcher_log_tail:")
            for ln in launcher.strip().splitlines()[-6:]:
                lines.append("    {0}".format(ln[:180]))
        return "\n".join(lines)

    def _test_cleanup_for_job(self, wid: int, job: dict[str, Any] | None) -> tuple[bool, str]:
        """Kill only PIDs bound to the current test-start run; verify with retries."""
        from ac_workers.emergency_stop import _stop_pid

        self._cancel_worker_bg_proc(wid)
        killed: list[str] = []
        ignored: list[str] = []
        pids_run: set[int] = set()
        if job:
            for key in ("bound_ac_pid", "bound_collector_pid"):
                pid = int(job.get(key) or 0)
                if pid > 0:
                    pids_run.add(pid)
            since = float(job.get("launch_ts") or job.get("started_at") or 0)
            launch_ac = self._read_launch_ac_pid_from_log(wid, since_ts=since)
            if launch_ac > 0:
                pids_run.add(launch_ac)
            initial_col = int(self._test_start_collector_pid.get(wid, 0))
            if initial_col > 0:
                pids_run.add(initial_col)
        else:
            return self._test_cleanup_failed_worker(wid)

        for pid in sorted(pids_run):
            if pid > 0:
                _stop_pid(pid)
                killed.append("pid {0}".format(pid))
        time.sleep(0.35)
        for pid in sorted(pids_run):
            if pid > 0 and pid_alive(pid):
                _stop_pid(pid)

        # Stale AC with matching worker title in launcher log — optional hard kill
        stale_ac = self._read_launch_ac_pid_from_log(wid, since_ts=0)
        if stale_ac > 0 and stale_ac not in pids_run and pid_alive(stale_ac):
            ignored.append("unrelated_ac_pid {0}".format(stale_ac))

        try:
            from ac_workers.session_cleanup import cleanup_stale_state

            cleanup_stale_state(self.repo, worker_ids=[wid])
        except Exception:
            pass
        try:
            from ac_workers.vjoy_ledger import clear_owner
            from ac_workers.worker_ports import worker_ports

            clear_owner(worker_ports(wid).vjoy, force=True)
        except Exception:
            pass
        with _DIAG_PROBE_LOCK:
            _DIAG_PROBE_CACHE.pop(wid, None)
        self._worker_seen_ac_pid.pop(wid, None)
        self._worker_op_state[wid] = WORKER_OP_FAILED
        self._invalidate_metrics_cache()

        ok, msg = False, "?"
        for _ in range(10):
            ok, msg = self._verify_job_pids_dead(job)
            if ok:
                break
            for pid in sorted(pids_run):
                if pid > 0 and pid_alive(pid):
                    _stop_pid(pid)
            time.sleep(0.5)
        detail = "killed=[{0}]".format(",".join(killed) if killed else "-")
        if ignored:
            detail += " ignored=[{0}]".format(",".join(ignored))
        if ok:
            return True, "{0} final=OK".format(detail)
        return False, "{0} final={1}".format(detail, msg)

    def _test_cleanup_failed_worker(self, wid: int, job: dict[str, Any] | None = None) -> tuple[bool, str]:
        """Kill AC/collector for a failed test-start so windows do not linger."""
        if job is not None:
            return self._test_cleanup_for_job(wid, job)
        self._cancel_worker_bg_proc(wid)
        self._force_stop_worker_processes(wid)
        try:
            from ac_workers.session_cleanup import cleanup_stale_state

            cleanup_stale_state(self.repo, worker_ids=[wid])
        except Exception:
            pass
        try:
            from ac_workers.vjoy_ledger import clear_owner
            from ac_workers.worker_ports import worker_ports

            clear_owner(worker_ports(wid).vjoy, force=True)
        except Exception:
            pass
        with _DIAG_PROBE_LOCK:
            _DIAG_PROBE_CACHE.pop(wid, None)
        self._worker_seen_ac_pid.pop(wid, None)
        self._worker_op_state[wid] = WORKER_OP_FAILED
        self._invalidate_metrics_cache()
        return self._verify_worker_clean(wid)

    def format_test_health(self, wid: int) -> str:
        h = self._test_health_dict(wid)
        lines = [
            "test-health worker {0} verdict={1}:".format(wid, h["verdict"]),
            "  ac_pid={0} alive={1}".format(h["ac_pid"] or "-", "yes" if h["ac_alive"] else "no"),
            "  collector_pid={0} alive={1}".format(h["collector_pid"] or "-", "yes" if h["collector_alive"] else "no"),
            "  diag_port={0} ping={1} boot_id={2}".format(
                h["diag_port"],
                "yes" if h["diag_ping_ok"] else "NO",
                str(h["boot_id"] or "-")[:12],
            ),
            "  phase={0} op={1}".format(h["phase"] or "-", h["op_state"]),
            "  telemetry_age_ms={0:.0f}".format(h["telemetry_age_ms"]),
            "  vjoy_device={0} acquired={1}".format(h["vjoy_device"], "yes" if h["vjoy_acquired"] else "no"),
            "  controls.ini JOY written={0} detected_index={1} verdict={2}".format(
                h["joy_index_written"] if h["joy_index_written"] is not None else "-",
                h["detected_joy_index"] if h["detected_joy_index"] is not None else "-",
                h["controls_snapshot_verdict"] or "-",
            ),
            "  session_attached={0} policy_ready={1}".format(
                "yes" if h["initial_session_attached"] else "no",
                "yes" if h["policy_ready"] else "no",
            ),
        ]
        if h["startup_error"]:
            lines.append("  startup_error: {0}".format(h["startup_error"][:200]))
        if h["verdict"] != "READY":
            lines.append("  failed_step: {0}".format(h["failed_step"]))
            if h["reason"]:
                lines.append("  reason: {0}".format(str(h["reason"])[:200]))
        return "\n".join(lines)

    def test_stop_worker(self, wid: int) -> str:
        """Stop a single worker (collector + AC) for fast mono-worker debug."""
        if not 0 <= wid < MAX_WORKERS:
            return "usage: test-stop 0"
        if self._is_start_op_active(wid):
            self._cancel_worker_bg_proc(wid)
        op = self._worker_op_state.get(wid, WORKER_OP_OFFLINE)
        stop_key = self._worker_job_key(wid)
        if op != WORKER_OP_STOPPING and self._bg_procs.get(stop_key) is None:
            self._worker_op_state[wid] = WORKER_OP_STOPPING
            self._launch_worker_stop(wid)
            deadline = time.time() + 12.0
            while time.time() < deadline:
                self._poll_background_ops()
                if self._worker_op_state.get(wid) == WORKER_OP_OFFLINE:
                    break
                time.sleep(0.25)
        self._force_stop_worker_processes(wid)
        try:
            from ac_workers.session_cleanup import cleanup_stale_state

            cleanup_stale_state(self.repo, worker_ids=[wid])
        except Exception:
            pass
        try:
            from ac_workers.vjoy_ledger import clear_owner
            from ac_workers.worker_ports import worker_ports

            clear_owner(worker_ports(wid).vjoy, force=True)
        except Exception:
            pass
        self._worker_seen_ac_pid.pop(wid, None)
        with _DIAG_PROBE_LOCK:
            _DIAG_PROBE_CACHE.pop(wid, None)
        self._worker_op_state[wid] = WORKER_OP_STOPPED
        self._worker_op_errors[wid] = "test-stop"
        self._worker_op_started_at.pop(wid, None)
        self._worker_seen_ac_pid.pop(wid, None)
        self._invalidate_metrics_cache()
        return "test-stop worker {0}: stopped (collector + AC killed, vJoy released)".format(wid)

    # ----- async mono-worker test bench (generic for any worker_id 0..5) -----

    def _test_job_log(self, job: dict[str, Any], line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        job.setdefault("log", []).append("[{0}] {1}".format(stamp, line))
        if len(job["log"]) > 80:
            del job["log"][:-80]

    def _set_test_step(self, job: dict[str, Any], step: str, extra: str = "") -> None:
        now = time.time()
        job["step"] = step
        job["elapsed"] = now - job["started_at"]
        line = "step={0} elapsed={1:.1f}s".format(step, job["elapsed"])
        if extra:
            line += " " + extra
        self._test_job_log(job, line)
        # Live dashboard hint: surfaced as the C4D status message, refreshed every loop.
        if job.get("running"):
            self._message = "worker {0} startup: {1}".format(job["worker_id"], line)

    def _test_infer_step(self, wid: int, h: dict[str, Any]) -> str:
        """Map a health snapshot to a coarse startup step name (generic per worker)."""
        if h["verdict"] == "READY":
            return "idle_ready"
        phase = str(h.get("phase") or "").lower()
        if not h["collector_alive"]:
            if not h["ac_alive"]:
                return "launch_ac"
            if "manage" in phase:
                return "wait_window"
            if "driv" in phase:
                return "click_drive"
            if "attach" in phase or "session" in phase:
                return "wait_session_attach"
            return "start_collector"
        # Collector process is alive from here on.
        if not h["diag_ping_ok"]:
            if h.get("diag_listener_ready") is False:
                return "diag_bind"
            return "diag_ping"
        if h["telemetry_age_ms"] >= 5000.0:
            return "telemetry_wait"
        if not h["vjoy_acquired"]:
            return "vjoy_acquire"
        return "idle_ready"

    def _test_force_clean(self, wid: int) -> tuple[bool, str]:
        """Hard cleanup + verify with retries (~5s). Generic per worker_id."""
        self._cancel_worker_bg_proc(wid)
        ok, msg = False, "?"
        for _ in range(10):
            self._force_stop_worker_processes(wid)
            ok, msg = self._verify_worker_clean(wid)
            if ok:
                break
            time.sleep(0.5)
        try:
            from ac_workers.session_cleanup import cleanup_stale_state

            cleanup_stale_state(self.repo, worker_ids=[wid])
        except Exception:
            pass
        try:
            from ac_workers.vjoy_ledger import clear_owner
            from ac_workers.worker_ports import worker_ports

            clear_owner(worker_ports(wid).vjoy, force=True)
        except Exception:
            pass
        with _DIAG_PROBE_LOCK:
            _DIAG_PROBE_CACHE.pop(wid, None)
        self._worker_seen_ac_pid.pop(wid, None)
        return self._verify_worker_clean(wid)

    def test_start_worker(self, wid: int) -> str:
        """Start one worker via the real pipeline in the BACKGROUND; return immediately.

        Generic for any worker_id (0..5). Live steps via `test-status <id>`.
        """
        if not 0 <= wid < MAX_WORKERS:
            return "usage: test-start <id> (0-5)"
        with self._test_jobs_lock:
            existing = self._test_jobs.get(wid)
            if existing and existing.get("running"):
                return (
                    "test-start worker {0}: already running (step={1}); "
                    "use test-status {0} / test-cancel {0}".format(wid, existing.get("step"))
                )
        missing = worker_profile_missing_message(wid, repo_root=self.repo)
        if missing:
            return "FAILED worker {0} step=preflight reason={1}".format(wid, missing)
        cancel = threading.Event()
        startup_run_id = uuid.uuid4().hex[:12]
        job: dict[str, Any] = {
            "worker_id": wid,
            "startup_run_id": startup_run_id,
            "expected_session_id": "",
            "expected_boot_id": "",
            "bound_ac_pid": 0,
            "bound_collector_pid": 0,
            "bound_boot_id": "",
            "reached_idle_ready": False,
            "launch_ts": 0.0,
            "last_ping_snap": {},
            "last_sources": {},
            "stale_notes_seen": set(),
            "running": True,
            "verdict": "PENDING",
            "step": "queued",
            "started_at": time.time(),
            "elapsed": 0.0,
            "last_error": "",
            "cleanup": "",
            "forensics": "",
            "log": [],
            "cancel": cancel,
        }
        with self._test_jobs_lock:
            self._test_jobs[wid] = job
        self._test_job_log(
            job, "test-start worker {0}: job started run_id={1}".format(wid, startup_run_id)
        )
        thread = threading.Thread(
            target=self._test_start_job,
            args=(wid, job, cancel),
            name="test-start-{0}".format(wid),
            daemon=True,
        )
        job["thread"] = thread
        thread.start()
        return (
            "test-start worker {0}: job started (live: test-status {0}, log: test-log {0}, "
            "abort: test-cancel {0})".format(wid)
        )

    def _finish_test_job(
        self, job: dict[str, Any], verdict: str, summary: str
    ) -> None:
        job["verdict"] = verdict
        job["running"] = False
        job["elapsed"] = time.time() - job["started_at"]
        self._test_job_log(job, "DONE verdict={0} {1}".format(verdict, summary))
        self._message = "test-start worker {0}: {1} ({2}) — test-health {0} for details".format(
            job["worker_id"], verdict, summary[:80]
        )
        self._invalidate_metrics_cache()

    def _test_start_job(
        self, wid: int, job: dict[str, Any], cancel: threading.Event
    ) -> None:
        try:
            self._set_test_step(job, "cleanup_old_worker")
            self._test_force_clean(wid)
            if cancel.is_set():
                self._finish_test_job(job, "CANCELLED", "cancelled before launch")
                return
            try:
                from ac_workers.vjoy_pool import validate_pool_mapping

                validate_pool_mapping([wid])
            except Exception as exc:
                job["last_error"] = str(exc)
                self._finish_test_job(job, "FAILED", "vjoy_mapping: {0}".format(exc))
                return
            self._worker_pre_start_manifest_sid[wid] = self._manifest_session_id()
            self._worker_op_session_id.pop(wid, None)
            self._worker_collector_validated.pop(wid, None)
            self._worker_ac_was_alive.pop(wid, None)
            self._worker_ac_dead_since.pop(wid, None)
            self._worker_running_ac_pid.pop(wid, None)
            self._worker_op_state[wid] = WORKER_OP_STARTING
            self._worker_op_started_at[wid] = time.time()
            self._worker_op_errors.pop(wid, None)
            self._worker_seen_ac_pid.pop(wid, None)
            self._test_start_collector_pid.pop(wid, None)
            with _DIAG_PROBE_LOCK:
                _DIAG_PROBE_CACHE.pop(wid, None)
            self._set_test_step(job, "launch_ac")
            job["launch_ts"] = time.time()
            self._launch_worker_start(wid)
            deadline = time.time() + TEST_START_TIMEOUT_SEC
            last_step = ""
            while time.time() < deadline:
                if cancel.is_set():
                    ok, msg = self._test_cleanup_failed_worker(wid, job)
                    job["cleanup"] = msg if ok else "FAILED ({0})".format(msg)
                    self._finish_test_job(job, "CANCELLED", "cancelled (cleanup={0})".format(job["cleanup"]))
                    return
                self._reload_session_from_manifest()
                h = self._test_health_dict(wid, diag_timeout=0.4, job=job)
                step = self._test_infer_step(wid, h)
                extra = (
                    "ac_pid={0} col={1} ping={2} boot={3} run={4}".format(
                        h["ac_pid"] or "-",
                        h["collector_pid"] or "-",
                        "yes" if h["diag_ping_ok"] else "no",
                        str(h.get("boot_id") or "-")[:8],
                        job.get("startup_run_id", "?")[:8],
                    )
                )
                if step != last_step:
                    self._set_test_step(job, step, extra=extra)
                    last_step = step
                else:
                    job["elapsed"] = time.time() - job["started_at"]
                    if job.get("running"):
                        self._message = "worker {0} startup: step={1} elapsed={2:.1f}s {3}".format(
                            wid, step, job["elapsed"], extra
                        )
                if h["verdict"] == "READY":
                    final_ok, final_reason = self._test_final_validation(wid, job)
                    if not final_ok:
                        job["last_error"] = final_reason
                        ok, msg = self._test_cleanup_failed_worker(wid, job)
                        job["cleanup"] = msg if ok else "FAILED ({0})".format(msg)
                        job["forensics"] = self._collector_forensics(wid)
                        self._finish_test_job(
                            job,
                            "FAILED",
                            "step=STALE_OR_WRONG_SESSION reason={0} cleanup={1}".format(
                                final_reason, job["cleanup"]
                            ),
                        )
                        return
                    self._worker_op_state[wid] = WORKER_OP_RUNNING
                    self._finish_test_job(job, "READY", "worker {0} idle_ready".format(wid))
                    return
                if h["verdict"] == "FAILED":
                    job["last_error"] = str(h["reason"] or h["startup_error"] or "?")[:200]
                    ok, msg = self._test_cleanup_failed_worker(wid, job)
                    job["cleanup"] = msg if ok else "FAILED ({0})".format(msg)
                    job["forensics"] = self._collector_forensics(wid)
                    self._finish_test_job(
                        job,
                        "FAILED",
                        "step={0} reason={1} cleanup={2}".format(
                            h["failed_step"], job["last_error"], job["cleanup"]
                        ),
                    )
                    return
                time.sleep(0.5)
            job["last_error"] = "exceeded {0}s".format(int(TEST_START_TIMEOUT_SEC))
            ok, msg = self._test_cleanup_failed_worker(wid, job)
            job["cleanup"] = msg if ok else "FAILED ({0})".format(msg)
            job["forensics"] = self._collector_forensics(wid)
            self._finish_test_job(
                job, "FAILED", "step=timeout {0} cleanup={1}".format(job["last_error"], job["cleanup"])
            )
        except Exception as exc:  # never leave a job stuck "running"
            job["last_error"] = "exception: {0}".format(exc)
            try:
                ok, msg = self._test_cleanup_failed_worker(wid, job)
                job["cleanup"] = msg if ok else "FAILED ({0})".format(msg)
            except Exception:
                pass
            self._finish_test_job(job, "FAILED", job["last_error"])

    def format_test_pids(self, wid: int) -> str:
        if not 0 <= wid < MAX_WORKERS:
            return "usage: test-pids <id> (0-5)"
        with self._test_jobs_lock:
            job = self._test_jobs.get(wid)
        sources = self._test_pid_sources(wid, job)
        lines = ["test-pids worker {0}:".format(wid)]
        if job:
            lines.extend([
                "  startup_run_id={0}".format(job.get("startup_run_id") or "-"),
                "  expected_session_id={0}".format(str(job.get("expected_session_id") or "-")[:16]),
                "  expected_boot_id={0}".format(str(job.get("expected_boot_id") or "-")[:16]),
                "  bound_ac_pid={0} alive={1}".format(
                    job.get("bound_ac_pid") or "-",
                    "yes" if int(job.get("bound_ac_pid") or 0) > 0 and pid_alive(int(job["bound_ac_pid"])) else "no",
                ),
                "  bound_collector_pid={0} alive={1}".format(
                    job.get("bound_collector_pid") or "-",
                    "yes"
                    if int(job.get("bound_collector_pid") or 0) > 0
                    and pid_alive(int(job["bound_collector_pid"]))
                    else "no",
                ),
                "  reached_idle_ready={0}".format("yes" if job.get("reached_idle_ready") else "no"),
            ])
        else:
            lines.append("  (no active test-start job — raw sources only)")
        lines.extend([
            "  ac_pid_from_launcher={0} alive={1}".format(
                sources["ac_pid_from_launcher"] or "-",
                "yes"
                if sources["ac_pid_from_launcher"] > 0 and pid_alive(sources["ac_pid_from_launcher"])
                else "no",
            ),
            "  ac_pid_from_manifest={0}".format(sources["ac_pid_from_manifest"] or "-"),
            "  ac_pid_from_status={0}".format(sources["ac_pid_from_status"] or "-"),
            "  ac_pid_from_seen={0}".format(sources["ac_pid_from_seen"] or "-"),
            "  collector_pid_from_manifest={0}".format(sources["collector_pid_from_manifest"] or "-"),
            "  collector_pid_from_status={0}".format(sources["collector_pid_from_status"] or "-"),
            "  collector_pid_from_diag={0}".format(sources["collector_pid_from_diag"] or "-"),
            "  status_session_id={0}".format(str(sources["status_session_id"] or "-")[:16]),
            "  manifest_session_id={0}".format(str(sources["manifest_session_id"] or "-")[:16]),
            "  status_boot_id={0}".format(str(sources["status_boot_id"] or "-")[:16]),
            "  diag_boot_id={0}".format(str(sources["diag_boot_id"] or "-")[:16]),
        ])
        if job and job.get("last_ping_snap"):
            snap = job["last_ping_snap"]
            lines.append(
                "  diag_response: collector_pid={0} boot_id={1} worker_id={2} diag_port={3}".format(
                    snap.get("collector_pid") or "-",
                    str(snap.get("boot_id") or "-")[:16],
                    snap.get("worker_id") or "-",
                    snap.get("diag_port") or "-",
                )
            )
        # Classify launcher AC as current vs stale when job present
        if job:
            since = float(job.get("launch_ts") or job.get("started_at") or 0)
            current_launch = self._read_launch_ac_pid_from_log(wid, since_ts=since)
            stale_launch = self._read_launch_ac_pid_from_log(wid, since_ts=0)
            if current_launch:
                lines.append("  launcher_ac current={0}".format(current_launch))
            if stale_launch and stale_launch != current_launch:
                lines.append(
                    "  launcher_ac stale={0} alive={1}".format(
                        stale_launch,
                        "yes" if pid_alive(stale_launch) else "no",
                    )
                )
        return "\n".join(lines)

    def format_test_status(self, wid: int) -> str:
        if not 0 <= wid < MAX_WORKERS:
            return "usage: test-status <id> (0-5)"
        with self._test_jobs_lock:
            job = self._test_jobs.get(wid)
        if job is None:
            return "test-status worker {0}: no test-start job (use test-start {0})".format(wid)
        running = bool(job.get("running"))
        if running:
            job["elapsed"] = time.time() - job["started_at"]
        h = self._test_health_dict(wid, diag_timeout=0.4, job=job)
        lines = [
            "test-status worker {0}:".format(wid),
            "  startup_run_id={0}".format(job.get("startup_run_id") or "-"),
            "  running={0} verdict={1}".format("yes" if running else "no", job.get("verdict")),
            "  step={0} elapsed={1:.1f}s".format(job.get("step"), job.get("elapsed", 0.0)),
            "  bound_ac_pid={0} bound_collector_pid={1}".format(
                job.get("bound_ac_pid") or "-", job.get("bound_collector_pid") or "-"
            ),
            "  ac_pid={0} alive={1}".format(h["ac_pid"] or "-", "yes" if h["ac_alive"] else "no"),
            "  collector_pid={0} alive={1}".format(
                h["collector_pid"] or "-", "yes" if h["collector_alive"] else "no"
            ),
            "  diag_port={0} ping={1}".format(h["diag_port"], "yes" if h["diag_ping_ok"] else "no"),
            "  telemetry_age_ms={0:.0f}".format(h["telemetry_age_ms"]),
            "  cleanup={0}".format(job.get("cleanup") or "-"),
            "  last_error={0}".format(str(job.get("last_error") or "")[:200] or "-"),
        ]
        return "\n".join(lines)

    def format_test_log(self, wid: int, tail: int = 25) -> str:
        if not 0 <= wid < MAX_WORKERS:
            return "usage: test-log <id> (0-5)"
        with self._test_jobs_lock:
            job = self._test_jobs.get(wid)
        if job is None:
            return "test-log worker {0}: no test-start job".format(wid)
        log = list(job.get("log") or [])
        lines = ["test-log worker {0} (last {1}):".format(wid, min(tail, len(log)))]
        lines.extend("  {0}".format(ln) for ln in log[-tail:])
        src = job.get("last_sources") or self._test_pid_sources(wid, job)
        lines.append("  pid_sources:")
        for key in (
            "ac_pid_from_launcher",
            "ac_pid_from_manifest",
            "ac_pid_from_status",
            "collector_pid_from_manifest",
            "collector_pid_from_status",
            "collector_pid_from_diag",
        ):
            lines.append("    {0}={1}".format(key, src.get(key) or "-"))
        if job.get("forensics"):
            lines.append(job["forensics"])
        return "\n".join(lines)

    def test_cancel_worker(self, wid: int) -> str:
        if not 0 <= wid < MAX_WORKERS:
            return "usage: test-cancel <id> (0-5)"
        with self._test_jobs_lock:
            job = self._test_jobs.get(wid)
        if job is None or not job.get("running"):
            # No active job: still perform a full cleanup so the worker is left clean.
            ok, msg = self._test_force_clean(wid)
            self._worker_op_state[wid] = WORKER_OP_STOPPED
            self._invalidate_metrics_cache()
            return "test-cancel worker {0}: no active job; cleanup={1}".format(
                wid, msg if ok else "FAILED ({0})".format(msg)
            )
        job["cancel"].set()
        return "test-cancel worker {0}: cancel requested (test-status {0} to confirm)".format(wid)

    def format_workers_readiness(self) -> str:
        from ac_workers.worker_handshake import probe_collector_diag_endpoint

        lines = [
            "workers readiness (diag endpoint required — not just status file / pid):",
            "  W  pid  diag  boot_id      phase            vjoy  identity_ready",
        ]
        for wid in range(MAX_WORKERS):
            st = self._worker_status(wid)
            col_pid = int(st.get("collector_pid") or 0)
            pid_alive_flag = col_pid > 0 and pid_alive(col_pid)
            diag_ok, diag_reason, snap = probe_collector_diag_endpoint(wid, file_status=st)
            boot_id = str(snap.get("boot_id") or st.get("boot_id") or "-")[:12]
            phase = str(snap.get("phase") or st.get("phase") or "-")
            vjoy = snap.get("vjoy_device") or st.get("vjoy_device") or "-"
            vjoy_ok = bool(snap.get("vjoy_device_acquired") or st.get("vjoy_device_acquired"))
            identity_ready = diag_ok and historical_worker_ready(st) and vjoy_ok
            lines.append(
                "  {0}  {1}  {2}  {3}  {4}  {5}  {6}".format(
                    wid,
                    "yes" if pid_alive_flag else "no ",
                    "yes" if diag_ok else "NO ",
                    boot_id,
                    phase[:16],
                    vjoy if vjoy_ok else "{0}?".format(vjoy),
                    "yes" if identity_ready else "NO",
                )
            )
            if not diag_ok and (pid_alive_flag or historical_worker_ready(st)):
                lines.append("       ! WORKER_NOT_READY: {0}".format(diag_reason))
        active = [str(w) for w, on in self.active_workers_map().items() if on]
        lines.append("workers actifs (diag-verified): " + (",".join(active) if active else "aucun"))
        return "\n".join(lines)

    def active_workers_map(self) -> dict[int, bool]:
        return {wid: self._evaluate_worker_activity(wid, lightweight=True).active for wid in range(MAX_WORKERS)}

    def refresh_metrics_cache(self, force: bool = False) -> None:
        now = time.time()
        if (
            not force
            and self._metrics_workers is not None
            and (now - self._metrics_ts) < self._refresh_interval_sec
        ):
            return
        workers = [self._build_worker_view(wid) for wid in range(MAX_WORKERS)]
        self._metrics_workers = workers
        self._metrics_global = self._build_global_view(workers)
        self._metrics_ts = now

    def _invalidate_metrics_cache(self) -> None:
        self._metrics_workers = None
        self._metrics_global = None
        self._metrics_ts = 0.0

    def set_refresh_rate(self, sec: float) -> str:
        sec = max(0.25, min(5.0, float(sec)))
        self._refresh_interval_sec = sec
        return "refresh-rate: {0:.2f}s (dashboard visual; diag probes run in background)".format(sec)

    def _resolve_ac_pid(self, wid: int, activity: WorkerActivity) -> int:
        if activity.ac_pid > 0 and pid_alive(activity.ac_pid):
            return activity.ac_pid
        manifest_pid = int(self._manifest_worker(wid).get("acPid") or 0)
        if manifest_pid > 0 and pid_alive(manifest_pid):
            return manifest_pid
        log_pid = self._read_launch_ac_pid_from_log(wid)
        if log_pid > 0 and pid_alive(log_pid):
            return log_pid
        seen = int(self._worker_seen_ac_pid.get(wid, 0))
        if seen > 0 and pid_alive(seen):
            return seen
        return 0

    def _build_worker_view(self, wid: int) -> WorkerView:
        from ac_workers.worker_ports import worker_ports

        ports = worker_ports(wid)
        op_state = self._worker_op_state.get(wid, WORKER_OP_OFFLINE)
        if op_state in (WORKER_OP_CANCELLED, WORKER_OP_STOPPED):
            ports = worker_ports(wid)
            op_error = self._worker_op_errors.get(wid, "")
            return WorkerView(
                worker_id=wid,
                active=False,
                phase=op_state,
                ac_pid=0,
                collector_pid=0,
                vjoy_device=ports.vjoy,
                diag_port=ports.diag,
                mgmt_port=ports.mgmt,
                telemetry_fresh=False,
                gear_engaged=False,
                transition_count=0,
                episode_count=0,
                avg_episode_sec=0.0,
                last_reset_reason="",
                last_reward=0.0,
                canonical_update_vjd_result=False,
                last_error=op_error,
                policy_paused=False,
            )
        st = self._status_for_op(wid) if op_state in STARTUP_OPS else self._worker_status(wid)
        if op_state in STARTUP_OPS:
            manifest_entry = self._manifest_worker(wid)
            status_collector_pid = int(st.get("collector_pid") or manifest_entry.get("collectorPid") or 0)
            ac_pid = int(manifest_entry.get("acPid") or st.get("ac_pid") or 0)
            if ac_pid <= 0:
                ac_pid = self._read_launch_ac_pid_from_log(wid)
            if ac_pid > 0:
                self._worker_seen_ac_pid[wid] = ac_pid
            self._note_startup_ac_presence(wid, ac_pid)
            if ac_pid > 0 and not pid_alive(ac_pid):
                ac_pid = 0
            if status_collector_pid > 0 and not pid_alive(status_collector_pid):
                if float(st.get("heartbeat_age_ms", 99999.0)) >= MAX_HEARTBEAT_MS:
                    status_collector_pid = 0
            status_phase = str(st.get("phase") or "")
            if status_phase in COLLECTOR_FAILED_PHASES or (
                status_phase == "stopped" and st.get("last_error")
            ):
                active = False
                display_phase = WORKER_OP_FAILED
            else:
                active = historical_worker_ready(st)
                if active:
                    diag_ok, _, _, _ = _diag_cache_peek(wid)
                    if not diag_ok:
                        _diag_probe_schedule(wid, st)
                    active = diag_ok
                display_phase = startup_display_phase(st, op_state)
            op_error = self._worker_op_errors.get(wid, "")
            view = WorkerView(
                worker_id=wid,
                active=active,
                phase=display_phase,
                ac_pid=ac_pid if ac_pid > 0 else int(self._worker_seen_ac_pid.get(wid, 0)),
                collector_pid=status_collector_pid,
                vjoy_device=int(st.get("vjoy_device") or ports.vjoy),
                diag_port=int(st.get("diag_port") or ports.diag),
                mgmt_port=int(st.get("mgmt_port") or ports.mgmt),
                telemetry_fresh=active and float(st.get("telemetry_age_ms", 99999.0)) < 2000.0,
                gear_engaged=active and bool(st.get("initial_gear_engaged")),
                vjoy_connected=active and bool(st.get("vjoy_device_acquired")),
                transition_count=int(st.get("transition_count") or 0) if active else 0,
                episode_count=int(st.get("episode_count") or 0) if active else 0,
                avg_episode_sec=float(st.get("average_episode_duration_seconds") or 0.0),
                last_reset_reason=str(st.get("last_reset_reason") or ""),
                last_reward=float(st.get("last_reward") or 0.0) if active else 0.0,
                canonical_update_vjd_result=active and bool(st.get("canonical_update_vjd_result")),
                last_error=str(st.get("last_error") or op_error or ""),
                policy_paused=active and bool(st.get("policy_paused")),
            )
            return self._attach_diag_probe_fields(wid, st, view)

        from ac_workers.collector_pid import INIT_COLLECTOR_PHASES

        st = self._worker_status(wid)
        activity = self._evaluate_worker_activity(wid, lightweight=True)
        op_sid = self._worker_op_session_id.get(wid, "") or self._manifest_session_id()
        status_sid = str(st.get("session_id") or "")
        if op_sid and status_sid and status_sid != op_sid:
            st = {}
        live_phase = str(st.get("phase") or "")
        status_collector_pid = int(st.get("collector_pid") or 0)
        manifest_entry = self._manifest_worker(wid)
        if activity.collector_pid > 0:
            status_collector_pid = activity.collector_pid
        ev_alive = activity.active or activity.reason in (
            "ok",
            "diag_alive",
            "initializing",
            *WAITING_STATUS_REASONS,
        )
        ev_reason = activity.reason
        op_state = self._worker_op_state.get(wid, WORKER_OP_OFFLINE)
        ac_pid = self._resolve_ac_pid(wid, activity)
        manifest_col_pid = int(manifest_entry.get("collectorPid") or 0)
        if status_collector_pid <= 0 and manifest_col_pid > 0:
            status_collector_pid = manifest_col_pid
        if ac_pid <= 0:
            manifest_ac = int(manifest_entry.get("acPid") or 0)
            if manifest_ac > 0 and pid_alive(manifest_ac):
                ac_pid = manifest_ac
        if ac_pid > 0:
            self._worker_seen_ac_pid[wid] = ac_pid

        if activity.active:
            display_phase = live_phase or WORKER_OP_RUNNING
        elif live_phase in ("attaching_live_session_failed", "attach_failed"):
            display_phase = "attach_failed" if ac_pid > 0 else WORKER_OP_FAILED
            if op_state == WORKER_OP_STARTING:
                self._worker_op_state[wid] = WORKER_OP_FAILED
                if st.get("last_error") and wid not in self._worker_op_errors:
                    self._worker_op_errors[wid] = str(st.get("last_error"))
        elif live_phase in COLLECTOR_FAILED_PHASES:
            display_phase = WORKER_OP_FAILED
            if op_state == WORKER_OP_STARTING:
                self._worker_op_state[wid] = WORKER_OP_FAILED
                if st.get("last_error") and wid not in self._worker_op_errors:
                    self._worker_op_errors[wid] = str(st.get("last_error"))
        elif activity.reason in WAITING_STATUS_REASONS:
            display_phase = activity.phase or "waiting_session_sync"
        elif ev_alive and live_phase in INIT_COLLECTOR_PHASES:
            display_phase = live_phase
        elif (
            not ev_alive
            and ev_reason == "collector_real_process_died"
            and ac_pid > 0
            and self._worker_collector_validated.get(wid)
        ):
            display_phase = "collector_failed"
        elif op_state == WORKER_OP_FAILED:
            display_phase = WORKER_OP_FAILED
        elif op_state == WORKER_OP_STARTING and self._is_start_op_active(wid):
            display_phase = WORKER_OP_STARTING
        elif op_state == WORKER_OP_STOPPING:
            display_phase = WORKER_OP_STOPPING
        else:
            display_phase = activity.phase or WORKER_OP_OFFLINE
        op_error = self._stale_startup_op_error(st, self._worker_op_errors.get(wid, ""))
        view = WorkerView(
            worker_id=wid,
            active=activity.active,
            phase=display_phase,
            ac_pid=ac_pid,
            collector_pid=status_collector_pid if status_collector_pid > 0 else activity.collector_pid,
            vjoy_device=int(st.get("vjoy_device") or ports.vjoy),
            diag_port=int(st.get("diag_port") or ports.diag),
            mgmt_port=int(st.get("mgmt_port") or ports.mgmt),
            telemetry_fresh=activity.active and float(st.get("telemetry_age_ms", 99999.0)) < 2000.0,
            gear_engaged=activity.active and bool(st.get("initial_gear_engaged")),
            vjoy_connected=activity.active and bool(st.get("vjoy_device_acquired")),
            transition_count=int(st.get("transition_count") or 0) if activity.active else 0,
            episode_count=int(st.get("episode_count") or 0) if activity.active else 0,
            avg_episode_sec=float(st.get("average_episode_duration_seconds") or 0.0),
            last_reset_reason=str(st.get("last_reset_reason") or ""),
            last_reward=float(st.get("last_reward") or 0.0) if activity.active else 0.0,
            canonical_update_vjd_result=activity.active and bool(st.get("canonical_update_vjd_result")),
            last_error=str(st.get("last_error") or op_error or activity.reason or ""),
            policy_paused=activity.active and bool(st.get("policy_paused")),
        )
        if activity.active:
            self._transition_rates[wid].add(view.transition_count)
            self._episode_rates[wid].add(view.episode_count)
            self._reset_rates[wid].add(int(st.get("reset_count") or 0))
        view.transitions_per_min = self._transition_rates[wid].per_minute() if activity.active else 0.0
        view.episodes_per_min = self._episode_rates[wid].per_minute() if activity.active else 0.0
        view.resets_per_min = self._reset_rates[wid].per_minute() if activity.active else 0.0
        return self._attach_diag_probe_fields(wid, st, view)

    def collect_worker_view(self, wid: int) -> WorkerView:
        self.refresh_metrics_cache()
        if self._metrics_workers:
            return self._metrics_workers[wid]
        return self._build_worker_view(wid)

    def _build_global_view(self, workers: list[WorkerView]) -> GlobalView:
        learner = _read_json(LEARNER_STATUS)
        learner_live = is_learner_really_active(learner, self._manifest_session_id())
        saved_steps = int(learner.get("global_steps") or learner.get("learner_global_steps") or 0)
        saved_replay = int(learner.get("replay_size") or learner.get("learner_replay_size") or 0)
        trans_queue = 0
        if learner_live or any(w.active for w in workers):
            try:
                from common.transition_server import TransitionClient

                trans_queue = int(TransitionClient(timeout=0.2).stats().get("stats", {}).get("queue_size") or 0)
            except Exception:
                trans_queue = 0
        gv = GlobalView(
            active_workers=sum(1 for w in workers if w.active),
            diag_ready_workers=sum(1 for w in workers if w.diag_ready),
            process_alive_workers=sum(1 for w in workers if w.process_alive),
            transitions_per_min=sum(w.transitions_per_min for w in workers),
            replay_size=saved_replay if learner_live else 0,
            global_steps=saved_steps if learner_live else 0,
            warmup_done=saved_steps if learner_live else 0,
            warmup_required=int(learner.get("learner_warmup_required") or 1500),
            sac_updates=int(learner.get("updates") or learner.get("learner_updates_started") or 0) if learner_live else 0,
            learner_pid=int(learner.get("learner_pid") or 0) if learner_live else 0,
            learner_active=learner_live,
            learner_error=str(learner.get("learner_last_error") or ""),
            learner_saved_steps=saved_steps,
            learner_saved_replay=saved_replay,
            session_sec=time.time() - self.session_started,
            transition_queue=trans_queue,
            training_session=self._training_session_name(),
        )
        return gv

    def collect_global_view(self, workers: list[WorkerView]) -> GlobalView:
        return self._build_global_view(workers)

    def worker_error_detail(self, wid: int) -> str:
        st = self._worker_status(wid)
        view = self.collect_worker_view(wid)
        parts = []
        if wid in self._worker_op_errors:
            parts.append(self._worker_op_errors[wid])
        launcher_tail = self._latest_launcher_log_tail(wid)
        if launcher_tail and launcher_tail not in "\n".join(parts):
            parts.append("launcher log:\n" + launcher_tail)
        stderr_tail = self._latest_collector_stderr_tail(wid)
        if stderr_tail and stderr_tail not in "\n".join(parts):
            col_pid = int(view.collector_pid or 0)
            if col_pid <= 0:
                parts.append("collector stderr (stale/orphan — run emergency-stop):\n" + stderr_tail)
            else:
                parts.append("collector stderr:\n" + stderr_tail)
        if view.last_error and view.last_error not in parts:
            parts.append("last_error: " + view.last_error)
        if view.last_reset_reason:
            parts.append("last_reset_reason: " + view.last_reset_reason)
        if st.get("last_safety_cause"):
            parts.append("last_safety_cause: " + str(st.get("last_safety_cause")))
        return "\n".join(parts) if parts else "(aucune erreur)"

    def worker_detail_text(self, wid: int) -> str:
        st = self._worker_status(wid)
        view = self.collect_worker_view(wid)
        payload = {
            "worker_id": wid,
            "active": view.active,
            "phase": view.phase,
            "ac_pid": view.ac_pid,
            "collector_pid": view.collector_pid,
            "vjoy_device": view.vjoy_device,
            "diag_port": view.diag_port,
            "mgmt_port": view.mgmt_port,
            "transition_count": view.transition_count,
            "episode_count": view.episode_count,
            "last_error": view.last_error,
            "last_reset_reason": view.last_reset_reason,
            "diag_tcp_reachable": view.diag_tcp_reachable,
            "diag_probe_error": view.diag_probe_error,
            "diag_probe_age_ms": view.diag_probe_age_ms,
            "diag_ready": view.diag_ready,
            "process_alive": view.process_alive,
            "status": st,
        }
        return json.dumps(payload, indent=2)

    def build_frame(
        self,
        cmd_buffer: str = "",
        message: str = "",
        *,
        term_width: int | None = None,
        workers: list[WorkerView] | None = None,
        global_view: GlobalView | None = None,
        include_prompt: bool = False,
    ) -> str:
        width = term_width or terminal_columns()
        if workers is None or global_view is None:
            self.refresh_metrics_cache(force=True)
            workers = self._metrics_workers or [self._build_worker_view(w) for w in range(MAX_WORKERS)]
            global_view = self._metrics_global or self._build_global_view(workers)
        cols, error_w = select_table_layout(width)
        col_widths = [w for _, w in cols] + [error_w]
        header_vals = [h for h, _ in cols] + ["Error"]
        aligns = _row_aligns(len(col_widths))

        lines: list[str] = [
            _clip_line(TITLE, width),
            _clip_line(
                "session={0:.0f}s actifs={1}/6 proc={2}/6 trans/min={3:.1f} replay={4} steps={5} warmup={6}/{7} SAC={8}".format(
                    global_view.session_sec,
                    global_view.diag_ready_workers,
                    global_view.process_alive_workers,
                    global_view.transitions_per_min,
                    global_view.replay_size,
                    global_view.global_steps,
                    global_view.warmup_done,
                    global_view.warmup_required,
                    global_view.sac_updates,
                ),
                width,
            ),
            _clip_line(
                "learner pid={0} active={1} queue={2} saved steps={3} replay={4} train={5} drive-click={6}".format(
                    global_view.learner_pid or "-",
                    "yes" if global_view.learner_active else "no",
                    global_view.transition_queue,
                    global_view.learner_saved_steps,
                    global_view.learner_saved_replay,
                    global_view.training_session or "-",
                    "on" if _drive_click_enabled() else "off",
                ),
                width,
            ),
            _format_row(header_vals, col_widths, aligns),
        ]
        for w in workers:
            lines.append(_format_row(_worker_row_values(w, cols), col_widths, aligns))
        job_lines = self._active_test_job_lines()
        if job_lines:
            lines.append("")
            for jl in job_lines:
                lines.append(_clip_line(jl, width))
        if message:
            lines.append("")
            lines.extend(_split_message_lines(message, width))
        if include_prompt:
            lines.append("")
            lines.append(_clip_line("C4D> " + cmd_buffer, width))
        for ln in lines:
            if len(ln) > width:
                raise ValueError("line exceeds terminal width: {0}>{1}".format(len(ln), width))
            if "\n" in ln or "\t" in ln:
                raise ValueError("invalid cell break in line")
        return "\n".join(lines)

    def _active_test_job_lines(self) -> list[str]:
        """One-line live status per active/recent test-start job (non-blocking, cache-only)."""
        out: list[str] = []
        with self._test_jobs_lock:
            jobs = list(self._test_jobs.values())
        for job in sorted(jobs, key=lambda j: j.get("worker_id", 0)):
            wid = job.get("worker_id")
            if job.get("running"):
                elapsed = time.time() - job.get("started_at", time.time())
                out.append(
                    "test-start W{0}: RUNNING step={1} elapsed={2:.1f}s".format(
                        wid, job.get("step"), elapsed
                    )
                )
            elif job.get("verdict") in ("FAILED", "CANCELLED"):
                out.append(
                    "test-start W{0}: {1} step={2} ({3})".format(
                        wid,
                        job.get("verdict"),
                        job.get("step"),
                        str(job.get("last_error") or "")[:60],
                    )
                )
        return out

    def render(self) -> str:
        return self.build_frame(message=self._message)

    def _run_ps_captured(self, script: str, args: list[str]) -> tuple[int, str, str]:
        path = self.scripts / script
        cmd = [
            "powershell",
            "-NoProfile",
            "-WindowStyle",
            "Hidden",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(path),
        ] + args
        proc = subprocess.run(
            cmd,
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_subprocess_no_window(),
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return int(proc.returncode), out.strip(), ""

    def _ps_cmd(self, script: str, args: list[str]) -> list[str]:
        path = self.scripts / script
        return [
            "powershell",
            "-NoProfile",
            "-WindowStyle",
            "Hidden",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(path),
        ] + args

    def _latest_collector_stderr_tail(self, wid: int, limit: int = 8000) -> str:
        log_dir = self.repo / "ac_workers" / "logs"
        if not log_dir.is_dir():
            return ""
        col_pid = int(self._manifest_worker(wid).get("collectorPid") or 0)
        paths = sorted(
            log_dir.glob("worker_{0}_*_stderr.log".format(wid)),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not paths:
            return ""
        if col_pid > 0:
            for path in paths[:8]:
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if "collector pid={0}".format(col_pid) in text or "pid={0}".format(col_pid) in text[:400]:
                    return text[-limit:]
        try:
            return paths[0].read_text(encoding="utf-8", errors="replace")[-limit:]
        except OSError:
            return ""

    def _latest_launcher_log_tail(self, wid: int, limit: int = 4000) -> str:
        log_dir = self.repo / "ac_workers" / "logs"
        if not log_dir.is_dir():
            return ""
        paths = sorted(
            (
                p
                for p in log_dir.glob("worker_{0}_*.log".format(wid))
                if "_stdout" not in p.name and "_stderr" not in p.name
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not paths:
            return ""
        try:
            return paths[0].read_text(encoding="utf-8", errors="replace")[-limit:]
        except OSError:
            return ""

    def _worker_job_key(self, wid: int) -> str:
        return "worker:{0}".format(wid)

    def _is_start_op_active(self, wid: int) -> bool:
        proc = self._bg_procs.get(self._worker_job_key(wid))
        return proc is not None and proc.poll() is None

    def _read_launch_ac_pid_from_log(self, wid: int, *, since_ts: float = 0.0) -> int:
        log_dir = self.repo / "ac_workers" / "logs"
        if not log_dir.is_dir():
            return 0
        paths = sorted(
            (
                p
                for p in log_dir.glob("worker_{0}_*.log".format(wid))
                if "_stdout" not in p.name and "_stderr" not in p.name
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for path in paths[:5]:
            if since_ts > 0:
                try:
                    if path.stat().st_mtime < since_ts - 0.5:
                        continue
                except OSError:
                    continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                if len(text) > 16384:
                    text = text[-16384:]
            except OSError:
                continue
            match = re.search(r"acs pid=(\d+)", text)
            if match:
                return int(match.group(1))
        return 0

    def _launch_ac_pid_for_worker(self, wid: int) -> int:
        ac_pid = int(self._manifest_worker(wid).get("acPid") or 0)
        if ac_pid <= 0:
            ac_pid = self._read_launch_ac_pid_from_log(wid)
        if ac_pid > 0:
            self._worker_seen_ac_pid[wid] = ac_pid
        return ac_pid

    def _terminate_bg_proc(self, key: str) -> str:
        proc = self._bg_procs.pop(key, None)
        if proc is None:
            return ""
        output = ""
        if proc.poll() is None:
            proc.terminate()
            try:
                stdout, stderr = proc.communicate(timeout=3)
                output = ((stdout or "") + (stderr or "")).strip()
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
                output = ((stdout or "") + (stderr or "")).strip()
        else:
            try:
                stdout, stderr = proc.communicate(timeout=0.1)
                output = ((stdout or "") + (stderr or "")).strip()
            except (subprocess.TimeoutExpired, ValueError):
                output = ""
        return output

    def _status_for_op(self, wid: int) -> dict[str, Any]:
        op_sid = self._sync_worker_op_session(wid)
        st = self._worker_status(wid)
        status_sid = str(st.get("session_id") or "")
        if op_sid and status_sid and status_sid != op_sid:
            phase = str(st.get("phase") or "")
            # A failed collector is authoritative even if the launcher session
            # bind is late: otherwise scale-test stays blocked on worker 0.
            if phase in COLLECTOR_FAILED_PHASES:
                return st
            # Late Drive / surviving ready collector: accept live idle/running even
            # when the launcher session id differs. Without this, C4D stays on
            # worker_start_timeout while the AC window is already ready.
            col_pid = int(st.get("collector_pid") or 0)
            if historical_worker_ready(st) and col_pid > 0 and pid_alive(col_pid):
                self._worker_op_session_id[wid] = status_sid
                self._invalidate_metrics_cache()
                return st
            return {}
        return st

    def _log_worker_event(self, wid: int, event: str, detail: str) -> None:
        """Debug (crash-forensic): append a worker lifecycle event to
        ac_workers/logs/worker_events.log so the user can see EXACTLY why a
        worker was closed / became absent. Never raises."""
        try:
            import json

            st = self._worker_status(wid) or {}
            rec = {
                "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "worker": int(wid),
                "event": event,
                "detail": str(detail)[:500],
                "phase": str(st.get("phase") or ""),
                "telemetry_age_ms": st.get("telemetry_age_ms"),
                "last_error": str(st.get("last_error") or "")[:300],
                "vjoy_device_acquired": st.get("vjoy_device_acquired"),
                "initial_session_attached": st.get("initial_session_attached"),
            }
            path = self.repo / "ac_workers" / "logs" / "worker_events.log"
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _mark_status_failure(self, wid: int, err: str) -> None:
        self._log_worker_event(wid, "failed", err)
        self._cancel_worker_bg_proc(wid)
        self._worker_op_state[wid] = WORKER_OP_FAILED
        self._worker_op_errors[wid] = err
        self._message = "worker {0} failed: {1}".format(wid, err)
        self._worker_op_started_at.pop(wid, None)

    def _is_retryable_startup_failure(self, wid: int, reason: str, output: str) -> bool:
        """True for transient/contention startup failures worth one auto-retry."""
        blob = (str(reason) + " " + str(output)).lower()
        if any(marker in blob for marker in STARTUP_RETRYABLE_MARKERS):
            return True
        phase = str(self._status_for_op(wid).get("phase") or "")
        return phase in STARTUP_AUTO_RESTART_PHASES

    def _attempt_startup_recovery(self, wid: int, reason: str) -> bool:
        """Recover a worker that hit a contention failure during a scale-test.

        Tears down any half-started processes and requeues the worker at the end
        of the scale queue so the next healthy worker proceeds first and this one
        retries after the system settles. Returns True if a retry was scheduled,
        False when the retry budget is exhausted (caller marks it failed).
        """
        if live_diagnostic(self._worker_status(wid)):
            return False
        if wid not in self._scale_targets:
            return False
        count = self._worker_startup_restart_count.get(wid, 0)
        if count >= MAX_STARTUP_AUTO_RESTARTS:
            return False
        self._worker_startup_restart_count[wid] = count + 1
        self._worker_startup_telemetry_seen.pop(wid, None)
        self._worker_op_started_at.pop(wid, None)
        self._worker_op_errors.pop(wid, None)
        self._worker_seen_ac_pid.pop(wid, None)
        self._cancel_worker_bg_proc(wid)
        try:
            self._force_stop_worker_processes(wid)
        except Exception:
            pass
        try:
            from ac_workers.session_cleanup import cleanup_stale_state

            cleanup_stale_state(self.repo, worker_ids=[wid])
        except Exception:
            pass
        self._worker_op_state[wid] = WORKER_OP_OFFLINE
        if wid not in self._scale_queue:
            self._scale_queue.append(wid)
        self._message = "worker {0} auto-retry {1}/{2} ({3})".format(
            wid, count + 1, MAX_STARTUP_AUTO_RESTARTS, reason
        )
        self._log_worker_event(
            wid,
            "auto-retry",
            "{0} ({1}/{2})".format(reason, count + 1, MAX_STARTUP_AUTO_RESTARTS),
        )
        self.refresh_metrics_cache(force=True)
        return True

    def _count_live_vjoy_workers(self, exclude: int | None = None) -> int:
        """Workers with a live AC session that owns vJoy (training or idle-ready)."""
        live_phases = LEARNER_LIVE_PHASES | frozenset({"waiting_for_management"})
        count = 0
        for wid in range(MAX_WORKERS):
            if exclude is not None and wid == exclude:
                continue
            if not self.worker_active(wid):
                continue
            st = self._worker_status(wid)
            phase = str(st.get("phase") or "")
            if phase not in live_phases:
                continue
            if not bool(st.get("initial_session_attached")):
                continue
            if not bool(st.get("vjoy_device_acquired") or st.get("vjoy_ready")):
                continue
            count += 1
        return count

    def _maybe_request_vjoy_reset_after_startup_failure(self, wid: int, reason: str) -> None:
        """Global vJoy janitor reset only when no sibling worker still owns a device."""
        blob = str(reason)
        if "AcquireVJD" not in blob and "vJoy" not in blob and "vjoy" not in blob:
            return
        self._force_stop_worker_processes(wid)
        if self._count_live_vjoy_workers(exclude=wid) > 0:
            return
        try:
            from ac_workers.vjoy_janitor import janitor_alive, request_vjoy_reset

            if janitor_alive():
                request_vjoy_reset(list(range(1, MAX_WORKERS + 1)))
        except Exception:
            pass

    def _pause_scale_boot(self) -> str:
        """Stop launching further scale-test workers (e.g. while learner trains)."""
        if not self._scale_queue and not self._scale_startup_busy():
            return ""
        queued = ",".join(str(w) for w in self._scale_queue) if self._scale_queue else ""
        self._scale_queue = []
        self._scale_boot_paused = True
        if queued:
            return "scale boot paused (was queued: {0})".format(queued)
        return "scale boot paused"

    def _finalize_start_failure(self, wid: int, reason: str, output: str = "") -> None:
        self._worker_op_state[wid] = WORKER_OP_FAILED
        detail = reason
        if output.strip():
            detail = reason + "\n" + output
        self._worker_op_errors[wid] = detail
        self._message = reason
        self._log_worker_event(wid, "start_failed", detail)
        self._worker_op_started_at.pop(wid, None)
        self._worker_seen_ac_pid.pop(wid, None)
        self._maybe_request_vjoy_reset_after_startup_failure(wid, detail)
        from ac_workers.session_cleanup import cleanup_stale_state

        cleanup_stale_state(self.repo, worker_ids=[wid])

    def _resolve_startup_ac_pid(self, wid: int) -> int:
        ac_pid = int(self._manifest_worker(wid).get("acPid") or 0)
        if ac_pid <= 0:
            ac_pid = int(self._worker_seen_ac_pid.get(wid, 0))
        if ac_pid <= 0:
            ac_pid = self._read_launch_ac_pid_from_log(wid)
        if ac_pid > 0:
            self._worker_seen_ac_pid[wid] = ac_pid
        return ac_pid

    def _note_startup_ac_presence(self, wid: int, ac_pid: int | None = None) -> None:
        pid = int(ac_pid if ac_pid is not None else self._resolve_startup_ac_pid(wid))
        if startup_ac_living(pid):
            self._worker_ac_was_alive[wid] = True
            self._worker_ac_dead_since.pop(wid, None)

    def _cancel_startup_ac_closed(self, wid: int) -> None:
        self._cancel_worker_bg_proc(wid)
        col_pid = int(self._manifest_worker(wid).get("collectorPid") or 0)
        st = self._status_for_op(wid)
        if col_pid <= 0:
            col_pid = int(st.get("collector_pid") or 0)
        if col_pid > 0 and pid_alive(col_pid):
            from ac_workers.emergency_stop import _stop_pid

            _stop_pid(col_pid)
        self._worker_op_state[wid] = WORKER_OP_CANCELLED
        err = (
            "worker {0} start cancelled: AC closed\n"
            "collector_terminated_by_orchestrator:ac_closed"
        ).format(wid)
        self._worker_op_errors[wid] = err
        self._message = "worker {0} start cancelled: AC closed".format(wid)
        self._worker_op_started_at.pop(wid, None)
        self._worker_seen_ac_pid.pop(wid, None)
        self._worker_ac_was_alive.pop(wid, None)
        self._worker_ac_dead_since.pop(wid, None)

    def _reconcile_ac_closed_during_startup(self, wid: int) -> bool:
        from ac_workers.worker_handshake import tcp_open
        from ac_workers.worker_ports import worker_ports

        ac_pid = self._resolve_startup_ac_pid(wid)
        if startup_ac_living(ac_pid):
            self._worker_ac_was_alive[wid] = True
            self._worker_ac_dead_since.pop(wid, None)
            return False
        if not self._worker_ac_was_alive.get(wid) or ac_pid <= 0:
            return False
        if not startup_ac_gone(ac_pid):
            self._worker_ac_dead_since.pop(wid, None)
            return False
        mgmt_open = tcp_open("127.0.0.1", worker_ports(wid).mgmt, timeout=0.15)
        now = time.time()
        if not mgmt_open:
            self._cancel_startup_ac_closed(wid)
            return True
        dead_since = self._worker_ac_dead_since.get(wid)
        if dead_since is None:
            self._worker_ac_dead_since[wid] = now
            return False
        if now - dead_since >= 2.0:
            self._cancel_startup_ac_closed(wid)
            return True
        return False

    def _reconcile_starting_ops(self) -> bool:
        changed = False
        now = time.time()
        for wid in range(MAX_WORKERS):
            op = self._worker_op_state.get(wid, WORKER_OP_OFFLINE)
            if op not in STARTUP_OPS:
                continue
            if live_diagnostic(self._worker_status(wid)):
                continue
            policy_error = startup_policy_error(self._status_for_op(wid))
            if policy_error:
                self._scale_queue.clear()
                self._scale_boot_paused = True
                self._mark_status_failure(wid, policy_error)
                changed = True
                continue
            if self._reconcile_ac_closed_during_startup(wid):
                changed = True
                continue
            if op == WORKER_OP_STARTING and self._is_start_op_active(wid):
                st_early = self._status_for_op(wid)
                fail_phase = str(st_early.get("phase") or "")
                if fail_phase in COLLECTOR_FAILED_PHASES or (
                    fail_phase == "stopped" and st_early.get("last_error")
                ):
                    self._cancel_worker_bg_proc(wid)
                    self._mark_status_failure(
                        wid, str(st_early.get("last_error") or fail_phase)
                    )
                    changed = True
                continue
            st = self._status_for_op(wid)
            # Drive can exceed the C4D start timeout while the collector still
            # reaches idle. Recover so learner start is not blocked forever.
            if startup_status_ready(st) or (
                op == WORKER_OP_START_TIMEOUT and historical_worker_ready(st)
            ):
                self._worker_op_state[wid] = WORKER_OP_OFFLINE
                self._worker_op_errors.pop(wid, None)
                self._worker_op_started_at.pop(wid, None)
                self._worker_startup_telemetry_seen.pop(wid, None)
                self._scale_last_ready_ts = now
                self._message = "worker {0} ready".format(wid)
                self._log_worker_event(wid, "ready", "")
                changed = True
                continue
            # Frozen-AC watchdog: once telemetry has been seen fresh during
            # startup, a sustained stale age means the AC instance has frozen
            # (render/window died) under multi-instance load even though its PID
            # is still alive. Auto-retry once, then fail.
            tel_age = float(st.get("telemetry_age_ms") or 1e9) if st else 1e9
            if tel_age >= 999000.0:
                # Le collecteur renvoie 999999.0 quand la télémétrie n'est pas
                # encore arrivée (ou a été réinitialisée lors d'un reset
                # post-attach) — c'est « inconnu », PAS « gelé ». Ne pas
                # déclencher le freeze sur ce sentinel (sinon chaque reset tue
                # le worker ~26 s après le lancement, malgré le seuil de 5 min).
                pass
            elif tel_age < STARTUP_FREEZE_TELEMETRY_AGE_MS:
                self._worker_startup_telemetry_seen[wid] = True
            elif self._worker_startup_telemetry_seen.get(wid):
                if self._attempt_startup_recovery(wid, "ac_frozen_telemetry_stale"):
                    changed = True
                    continue
                self._mark_status_failure(
                    wid,
                    "ac_frozen_during_startup (telemetry stale {0:.0f}ms)".format(tel_age),
                )
                changed = True
                continue
            status_phase = str(st.get("phase") or "")
            if status_phase in COLLECTOR_FAILED_PHASES or (
                status_phase == "stopped" and st.get("last_error")
            ):
                if status_phase in STARTUP_AUTO_RESTART_PHASES and self._attempt_startup_recovery(
                    wid, status_phase
                ):
                    changed = True
                    continue
                self._mark_status_failure(wid, str(st.get("last_error") or status_phase))
                changed = True
                continue
            started_at = self._worker_op_started_at.get(wid)
            if op == WORKER_OP_START_TIMEOUT:
                # Chargement trop long (> 1 min) : on ferme le worker et on passe
                # au suivant. Le worker n'est pas « ready » ici (le cas ready est
                # déjà traité plus haut), donc pas de risque de tuer un worker sain.
                self._log_worker_event(wid, "timeout_skip", "worker_start_timeout")
                self._cancel_worker_bg_proc(wid)
                try:
                    self._force_stop_worker_processes(wid)
                except Exception:
                    pass
                self._worker_op_state[wid] = WORKER_OP_OFFLINE
                self._worker_op_errors.pop(wid, None)
                self._worker_op_started_at.pop(wid, None)
                changed = True
                continue
            if started_at is not None and now - started_at > WORKER_START_READY_TIMEOUT_SEC:
                self._worker_op_state[wid] = WORKER_OP_START_TIMEOUT
                self._worker_op_errors[wid] = "worker_start_timeout"
                self._message = "worker {0} start timeout (60s)".format(wid)
                changed = True
                continue
        return changed

    def _handle_worker_window_closed(self, wid: int) -> None:
        """Operator closed a running worker's AC window: stop it and report."""
        self._cancel_worker_bg_proc(wid)
        col_pid = int(self._manifest_worker(wid).get("collectorPid") or 0)
        if col_pid <= 0:
            col_pid = int(self._worker_status(wid).get("collector_pid") or 0)
        if col_pid > 0 and pid_alive(col_pid):
            from ac_workers.emergency_stop import _stop_pid

            _stop_pid(col_pid)
        try:
            from ac_workers.session_cleanup import cleanup_stale_state

            cleanup_stale_state(self.repo, worker_ids=[wid])
        except Exception:
            pass
        self._worker_op_state[wid] = WORKER_OP_STOPPED
        self._worker_op_errors[wid] = "worker {0} stopped working: AC window closed".format(wid)
        self._message = "worker {0} stopped working (window closed)".format(wid)
        self._worker_running_ac_pid.pop(wid, None)
        self._worker_seen_ac_pid.pop(wid, None)
        self._worker_ac_was_alive.pop(wid, None)
        self._worker_ac_dead_since.pop(wid, None)
        self.refresh_metrics_cache(force=True)

    def _reconcile_running_ac_closed(self) -> bool:
        """Detect a running worker whose AC window was closed by the operator.

        Once a worker is live (collector + AC running) we remember its AC pid.
        If that process disappears (window closed / crash) we stop the worker and
        surface "worker stopped working" instead of leaving an orphan collector.
        """
        changed = False
        for wid in range(MAX_WORKERS):
            op = self._worker_op_state.get(wid, WORKER_OP_OFFLINE)
            if op in STARTUP_OPS or op in (
                WORKER_OP_STOPPING,
                WORKER_OP_STOPPED,
                WORKER_OP_CANCELLED,
            ):
                continue
            activity = self._evaluate_worker_activity(wid, lightweight=True)
            ac_pid = self._resolve_ac_pid(wid, activity)
            if activity.active and ac_pid > 0 and pid_alive(ac_pid):
                self._worker_running_ac_pid[wid] = ac_pid
                continue
            seen = int(self._worker_running_ac_pid.get(wid, 0))
            if seen <= 0:
                continue
            if pid_alive(seen):
                continue
            self._handle_worker_window_closed(wid)
            changed = True
        return changed

    def _cancel_worker_bg_proc(self, wid: int) -> None:
        self._terminate_bg_proc(self._worker_job_key(wid))

    def _spawn_ps_job(
        self,
        key: str,
        script: str,
        args: list[str],
        *,
        operation: str,
        worker_id: int | None = None,
        chain: str = "",
    ) -> None:
        cmd = self._ps_cmd(script, args)

        def _runner() -> None:
            t0 = time.time()
            code = 1
            output = ""
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(self.repo),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    **_subprocess_no_window(),
                )
                self._bg_procs[key] = proc
                stdout, stderr = proc.communicate()
                code = int(proc.returncode or 0)
                output = ((stdout or "") + (stderr or "")).strip()
            except Exception as exc:
                output = str(exc)
            finally:
                self._bg_procs.pop(key, None)
            self._bg_queue.put(
                _BgResult(
                    key=key,
                    operation=operation,
                    code=code,
                    output=output,
                    worker_id=worker_id,
                    duration_sec=time.time() - t0,
                    chain=chain,
                )
            )

        thread = threading.Thread(target=_runner, name="c4d-ps-" + key, daemon=True)
        thread.start()

    def _poll_background_ops(self) -> bool:
        changed = False
        now = time.time()
        if now - self._bg_reconcile_ts >= self._refresh_interval_sec:
            self._bg_reconcile_ts = now
            if self._reconcile_starting_ops():
                changed = True
            if self._reconcile_running_ac_closed():
                changed = True
            if self._reconcile_healed_workers():
                changed = True
            self._schedule_dashboard_diag_probes()
        self._maybe_run_vjoy_reaper()
        while True:
            try:
                result = self._bg_queue.get_nowait()
            except queue.Empty:
                break
            changed = True
            self._handle_bg_result(result)
        if self._advance_scale_queue():
            changed = True
        return changed

    def _reconcile_healed_workers(self) -> bool:
        """Clear stale FAILED op state when the collector recovered to idle/running."""
        changed = False
        for wid in range(MAX_WORKERS):
            if self._worker_op_state.get(wid) != WORKER_OP_FAILED:
                continue
            st = self._worker_status(wid)
            if not historical_worker_ready(st):
                continue
            diag_ok, _, _, _ = _diag_cache_peek(wid)
            if not diag_ok:
                continue
            self._worker_op_state[wid] = WORKER_OP_OFFLINE
            self._worker_op_errors.pop(wid, None)
            changed = True
        return changed

    def _schedule_dashboard_diag_probes(self) -> None:
        """Background diag refresh for workers with a live collector (never blocks UI)."""
        for wid in range(MAX_WORKERS):
            st = self._worker_status(wid)
            col_pid = int(st.get("collector_pid") or self._manifest_worker(wid).get("collectorPid") or 0)
            if col_pid > 0 and pid_alive(col_pid):
                _diag_probe_schedule(wid, st)

    def _handle_bg_result(self, result: _BgResult) -> None:
        op = result.operation
        wid = result.worker_id
        notice = self._short_script_notice(result.code, result.output)
        if op == "start" and wid is not None:
            self._reload_session_from_manifest()
            if live_diagnostic(self._worker_status(wid)):
                self._worker_op_state[wid] = WORKER_OP_OFFLINE
                self._worker_op_started_at.pop(wid, None)
                self._worker_op_errors.pop(wid, None)
                self._message = "worker {0}: diagnostic actif, aucune relance automatique".format(wid)
                return
            if self._worker_op_state.get(wid) == WORKER_OP_CANCELLED:
                self.refresh_metrics_cache(force=True)
                return
            if result.code == 0:
                self._worker_op_state[wid] = WORKER_OP_WAITING_READY
                self._worker_op_errors.pop(wid, None)
                self._note_startup_ac_presence(wid)
                self._message = "worker {0} launch completed, waiting for ready".format(wid)
            else:
                reason = "worker {0} start failed: exit={1}".format(wid, result.code)
                if self._is_retryable_startup_failure(
                    wid, reason, result.output
                ) and self._attempt_startup_recovery(
                    wid, "start exit={0}".format(result.code)
                ):
                    return
                self._finalize_start_failure(
                    wid,
                    reason,
                    notice + "\n" + result.output,
                )
            self.refresh_metrics_cache(force=True)
            return
        if op == "stop" and wid is not None:
            from ac_workers.session_cleanup import cleanup_stale_state

            cleanup_stale_state(self.repo, worker_ids=[wid])
            if result.chain == "start":
                self._worker_op_state[wid] = WORKER_OP_STARTING
                self._launch_worker_start(wid)
                self._message = "worker {0}: starting...".format(wid)
            elif result.code == 0:
                self._worker_op_state[wid] = WORKER_OP_OFFLINE
                self._message = "worker {0} stop completed".format(wid)
            else:
                self._worker_op_state[wid] = WORKER_OP_FAILED
                self._worker_op_errors[wid] = notice + "\n" + result.output
                self._message = "worker {0} stop failed: exit={1}".format(wid, result.code)
            self.refresh_metrics_cache(force=True)
            return
        if op == "stop-all":
            from ac_workers.session_cleanup import cleanup_stale_state

            cleanup_stale_state(self.repo, worker_ids=list(range(MAX_WORKERS)), audit_all=True)
            for i in range(MAX_WORKERS):
                if self._worker_op_state[i] == WORKER_OP_STOPPING:
                    self._worker_op_state[i] = WORKER_OP_OFFLINE
            if result.code == 0:
                self._message = "stop-all completed"
            else:
                self._message = "stop-all failed: " + notice
            self.refresh_metrics_cache(force=True)
            return
        if op == "learner_start":
            self._reload_session_from_manifest()
            self._write_learner_start_log(result.code, result.output, result.duration_sec)
            if result.code == 0:
                self._learner_op_state = WORKER_OP_OFFLINE
                self._learner_last_start_error = ""
                self._message = "learner start completed"
            else:
                self._learner_op_state = WORKER_OP_FAILED
                self._learner_last_start_error = result.output.strip()
                tail = self._learner_start_log_tail(limit=800)
                self._message = "learner start failed: " + notice
                if tail:
                    self._message += "\n--- learner_start_last.log ---\n" + tail
            self.refresh_metrics_cache(force=True)
            return

    def _worker_presentation_args(self) -> list[str]:
        return [
            "-RenderPreset",
            "SixInstances",
            "-PresentationMode",
            "-PresentationCamera",
            "ThirdPerson",
            "-PresentationChaseIndex",
            "0",
        ]

    def _load_ui_window_size(self) -> tuple[int, int]:
        """Read worker window size from config/ui_settings.json (Paramètres UI)."""
        path = self.repo / "config" / "ui_settings.json"
        width, height = 832, 468
        if not path.is_file():
            return width, height
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            w = int(data.get("WorkerWidth") or width)
            h = int(data.get("WorkerHeight") or height)
            if 320 <= w <= 3840:
                width = w
            if 240 <= h <= 2160:
                height = h
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
        return width, height

    def _worker_start_script_args(self, wid: int) -> list[str]:
        """Delegate to existing scripts; worker 0 uses the validated single-worker entry.

        -GridWorkerCount 6 keeps every worker tiled in the same 3x2 layout (worker 0
        top-left, 1 top-middle, 2 top-right, 3 bottom-left, ...) even though each
        worker is launched in its own invocation.
        """
        win_w, win_h = self._load_ui_window_size()
        window_args = (
            [
                "-Windowed",
                "-WindowLayout",
                "Legacy",
                "-GridWorkerCount",
                "6",
                "-WindowWidth",
                str(win_w),
                "-WindowHeight",
                str(win_h),
            ]
            + self._worker_presentation_args()
        )
        if wid == 0:
            args = ["-Workers", "1"] + window_args
            if self._skip_vjoy_index_probe:
                args.append("-SkipVjoyIndexProbe")
            return args
        return ["-OnlyWorkerId", str(wid), "-Workers", "6"] + window_args

    def _launch_worker_start(self, wid: int) -> None:
        self._spawn_ps_job(
            self._worker_job_key(wid),
            "Start-Multi-AI-Session.ps1",
            self._worker_start_script_args(wid),
            operation="start",
            worker_id=wid,
        )

    def _launch_worker_stop(self, wid: int, *, chain: str = "") -> None:
        self._spawn_ps_job(
            self._worker_job_key(wid),
            "Stop-Multi-AI-Session.ps1",
            ["-WorkerId", str(wid)],
            operation="stop",
            worker_id=wid,
            chain=chain,
        )

    def _short_script_notice(self, code: int, output: str, limit: int = 240) -> str:
        line = " ".join(output.replace("\r", "\n").split())
        if len(line) > limit:
            line = line[: limit - 1] + "…"
        return "exit={0} {1}".format(code, line).strip()

    def start_workers(self, worker_ids: list[int]) -> str:
        with self._start_op_lock:
            profile_errors = [
                worker_profile_missing_message(wid, repo_root=self.repo)
                for wid in worker_ids
            ]
            profile_errors = [e for e in profile_errors if e]
            if profile_errors:
                return "REFUS: " + "; ".join(profile_errors)
            for wid in worker_ids:
                busy = self._worker_runtime_busy(wid)
                if busy:
                    return busy
                op = self._worker_op_state.get(wid, WORKER_OP_OFFLINE)
                if op in (WORKER_OP_STARTING, WORKER_OP_WAITING_READY) or self._is_start_op_active(wid):
                    return "worker {0} start already in progress".format(wid)
                if op == WORKER_OP_STOPPING:
                    return "worker {0} stop in progress".format(wid)
                activity = self._evaluate_worker_activity(wid)
                if activity.active:
                    return "worker {0} already active: AC pid={1}, collector pid={2}".format(
                        wid, activity.ac_pid or "-", activity.collector_pid or "-"
                    )
            errors = validate_worker_start(
                worker_ids, self.active_workers_map(), self._worker_activity
            )
            if errors:
                return "REFUS: " + "; ".join(errors)
            lines: list[str] = []
            for wid in worker_ids:
                self._worker_pre_start_manifest_sid[wid] = self._manifest_session_id()
                self._worker_op_session_id.pop(wid, None)
                self._worker_collector_validated.pop(wid, None)
                self._worker_ac_was_alive.pop(wid, None)
                self._worker_ac_dead_since.pop(wid, None)
                self._worker_running_ac_pid.pop(wid, None)
                self._invalidate_metrics_cache()
                self._worker_op_state[wid] = WORKER_OP_STARTING
                self._worker_op_started_at[wid] = time.time()
                self._worker_seen_ac_pid.pop(wid, None)
                self._worker_op_errors.pop(wid, None)
                self._launch_worker_start(wid)
                lines.append("worker {0}: starting...".format(wid))
            return "\n".join(lines)

    def start_workers_sequential(self, worker_ids: list[int]) -> str:
        """Queue workers to start one-by-one (next starts when previous startup ends)."""
        if self._bg_procs.get("stop-all") is not None:
            return "scale-test refuse: stop-all en cours; attendez la fin de l'arret."
        if any(live_diagnostic(self._worker_status(w)) for w in range(MAX_WORKERS)):
            return "Demarrage refuse: diagnostic actif. Arretez le diagnostic avant un autre lancement."
        ids = [w for w in worker_ids if 0 <= w < MAX_WORKERS]
        if not ids:
            return "usage: scale-test 0-5"
        # Fail-fast: the launch set must map 1:1 onto distinct pool devices before
        # any process is started (no implicit collisions at any scale up to 6).
        try:
            from ac_workers.vjoy_pool import validate_pool_mapping

            validate_pool_mapping(ids)
        except Exception as exc:
            return "scale-test aborted: vjoy mapping invalid ({0})".format(exc)
        pending = [
            w
            for w in ids
            if not self._worker_runtime_busy(w)
            and not worker_profile_missing_message(w, repo_root=self.repo)
        ]
        if not pending:
            missing = [
                worker_profile_missing_message(w, repo_root=self.repo)
                for w in ids
                if worker_profile_missing_message(w, repo_root=self.repo)
            ]
            if missing:
                return "scale-test: " + "; ".join(missing)
            return "scale-test: all requested workers already active"
        # Hard clean + verify BEFORE launching: kills orphan acs.exe/collectors so a
        # repeated scale-test never collides with an instance left alive by a prior
        # run (the close_game=False footgun that stuck worker 2 on a stale AC pid).
        clean_msg = self._hard_clean_and_verify()
        if clean_msg:
            return clean_msg
        # After the hard clean nothing is running, so the requested set is launchable.
        pending = [
            w
            for w in ids
            if not worker_profile_missing_message(w, repo_root=self.repo)
        ]
        try:
            from ac_workers.session_cleanup import cleanup_stale_state

            cleanup_stale_state(self.repo, worker_ids=pending)
        except Exception:
            pass
        try:
            from ac_workers.vjoy_ledger import clear_all_owners

            clear_all_owners()
        except Exception:
            pass
        prep_msg = self._run_vjoy_clean_slate_blocking()
        if prep_msg:
            return prep_msg
        self._reset_workers_for_scale_boot(ids)
        self._scale_targets = list(ids)
        self._scale_queue = list(pending)
        self._scale_boot_paused = False
        self._scale_last_ready_ts = 0.0
        self._worker_startup_restart_count = {w: 0 for w in pending}
        self._worker_startup_telemetry_seen = {}
        self._advance_scale_queue()
        return self._message

    def _scale_boot_active_for(self, wid: int) -> bool:
        """True while this worker is part of the current scale-test boot sequence."""
        if not self._scale_targets or wid not in self._scale_targets:
            return False
        if wid in self._scale_queue:
            return True
        op = self._worker_op_state.get(wid, WORKER_OP_OFFLINE)
        return op in (WORKER_OP_STARTING, WORKER_OP_WAITING_READY, WORKER_OP_START_TIMEOUT)

    def _reset_workers_for_scale_boot(self, worker_ids: list[int]) -> None:
        """Drop stale C4D op state from a prior run before a new scale-test."""
        for wid in worker_ids:
            self._worker_op_state[wid] = WORKER_OP_OFFLINE
            self._worker_op_errors.pop(wid, None)
            self._worker_op_started_at.pop(wid, None)
            self._worker_op_session_id.pop(wid, None)
            self._worker_pre_start_manifest_sid.pop(wid, None)
            self._worker_startup_telemetry_seen.pop(wid, None)
            self._worker_seen_ac_pid.pop(wid, None)
            self._worker_startup_restart_count.pop(wid, None)
        self._invalidate_metrics_cache()

    def _scale_startup_busy(self) -> bool:
        for wid in range(MAX_WORKERS):
            if not self._scale_boot_active_for(wid):
                continue
            st = self._status_for_op(wid)
            if str(st.get("phase") or "") in COLLECTOR_FAILED_PHASES:
                continue
            op = self._worker_op_state.get(wid, WORKER_OP_OFFLINE)
            if op == WORKER_OP_FAILED:
                continue
            if startup_status_ready(st):
                continue
            if self._is_start_op_active(wid):
                return True
            if op == WORKER_OP_STARTING:
                return True
            if op == WORKER_OP_WAITING_READY:
                if not worker_documents_released(st):
                    return True
        return False

    def _vjoy_all_devices_free(self) -> bool:
        try:
            from ac_workers.vjoy_session import VJD_STAT_FREE, get_vjd_status

            for device_id in range(1, MAX_WORKERS + 1):
                if int(get_vjd_status(device_id)) != int(VJD_STAT_FREE):
                    return False
            return True
        except Exception:
            return False

    def _vjoy_saved_mapping_ready(self) -> bool:
        path = self.repo / "outputs" / "diagnostics" / "vjoy_mapping_latest.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False
        mapping = data.get("device_to_index") or {}
        return isinstance(mapping, dict) and len(mapping) >= MAX_WORKERS

    def _run_vjoy_clean_slate_blocking(self) -> str:
        """Reset vJoy only when devices are stuck; skip the driver bounce if all are free."""
        self._skip_vjoy_index_probe = False
        if self._vjoy_all_devices_free():
            self._skip_vjoy_index_probe = self._vjoy_saved_mapping_ready()
            self._message = "scale-test: vJoy already free (skip driver reset)"
            return ""
        try:
            from ac_workers.vjoy_janitor import janitor_alive, request_vjoy_reset
        except Exception:
            return self._abort_scale_test_if_vjoy_not_clean("vjoy_janitor_import_failed")

        if not janitor_alive():
            return self._abort_scale_test_if_vjoy_not_clean("janitor_offline")

        self._message = "scale-test: vJoy clean-slate (janitor reset)..."
        self.refresh_metrics_cache(force=True)
        result = request_vjoy_reset(list(range(1, MAX_WORKERS + 1)))
        self._vjoy_prep_result = result
        if result.get("ok") and result.get("verified"):
            self._message = "scale-test: vJoy clean-slate OK"
            return ""
        reason = str(result.get("reason") or result.get("message") or "janitor_reset_failed")
        return "scale-test aborted: vJoy clean-slate failed ({0})".format(reason)

    def _abort_scale_test_if_vjoy_not_clean(self, reason: str) -> str:
        """Verify all devices are acquirable; refuse scale-test if not."""
        try:
            import subprocess

            py = self.venv_python
            if not py.is_file():
                return (
                    "scale-test aborted: vJoy not verified ({0}); "
                    "run Reset-vJoy-Driver.ps1 then retry".format(reason)
                )
            proc = subprocess.run(
                [str(py), str(self.repo / "ac_workers" / "verify_vjoy_devices.py")],
                cwd=str(self.repo),
                capture_output=True,
                text=True,
                timeout=30,
                env={
                    **os.environ,
                    "PYTHONPATH": "{0};{1}".format(self.repo, self.repo / "assetto_corsa_gym"),
                    "VJOY_VERIFY_COUNT": str(MAX_WORKERS),
                },
                **_subprocess_no_window(),
            )
            if proc.returncode == 0:
                self._message = "scale-test: vJoy verify OK ({0})".format(reason)
                return ""
            tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-3:]
            detail = " | ".join(tail) if tail else "verify failed"
            return (
                "scale-test aborted: vJoy devices not acquirable ({0}: {1}). "
                "Run Reset-vJoy-Driver.ps1 (admin) then retry".format(reason, detail)
            )
        except Exception as exc:
            return "scale-test aborted: vJoy verify error ({0}: {1})".format(reason, exc)

    def _start_vjoy_clean_slate(self) -> None:
        """Best-effort: ask the privileged janitor for a verified-clean vJoy state
        before launching workers. Runs in the background and gates the scale queue so
        no worker launches onto a stuck device. No-op (does not gate) when the janitor
        is not installed/alive — graceful shutdown already covers the common case.
        """
        try:
            from ac_workers.vjoy_janitor import janitor_alive
        except Exception:
            return
        if not janitor_alive():
            self._vjoy_prep_pending = False
            return
        if self._vjoy_prep_thread is not None and self._vjoy_prep_thread.is_alive():
            return
        self._vjoy_prep_pending = True

        def _worker() -> None:
            result: dict[str, Any] = {}
            try:
                from ac_workers.vjoy_janitor import request_vjoy_reset

                result = request_vjoy_reset(list(range(1, MAX_WORKERS + 1)))
            except Exception as exc:
                result = {"ok": False, "reason": "exception:{0}".format(exc)}
            finally:
                self._vjoy_prep_result = result
                self._vjoy_prep_pending = False

        self._vjoy_prep_thread = threading.Thread(
            target=_worker, name="vjoy-clean-slate", daemon=True
        )
        self._vjoy_prep_thread.start()

    def _live_training_in_progress(self) -> bool:
        """True while the learner or any attached collector is in a live episode cycle."""
        learner = _read_json(LEARNER_STATUS)
        if is_learner_really_active(learner, self._manifest_session_id()):
            return True
        for wid in range(MAX_WORKERS):
            st = self._worker_status(wid)
            phase = str(st.get("phase") or "")
            if phase not in LEARNER_LIVE_PHASES:
                continue
            col_pid = int(st.get("collector_pid") or 0)
            if col_pid > 0 and pid_alive(col_pid):
                return True
        return False

    def _maybe_run_vjoy_reaper(self, interval_sec: float = 12.0) -> None:
        """Throttled background sweep: detect leaked vJoy devices (dead owner, still
        stuck) and, when safe, ask the privileged janitor to reset the driver."""
        if self._live_training_in_progress():
            return
        now = time.monotonic()
        if now - self._vjoy_reaper_last_ts < interval_sec:
            return
        if self._vjoy_reaper_thread is not None and self._vjoy_reaper_thread.is_alive():
            return
        self._vjoy_reaper_last_ts = now

        def _worker() -> None:
            try:
                from ac_workers.vjoy_reaper import sweep

                summary = sweep()
                self._vjoy_reaper_status = summary
                if summary.get("needs_global_reset") and summary.get("safe_to_reset"):
                    from ac_workers.vjoy_janitor import janitor_alive, request_vjoy_reset

                    if janitor_alive():
                        res = request_vjoy_reset(summary.get("leaked") or list(range(1, MAX_WORKERS + 1)))
                        self._vjoy_reaper_status = {**summary, "janitor_result": res}
            except Exception as exc:
                self._vjoy_reaper_status = {"error": str(exc)}

        self._vjoy_reaper_thread = threading.Thread(
            target=_worker, name="vjoy-reaper", daemon=True
        )
        self._vjoy_reaper_thread.start()

    def _advance_scale_queue(self) -> bool:
        if self._scale_boot_paused:
            return False
        if self._vjoy_prep_pending:
            return False
        if not self._scale_queue or self._scale_startup_busy():
            return False
        # Stagger: after a worker reaches ready, let it stabilize before launching
        # the next heavy CSP instance, so the new load does not starve/freeze the
        # one that just came up. The gate is inert until the first worker readies.
        if (
            self._scale_last_ready_ts
            and (time.time() - self._scale_last_ready_ts) < SCALE_SETTLE_SEC
        ):
            return False
        wid = self._scale_queue.pop(0)
        done = len(self._scale_targets) - len(self._scale_queue)
        busy = self._worker_runtime_busy(wid)
        if busy:
            self._message = "scale-test: skip worker {0} — {1} ({2}/{3})".format(
                wid, busy, done, len(self._scale_targets)
            )
            return True
        result = self.start_workers([wid])
        self._message = "scale-test {0}/{1} ({2} queued): {3}".format(
            done, len(self._scale_targets), len(self._scale_queue), result
        )
        return True

    def stop_worker(self, wid: int, *, chain: str = "") -> str:
        op = self._worker_op_state.get(wid, WORKER_OP_OFFLINE)
        if op == WORKER_OP_STOPPING and not chain:
            return "worker {0} stop already in progress".format(wid)
        if op in (WORKER_OP_STARTING, WORKER_OP_WAITING_READY):
            self._cancel_worker_bg_proc(wid)
        self._worker_op_state[wid] = WORKER_OP_STOPPING
        self._launch_worker_stop(wid, chain=chain)
        return "worker {0}: stopping...".format(wid)

    def abort_worker(self, wid: int) -> str:
        """Force-abort an in-progress worker start (collector + launcher)."""
        op = self._worker_op_state.get(wid, WORKER_OP_OFFLINE)
        if op not in STARTUP_OPS and op != WORKER_OP_START_TIMEOUT:
            activity = self._evaluate_worker_activity(wid, lightweight=True)
            if not activity.active:
                return "worker {0} not in startup".format(wid)
        self._cancel_worker_bg_proc(wid)
        col_pid = int(self._manifest_worker(wid).get("collectorPid") or 0)
        if col_pid <= 0:
            st = self._status_for_op(wid) if op in STARTUP_OPS else self._worker_status(wid)
            col_pid = int(st.get("collector_pid") or 0)
        if col_pid > 0 and pid_alive(col_pid):
            from ac_workers.emergency_stop import _stop_pid

            _stop_pid(col_pid)
        self._worker_op_state[wid] = WORKER_OP_CANCELLED
        self._worker_op_errors[wid] = "worker {0} aborted by operator".format(wid)
        self._worker_op_started_at.pop(wid, None)
        self._worker_seen_ac_pid.pop(wid, None)
        self._worker_ac_was_alive.pop(wid, None)
        self._worker_ac_dead_since.pop(wid, None)
        self._message = "worker {0} aborted".format(wid)
        self.refresh_metrics_cache(force=True)
        return self._message

    def restart_worker(self, wid: int) -> str:
        op = self._worker_op_state.get(wid, WORKER_OP_OFFLINE)
        if op in (WORKER_OP_STARTING, WORKER_OP_WAITING_READY):
            return "worker {0} start already in progress".format(wid)
        if op == WORKER_OP_STOPPING:
            return "worker {0} stop in progress".format(wid)
        return self.stop_worker(wid, chain="start")

    def stop_all_workers(self) -> str:
        if self._bg_procs.get("stop-all") is not None:
            return "stop-all already in progress"
        # Cancel queued launches before stopping any process. Otherwise a later
        # refresh can start the next worker while the stop job clears the manifest.
        self._scale_queue.clear()
        self._scale_boot_paused = True
        self._scale_targets = []
        self._scale_last_ready_ts = 0.0
        for wid in range(MAX_WORKERS):
            if self._worker_op_state.get(wid) in (WORKER_OP_STARTING, WORKER_OP_WAITING_READY):
                self._cancel_worker_bg_proc(wid)
            self._worker_op_state[wid] = WORKER_OP_STOPPING
        self._spawn_ps_job("stop-all", "Stop-Multi-AI-Session.ps1", [], operation="stop-all")
        return "stop-all: stopping..."

    def emergency_stop(self) -> str:
        # Same as kill-all intent for the UI: stop collectors/learner AND close
        # every worker AC window (acs.exe). Soft stop without -CloseGame left
        # orphan game windows open.
        code, output, _ = self._run_ps_captured("Emergency-Stop-AI.ps1", ["-CloseGame"])
        time.sleep(1.0)
        self.refresh_metrics_cache(force=True)
        notice = self._short_script_notice(code, output)
        try:
            from ac_workers import system_control

            orphans = system_control.scan_orphan_acs(self.repo)
            if orphans:
                notice += (
                    "\n[!] {0} acs.exe encore vivant(s) {1} — relance l’arrêt "
                    "ou utilise 'kill-all'.".format(len(orphans), orphans)
                )
            else:
                notice += "\nFenêtres workers (acs.exe) fermées."
        except Exception:
            pass
        return notice

    def kill_all(self) -> str:
        """Hard global clean: vJoy-safe collector stop + kill acs.exe + verify."""
        from ac_workers import system_control

        code, output, _ = self._run_ps_captured("Emergency-Stop-AI.ps1", ["-CloseGame"])
        time.sleep(2.0)
        self.refresh_metrics_cache(force=True)
        health = system_control.system_health(self.repo)
        notice = self._short_script_notice(code, output)
        return notice + "\n" + system_control.format_health(health)

    def system_health_text(self) -> str:
        from ac_workers import system_control

        return system_control.format_health(system_control.system_health(self.repo))

    def _hard_clean_and_verify(self) -> str:
        """Kill orphan acs.exe/collectors/learner and verify a clean slate.

        Returns "" when the system is clean (safe to start), else a DIRTY message.
        Shared by scale-test and clean-start so neither can launch into an orphan
        AC instance left by a prior run.
        """
        from ac_workers import system_control
        from ac_workers.emergency_stop import _stop_pid

        def _kill_zombie_collectors(health: dict[str, Any]) -> None:
            zombies = health.get("zombie_collectors") or {}
            for _wid, pids in zombies.items():
                for pid in pids or []:
                    try:
                        _stop_pid(int(pid))
                    except Exception:
                        pass

        clean, health = system_control.assert_clean_state(self.repo)
        if clean:
            self.refresh_metrics_cache(force=True)
            return ""
        for _attempt in range(2):
            self._run_ps_captured("Emergency-Stop-AI.ps1", ["-CloseGame"])
            time.sleep(HARD_CLEAN_SETTLE_SEC)
            clean, health = system_control.assert_clean_state(self.repo)
            if clean:
                break
            # Emergency-Stop can miss a stray collector PID; kill them explicitly.
            _kill_zombie_collectors(health)
            time.sleep(HARD_CLEAN_SETTLE_SEC)
            clean, health = system_control.assert_clean_state(self.repo)
            if clean:
                break
            # Transition server can respawn briefly; one more settle before re-scan.
            if health.get("busy_ports") == [system_control.TRANSITION_PORT]:
                time.sleep(HARD_CLEAN_SETTLE_SEC)
                clean, health = system_control.assert_clean_state(self.repo)
                if clean:
                    break
        self.refresh_metrics_cache(force=True)
        if not clean:
            return (
                "demarrage BLOQUE — etat non propre apres nettoyage:\n"
                + system_control.format_health(health)
                + "\nFerme les fenetres AC restantes (ou 'kill-all') puis reessaie."
            )
        return ""

    def clean_start(self, worker_ids: list[int]) -> str:
        """Deterministic bootstrap. scale-test now performs the same hard clean +
        verify, so clean-start is its alias kept for muscle memory."""
        return self.start_workers_sequential(list(worker_ids))

    def worker_diag(self, wid: int, cmd: str) -> dict[str, Any]:
        from ac_workers.collector_diag import CollectorDiagClient, DiagRequestError

        try:
            return CollectorDiagClient(wid).request(cmd)
        except DiagRequestError as exc:
            return {"ok": False, "error": exc.message}

    def _worker_ready_for_learner(self, wid: int) -> bool:
        op = self._worker_op_state.get(wid, WORKER_OP_OFFLINE)
        if op in (WORKER_OP_STARTING, WORKER_OP_WAITING_READY):
            return False
        st = self._worker_status(wid)
        late_ready = historical_worker_ready(st) and op == WORKER_OP_START_TIMEOUT
        if not late_ready and not self.worker_active(wid):
            return False
        phase = str(st.get("phase") or "")
        if phase in COLLECTOR_FAILED_PHASES:
            return False
        if phase not in LEARNER_LIVE_PHASES:
            return False
        if not bool(st.get("policy_ready")):
            return False
        if float(st.get("telemetry_age_ms", 99999.0)) > 2000.0:
            return False
        if not bool(st.get("vjoy_device_acquired") or st.get("vjoy_ready")):
            return False
        if not bool(st.get("initial_session_attached")):
            return False
        # Workers idle at neutral until the learner starts them, so no transitions
        # or policy observations are required to be ready: attach + vJoy + fresh
        # telemetry is enough.
        return True

    def _ready_worker_count_for_learner(self) -> int | None:
        count = 0
        for wid in range(MAX_WORKERS):
            if self._worker_ready_for_learner(wid):
                count = wid + 1
                continue
            if self.worker_active(wid):
                break
            break
        return count if count > 0 else None

    def _ready_worker_ids_for_learner(self) -> list[int]:
        """All workers ready to train, tolerating holes (e.g. [0,1,3,5]).

        The transition server is shared and the trainer consumes per worker id,
        so contiguity is not required; only worker 0 is mandatory because the
        Run-SAC precheck validates worker_0.json specifically.
        """
        return [w for w in range(MAX_WORKERS) if self._worker_ready_for_learner(w)]

    def _worker_blocked_for_learner_stop(self, wid: int) -> bool:
        """Workers to stop before learner — definitive failures and prefix blockers only."""
        if not self.worker_active(wid):
            return False
        op = self._worker_op_state.get(wid, WORKER_OP_OFFLINE)
        if op in STARTUP_OPS or op == WORKER_OP_STOPPING:
            return False
        st = self._worker_status(wid)
        phase = str(st.get("phase") or "")
        if phase in COLLECTOR_FAILED_PHASES:
            return True
        ready_ids = [w for w in range(MAX_WORKERS) if self._worker_ready_for_learner(w)]
        if not ready_ids:
            if int(st.get("transition_count") or 0) <= 0 and not bool(st.get("initial_session_attached")):
                return phase not in LEARNER_LIVE_PHASES
            return False
        span = max(ready_ids) + 1
        if wid >= span or self._worker_ready_for_learner(wid):
            return False
        if phase in LEARNER_LIVE_PHASES and int(st.get("transition_count") or 0) > 0:
            return False
        return True

    def _force_stop_worker_processes(self, wid: int) -> list[int]:
        """Kill AC/collector PIDs immediately so blocked workers release vJoy and windows."""
        from ac_workers.emergency_stop import _stop_pid

        pids: set[int] = set()
        entry = self._manifest_worker(wid)
        for field in ("collectorPid", "collectorLauncherPid", "acPid"):
            pid = int(entry.get(field) or 0)
            if pid > 0:
                pids.add(pid)
        st = self._worker_status(wid)
        for field in ("collector_pid", "ac_pid"):
            pid = int(st.get(field) or 0)
            if pid > 0:
                pids.add(pid)
        seen_ac = int(self._worker_seen_ac_pid.get(wid, 0))
        if seen_ac > 0:
            pids.add(seen_ac)
        log_ac = self._read_launch_ac_pid_from_log(wid)
        if log_ac > 0:
            pids.add(log_ac)
        activity = self._worker_activity.get(wid)
        if activity is not None:
            if activity.collector_pid:
                pids.add(int(activity.collector_pid))
            if activity.ac_pid:
                pids.add(int(activity.ac_pid))
        stopped: list[int] = []
        for pid in sorted(pids):
            if pid > 0:
                _stop_pid(pid)
                stopped.append(pid)
        time.sleep(0.35)
        for pid in sorted(pids):
            if pid > 0 and pid_alive(pid):
                _stop_pid(pid)
        return stopped

    def _partition_workers_for_learner(self) -> tuple[int | None, list[int]]:
        """Return (contiguous worker count 0..N-1, worker ids to stop)."""
        blocked = [
            wid for wid in range(MAX_WORKERS) if self._worker_blocked_for_learner_stop(wid)
        ]
        count = self._ready_worker_count_for_learner()
        return count, blocked

    def _policy_command_all(self, action: str) -> str:
        """Pause or resume policy driving on every active worker (diag pause/resume).

        This does not kill workers, reset cars, or touch the learner — it only
        toggles whether collectors apply policy actions vs hold a safe stop.
        """
        active = [w for w, on in self.active_workers_map().items() if on]
        if not active:
            return "policy {0}: no active workers".format(action)
        results = []
        for wid in active:
            resp = self.worker_diag(wid, action)
            ok = bool(resp.get("ok"))
            results.append("{0}:{1}".format(wid, "ok" if ok else "fail"))
        return "policy {0} -> {1}".format(action, " ".join(results))

    def _resume_workers_for_learner(self, worker_ids: list[int]) -> tuple[list[int], list[int]]:
        """Start driving on idle collectors (diag resume + file sentinel fallback)."""
        from ac_workers.collector_resume import request_collector_resume

        ok_ids: list[int] = []
        failed: list[int] = []
        for wid in worker_ids:
            resp = self.worker_diag(wid, "resume")
            if resp.get("ok"):
                ok_ids.append(wid)
                continue
            request_collector_resume(wid)
            deadline = time.monotonic() + 3.0
            started = False
            while time.monotonic() < deadline:
                st = self._worker_status(wid)
                phase = str(st.get("phase") or "")
                if bool(st.get("started")) or phase in ("running", "resetting"):
                    started = True
                    break
                time.sleep(0.15)
            if started:
                ok_ids.append(wid)
            else:
                failed.append(wid)
        return ok_ids, failed

    def _learner_resume_available(self) -> bool:
        """True only if the resume bundle matches the current training contract."""
        bundle = LEARNER_RESUME_CKPT / "training_state.pt"
        if not bundle.is_file():
            return False
        try:
            from omegaconf import OmegaConf
            from ac_workers.training_checkpoint import is_compatible_resume

            cfg = OmegaConf.load(str(REPO / "config_first_sac_cpu_v3.yml"))
            return is_compatible_resume(bundle, cfg)
        except Exception:
            return False

    def _learner_pid_from_status_or_manifest(self) -> int:
        learner = _read_json(LEARNER_STATUS)
        pid = int(learner.get("learner_pid") or 0) if learner else 0
        if pid > 0:
            return pid
        manifest = _read_json(MANIFEST)
        return int(manifest.get("learnerPid") or 0)

    def _write_learner_start_log(self, code: int, output: str, duration_sec: float) -> None:
        LEARNER_START_LOG.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat()
        body = (
            "timestamp={0}\nexit_code={1}\nduration_sec={2:.3f}\n\n{3}\n".format(
                stamp, code, duration_sec, (output or "").strip()
            )
        )
        LEARNER_START_LOG.write_text(body, encoding="utf-8")

    def _learner_start_log_tail(self, limit: int = 800) -> str:
        if not LEARNER_START_LOG.is_file():
            return self._learner_last_start_error
        text = LEARNER_START_LOG.read_text(encoding="utf-8", errors="replace").strip()
        if len(text) <= limit:
            return text
        return text[-limit:]

    def learner_error_text(self) -> str:
        if LEARNER_START_LOG.is_file():
            return LEARNER_START_LOG.read_text(encoding="utf-8", errors="replace").strip()
        if self._learner_last_start_error:
            return self._learner_last_start_error
        return "no learner start error logged"

    def action_diversity_text(self) -> str:
        from ac_workers.action_diagnostics import summarize_live_worker_actions

        return summarize_live_worker_actions(self.repo)

    def _build_learner_status_report(self) -> dict[str, Any]:
        learner = _read_json(LEARNER_STATUS) or {}
        trainer_state = _read_json(TRAINER_STATE) or {}
        manifest = _read_json(MANIFEST) or {}
        server_stats = self._transition_server_stats()
        active_worker_ids = self._active_worker_ids_from_server(server_stats)
        pid = self._learner_pid_from_status_or_manifest()
        live = bool(learner) and is_learner_really_active(
            learner, str(manifest.get("sessionId") or "")
        )
        if pid > 0 and pid_alive(pid) and is_learner_process(pid):
            live = True
        publish_meta = self._read_published_metadata()
        resume_path = str(LEARNER_RESUME_CKPT) if self._learner_resume_available() else ""
        last_ckpt = str(learner.get("checkpoint_saved_path") or trainer_state.get("last_checkpoint") or "")
        if not last_ckpt:
            ckpt_glob = sorted(
                (LEARNER_WORK_DIR / "model" / "checkpoints").glob("step_*"),
                key=lambda p: p.stat().st_mtime,
            )
            if ckpt_glob:
                last_ckpt = str(ckpt_glob[-1])
        return {
            "learner_running": live,
            "learner_pid": pid if live else (pid if pid_alive(pid) else 0),
            "learner_process_alive": bool(pid > 0 and pid_alive(pid)),
            "replay_size": int(
                learner.get("replay_size")
                or learner.get("learner_replay_size")
                or trainer_state.get("replay_size")
                or 0
            ),
            "sac_updates": int(
                learner.get("updates")
                or learner.get("learner_training_step")
                or trainer_state.get("updates")
                or 0
            ),
            "global_steps": int(
                learner.get("global_steps")
                or learner.get("learner_global_steps")
                or trainer_state.get("steps")
                or 0
            ),
            "checkpoint_loaded_path": str(learner.get("checkpoint_loaded_path") or resume_path),
            "checkpoint_saved_path": last_ckpt,
            "policy_version": int(
                publish_meta.get("training_step")
                or learner.get("policy_version")
                or trainer_state.get("steps")
                or 0
            ),
            "mean_reward": float(learner.get("mean_reward") or 0.0),
            "avg_episode_length_sec": float(learner.get("avg_episode_length_sec") or 0.0),
            "done_reason_histogram": learner.get("done_reason_histogram") or {},
            "learner_status_note": str(learner.get("learner_status_note") or ""),
            "resume_checkpoint_available": self._learner_resume_available(),
            "publish_dir": str(LEARNER_PUBLISH_DIR),
            "learner_start_log": str(LEARNER_START_LOG),
            "learner_start_last_error": self._learner_start_log_tail(limit=400),
            "continuous_mode": bool(learner.get("continuous_mode", True)),
            "checkpoint_loaded_step": int(
                learner.get("checkpoint_loaded_step")
                or trainer_state.get("checkpoint_loaded_step")
                or 0
            ),
            "target_steps": learner.get("target_steps"),
            "remaining_steps": learner.get("remaining_steps"),
            "control_mode": str(
                server_stats.get("control_mode") or learner.get("control_mode") or "async_local_actor"
            ),
            "last_checkpoint_path": str(learner.get("last_checkpoint_path") or last_ckpt),
            "last_publish_path": str(
                learner.get("last_publish_path") or str(LEARNER_PUBLISH_DIR)
            ),
            "last_checkpoint_age_sec": learner.get("last_checkpoint_age_sec"),
            "last_publish_age_sec": learner.get("last_publish_age_sec"),
            "publish_every_updates": int(learner.get("publish_every_updates", 1)),
            "policy_published_count": int(learner.get("policy_published_count", 0)),
            "last_publish_version": int(learner.get("last_publish_version", 0)),
            "last_publish_complete": bool(learner.get("last_publish_complete", True)),
            "last_publish_error": str(learner.get("last_publish_error", "")),
            "publish_in_progress": bool(learner.get("publish_in_progress", False)),
            "last_transition_worker_id": int(learner.get("last_transition_worker_id", -1)),
            "transitions_by_worker": learner.get("transitions_by_worker") or {},
            "updates_since_start": int(learner.get("updates_since_start", 0)),
            "actor_requests_total": int(server_stats.get("actor_requests_total", 0)),
            "actor_responses_total": int(server_stats.get("actor_responses_total", 0)),
            "actor_timeouts_total": int(server_stats.get("actor_timeouts_total", 0)),
            "active_worker_ids": active_worker_ids,
        }

    def set_control_mode(self, mode: str) -> str:
        mode = (mode or "").strip().lower()
        valid = {"async_local_actor", "async_actor", "async_local"}
        if mode not in valid:
            current = str(
                self._transition_server_stats().get("control_mode") or "async_local_actor"
            )
            return (
                "usage: control-mode async_local_actor | async_actor | async_local "
                "(current={0})".format(current)
            )
        try:
            from common.transition_server import TransitionClient

            TransitionClient().set_control_mode(mode)
        except Exception as exc:
            return "control-mode failed: {0} (is the transition server running?)".format(exc)
        return (
            "control_mode set to {0} — workers pick it up within ~0.5s "
            "(no restart needed)".format(mode)
        )

    def actor_stats_text(self) -> str:
        stats = self._transition_server_stats()
        if not stats:
            return "actor-stats: transition server not reachable"
        report = {
            "control_mode": str(stats.get("control_mode") or "async_local_actor"),
            "actor_requests_total": int(stats.get("actor_requests_total", 0)),
            "actor_responses_total": int(stats.get("actor_responses_total", 0)),
            "actor_timeouts_total": int(stats.get("actor_timeouts_total", 0)),
            "actor_pending_requests": stats.get("actor_pending_requests", []),
            "active_worker_ids": self._active_worker_ids_from_server(stats),
            "queue_size": int(stats.get("queue_size", 0)),
        }
        return json.dumps(report, indent=2)

    def policy_status_text(self) -> str:
        """Per-worker local actor policy status."""
        lines: list[str] = []
        for wid in range(MAX_WORKERS):
            snap = self._read_worker_status(wid)
            if not snap:
                continue
            lines.append(
                "worker {0}: mode={1} active_v={2} pending_v={3} swap_pending={4} "
                "swaps={5} last_swap_ep={6} ep={7} steps={8} reloads={9} "
                "failed={10} seed={11} src={12} last_err={13}".format(
                    wid,
                    snap.get("control_mode", "?"),
                    snap.get("active_policy_version", 0),
                    snap.get("pending_policy_version", 0),
                    snap.get("policy_swap_pending", False),
                    snap.get("policy_swap_count", 0),
                    snap.get("last_swap_episode", -1),
                    snap.get("episode_id", 0),
                    snap.get("steps_since_episode_start", 0),
                    snap.get("policy_reload_count", 0),
                    snap.get("policy_reload_failed_count", 0),
                    snap.get("worker_seed", snap.get("policy_seed", "?")),
                    snap.get("action_source", "?"),
                    snap.get("last_policy_reload_error", "") or "none",
                )
            )
        if not lines:
            return "policy-status: no active workers found"
        return "\n".join(lines)

    def _read_published_metadata(self) -> dict[str, Any]:
        """Metadata of the currently published policy (versioned pointer first)."""
        model_dir = LEARNER_WORK_DIR / "model"
        try:
            from ac_workers.policy_metadata import read_pointer

            pointer = read_pointer(model_dir)
            if pointer is not None:
                version_dir = model_dir / "publish_versions" / str(pointer.get("version_dir", ""))
                meta = _read_json(version_dir / "policy_metadata.json")
                if meta:
                    return meta
        except Exception:
            pass
        # Legacy fallback for old checkpoints published in-place.
        return _read_json(LEARNER_PUBLISH_DIR / "policy_metadata.json") or {}

    def _transition_server_stats(self) -> dict[str, Any]:
        try:
            from common.transition_server import TransitionClient

            return TransitionClient().stats()
        except Exception:
            return {}

    def _active_worker_ids_from_server(self, stats: dict[str, Any], max_age_sec: float = 10.0) -> list[int]:
        out: list[int] = []
        now = time.time()
        for wid, last in (stats.get("heartbeats") or {}).items():
            try:
                if now - float(last) <= max_age_sec:
                    out.append(int(wid))
            except (TypeError, ValueError):
                continue
        return sorted(out)

    def learner_start(self) -> str:
        for wid in range(MAX_WORKERS):
            status = self._worker_status(wid)
            if pid_alive(int(status.get("collector_pid") or 0)):
                error = startup_policy_error(status)
                if error:
                    return "learner start refuse: worker {0}: {1}".format(wid, error)
        if self._learner_op_state == WORKER_OP_STARTING:
            return "learner start already in progress"
        if self._scale_queue or self._scale_startup_busy():
            return (
                "learner start refuse: scale-test encore en cours. "
                "Attends que les workers demandes soient prets, puis relance learner start."
            )
        # Tolerant (degraded) mode: train on every ready worker, tolerating holes
        # (e.g. 0,1,3,5). The transition server is shared and the trainer consumes
        # per worker id, so contiguity is not required. Failed workers in the middle
        # are left alone (not killed) and only reported.
        ready_ids = self._ready_worker_ids_for_learner()
        if not ready_ids:
            return (
                "learner start refused: no ready workers "
                "(fix vJoy/attach failures first)"
            )
        if 0 not in ready_ids:
            return (
                "learner start refused: worker 0 not ready "
                "(worker 0 is mandatory — the Run-SAC precheck validates it)"
            )
        pause_note = self._pause_scale_boot()
        resumed, resume_failed = self._resume_workers_for_learner(list(ready_ids))
        if resume_failed:
            return (
                "learner start refused: resume failed on worker(s) {0} "
                "(collectors must leave idle before transitions)".format(
                    ",".join(str(w) for w in resume_failed)
                )
            )
        worker_ids_arg = ",".join(str(w) for w in ready_ids)
        sac_args = ["-Workers", str(len(ready_ids)), "-WorkerIds", worker_ids_arg, "-SkipPrecheck", "-Continuous"]
        resume_note = ""
        if self._learner_resume_available():
            sac_args.append("-Resume")
            resume_note = " (resume from {0})".format(LEARNER_RESUME_CKPT)
        elif (LEARNER_RESUME_CKPT / "policy_net.pth").is_file():
            resume_note = " (ancien resume ignore, config changee — train wipe recommande)"
        self._learner_op_state = WORKER_OP_STARTING
        self._spawn_ps_job(
            "learner",
            "Run-SAC-MultiWorker.ps1",
            sac_args,
            operation="learner_start",
        )
        msg = "learner: starting... (Workers={0}: {1}){2}".format(
            len(ready_ids), worker_ids_arg, resume_note
        )
        skipped = [
            wid
            for wid in range(MAX_WORKERS)
            if self.worker_active(wid) and wid not in ready_ids
        ]
        if skipped:
            msg += (
                " | skipped not-ready workers: "
                + ",".join(str(w) for w in skipped)
                + " (degraded mode — left running, fix later)"
            )
        if pause_note:
            msg += " | " + pause_note
        return msg

    def learner_save(self) -> str:
        LEARNER_SAVE.parent.mkdir(parents=True, exist_ok=True)
        LEARNER_SAVE.write_text("save\n", encoding="utf-8")
        pid = self._learner_pid_from_status_or_manifest()
        if pid <= 0 or not pid_alive(pid):
            return "learner save requested but no active learner process found"
        return "learner save requested (checkpoint will be written without stopping)"

    def learner_stop(self, *, force: bool = False) -> str:
        pid = self._learner_pid_from_status_or_manifest()
        if pid <= 0 or not pid_alive(pid):
            from ac_workers.session_cleanup import cleanup_stale_state

            cleanup_stale_state(self.repo, cleanup_learner=True)
            self.refresh_metrics_cache(force=True)
            return "learner not running (stale state cleaned up)"

        LEARNER_STOP.parent.mkdir(parents=True, exist_ok=True)
        LEARNER_STOP.write_text("stop\n", encoding="utf-8")

        deadline = time.monotonic() + LEARNER_STOP_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if not pid_alive(pid):
                break
            time.sleep(0.5)

        if pid_alive(pid):
            if not force:
                return (
                    "learner stop requested but process pid={0} still running after {1:.0f}s "
                    "(checkpoint should save on stop; use 'learner force-stop' to kill)"
                ).format(pid, LEARNER_STOP_TIMEOUT_SEC)
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/F"],
                        check=False,
                        capture_output=True,
                        **_subprocess_no_window(),
                    )
                else:
                    os.kill(pid, 9)
            except OSError:
                pass
            time.sleep(0.5)

        from ac_workers.session_cleanup import cleanup_stale_state

        cleanup_stale_state(self.repo, cleanup_learner=True)
        self.refresh_metrics_cache(force=True)
        report = self._build_learner_status_report()
        return (
            "learner stopped pid={0} | replay_size={1} sac_updates={2} "
            "checkpoint_saved={3} policy_version={4}"
        ).format(
            pid,
            report.get("replay_size", 0),
            report.get("sac_updates", 0),
            report.get("checkpoint_saved_path") or LEARNER_RESUME_CKPT,
            report.get("policy_version", 0),
        )

    def learner_status_text(self) -> str:
        report = self._build_learner_status_report()
        if not report.get("learner_running") and not report.get("resume_checkpoint_available"):
            if not _read_json(LEARNER_STATUS) and not _read_json(TRAINER_STATE):
                return "learner status: no learner data (never started or cleaned up)"
        return json.dumps(report, indent=2)

    def _training_session_name(self) -> str:
        try:
            from ac_workers.training_sessions import current_session_name

            return current_session_name()
        except Exception:
            return ""

    def _learner_is_live_process(self) -> bool:
        pid = self._learner_pid_from_status_or_manifest()
        return pid > 0 and pid_alive(pid) and is_learner_process(pid)

    def handle_wipe_memory(self) -> str:
        """Stop the learner, erase resume/replay pointers, publish a random brain."""
        notes: list[str] = []
        if self._learner_is_live_process():
            notes.append(self.learner_stop(force=True))
            if self._learner_is_live_process():
                return "train wipe refuse: le learner tourne encore. Relance 'train wipe' ou 'learner force-stop'."
        try:
            from ac_workers.training_sessions import wipe_and_reseed

            result = wipe_and_reseed(LEARNER_WORK_DIR)
        except Exception as exc:
            return "train wipe echoue: {0}".format(exc)
        pointer = result.get("published_pointer") or {}
        version = pointer.get("policy_version", 0)
        resume = result.get("resume_checkpoint_present")
        self.refresh_metrics_cache(force=True)
        extra = (" | " + " | ".join(notes)) if notes else ""
        return (
            "memoire effacee: cerveau neuf version={0} resume={1}.{2} "
            "Les workers prennent le nouveau cerveau au prochain reset. "
            "Puis: learner start (sans resume)."
        ).format(version, resume, extra)

    def handle_training_session_command(self, arg: str) -> str:
        from ac_workers import training_sessions as ts

        raw = (arg or "").strip()
        parts = raw.split(None, 1)
        sub = (parts[0].lower() if parts else "")
        name = parts[1].strip() if len(parts) > 1 else ""
        if sub in ("", "list", "ls"):
            return ts.format_session_list()
        if sub == "save":
            if not name:
                return "usage: train save <nom>"
            try:
                meta = ts.save_session(name)
            except ValueError as exc:
                return str(exc)
            except OSError as exc:
                return "sauvegarde impossible: {0}".format(exc)
            note = ""
            if self._learner_is_live_process():
                note = " (learner encore actif: snapshot disque, pas le replay RAM)"
            return "session sauvee: {0} steps={1} updates={2}{3}".format(
                meta["name"], meta.get("steps", 0), meta.get("updates", 0), note
            )
        if sub == "load":
            if not name:
                return "usage: train load <nom>"
            if self._learner_is_live_process():
                return "train load refuse: arrete le learner d abord (learner stop)"
            try:
                meta = ts.load_session(name)
            except (ValueError, FileNotFoundError, OSError) as exc:
                return str(exc)
            return "session chargee: {0} steps={1} — relance avec 'learner start'".format(
                meta.get("name") or name, meta.get("steps", 0)
            )
        if sub == "new":
            if not name:
                return "usage: train new <nom>"
            if self._learner_is_live_process():
                return "train new refuse: arrete le learner d abord (learner stop)"
            try:
                meta = ts.new_session(name)
            except (ValueError, FileExistsError, OSError) as exc:
                return str(exc)
            extra = ""
            if meta.get("autosaved"):
                extra = " (ancien live sauve sous {0})".format(meta["autosaved"])
            return "nouvelle session: {0}{1} — poids aleatoires prets, 'learner start' pour entrainer".format(
                meta["name"], extra
            )
        if sub in ("wipe", "forget", "clear", "reset-memory"):
            return self.handle_wipe_memory()
        if sub in ("delete", "del", "rm"):
            if not name:
                return "usage: train delete <nom>"
            try:
                deleted = ts.delete_session(name)
            except (ValueError, FileNotFoundError, OSError) as exc:
                return str(exc)
            return "session supprimee: {0}".format(deleted)
        return (
            "usage: train list | train save <nom> | train load <nom> | "
            "train new <nom> | train delete <nom> | train wipe"
        )

    def transition_stats_text(self) -> str:
        try:
            from common.transition_server import TransitionClient

            return json.dumps(TransitionClient().stats(), indent=2)
        except Exception as exc:
            return "transition stats error: {0}".format(exc)

    def evolve_start(self) -> str:
        import threading

        from omegaconf import OmegaConf
        from ac_workers.evolution_runner import run_evolution
        from ac_workers.neuroevolution import NeuroEvolution

        if getattr(self, "_evolution_thread", None) and self._evolution_thread.is_alive():
            _g = getattr(self._evolution_evo, "generation", "?")
            return "evolve: déjà en cours (génération {0})".format(_g)

        cfg = OmegaConf.load(str(REPO / "config_first_sac_cpu_v3.yml"))
        evo = NeuroEvolution(
            state_dim=8,
            action_dim=3,
            hidden=(256, 256),
            population_size=int(OmegaConf.select(cfg, "Agent.neuroevolution_population", default=120)),
            elitism=int(OmegaConf.select(cfg, "Agent.neuroevolution_elitism", default=2)),
            parents=int(OmegaConf.select(cfg, "Agent.neuroevolution_parents", default=10)),
            mutation_rate=float(OmegaConf.select(cfg, "Agent.neuroevolution_mutation_rate", default=0.05)),
            mutation_intensity=float(OmegaConf.select(cfg, "Agent.neuroevolution_mutation_intensity", default=0.03)),
            work_dir=str(REPO / "outputs" / "neuroevolution"),
        )
        if not evo.load():
            evo.init_generation(seed=0)
            evo.save()
        self._evolution_evo = evo
        self._evolution_stop_event = threading.Event()
        self._evolution_thread = threading.Thread(
            target=run_evolution,
            args=(evo,),
            kwargs={"stop_event": self._evolution_stop_event},
            daemon=True,
        )
        self._evolution_thread.start()
        return "evolve: génération {0} lancée ({1} candidats, 6 workers)".format(
            evo.generation, evo.population_size
        )

    def evolve_stop(self) -> str:
        if getattr(self, "_evolution_stop_event", None) is not None:
            self._evolution_stop_event.set()
            return "evolve: arrêt demandé (fin du cycle en cours)"
        return "evolve: aucun cycle en cours"

    def evolve_status_text(self) -> str:
        evo = getattr(self, "_evolution_evo", None)
        if evo is None:
            return "evolve: aucun cycle (lancez 'evolve start')"
        evaluated = int(getattr(evo, "evaluated", []).sum()) if getattr(evo, "evaluated", None) is not None else 0
        return "evolve: génération {0} — {1}/{2} candidats évalués".format(
            evo.generation, evaluated, evo.population_size
        )

    def handle_command(self, line: str) -> str | None:
        line = (line or "").strip()
        if not line:
            return ""
        parts = line.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("help", "?"):
            return (
                "help status workers start <spec> scale-test [spec] stop <id> abort <id> restart <id> stop-all "
                "reset <id> pause <id> resume <id> error <id> worker <id> timeline [<id>] shm-check "
                "action-diversity action_source policy pause|resume workers pause-policy|resume-policy "
                "control-mode async_local_actor|async_actor|async_local actor-stats "
                "emergency-stop kill-all health clean-start [spec] test-start <id> test-status <id> test-log <id> "
                "test-cancel <id> test-pids <id> test-health <id> test-stop <id> "
                "refresh-rate [sec] sound [on|off] drive-click [on|off] diag-check [spec] diag-ping [spec] identity-test [--pulse|--direct] [spec] "
                "learner start|stop|save|force-stop|status|error "
                "train list|save <nom>|load <nom>|new <nom>|delete <nom>|wipe "
                "memory clear "
                "stats clear quit"
            )
        if cmd == "quit":
            self._quit = True
            return "au revoir"
        if cmd == "clear":
            return "__CLEAR__"
        if cmd == "status":
            return ""
        if cmd == "workers":
            sub = arg.lower().strip()
            if sub in ("pause-policy", "pause_policy"):
                return self._policy_command_all("pause")
            if sub in ("resume-policy", "resume_policy"):
                return self._policy_command_all("resume")
            if sub:
                return "usage: workers | workers pause-policy | workers resume-policy"
            return self.format_workers_readiness()
        if cmd == "stats":
            return self.transition_stats_text()
        if cmd == "start":
            ids = parse_worker_spec(arg)
            if not ids:
                return "usage: start 0 | start 0,2,4 | start 0-5"
            return self.start_workers(ids)
        if cmd in ("scale-test", "scale", "start-seq", "startall", "start-all"):
            ids = parse_worker_spec(arg) if arg else list(range(MAX_WORKERS))
            if not ids:
                return "usage: scale-test | scale-test 0-5 | scale-test 0,2,4"
            return self.start_workers_sequential(ids)
        if cmd == "stop" and arg.lower() == "all":
            return self.stop_all_workers()
        if cmd == "stop-all":
            return self.stop_all_workers()
        if cmd == "stop":
            ids = parse_worker_spec(arg)
            if len(ids) != 1:
                return "usage: stop <worker_id>"
            return self.stop_worker(ids[0])
        if cmd == "abort":
            ids = parse_worker_spec(arg)
            if len(ids) != 1:
                return "usage: abort <worker_id>"
            return self.abort_worker(ids[0])
        if cmd == "restart":
            ids = parse_worker_spec(arg)
            if len(ids) != 1:
                return "usage: restart <worker_id>"
            return self.restart_worker(ids[0])
        if cmd == "reset":
            ids = parse_worker_spec(arg)
            if len(ids) != 1:
                return "usage: reset <worker_id>"
            return json.dumps(self.worker_diag(ids[0], "reset"))
        if cmd == "pause":
            ids = parse_worker_spec(arg)
            if len(ids) != 1:
                return "usage: pause <worker_id>"
            return json.dumps(self.worker_diag(ids[0], "pause"))
        if cmd == "resume":
            ids = parse_worker_spec(arg)
            if len(ids) != 1:
                return "usage: resume <worker_id>"
            return json.dumps(self.worker_diag(ids[0], "resume"))
        if cmd == "neutralize":
            ids = parse_worker_spec(arg)
            if len(ids) != 1:
                return "usage: neutralize <worker_id>"
            return json.dumps(self.worker_diag(ids[0], "neutralize"))
        if cmd == "error":
            ids = parse_worker_spec(arg)
            if len(ids) != 1:
                return "usage: error <worker_id>"
            return self.worker_error_detail(ids[0])
        if cmd == "timeline":
            from ac_workers import pipeline_timeline

            if not arg:
                return pipeline_timeline.summarize(
                    list(range(MAX_WORKERS)), repo_root=self.repo
                )
            ids = parse_worker_spec(arg)
            if len(ids) != 1:
                return "usage: timeline | timeline <worker_id>"
            records = pipeline_timeline.read_timeline(ids[0], repo_root=self.repo)
            if not records:
                return "worker {0}: aucune timeline".format(ids[0])
            lines = ["worker {0} timeline:".format(ids[0])]
            for r in records:
                flag = " [ORPHAN]" if r.get("orphan") else ""
                lines.append(
                    "{0:>7.3f}s  rid={1:<4} {2:<11} {3:<9} {4}{5}{6}".format(
                        float(r.get("elapsed", 0.0)),
                        r.get("reset_id", 0),
                        str(r.get("cycle", "")),
                        str(r.get("component", "")),
                        str(r.get("step", "")),
                        flag,
                        (" — " + str(r.get("detail"))) if r.get("detail") else "",
                    )
                )
            mismatches = pipeline_timeline.count_orphans(ids[0], repo_root=self.repo)
            if mismatches:
                lines.append("cross-epoch orphan events: {0}".format(mismatches))
            anomalies = pipeline_timeline.validate_timeline(ids[0], repo_root=self.repo)
            if anomalies:
                lines.append("ordering anomalies:")
                lines.extend("  - " + a for a in anomalies)
            return "\n".join(lines)
        if cmd in ("shm-check", "shm", "shmcheck"):
            from ac_workers import pipeline_timeline

            return pipeline_timeline.shm_collision_report(
                list(range(MAX_WORKERS)), repo_root=self.repo
            )
        if cmd in ("action-diversity", "action_diversity", "actions", "action_source"):
            return self.action_diversity_text()
        if cmd == "worker":
            ids = parse_worker_spec(arg)
            if len(ids) != 1:
                return "usage: worker <worker_id>"
            return self.worker_detail_text(ids[0])
        if cmd == "learner":
            sub = arg.lower()
            if sub == "start":
                return self.learner_start()
            if sub == "stop":
                return self.learner_stop()
            if sub in ("force-stop", "force_stop", "forcestop"):
                return self.learner_stop(force=True)
            if sub == "save":
                return self.learner_save()
            if sub == "status":
                return self.learner_status_text()
            if sub in ("error", "errors", "last-error", "last_error"):
                return self.learner_error_text()
            return "usage: learner start|stop|save|force-stop|status|error"
        if cmd in ("train", "training", "session"):
            return self.handle_training_session_command(arg)
        if cmd in ("wipe", "forget") or (
            cmd == "memory" and arg.lower() in ("", "clear", "wipe", "forget")
        ):
            return self.handle_wipe_memory()
        if cmd == "memory":
            return "usage: memory clear"
        if cmd == "policy":
            sub = arg.lower().strip()
            if sub in ("pause", "resume"):
                return self._policy_command_all(sub)
            return "usage: policy pause | policy resume"
        if cmd == "evaluate":
            from ac_workers.collector_diag import CollectorDiagClient
            worker = int(arg.strip() or "0")
            result = CollectorDiagClient(worker).request("evaluate")
            return "Evaluation requested; frozen policy, separate results." if result.get("ok") else str(result.get("error"))
        if cmd == "evolve":
            sub = arg.lower().strip()
            if sub in ("start", ""):
                return self.evolve_start()
            if sub == "stop":
                return self.evolve_stop()
            if sub in ("status", "stat"):
                return self.evolve_status_text()
            return "usage: evolve start|stop|status"
        if cmd in ("control-mode", "control_mode", "controlmode"):
            return self.set_control_mode(arg.strip())
        if cmd in ("actor-stats", "actor_stats", "actorstats"):
            return self.actor_stats_text()
        if cmd in ("policy-status", "policy_status", "policystatus"):
            return self.policy_status_text()
        if cmd in ("emergency", "emergency-stop", "emergency_stop", "estop"):
            return self.emergency_stop()
        if cmd in ("kill-all", "killall", "kill_all"):
            return self.kill_all()
        if cmd in ("health", "health-check", "system-health", "healthcheck"):
            return self.system_health_text()
        if cmd in ("clean-start", "clean_start", "cleanstart"):
            ids = parse_worker_spec(arg) if arg else list(range(MAX_WORKERS))
            if not ids:
                return "usage: clean-start | clean-start 0-5 | clean-start 0,2,4"
            return self.clean_start(ids)
        if cmd in ("diag-check", "diag_check", "diagcheck"):
            ids = parse_worker_spec(arg) if arg else list(range(MAX_WORKERS))
            if not ids:
                return "usage: diag-check | diag-check 0-5 | diag-check 0,2"
            return self.format_diag_check(ids)
        if cmd in ("diag-ping", "diag_ping", "diagping"):
            ids = parse_worker_spec(arg) if arg else list(range(MAX_WORKERS))
            if not ids:
                return "usage: diag-ping | diag-ping 0 | diag-ping 0-5"
            return self.format_diag_ping(ids)
        if cmd in ("test-start", "test_start", "teststart"):
            ids = parse_worker_spec(arg) if arg else [0]
            if len(ids) != 1:
                return "usage: test-start 0  (single worker only for fast debug)"
            return self.test_start_worker(ids[0])
        if cmd in ("test-health", "test_health", "testhealth"):
            ids = parse_worker_spec(arg) if arg else [0]
            if len(ids) != 1:
                return "usage: test-health 0"
            return self.format_test_health(ids[0])
        if cmd in ("test-stop", "test_stop", "teststop"):
            ids = parse_worker_spec(arg) if arg else [0]
            if len(ids) != 1:
                return "usage: test-stop 0"
            return self.test_stop_worker(ids[0])
        if cmd in ("test-status", "test_status", "teststatus"):
            ids = parse_worker_spec(arg) if arg else [0]
            if len(ids) != 1:
                return "usage: test-status 0"
            return self.format_test_status(ids[0])
        if cmd in ("test-log", "test_log", "testlog"):
            ids = parse_worker_spec(arg) if arg else [0]
            if len(ids) != 1:
                return "usage: test-log 0"
            return self.format_test_log(ids[0])
        if cmd in ("test-cancel", "test_cancel", "testcancel"):
            ids = parse_worker_spec(arg) if arg else [0]
            if len(ids) != 1:
                return "usage: test-cancel 0"
            return self.test_cancel_worker(ids[0])
        if cmd in ("test-pids", "test_pids", "testpids"):
            ids = parse_worker_spec(arg) if arg else [0]
            if len(ids) != 1:
                return "usage: test-pids 0"
            return self.format_test_pids(ids[0])
        if cmd in ("sound", "volume", "audio", "son"):
            from ac_workers.ac_sound import handle_sound_command

            return handle_sound_command(arg)
        if cmd in ("drive-click", "drive_click", "driveclick", "clic-drive"):
            from ac_workers.drive_click import handle_drive_click_command

            return handle_drive_click_command(arg)
        if cmd in ("refresh-rate", "refresh_rate", "refreshrate"):
            arg = (arg or "").strip()
            if not arg:
                return "refresh-rate: {0:.2f}s".format(self._refresh_interval_sec)
            try:
                return self.set_refresh_rate(float(arg))
            except ValueError:
                return "usage: refresh-rate 0.5"
        if cmd in ("identity-test", "identity_test", "identitytest", "identity"):
            return self.identity_test_text(arg)
        if cmd in ("calibrate", "calibration", "calibrer", "test-vjoy", "test-vidjoy"):
            return self.calibration_test_text(arg)
        return "commande inconnue: {0} (help)".format(cmd)

    def calibration_test_text(self, arg: str) -> str:
        """Run the vJoy/car-command calibration (drive 2 m -> brake -> steer
        left/right) against the Control Center's OWN active workers."""
        from ac_workers import calibration_test as ct

        if self._scale_startup_busy() or self._scale_queue:
            queued = len(self._scale_queue)
            total = len(self._scale_targets) or queued
            return (
                "calibrate refused: clean-start/scale-test still in progress "
                "({0}/{1} workers queued or starting). Wait until all workers show "
                "idle/running, then rerun calibrate.".format(queued, total)
            )
        spec = (arg or "").strip()
        if spec:
            ids = parse_worker_spec(spec)
        else:
            ids = [w for w, on in self.active_workers_map().items() if on]
        if not ids:
            return "calibrate: no workers active (Démarrer les workers d'abord)"
        try:
            report = ct.run_all(ids)
        except Exception as exc:
            return "calibrate failed: {0}".format(exc)
        return ct.format_report(report, ids)

    def identity_test_text(self, arg: str) -> str:
        """Run the authoritative worker<->car pulse against the Control Center's
        OWN active workers (not a fragile external discovery)."""
        from ac_workers import identity_test as it

        arg = (arg or "").strip().lower()
        if self._scale_startup_busy() or self._scale_queue:
            queued = len(self._scale_queue)
            total = len(self._scale_targets) or queued
            return (
                "identity-test refused: clean-start/scale-test still in progress "
                "({0}/{1} workers queued or starting). Wait until all workers show "
                "idle/running, then rerun identity-test --pulse.".format(queued, total)
            )
        spec = arg.replace("--pulse", "").replace("--direct", "").replace("--direct-vjoy-pulse", "").strip()
        if spec:
            ids = parse_worker_spec(spec)
        else:
            ids = [w for w, on in self.active_workers_map().items() if on]
            if not ids:
                ids = list(range(MAX_WORKERS))
        if not ids:
            return "identity-test: no workers specified (start a session first)"
        proc_alive = 0
        for wid in ids:
            st = self._worker_status(wid)
            cpid = int(st.get("collector_pid") or 0)
            if cpid > 0 and pid_alive(cpid):
                proc_alive += 1
        diag_ready = sum(1 for w in ids if self.active_workers_map().get(w))
        if proc_alive > 0 and diag_ready == 0:
            return (
                "identity-test refused: PREFLIGHT_BLOCKED — diag ports unreachable even though "
                "{0} worker process(es) are alive. Run `diag-check`, then stop-all + clean-start "
                "(restart C4D if the dashboard still shows actifs without diag).".format(proc_alive)
            )
        try:
            if "--direct" in arg:
                return it.format_direct(it.direct_vjoy_pulse(ids))
            return it.format_pulse(it.pulse(ids))
        except Exception as exc:
            return "identity-test failed: {0}".format(exc)

    def _drain_keys_windows(self, buffer: str, cursor: int) -> tuple[str, int, bool, bool]:
        import msvcrt

        submit = False
        buffer_changed = False
        while msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                special = msvcrt.getwch()
                if special == "K" and cursor > 0:
                    cursor -= 1
                    buffer_changed = True
                elif special == "M" and cursor < len(buffer):
                    cursor += 1
                    buffer_changed = True
                continue
            if ch in ("\r", "\n"):
                submit = True
                break
            if ch == "\x03":
                self._quit = True
                break
            if ch in ("\x08", "\x7f"):
                if cursor > 0:
                    buffer = buffer[: cursor - 1] + buffer[cursor:]
                    cursor -= 1
                    buffer_changed = True
            elif len(ch) == 1 and ch >= " ":
                buffer = buffer[:cursor] + ch + buffer[cursor:]
                cursor += 1
                buffer_changed = True
        return buffer, cursor, buffer_changed, submit

    def _run_input_loop_windows(self, renderer: TerminalRenderer) -> int:
        buffer = ""
        cursor = 0
        last_frame = ""
        last_data_refresh = 0.0
        self.refresh_metrics_cache(force=True)
        workers = self._metrics_workers or []
        global_view = self._metrics_global or self._build_global_view(workers)

        while not self._quit:
            now = time.time()
            data_due = (now - last_data_refresh) >= self._refresh_interval_sec
            bg_changed = self._poll_background_ops()
            buffer, cursor, buffer_changed, submit = self._drain_keys_windows(buffer, cursor)

            if submit:
                line = buffer.strip()
                buffer = ""
                cursor = 0
                if line:
                    result = self.handle_command(line)
                    if result == "__CLEAR__":
                        self._message = ""
                    elif result:
                        self._message = result
                    renderer.reset_cache()
                    self.refresh_metrics_cache(force=True)
                    workers = self._metrics_workers or workers
                    global_view = self._metrics_global or global_view

            if data_due or bg_changed:
                self.refresh_metrics_cache(force=True)
                workers = self._metrics_workers or workers
                global_view = self._metrics_global or global_view
                last_data_refresh = now

            if data_due or buffer_changed or submit or bg_changed:
                frame = self.build_frame(
                    buffer, self._message, workers=workers, global_view=global_view
                )
                if frame != last_frame:
                    renderer.paint(frame)
                    last_frame = frame
            else:
                time.sleep(KEY_POLL_SLEEP_SEC)

        return 0

    def _run_input_loop_fallback(self, renderer: TerminalRenderer) -> int:
        """Non-Windows: blocking input, no background refresh thread."""
        renderer.paint(self.build_frame("", self._message))
        while not self._quit:
            try:
                line = input("C4D> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            result = self.handle_command(line)
            if result == "__CLEAR__":
                self._message = ""
            elif result:
                self._message = result
            renderer.reset_cache()
            renderer.paint(self.build_frame("", self._message))
        return 0

    def run_interactive(self) -> int:
        enable_ansi_stdout()
        renderer = TerminalRenderer()
        try:
            if os.name == "nt":
                return self._run_input_loop_windows(renderer)
            return self._run_input_loop_fallback(renderer)
        finally:
            sys.stdout.write(ANSI_SHOW_CURSOR)
            sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    import argparse

    for p in (REPO, REPO / "assetto_corsa_gym"):
        sp = str(p)
        if sp not in sys.path:
            sys.path.insert(0, sp)

    parser = argparse.ArgumentParser(prog="control_center")
    sub = parser.add_subparsers(dest="mode")
    p_server = sub.add_parser("server", help="central C4D server (localhost only)")
    p_server.add_argument("--host", default="127.0.0.1")
    p_server.add_argument("--port", type=int, default=24700)
    p_server.add_argument("--ready-file", type=Path, default=None)
    p_server.add_argument("--repo-root", type=Path, default=REPO)
    p_server.add_argument("--session-token", default="")
    p_server.add_argument("--session-file", type=Path, default=None)
    p_dash = sub.add_parser("dashboard", help="read-only dashboard client")
    p_dash.add_argument("--host", default="127.0.0.1")
    p_dash.add_argument("--port", type=int, default=24700)
    p_dash.add_argument("--repo-root", type=Path, default=REPO)
    p_dash.add_argument("--session-token", default="")
    p_console = sub.add_parser("console", help="command console client")
    p_console.add_argument("--host", default="127.0.0.1")
    p_console.add_argument("--port", type=int, default=24700)
    p_console.add_argument("--repo-root", type=Path, default=REPO)
    p_console.add_argument("--session-token", default="")
    args = parser.parse_args(argv)

    if args.mode == "dashboard":
        from ac_workers.c4d_ipc import run_dashboard_client

        return run_dashboard_client(
            args.host,
            args.port,
            token=args.session_token,
            repo_root=args.repo_root,
        )
    if args.mode == "console":
        from ac_workers.c4d_ipc import run_console_client

        return run_console_client(
            args.host,
            args.port,
            token=args.session_token,
            repo_root=args.repo_root,
        )
    if args.mode == "server":
        from ac_workers.c4d_ipc import C4DControlServer
        from ac_workers.c4d_server_lock import ServerRuntimeLock

        repo_root = Path(args.repo_root).resolve()
        runtime_lock = ServerRuntimeLock(repo_root, args.host, args.port, args.session_token)
        ok, reason = runtime_lock.acquire()
        if not ok:
            print("C4D server start refused: {0}".format(reason), file=sys.stderr)
            return 2
        center = ControlCenter(repo_root)
        server = C4DControlServer(
            center,
            host=args.host,
            port=args.port,
            session_token=args.session_token,
            repo_root=repo_root,
            session_file=args.session_file,
            runtime_lock=runtime_lock,
        )
        return server.run(ready_file=args.ready_file)

    center = ControlCenter()
    return center.run_interactive()


if __name__ == "__main__":
    raise SystemExit(main())
