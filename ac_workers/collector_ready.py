"""Wait until collector passes session-bound readiness checks (offline-testable)."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))

from ac_workers.collector_diag import CollectorDiagClient, DiagRequestError
from ac_workers.collector_pid import GRACE_SEC, archive_collector_failure, collector_evidence
from ac_workers.collector_status import format_not_ready_line, validate_snapshot


def wait_collector_ready(
    worker_id: int,
    session_id: str,
    collector_pid: int,
    timeout_sec: float = 180.0,
    poll_sec: float = 0.5,
    require_transitions: bool = True,
    launcher_pid: int | None = None,
    repo_root: Path | None = None,
    stdout_log: str = "",
    stderr_log: str = "",
) -> tuple[bool, dict]:
    launcher = int(launcher_pid or 0) or int(collector_pid or 0)
    real_pid = int(collector_pid or 0)
    deadline = time.time() + timeout_sec
    last_details: dict = {}
    last_errors: list[str] = []
    client = CollectorDiagClient(worker_id, timeout=2.0)
    dead_since: float | None = None

    while time.time() < deadline:
        alive, canonical, reason, _ev = collector_evidence(
            worker_id,
            session_id,
            real_pid,
            launcher,
            repo_root=repo_root,
            grace_sec=GRACE_SEC,
        )
        if alive and canonical > 0:
            real_pid = canonical
            dead_since = None
        elif reason == "collector_real_process_died":
            if dead_since is None:
                dead_since = time.time()
            elif time.time() - dead_since >= GRACE_SEC:
                archive_collector_failure(
                    worker_id,
                    session_id,
                    reason,
                    repo_root=repo_root,
                    launcher_pid=launcher,
                    known_pid=real_pid,
                    stdout_log=stdout_log,
                    stderr_log=stderr_log,
                )
                return False, {"errors": [reason], "details": last_details}
            time.sleep(poll_sec)
            continue
        else:
            dead_since = None

        try:
            snap = client.request("readonly_check")
        except DiagRequestError as exc:
            last_details = {"processAlive": alive, "diag": False}
            last_errors = [str(exc.message)]
            time.sleep(poll_sec)
            continue

        ok, errs, details = validate_snapshot(
            snap,
            session_id=session_id,
            collector_pid=int(snap.get("collector_pid") or real_pid or 0),
            require_transitions=require_transitions,
        )
        details["processAlive"] = alive
        details["diag"] = True
        last_details = details
        last_errors = errs
        if ok:
            return True, {
                "session_id": snap.get("session_id"),
                "collector_pid": snap.get("collector_pid"),
                "telemetry_age_ms": snap.get("telemetry_age_ms"),
                "transition_count": snap.get("transition_count"),
                "details": details,
            }
        time.sleep(poll_sec)

    archive_collector_failure(
        worker_id,
        session_id,
        last_errors[0] if last_errors else "collector_timeout",
        repo_root=repo_root,
        launcher_pid=launcher,
        known_pid=real_pid,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
    )
    return False, {"errors": last_errors, "details": last_details}


VJOY_PREHOLD_OK_PHASES = frozenset(
    {
        "vjoy_prehold_active",
        "environment_construct_begin",
        "environment_construct_complete",
        "attaching_live_session",
        "idle",
        "running",
    }
)


def wait_vjoy_prehold(
    worker_id: int,
    session_id: str,
    collector_pid: int,
    *,
    timeout_sec: float = 90.0,
    poll_sec: float = 0.25,
    repo_root: Path | None = None,
) -> tuple[bool, str]:
    """Wait until the collector owns its vJoy device BEFORE Assetto Corsa starts.

    Assetto Corsa binds vJoy via DirectInput while loading. If acs.exe starts
    first, AcquireVJD fails with status=FREE even though the device looks free.
    """
    from ac_workers.collector_diag import CollectorDiagClient, DiagRequestError
    from ac_workers.collector_status import read_worker_status

    repo = repo_root or REPO
    deadline = time.time() + timeout_sec
    client = CollectorDiagClient(worker_id, timeout=2.0)
    state = {
        "phase": "",
        "err": "",
        "source": "none",
        "session": "",
        "pid": 0,
        "diag_seen": False,
        "status_seen": False,
    }

    def _evaluate(snap: dict, source: str):
        """Return (ok, msg) when conclusive, else None. Records what we saw."""
        if not snap:
            return None
        snap_session = str(snap.get("session_id") or "")
        pid = int(snap.get("collector_pid") or 0)
        if snap_session:
            state["session"] = snap_session
        if pid:
            state["pid"] = pid
        # collector_pid is the strongest identity signal: the launcher already
        # resolved the real python pid before calling us, so a pid match means
        # this is the right collector even if the session stamp formatting drifts.
        pid_match = collector_pid > 0 and pid == collector_pid
        session_match = bool(snap_session) and snap_session == session_id
        if not pid_match and not session_match:
            return None
        phase = str(snap.get("phase") or "")
        state["phase"] = phase
        state["err"] = str(snap.get("last_error") or "")
        state["source"] = source
        if phase == "vjoy_startup_failed":
            return False, state["err"] or "vJoy prehold failed"
        if bool(snap.get("vjoy_device_acquired")) or phase in VJOY_PREHOLD_OK_PHASES:
            return True, ""
        return None

    while time.time() < deadline:
        # 1) Authoritative live signal: the diagnostic channel. This is the same
        # source collector_evidence/Resolve-CollectorRealPid trust, so the wait
        # can no longer disagree with the pid resolution that precedes it.
        try:
            diag_snap = client.request("readonly_check")
            state["diag_seen"] = True
        except DiagRequestError:
            diag_snap = None
        result = _evaluate(diag_snap, "diag")
        if result is not None:
            return result
        # 2) Fallback: the on-disk status file.
        disk_snap = read_worker_status(worker_id, repo_root=repo)
        if disk_snap:
            state["status_seen"] = True
        result = _evaluate(disk_snap, "status")
        if result is not None:
            return result
        time.sleep(poll_sec)

    if not state["diag_seen"] and not state["status_seen"]:
        return False, (
            "vJoy prehold timeout: collector worker {0} reported no diag and no "
            "status (expected session={1} pid={2})".format(
                worker_id, session_id, collector_pid
            )
        )
    return False, (
        "vJoy prehold timeout (phase={0}, err={1}, source={2}, diagSeen={3}, "
        "statusSeen={4}, seenSession={5}, wantSession={6}, seenPid={7}, "
        "wantPid={8})".format(
            state["phase"],
            state["err"],
            state["source"],
            state["diag_seen"],
            state["status_seen"],
            state["session"],
            session_id,
            state["pid"],
            collector_pid,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-id", type=int, required=True)
    parser.add_argument("--session-id", type=str, required=True)
    parser.add_argument("--collector-pid", type=int, required=True)
    parser.add_argument("--launcher-pid", type=int, default=0)
    parser.add_argument("--repo-root", type=Path, default=REPO)
    parser.add_argument("--stdout-log", default="")
    parser.add_argument("--stderr-log", default="")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--no-require-transitions", action="store_true")
    parser.add_argument(
        "--wait-vjoy-prehold",
        action="store_true",
        help="wait until vJoy is acquired (call before starting acs.exe)",
    )
    args = parser.parse_args()

    if args.wait_vjoy_prehold:
        ok, err = wait_vjoy_prehold(
            args.worker_id,
            args.session_id,
            args.collector_pid,
            timeout_sec=min(float(args.timeout), 90.0),
            repo_root=args.repo_root,
        )
        if ok:
            print("collector worker {0} vjoy prehold ready".format(args.worker_id))
            return 0
        print("ERROR: {0}".format(err))
        return 1

    ok, info = wait_collector_ready(
        args.worker_id,
        args.session_id,
        args.collector_pid,
        timeout_sec=args.timeout,
        require_transitions=not args.no_require_transitions,
        launcher_pid=int(args.launcher_pid or 0) or None,
        repo_root=args.repo_root,
        stdout_log=args.stdout_log,
        stderr_log=args.stderr_log,
    )
    if ok:
        print(
            "collector worker {0} ready session={1} pid={2} telemetryAgeMs={3} transitions={4}".format(
                args.worker_id,
                info.get("session_id"),
                info.get("collector_pid"),
                info.get("telemetry_age_ms"),
                info.get("transition_count"),
            )
        )
        return 0

    details = info.get("details") or {}
    print(format_not_ready_line(args.worker_id, details))
    for err in info.get("errors") or []:
        print("ERROR: {0}".format(err))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
