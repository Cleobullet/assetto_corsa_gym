"""Runtime health check for transition server (check only — no start)."""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _setup_sys_path(repo_root: Path) -> None:
    for p in (repo_root, repo_root / "assetto_corsa_gym"):
        sp = str(p)
        if sp not in sys.path:
            sys.path.insert(0, sp)


def _result(**kwargs) -> dict:
    out = {
        "ok": False,
        "listenerPresent": False,
        "owningPid": None,
        "projectOwned": False,
        "pingOk": False,
        "stale": False,
        "action": "none",
        "error": None,
        "commandLine": None,
        "pid": None,
        "port": None,
        "started_at": None,
        "reused": False,
        "healthy": False,
        "manifestPid": None,
    }
    out.update(kwargs)
    if out["pid"] is None and out["owningPid"] is not None:
        out["pid"] = out["owningPid"]
    if out["commandLine"] is None and kwargs.get("commandline"):
        out["commandLine"] = kwargs["commandline"]
    if out["ok"] and not out["healthy"]:
        out["healthy"] = True
    return out


def check_transition_server(repair_manifest: bool = False) -> dict:
    repo_root = REPO
    _setup_sys_path(repo_root)

    from ac_workers.transition_server_process import (
        detect_transition_server_pid,
        get_process_command_line,
        is_port_listening,
        ping_transition_server,
    )
    from AssettoCorsaEnv.ac_worker_runtime import read_manifest, write_manifest
    from common.transition_server import transition_server_port

    port = transition_server_port()
    manifest = read_manifest()
    manifest_pid = manifest.get("transitionServerPid")
    listener_present = is_port_listening(port)
    ping_ok = ping_transition_server(port, timeout=1.0) if listener_present else False

    info = detect_transition_server_pid(manifest, port) if listener_present else {}
    listener_pid = info.get("pid")
    recognized = bool(info.get("recognized"))
    cmd = info.get("commandline") or (
        get_process_command_line(int(listener_pid)) if listener_pid else None
    )

    stale = bool(manifest_pid and listener_pid and int(manifest_pid) != int(listener_pid))
    ok = bool(listener_present and ping_ok and recognized and listener_pid)

    started_at = manifest.get("transitionServerStartedAt")
    if ok and repair_manifest:
        launcher_pid = manifest.get("transitionServerLauncherPid")
        manifest["transitionServerPid"] = int(listener_pid)
        manifest["transitionServerPort"] = port
        manifest["transitionServerCommandLine"] = cmd
        manifest["transitionServerHealthy"] = True
        if launcher_pid is not None:
            manifest["transitionServerLauncherPid"] = int(launcher_pid)
        if not started_at:
            started_at = datetime.now(timezone.utc).isoformat()
            manifest["transitionServerStartedAt"] = started_at
        write_manifest(manifest)

    action = "reused" if ok else "none"
    if stale and ok:
        action = "repaired_manifest"

    return _result(
        ok=ok,
        listenerPresent=listener_present,
        owningPid=listener_pid,
        projectOwned=recognized,
        pingOk=ping_ok,
        stale=stale,
        action=action,
        error=None if ok else _failure_reason(listener_present, ping_ok, recognized, listener_pid, port),
        commandLine=cmd,
        pid=int(listener_pid) if listener_pid else None,
        port=port,
        started_at=started_at,
        reused=ok,
        healthy=ok,
        manifestPid=manifest_pid,
    )


def _failure_reason(
    listener_present: bool,
    ping_ok: bool,
    recognized: bool,
    listener_pid: int | None,
    port: int,
) -> str:
    if not listener_present:
        return "no listener on port {0}".format(port)
    if not recognized:
        return "listener pid={0} not project transition server".format(listener_pid)
    if not ping_ok:
        return "health ping failed on port {0}".format(port)
    return "transition server check failed"


def wait_for_health(port: int | None = None, timeout_sec: float = 10.0) -> dict:
    _setup_sys_path(REPO)
    from ac_workers.transition_server_process import ping_transition_server
    from common.transition_server import transition_server_port

    p = port if port is not None else transition_server_port()
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if ping_transition_server(p, timeout=1.0):
            return check_transition_server(repair_manifest=False)
        time.sleep(0.2)
    return _result(
        listenerPresent=True,
        pingOk=False,
        action="failed",
        error="health ping timeout on port {0}".format(p),
        port=p,
    )


def write_manifest_from_check(info: dict) -> None:
    _setup_sys_path(REPO)
    from AssettoCorsaEnv.ac_worker_runtime import read_manifest, write_manifest
    from common.transition_server import transition_server_port

    if not info.get("ok"):
        return
    manifest = read_manifest()
    manifest["transitionServerPid"] = info.get("pid")
    manifest["transitionServerPort"] = info.get("port", transition_server_port())
    manifest["transitionServerStartedAt"] = info.get("started_at") or datetime.now(
        timezone.utc
    ).isoformat()
    manifest["transitionServerCommandLine"] = info.get("commandLine")
    manifest["transitionServerHealthy"] = True
    write_manifest(manifest)


def main() -> int:
    result = _result(error="no action specified")
    try:
        parser = argparse.ArgumentParser()
        parser.add_argument("--check", action="store_true")
        parser.add_argument("--verify", action="store_true")
        parser.add_argument("--wait-ping", action="store_true")
        parser.add_argument("--repair-manifest", action="store_true")
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--timeout", type=float, default=10.0)
        args = parser.parse_args()

        if args.check or args.verify:
            result = check_transition_server(repair_manifest=args.repair_manifest or args.verify)
        elif args.wait_ping:
            result = wait_for_health(timeout_sec=args.timeout)
            if result.get("ok") and args.repair_manifest:
                write_manifest_from_check(result)
        else:
            result = _result(error="no action specified")
    except Exception as exc:
        result = _result(error=str(exc), action="failed")
        _ = traceback.format_exc()

    print(json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
