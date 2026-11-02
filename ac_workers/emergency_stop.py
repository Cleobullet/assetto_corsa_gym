"""Emergency stop: learner, collectors, transition server, vJoy neutral (C4D)."""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))

from ac_workers.collector_safety import neutral_all_vjoy
from ac_workers.transition_server_process import stop_transition_server

# How long to wait for collectors to relinquish vJoy and exit on their own before
# we resort to TerminateProcess (which would leak a stuck vJoy PDO handle).
GRACEFUL_COLLECTOR_STOP_SEC = 4.0


def _pid_alive(pid: int) -> bool:
    try:
        from ac_workers.control_center import pid_alive

        return bool(pid_alive(int(pid)))
    except Exception:
        return True


def _graceful_stop_collectors(manifest: dict, grace_sec: float = GRACEFUL_COLLECTOR_STOP_SEC) -> list[int]:
    """Ask live collectors to relinquish vJoy and exit cleanly before force-kill.

    Returns the pids that exited within the grace window. A clean exit runs the
    collector's ``finally`` (RelinquishVJD), so the next run does not hit
    ``AcquireVJD failed status=1 (FREE)``.
    """
    from ac_workers.collector_pid import scan_collector_pids
    from ac_workers.collector_shutdown import clear_collector_shutdown, request_collector_shutdown

    targets: dict[int, int] = {}
    requested: set[int] = set()

    def _request(wid: int) -> None:
        if wid not in requested:
            request_collector_shutdown(wid)
            requested.add(wid)

    for w in manifest.get("workers", []):
        wid = w.get("workerId")
        if wid is None:
            continue
        wid = int(wid)
        pid = w.get("collectorPid")
        if pid and int(pid) > 0:
            targets[int(pid)] = wid
        _request(wid)
    for wid in range(6):
        for pid in scan_collector_pids(wid):
            targets[int(pid)] = wid
            _request(wid)

    exited: list[int] = []
    if targets:
        pending = dict(targets)
        deadline = time.monotonic() + max(0.0, grace_sec)
        while pending and time.monotonic() < deadline:
            time.sleep(0.2)
            for pid in list(pending):
                if not _pid_alive(pid):
                    exited.append(pid)
                    pending.pop(pid, None)
    for wid in requested:
        clear_collector_shutdown(wid)
    return exited


def _stop_pid(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except (OSError, AttributeError):
        try:
            import subprocess

            result = subprocess.run(["taskkill", "/PID", str(pid), "/F"], check=False, capture_output=True)
            return result.returncode == 0
        except Exception:
            return False


def _kill_ai_acs_processes(repo: Path) -> list[int]:
    """Stop orphan acs.exe launched from the AI install (manifest may be stale)."""
    from ac_workers.system_control import scan_orphan_acs
    return [pid for pid in scan_orphan_acs(repo) if _stop_pid(pid)]


def _kill_stray_official_ac(repo: Path) -> list[int]:
    """Stop the normal Steam Assetto Corsa that a cold start can open beside the workers."""
    from ac_workers.system_control import scan_stray_official_ac
    return [pid for pid in scan_stray_official_ac(repo) if _stop_pid(pid)]


def emergency_stop(close_game: bool = False) -> dict:
    from AssettoCorsaEnv.ac_worker_runtime import read_manifest, write_manifest

    report = {"vjoy_neutralized": {}, "stopped": [], "transition_server": {}, "errors": [], "graceful_exited": []}

    manifest = read_manifest()

    # Give collectors a chance to relinquish vJoy and exit cleanly BEFORE any hard
    # kill. A TerminateProcess skips their finally -> leaked stuck vJoy PDO handle ->
    # AcquireVJD status=1 (FREE) on the next run.
    try:
        report["graceful_exited"] = _graceful_stop_collectors(manifest)
    except Exception as exc:
        report["errors"].append("graceful_stop: {0}".format(exc))

    listener_pid = manifest.get("transitionServerPid")
    launcher_pid = manifest.get("transitionServerLauncherPid")
    if launcher_pid and listener_pid and int(launcher_pid) != int(listener_pid):
        if _stop_pid(int(launcher_pid)):
            report["stopped"].append({"transitionServerLauncherPid": int(launcher_pid)})
    elif launcher_pid and not listener_pid:
        if _stop_pid(int(launcher_pid)):
            report["stopped"].append({"transitionServerLauncherPid": int(launcher_pid)})

    stopped_pids: set[int] = set()

    def stop_once(pid: int, label: dict) -> None:
        if pid <= 0 or pid in stopped_pids:
            return
        if _stop_pid(pid):
            stopped_pids.add(pid)
            report["stopped"].append(label)

    for w in manifest.get("workers", []):
        for field in ("collectorPid", "collectorLauncherPid"):
            pid = w.get(field)
            if pid:
                stop_once(int(pid), {field: int(pid), "worker": w.get("workerId")})
        if close_game:
            pid = w.get("acPid")
            if pid:
                stop_once(int(pid), {"acPid": int(pid), "worker": w.get("workerId")})

    if close_game:
        try:
            for pid in _kill_ai_acs_processes(REPO):
                stop_once(pid, {"acPid": pid, "orphan_acs": True})
        except Exception as exc:
            report["errors"].append("kill_ai_acs: {0}".format(exc))
        try:
            for pid in _kill_stray_official_ac(REPO):
                stop_once(pid, {"acPid": pid, "official_ac": True})
        except Exception as exc:
            report["errors"].append("kill_official_ac: {0}".format(exc))

    # Manifest may be empty while failed collectors still hold diag ports.
    from ac_workers.collector_pid import scan_collector_pids

    for wid in range(6):
        for pid in scan_collector_pids(wid):
            stop_once(pid, {"collectorPid": pid, "worker": wid, "orphan": True})

    learner_pid = manifest.get("learnerPid")
    if learner_pid:
        stop_once(int(learner_pid), {"learnerPid": int(learner_pid)})

    # Neutralize vJoy only after feeders are stopped — acquiring all devices while
    # collectors still OWN them corrupts the driver during scale-test handoffs.
    report["vjoy_neutralized"] = neutral_all_vjoy(6)
    try:
        from ac_workers.vjoy_session import prep_vjoy_device

        for dev in range(1, 7):
            prep_vjoy_device(dev, reset_free=True)
    except Exception:
        pass

    report["transition_server"] = stop_transition_server(manifest=manifest, wait_sec=5.0)
    report["transition_server"]["port"] = report["transition_server"].get("port") or 24050

    manifest["learnerPid"] = None
    manifest["transitionServerPid"] = None
    manifest["transitionServerLauncherPid"] = None
    manifest["transitionServerHealthy"] = False
    try:
        write_manifest(manifest)
    except Exception as exc:
        report["errors"].append("manifest: {0}".format(exc))

    # Stop the C4D server itself. If it survives, its scale-test loop keeps
    # relaunching the workers in the background (observed: an "arret d'urgence"
    # did not hold because the C4D re-launched all six workers right after).
    try:
        from ac_workers.c4d_server_lock import stop_all_c4d_for_repo

        report["c4d_stopped"] = stop_all_c4d_for_repo(REPO, 24700)
    except Exception as exc:
        report["errors"].append("c4d_stop: {0}".format(exc))

    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--close-game", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        print("Emergency-Stop dry-run: OK")
        return 0
    report = emergency_stop(close_game=args.close_game)
    ts = report["transition_server"]
    if ts.get("detected_pid") and not ts.get("stopped") and not ts.get("skipped_unknown"):
        pass
    elif not ts.get("detected_pid") and ts.get("port_released"):
        print("transition server port {0} released".format(ts.get("port") or 24050))
    if args.close_game:
        from ac_workers.system_control import system_health, format_health
        from ac_workers.system_control import scan_stray_official_ac
        health = system_health(REPO)
        stray = scan_stray_official_ac(REPO)
        if stray:
            health["clean"] = False
            print("official Assetto Corsa still open: {0}".format(stray))
        if health.get("busy_ports") or health.get("orphan_acs") or health.get("zombie_collectors") or stray:
            print("Emergency-Stop-AI: INCOMPLETE")
            print(format_health(health))
            return 1
    print("Emergency-Stop-AI: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
