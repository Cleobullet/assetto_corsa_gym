"""Resolve canonical collector PID (real process vs venv launcher wrapper)."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
INIT_COLLECTOR_PHASES = frozenset(
    {
        "creating_environment",
        "environment_loading",
        "attaching_live_session",
        "waiting_for_drive",
        "gear_initialization",
        "vjoy_startup",
        "policy_loading",
    }
)
GRACE_SEC = 3.0
MAX_HEARTBEAT_MS = 5000.0


def _setup_paths(repo_root: Path | None) -> None:
    root = repo_root or REPO
    for p in (root, root / "assetto_corsa_gym"):
        sp = str(p)
        if sp not in sys.path:
            sys.path.insert(0, sp)


def read_worker_status(worker_id: int, repo_root: Path | None = None) -> dict[str, Any]:
    root = Path(repo_root or REPO)
    path = root / "ac_workers" / "status" / "worker_{0}.json".format(worker_id)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}


def _pid_alive(pid: int) -> bool:
    from ac_workers.control_center import pid_alive

    return pid_alive(pid)


def _is_collector_process(pid: int, worker_id: int) -> bool:
    from ac_workers.control_center import is_collector_process

    return is_collector_process(pid, worker_id)


def _process_command_line(pid: int) -> str:
    try:
        from ac_workers.transition_server_process import get_process_command_line

        return get_process_command_line(pid) or ""
    except Exception:
        return ""


def _diag_snapshot(worker_id: int) -> dict[str, Any] | None:
    try:
        from ac_workers.collector_diag import CollectorDiagClient

        return CollectorDiagClient(worker_id, timeout=2.0).request("readonly_check")
    except Exception:
        return None


def _heartbeat_fresh(status: dict[str, Any] | None, diag: dict[str, Any] | None) -> bool:
    from ac_workers.control_center import status_age_ms

    if status:
        if status_age_ms(status) <= MAX_HEARTBEAT_MS:
            return True
    if diag:
        hb = diag.get("heartbeat_age_ms")
        if hb is not None and float(hb) <= MAX_HEARTBEAT_MS:
            return True
    return False


def scan_collector_pids(worker_id: int) -> list[int]:
    if os.name != "nt":
        return []
    marker = "ac_workers.worker_collector"
    wid_needle = "--worker-id {0}".format(worker_id)
    wid_needle_eq = "--worker-id={0}".format(worker_id)
    found: list[int] = []
    try:
        ps = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.CommandLine -like '*ac_workers.worker_collector*' } | "
            "ForEach-Object { \"$($_.ProcessId)`t$($_.ParentProcessId)`t$($_.CommandLine)\" }"
        )
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps],
            text=True,
            errors="replace",
            timeout=15,
        )
        for line in out.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 2)
            if len(parts) < 3:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            cmd = parts[2]
            if marker not in cmd:
                continue
            if wid_needle not in cmd and wid_needle_eq not in cmd:
                continue
            found.append(pid)
    except Exception:
        pass
    return list(dict.fromkeys(found))


def find_descendant_collector_pids(
    root_pids: list[int],
    worker_id: int,
) -> list[int]:
    if os.name != "nt" or not root_pids:
        return []
    try:
        ps = (
            "Get-CimInstance Win32_Process | "
            "ForEach-Object { \"$($_.ProcessId)`t$($_.ParentProcessId)`t$($_.CommandLine)\" }"
        )
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps],
            text=True,
            errors="replace",
            timeout=15,
        )
        rows: list[tuple[int, int, str]] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 2)
            if len(parts) < 3:
                continue
            try:
                rows.append((int(parts[0]), int(parts[1]), parts[2]))
            except ValueError:
                continue
        children: dict[int, list[int]] = {}
        for pid, ppid, _cmd in rows:
            children.setdefault(ppid, []).append(pid)
        seeds = [p for p in root_pids if p > 0]
        seen: set[int] = set()
        queue = list(seeds)
        out_pids: list[int] = []
        while queue:
            pid = queue.pop(0)
            if pid in seen:
                continue
            seen.add(pid)
            cmd = _process_command_line(pid)
            if _is_collector_process(pid, worker_id) or "ac_workers.worker_collector" in cmd:
                out_pids.append(pid)
            for child in children.get(pid, []):
                queue.append(child)
        return list(dict.fromkeys(out_pids))
    except Exception:
        return []


def _diag_open(worker_id: int) -> bool:
    try:
        from ac_workers.worker_handshake import tcp_open
        from ac_workers.worker_ports import worker_ports

        return tcp_open("127.0.0.1", worker_ports(worker_id).diag, timeout=0.15)
    except Exception:
        return False


def _evidence_payload(
    *,
    known_pid: int,
    launcher_pid: int,
    status_pid: int,
    diag_pid: int,
    candidates: list[int],
    phase: str,
    fresh: bool,
    diag_sid: str,
    status_sid: str,
) -> dict[str, Any]:
    return {
        "known_pid": known_pid,
        "launcher_pid": launcher_pid,
        "status_pid": status_pid,
        "diag_pid": diag_pid,
        "candidates": candidates,
        "phase": phase,
        "heartbeat_fresh": fresh,
        "diag_sid": diag_sid,
        "status_sid": status_sid,
    }


def collector_evidence(
    worker_id: int,
    session_id: str,
    known_pid: int = 0,
    launcher_pid: int = 0,
    *,
    repo_root: Path | None = None,
    grace_sec: float = 0.0,
    deep_scan: bool = True,
) -> tuple[bool, int, str, dict[str, Any]]:
    _setup_paths(repo_root)
    status = read_worker_status(worker_id, repo_root)
    status_pid = int(status.get("collector_pid") or 0)
    status_sid = str(status.get("session_id") or "")
    phase = str(status.get("phase") or "")
    status_fresh = _heartbeat_fresh(status, None)
    quick_pids = [p for p in (status_pid, known_pid, launcher_pid) if p > 0]
    for pid in quick_pids:
        if not _pid_alive(pid):
            continue
        if status_sid and status_sid != session_id:
            continue
        if deep_scan and not _is_collector_process(pid, worker_id):
            continue
        evidence = _evidence_payload(
            known_pid=known_pid,
            launcher_pid=launcher_pid,
            status_pid=status_pid,
            diag_pid=0,
            candidates=quick_pids,
            phase=phase,
            fresh=status_fresh,
            diag_sid="",
            status_sid=status_sid,
        )
        return True, pid, "ok", evidence

    diag: dict[str, Any] | None = None
    diag_pid = 0
    diag_sid = ""
    if _diag_open(worker_id):
        diag = _diag_snapshot(worker_id)
        if diag:
            diag_pid = int(diag.get("collector_pid") or 0)
            diag_sid = str(diag.get("session_id") or "")
            phase = phase or str(diag.get("phase") or "")
    fresh = _heartbeat_fresh(status, diag)
    candidates = list(dict.fromkeys([p for p in (diag_pid, status_pid, *quick_pids) if p > 0]))
    evidence = _evidence_payload(
        known_pid=known_pid,
        launcher_pid=launcher_pid,
        status_pid=status_pid,
        diag_pid=diag_pid,
        candidates=candidates,
        phase=phase,
        fresh=fresh,
        diag_sid=diag_sid,
        status_sid=status_sid,
    )
    if diag and fresh and diag_sid == session_id and diag_pid > 0:
        if _pid_alive(diag_pid) and _is_collector_process(diag_pid, worker_id):
            return True, diag_pid, "ok", evidence
        if phase in INIT_COLLECTOR_PHASES or grace_sec > 0.0:
            return True, diag_pid, "diag_alive", evidence
    for pid in candidates:
        if not _pid_alive(pid):
            continue
        if deep_scan and not _is_collector_process(pid, worker_id):
            continue
        if status_sid and status_sid != session_id:
            continue
        if diag_sid and diag_sid != session_id:
            continue
        return True, pid, "ok", evidence
    if fresh and (phase in INIT_COLLECTOR_PHASES or grace_sec > 0.0):
        fallback = diag_pid or status_pid or (candidates[0] if candidates else 0)
        if fallback > 0:
            return True, fallback, "initializing", evidence
    if diag and fresh and diag_sid == session_id:
        return True, diag_pid or status_pid or known_pid, "diag_alive", evidence

    need_scan = deep_scan and bool(
        quick_pids
        or phase in INIT_COLLECTOR_PHASES
        or grace_sec > 0.0
        or (status_sid and status_sid == session_id)
        or (diag_sid and diag_sid == session_id)
    )
    if need_scan:
        roots = [p for p in (launcher_pid, known_pid, status_pid, diag_pid) if p > 0]
        scanned = scan_collector_pids(worker_id)
        descendants = find_descendant_collector_pids(roots, worker_id)
        for pid in (*scanned, *descendants):
            if pid > 0 and pid not in candidates:
                candidates.append(pid)
        evidence["candidates"] = candidates
        for pid in candidates:
            if not _pid_alive(pid):
                continue
            if not _is_collector_process(pid, worker_id):
                continue
            if status_sid and status_sid != session_id:
                continue
            if diag_sid and diag_sid != session_id:
                continue
            return True, pid, "ok", evidence
        if fresh and (phase in INIT_COLLECTOR_PHASES or grace_sec > 0.0):
            fallback = diag_pid or status_pid or (candidates[0] if candidates else 0)
            if fallback > 0:
                return True, fallback, "initializing", evidence
    elif not deep_scan and fresh and status_sid and status_sid == session_id:
        trust_pid = status_pid or known_pid or launcher_pid or diag_pid
        if trust_pid > 0 and _pid_alive(trust_pid):
            return True, trust_pid, "ok", evidence
    if fresh and phase in INIT_COLLECTOR_PHASES and _diag_open(worker_id):
        if not diag:
            diag = _diag_snapshot(worker_id)
        if diag and (not session_id or str(diag.get("session_id") or "") == session_id):
            init_pid = int(diag.get("collector_pid") or status_pid or known_pid or 0)
            return True, init_pid, "initializing", evidence
    if fresh and phase in INIT_COLLECTOR_PHASES and status_sid == session_id:
        init_pid = status_pid or known_pid or diag_pid
        if init_pid > 0 or _diag_open(worker_id):
            return True, init_pid, "initializing", evidence
    return False, known_pid or status_pid or diag_pid, "collector_real_process_died", evidence


def archive_collector_failure(
    worker_id: int,
    session_id: str,
    reason: str,
    *,
    repo_root: Path | None = None,
    launcher_pid: int = 0,
    known_pid: int = 0,
    stdout_log: str = "",
    stderr_log: str = "",
    orchestrator_note: str = "",
) -> Path | None:
    root = Path(repo_root or REPO)
    archive_dir = root / "ac_workers" / "status" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    alive, canonical, _reason, evidence = collector_evidence(
        worker_id, session_id, known_pid, launcher_pid, repo_root=root
    )
    payload = {
        "worker_id": worker_id,
        "session_id": session_id,
        "reason": reason,
        "orchestrator_note": orchestrator_note,
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "launcher_pid": launcher_pid,
        "known_pid": known_pid,
        "canonical_pid": canonical,
        "alive_at_archive": alive,
        "evidence": evidence,
        "status": read_worker_status(worker_id, root),
        "diag": _diag_snapshot(worker_id),
        "processes": [
            {"pid": pid, "cmdline": _process_command_line(pid), "alive": _pid_alive(pid)}
            for pid in evidence.get("candidates", [])
        ],
        "stdout_log": stdout_log,
        "stderr_log": stderr_log,
    }
    path = archive_dir / "worker_{0}_{1}_failure.json".format(worker_id, session_id)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def resolve_real_collector_pid(
    worker_id: int,
    session_id: str,
    launcher_pid: int | None = None,
    *,
    repo_root: Path | None = None,
    timeout_sec: float = 120.0,
    poll_sec: float = 0.25,
) -> tuple[int | None, str]:
    _setup_paths(repo_root)
    deadline = time.time() + timeout_sec
    last_error = "collector_real_pid_not_reported"
    launcher = int(launcher_pid or 0)
    while time.time() < deadline:
        alive, pid, reason, evidence = collector_evidence(
            worker_id,
            session_id,
            launcher,
            launcher,
            repo_root=repo_root,
            grace_sec=GRACE_SEC,
        )
        reported = (
            evidence.get("status_sid") == session_id and evidence.get("status_pid") == pid
        ) or (
            evidence.get("diag_sid") == session_id and evidence.get("diag_pid") == pid
        )
        if alive and pid > 0 and reported:
            return pid, "ok"
        last_error = reason if reason != "ok" else last_error
        time.sleep(poll_sec)
    return None, last_error


def canonical_collector_pid(
    worker_id: int,
    manifest_entry: dict[str, Any],
    status: dict[str, Any] | None = None,
) -> int:
    status = status or {}
    snap_pid = int(status.get("collector_pid") or 0)
    if snap_pid > 0:
        return snap_pid
    return int(manifest_entry.get("collectorPid") or 0)


def collector_process_alive(
    worker_id: int,
    manifest_entry: dict[str, Any],
    status: dict[str, Any] | None = None,
    session_id: str = "",
) -> tuple[bool, int, str]:
    status = status or {}
    launcher_pid = int(manifest_entry.get("collectorLauncherPid") or 0)
    known_pid = canonical_collector_pid(worker_id, manifest_entry, status)
    sid = str(session_id or status.get("session_id") or "")
    if not sid:
        if known_pid <= 0:
            return False, 0, "collector_real_pid_not_reported"
        if not _pid_alive(known_pid):
            return False, known_pid, "collector_real_process_died"
        if not _is_collector_process(known_pid, worker_id):
            return False, known_pid, "collector_pid_recycled"
        return True, known_pid, "ok"
    alive, pid, reason, _ev = collector_evidence(
        worker_id, sid, known_pid, launcher_pid, grace_sec=GRACE_SEC
    )
    if alive and pid > 0:
        return True, pid, reason
    return False, pid or known_pid, reason


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="collector_pid")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_resolve = sub.add_parser("resolve")
    p_resolve.add_argument("--worker-id", type=int, required=True)
    p_resolve.add_argument("--session-id", required=True)
    p_resolve.add_argument("--launcher-pid", type=int, default=0)
    p_resolve.add_argument("--repo-root", type=Path, default=REPO)
    p_resolve.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    if args.cmd == "resolve":
        launcher = int(args.launcher_pid or 0) or None
        pid, err = resolve_real_collector_pid(
            args.worker_id,
            args.session_id,
            launcher,
            repo_root=args.repo_root,
            timeout_sec=args.timeout,
        )
        payload = {"ok": pid is not None, "collector_pid": pid, "error": err if pid is None else ""}
        print(json.dumps(payload))
        return 0 if pid else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
