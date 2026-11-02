"""Offline tests for collector diagnostic protocol."""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def test_ten_status_requests():
    from ac_workers import collector_diag as cd
    from ac_workers.collector_diag import CollectorDiagClient, CollectorDiagServer

    wid = 99
    with mock.patch.object(cd, "DIAG_BASE_PORT", 24900):
        lock = threading.RLock()
        state = {"worker_id": wid, "transition_count": 1, "telemetry_age_ms": 50.0, "vjoy_ready": True}
        srv = CollectorDiagServer(wid, lambda: dict(state), lambda: {"ok": True}, lambda: {"ok": True}, lock)
        srv.start()
        assert srv.wait_until_listening(timeout_sec=3.0), srv.thread_error()
        client = CollectorDiagClient(wid, timeout=2.0)
        for _ in range(10):
            resp = client.request("status")
            assert resp.get("ok") is True
        srv.stop()


def test_status_readonly_status_sequence():
    from ac_workers import collector_diag as cd
    from ac_workers.collector_diag import CollectorDiagClient, CollectorDiagServer

    wid = 98
    with mock.patch.object(cd, "DIAG_BASE_PORT", 24900):
        lock = threading.RLock()
        state = {"worker_id": wid, "telemetry_age_ms": 100.0, "last_observation": [1.0], "telemetry_snapshot": {"a": 1}}
        srv = CollectorDiagServer(wid, lambda: dict(state), lambda: {"ok": True}, lambda: {"ok": True}, lock)
        srv.start()
        assert srv.wait_until_listening(timeout_sec=3.0), srv.thread_error()
        c = CollectorDiagClient(wid, timeout=2.0)
        assert c.request("status")["ok"]
        ro = c.request("readonly_check")
        assert ro.get("telemetry_recent") is True
        assert c.request("status")["ok"]
        c.request("neutralize")
        assert c.request("status")["ok"]
        srv.stop()


def test_client_timeout_message():
    from ac_workers.collector_diag import CollectorDiagClient, DiagRequestError, diag_port

    wid = 99
    closed_port = diag_port(wid)
    try:
        CollectorDiagClient(wid, timeout=0.2).request("status")
        assert False, "expected timeout"
    except DiagRequestError as exc:
        assert "timeout on port {0}".format(closed_port) in exc.message


def test_server_returns_to_accept():
    from ac_workers import collector_diag as cd
    from ac_workers.collector_diag import CollectorDiagClient, CollectorDiagServer

    wid = 97
    with mock.patch.object(cd, "DIAG_BASE_PORT", 24900):
        lock = threading.RLock()
        srv = CollectorDiagServer(wid, lambda: {"worker_id": wid}, lambda: {"ok": True}, lambda: {"ok": True}, lock)
        srv.start()
        assert srv.wait_until_listening(timeout_sec=3.0), srv.thread_error()
        c = CollectorDiagClient(wid, timeout=2.0)
        c.request("status")
        c.request("status")
        srv.stop()


def test_snapshot_no_blocking_in_handler():
    from ac_workers.collector_diag import CollectorDiagServer

    lock = threading.RLock()
    calls = {"n": 0}

    def slow_snapshot():
        calls["n"] += 1
        return {"worker_id": 0, "n": calls["n"]}

    srv = CollectorDiagServer(0, slow_snapshot, lambda: {"ok": True}, lambda: {"ok": True}, lock)
    with mock.patch.object(srv, "_copy_snapshot", return_value={"worker_id": 0}):
        resp = srv._dispatch({"cmd": "status"})
    assert resp["ok"] is True
    assert calls["n"] == 0


def test_ping_lock_free_no_snapshot():
    from ac_workers import collector_diag as cd
    from ac_workers.collector_diag import CollectorDiagClient, CollectorDiagServer

    wid = 96
    with mock.patch.object(cd, "DIAG_BASE_PORT", 24900):
        lock = threading.RLock()
        snap_calls = {"n": 0}

        def counting_snapshot():
            snap_calls["n"] += 1
            return {"worker_id": wid, "collector_pid": 42, "boot_id": "bootx"}

        srv = CollectorDiagServer(
            wid,
            counting_snapshot,
            lambda: {"ok": True},
            lambda: {"ok": True},
            lock,
            on_ping=lambda: {
                "ok": True,
                "worker_id": wid,
                "collector_pid": 42,
                "boot_id": "bootx",
                "diag_port": 24900 + wid,
                "ts": time.time(),
            },
        )
        srv.start()
        assert srv.wait_until_listening(timeout_sec=3.0), srv.thread_error()
        resp = CollectorDiagClient(wid, timeout=2.0).request("ping")
        assert resp.get("ok") is True
        assert resp.get("boot_id") == "bootx"
        assert snap_calls["n"] == 0
        srv.stop()


def test_handler_exception_returns_json_not_close():
    from ac_workers import collector_diag as cd
    from ac_workers.collector_diag import CollectorDiagClient, CollectorDiagServer

    wid = 92
    with mock.patch.object(cd, "DIAG_BASE_PORT", 24900):
        lock = threading.RLock()

        def boom_snapshot():
            raise RuntimeError("status exploded")

        srv = CollectorDiagServer(
            wid,
            boom_snapshot,
            lambda: {"ok": True},
            lambda: {"ok": True},
            lock,
            get_identity=lambda: {"worker_id": wid, "collector_pid": 7, "boot_id": "deadbeef"},
            on_ping=lambda: {
                "ok": True,
                "worker_id": wid,
                "collector_pid": 7,
                "boot_id": "deadbeef",
                "diag_port": 24900 + wid,
                "ts": time.time(),
            },
        )
        srv.start()
        assert srv.wait_until_listening(timeout_sec=3.0), srv.thread_error()
        c = CollectorDiagClient(wid, timeout=2.0)
        ping = c.request("ping")
        assert ping.get("ok") is True
        status = c.request("status")
        assert status.get("ok") is False
        assert "status exploded" in str(status.get("error") or "")
        assert status.get("boot_id") == "deadbeef"
        assert status.get("collector_pid") == 7
        assert status.get("traceback")
        srv.stop()


def test_probe_collector_diag_endpoint_uses_ping():
    from ac_workers.worker_handshake import probe_collector_diag_endpoint

    with mock.patch(
        "ac_workers.worker_handshake.CollectorDiagClient"
    ) as client_cls, mock.patch("ac_workers.worker_handshake.tcp_open", return_value=True):
        client_cls.return_value.request.return_value = {
            "ok": True,
            "cmd": "ping",
            "worker_id": 2,
            "collector_pid": 99,
            "boot_id": "boot1",
        }
        ok, reason, snap = probe_collector_diag_endpoint(2, file_status={"collector_pid": 99})
        assert ok is True
        assert reason == "ok"
        assert snap["boot_id"] == "boot1"
        client_cls.return_value.request.assert_called_with("ping")


def test_stale_manifest_repair():
    from ac_workers.transition_server_health import check_transition_server

    with mock.patch("ac_workers.transition_server_process.is_port_listening", return_value=True), mock.patch(
        "ac_workers.transition_server_process.ping_transition_server", return_value=True
    ), mock.patch(
        "ac_workers.transition_server_process.detect_transition_server_pid",
        return_value={"pid": 7777, "recognized": True, "commandline": "python -m ac_workers.transition_server_main"},
    ), mock.patch(
        "ac_workers.transition_server_process.get_process_command_line",
        return_value="python -m ac_workers.transition_server_main",
    ), mock.patch(
        "AssettoCorsaEnv.ac_worker_runtime.read_manifest",
        return_value={"transitionServerPid": 2568, "workers": []},
    ), mock.patch("AssettoCorsaEnv.ac_worker_runtime.write_manifest") as write_mock:
        result = check_transition_server(repair_manifest=True)
    assert result["ok"] is True
    assert result["stale"] is True
    assert result["pid"] == 7777
    write_mock.assert_called_once()


def test_run_sac_accepts_stale_manifest():
    from ac_workers.transition_server_health import check_transition_server

    with mock.patch("ac_workers.transition_server_process.is_port_listening", return_value=True), mock.patch(
        "ac_workers.transition_server_process.ping_transition_server", return_value=True
    ), mock.patch(
        "ac_workers.transition_server_process.detect_transition_server_pid",
        return_value={"pid": 8888, "recognized": True, "commandline": "-m ac_workers.transition_server_main"},
    ), mock.patch(
        "ac_workers.transition_server_process.get_process_command_line",
        return_value="-m ac_workers.transition_server_main",
    ), mock.patch(
        "AssettoCorsaEnv.ac_worker_runtime.read_manifest",
        return_value={"transitionServerPid": 2568},
    ), mock.patch("AssettoCorsaEnv.ac_worker_runtime.write_manifest"):
        r = check_transition_server(repair_manifest=True)
    assert r["ok"] is True
    assert r["manifestPid"] == 2568


def test_verify_refuses_no_health():
    from ac_workers.transition_server_health import check_transition_server

    with mock.patch("ac_workers.transition_server_process.is_port_listening", return_value=False):
        r = check_transition_server(repair_manifest=False)
    assert r["ok"] is False
    assert "no listener" in r["error"]


def test_listener_ready_only_after_listen():
    from ac_workers import collector_diag as cd
    from ac_workers.collector_diag import CollectorDiagClient, CollectorDiagServer

    wid = 95
    with mock.patch.object(cd, "DIAG_BASE_PORT", 24900):
        lock = threading.RLock()
        srv = CollectorDiagServer(wid, lambda: {"worker_id": wid}, lambda: {"ok": True}, lambda: {"ok": True}, lock)
        assert not srv.listener_ready()
        srv.start()
        assert srv.wait_until_listening(timeout_sec=3.0), srv.thread_error()
        assert srv.listener_ready()
        resp = CollectorDiagClient(wid, timeout=2.0).request("status")
        assert resp.get("worker_id") == wid
        srv.stop()


def test_bind_conflict_fails_fast():
    from ac_workers import collector_diag as cd
    from ac_workers.collector_diag import CollectorDiagClient, CollectorDiagServer

    wid = 94
    port = 24900 + wid
    with mock.patch.object(cd, "DIAG_BASE_PORT", 24900):
        lock = threading.RLock()
        snap = lambda: {"worker_id": wid, "phase": "running"}
        first = CollectorDiagServer(wid, snap, lambda: {"ok": True}, lambda: {"ok": True}, lock)
        first.start()
        assert first.wait_until_listening(timeout_sec=3.0), first.thread_error()

        second = CollectorDiagServer(
            wid, snap, lambda: {"ok": True}, lambda: {"ok": True}, threading.RLock()
        )
        second.start()
        t0 = time.monotonic()
        ok = second.wait_until_listening(timeout_sec=2.0)
        elapsed = time.monotonic() - t0
        assert not ok
        assert elapsed < 1.5
        err = second.thread_error()
        assert err is not None
        msg = str(err)
        assert "already running" in msg
        assert str(port) in msg or str(wid) in msg
        assert "diagnostic listener timeout" not in msg

        resp = CollectorDiagClient(wid, timeout=2.0).request("status")
        assert resp.get("worker_id") == wid
        assert resp.get("phase") == "running"
        assert first.thread_is_alive()
        assert not second.listener_ready()

        first.stop()


def test_configure_diag_listener_socket_platform():
    import socket as sock_mod

    from ac_workers.collector_diag import configure_diag_listener_socket

    sock = sock_mod.socket(sock_mod.AF_INET, sock_mod.SOCK_STREAM)
    try:
        mode = configure_diag_listener_socket(sock)
        if sys.platform == "win32":
            assert mode == "SO_EXCLUSIVEADDRUSE"
        else:
            assert mode == "exclusive(no SO_REUSEADDR)"
    finally:
        sock.close()


def test_diag_port_in_use_message_windows_style():
    from ac_workers.collector_diag import diag_port_in_use_message

    exc = OSError(10048, "Only one usage of each socket address is normally permitted")
    exc.winerror = 10048  # type: ignore[attr-defined]
    msg = diag_port_in_use_message(0, 24100, exc)
    assert "worker 0 already running" in msg
    assert "24100" in msg
    assert "10048" in msg


def test_second_bind_does_not_stop_first_listener():
    from ac_workers import collector_diag as cd
    from ac_workers.collector_diag import CollectorDiagClient, CollectorDiagServer

    wid = 90
    with mock.patch.object(cd, "DIAG_BASE_PORT", 24900):
        first = CollectorDiagServer(
            wid,
            lambda: {"worker_id": wid, "phase": "gear_mapping_invalid"},
            lambda: {"ok": True},
            lambda: {"ok": True},
            threading.RLock(),
        )
        first.start()
        assert first.wait_until_listening(timeout_sec=3.0), first.thread_error()

        second = CollectorDiagServer(
            wid,
            lambda: {"worker_id": wid},
            lambda: {"ok": True},
            lambda: {"ok": True},
            threading.RLock(),
        )
        second.start()
        assert not second.wait_until_listening(timeout_sec=2.0)
        assert second.thread_error() is not None

        resp = CollectorDiagClient(wid, timeout=2.0).request("status")
        assert resp.get("phase") == "gear_mapping_invalid"
        assert first.thread_is_alive()
        first.stop()


def test_dead_thread_detected_without_full_timeout():
    from ac_workers.collector_diag import CollectorDiagServer

    lock = threading.RLock()
    srv = CollectorDiagServer(0, lambda: {"worker_id": 0}, lambda: {"ok": True}, lambda: {"ok": True}, lock)

    def _crash(_self):
        raise RuntimeError("diag crash before bind")

    with mock.patch.object(CollectorDiagServer, "_serve", _crash):
        srv.start()
        t0 = time.monotonic()
        ok = srv.wait_until_listening(timeout_sec=3.0)
        elapsed = time.monotonic() - t0
    assert not ok
    assert elapsed < 1.5
    assert srv.thread_error() is not None


def _powershell_tcp_probe(host: str, port: int, wait_ms: float = 500.0) -> bool:
    """Mirror Test-TcpPortOpen: BeginConnect-style connect with short timeout, no payload."""
    import socket as sock_mod

    sock = sock_mod.socket(sock_mod.AF_INET, sock_mod.SOCK_STREAM)
    sock.settimeout(wait_ms / 1000.0)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


def test_powershell_tcp_probe_after_listen():
    from ac_workers import collector_diag as cd
    from ac_workers.collector_diag import CollectorDiagServer

    wid = 93
    with mock.patch.object(cd, "DIAG_BASE_PORT", 24900):
        lock = threading.RLock()
        srv = CollectorDiagServer(wid, lambda: {"worker_id": wid}, lambda: {"ok": True}, lambda: {"ok": True}, lock)
        srv.start()
        assert srv.wait_until_listening(timeout_sec=3.0), srv.thread_error()
        assert _powershell_tcp_probe("127.0.0.1", 24900 + wid)
        srv.stop()


def test_status_during_slow_reset_and_gear_mapping_invalid():
    from ac_workers import collector_diag as cd
    from ac_workers.collector_diag import CollectorDiagClient, CollectorDiagServer
    from ac_workers.worker_collector import CollectorRuntime

    wid = 3
    with mock.patch.object(cd, "DIAG_BASE_PORT", 24900):
        runtime = CollectorRuntime(wid, 1, 2347)
        runtime.set_phase("starting")
        runtime.write_minimal_status()
        reset_done = threading.Event()
        stop_idle = threading.Event()

        srv = CollectorDiagServer(
            wid,
            runtime.get_diag_snapshot,
            lambda: {"ok": False},
            lambda: {"ok": True},
            runtime.lock,
        )
        srv.start()
        assert srv.wait_until_listening(timeout_sec=3.0), srv.thread_error()

        def slow_reset():
            runtime.set_phase("resetting")
            runtime.write_minimal_status()
            time.sleep(0.35)
            runtime.set_phase("gear_mapping_invalid", "controls.ini GEARUP not on vJoy")
            runtime.write_minimal_status()
            reset_done.set()

        def idle_writes():
            while not stop_idle.is_set():
                runtime.touch_heartbeat()
                runtime.write_minimal_status()
                time.sleep(0.01)

        with mock.patch("ac_workers.worker_collector._STATUS_IO_LOCK", threading.RLock()):
            threading.Thread(target=slow_reset, daemon=True).start()
            idle = threading.Thread(target=idle_writes, daemon=True)
            idle.start()
            time.sleep(0.05)
            port = 24900 + wid
            assert _powershell_tcp_probe("127.0.0.1", port)
            resp = CollectorDiagClient(wid, timeout=2.0).request("status")
            assert resp.get("ok") is True
            assert resp.get("phase") in ("resetting", "gear_mapping_invalid", "starting")
            assert reset_done.wait(2.0)
            assert _powershell_tcp_probe("127.0.0.1", port)
            resp2 = CollectorDiagClient(wid, timeout=2.0).request("status")
            assert resp2.get("phase") == "gear_mapping_invalid"
            assert srv.thread_is_alive()
            stop_idle.set()
            idle.join(timeout=2.0)
        srv.stop()


def test_diag_snapshot_never_refreshes():
    from ac_workers.worker_collector import CollectorRuntime

    runtime = CollectorRuntime(0, 1, 2347)
    runtime.set_phase("gear_mapping_invalid", "test")
    runtime.write_minimal_status()
    calls = {"refresh": 0}
    original = runtime.refresh_snapshot

    def tracked_refresh():
        calls["refresh"] += 1
        return original()

    runtime.refresh_snapshot = tracked_refresh  # type: ignore[method-assign]
    snap = runtime.get_diag_snapshot()
    assert snap.get("phase") == "gear_mapping_invalid"
    assert calls["refresh"] == 0
    assert "last_observation" not in snap


def test_diag_handoff_survives_runtime_lock():
    import threading
    import time

    from ac_workers.worker_collector import CollectorRuntime

    runtime = CollectorRuntime(0, 1, 2347)
    runtime.write_minimal_status()
    held = threading.Event()
    release = threading.Event()

    def hold_lock():
        with runtime.lock:
            held.set()
            release.wait(2.0)

    t = threading.Thread(target=hold_lock, daemon=True)
    t.start()
    assert held.wait(1.0)
    snap = runtime.get_diag_snapshot()
    assert snap.get("collector_pid") == runtime.collector_pid
    assert snap.get("boot_id") == runtime.boot_id
    release.set()
    t.join(timeout=2.0)


def test_status_response_ok_not_overridden_by_snap():
    from ac_workers import collector_diag as cd
    from ac_workers.collector_diag import CollectorDiagClient, CollectorDiagServer

    lock = threading.RLock()
    wid = 97
    with mock.patch.object(cd, "DIAG_BASE_PORT", 24970):
        srv = CollectorDiagServer(
            wid,
            lambda: {"worker_id": wid, "collector_pid": 1, "boot_id": "abc", "ok": False},
            lambda: {"ok": True},
            lambda: {"ok": True},
            lock,
        )
        srv.start()
        assert srv.wait_until_listening(2.0)
        try:
            resp = CollectorDiagClient(wid, timeout=2.0).request("status")
            assert resp.get("ok") is True
            assert resp.get("worker_id") == wid
        finally:
            srv.stop()


def test_probe_collector_diag_endpoint_ready():
    from ac_workers.worker_handshake import probe_collector_diag_endpoint

    with mock.patch(
        "ac_workers.worker_handshake.CollectorDiagClient"
    ) as client_cls, mock.patch("ac_workers.worker_handshake.tcp_open", return_value=True):
        client_cls.return_value.request.return_value = {
            "ok": True,
            "cmd": "ping",
            "worker_id": 2,
            "collector_pid": 99,
            "boot_id": "boot1",
        }
        ok, reason, snap = probe_collector_diag_endpoint(2, file_status={"collector_pid": 99})
        assert ok is True
        assert reason == "ok"
        assert snap["boot_id"] == "boot1"


def test_idle_after_failure_does_not_stop_diag():
    src = (Path(__file__).resolve().parents[1] / "ac_workers" / "worker_collector.py").read_text(encoding="utf-8")
    idle_block = src.split("def _idle_after_failure", 1)[1].split("\n    runtime.set_phase", 1)[0]
    assert "diag.stop" not in idle_block


def main() -> int:
    tests = [
        test_ten_status_requests,
        test_status_readonly_status_sequence,
        test_client_timeout_message,
        test_server_returns_to_accept,
        test_snapshot_no_blocking_in_handler,
        test_ping_lock_free_no_snapshot,
        test_handler_exception_returns_json_not_close,
        test_probe_collector_diag_endpoint_uses_ping,
        test_listener_ready_only_after_listen,
        test_bind_conflict_fails_fast,
        test_configure_diag_listener_socket_platform,
        test_diag_port_in_use_message_windows_style,
        test_second_bind_does_not_stop_first_listener,
        test_dead_thread_detected_without_full_timeout,
        test_powershell_tcp_probe_after_listen,
        test_status_during_slow_reset_and_gear_mapping_invalid,
        test_diag_snapshot_never_refreshes,
        test_diag_handoff_survives_runtime_lock,
        test_status_response_ok_not_overridden_by_snap,
        test_probe_collector_diag_endpoint_ready,
        test_idle_after_failure_does_not_stop_diag,
        test_stale_manifest_repair,
        test_run_sac_accepts_stale_manifest,
        test_verify_refuses_no_health,
    ]
    for fn in tests:
        fn()
        print("OK", fn.__name__)
    print("Test-CollectorDiag: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
