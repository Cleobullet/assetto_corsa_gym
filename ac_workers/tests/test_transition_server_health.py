"""Offline tests for transition_server_health (check-only, no start)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def test_check_healthy_listener():
    from ac_workers.transition_server_health import check_transition_server

    with mock.patch("ac_workers.transition_server_process.is_port_listening", return_value=True), mock.patch(
        "ac_workers.transition_server_process.ping_transition_server", return_value=True
    ), mock.patch(
        "ac_workers.transition_server_process.detect_transition_server_pid",
        return_value={"pid": 5000, "recognized": True, "commandline": "python -m ac_workers.transition_server_main"},
    ), mock.patch(
        "ac_workers.transition_server_process.get_process_command_line",
        return_value="python -m ac_workers.transition_server_main",
    ), mock.patch(
        "AssettoCorsaEnv.ac_worker_runtime.read_manifest", return_value={"transitionServerPid": 5000}
    ):
        r = check_transition_server(repair_manifest=False)
    assert r["ok"] is True, r


def test_check_no_listener():
    from ac_workers.transition_server_health import check_transition_server

    with mock.patch("ac_workers.transition_server_process.is_port_listening", return_value=False), mock.patch(
        "AssettoCorsaEnv.ac_worker_runtime.read_manifest", return_value={}
    ):
        r = check_transition_server()
    assert r["ok"] is False


def test_main_always_json():
    import sys
    from ac_workers import transition_server_health as tsh

    argv = sys.argv[:]
    try:
        sys.argv = ["transition_server_health", "--check", "--json"]
        with mock.patch.object(tsh, "check_transition_server", return_value=tsh._result(ok=True, pid=1)):
            code = tsh.main()
        assert code == 0
    finally:
        sys.argv = argv


def main() -> int:
    tests = [test_check_healthy_listener, test_check_no_listener, test_main_always_json]
    for fn in tests:
        fn()
        print("OK", fn.__name__)
    print("Test-TransitionServerHealth: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
