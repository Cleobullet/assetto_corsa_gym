"""Observer-only pipeline timeline for worker startup and episode resets.

This module records, exactly once per step, the moment a worker crosses a
named milestone of its startup or episode-reset pipeline. It exists purely to
*diagnose* where the six workers stop being synchronized; it never changes any
business logic, timeout, retry, Drive detection or vJoy behaviour.

Design constraints (deliberate):
    - each step is recorded ONCE per cycle (no spam, no polling, no loop) — only
      the first transition is kept;
    - timestamps are monotonic, so elapsed times are always non-decreasing;
    - the collector process writes its own per-worker file; the Control Center
      only reads, so there is never cross-process write contention.

A "cycle" groups the steps of one pipeline run:
    - "startup"          -> the launch/attach pipeline (recorded once)
    - "reset:<n>"        -> the n-th episode reset (re-recorded each episode)
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STATUS_DIR = REPO / "ac_workers" / "status"

# --- Startup steps (ordered) ------------------------------------------------
START = "START"
AC_PROCESS_STARTED = "AC_PROCESS_STARTED"
PLUGIN_CONNECTED = "PLUGIN_CONNECTED"
FIRST_TELEMETRY = "FIRST_TELEMETRY"
DRIVE_CONFIRMED = "DRIVE_CONFIRMED"
VJOY_READY = "VJOY_READY"
IDLE_READY = "IDLE_READY"

# --- Episode-reset steps (ordered) ------------------------------------------
# Collector-attributed events (what the collector decides / commands):
RESET_REQUESTED = "RESET_REQUESTED"
RESET_NEUTRALIZED = "RESET_NEUTRALIZED"
RESET_TELEPORT_SENT = "RESET_TELEPORT_SENT"
RESET_TELEMETRY_FRESH = "RESET_TELEMETRY_FRESH"
RESET_DRIVING_CONFIRMED = "RESET_DRIVING_CONFIRMED"
RESET_VJOY_VALIDATED = "RESET_VJOY_VALIDATED"
RESET_RUNNING = "RESET_RUNNING"
RESET_FAILED = "RESET_FAILED"
# Plugin-attributed events (what the in-game plugin reports back over the mgmt
# port, observed by the collector while it polls — so we can tell whether a
# stall is on the plugin side or the collector side):
PLUGIN_SIM_LIVE = "PLUGIN_SIM_LIVE"  # sim alive again after ext_resetCar (teleport done)
PLUGIN_DRIVING_TRUE = "PLUGIN_DRIVING_TRUE"  # plugin's own driving flag flipped True

# Components that can emit an event (for attribution in the trace).
COMPONENT_COLLECTOR = "collector"
COMPONENT_PLUGIN = "plugin"
COMPONENT_LEARNER = "learner"

# Expected order, used only to compute "next expected step" in the summary.
STARTUP_ORDER = (
    START,
    AC_PROCESS_STARTED,
    PLUGIN_CONNECTED,
    FIRST_TELEMETRY,
    DRIVE_CONFIRMED,
    VJOY_READY,
    IDLE_READY,
)
RESET_ORDER = (
    RESET_REQUESTED,
    RESET_NEUTRALIZED,
    RESET_TELEPORT_SENT,
    RESET_TELEMETRY_FRESH,
    PLUGIN_SIM_LIVE,
    PLUGIN_DRIVING_TRUE,
    RESET_DRIVING_CONFIRMED,
    RESET_VJOY_VALIDATED,
    RESET_RUNNING,
)

# Which component each step is attributed to (default collector).
_STEP_COMPONENT = {
    PLUGIN_SIM_LIVE: COMPONENT_PLUGIN,
    PLUGIN_DRIVING_TRUE: COMPONENT_PLUGIN,
}

# Steps that only make sense inside an open reset cycle. Recording one of these
# while the worker is not in a reset epoch (reset_id == 0) is a causality
# violation (cross-epoch bleeding) and is flagged, never silently attributed.
_RESET_STEPS = frozenset(
    RESET_ORDER + (RESET_FAILED,)
)

_LOCK = threading.Lock()
# Per-process recorder state (keyed by worker_id).
_t0: dict[int, float] = {}
_cycle: dict[int, str] = {}
_reset_id: dict[int, int] = {}  # worker_id -> active reset epoch (0 = startup)
_seen: dict[int, set] = {}
_initialized: set = set()
_global_reset_seq = 0  # strictly increasing across ALL workers
_mismatch_count = 0  # RESET_EPOCH_MISMATCH_COUNT
# Per-component monotonic sequence, reset to 0 on every reset_id change. This
# distinguishes a clean causal chain from "two reads of a poll-based snapshot at
# different moments of the same epoch": each source advances its own counter.
_comp_seq: dict[int, dict[str, int]] = {}  # worker -> {component -> last seq}
_comp_seq_epoch: dict[int, int] = {}  # worker -> reset_id the seqs belong to


def timeline_path(worker_id: int, *, repo_root: Path | None = None) -> Path:
    base = (repo_root / "ac_workers" / "status") if repo_root else STATUS_DIR
    return base / "worker_{0}_timeline.jsonl".format(worker_id)


def begin_reset_cycle(worker_id: int, episode_id: int) -> int:
    """Open a fresh reset cycle and return its globally-unique reset_id.

    The reset_id is a strictly increasing counter shared by all workers, so an
    event that leaks across epochs (or across workers) can be detected: its
    reset_id will not match the active epoch when it is recorded.
    """
    global _global_reset_seq
    with _LOCK:
        if worker_id not in _t0:
            _t0[worker_id] = time.monotonic()
            _seen[worker_id] = set()
        _global_reset_seq += 1
        rid = _global_reset_seq
        _reset_id[worker_id] = rid
        _cycle[worker_id] = "reset:{0}".format(int(episode_id))
        return rid


def current_reset_id(worker_id: int) -> int:
    with _LOCK:
        return int(_reset_id.get(worker_id, 0))


def epoch_mismatch_count() -> int:
    """Number of cross-epoch event-bleeding violations seen this process."""
    with _LOCK:
        return _mismatch_count


def mark(
    worker_id: int,
    step: str,
    *,
    component: str | None = None,
    cycle: str | None = None,
    detail: str = "",
    expected_reset_id: int | None = None,
    repo_root: Path | None = None,
) -> bool:
    """Record `step` once for the worker's current cycle. Returns True if written.

    `component` attributes the event (collector / plugin / learner). When omitted
    it defaults from the step (plugin steps -> plugin, otherwise collector).

    `expected_reset_id`: the epoch the caller believes it is recording against.
    When it does not match the worker's active reset_id (or a reset step is
    recorded with no open cycle), the event is treated as cross-epoch bleeding:
    RESET_EPOCH_MISMATCH_COUNT is incremented and the record is written with
    "orphan": True instead of being attributed to the live cycle. Orphans never
    become a worker's "last step" in the summary.

    Observer-only and exception-safe: any I/O problem is swallowed so this can
    never affect the pipeline it observes.
    """
    global _mismatch_count
    try:
        with _LOCK:
            now = time.monotonic()
            if worker_id not in _t0:
                _t0[worker_id] = now
                _cycle.setdefault(worker_id, "startup")
                _seen[worker_id] = set()
            if cycle is not None:
                _cycle[worker_id] = cycle
            cur_cycle = _cycle.get(worker_id, "startup")
            active_rid = int(_reset_id.get(worker_id, 0))

            # Causality guard: detect events that belong to a different epoch.
            orphan = False
            if expected_reset_id is not None and int(expected_reset_id) != active_rid:
                orphan = True
            elif step in _RESET_STEPS and active_rid == 0:
                # A reset step with no open reset cycle is a stray/stale event.
                orphan = True
            if orphan:
                _mismatch_count += 1

            # Dedupe: legit events per (cycle, step); orphans per (epoch, step)
            # so repeated stray polls don't spam the file.
            if orphan:
                key = ("orphan", expected_reset_id, active_rid, step)
            else:
                key = (cur_cycle, step)
            if key in _seen[worker_id]:
                return False
            _seen[worker_id].add(key)

            elapsed = round(now - _t0[worker_id], 3)
            comp = component or _STEP_COMPONENT.get(step, COMPONENT_COLLECTOR)
            record = {
                "ts_monotonic": round(now, 6),
                "elapsed": elapsed,
                "worker_id": int(worker_id),
                "cycle": cur_cycle,
                "reset_id": active_rid,
                "component": comp,
                "step": step,
            }
            if orphan:
                record["orphan"] = True
                if expected_reset_id is not None:
                    record["expected_reset_id"] = int(expected_reset_id)
            else:
                # Per-component monotonic seq within this epoch. Reset all
                # component counters whenever the worker's reset_id changes.
                if _comp_seq_epoch.get(worker_id) != active_rid:
                    _comp_seq[worker_id] = {}
                    _comp_seq_epoch[worker_id] = active_rid
                seqmap = _comp_seq.setdefault(worker_id, {})
                seq = int(seqmap.get(comp, 0)) + 1
                seqmap[comp] = seq
                record["seq"] = seq
            if detail:
                record["detail"] = str(detail)[:200]
            path = timeline_path(worker_id, repo_root=repo_root)
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = "a"
            if worker_id not in _initialized:
                _initialized.add(worker_id)
                mode = "w"  # truncate stale data from a previous process/session
            with path.open(mode, encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
            return not orphan
    except Exception:
        return False


def read_timeline(worker_id: int, *, repo_root: Path | None = None) -> list:
    """Return the recorded steps for a worker (oldest first); [] if none."""
    path = timeline_path(worker_id, repo_root=repo_root)
    out: list = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def validate_timeline(worker_id: int, *, repo_root: Path | None = None) -> list:
    """Return ordering anomalies for a worker's timeline (empty == clean).

    Checks the two invariants the seq layer guarantees:
      - append-order timestamps never go backwards (no reordered events);
      - each component's seq strictly increases within an epoch, and restarts at
        1 in a new epoch (no stale/replayed source readings).
    """
    recs = read_timeline(worker_id, repo_root=repo_root)
    anomalies: list = []
    last_ts: float | None = None
    last_seq: dict = {}  # (reset_id, component) -> last seq
    seen_epoch_comp: set = set()
    for r in recs:
        if r.get("orphan"):
            continue
        ts = float(r.get("ts_monotonic", 0.0))
        if last_ts is not None and ts < last_ts:
            anomalies.append(
                "ts regression at {0} (rid {1})".format(r.get("step"), r.get("reset_id"))
            )
        last_ts = ts
        seq = r.get("seq")
        if seq is None:
            continue
        key = (int(r.get("reset_id", 0)), str(r.get("component", "")))
        if key not in seen_epoch_comp and int(seq) != 1:
            anomalies.append(
                "{0} seq starts at {1} (expected 1) in rid {2}".format(
                    key[1], seq, key[0]
                )
            )
        seen_epoch_comp.add(key)
        prev = last_seq.get(key)
        if prev is not None and int(seq) <= int(prev):
            anomalies.append(
                "{0} seq not increasing ({1}<={2}) in rid {3}".format(
                    key[1], seq, prev, key[0]
                )
            )
        last_seq[key] = int(seq)
    return anomalies


def _next_expected(step: str, order: tuple) -> str:
    if step in order:
        idx = order.index(step)
        if idx + 1 < len(order):
            return order[idx + 1]
    return ""


def worker_summary(worker_id: int, *, repo_root: Path | None = None) -> dict:
    """Summarize the last reset cycle if present, else the startup cycle."""
    records = read_timeline(worker_id, repo_root=repo_root)
    # Orphans (cross-epoch leaks) are kept on disk for forensics but never count
    # as a worker's real progress.
    records = [r for r in records if not r.get("orphan")]
    if not records:
        return {"worker_id": worker_id, "last_step": "", "component": "", "elapsed": 0.0,
                "cycle": "", "reset_id": 0, "next_expected": "", "complete": False,
                "failed": False, "detail": ""}
    reset_records = [r for r in records if str(r.get("cycle", "")).startswith("reset:")]
    scope = reset_records or records
    order = RESET_ORDER if reset_records else STARTUP_ORDER
    if reset_records:
        # Restrict to the latest epoch so a stalled current reset is not masked
        # by a completed earlier one.
        last_rid = max(int(r.get("reset_id", 0)) for r in reset_records)
        epoch = [r for r in reset_records if int(r.get("reset_id", 0)) == last_rid]
        scope = epoch or scope
    last = scope[-1]
    step = str(last.get("step", ""))
    terminal = step in (IDLE_READY, RESET_RUNNING)
    failed = step == RESET_FAILED
    return {
        "worker_id": worker_id,
        "last_step": step,
        "component": str(last.get("component", "")),
        "elapsed": float(last.get("elapsed", 0.0)),
        "cycle": str(last.get("cycle", "")),
        "reset_id": int(last.get("reset_id", 0)),
        "next_expected": "" if (terminal or failed) else _next_expected(step, order),
        "complete": terminal,
        "failed": failed,
        "detail": str(last.get("detail", "")),
    }


def _shm_path(worker_id: int, *, repo_root: Path | None = None) -> Path:
    base = (repo_root / "ac_workers" / "status") if repo_root else STATUS_DIR
    return base / "worker_{0}_shm.json".format(worker_id)


def record_shm_fingerprint(
    worker_id: int,
    packet_id: int,
    clock_ms: int = 0,
    *,
    repo_root: Path | None = None,
) -> None:
    """Persist this worker's latest AC shared-memory fingerprint (latest only).

    Observer-only. `packet_id` is AC's per-physics-tick counter: if two workers
    report the same packet_id at the same instant, their AC instances are reading
    the SAME global shared memory (the multi-instance acpmf_* collision).
    """
    try:
        path = _shm_path(worker_id, repo_root=repo_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "worker_id": int(worker_id),
            "wall": round(time.time(), 3),
            "packet_id": int(packet_id),
            "clock_ms": int(clock_ms),
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        pass


def read_shm_fingerprint(worker_id: int, *, repo_root: Path | None = None) -> dict:
    try:
        return json.loads(_shm_path(worker_id, repo_root=repo_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def shm_collision_report(worker_ids, *, repo_root: Path | None = None) -> str:
    """Compare workers' latest SHM fingerprints and flag shared-memory collisions.

    A packet_id shared by two workers whose samples are close in wall time is
    strong evidence both AC instances map the same acpmf_physics shared memory.
    """
    samples = []
    for wid in worker_ids:
        fp = read_shm_fingerprint(wid, repo_root=repo_root)
        if fp:
            samples.append(fp)
    header = "W | wall            | packet_id | clock_ms"
    lines = ["AC shared-memory fingerprints:", header, "-" * len(header)]
    for fp in sorted(samples, key=lambda s: s.get("worker_id", 0)):
        lines.append(
            "{0} | {1:<15} | {2:>9} | {3}".format(
                fp.get("worker_id"), fp.get("wall"), fp.get("packet_id"), fp.get("clock_ms")
            )
        )
    if not samples:
        lines.append("(no fingerprints yet — run a session first)")
        return "\n".join(lines)
    by_packet: dict = {}
    for fp in samples:
        by_packet.setdefault(int(fp.get("packet_id", -1)), []).append(int(fp.get("worker_id", -1)))
    collisions = {pid: ws for pid, ws in by_packet.items() if pid >= 0 and len(ws) > 1}
    lines.append("-" * len(header))
    if collisions:
        lines.append("!! SHARED-MEMORY COLLISION DETECTED:")
        for pid, ws in sorted(collisions.items()):
            lines.append(
                "   packet_id {0} reported by workers {1} -> same acpmf_physics".format(
                    pid, ",".join(str(w) for w in sorted(ws))
                )
            )
        lines.append(
            "   => all colliding workers read ONE AC instance's state. This is the"
        )
        lines.append(
            "      multi-instance acpmf_* collision; per-worker namespacing required."
        )
    else:
        lines.append("OK: every worker reports a distinct packet_id (no collision).")
        lines.append(
            "   Tip: sample twice; independent instances advance their counters out of lockstep."
        )
    return "\n".join(lines)


def count_orphans(worker_id: int, *, repo_root: Path | None = None) -> int:
    """How many cross-epoch (orphan) events were recorded for a worker on disk."""
    return sum(1 for r in read_timeline(worker_id, repo_root=repo_root) if r.get("orphan"))


def summarize(worker_ids, *, repo_root: Path | None = None) -> str:
    """Comparison table across workers: who reached which step + elapsed time."""
    header = "W | Epoch | By        | Last completed step     | Cycle       | Time   | Next expected | Orphans"
    sep = "-" * len(header)
    lines = [header, sep]
    total_orphans = 0
    for wid in worker_ids:
        s = worker_summary(wid, repo_root=repo_root)
        orphans = count_orphans(wid, repo_root=repo_root)
        total_orphans += orphans
        if not s["last_step"]:
            lines.append("{0} | (no timeline)".format(wid))
            continue
        lines.append(
            "{0} | {1:>5} | {2:<9} | {3:<23} | {4:<11} | {5:>5.1f}s | {6:<13} | {7}".format(
                wid,
                s.get("reset_id", 0),
                s.get("component", "") or "-",
                s["last_step"],
                s["cycle"],
                s["elapsed"],
                s["next_expected"] or ("OK" if s["complete"] else ("FAILED" if s.get("failed") else "")),
                orphans,
            )
        )
    lines.append(sep)
    lines.append(
        "cross-epoch event leaks (this process): {0} | on-disk orphan events: {1}".format(
            epoch_mismatch_count(), total_orphans
        )
    )
    return "\n".join(lines)


def reset_state_for_tests() -> None:
    """Clear in-process recorder state (test helper only)."""
    global _global_reset_seq, _mismatch_count
    with _LOCK:
        _t0.clear()
        _cycle.clear()
        _reset_id.clear()
        _seen.clear()
        _initialized.clear()
        _comp_seq.clear()
        _comp_seq_epoch.clear()
        _global_reset_seq = 0
        _mismatch_count = 0
