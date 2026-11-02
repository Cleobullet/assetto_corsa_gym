"""Global lifecycle control: hard clean, health check, clean bootstrap.

This is a system *lifecycle* layer that sits above the RL logic. It does not
touch training, the Drive pipeline, vJoy semantics or the timeline system; it
only guarantees a clean process/port state before a run and provides a single
reliable kill-switch.

It builds on the existing pieces instead of duplicating them:
  - ac_workers.emergency_stop.emergency_stop(close_game=True) already does the
    vJoy-safe graceful collector stop, learner/transition-server stop, vJoy
    neutralization, AND orphan acs.exe kill. We reuse it as the kill primitive.
  - This module adds the *verification* the previous flow lacked: detect orphan
    acs.exe, busy ports and zombie collectors, and refuse to start until clean.

Design rules: deterministic, idempotent, safe to run repeatedly, independent of
RL/timeline code.
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "assetto_corsa_gym") not in sys.path:
    sys.path.insert(0, str(REPO / "assetto_corsa_gym"))

MAX_WORKERS = 6
TRANSITION_PORT = 24050


def all_known_ports(worker_ids=range(MAX_WORKERS)) -> list[int]:
    """Every TCP port the system uses for the given workers (+ transition srv)."""
    from ac_workers.worker_ports import worker_ports

    ports: set[int] = {TRANSITION_PORT}
    for wid in worker_ids:
        p = worker_ports(int(wid))
        ports.update((p.ego, p.opponents, p.mgmt, p.diag))
    return sorted(ports)


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """True if the port cannot be bound. Bind-test (non-Windows fallback only).

    NOTE: a bind-test also fails for a port left in TIME_WAIT by a just-killed
    server, so it over-reports on Windows right after emergency-stop. Health uses
    listening_ports() instead; this remains the fallback where netstat is absent.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        sock.bind((host, int(port)))
        return False
    except OSError:
        return True
    finally:
        sock.close()


def listening_ports() -> set[int]:
    """Set of TCP ports in LISTENING state (single netstat pass).

    Unlike a bind-test this ignores sockets in TIME_WAIT left by a just-killed
    server, so the health check stops reporting a false DIRTY for the few seconds
    after emergency-stop releases the transition server (port 24050).
    """
    if os.name != "nt":
        return set()
    try:
        out = subprocess.check_output(
            ["netstat", "-ano", "-p", "TCP"], text=True, errors="replace", timeout=10
        )
    except Exception:
        return set()
    return parse_listening_ports(out)


def parse_listening_ports(netstat_output: str) -> set[int]:
    """Extract LISTENING local ports from `netstat -ano` output."""
    ports: set[int] = set()
    for line in netstat_output.splitlines():
        if "LISTENING" not in line.upper():
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        _, _, port_s = parts[1].rpartition(":")
        if port_s.isdigit():
            ports.add(int(port_s))
    return ports


def busy_ports(worker_ids=range(MAX_WORKERS), *, probe=None, listening=None) -> list[int]:
    known = all_known_ports(worker_ids)
    if probe is not None:
        return [p for p in known if probe(p)]
    if listening is None and os.name == "nt":
        listening = listening_ports()
    if listening is None:
        # Non-Windows / netstat unavailable: fall back to the bind-test.
        return [p for p in known if port_in_use(p)]
    return [p for p in known if p in listening]


def _ac_process_rows() -> list:
    import json
    ps = ("@(Get-Process -Name acs,AssettoCorsa -ErrorAction SilentlyContinue | "
          "Select-Object Id,Path) | ConvertTo-Json -Compress")
    out = subprocess.check_output(
        ["powershell", "-NoProfile", "-Command", ps],
        text=True, errors="replace", timeout=15)
    rows = json.loads(out) if out.strip() else []
    if isinstance(rows, dict):
        rows = [rows]
    return rows


def is_ai_clone_ac(executable: str, ai_root: Path) -> bool:
    path = Path(executable).resolve()
    return path == ai_root or ai_root in path.parents


def is_stray_official_ac(executable: str, ai_root: Path) -> bool:
    """Steam's normal Assetto Corsa, not the AI clone."""
    path = Path(executable)
    if path.name.lower() not in ("assettocorsa.exe", "acs.exe"):
        return False
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    if resolved == ai_root or ai_root in resolved.parents:
        return False
    return "assettocorsa" in str(resolved).lower()


def scan_orphan_acs(repo: Path = REPO) -> list[int]:
    """PIDs of acs.exe launched from the AI clone (orphans if no run is active)."""
    ai_root = (repo / "AssettoCorsa-AI").resolve()
    if not ai_root.is_dir():
        return []
    result = []
    for row in _ac_process_rows():
        executable = row.get("Path")
        if not executable:
            raise RuntimeError("AC process path unavailable; cannot verify cleanup")
        if is_ai_clone_ac(executable, ai_root):
            result.append(int(row["Id"]))
    return result


def scan_stray_official_ac(repo: Path = REPO) -> list[int]:
    """PIDs of the normal Steam Assetto Corsa menu/game, outside the AI clone."""
    ai_root = (repo / "AssettoCorsa-AI").resolve()
    result = []
    for row in _ac_process_rows():
        executable = row.get("Path")
        if not executable:
            continue
        if is_stray_official_ac(executable, ai_root):
            result.append(int(row["Id"]))
    return result


def scan_zombie_collectors(worker_ids=range(MAX_WORKERS)) -> dict:
    """Map worker_id -> live collector pids still bound to its diag identity."""
    try:
        from ac_workers.collector_pid import scan_collector_pids
    except Exception:
        return {}
    out: dict = {}
    for wid in worker_ids:
        try:
            pids = [int(p) for p in scan_collector_pids(int(wid))]
        except Exception:
            pids = []
        if pids:
            out[int(wid)] = pids
    return out


def system_health(
    repo: Path = REPO,
    worker_ids=range(MAX_WORKERS),
    *,
    acs_scan=scan_orphan_acs,
    port_scan=busy_ports,
    collector_scan=scan_zombie_collectors,
) -> dict:
    """Structured snapshot of anything that would poison a fresh start."""
    orphan_acs = list(acs_scan(repo))
    ports = list(port_scan(worker_ids))
    zombies = dict(collector_scan(worker_ids))
    clean = not orphan_acs and not ports and not zombies
    return {
        "clean": clean,
        "orphan_acs": orphan_acs,
        "busy_ports": ports,
        "zombie_collectors": zombies,
    }


def assert_clean_state(
    repo: Path = REPO, worker_ids=range(MAX_WORKERS), **scans
) -> tuple[bool, dict]:
    """(is_clean, health). Callers must BLOCK start when is_clean is False."""
    health = system_health(repo, worker_ids, **scans)
    return bool(health["clean"]), health


def kill_all_systems(
    repo: Path = REPO,
    worker_ids=range(MAX_WORKERS),
    *,
    emergency_fn=None,
    settle_sec: float = 2.0,
    sleep_fn=time.sleep,
    health_fn=system_health,
) -> dict:
    """Hard, vJoy-safe global clean. Idempotent; safe to run repeatedly.

    Order matters: emergency_stop(close_game=True) first asks collectors to
    relinquish vJoy gracefully (avoids AcquireVJD FREE), then kills acs.exe,
    learner and transition server. We then settle and re-scan to report what
    (if anything) survived.
    """
    if emergency_fn is None:
        from ac_workers.emergency_stop import emergency_stop as emergency_fn

    report = {
        "acs_killed": [],
        "python_processes_killed": 0,
        "ports_freed": [],
        "orphan_detected": [],
        "errors": [],
    }
    before = health_fn(repo, worker_ids)
    try:
        em = emergency_fn(close_game=True)
    except Exception as exc:  # never raise from a kill-switch
        report["errors"].append("emergency_stop: {0}".format(exc))
        em = {}
    stopped = em.get("stopped", []) if isinstance(em, dict) else []
    report["acs_killed"] = [
        s.get("acPid") for s in stopped if isinstance(s, dict) and s.get("acPid")
    ]
    report["python_processes_killed"] = sum(
        1 for s in stopped if isinstance(s, dict) and not s.get("acPid")
    )
    if settle_sec > 0:
        sleep_fn(settle_sec)
    after = health_fn(repo, worker_ids)
    freed = sorted(set(before["busy_ports"]) - set(after["busy_ports"]))
    report["ports_freed"] = freed
    report["orphan_detected"] = list(after["orphan_acs"])
    report["still_busy_ports"] = list(after["busy_ports"])
    report["zombie_collectors"] = dict(after["zombie_collectors"])
    report["clean_after"] = bool(after["clean"])
    return report


def format_health(health: dict) -> str:
    lines = ["System health: {0}".format("CLEAN" if health["clean"] else "DIRTY")]
    lines.append("  orphan acs.exe   : {0}".format(health["orphan_acs"] or "none"))
    lines.append("  busy ports       : {0}".format(health["busy_ports"] or "none"))
    zc = health["zombie_collectors"]
    lines.append("  zombie collectors: {0}".format(zc or "none"))
    if not health["clean"]:
        lines.append("  => NOT safe to start. Run kill-all (or emergency-stop) first.")
    return "\n".join(lines)


def format_kill_report(report: dict) -> str:
    lines = ["kill-all complete:"]
    lines.append("  acs.exe killed       : {0}".format(report["acs_killed"] or "none"))
    lines.append("  python procs killed  : {0}".format(report["python_processes_killed"]))
    lines.append("  ports freed          : {0}".format(report["ports_freed"] or "none"))
    if report.get("c4d_stopped"):
        lines.append("  c4d processes killed : {0}".format(report["c4d_stopped"]))
    if report.get("errors"):
        lines.append("  errors               : {0}".format(report["errors"]))
    if report.get("clean_after"):
        lines.append("  state                : CLEAN (safe to start)")
    else:
        lines.append("  state                : DIRTY — survivors below")
        lines.append("    orphan acs.exe     : {0}".format(report["orphan_detected"] or "none"))
        lines.append("    still-busy ports   : {0}".format(report.get("still_busy_ports") or "none"))
        lines.append("    zombie collectors  : {0}".format(report.get("zombie_collectors") or "none"))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ac_workers.system_control")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("kill-all", help="hard global clean (acs.exe + collectors + learner + C4D)")
    sub.add_parser("status", help="system health (orphan acs.exe, busy ports, zombies)")
    sub.add_parser("health", help="alias of status")
    args = parser.parse_args(argv)

    if args.cmd in ("status", "health"):
        print(format_health(system_health()))
        return 0
    if args.cmd == "kill-all":
        report = kill_all_systems()
        try:
            from ac_workers.c4d_server_lock import stop_all_c4d_for_repo

            report["c4d_stopped"] = stop_all_c4d_for_repo(REPO, 24700)
        except Exception as exc:
            report.setdefault("errors", []).append("c4d_stop: {0}".format(exc))
        print(format_kill_report(report))
        return 0 if report.get("clean_after") else 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
