"""Offline tests for transition server detection/stop (no AC live)."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def test_commandline_markers():
    from ac_workers.transition_server_process import is_transition_server_commandline

    assert is_transition_server_commandline(
        r'python.exe -m ac_workers.transition_server_main'
    )
    assert is_transition_server_commandline(
        r'C:\Python\python.exe -m common.transition_server'
    )
    assert is_transition_server_commandline(
        r'python C:\proj\ac_workers\transition_server_main.py'
    )
    assert not is_transition_server_commandline(
        r'python.exe -m http.server 24050'
    )
    assert not is_transition_server_commandline(
        r'python.exe -m ac_workers.worker_collector --worker-id 0'
    )
    assert not is_transition_server_commandline(None)


def test_detect_priority_manifest():
    from ac_workers.transition_server_process import detect_transition_server_pid

    manifest = {"transitionServerPid": 999999}
    info = detect_transition_server_pid(manifest, port=59999)
    assert info["source"] is None or info["pid"] is None or not info.get("recognized")


def test_preflight_no_learner_ping():
    txt = (REPO / "scripts" / "tests" / "Test-Multi-AI-Preflight.ps1").read_text(encoding="utf-8")
    assert "learner_ping" not in txt
    assert "TransitionServer()" not in txt


def test_ping_timeout_no_traceback():
    from ac_workers.transition_server_process import ping_transition_server

    assert ping_transition_server(port=59998, timeout=0.2) is False


def test_stop_unknown_skipped():
    from ac_workers.transition_server_process import stop_transition_server

    report = stop_transition_server(manifest={}, port=59997)
    assert report["stopped"] is False


def test_is_transition_server_commandline_repo_not_required():
    from ac_workers.transition_server_process import is_transition_server_commandline

    cmd = ".venv-acgym\\Scripts\\python.exe -m ac_workers.transition_server_main"
    assert is_transition_server_commandline(cmd)
    assert "Leo projet" not in cmd


def test_stop_blocked_recognized():
    from unittest import mock

    from ac_workers import transition_server_process as tsp

    listening = [True, True, False]

    def port_listen(_port):
        if listening:
            return listening.pop(0)
        return False

    with mock.patch.object(tsp, "get_tcp_listener_pids", return_value=[4242]), mock.patch.object(
        tsp, "get_process_command_line", return_value="python.exe -m ac_workers.transition_server_main"
    ), mock.patch.object(tsp, "is_process_alive", return_value=True), mock.patch.object(
        tsp, "stop_process_pid", return_value=True
    ) as stop_mock, mock.patch.object(
        tsp, "is_port_listening", side_effect=port_listen
    ):
        report = tsp.stop_transition_server(manifest={}, port=24050, wait_sec=0.5)
    assert report["stopped"] is True
    assert report["detected_pid"] == 4242
    stop_mock.assert_called()


def test_stop_unknown_never_killed():
    from unittest import mock

    from ac_workers import transition_server_process as tsp

    with mock.patch.object(tsp, "get_tcp_listener_pids", return_value=[9999]), mock.patch.object(
        tsp, "get_process_command_line", return_value="python.exe -m http.server 24050"
    ), mock.patch.object(tsp, "is_port_listening", return_value=True), mock.patch.object(
        tsp, "stop_process_pid"
    ) as stop_mock:
        report = tsp.stop_transition_server(manifest={}, port=24050, wait_sec=0.15)
    assert report["skipped_unknown"] is True
    assert report["stopped"] is False
    stop_mock.assert_not_called()


def test_emergency_stop_idempotent():
    from ac_workers.emergency_stop import emergency_stop

    r1 = emergency_stop(close_game=False)
    r2 = emergency_stop(close_game=False)
    assert r1["transition_server"]["port_released"] is True
    assert r2["transition_server"]["port_released"] is True


def main() -> int:
    tests = [
        test_commandline_markers,
        test_detect_priority_manifest,
        test_preflight_no_learner_ping,
        test_ping_timeout_no_traceback,
        test_stop_unknown_skipped,
        test_is_transition_server_commandline_repo_not_required,
        test_stop_blocked_recognized,
        test_stop_unknown_never_killed,
        test_emergency_stop_idempotent,
    ]
    for fn in tests:
        fn()
        print("OK", fn.__name__)
    print("Test-TransitionServerProcess: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
