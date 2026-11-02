"""Worker readiness via collector diagnostic channel (no UDP probe)."""
from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))

from AssettoCorsaEnv.ac_worker_runtime import load_worker_config
from ac_workers.collector_diag import CollectorDiagClient, DiagRequestError, diag_port
from ac_workers.collector_status import validate_snapshot
from ac_workers.worker_ports import worker_ports, worker_ports_dict


def tcp_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_collector_diag_ping(
    worker_id: int,
    *,
    timeout: float = 1.5,
) -> tuple[bool, bool, str, dict[str, Any]]:
    """Ping the lock-free diag handler.

    Returns (json_received, ping_ok, reason, snap).
    json_received is True when a valid JSON line was returned (even ok=false).
    ping_ok requires ok=true plus collector_pid and boot_id.
    """
    try:
        snap = CollectorDiagClient(worker_id, timeout=timeout).request("ping")
    except DiagRequestError as exc:
        msg = str(exc.message or exc)
        if "refused" in msg.lower():
            return False, False, "diag_unreachable:connection_refused", {}
        if "timeout" in msg.lower():
            return False, False, "diag_unreachable:timeout", {}
        return False, False, "diag_unreachable:{0}".format(msg), {}
    if not snap:
        return False, False, "diag_unreachable:empty_response", {}
    if snap.get("ok") is False:
        err = str(snap.get("error") or "ping_failed")
        return True, False, "diag_error:ping:{0}".format(err), snap
    if snap.get("worker_id") is not None and int(snap.get("worker_id")) != int(worker_id):
        return True, False, "diag_mismatch:worker_id", snap
    if not snap.get("collector_pid"):
        return True, False, "diag_incomplete:missing_collector_pid", snap
    if not str(snap.get("boot_id", "") or "").strip():
        return True, False, "diag_incomplete:missing_boot_id", snap
    return True, True, "ok", snap


def probe_collector_diag_endpoint(
    worker_id: int,
    *,
    file_status: dict[str, Any] | None = None,
    timeout: float = 1.5,
) -> tuple[bool, str, dict[str, Any]]:
    """True when the collector diag port answers ping with a coherent identity.

    Does NOT call tcp_open() first — that wastes the single-threaded diag server's
    time handling an empty connection before the real ping arrives.
    """
    json_ok, ping_ok, reason, snap = probe_collector_diag_ping(worker_id, timeout=timeout)
    if not json_ok:
        return False, reason, snap
    if not ping_ok:
        return False, reason, snap
    file_st = file_status or {}
    file_pid = int(file_st.get("collector_pid") or 0)
    live_pid = int(snap.get("collector_pid") or 0)
    if file_pid > 0 and live_pid > 0 and file_pid != live_pid:
        return False, "diag_pid_mismatch:file={0} live={1}".format(file_pid, live_pid), snap
    return True, "ok", snap


_tcp_open = tcp_open


def mgmt_static_info(port: int, timeout: float = 3.0) -> dict | None:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.sendall(b"get_static_info")
            data = b""
            sock.settimeout(timeout)
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
                if len(data) > 128:
                    break
            if not data:
                return None
            return json.loads(data.decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None


_mgmt_static_info = mgmt_static_info


def vjoy_accessible(device_id: int) -> bool:
    try:
        import AssettoCorsaPlugin.plugins.sensors_par.vjoy as vjoy_mod

        vj = vjoy_mod.vJoy(reference=device_id)
        vj.open()
        vj.close()
        return True
    except Exception:
        return False


_vjoy_accessible = vjoy_accessible


def _tcp_open(host: str, port: int, timeout: float = 1.0) -> bool:
    return tcp_open(host, port, timeout)


def _mgmt_static_info(port: int, timeout: float = 3.0) -> dict | None:
    return mgmt_static_info(port, timeout)


def _diag_request(worker_id: int, cmd: str, timeout: float = 3.0) -> dict | None:
    try:
        return CollectorDiagClient(worker_id, timeout=timeout).request(cmd)
    except (OSError, DiagRequestError):
        return None


def run_worker_handshake(
    worker_id: int,
    require_transition: bool = False,
    session_id: str | None = None,
    collector_pid: int | None = None,
) -> dict:
    # Le lanceur ne doit plus échouer parce que le jeu démarre encore :
    # on réessaie jusqu'à ~20 s pour laisser la télémétrie devenir fraîche.
    deadline = time.time() + 20.0
    last: dict[str, Any] = {}
    while True:
        result = _handshake_once(worker_id, require_transition, session_id, collector_pid)
        last = result
        if result.get("ok"):
            return result
        if time.time() >= deadline:
            return last
        time.sleep(1.0)


def _handshake_once(
    worker_id: int,
    require_transition: bool,
    session_id: str | None,
    collector_pid: int | None,
) -> dict:
    cfg = load_worker_config(worker_id)
    ports = worker_ports(worker_id)
    result = {
        "worker_id": worker_id,
        "diagnostic_ready": False,
        "telemetry_ready": False,
        "management_ready": False,
        "static_info_ready": False,
        "controls_ready": False,
        "transition_server_ready": False,
        "first_transition_ready": False,
        "learner_connected": False,
        "errors": [],
        "ports": worker_ports_dict(worker_id),
    }

    if not tcp_open("127.0.0.1", ports.diag):
        result["errors"].append(
            "collector diagnostic port {0} closed".format(ports.diag)
        )
    else:
        result["diagnostic_ready"] = True

    diag = _diag_request(worker_id, "readonly_check") or _diag_request(worker_id, "status")
    if not diag or not diag.get("ok", True):
        result["errors"].append("collector diagnostic unavailable")
        result["ok"] = False
        return result

    ok_val, val_errors, _details = validate_snapshot(
        diag,
        session_id=session_id,
        collector_pid=collector_pid,
        require_transitions=require_transition,
    )
    result["telemetry_ready"] = float(diag.get("telemetry_age_ms", 999999)) < 2000.0 and bool(
        diag.get("observation_complete")
    )
    if not result["telemetry_ready"]:
        result["errors"].append("telemetry stale or observation incomplete via collector diag")

    if diag.get("management_connected"):
        result["management_ready"] = True
    elif tcp_open("127.0.0.1", cfg.simulation_management_server_port):
        result["management_ready"] = True
    else:
        result["errors"].append("management TCP {0} closed".format(cfg.simulation_management_server_port))

    info = mgmt_static_info(cfg.simulation_management_server_port)
    if info and info.get("CarName") and info.get("TrackName"):
        result["static_info_ready"] = True
    else:
        result["errors"].append("get_static_info failed on port {0}".format(cfg.simulation_management_server_port))

    if diag.get("vjoy_ready"):
        result["controls_ready"] = True
    else:
        result["errors"].append("vJoy device {0} not ready (collector)".format(cfg.vjoy_device_id))

    if diag.get("transition_server_ok") or diag.get("transition_server_connected"):
        result["transition_server_ready"] = True
    else:
        try:
            from common.transition_server import TransitionClient

            TransitionClient().stats()
            result["transition_server_ready"] = True
        except OSError as exc:
            result["errors"].append("transition server: {0}".format(exc))

    tc = int(diag.get("transition_count", 0))
    result["first_transition_ready"] = tc > 0
    if require_transition and tc <= 0:
        result["errors"].append("no transition for worker {0} (transition_count=0)".format(worker_id))

    if val_errors:
        result["errors"].extend(val_errors)

    try:
        from common.transition_server import TransitionClient

        stats = TransitionClient().stats()
        if stats.get("learner_connected"):
            result["learner_connected"] = True
    except OSError:
        pass

    result["ok"] = (
        result["diagnostic_ready"]
        and ok_val
        and result["management_ready"]
        and result["static_info_ready"]
        and result["controls_ready"]
        and result["transition_server_ready"]
        and (not require_transition or result["first_transition_ready"])
    )
    result["transition_count"] = tc
    return result


def log_worker_handshake(worker_id: int, result: dict) -> None:
    if result.get("diagnostic_ready"):
        print("collector worker {0} diagnostic ready port={1}".format(worker_id, diag_port(worker_id)))
    if result.get("telemetry_ready"):
        print("collector worker {0} telemetry ready".format(worker_id))
    if result.get("management_ready"):
        print("collector worker {0} management ready".format(worker_id))
    if result.get("transition_server_ready"):
        print("collector worker {0} transition server ready".format(worker_id))
    if result.get("controls_ready"):
        print("worker {0} controls ready".format(worker_id))
    if result.get("first_transition_ready"):
        print("collector worker {0} first transition ready".format(worker_id))
    if result.get("learner_connected"):
        print("learner connected")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-id", type=int, required=True)
    parser.add_argument("--require-transition", action="store_true")
    parser.add_argument("--session-id", type=str, default="")
    parser.add_argument("--collector-pid", type=int, default=0)
    args = parser.parse_args()
    session_id = args.session_id or None
    collector_pid = args.collector_pid if args.collector_pid > 0 else None
    result = run_worker_handshake(
        args.worker_id,
        require_transition=args.require_transition,
        session_id=session_id,
        collector_pid=collector_pid,
    )
    log_worker_handshake(args.worker_id, result)
    if not result.get("ok"):
        for err in result.get("errors", []):
            print("ERROR: {0}".format(err))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
