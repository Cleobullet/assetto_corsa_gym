"""AI control link test via collector diagnostic channel (no concurrent UDP)."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))

from ac_workers.collector_diag import CollectorDiagClient, DiagRequestError, diag_port
from ac_workers.collector_status import validate_snapshot
from ac_workers.worker_handshake import mgmt_static_info, tcp_open
from AssettoCorsaEnv.ac_worker_runtime import _pid_alive, load_worker_config, read_manifest
from common.transition_server import TransitionClient


def _acs_worker_alive(worker_id: int) -> tuple[bool, int | None]:
    manifest = read_manifest()
    for w in manifest.get("workers", []):
        if int(w.get("workerId", -1)) == worker_id:
            pid = w.get("acPid")
            if not pid:
                return False, None
            pid = int(pid)
            return _pid_alive(pid), pid
    return False, None


def _collector_alive(worker_id: int) -> tuple[bool, int | None]:
    manifest = read_manifest()
    for w in manifest.get("workers", []):
        if int(w.get("workerId", -1)) == worker_id:
            pid = w.get("collectorPid")
            if not pid:
                return False, None
            pid = int(pid)
            return _pid_alive(pid), pid
    return False, None


def _manifest_session(worker_id: int) -> tuple[str | None, int | None]:
    manifest = read_manifest()
    session_id = manifest.get("sessionId") or manifest.get("sessionStamp")
    collector_pid = None
    for w in manifest.get("workers", []):
        if int(w.get("workerId", -1)) == worker_id:
            collector_pid = w.get("collectorPid")
            break
    return (str(session_id) if session_id else None, int(collector_pid) if collector_pid else None)


def _diag_available(worker_id: int) -> bool:
    import socket

    port = diag_port(worker_id)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _fetch_diag_status(diag: CollectorDiagClient, read_only: bool) -> dict[str, Any]:
    cmd = "readonly_check" if read_only else "status"
    resp = diag.request(cmd)
    if not resp.get("ok", True):
        raise DiagRequestError(resp.get("error") or "diag {0} failed".format(cmd))
    return resp


def run_readonly_probe(
    worker_id: int,
    session_id: str | None = None,
    collector_pid: int | None = None,
) -> tuple[bool, dict[str, Any], list[str]]:
    errors: list[str] = []
    diag = CollectorDiagClient(worker_id)
    try:
        st0 = _fetch_diag_status(diag, read_only=True)
    except DiagRequestError as exc:
        return False, {}, [str(exc.message)]

    ok0, val_errors0, _ = validate_snapshot(
        st0, session_id=session_id, collector_pid=collector_pid, require_transitions=False
    )
    errors.extend(val_errors0)
    time.sleep(0.5)
    try:
        st1 = diag.request("status")
    except DiagRequestError as exc:
        return False, st0, [str(exc.message)]

    ok1, val_errors1, _ = validate_snapshot(
        st1, session_id=session_id, collector_pid=collector_pid, require_transitions=False
    )
    errors.extend(val_errors1)

    tc0 = int(st0.get("transition_count", 0))
    tc1 = int(st1.get("transition_count", 0))
    transition_ok = tc1 > tc0 or tc0 > 0
    if not transition_ok:
        errors.append("transition_count not increasing (was {0}, now {1})".format(tc0, tc1))

    hb0 = st0.get("heartbeat_monotonic")
    hb1 = st1.get("heartbeat_monotonic")
    if hb0 is not None and hb1 is not None and float(hb1) < float(hb0):
        errors.append("heartbeat_monotonic regressed")

    telem0 = float(st0.get("telemetry_age_ms", 999999))
    telem1 = float(st1.get("telemetry_age_ms", 999999))
    if telem0 > 2000 or telem1 > 2000:
        errors.append("telemetry stale age_ms={0}/{1}".format(telem0, telem1))

    ok = ok0 and ok1 and transition_ok and len(errors) == 0
    return ok, {"first": st0, "second": st1}, errors


def run_control_link_test(
    worker_id: int = 0,
    read_only: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    cfg = load_worker_config(worker_id)
    session_id, collector_pid = _manifest_session(worker_id)
    result: dict[str, Any] = {
        "worker_id": worker_id,
        "acs_alive": False,
        "collector_alive": False,
        "management_ready": False,
        "static_info_ready": False,
        "telemetry_ready": False,
        "controls_ready": False,
        "observation_ready": False,
        "executed_action_confirmed": read_only,
        "transition_ready": False,
        "learner_connected": False,
        "errors": [],
        "read_only": read_only,
        "dry_run": dry_run,
    }

    if dry_run:
        result.update(
            {
                "acs_alive": True,
                "collector_alive": True,
                "management_ready": True,
                "static_info_ready": True,
                "telemetry_ready": True,
                "controls_ready": True,
                "observation_ready": True,
                "executed_action_confirmed": True,
                "transition_ready": True,
                "learner_connected": True,
                "ok": True,
            }
        )
        return result

    alive, pid = _acs_worker_alive(worker_id)
    result["acs_pid"] = pid
    result["acs_alive"] = alive
    if not alive:
        result["errors"].append("acs.exe worker {0} not running (pid={1})".format(worker_id, pid))

    col_alive, col_pid = _collector_alive(worker_id)
    result["collector_pid"] = col_pid
    result["collector_alive"] = col_alive
    if not col_alive:
        result["errors"].append("collector worker {0} not running".format(worker_id))
        result["ok"] = False
        return result

    if collector_pid is None:
        collector_pid = col_pid

    if not _diag_available(worker_id):
        result["errors"].append("collector diagnostic port {0} closed".format(diag_port(worker_id)))
        result["ok"] = False
        return result

    if tcp_open("127.0.0.1", cfg.simulation_management_server_port):
        result["management_ready"] = True
    else:
        result["errors"].append("management TCP {0} closed".format(cfg.simulation_management_server_port))

    info = mgmt_static_info(cfg.simulation_management_server_port)
    if info and info.get("CarName") and info.get("TrackName"):
        result["static_info_ready"] = True
        result["static_info"] = {"car": info.get("CarName"), "track": info.get("TrackName")}
    else:
        result["errors"].append("get_static_info invalid on port {0}".format(cfg.simulation_management_server_port))

    ro_ok, ro_snaps, ro_errors = run_readonly_probe(worker_id, session_id, collector_pid)
    st = ro_snaps.get("second") or ro_snaps.get("first") or {}
    result["collector_status"] = st
    result["errors"].extend(ro_errors)

    telem_age = float(st.get("telemetry_age_ms", 99999.0))
    result["telemetry_ready"] = telem_age < 2000.0 and bool(st.get("observation_complete"))
    if not result["telemetry_ready"] and not any("telemetry" in e for e in ro_errors):
        result["errors"].append("telemetry stale age_ms={0}".format(telem_age))

    result["controls_ready"] = bool(st.get("vjoy_ready"))
    if not result["controls_ready"]:
        result["errors"].append("vJoy device {0} not ready".format(cfg.vjoy_device_id))

    obs = st.get("last_observation") or []
    snap = st.get("telemetry_snapshot") or {}
    result["observation_ready"] = bool(st.get("observation_complete")) and len(obs) > 0 and bool(snap)
    if not result["observation_ready"]:
        result["errors"].append("observation incomplete from collector")

    tc0 = int((ro_snaps.get("first") or {}).get("transition_count", 0))
    tc1 = int(st.get("transition_count", tc0))
    result["transition_ready"] = tc1 > tc0 or tc0 > 0

    try:
        stats = TransitionClient().stats()
        result["learner_connected"] = bool(stats.get("learner_connected"))
    except OSError as exc:
        result["errors"].append("transition server: {0}".format(exc))

    if read_only:
        result["executed_action_confirmed"] = True
        result["ok"] = (
            ro_ok
            and result["acs_alive"]
            and result["collector_alive"]
            and result["management_ready"]
            and result["static_info_ready"]
            and result["telemetry_ready"]
            and result["observation_ready"]
            and result["transition_ready"]
        )
        return result

    if not ro_ok:
        result["ok"] = False
        return result

    diag = CollectorDiagClient(worker_id)
    if float(st.get("telemetry_age_ms", 999999)) > 2000:
        result["errors"].append("safe_action_test refused: telemetry stale")
        result["ok"] = False
        return result

    try:
        test_resp = diag.request("safe_action_test")
    except DiagRequestError as exc:
        result["errors"].append(str(exc.message))
        result["ok"] = False
        return result

    result["safe_action_test"] = test_resp
    if not test_resp.get("ok"):
        err = test_resp.get("error") or "safe_action_test failed or telemetry unchanged"
        result["errors"].append(err)
    elif test_resp.get("telemetry_changed"):
        result["executed_action_confirmed"] = True
        print("worker {0} executed action confirmed".format(worker_id))
    else:
        result["errors"].append("safe_action_test failed or telemetry unchanged")

    time.sleep(0.3)
    try:
        st_after = diag.request("status")
    except DiagRequestError as exc:
        result["errors"].append(str(exc.message))
        result["ok"] = False
        return result

    tc_after = int(st_after.get("transition_count", tc1))
    result["transition_ready"] = tc_after > tc1 or tc1 > 0
    if result["transition_ready"]:
        print("worker {0} first transition ready".format(worker_id))
    else:
        result["errors"].append("no new transition after safe_action_test")

    result["ok"] = (
        result["acs_alive"]
        and result["collector_alive"]
        and result["management_ready"]
        and result["static_info_ready"]
        and result["telemetry_ready"]
        and result["controls_ready"]
        and result["observation_ready"]
        and result["executed_action_confirmed"]
        and result["transition_ready"]
    )
    if result.get("learner_connected"):
        print("learner connected")
    return result


def log_control_link_result(result: dict[str, Any]) -> None:
    wid = result.get("worker_id", 0)
    if result.get("telemetry_ready"):
        print("worker {0} telemetry ready".format(wid))
    if result.get("controls_ready") and result.get("executed_action_confirmed") and not result.get("read_only"):
        print("worker {0} controls ready".format(wid))
    if result.get("executed_action_confirmed") and not result.get("read_only"):
        print("worker {0} executed action confirmed".format(wid))
    if result.get("transition_ready"):
        print("worker {0} first transition ready".format(wid))
    if result.get("learner_connected"):
        print("learner connected")
    if result.get("ok"):
        print("AI control link: OK")
    else:
        for err in result.get("errors", []):
            print("ERROR: {0}".format(err))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-id", type=int, default=0)
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        result = run_control_link_test(args.worker_id, read_only=args.read_only, dry_run=args.dry_run)
    except DiagRequestError as exc:
        print("ERROR: {0}".format(exc.message))
        return 1
    except Exception as exc:
        print("ERROR: {0}".format(exc))
        return 1
    log_control_link_result(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
