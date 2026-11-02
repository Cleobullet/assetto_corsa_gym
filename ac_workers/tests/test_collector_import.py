"""Offline tests: collector imports, vJoy API, Run-SAC status gates."""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))


def test_import_worker_collector():
    importlib.import_module("ac_workers.worker_collector")


def test_import_worker_handshake():
    importlib.import_module("ac_workers.worker_handshake")


def test_import_collector_diag():
    importlib.import_module("ac_workers.collector_diag")


def test_import_collector_safety():
    importlib.import_module("ac_workers.collector_safety")


def test_vjoy_public_api():
    from ac_workers.worker_handshake import vjoy_accessible, _vjoy_accessible

    assert vjoy_accessible is _vjoy_accessible
    assert callable(vjoy_accessible)


def test_tcp_open_public_api():
    from ac_workers.worker_handshake import tcp_open, _tcp_open

    assert callable(tcp_open)
    assert tcp_open("127.0.0.1", 59990, timeout=0.1) is False
    assert _tcp_open("127.0.0.1", 59989, timeout=0.1) is False


def test_run_sac_refused_zero_transitions():
    from ac_workers.run_sac_status import validate_worker_for_training
    import time

    status_dir = REPO / "ac_workers" / "status"
    status_dir.mkdir(parents=True, exist_ok=True)
    path = status_dir / "worker_0.json"
    path.write_text(
        json.dumps(
            {
                "transition_count": 0,
                "heartbeat_age_ms": 100.0,
                "heartbeat_monotonic": time.monotonic(),
                "telemetry_age_ms": 100.0,
                "vjoy_ready": True,
                "transition_server_ok": True,
                "transition_server_connected": True,
                "management_connected": True,
                "observation_complete": True,
                "last_observation": [1.0],
                "telemetry_snapshot": {"speed": 0.0},
            }
        ),
        encoding="utf-8",
    )
    ok, errs = validate_worker_for_training(0)
    assert not ok
    assert any("transition_count" in e for e in errs)


def test_run_sac_accepted_with_transitions():
    from ac_workers.run_sac_status import validate_worker_for_training
    import time

    path = REPO / "ac_workers" / "status" / "worker_0.json"
    path.write_text(
        json.dumps(
            {
                "transition_count": 3,
                "heartbeat_age_ms": 100.0,
                "heartbeat_monotonic": time.monotonic(),
                "telemetry_age_ms": 100.0,
                "vjoy_ready": True,
                "transition_server_ok": True,
                "transition_server_connected": True,
                "management_connected": True,
                "observation_complete": True,
                "last_observation": [1.0],
                "telemetry_snapshot": {"speed": 0.0},
            }
        ),
        encoding="utf-8",
    )
    ok, errs = validate_worker_for_training(0)
    assert ok, errs


def test_worker1_ports_in_python_ini_writer():
    txt = (REPO / "scripts" / "Ac-AiSessionCommon.ps1").read_text(encoding="utf-8")
    assert "ACGYM_MGMT_PORT" in txt
    assert "WorkerId" in txt


def test_launcher_timeout_message_format():
    txt = (REPO / "scripts" / "Start-Multi-AI-Session.ps1").read_text(encoding="utf-8")
    assert "management" in txt
    assert "staticInfo=" in txt
    assert "port/static/window" not in txt


def main() -> int:
    tests = [
        test_import_worker_collector,
        test_import_worker_handshake,
        test_import_collector_diag,
        test_import_collector_safety,
        test_vjoy_public_api,
        test_tcp_open_public_api,
        test_run_sac_refused_zero_transitions,
        test_run_sac_accepted_with_transitions,
        test_worker1_ports_in_python_ini_writer,
        test_launcher_timeout_message_format,
    ]
    for fn in tests:
        fn()
        print("OK", fn.__name__)
    print("Test-CollectorImport: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
