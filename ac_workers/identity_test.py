"""Live worker<->car correspondence test (vJoy->car BINDING proof).

The static map in ``worker_identity.py`` proves STRUCTURE (each worker owns a
distinct vJoy device, distinct ports, distinct seed, distinct local actor). It
cannot prove which *physical car* a vJoy device actually drives, because Assetto
Corsa binds vJoy devices in controls.ini by product GUID (identical across all
vJoy devices) plus an enumeration index (JOY=N). If that index is wrong, worker N
writes to vJoy device N+1 but a *different* car reacts — the "all cars
interconnected" symptom.

This module proves the binding with the only authority that exists for it: the
live game. Two modes, both safe for the learner/replay buffer:

  observe  (default, fully read-only): sample every worker's COMMANDED steer
           (physical_action[0]) and its OBSERVED car steer (telemetry steer) over
           a window, then build a command->car correlation matrix. The diagonal
           must dominate. A worker whose commands correlate with another worker's
           car (or with every car) is cross-wired. Touches nothing.

  pulse    (--pulse, opt-in): one car at a time, pause every worker, then fire a
           short steer pulse on worker T's own vJoy via the existing
           ``safe_action_test`` diagnostic (which never pushes a transition), and
           record which car physically moved. Restores the prior pause/resume
           state at the end. Decisive, and the replay buffer is never written.

Neither mode ever sends an action to a car from the learner, and neither writes
to the replay buffer.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from ac_workers.collector_diag import CollectorDiagClient, DiagRequestError
from ac_workers.vjoy_pool import MAX_WORKERS
from ac_workers.worker_collector import BINDING_READY_PHASES

DEFAULT_SAMPLE_HZ = 10.0
DEFAULT_OBSERVE_SEC = 6.0
# Minimum command activity (std of commanded steer) for a worker's row to be
# trustworthy. A worker holding neutral cannot be correlated to any car.
MIN_COMMAND_STD = 0.02
# Correlation at/above this is treated as "this command drives this car".
CORR_MATCH = 0.6
# Pulse-mode motion threshold: |steer| change beyond this means the car moved.
PULSE_MOTION_EPS = 0.02


@dataclass
class WorkerSample:
    worker_id: int
    ts: float
    commanded_steer: float
    observed_steer: float
    observed_gas: float
    speed: float
    phase: str
    started: bool
    policy_paused: bool


def _status_via_diag(worker_id: int, timeout: float = 2.0) -> dict[str, Any]:
    try:
        return CollectorDiagClient(worker_id, timeout=timeout).request("status")
    except DiagRequestError:
        return {}


def _sample_worker(worker_id: int) -> WorkerSample | None:
    snap = _status_via_diag(worker_id)
    if not snap or not snap.get("ok", True):
        # Some collectors answer status without an explicit ok; accept any dict
        # that carries a telemetry snapshot.
        if not snap or "telemetry_snapshot" not in snap:
            return None
    telem = snap.get("telemetry_snapshot") or {}
    phys = snap.get("physical_action") or snap.get("last_executed_action") or []
    commanded_steer = float(phys[0]) if phys else 0.0
    return WorkerSample(
        worker_id=worker_id,
        ts=time.time(),
        commanded_steer=commanded_steer,
        observed_steer=float(telem.get("steer", 0.0) or 0.0),
        observed_gas=float(telem.get("gas", 0.0) or 0.0),
        speed=float(telem.get("speed", 0.0) or 0.0),
        phase=str(snap.get("phase", "")),
        started=bool(snap.get("started", False)),
        policy_paused=bool(snap.get("policy_paused", False)),
    )


def _live_workers(worker_ids: list[int]) -> list[int]:
    live: list[int] = []
    for wid in worker_ids:
        # One retry: the very first diag connect after process start can time out
        # cold, which would otherwise hide a perfectly live collector.
        if _sample_worker(wid) is not None or _sample_worker(wid) is not None:
            live.append(wid)
    return live


TELEMETRY_FRESH_MS = 2000.0
# Motion threshold (|delta| of steer/gas/brake) that counts as "the car moved".
PULSE_MOTION_EPS = 0.02
# Status files C4D / worker_identity read (what the dashboard believes is running).
_STATUS_DIR = REPO / "ac_workers" / "status"
_STATUS_FRESH_SEC = 30.0


def _diag_status_ok(snap: dict[str, Any], worker_id: int) -> bool:
    """True only when the diag endpoint returned a real collector status snapshot."""
    if not snap or snap.get("ok") is False:
        return False
    if snap.get("worker_id") is not None and int(snap.get("worker_id")) != int(worker_id):
        return False
    if not snap.get("collector_pid"):
        return False
    if not str(snap.get("boot_id", "") or "").strip():
        return False
    return True


def _format_failure_detail(resp: dict[str, Any], indent: str = "       ") -> list[str]:
    """Expand a failed pulse/selftest response into human-readable diagnostic lines."""
    lines: list[str] = []
    err = str(resp.get("error", "") or "")
    if err:
        lines.append("{0}error: {1}".format(indent, err))
    for key in (
        "writer",
        "action_sent",
        "vjoy_device_id",
        "last_vjoy_write_device",
        "vjoy_device_acquired",
        "vjoy_acquired_flag",
        "vjoy_handle_status",
        "local_controls_id",
        "vjoy_object_id",
        "device_status_before_update",
        "canonical_result",
        "steps",
        "phase",
        "env_ready",
        "diag_listener_ready",
        "diag_thread_error",
    ):
        if key in resp and resp[key] not in (None, "", {}):
            lines.append("{0}{1}: {2}".format(indent, key, resp[key]))
    trace = str(resp.get("trace", "") or "").strip()
    if trace:
        lines.append("{0}traceback:".format(indent))
        for tline in trace.splitlines():
            lines.append("{0}  {1}".format(indent, tline))
    return lines


def _read_status_file(worker_id: int) -> dict[str, Any]:
    """What C4D / worker_identity see for this worker (the status JSON on disk)."""
    path = _STATUS_DIR / "worker_{0}.json".format(worker_id)
    try:
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


@dataclass
class Identity:
    """Live identity of a collector as seen through its diag endpoint, cross-checked
    against the on-disk status file C4D uses. The point: never pulse a stale port /
    wrong process, and surface DIAG_STATE_MISMATCH instead of a false 'not started'.
    """

    worker_id: int
    diag_reachable: bool = False
    collector_pid: int | None = None
    boot_id: str = ""
    phase: str = ""
    vjoy_device_id: int | None = None
    vjoy_acquired: bool = False
    telemetry_age_ms: float = float("inf")
    # From the on-disk status file (what C4D believes):
    file_present: bool = False
    file_fresh: bool = False
    file_collector_pid: int | None = None
    file_pid_alive: bool | None = None
    status_file_path: str = ""
    diag_port: int = 0
    state: str = "UNKNOWN"          # LIVE | DIAG_STATE_MISMATCH | OFFLINE
    note: str = ""

    @property
    def telemetry_fresh(self) -> bool:
        return self.telemetry_age_ms < TELEMETRY_FRESH_MS


def _pid_alive(pid: int | None) -> bool | None:
    if not pid:
        return None
    try:
        from ac_workers.control_center import pid_alive

        return bool(pid_alive(int(pid)))
    except Exception:
        return None


def _identity_probe(worker_id: int) -> Identity:
    """Probe the diag endpoint and reconcile it with the on-disk status file.

    States:
      LIVE                 : diag answers; if a status file exists its pid matches.
      DIAG_STATE_MISMATCH  : C4D's status file says this worker is running (fresh,
                             has a pid) but the diag endpoint is unreachable or
                             reports a DIFFERENT collector_pid — we are NOT talking
                             to the process C4D thinks is live.
      OFFLINE              : neither a fresh status file nor a reachable diag.
    """
    ident = Identity(worker_id=worker_id)
    ident.diag_port = 24100 + worker_id
    ident.status_file_path = str(_STATUS_DIR / "worker_{0}.json".format(worker_id))
    file_st = _read_status_file(worker_id)
    if file_st:
        ident.file_present = True
        ident.file_collector_pid = file_st.get("collector_pid")
        ident.file_pid_alive = _pid_alive(ident.file_collector_pid)
        ts = file_st.get("ts") or file_st.get("status_written_at_epoch")
        # status_written_at is ISO; fall back to mtime-free freshness via heartbeat.
        hb_age = file_st.get("heartbeat_age_ms")
        if isinstance(hb_age, (int, float)):
            ident.file_fresh = hb_age < _STATUS_FRESH_SEC * 1000.0
        elif isinstance(ts, (int, float)):
            ident.file_fresh = (time.time() - float(ts)) < _STATUS_FRESH_SEC

    snap = _status_via_diag(worker_id)
    if _diag_status_ok(snap, worker_id):
        ident.diag_reachable = True
        ident.collector_pid = snap.get("collector_pid")
        ident.boot_id = str(snap.get("boot_id", "") or "")
        ident.phase = str(snap.get("phase", "") or "")
        ident.vjoy_device_id = snap.get("vjoy_device") or snap.get("vjoy_device_id")
        ident.vjoy_acquired = bool(snap.get("vjoy_device_acquired", False))
        ident.telemetry_age_ms = float(snap.get("telemetry_age_ms", float("inf")))
        if snap.get("diag_listener_ready") is False:
            ident.state = "DIAG_STATE_MISMATCH"
            ident.note = (
                "diag port answers but listener_ready=False inside collector "
                "(diag thread error: {0})".format(snap.get("diag_thread_error") or "?")
            )
            return ident
    elif snap:
        err = str(snap.get("error") or "")
        if snap.get("ok") is False and err:
            ident.note = (
                "diag port {0} answered but handler failed: {1}".format(ident.diag_port, err)
            )
        else:
            ident.note = (
                "diag port {0} answered but status incomplete (missing pid/boot_id) — "
                "collector likely still starting".format(ident.diag_port)
            )

    # Reconcile.
    if ident.diag_reachable:
        if (
            ident.file_present
            and ident.file_collector_pid is not None
            and ident.collector_pid is not None
            and int(ident.file_collector_pid) != int(ident.collector_pid)
        ):
            ident.state = "DIAG_STATE_MISMATCH"
            ident.note = (
                "status file pid={0} != live diag pid={1} (talking to a DIFFERENT "
                "process than C4D thinks); status_file={2}".format(
                    ident.file_collector_pid, ident.collector_pid, ident.status_file_path
                )
            )
        else:
            ident.state = "LIVE"
    else:
        if ident.file_present and ident.file_fresh and ident.file_collector_pid:
            alive = ident.file_pid_alive
            if alive:
                hint = (
                    "process pid={0} is ALIVE but diag port {1} refuses connections "
                    "-> diag server likely crashed/not listening in that process".format(
                        ident.file_collector_pid, ident.diag_port
                    )
                )
            else:
                hint = (
                    "process pid={0} is NOT alive -> C4D status file is stale (worker "
                    "died); rerun after a clean restart".format(ident.file_collector_pid)
                )
            ident.state = "DIAG_STATE_MISMATCH"
            ident.note = (
                "C4D status file shows worker running (pid={0}, fresh) but diag port "
                "{1} unreachable. {2}. status_file={3}".format(
                    ident.file_collector_pid, ident.diag_port, hint, ident.status_file_path
                )
            )
        else:
            ident.state = "OFFLINE"
            ident.note = (
                "no reachable diag (port {0}) and no fresh status file ({1})".format(
                    ident.diag_port, ident.status_file_path
                )
            )
    return ident


def _diag_pulse(worker_id: int, payload: dict[str, Any] | None = None, timeout: float = 6.0) -> dict[str, Any]:
    """Call the authoritative collector-side vJoy pulse (diagnostic_vjoy_pulse)."""
    try:
        resp = CollectorDiagClient(worker_id, timeout=timeout).request(
            "diagnostic_vjoy_pulse", payload or {}
        )
    except DiagRequestError as exc:
        return {"ok": False, "write_ok": False, "error": str(exc), "unreachable": True}
    err = str(resp.get("error", "") or "")
    if not resp.get("write_ok") and ("unknown" in err.lower() or "not supported" in err.lower()):
        resp["error"] = (
            "collector predates the authoritative pulse (no diagnostic_vjoy_pulse handler) "
            "— restart this worker (clean-start) to load the new code"
        )
        resp["stale_code"] = True
    return resp


def _diag_selftest(worker_id: int, timeout: float = 5.0) -> dict[str, Any]:
    try:
        return CollectorDiagClient(worker_id, timeout=timeout).request("diag_selftest_vjoy_write")
    except DiagRequestError as exc:
        return {"ok": False, "write_ok": False, "error": str(exc), "unreachable": True}


@dataclass
class BindingGateResult:
    worker_ids: list[int]
    ready: bool = False
    blockers: list[str] = field(default_factory=list)
    identities: dict[int, Identity] = field(default_factory=dict)
    selftests: dict[int, dict[str, Any]] = field(default_factory=dict)


def gate_binding_test(worker_ids: list[int]) -> BindingGateResult:
    """Preflight: every worker must be diag-LIVE (pid/boot_id), in a ready phase, and
    pass vJoy selftest before we attempt a car-binding pulse."""
    result = BindingGateResult(worker_ids=list(worker_ids))
    result.identities = {w: _identity_probe(w) for w in worker_ids}

    for wid in worker_ids:
        ident = result.identities[wid]
        if ident.state == "DIAG_STATE_MISMATCH":
            result.blockers.append("W{0}: DIAG_STATE_MISMATCH — {1}".format(wid, ident.note))
            continue
        if ident.state == "OFFLINE":
            result.blockers.append("W{0}: OFFLINE — {1}".format(wid, ident.note))
            continue
        if not ident.diag_reachable:
            result.blockers.append(
                "W{0}: diag port {1} not ready (no pid/boot_id in status) — {2}".format(
                    wid, ident.diag_port, ident.note or "still starting?"
                )
            )
            continue
        if ident.phase and ident.phase not in BINDING_READY_PHASES:
            result.blockers.append(
                "W{0}: phase={1!r} (need {2}) — wait for clean-start to finish".format(
                    wid, ident.phase, ",".join(sorted(BINDING_READY_PHASES))
                )
            )
            continue
        if not ident.vjoy_acquired:
            result.blockers.append("W{0}: vJoy device not acquired yet".format(wid))

    if result.blockers:
        return result

    for wid in worker_ids:
        st = _diag_selftest(wid)
        result.selftests[wid] = st
        if st.get("unreachable"):
            result.blockers.append(
                "W{0}: vJoy selftest comms error — {1}".format(wid, st.get("error", "?"))
            )
            continue
        pre, _detail = _classify_pulse_response(st)
        if pre == V_STALE_CODE:
            result.blockers.append("W{0}: collector predates selftest handler — clean-start".format(wid))
            continue
        if not st.get("write_ok"):
            summary = st.get("error") or "selftest write failed"
            result.blockers.append("W{0}: vJoy selftest FAILED — {1}".format(wid, summary))

    result.ready = len(result.blockers) == 0
    return result


def format_binding_gate(gate: BindingGateResult) -> str:
    lines = ["binding test preflight (diag-live + vJoy selftest required before car pulse):"]
    lines.append(format_identity(gate.identities))
    if gate.selftests:
        lines.append("")
        lines.append("  vJoy selftest:")
        for wid in gate.worker_ids:
            st = gate.selftests.get(wid)
            if not st:
                continue
            lines.append(
                "  W{0}  write_ok={1}  phase={2}".format(
                    wid, ("yes" if st.get("write_ok") else "NO"), st.get("phase", "?")
                )
            )
            if not st.get("write_ok"):
                lines.extend(_format_failure_detail(st))
    if gate.blockers:
        lines.append("")
        lines.append("  BLOCKED — binding NOT tested:")
        for b in gate.blockers:
            lines.append("    ! " + b)
    else:
        lines.append("")
        lines.append("  preflight OK: all workers diag-live and vJoy selftest passed.")
    return "\n".join(lines)


def format_identity(identities: dict[int, Identity]) -> str:
    lines = ["collector identity (diag endpoint reconciled with C4D status file):"]
    lines.append("  W  state               diag  pid       boot_id      phase            vjoy  telem")
    for wid in sorted(identities):
        i = identities[wid]
        age = "inf" if i.telemetry_age_ms == float("inf") else "{0:.0f}ms".format(i.telemetry_age_ms)
        lines.append(
            "  {wid:<2} {state:<19} {diag:<5} {pid:<9} {boot:<12} {phase:<16} {vjoy:<5} {age}".format(
                wid=wid,
                state=i.state,
                diag=("yes" if i.diag_reachable else "NO"),
                pid=str(i.collector_pid or "-"),
                boot=(i.boot_id or "-")[:12],
                phase=(i.phase or "-")[:16],
                vjoy=str(i.vjoy_device_id if i.vjoy_device_id is not None else "-"),
                age=age,
            )
        )
        if i.note:
            lines.append("       - {0}".format(i.note))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# observe mode (read-only)
# --------------------------------------------------------------------------- #
@dataclass
class ObserveResult:
    worker_ids: list[int]
    commanded: dict[int, list[float]] = field(default_factory=dict)
    observed: dict[int, list[float]] = field(default_factory=dict)
    command_std: dict[int, float] = field(default_factory=dict)
    corr_matrix: dict[int, dict[int, float]] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)

    @property
    def trustworthy_rows(self) -> list[int]:
        return [w for w in self.worker_ids if self.command_std.get(w, 0.0) >= MIN_COMMAND_STD]


def observe(
    worker_ids: list[int],
    *,
    duration_sec: float = DEFAULT_OBSERVE_SEC,
    sample_hz: float = DEFAULT_SAMPLE_HZ,
) -> ObserveResult:
    """Sample commanded vs observed steer for every worker; correlate (read-only)."""
    interval = 1.0 / max(1.0, sample_hz)
    cmd: dict[int, list[float]] = {w: [] for w in worker_ids}
    obs: dict[int, list[float]] = {w: [] for w in worker_ids}
    deadline = time.monotonic() + duration_sec
    while time.monotonic() < deadline:
        for wid in worker_ids:
            s = _sample_worker(wid)
            if s is None:
                cmd[wid].append(float("nan"))
                obs[wid].append(float("nan"))
            else:
                cmd[wid].append(s.commanded_steer)
                obs[wid].append(s.observed_steer)
        time.sleep(interval)

    result = ObserveResult(worker_ids=list(worker_ids), commanded=cmd, observed=obs)
    for wid in worker_ids:
        arr = np.asarray(cmd[wid], dtype=np.float64)
        arr = arr[~np.isnan(arr)]
        result.command_std[wid] = float(np.std(arr)) if arr.size else 0.0

    for ci in worker_ids:
        result.corr_matrix[ci] = {}
        cmd_i = np.asarray(cmd[ci], dtype=np.float64)
        for cj in worker_ids:
            obs_j = np.asarray(obs[cj], dtype=np.float64)
            corr = _safe_corr(cmd_i, obs_j)
            result.corr_matrix[ci][cj] = corr

    _flag_observe_violations(result)
    return result


def _safe_corr(a: np.ndarray, b: np.ndarray) -> float:
    mask = ~(np.isnan(a) | np.isnan(b))
    a, b = a[mask], b[mask]
    if a.size < 3 or np.std(a) < 1e-6 or np.std(b) < 1e-6:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def _flag_observe_violations(result: ObserveResult) -> None:
    for wid in result.trustworthy_rows:
        row = result.corr_matrix[wid]
        self_corr = row.get(wid, float("nan"))
        # Other cars that track this worker's command as strongly as its own car.
        cross = [
            cj
            for cj in result.worker_ids
            if cj != wid
            and not np.isnan(row.get(cj, float("nan")))
            and row[cj] >= CORR_MATCH
        ]
        if not np.isnan(self_corr) and self_corr >= CORR_MATCH and not cross:
            continue  # clean: only its own car follows it
        if cross:
            result.violations.append(
                "worker {0} command correlates with car(s) {1} (self={2}) -> cross-wired vJoy binding".format(
                    wid, cross, _fmt(self_corr)
                )
            )
        elif np.isnan(self_corr) or self_corr < CORR_MATCH:
            result.violations.append(
                "worker {0} command does NOT track its own car (self_corr={1}); "
                "its vJoy device may drive a different car".format(wid, _fmt(self_corr))
            )


def _fmt(x: float) -> str:
    return "nan" if x != x else "{0:+.2f}".format(x)


def format_observe(result: ObserveResult) -> str:
    lines = ["command->car steer correlation (observe mode, read-only):"]
    header = "  cmd\\car " + " ".join("  W{0}  ".format(w) for w in result.worker_ids)
    lines.append(header)
    for ci in result.worker_ids:
        cells = []
        for cj in result.worker_ids:
            cells.append("{0:>6}".format(_fmt(result.corr_matrix[ci][cj])))
        std = result.command_std.get(ci, 0.0)
        tag = "" if std >= MIN_COMMAND_STD else " (idle/neutral - row not trusted)"
        lines.append("  W{0:<6}".format(ci) + " ".join(cells) + tag)
    lines.append("")
    lines.append("  expectation: the diagonal (Wi cmd vs Wi car) dominates; off-diagonal ~0.")
    if result.violations:
        lines.append("  VIOLATIONS:")
        for v in result.violations:
            lines.append("    ! " + v)
    elif result.trustworthy_rows:
        lines.append("  binding OK for active workers: each command drives only its own car.")
    else:
        lines.append(
            "  no worker steered enough to correlate (all idle/neutral) — rerun while driving "
            "or use --pulse."
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# pulse mode (opt-in, decisive)
# --------------------------------------------------------------------------- #
@dataclass
class PulseTarget:
    target: int
    verdict: str = "?"                  # one of the V_* categories
    detail: str = ""
    write_ok: bool = False
    collector_pid: int | None = None
    boot_id: str = ""
    vjoy_device_id: int | None = None
    last_vjoy_write_device: int | None = None
    error: str = ""
    failure_resp: dict[str, Any] = field(default_factory=dict)
    self_motion: float = 0.0            # peak telemetry delta on target's OWN car
    self_fresh: bool = False
    other_motion: dict[int, float] = field(default_factory=dict)
    other_fresh: dict[int, bool] = field(default_factory=dict)


@dataclass
class PulseResult:
    worker_ids: list[int]
    identities: dict[int, Identity] = field(default_factory=dict)
    targets: dict[int, PulseTarget] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)
    gate_blocked: bool = False
    gate_blockers: list[str] = field(default_factory=list)
    selftests: dict[int, dict[str, Any]] = field(default_factory=dict)


# Authoritative per-target verdicts (exactly the categories the user asked for).
V_WRITE_FAILED = "PULSE_WRITE_FAILED"
V_NO_CAR = "PULSE_WRITE_OK_BUT_NO_CAR_MOVED"
V_WRONG_CAR = "PULSE_WRITE_OK_AND_WRONG_CAR_MOVED"
V_OWN_CAR = "PULSE_WRITE_OK_AND_OWN_CAR_MOVED"
V_UNKNOWN = "PULSE_WRITE_OK_CAR_MOTION_UNKNOWN"
V_DIAG_MISMATCH = "DIAG_STATE_MISMATCH"
V_OFFLINE = "OFFLINE"
# These three are TEST-STATE problems, never a binding conclusion:
V_DIAG_COMMS = "DIAG_COMMS_ERROR"          # socket reset/timeout talking to diag
V_STALE_CODE = "COLLECTOR_PREDATES_PULSE"  # old collector, no pulse handler
V_ENV_NOT_READY = "ENV_NOT_READY"          # collector up but env/vjoy not bound yet

# A binding conclusion is only possible when the vJoy write actually happened.
_WROTE_OK_VERDICTS = frozenset({V_NO_CAR, V_WRONG_CAR, V_OWN_CAR, V_UNKNOWN})
# Verdicts that prove a real wrong/missing binding (write succeeded but car wrong).
_BINDING_FAIL_VERDICTS = frozenset({V_WRONG_CAR, V_NO_CAR})


def _classify_pulse_response(resp: dict[str, Any]) -> tuple[str, str]:
    """Map a diagnostic_vjoy_pulse response to a (verdict, detail) BEFORE looking at
    cross-car motion. Separates comms / stale-code / env-not-ready / write-failed so a
    socket error is NEVER reported as a write failure or a binding problem."""
    err = str(resp.get("error", "") or "")
    if resp.get("stale_code"):
        return V_STALE_CODE, err
    if resp.get("unreachable"):
        return V_DIAG_COMMS, "diag comms error during pulse: {0}".format(err)
    if resp.get("env_ready") is False or err.startswith("ENV_NOT_READY"):
        return V_ENV_NOT_READY, err or "collector env/vjoy not bound yet"
    if not resp.get("write_ok"):
        detail_lines = _format_failure_detail(resp)
        detail = "\n".join(detail_lines) if detail_lines else (err or "collector could not write its vJoy device")
        return V_WRITE_FAILED, detail
    return "", ""  # write_ok: caller decides own/wrong/no-car from motion


def _car_motion(worker_id: int) -> tuple[float, bool]:
    """Return (peak |steer/gas/brake|, telemetry_fresh) for a worker's observed car."""
    snap = _status_via_diag(worker_id)
    if not snap:
        return 0.0, False
    age = float(snap.get("telemetry_age_ms", float("inf")))
    telem = snap.get("telemetry_snapshot") or {}
    peak = max(
        abs(float(telem.get("steer", 0.0) or 0.0)),
        abs(float(telem.get("gas", 0.0) or 0.0)),
        abs(float(telem.get("brake", 0.0) or 0.0)),
    )
    return peak, age < TELEMETRY_FRESH_MS


def pulse(
    worker_ids: list[int],
    *,
    pulse_sec: float = 2.0,
    sample_hz: float = DEFAULT_SAMPLE_HZ,
    settle_sec: float = 0.4,
) -> PulseResult:
    """Authoritative one-at-a-time pulse: the COLLECTOR writes to its own vJoy.

    For each target T we call ``diagnostic_vjoy_pulse`` (collector-side, bypasses
    policy/learner/replay/phase). While the collector holds the action we poll every
    worker's observed-car telemetry to see which car physically reacted. The result
    is NEVER a silent 0.00: each target gets one of the explicit verdicts
    (WRITE_FAILED / WRITE_OK_BUT_NO_CAR_MOVED / WRITE_OK_AND_WRONG_CAR_MOVED /
    WRITE_OK_AND_OWN_CAR_MOVED / DIAG_STATE_MISMATCH).
    """
    import threading

    result = PulseResult(worker_ids=list(worker_ids))
    gate = gate_binding_test(worker_ids)
    result.identities = gate.identities
    result.selftests = gate.selftests
    if not gate.ready:
        result.gate_blocked = True
        result.gate_blockers = list(gate.blockers)
        return result

    # Best-effort quiesce: pause every reachable worker so its policy loop holds
    # neutral and cannot move its car during another target's pulse (which would
    # look like cross-talk). This is NOT a gate — failure to pause never blocks the
    # pulse. We restore prior pause state at the end.
    prior_paused: dict[int, bool] = {}
    for w in worker_ids:
        snap = _status_via_diag(w)
        prior_paused[w] = bool(snap.get("policy_paused", False)) if snap else True
        if result.identities[w].state == "LIVE":
            _diag_cmd_simple(w, "pause")
    time.sleep(settle_sec)

    for target in worker_ids:
        ident = result.identities[target]
        tr = PulseTarget(target=target)
        tr.collector_pid = ident.collector_pid
        tr.boot_id = ident.boot_id
        tr.vjoy_device_id = ident.vjoy_device_id
        if ident.state == V_DIAG_MISMATCH:
            tr.verdict = V_DIAG_MISMATCH
            tr.detail = ident.note
            result.targets[target] = tr
            continue
        if ident.state == V_OFFLINE:
            tr.verdict = V_OFFLINE
            tr.detail = ident.note
            result.targets[target] = tr
            continue

        # The diag server is single-threaded and the pulse holds runtime.lock for the
        # whole hold; polling the TARGET's own port during its pulse would queue behind
        # it and reset the socket ([Errno 22]/10054). So we ONLY sample the OTHER
        # workers concurrently. The target's own-car motion comes from the collector's
        # authoritative before/after (car_moved/car_motion) inside the pulse response.
        others = [w for w in worker_ids if w != target]
        baseline = {w: _car_motion(w)[0] for w in others}
        peak = {w: 0.0 for w in others}
        fresh = {w: False for w in others}

        pulse_resp: dict[str, Any] = {}
        stop_sampler = threading.Event()

        def _sample_others() -> None:
            interval = 1.0 / max(1.0, sample_hz)
            while not stop_sampler.is_set():
                for w in others:
                    mv, fr = _car_motion(w)
                    if fr:
                        fresh[w] = True
                    dev = abs(mv - baseline[w])
                    if dev > peak[w]:
                        peak[w] = dev
                time.sleep(interval)

        sampler = threading.Thread(target=_sample_others, daemon=True)
        sampler.start()
        pulse_resp.update(
            _diag_pulse(
                target,
                {"steer": 0.6, "throttle": 0.0, "brake": 1.0, "hold_sec": pulse_sec},
                timeout=pulse_sec + 5.0,
            )
        )
        stop_sampler.set()
        sampler.join(timeout=2.0)

        tr.write_ok = bool(pulse_resp.get("write_ok"))
        tr.collector_pid = pulse_resp.get("collector_pid", tr.collector_pid)
        tr.boot_id = str(pulse_resp.get("boot_id", tr.boot_id) or "")
        tr.vjoy_device_id = pulse_resp.get("vjoy_device_id", tr.vjoy_device_id)
        tr.last_vjoy_write_device = pulse_resp.get("last_vjoy_write_device")
        tr.error = str(pulse_resp.get("error", "") or "")
        tr.failure_resp = dict(pulse_resp)
        tr.self_motion = float(pulse_resp.get("car_motion", 0.0) or 0.0)
        tr.self_fresh = bool(pulse_resp.get("telemetry_fresh"))
        tr.other_motion = dict(peak)
        tr.other_fresh = dict(fresh)

        # First, separate comms / stale / env-not-ready / write-failed. None of these
        # are a binding conclusion.
        pre_verdict, pre_detail = _classify_pulse_response(pulse_resp)
        if pre_verdict:
            tr.verdict = pre_verdict
            tr.detail = pre_detail
            result.targets[target] = tr
            _diag_cmd_neutralize(target)
            continue

        # write_ok is True -> we can now judge the binding from motion.
        own_moved_resp = pulse_resp.get("car_moved")
        self_moved = own_moved_resp is True
        wrong = [
            w for w, mv in tr.other_motion.items()
            if tr.other_fresh.get(w, False) and mv >= PULSE_MOTION_EPS
        ]
        any_fresh = (own_moved_resp is not None) or any(tr.other_fresh.values())

        if not any_fresh:
            tr.verdict = V_UNKNOWN
            tr.detail = (
                "vJoy write OK but no fresh car telemetry to confirm motion "
                "(AC not sending telemetry / car in menu)"
            )
        elif wrong and not self_moved:
            tr.verdict = V_WRONG_CAR
            tr.detail = "moved car(s) {0}, NOT its own".format(["W{0}".format(w) for w in wrong])
        elif wrong and self_moved:
            tr.verdict = V_WRONG_CAR
            tr.detail = "moved its own car AND car(s) {0} (shared/cross-wired)".format(
                ["W{0}".format(w) for w in wrong]
            )
        elif self_moved:
            tr.verdict = V_OWN_CAR
        elif own_moved_resp is False:
            tr.verdict = V_NO_CAR
            tr.detail = "vJoy write OK but observed car did not move -> input not reaching the car"
        else:
            tr.verdict = V_UNKNOWN
            tr.detail = "vJoy write OK but own-car motion could not be confirmed"

        result.targets[target] = tr
        _diag_cmd_neutralize(target)

    # Restore: resume workers that were driving before the test.
    for w in worker_ids:
        if result.identities[w].state != "LIVE":
            continue
        if prior_paused.get(w, True):
            _diag_cmd_neutralize(w)
        else:
            _diag_cmd_simple(w, "resume")

    _flag_pulse_violations(result)
    return result


def _diag_cmd_simple(worker_id: int, cmd: str) -> dict[str, Any]:
    try:
        return CollectorDiagClient(worker_id).request(cmd)
    except DiagRequestError as exc:
        return {"ok": False, "error": str(exc)}


def _diag_cmd_neutralize(worker_id: int) -> None:
    try:
        CollectorDiagClient(worker_id).request("neutralize")
    except DiagRequestError:
        pass


def _flag_pulse_violations(result: PulseResult) -> None:
    """Only a SUCCESSFUL vJoy write that moved the wrong car (or no car) is a binding
    violation. write-failed / comms / stale / env-not-ready / mismatch are test-state
    problems and must NEVER be reported as a binding violation."""
    for target, tr in result.targets.items():
        if tr.verdict == V_WRONG_CAR:
            result.violations.append("worker {0}: {1} ({2})".format(target, tr.verdict, tr.detail))
        elif tr.verdict == V_NO_CAR:
            result.violations.append(
                "worker {0}: {1} — vJoy device {2} write succeeded but input is not "
                "reaching its car".format(target, tr.verdict, target + 1)
            )


def _gate_blocked_verdict(gate: BindingGateResult) -> str:
    blockers = gate.blockers
    if blockers and all(
        "DIAG_STATE_MISMATCH" in b or "unreachable" in b.lower() or "diag_" in b.lower()
        for b in blockers
    ):
        return (
            "  VERDICT: PREFLIGHT_BLOCKED — diag ports unreachable even though workers may be "
            "marked active in C4D. Run `diag-check`, then stop-all + clean-start "
            "(restart C4D after code update)."
        )
    return (
        "  VERDICT: PREFLIGHT_BLOCKED — wait for clean-start to finish (all workers "
        "diag-live + vJoy selftest OK) before binding can be tested."
    )


def format_pulse(result: PulseResult) -> str:
    lines = ["AUTHORITATIVE pulse (collector writes its own vJoy via the normal driving writer):"]
    if result.gate_blocked:
        gate = BindingGateResult(
            worker_ids=result.worker_ids,
            ready=False,
            blockers=result.gate_blockers,
            identities=result.identities,
            selftests=result.selftests,
        )
        lines.append(format_binding_gate(gate))
        lines.append("")
        lines.append(_gate_blocked_verdict(gate))
        return "\n".join(lines)

    lines.append(format_identity(result.identities))
    lines.append("")
    lines.append("  W   write_ok  vjoy  wrote_dev  own_moved  wrong_cars        verdict")
    for target in result.worker_ids:
        tr = result.targets.get(target)
        if tr is None:
            continue
        wrong = [
            "W{0}".format(w) for w, mv in tr.other_motion.items()
            if tr.other_fresh.get(w, False) and mv >= PULSE_MOTION_EPS
        ]
        own = "-"
        if tr.verdict in _WROTE_OK_VERDICTS:
            own = "{0:.2f}{1}".format(tr.self_motion, "" if tr.self_fresh else "?")
        lines.append(
            "  W{0:<3} {1:<9} {2:<5} {3:<10} {4:<10} {5:<17} {6}".format(
                target,
                ("yes" if tr.write_ok else "NO"),
                str(tr.vjoy_device_id if tr.vjoy_device_id is not None else "-"),
                str(tr.last_vjoy_write_device if tr.last_vjoy_write_device is not None else "-"),
                own,
                (",".join(wrong) if wrong else "none"),
                tr.verdict,
            )
        )
        if tr.detail:
            for dline in str(tr.detail).splitlines():
                lines.append(dline if dline.startswith("       ") else "       " + dline)
        if tr.verdict in (V_WRITE_FAILED, V_DIAG_COMMS, V_ENV_NOT_READY, V_STALE_CODE) and tr.failure_resp:
            lines.extend(_format_failure_detail(tr.failure_resp))
    lines.append("")

    targets = [result.targets[t] for t in result.worker_ids if result.targets.get(t)]
    wrote_ok = [t for t in targets if t.write_ok]
    own_ok = [t for t in targets if t.verdict == V_OWN_CAR]
    mismatch = [t for t in targets if t.verdict == V_DIAG_MISMATCH]
    comms = [t for t in targets if t.verdict in (V_DIAG_COMMS,)]
    stale = [t for t in targets if t.verdict == V_STALE_CODE]

    # GOLDEN RULE: with zero successful writes we have NOT tested the binding, so we
    # must never say BINDING PROBLEM.
    if not wrote_ok:
        if stale:
            lines.append(
                "  VERDICT: COLLECTOR_PREDATES_PULSE — some collectors run old code "
                "without the pulse handler. clean-start to reload, then rerun."
            )
        elif mismatch and not comms:
            lines.append(
                "  VERDICT: DIAG_STATE_MISMATCH — C4D and the diag endpoints do not agree "
                "on the live collectors. Binding NOT tested (no vJoy write succeeded)."
            )
        elif comms or mismatch:
            lines.append(
                "  VERDICT: DIAG_STATE_MISMATCH / DIAG_COMMS_ERROR — could not talk cleanly "
                "to the collectors. Binding NOT tested (no vJoy write succeeded)."
            )
        else:
            lines.append(
                "  VERDICT: WRITE_FAILED — no collector could write its vJoy, so the binding "
                "was NOT tested (this is NOT a binding problem). See per-row detail/trace."
            )
        return "\n".join(lines)

    # At least one write succeeded -> a binding conclusion is possible.
    if result.violations:
        lines.append("  VERDICT: BINDING PROBLEM (only among workers whose write succeeded)")
        for v in result.violations:
            lines.append("    ! " + v)
        return "\n".join(lines)

    if len(own_ok) == len(targets) and targets:
        lines.append("  VERDICT: WRITE_OK + DIAGONAL_OK — every worker pulse moved only its own car.")
        return "\n".join(lines)

    untested = [t.target for t in targets if not t.write_ok]
    lines.append(
        "  VERDICT: PARTIAL_TEST_INVALID — binding OK for workers {0} (write_ok + own car). "
        "Not tested for {1} (see per-row reason). No wrong-car detected.".format(
            [t.target for t in own_ok], untested
        )
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# direct vJoy pulse mode (per-device, bypasses policy + learner entirely)
# --------------------------------------------------------------------------- #
@dataclass
class DirectPulseResult:
    worker_ids: list[int]
    identities: dict[int, Identity] = field(default_factory=dict)
    write_ok: dict[int, bool] = field(default_factory=dict)
    car_moved: dict[int, bool | None] = field(default_factory=dict)
    motion: dict[int, float] = field(default_factory=dict)
    verdict: dict[int, str] = field(default_factory=dict)
    detail: dict[int, str] = field(default_factory=dict)


def direct_vjoy_pulse(worker_ids: list[int], *, repeats: int = 3) -> DirectPulseResult:
    """Pulse each worker's vJoy device DIRECTLY via the collector's own handle.

    Uses the authoritative ``diagnostic_vjoy_pulse`` command — bypasses policy,
    learner, replay buffer and phase. Per device: did the write succeed, and did the
    car this worker observes react? This isolates 'device N+1 -> car observed by
    worker N'. Verdicts are explicit; a stale/idle car yields CAR_MOTION_UNKNOWN, not
    a false negative.
    """
    result = DirectPulseResult(worker_ids=list(worker_ids))
    result.identities = {w: _identity_probe(w) for w in worker_ids}
    for wid in worker_ids:
        ident = result.identities[wid]
        if ident.state == V_DIAG_MISMATCH:
            result.verdict[wid] = V_DIAG_MISMATCH
            result.detail[wid] = ident.note
            continue
        if ident.state == V_OFFLINE:
            result.verdict[wid] = V_OFFLINE
            result.detail[wid] = ident.note
            continue
        write_ok = False
        moved: bool | None = None
        peak = 0.0
        last_resp: dict[str, Any] = {}
        for _ in range(max(1, repeats)):
            resp = _diag_pulse(wid, {"steer": 0.6, "throttle": 0.0, "brake": 1.0, "hold_sec": 0.4})
            last_resp = resp
            if resp.get("write_ok"):
                write_ok = True
            peak = max(peak, float(resp.get("car_motion", 0.0) or 0.0))
            cm = resp.get("car_moved")
            if cm is True:
                moved = True
            elif cm is False and moved is None:
                moved = False
            if write_ok:
                break  # a single successful write is enough to judge binding
            time.sleep(0.1)
        result.write_ok[wid] = write_ok
        result.car_moved[wid] = moved
        result.motion[wid] = peak
        # Separate comms / stale / env-not-ready / write-failed exactly like --pulse.
        pre_verdict, pre_detail = _classify_pulse_response(last_resp)
        if not write_ok and pre_verdict:
            result.verdict[wid] = pre_verdict
            result.detail[wid] = pre_detail
        elif not write_ok:
            result.verdict[wid] = V_WRITE_FAILED
            result.detail[wid] = str(last_resp.get("error", "") or "collector could not write its vJoy device")
        elif moved is True:
            result.verdict[wid] = V_OWN_CAR
        elif moved is None:
            result.verdict[wid] = V_UNKNOWN
            result.detail[wid] = "vJoy write OK but car telemetry not fresh (can't confirm motion)"
        else:
            result.verdict[wid] = V_NO_CAR
            result.detail[wid] = "vJoy write OK but observed car did not move"
        _diag_cmd_neutralize(wid)
    return result


def format_direct(result: DirectPulseResult) -> str:
    lines = ["direct vJoy pulse (authoritative; per device; no policy / no learner):"]
    lines.append(format_identity(result.identities))
    lines.append("")
    lines.append("  device  worker  write_ok  observed_car_moved  peak    verdict")
    for wid in result.worker_ids:
        device = wid + 1
        moved = result.car_moved.get(wid)
        moved_txt = "-" if moved is None else ("yes" if moved else "NO")
        lines.append(
            "  dev{0:<4} W{1:<5}  {2:<9} {3:<19} {4:<7.2f} {5}".format(
                device,
                wid,
                ("yes" if result.write_ok.get(wid) else "NO"),
                moved_txt,
                result.motion.get(wid, 0.0),
                result.verdict.get(wid, "?"),
            )
        )
        if result.detail.get(wid):
            lines.append("       - {0}".format(result.detail[wid]))
    lines.append("")
    lines.append(
        "  reading: write_ok=yes + observed_car_moved=yes => device N+1 drives the car worker N "
        "reads (binding OK)."
    )
    lines.append(
        "  write_ok=NO => collector can't write its vJoy (WRITE_FAILED). write_ok=yes + moved=NO "
        "=> input not reaching AC."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# enumerate mode (read-only hardware evidence)
# --------------------------------------------------------------------------- #
def enumerate_joysticks() -> list[dict[str, Any]]:
    """List the machine's joysticks (Windows winmm), no dependency, read-only.

    Every vJoy device is created by the same driver, so all of them report the
    SAME product name and the SAME manufacturer/product id. The only thing that
    differs is the enumeration INDEX. This is the concrete reason worker->car
    identity cannot be guaranteed from controls.ini alone: AC binds by product
    GUID (identical) + JOY index, so the index ordering is the single point of
    failure.
    """
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    winmm = ctypes.WinDLL("winmm")

    class JOYCAPS(ctypes.Structure):
        _fields_ = [
            ("wMid", wintypes.WORD), ("wPid", wintypes.WORD),
            ("szPname", ctypes.c_wchar * 32),
            ("wXmin", wintypes.UINT), ("wXmax", wintypes.UINT),
            ("wYmin", wintypes.UINT), ("wYmax", wintypes.UINT),
            ("wZmin", wintypes.UINT), ("wZmax", wintypes.UINT),
            ("wNumButtons", wintypes.UINT),
            ("wPeriodMin", wintypes.UINT), ("wPeriodMax", wintypes.UINT),
            ("wRmin", wintypes.UINT), ("wRmax", wintypes.UINT),
            ("wUmin", wintypes.UINT), ("wUmax", wintypes.UINT),
            ("wVmin", wintypes.UINT), ("wVmax", wintypes.UINT),
            ("wCaps", wintypes.UINT),
            ("wMaxAxes", wintypes.UINT), ("wNumAxes", wintypes.UINT),
            ("wMaxButtons", wintypes.UINT),
            ("szRegKey", ctypes.c_wchar * 32),
            ("szOEMVxD", ctypes.c_wchar * 260),
        ]

    class JOYINFOEX(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
            ("dwXpos", wintypes.DWORD), ("dwYpos", wintypes.DWORD), ("dwZpos", wintypes.DWORD),
            ("dwRpos", wintypes.DWORD), ("dwUpos", wintypes.DWORD), ("dwVpos", wintypes.DWORD),
            ("dwButtons", wintypes.DWORD), ("dwButtonNumber", wintypes.DWORD),
            ("dwPOV", wintypes.DWORD), ("dwReserved1", wintypes.DWORD), ("dwReserved2", wintypes.DWORD),
        ]

    joy_returnall = 0x000000FF
    out: list[dict[str, Any]] = []
    for i in range(int(winmm.joyGetNumDevs())):
        info = JOYINFOEX()
        info.dwSize = ctypes.sizeof(JOYINFOEX)
        info.dwFlags = joy_returnall
        present = winmm.joyGetPosEx(i, ctypes.byref(info)) == 0
        caps = JOYCAPS()
        winmm.joyGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(JOYCAPS))
        if present:
            out.append(
                {
                    "index": i,
                    "product": caps.szPname,
                    "wMid": int(caps.wMid),
                    "wPid": int(caps.wPid),
                    "axes": int(caps.wNumAxes),
                    "buttons": int(caps.wNumButtons),
                }
            )
    return out


def format_enumeration(devs: list[dict[str, Any]]) -> str:
    if not devs:
        return "no joysticks enumerated (non-Windows or none present)"
    lines = ["enumerated joysticks (winmm, read-only) — what AC sees to resolve JOY index:"]
    lines.append("  idx  product              wMid   wPid  axes buttons")
    for d in devs:
        lines.append(
            "  {idx:<4} {product:<20} {mid:<6} {pid:<5} {axes:<4} {btn}".format(
                idx=d["index"], product=str(d["product"])[:20], mid=d["wMid"],
                pid=d["wPid"], axes=d["axes"], btn=d["buttons"],
            )
        )
    # Indistinguishability check: identical (product, mid, pid) => only index differs.
    signatures = {(d["product"], d["wMid"], d["wPid"]) for d in devs}
    lines.append("")
    if len(signatures) == 1 and len(devs) > 1:
        lines.append(
            "  ! all {0} devices are IDENTICAL (same product/GUID): worker->car identity "
            "depends\n    ENTIRELY on the JOY enumeration index. controls.ini cannot tell them "
            "apart by GUID.".format(len(devs))
        )
    else:
        lines.append("  devices are distinguishable by product/GUID ({0} distinct signatures).".format(len(signatures)))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Live worker<->car correspondence test")
    parser.add_argument("--workers", type=str, default="", help="comma list, default = live ones")
    parser.add_argument("--enumerate", action="store_true", help="list joysticks (hardware evidence, read-only)")
    parser.add_argument("--pulse", action="store_true", help="decisive pulse mode (perturbs driving briefly)")
    parser.add_argument(
        "--direct-vjoy-pulse",
        action="store_true",
        help="pulse each vJoy device directly (no policy/learner) and read its car",
    )
    parser.add_argument("--duration", type=float, default=DEFAULT_OBSERVE_SEC)
    parser.add_argument("--sample-hz", type=float, default=DEFAULT_SAMPLE_HZ)
    parser.add_argument("--pulse-sec", type=float, default=2.0)
    args = parser.parse_args(argv)

    if args.enumerate:
        print(format_enumeration(enumerate_joysticks()))
        return 0

    if args.workers.strip():
        worker_ids = [int(x) for x in args.workers.split(",") if x.strip() != ""]
    else:
        worker_ids = _live_workers(list(range(MAX_WORKERS)))
        if worker_ids:
            print("live workers detected: {0}".format(worker_ids))
        else:
            # Cold-start probes can flake; scan the full set and let preflight in each
            # mode explain, per worker, exactly why it is/ isn't pulsable (never 0.00
            # presented as proof).
            worker_ids = list(range(MAX_WORKERS))
            print(
                "no workers confirmed on first probe; scanning full set {0} "
                "(preflight will explain each)".format(worker_ids)
            )

    if args.direct_vjoy_pulse:
        print("DIRECT vJoy PULSE: collector writes its own vJoy, bypassing policy/learner...")
        dres = direct_vjoy_pulse(worker_ids)
        print(format_direct(dres))
        # Binding failure = a real WRONG/NO_CAR verdict (write actually succeeded).
        # WRITE_FAILED / COMMS / STALE / ENV_NOT_READY / UNKNOWN / MISMATCH / OFFLINE
        # are test-state issues, NOT a binding conclusion.
        binding_fail = any(
            dres.verdict.get(w) in _BINDING_FAIL_VERDICTS for w in worker_ids
        )
        return 1 if binding_fail else 0

    if args.pulse:
        print("AUTHORITATIVE PULSE: collector writes its own vJoy (no policy/learner/phase gate)...")
        result = pulse(worker_ids, pulse_sec=args.pulse_sec, sample_hz=args.sample_hz)
        print(format_pulse(result))
        if result.gate_blocked:
            return 0  # preflight blocked is not a binding verdict
        return 1 if result.violations else 0

    result = observe(worker_ids, duration_sec=args.duration, sample_hz=args.sample_hz)
    print(format_observe(result))
    return 1 if result.violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
