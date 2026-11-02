"""Stale session cleanup: manifest, worker status, learner, transition server."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
STATUS_DIR = REPO / "ac_workers" / "status"
STATUS_ARCHIVE = STATUS_DIR / "archive"
LEARNER_STATUS = REPO / "outputs" / "multi_sac_cpu_v3" / "learner_status.json"
MAX_HEARTBEAT_MS = 5000.0


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _setup_paths(repo_root: Path | None) -> None:
    root = repo_root or REPO
    for p in (root, root / "assetto_corsa_gym"):
        sp = str(p)
        if sp not in sys.path:
            sys.path.insert(0, sp)


def archive_worker_status(worker_id: int, stamp: str | None = None) -> bool:
    path = STATUS_DIR / "worker_{0}.json".format(worker_id)
    if not path.is_file():
        return False
    STATUS_ARCHIVE.mkdir(parents=True, exist_ok=True)
    stamp = stamp or time.strftime("%Y%m%d_%H%M%S")
    dest = STATUS_ARCHIVE / "worker_{0}_{1}.json".format(worker_id, stamp)
    try:
        os.replace(path, dest)
    except OSError:
        path.rename(dest)
    return True


def release_worker_lock(worker_id: int) -> bool:
    try:
        from AssettoCorsaEnv.ac_worker_runtime import load_worker_config, release_worker_lock

        release_worker_lock(load_worker_config(worker_id))
        return True
    except Exception:
        lock = REPO / "ac_workers" / "locks" / "worker_{0}.lock".format(worker_id)
        if lock.is_file():
            lock.unlink()
            return True
        return False


def archive_learner_status() -> bool:
    if not LEARNER_STATUS.is_file():
        return False
    try:
        data = _read_json(LEARNER_STATUS)
        data["learner_running"] = False
        LEARNER_STATUS.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = LEARNER_STATUS.with_name("learner_status_archive_{0}.json".format(stamp))
    try:
        os.replace(LEARNER_STATUS, dest)
    except OSError:
        LEARNER_STATUS.rename(dest)
    return True


def transition_server_online(port: int = 24050) -> bool:
    try:
        from ac_workers.worker_handshake import tcp_open

        return bool(tcp_open("127.0.0.1", port, timeout=0.5))
    except Exception:
        return False


def worker_effective_state(
    worker_id: int,
    manifest_entry: dict[str, Any],
    status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from ac_workers.control_center import (
        is_ac_process,
        pid_alive,
        status_age_ms,
    )

    status = status or {}
    manifest_state = str(manifest_entry.get("state") or "unknown")
    ac_pid = int(manifest_entry.get("acPid") or 0)
    from ac_workers.collector_pid import canonical_collector_pid, collector_process_alive

    col_alive, col_pid, _reason = collector_process_alive(
        worker_id, manifest_entry, status, session_id=str(status.get("session_id") or "")
    )
    if col_pid <= 0:
        col_pid = canonical_collector_pid(worker_id, manifest_entry, status)
    ac_alive = ac_pid > 0 and pid_alive(ac_pid) and is_ac_process(ac_pid)
    hb_fresh = status_age_ms(status) < MAX_HEARTBEAT_MS if status else False

    if ac_alive and col_alive and hb_fresh:
        effective = "running"
    elif ac_pid > 0 or col_pid > 0:
        effective = "stale" if (not ac_alive or not col_alive) else "stale"
    else:
        effective = "offline"
    if not ac_alive and not col_alive and not status:
        effective = "offline"
    return {
        "worker_id": worker_id,
        "manifest_state": manifest_state,
        "effective_state": effective,
        "ac_pid": ac_pid,
        "collector_pid": col_pid,
        "ac_alive": ac_alive,
        "collector_alive": col_alive,
        "heartbeat_fresh": hb_fresh,
    }


def learner_effective_state(
    learner: dict[str, Any] | None = None,
    control_session_id: str | None = None,
) -> dict[str, Any]:
    from ac_workers.control_center import is_learner_really_active

    learner = learner or _read_json(LEARNER_STATUS)
    sid = control_session_id or str(learner.get("session_id") or "")
    live = is_learner_really_active(learner, sid) if learner else False
    return {
        "active": live,
        "status": "running" if live else "stale",
        "learner_pid": int(learner.get("learner_pid") or 0),
        "saved_steps": int(learner.get("global_steps") or learner.get("learner_global_steps") or 0),
        "saved_replay": int(learner.get("replay_size") or learner.get("learner_replay_size") or 0),
    }


def _worker_is_stale_in_manifest(entry: dict[str, Any]) -> bool:
    wid = int(entry.get("workerId", -1))
    if wid < 0:
        return True
    eff = worker_effective_state(wid, entry, _read_json(STATUS_DIR / "worker_{0}.json".format(wid)))
    return eff["effective_state"] != "running"


def cleanup_stale_state(
    repo_root: Path | None = None,
    *,
    worker_ids: list[int] | None = None,
    cleanup_learner: bool = False,
    audit_all: bool = False,
) -> dict[str, Any]:
    """Remove stale manifest/status/lock entries. Never stops live processes."""
    _setup_paths(repo_root)
    from AssettoCorsaEnv.ac_worker_runtime import read_manifest, write_manifest

    report: dict[str, Any] = {
        "removed_workers": [],
        "archived_status": [],
        "locks_removed": [],
        "learner_archived": False,
        "transition_cleared": False,
    }
    manifest = read_manifest()
    to_remove: set[int] = set(int(w) for w in (worker_ids or []))

    if audit_all:
        for entry in list(manifest.get("workers", [])):
            wid = int(entry.get("workerId", -1))
            if wid < 0:
                continue
            if _worker_is_stale_in_manifest(entry):
                to_remove.add(wid)
        manifest_ids = {int(w.get("workerId", -1)) for w in manifest.get("workers", [])}
        for wid in range(6):
            status_path = STATUS_DIR / "worker_{0}.json".format(wid)
            if status_path.is_file() and wid not in manifest_ids:
                to_remove.add(wid)

    archived: set[int] = set()
    for wid in sorted(to_remove):
        if archive_worker_status(wid):
            archived.add(wid)
            report["archived_status"].append(wid)
        if release_worker_lock(wid):
            report["locks_removed"].append(wid)

    if to_remove:
        removed = sorted(to_remove)
        manifest["workers"] = [
            w for w in manifest.get("workers", []) if int(w.get("workerId", -1)) not in to_remove
        ]
        report["removed_workers"] = removed

    learner = _read_json(LEARNER_STATUS)
    manifest_sid = str(manifest.get("sessionId") or "")
    if cleanup_learner or (audit_all and not learner_effective_state(learner, manifest_sid)["active"]):
        if archive_learner_status():
            report["learner_archived"] = True
        manifest["learnerPid"] = None

    if not transition_server_online():
        if manifest.get("transitionServerPid") or manifest.get("transitionServerLauncherPid"):
            manifest["transitionServerPid"] = None
            manifest["transitionServerLauncherPid"] = None
            manifest["transitionServerHealthy"] = False
            report["transition_cleared"] = True

    write_manifest(manifest)
    return report


def effective_report(repo_root: Path | None = None) -> dict[str, Any]:
    _setup_paths(repo_root)
    from AssettoCorsaEnv.ac_worker_runtime import read_manifest

    manifest = read_manifest()
    workers: dict[str, Any] = {}
    for entry in manifest.get("workers", []):
        wid = int(entry.get("workerId", -1))
        if wid < 0:
            continue
        workers[str(wid)] = worker_effective_state(
            wid, entry, _read_json(STATUS_DIR / "worker_{0}.json".format(wid))
        )
    manifest_sid = str(manifest.get("sessionId") or "")
    learner = learner_effective_state(_read_json(LEARNER_STATUS), manifest_sid)
    transition = {
        "online": transition_server_online(),
        "listener_pid": manifest.get("transitionServerPid"),
        "launcher_pid": manifest.get("transitionServerLauncherPid"),
    }
    if not transition["online"]:
        transition["effective_state"] = "offline"
    else:
        transition["effective_state"] = "running"
    return {"workers": workers, "learner": learner, "transition": transition}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", default="", help="comma-separated worker ids to remove")
    parser.add_argument("--learner", action="store_true")
    parser.add_argument("--audit-all", action="store_true")
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()
    if args.report:
        print(json.dumps(effective_report(), indent=2))
        return 0
    worker_ids = []
    if args.workers.strip():
        worker_ids = [int(x.strip()) for x in args.workers.split(",") if x.strip()]
    report = cleanup_stale_state(
        worker_ids=worker_ids or None,
        cleanup_learner=args.learner,
        audit_all=args.audit_all,
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
