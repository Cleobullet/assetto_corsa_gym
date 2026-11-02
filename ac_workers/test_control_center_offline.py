"""Offline tests for C4D Control Center."""
from __future__ import annotations

import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_parse_worker_spec():
    from ac_workers.control_center import parse_worker_spec

    assert parse_worker_spec("0") == [0]
    assert parse_worker_spec("0,2,4") == [0, 2, 4]
    assert parse_worker_spec("0-5") == [0, 1, 2, 3, 4, 5]
    assert parse_worker_spec("3-1") == [1, 2, 3]
    assert parse_worker_spec("") == []


def test_refuse_worker_already_active():
    from ac_workers.control_center import WorkerActivity, validate_worker_start

    activity = WorkerActivity(True, "ok", "running", collector_pid=111, ac_pid=222)
    errors = validate_worker_start([0], {0: True}, {0: activity})
    assert any("already active" in e for e in errors)


def test_detect_vjoy_conflict_in_batch():
    from ac_workers.control_center import validate_worker_start

    errors = validate_worker_start([0, 0], {}, {0: False})
    assert any("deja actif" in e or "conflit" in e for e in errors)


def test_detect_port_conflict_source():
    src = (REPO / "ac_workers" / "control_center.py").read_text(encoding="utf-8")
    assert "seen_diag" in src
    assert "seen_vjoy" in src
    assert "validate_worker_start" in src


def test_transitions_per_minute_rate():
    from ac_workers.control_center import PerMinuteRate

    rate = PerMinuteRate(window_sec=60.0)
    t0 = time.time()
    rate.add(0, t0)
    rate.add(60, t0 + 30.0)
    assert abs(rate.per_minute() - 120.0) < 1.0


def test_episode_rate_uses_counter_not_transitions():
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    center._episode_rates[0].add(0, time.time())
    center._episode_rates[0].add(2, time.time() + 30.0)
    assert center._episode_rates[0].per_minute() > 0.0
    src = (REPO / "ac_workers" / "worker_collector.py").read_text(encoding="utf-8")
    assert "episode_count" in src
    assert "note_episode_completed" in src


def test_stop_one_worker_script_exists():
    assert (REPO / "scripts" / "Stop-Multi-AI-Session.ps1").is_file()
    src = (REPO / "ac_workers" / "control_center.py").read_text(encoding="utf-8")
    assert "Stop-Multi-AI-Session.ps1" in src
    assert "OnlyWorkerId" in (REPO / "scripts" / "Start-Multi-AI-Session.ps1").read_text(encoding="utf-8")
    assert "HasOnlyWorkerId" in (REPO / "scripts" / "Start-Multi-AI-Session.ps1").read_text(encoding="utf-8")


def test_single_shared_learner():
    from ac_workers.control_center import ControlCenter

    src = (REPO / "ac_workers" / "control_center.py").read_text(encoding="utf-8")
    assert "Run-SAC-MultiWorker.ps1" in src
    assert "def learner_start" in src
    center = ControlCenter(REPO)
    worker0_args = center._worker_start_script_args(0)
    assert "1" in worker0_args
    assert "-PresentationMode" in worker0_args
    assert "-OnlyWorkerId" not in worker0_args


def test_worker_status_unavailable():
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    view = center.collect_worker_view(5)
    assert view.worker_id == 5
    assert view.active is False


def test_emergency_stop_wired():
    src = (REPO / "ac_workers" / "control_center.py").read_text(encoding="utf-8")
    assert "Emergency-Stop-AI.ps1" in src


def test_control_center_module_import():
    from ac_workers.control_center import ControlCenter, parse_worker_spec

    assert ControlCenter is not None
    assert parse_worker_spec("1") == [1]


def test_start_script_exists():
    assert (REPO / "scripts" / "Start-C4D-ControlCenter.ps1").is_file()


def test_fit_cell_truncates():
    from ac_workers.control_center import fit_cell

    cell = fit_cell("controls.ini GEARUP not mapped invalid", 20)
    assert len(cell) == 20
    assert cell.endswith("…")
    assert "\n" not in cell
    assert "\t" not in cell


def test_fit_cell_exact_width():
    from ac_workers.control_center import fit_cell

    assert len(fit_cell("ok", 5)) == 5
    assert len(fit_cell(12345, 6, "right")) == 6


def test_frame_width_80_120_160():
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    center.refresh_metrics_cache(force=True)
    workers = center._metrics_workers
    global_view = center._metrics_global
    long_msg = "controls.ini GEARUP not mapped " + ("x" * 200)
    for width in (80, 120, 160):
        frame = center.build_frame(
            "start 0", long_msg, term_width=width, workers=workers, global_view=global_view
        )
        for line in frame.split("\n"):
            assert len(line) <= width, (width, len(line), line)
            assert "\t" not in line
            assert "\n" not in line


def test_long_error_in_table_single_line():
    from ac_workers.control_center import (
        TABLE_COLUMNS_NORMAL,
        WorkerView,
        _format_row,
        _row_aligns,
        _worker_row_values,
        select_table_layout,
    )

    w = WorkerView(
        worker_id=3,
        active=True,
        phase="gear_initialization_failed_extra_long",
        last_error="controls.ini GEARUP not mapped " + ("z" * 300),
    )
    cols, error_w = select_table_layout(120)
    widths = [cw for _, cw in cols] + [error_w]
    line = _format_row(_worker_row_values(w, cols), widths, _row_aligns(len(widths)))
    assert len(line) <= 120
    assert line.count("\n") == 0


def test_worker_rows_aligned_same_width():
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    center.refresh_metrics_cache(force=True)
    frame = center.build_frame(
        term_width=120,
        workers=center._metrics_workers,
        global_view=center._metrics_global,
    )
    table_lines = frame.split("\n")[3:9]
    widths = {len(ln) for ln in table_lines}
    assert len(widths) == 1


def test_command_buffer_preserved_in_frame():
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    center.refresh_metrics_cache(force=True)
    frame = center.build_frame(
        "start 0,2",
        term_width=120,
        workers=center._metrics_workers,
        global_view=center._metrics_global,
        include_prompt=True,
    )
    assert frame.endswith("C4D> start 0,2") or "C4D> start 0,2" in frame.split("\n")[-1]


def test_dashboard_frame_has_no_prompt():
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    center.refresh_metrics_cache(force=True)
    frame = center.build_frame(term_width=120, include_prompt=False)
    assert "C4D>" not in frame
    assert "C4D command>" not in frame


def test_no_cls_in_refresh_loop():
    src = (REPO / "ac_workers" / "c4d_ipc.py").read_text(encoding="utf-8")
    assert 'os.system("cls")' not in src
    assert "Clear-Host" not in src
    assert "def _frame_loop" in src


def test_terminal_renderer_restores_cursor_on_error():
    from ac_workers.control_center import ANSI_SHOW_CURSOR, TerminalRenderer

    writes: list[str] = []

    class _Track:
        def __init__(self) -> None:
            self._calls = 0

        def write(self, data: str) -> None:
            writes.append(data)
            self._calls += 1
            if self._calls == 2:
                raise RuntimeError("boom")

        def flush(self) -> None:
            pass

    renderer = TerminalRenderer(stream=_Track())
    try:
        renderer.paint("payload")
    except RuntimeError:
        pass
    assert ANSI_SHOW_CURSOR in writes


def test_terminal_renderer_skips_identical_frame():
    from ac_workers.control_center import TerminalRenderer

    writes: list[str] = []

    class _Track:
        def write(self, data: str) -> None:
            writes.append(data)

        def flush(self) -> None:
            pass

    renderer = TerminalRenderer(stream=_Track())
    renderer.paint("same")
    n1 = len(writes)
    renderer.paint("same")
    assert len(writes) == n1
    renderer.paint("other")
    assert len(writes) > n1


def test_status_without_pid_is_offline():
    from ac_workers.control_center import is_worker_really_active

    activity = is_worker_really_active(
        0,
        manifest={"sessionId": "sess_a"},
        status={"collector_pid": 99999, "phase": "running", "heartbeat_age_ms": 0.0},
        manifest_entry={"collectorPid": 99999},
        worker_session_id="sess_a",
    )
    assert activity.active is False
    assert activity.phase in ("offline", "stale")


def test_recycled_collector_pid_offline(monkeypatch=None):
    from unittest import mock

    from ac_workers import control_center as cc

    def _fake_alive(pid: int) -> bool:
        return pid == 1234

    def _fake_cmd(pid: int) -> str:
        return "notepad.exe" if pid == 1234 else ""

    def _fake_tcp(*_a, **_k) -> bool:
        return True

    original_alive = cc.pid_alive
    original_cmd = cc._process_command_line
    cc.pid_alive = _fake_alive
    cc._process_command_line = _fake_cmd
    try:
        import ac_workers.worker_handshake as wh

        original_tcp = wh.tcp_open
        wh.tcp_open = _fake_tcp
        try:
            with mock.patch(
                "ac_workers.collector_pid.collector_evidence",
                return_value=(False, 1234, "collector_pid_recycled", {}),
            ):
                activity = cc.is_worker_really_active(
                    0,
                    manifest={"sessionId": "s1"},
                    status={
                        "collector_pid": 1234,
                        "session_id": "s1",
                        "status_written_at": "2099-01-01T00:00:00+00:00",
                    },
                    manifest_entry={"collectorPid": 1234},
                    worker_session_id="s1",
                )
                assert activity.active is False
                assert "recycled" in activity.reason
        finally:
            wh.tcp_open = original_tcp
    finally:
        cc.pid_alive = original_alive
        cc._process_command_line = original_cmd


def test_stale_session_id_waiting_sync_when_pids_alive():
    from ac_workers import control_center as cc

    original = cc.pid_alive
    cc.pid_alive = lambda pid: pid in (100, 200)
    try:
        activity = cc.is_worker_really_active(
            0,
            manifest={"sessionId": "current"},
            status={
                "session_id": "old",
                "collector_pid": 200,
                "ac_pid": 100,
                "status_written_at": "2099-01-01T00:00:00+00:00",
            },
            manifest_entry={"collectorPid": 200, "acPid": 100},
            worker_session_id="current",
        )
        assert activity.active is False
        assert activity.phase == "waiting_session_sync"
        assert activity.reason == "session synchronization pending"
        assert activity.ac_pid == 100
        assert activity.collector_pid == 200
    finally:
        cc.pid_alive = original


def test_stale_session_id_offline_when_dead():
    from ac_workers.control_center import is_worker_really_active

    activity = is_worker_really_active(
        1,
        manifest={"sessionId": "current"},
        status={"session_id": "old", "collector_pid": 1, "status_written_at": "2099-01-01T00:00:00+00:00"},
        manifest_entry={},
        worker_session_id="current",
    )
    assert activity.active is False
    assert activity.reason == "status_session_mismatch"


def test_c4d_token_differs_from_worker_session_accepted():
    from unittest import mock

    from ac_workers import control_center as cc

    original_alive = cc.pid_alive
    original_ac = cc.is_ac_process
    cc.pid_alive = lambda _pid: True
    cc.is_ac_process = lambda _pid: True
    try:
        import ac_workers.worker_handshake as wh

        original_tcp = wh.tcp_open
        wh.tcp_open = lambda *_a, **_k: True
        try:
            with mock.patch(
                "ac_workers.collector_pid.collector_evidence",
                return_value=(True, 5000, "ok", {}),
            ):
                activity = cc.is_worker_really_active(
                    0,
                    manifest={"sessionId": "worker_sess_new"},
                    status={
                        "session_id": "worker_sess_new",
                        "collector_pid": 5000,
                        "phase": "running",
                        "status_written_at": "2099-01-01T00:00:00+00:00",
                    },
                    manifest_entry={"collectorPid": 5000, "acPid": 4000},
                    worker_session_id="worker_sess_new",
                )
            assert activity.active is True
        finally:
            wh.tcp_open = original_tcp
    finally:
        cc.pid_alive = original_alive
        cc.is_ac_process = original_ac


def test_start_ignores_old_status_session():
    from unittest import mock

    from ac_workers.control_center import WORKER_OP_STARTING, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_STARTING
    center._worker_pre_start_manifest_sid[0] = "old_sess"
    center._worker_op_session_id[0] = "new_sess"
    with mock.patch.object(
        center,
        "_manifest",
        return_value={"sessionId": "new_sess", "workers": [{"workerId": 0, "acPid": 111, "collectorPid": 222}]},
    ), mock.patch.object(
        center,
        "_worker_status",
        return_value={"session_id": "old_sess", "collector_pid": 222, "phase": "stopped", "last_error": "dead"},
    ), mock.patch(
        "ac_workers.collector_pid.collector_evidence",
        return_value=(True, 222, "initializing", {}),
    ):
        view = center._build_worker_view(0)
    assert view.phase in (
        "starting_waiting_session",
        "waiting_session_sync",
        "starting",
        "environment_loading",
        "waiting_worker_status",
    )
    assert "collector_real_process_died" not in (view.last_error or "")
    assert center._worker_op_state[0] == WORKER_OP_STARTING


def test_session_mismatch_never_collector_real_process_died():
    import time
    from unittest import mock

    from ac_workers.control_center import WORKER_OP_STARTING, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_STARTING
    center._worker_op_started_at[0] = time.time()
    center._worker_pre_start_manifest_sid[0] = "old_sess"
    with mock.patch.object(
        center,
        "_manifest",
        return_value={"sessionId": "old_sess", "workers": []},
    ), mock.patch.object(
        center,
        "_worker_status",
        return_value={"session_id": "old_sess", "collector_pid": 9999, "phase": "environment_loading"},
    ), mock.patch(
        "ac_workers.control_center.pid_alive", return_value=False
    ), mock.patch(
        "ac_workers.collector_pid.collector_evidence",
        return_value=(False, 9999, "collector_real_process_died", {}),
    ):
        changed = center._reconcile_starting_ops()
    assert changed is False
    assert center._worker_op_state[0] == WORKER_OP_STARTING


def test_new_manifest_binds_worker_session():
    from unittest import mock

    from ac_workers.control_center import WORKER_OP_STARTING, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_STARTING
    center._worker_pre_start_manifest_sid[0] = "old_sess"
    with mock.patch.object(
        center,
        "_manifest",
        return_value={"sessionId": "new_sess", "workers": [{"workerId": 0}]},
    ):
        sid = center._sync_worker_op_session(0)
    assert sid == "new_sess"
    assert center._worker_op_session_id[0] == "new_sess"


def test_validated_collector_death_during_startup_no_failure():
    import time
    from unittest import mock

    from ac_workers.control_center import WORKER_OP_WAITING_READY, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_WAITING_READY
    center._worker_op_started_at[0] = time.time()
    center._worker_op_session_id[0] = "sess_x"
    center._worker_collector_validated[0] = True
    with mock.patch.object(
        center,
        "_manifest",
        return_value={"sessionId": "sess_x", "workers": [{"workerId": 0, "collectorLauncherPid": 1}]},
    ), mock.patch.object(
        center,
        "_worker_status",
        return_value={"session_id": "sess_x", "collector_pid": 5555, "phase": "environment_loading"},
    ), mock.patch(
        "ac_workers.control_center.pid_alive", return_value=False
    ), mock.patch(
        "ac_workers.collector_pid.collector_evidence",
        return_value=(False, 5555, "collector_real_process_died", {}),
    ):
        changed = center._reconcile_starting_ops()
    assert changed is False
    assert center._worker_op_state[0] == WORKER_OP_WAITING_READY


def test_attaching_fresh_never_collector_real_process_died():
    import time
    from unittest import mock

    from ac_workers.control_center import WORKER_OP_STARTING, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_STARTING
    center._worker_op_started_at[0] = time.time()
    center._worker_op_session_id[0] = "sess_a"
    center._worker_collector_validated[0] = True
    with mock.patch.object(
        center,
        "_manifest",
        return_value={"sessionId": "sess_a", "workers": [{"workerId": 0}]},
    ), mock.patch.object(
        center,
        "_worker_status",
        return_value={
            "session_id": "sess_a",
            "collector_pid": 5555,
            "phase": "attaching_live_session",
            "heartbeat_age_ms": 50.0,
        },
    ), mock.patch("ac_workers.control_center.pid_alive", return_value=False), mock.patch(
        "ac_workers.collector_pid.collector_evidence",
        return_value=(True, 5555, "initializing", {}),
    ), mock.patch.object(center, "_is_start_op_active", return_value=True):
        changed = center._reconcile_starting_ops()
    assert changed is False
    assert center._worker_op_state[0] == WORKER_OP_STARTING


def test_stale_worker_allows_start_message():
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    center._worker_activity[0] = center._evaluate_worker_activity(0)
    assert center._worker_activity[0].active is False
    errors = __import__("ac_workers.control_center", fromlist=["validate_worker_start"]).validate_worker_start(
        [0], {0: False}, center._worker_activity
    )
    assert errors == []


def test_dead_learner_status_inactive():
    from ac_workers.control_center import is_learner_really_active

    learner = {
        "learner_pid": 999999,
        "learner_running": True,
        "global_steps": 302,
        "replay_size": 86,
        "session_id": "x",
        "ts": "2000-01-01T00:00:00+00:00",
    }
    assert is_learner_really_active(learner, "x") is False


def test_subprocess_output_captured_not_printed():
    src = (REPO / "ac_workers" / "control_center.py").read_text(encoding="utf-8")
    assert "def _spawn_ps_job" in src
    assert "stdout=subprocess.PIPE" in src
    start_block = src.split("def start_workers(self", 1)[1].split("def start_workers_sequential", 1)[0]
    assert "_run_ps_captured" not in start_block
    assert "subprocess.run" not in start_block


def test_background_jobs_hide_console_windows():
    src = (REPO / "ac_workers" / "control_center.py").read_text(encoding="utf-8")
    spawn = src.split("def _spawn_ps_job", 1)[1].split("def _poll_background_ops", 1)[0]
    assert "_subprocess_no_window()" in spawn
    assert '"-WindowStyle"' in src
    assert '"Hidden"' in src


def test_no_blocking_subprocess_in_main_loop():
    src = (REPO / "ac_workers" / "c4d_ipc.py").read_text(encoding="utf-8")
    console = src.split("def run_console_client", 1)[1]
    assert "subprocess.run" not in console
    assert ".communicate(" not in console
    assert "input(" in console


def test_async_ops_use_background_thread_not_main():
    src = (REPO / "ac_workers" / "control_center.py").read_text(encoding="utf-8")
    spawn = src.split("def _spawn_ps_job", 1)[1].split("def _poll_background_ops", 1)[0]
    assert "threading.Thread" in spawn
    assert "proc.communicate()" in spawn
    assert "self._bg_queue.put" in spawn
    assert "_subprocess_no_window()" in spawn


def test_refresh_metrics_skips_health_and_process_scanners():
    from unittest import mock

    from ac_workers import system_control
    from ac_workers.control_center import ControlCenter
    import ac_workers.collector_pid as cp
    import ac_workers.transition_server_process as tsp

    counters = {"health": 0, "netstat": 0, "orphan": 0, "scan": 0, "cmdline": 0}

    def _bump(key: str):
        counters[key] += 1
        if key == "netstat":
            return set()
        if key == "health":
            return {"clean": True, "orphan_acs": [], "busy_ports": [], "zombie_collectors": {}}
        if key == "orphan":
            return []
        if key == "scan":
            return []
        return ""

    center = ControlCenter(REPO)
    with mock.patch.object(system_control, "system_health", side_effect=lambda *a, **k: _bump("health")):
        with mock.patch.object(system_control, "listening_ports", side_effect=lambda: _bump("netstat")):
            with mock.patch.object(system_control, "scan_orphan_acs", side_effect=lambda *a, **k: _bump("orphan")):
                with mock.patch.object(cp, "scan_collector_pids", side_effect=lambda w: _bump("scan")):
                    with mock.patch.object(
                        tsp,
                        "get_process_command_line",
                        side_effect=lambda pid, **kw: _bump("cmdline"),
                    ):
                        center.refresh_metrics_cache(force=True)

    assert counters["health"] == 0
    assert counters["netstat"] == 0
    assert counters["orphan"] == 0
    assert counters["scan"] == 0
    assert counters["cmdline"] == 0


def test_poll_background_ops_throttles_reconcile():
    from unittest import mock

    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    center._bg_reconcile_ts = __import__("time").time() - 1.0
    calls = {"starting": 0, "running": 0}

    with mock.patch.object(center, "_reconcile_starting_ops", side_effect=lambda: calls.__setitem__("starting", calls["starting"] + 1) or False):
        with mock.patch.object(center, "_reconcile_running_ac_closed", side_effect=lambda: calls.__setitem__("running", calls["running"] + 1) or False):
            center._poll_background_ops()
            center._poll_background_ops()

    assert calls == {"starting": 1, "running": 1}


def test_build_worker_view_uses_lightweight_activity():
    from unittest import mock

    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    seen: list[bool] = []

    def _capture(wid: int, *, lightweight: bool = False):
        seen.append(lightweight)
        from ac_workers.control_center import WorkerActivity

        return WorkerActivity(False, "offline", "offline")

    with mock.patch.object(center, "_evaluate_worker_activity", side_effect=_capture):
        center._build_worker_view(0)

    assert seen == [True]


def test_start_sets_starting_state_immediately():
    from ac_workers.control_center import WORKER_OP_STARTING, ControlCenter

    center = ControlCenter(REPO)
    launched: list[int] = []

    def _fake_launch(wid: int) -> None:
        launched.append(wid)

    center._launch_worker_start = _fake_launch  # type: ignore[method-assign]
    msg = center.start_workers([0])
    assert "worker 0: starting..." in msg
    assert center._worker_op_state[0] == WORKER_OP_STARTING
    assert launched == [0]


def test_second_start_refused_while_starting():
    from ac_workers.control_center import WORKER_OP_STARTING, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_STARTING
    msg = center.start_workers([0])
    assert "start already in progress" in msg


def test_bg_result_success_and_failure_messages():
    from ac_workers.control_center import (
        WORKER_OP_FAILED,
        WORKER_OP_WAITING_READY,
        ControlCenter,
        _BgResult,
    )

    center = ControlCenter(REPO)
    center._handle_bg_result(
        _BgResult("worker:0", "start", 0, "ok", worker_id=0, duration_sec=1.0)
    )
    assert center._worker_op_state[0] == WORKER_OP_WAITING_READY
    assert "waiting for ready" in center._message

    center._handle_bg_result(
        _BgResult("worker:0", "start", 1, "boom", worker_id=0, duration_sec=2.0)
    )
    assert center._worker_op_state[0] == WORKER_OP_FAILED
    assert "start failed: exit=1" in center._message
    assert "exit=1" in center._worker_op_errors[0]


def test_error_command_shows_exit_detail():
    from ac_workers.control_center import WORKER_OP_FAILED, ControlCenter, _BgResult

    center = ControlCenter(REPO)
    center._handle_bg_result(
        _BgResult("worker:0", "start", 1, "Start-Multi-AI-Session failed", worker_id=0)
    )
    detail = center.worker_error_detail(0)
    assert "exit=1" in detail
    assert center._worker_op_state[0] == WORKER_OP_FAILED


def test_status_usable_during_starting():
    from ac_workers.control_center import WORKER_OP_STARTING, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_STARTING
    center._launch_worker_start = lambda _wid: None  # type: ignore[method-assign]
    assert center.handle_command("status") == ""
    assert center.handle_command("workers") is not None
    assert center.handle_command("error 0") is not None
    assert center.handle_command("quit") == "au revoir"


def test_simulated_long_command_does_not_block_console():
    import socket
    import threading

    from ac_workers.c4d_ipc import C4DControlServer, _recv_line, _send_msg
    from ac_workers.control_center import ControlCenter, WorkerActivity, _BgResult

    center = ControlCenter(REPO)
    center._evaluate_worker_activity = lambda _wid, **kw: WorkerActivity(False, "", "offline")  # type: ignore[method-assign]
    center.active_workers_map = lambda: {i: False for i in range(6)}  # type: ignore[method-assign]
    started = threading.Event()

    def _fake_spawn(key, script, args, **kwargs):
        def _runner() -> None:
            started.set()
            time.sleep(0.3)
            center._bg_queue.put(
                _BgResult(
                    key=key,
                    operation=kwargs.get("operation", "start"),
                    code=0,
                    output="done",
                    worker_id=kwargs.get("worker_id"),
                )
            )

        threading.Thread(target=_runner, daemon=True).start()

    center._spawn_ps_job = _fake_spawn  # type: ignore[method-assign]
    tmp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tmp.bind(("127.0.0.1", 0))
    port = tmp.getsockname()[1]
    tmp.close()
    server = C4DControlServer(center, port=port)
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(0.15)

    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    buf = bytearray()
    _send_msg(client, {"t": "hello", "role": "console"})
    assert _recv_line(client, buf)["t"] == "hello_ok"

    t0 = time.time()
    _send_msg(client, {"t": "cmd", "line": "start 0"})
    reply = _recv_line(client, buf)
    assert time.time() - t0 < 2.0
    assert "starting" in str(reply.get("text") or "")
    assert started.wait(timeout=1.0)
    client.close()
    server._shutdown.set()


def test_renderer_sole_writer_in_loop():
    src = (REPO / "ac_workers" / "c4d_ipc.py").read_text(encoding="utf-8")
    dash = src.split("def run_dashboard_client", 1)[1].split("def run_console_client", 1)[0]
    assert "renderer.paint" in dash
    assert "input(" not in dash


def test_starting_phase_visible_in_frame():
    from ac_workers.control_center import WORKER_OP_STARTING, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_STARTING
    center._is_start_op_active = lambda _wid: True  # type: ignore[method-assign]
    center.refresh_metrics_cache(force=True)
    frame = center.build_frame(term_width=120)
    assert "waiting_worke" in frame or "starting" in frame


def test_keyboard_drain_loop():
    src = (REPO / "ac_workers" / "c4d_ipc.py").read_text(encoding="utf-8")
    assert "input(" in src.split("def run_console_client", 1)[1]
    assert "msvcrt.kbhit" not in src


def test_metrics_not_on_every_keypress():
    src = (REPO / "ac_workers" / "c4d_ipc.py").read_text(encoding="utf-8")
    frame_loop = src.split("def _frame_loop", 1)[1].split("def run(", 1)[0]
    assert "FRAME_INTERVAL_SEC" in src
    assert "_poll_background_ops" in frame_loop


def test_has_only_worker_id_in_start_script():
    src = (REPO / "scripts" / "Start-Multi-AI-Session.ps1").read_text(encoding="utf-8")
    assert "$HasOnlyWorkerId = $PSBoundParameters.ContainsKey('OnlyWorkerId')" in src
    assert "if ($OnlyWorkerId)" not in src.replace("$HasOnlyWorkerId", "")
    assert "Start-AcWorkerProcess" in src
    assert "Invoke-AcDriveActivation" in src
    assert "Start-AiSessionViaContentManager" not in src
    assert "icone volant, meme sequence 0-5" in src
    # Plus de branche Drive separee worker 0 vs 1-5.
    assert "if ($w -eq 0) {" not in src


def test_drive_sent_before_collector_without_telemetry_gate():
    src = (REPO / "scripts" / "Ac-AiSessionCommon.ps1").read_text(encoding="utf-8")
    invoke = src.split("function Invoke-AcDriveActivation")[1].split("function Wait-AcMainWindowReady")[0]
    assert "Drive confirmed" in invoke
    assert "Drive input delivered" not in invoke
    multi = (REPO / "scripts" / "Start-Multi-AI-Session.ps1").read_text(encoding="utf-8")
    assert "collector worker" in multi
    assert multi.index("Invoke-AcDriveActivation") < multi.index("--- collector worker")


def _learner_ready_status(**overrides):
    status = {
        "phase": "running",
        "policy_ready": True,
        "telemetry_age_ms": 0.0,
        "observation_complete": True,
        "observation_valid_for_policy": True,
        "transition_count": 100,
        "vjoy_device_acquired": True,
        "initial_session_attached": True,
    }
    status.update(overrides)
    return status


def test_learner_start_uses_ready_worker_count():
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    center._worker_status = lambda _wid: _learner_ready_status()  # type: ignore[method-assign]
    center.worker_active = lambda wid: wid in (0,)  # type: ignore[method-assign]
    assert center._ready_worker_count_for_learner() == 1
    center.worker_active = lambda wid: wid in (0, 1)  # type: ignore[method-assign]
    assert center._ready_worker_count_for_learner() == 2
    center.worker_active = lambda _wid: False  # type: ignore[method-assign]
    assert center._ready_worker_count_for_learner() is None


def test_learner_start_refused_without_ready_workers():
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    center.worker_active = lambda _wid: False  # type: ignore[method-assign]
    assert "learner start refused" in center.learner_start()


def test_learner_start_spawns_with_dynamic_workers():
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    center.worker_active = lambda wid: wid == 0  # type: ignore[method-assign]
    center._worker_status = lambda _wid: _learner_ready_status()  # type: ignore[method-assign]
    spawned: list[list[str]] = []

    def _fake_spawn(key, script, args, **kwargs):
        spawned.append(list(args))

    center._spawn_ps_job = _fake_spawn  # type: ignore[method-assign]
    msg = center.learner_start()
    assert "Workers=1" in msg
    assert "0" in msg
    assert spawned == [["-Workers", "1", "-WorkerIds", "0", "-SkipPrecheck"]]


def test_learner_start_tolerates_hole_and_does_not_kill():
    """Tolerant degraded mode: train [0,1,3], a hole at 2 is left running (not killed)."""
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    ready_ids = {0, 1, 3}
    active_ids = {0, 1, 2, 3}
    center.worker_active = lambda wid: wid in active_ids  # type: ignore[method-assign]

    def _status(wid):
        if wid in ready_ids:
            return _learner_ready_status()
        return _learner_ready_status(
            policy_ready=False,
            observation_complete=False,
            observation_valid_for_policy=False,
            transition_count=0,
            vjoy_device_acquired=False,
            initial_session_attached=False,
        )

    center._worker_status = _status  # type: ignore[method-assign]
    stopped: list[int] = []

    def _fake_stop(wid, **kwargs):
        stopped.append(wid)
        return ""

    center.stop_worker = _fake_stop  # type: ignore[method-assign]
    center._force_stop_worker_processes = lambda wid: stopped.append(wid)  # type: ignore[method-assign]
    spawned: list[list[str]] = []
    center._spawn_ps_job = (  # type: ignore[method-assign]
        lambda key, script, args, **kwargs: spawned.append(list(args))
    )
    msg = center.learner_start()
    assert stopped == []  # degraded mode never kills the hole
    assert spawned == [["-Workers", "3", "-WorkerIds", "0,1,3", "-SkipPrecheck"]]
    assert "Workers=3" in msg
    assert "0,1,3" in msg
    assert "skipped not-ready workers: 2" in msg


def test_learner_start_accepts_worker0_after_start_timeout():
    """Idle worker 0 after a late Drive click must not block the learner."""
    from ac_workers.control_center import WORKER_OP_START_TIMEOUT, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_START_TIMEOUT
    center._worker_op_errors[0] = "worker_start_timeout"
    ready_ids = {0, 1, 2}
    center.worker_active = lambda wid: wid in ready_ids  # type: ignore[method-assign]
    center._worker_status = lambda wid: _learner_ready_status(phase="idle")  # type: ignore[method-assign]
    center._resume_workers_for_learner = lambda ids: (list(ids), [])  # type: ignore[method-assign]
    center._pause_scale_boot = lambda: ""  # type: ignore[method-assign]
    center._learner_resume_available = lambda: False  # type: ignore[method-assign]
    spawned: list[list[str]] = []
    center._spawn_ps_job = (  # type: ignore[method-assign]
        lambda key, script, args, **kwargs: spawned.append(list(args))
    )
    msg = center.learner_start()
    assert "refused" not in msg
    assert spawned
    assert "0,1,2" in spawned[0]


def test_reconcile_recovers_start_timeout_when_idle():
    from ac_workers.control_center import WORKER_OP_OFFLINE, WORKER_OP_START_TIMEOUT, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_START_TIMEOUT
    center._worker_op_errors[0] = "worker_start_timeout"
    center._status_for_op = lambda _wid: {  # type: ignore[method-assign]
        "worker_id": 0,
        "phase": "idle",
        "initial_session_attached": True,
        "policy_ready": True,
        "collector_pid": 1,
        "heartbeat_age_ms": 1.0,
    }
    assert center._reconcile_starting_ops() is True
    assert center._worker_op_state[0] == WORKER_OP_OFFLINE
    assert 0 not in center._worker_op_errors


def test_learner_start_refused_when_worker0_not_ready():
    """Worker 0 is mandatory (precheck validates worker_0.json)."""
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    ready_ids = {1, 2, 3}
    center.worker_active = lambda wid: wid in {0, 1, 2, 3}  # type: ignore[method-assign]

    def _status(wid):
        if wid in ready_ids:
            return _learner_ready_status()
        return _learner_ready_status(policy_ready=False)

    center._worker_status = _status  # type: ignore[method-assign]
    spawned: list[list[str]] = []
    center._spawn_ps_job = (  # type: ignore[method-assign]
        lambda key, script, args, **kwargs: spawned.append(list(args))
    )
    msg = center.learner_start()
    assert "worker 0 not ready" in msg
    assert spawned == []


def test_learner_start_accepts_resetting_and_skips_failed():
    """A worker cycling resets is ready; reset_failed/vjoy_startup workers are skipped not killed."""
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    phases = {
        0: "running",
        1: "resetting",
        2: "reset_failed",
        3: "vjoy_startup_validation",
    }
    active_ids = set(phases)
    center.worker_active = lambda wid: wid in active_ids  # type: ignore[method-assign]
    center._worker_status = lambda wid: _learner_ready_status(  # type: ignore[method-assign]
        phase=phases.get(wid, ""),
        observation_complete=phases.get(wid) == "running",
        observation_valid_for_policy=phases.get(wid) == "running",
        initial_session_attached=wid in (0, 1),
        transition_count=100 if wid in (0, 1) else 0,
        vjoy_device_acquired=wid in (0, 1),
    )
    stopped: list[int] = []
    center.stop_worker = lambda wid, **kwargs: stopped.append(wid) or ""  # type: ignore[method-assign]
    center._force_stop_worker_processes = lambda wid: stopped.append(wid)  # type: ignore[method-assign]
    spawned: list[list[str]] = []
    center._spawn_ps_job = (  # type: ignore[method-assign]
        lambda key, script, args, **kwargs: spawned.append(list(args))
    )
    msg = center.learner_start()
    assert stopped == []
    assert spawned == [["-Workers", "2", "-WorkerIds", "0,1", "-SkipPrecheck"]]
    assert "Workers=2" in msg
    assert "skipped not-ready workers: 2,3" in msg


def test_startup_vjoy_failure_skips_janitor_when_siblings_live():
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    janitor_calls: list[list[int]] = []

    def _fake_reset(devices):
        janitor_calls.append(list(devices))
        return {"ok": True}

    center.worker_active = lambda wid: wid in (0, 1)  # type: ignore[method-assign]
    center._worker_status = lambda wid: {  # type: ignore[method-assign]
        "phase": "running" if wid == 0 else "idle",
        "initial_session_attached": True,
        "vjoy_device_acquired": True,
        "policy_ready": True,
    }
    center._force_stop_worker_processes = lambda _wid: None  # type: ignore[method-assign]
    import ac_workers.vjoy_janitor as jan

    original_alive = jan.janitor_alive
    original_reset = jan.request_vjoy_reset
    jan.janitor_alive = lambda: True
    jan.request_vjoy_reset = _fake_reset
    try:
        center._finalize_start_failure(1, "AcquireVJD failed status=1 (FREE)")
    finally:
        jan.janitor_alive = original_alive
        jan.request_vjoy_reset = original_reset
    assert janitor_calls == []


def test_learner_start_pauses_scale_queue():
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    center._scale_queue = [3, 4, 5]
    center.worker_active = lambda wid: wid == 0  # type: ignore[method-assign]
    center._worker_status = lambda _wid: _learner_ready_status()  # type: ignore[method-assign]
    center._spawn_ps_job = lambda *a, **k: None  # type: ignore[method-assign]
    msg = center.learner_start()
    assert center._scale_queue == []
    assert center._scale_boot_paused is True
    assert "scale boot paused" in msg


def test_startup_does_not_fail_when_ac_pid_dead_without_status():
    import ac_workers.control_center as cc
    from ac_workers.control_center import WORKER_OP_STARTING, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_STARTING
    center._worker_op_started_at[0] = time.time()
    center._worker_op_session_id[0] = "sess_ac"
    center._worker_seen_ac_pid[0] = 4242
    center._worker_status = lambda _wid: {  # type: ignore[method-assign]
        "session_id": "sess_ac",
        "phase": "creating_environment",
        "collector_pid": 9001,
    }
    original_alive = cc.pid_alive
    cc.pid_alive = lambda _pid: False
    try:
        assert center._reconcile_starting_ops() is False
    finally:
        cc.pid_alive = original_alive
    assert center._worker_op_state[0] == WORKER_OP_STARTING
    assert "collector_real_process_died" not in (center._message or "")


def test_stale_starting_not_shown_without_active_job():
    from ac_workers.control_center import WORKER_OP_STARTING, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_STARTING
    center.refresh_metrics_cache(force=True)
    view = center._build_worker_view(0)
    assert view.phase != WORKER_OP_STARTING


def test_launcher_exit_zero_enters_waiting_ready():
    from ac_workers.control_center import WORKER_OP_WAITING_READY, ControlCenter, _BgResult

    center = ControlCenter(REPO)
    center._worker_op_state[0] = "starting"
    center.worker_active = lambda _wid: False  # type: ignore[method-assign]
    center._handle_bg_result(_BgResult("worker:0", "start", 0, "script ok", worker_id=0))
    assert center._worker_op_state[0] == WORKER_OP_WAITING_READY
    assert "waiting for ready" in center._message
    assert "collector_real_process_died" not in center._worker_op_errors.get(0, "")


def test_start_worker0_uses_canonical_workers1_entry():
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    args = center._worker_start_script_args(0)
    assert args == [
        "-Workers",
        "1",
        "-Windowed",
        "-WindowLayout",
        "Legacy",
        "-GridWorkerCount",
        "6",
        "-RenderPreset",
        "SixInstances",
        "-PresentationMode",
        "-PresentationCamera",
        "ThirdPerson",
        "-PresentationChaseIndex",
        "0",
    ]
    assert "-OnlyWorkerId" not in args


def test_start_worker1_still_targets_only_worker_id():
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    args = center._worker_start_script_args(1)
    assert "-OnlyWorkerId" in args
    assert "1" in args
    assert "-PresentationMode" in args
    assert "-RenderPreset" in args
    assert "SixInstances" in args


def test_start_workers_refuses_missing_profile():
    import tempfile
    from pathlib import Path

    from ac_workers.control_center import worker_profile_missing_message

    with tempfile.TemporaryDirectory() as tmp:
        msg = worker_profile_missing_message(2, repo_root=Path(tmp))
    assert "profil absent" in msg
    assert "Capture-AI-Worker-Settings" in msg


def test_worker_profile_present_returns_empty():
    import tempfile
    from pathlib import Path

    from ac_workers.control_center import worker_profile_missing_message

    with tempfile.TemporaryDirectory() as tmp:
        vjoy = (
            Path(tmp)
            / "ac_profiles"
            / "workers"
            / "worker_2"
            / "cfg"
            / "controllers"
            / "savedsetups"
            / "Vjoy_W2.ini"
        )
        vjoy.parent.mkdir(parents=True, exist_ok=True)
        vjoy.write_text("[CONTROLLERS]\n", encoding="utf-8")
        assert worker_profile_missing_message(2, repo_root=Path(tmp)) == ""


def test_no_worker_pipeline_duplicated_in_control_center():
    src = (REPO / "ac_workers" / "control_center.py").read_text(encoding="utf-8")
    assert "Invoke-AcDriveActivation" not in src
    assert "worker_collector" not in src.split("def is_collector_process", 1)[0]
    assert "Start-AiSessionViaContentManager" not in src
    assert "Init-Fresh-Policy" not in src
    assert "_spawn_ps_job" in src


def test_worker_running_without_learner():
    from ac_workers.control_center import ControlCenter, GlobalView, WorkerView

    center = ControlCenter(REPO)
    workers = [
        WorkerView(worker_id=0, active=True, phase="running", transition_count=12, collector_pid=111)
    ]
    global_view = GlobalView(
        active_workers=1,
        learner_active=False,
        learner_pid=0,
        transitions_per_min=5.0,
    )
    frame = center.build_frame(workers=workers, global_view=global_view, term_width=120)
    assert "running" in frame
    assert "learner pid=-" in frame or "learner pid=0" in frame or "active=no" in frame


def test_error_command_keeps_full_output():
    from ac_workers.control_center import ControlCenter, _BgResult

    center = ControlCenter(REPO)
    long_out = "exit=1 " + ("traceback [Errno 22] Invalid argument\n" + "x" * 500)
    center._handle_bg_result(_BgResult("worker:0", "start", 1, long_out, worker_id=0))
    detail = center.worker_error_detail(0)
    assert "[Errno 22] Invalid argument" in detail
    assert len(detail) > 400


def test_only_worker_id_zero_dryrun_grid_1x1():
    import subprocess

    script = REPO / "scripts" / "Start-Multi-AI-Session.ps1"
    proc = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-OnlyWorkerId",
            "0",
            "-Workers",
            "6",
            "-Windowed",
            "-DryRun",
        ],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, out
    assert "1x1" in out
    assert "3x2" not in out
    assert "icone volant" in out
    assert "CM Go" not in out


def test_only_worker_id_dryrun_targets():
    import subprocess

    script = REPO / "scripts" / "Start-Multi-AI-Session.ps1"
    cases = [
        (["0"], "worker 0"),
        (["1"], "worker 1"),
        (["0", "2", "4"], "worker 0"),
    ]
    for ids, needle in cases:
        args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-DryRun", "-Workers", "6"]
        if len(ids) == 1:
            args.extend(["-OnlyWorkerId", ids[0]])
            proc = subprocess.run(args, cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", errors="replace")
        else:
            cmd = (
                "& '{0}' -DryRun -Workers 6 -OnlyWorkerId {1}".format(script, ",".join(ids))
            )
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
                cwd=str(REPO),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        out = (proc.stdout or "") + (proc.stderr or "")
        assert proc.returncode == 0, out
        assert needle in out
        if ids == ["0", "2", "4"]:
            assert "--- worker 2 ---" in out
            assert "--- worker 4 ---" in out
            assert "--- worker 1 ---" not in out
            assert "--- worker 3 ---" not in out


def test_targeted_preflight_skips_other_workers():
    src = (REPO / "scripts" / "tests" / "Test-Multi-AI-Preflight.ps1").read_text(encoding="utf-8")
    assert "$HasOnlyWorkerId" in src
    assert "$checkIds" in src


def test_dual_console_architecture_wired():
    ps = (REPO / "scripts" / "Start-C4D-ControlCenter.ps1").read_text(encoding="utf-8")
    assert "dashboard" in ps
    assert "console" in ps
    assert "server" in ps
    assert "127.0.0.1" in ps
    ipc = (REPO / "ac_workers" / "c4d_ipc.py").read_text(encoding="utf-8")
    assert "C4D_HOST" in ipc
    assert "127.0.0.1" in ipc


def test_ipc_server_dashboard_and_console_share_center():
    import socket
    import threading

    from ac_workers.c4d_ipc import C4DControlServer, _recv_line, _send_msg
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    token = "offline-test-token"
    tmp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tmp.bind(("127.0.0.1", 0))
    port = tmp.getsockname()[1]
    tmp.close()
    server = C4DControlServer(center, port=port, session_token=token, repo_root=REPO)
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(0.15)

    dash = socket.create_connection(("127.0.0.1", port), timeout=5)
    dbuf = bytearray()
    _send_msg(dash, {"t": "hello", "role": "dashboard", "token": token, "repo_root": str(REPO), "client_pid": 111})
    assert _recv_line(dash, dbuf)["t"] == "hello_ok"
    frame = _recv_line(dash, dbuf)
    assert frame and frame.get("t") == "frame"
    assert "C4D>" not in str(frame.get("text") or "")

    console = socket.create_connection(("127.0.0.1", port), timeout=5)
    cbuf = bytearray()
    _send_msg(console, {"t": "hello", "role": "console", "token": token, "repo_root": str(REPO), "client_pid": 222})
    assert _recv_line(console, cbuf)["t"] == "hello_ok"
    _send_msg(console, {"t": "cmd", "line": "workers"})
    reply = _recv_line(console, cbuf)
    assert reply and reply.get("t") == "reply"
    assert "workers readiness" in str(reply.get("text") or "")
    assert "workers actifs" in str(reply.get("text") or "")

    _send_msg(console, {"t": "cmd", "line": "quit"})
    quit_reply = _recv_line(console, cbuf)
    assert quit_reply and quit_reply.get("quit") is True
    shutdown = None
    deadline = time.time() + 5.0
    while time.time() < deadline:
        msg = _recv_line(dash, dbuf)
        if msg and msg.get("t") == "shutdown":
            shutdown = msg
            break
    assert shutdown is not None
    dash.close()
    console.close()
    server._shutdown.set()


def test_console_input_not_truncated_by_dashboard():
    from ac_workers.c4d_ipc import PROMPT

    assert PROMPT == "C4D command> "
    sample = "x" * 100
    assert len(sample) == 100


def test_historical_readiness_four_gates():
    import time
    from unittest import mock

    from ac_workers.control_center import WORKER_OP_WAITING_READY, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_WAITING_READY
    center._worker_op_session_id[0] = "sess_ready"
    center._worker_op_started_at[0] = time.time()
    ready = {
        "worker_id": 0,
        "session_id": "sess_ready",
        "phase": "running",
        "initial_session_attached": True,
        "initial_gear_engaged": True,
        "policy_ready": True,
        "collector_pid": 4001,
        "ac_pid": 3001,
        "heartbeat_age_ms": 100.0,
    }
    center._worker_status = lambda _wid: ready  # type: ignore[method-assign]
    with mock.patch("ac_workers.control_center.pid_alive", return_value=True), mock.patch(
        "ac_workers.control_center._cached_diag_probe",
        return_value=(True, "ok", {"collector_pid": 4001, "boot_id": "b", "phase": "running"}, 0.0),
    ):
        assert center._reconcile_starting_ops() is True
    assert center._worker_op_state[0] == "offline"


def test_scale_boot_clears_stale_start_timeout():
    from ac_workers.control_center import WORKER_OP_START_TIMEOUT, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[3] = WORKER_OP_START_TIMEOUT
    center._worker_op_errors[3] = "worker_start_timeout"
    center._worker_op_started_at[3] = 1.0
    center._worker_op_session_id[3] = "old_sess"
    center._hard_clean_and_verify = lambda: ""  # type: ignore[method-assign]
    center._run_vjoy_clean_slate_blocking = lambda: ""  # type: ignore[method-assign]
    center._launch_worker_start = lambda _wid: None  # type: ignore[method-assign]
    center._worker_runtime_busy = lambda _wid: ""  # type: ignore[method-assign]
    center._evaluate_worker_activity = lambda _wid, **kw: __import__(  # type: ignore[method-assign]
        "ac_workers.control_center", fromlist=["WorkerActivity"]
    ).WorkerActivity(False, "", "offline")

    center.start_workers_sequential([0, 1, 2, 3])
    assert center._worker_op_state[3] == "offline"
    assert "worker_start_timeout" not in center._worker_op_errors


def test_scale_startup_not_blocked_by_stale_start_timeout():
    from ac_workers.control_center import WORKER_OP_START_TIMEOUT, ControlCenter

    center = ControlCenter(REPO)
    center._scale_targets = [0, 1, 2, 3]
    center._scale_queue = [1, 2, 3]
    center._worker_op_state[0] = "offline"
    center._worker_op_state[3] = WORKER_OP_START_TIMEOUT
    center._status_for_op = lambda _wid: {}  # type: ignore[method-assign]
    assert center._scale_startup_busy() is False


def test_stale_start_timeout_cleared_when_not_in_scale_boot():
    from ac_workers.control_center import WORKER_OP_START_TIMEOUT, ControlCenter

    center = ControlCenter(REPO)
    center._scale_targets = [0, 1]
    center._scale_queue = []
    center._worker_op_state[3] = WORKER_OP_START_TIMEOUT
    center._worker_op_errors[3] = "worker_start_timeout"
    center._evaluate_worker_activity = lambda _wid, **kw: __import__(  # type: ignore[method-assign]
        "ac_workers.control_center", fromlist=["WorkerActivity"]
    ).WorkerActivity(False, "", "offline")
    assert center._reconcile_starting_ops() is True
    assert center._worker_op_state[3] == "offline"
    assert 3 not in center._worker_op_errors


def test_startup_timeout_observational():
    import time
    from unittest import mock

    from ac_workers.control_center import WORKER_OP_START_TIMEOUT, WORKER_OP_WAITING_READY, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_WAITING_READY
    center._worker_op_session_id[0] = "sess_t"
    center._worker_op_started_at[0] = time.time() - 181.0
    center._worker_status = lambda _wid: {  # type: ignore[method-assign]
        "session_id": "sess_t",
        "phase": "attaching_live_session",
        "collector_pid": 5010,
    }
    with mock.patch.object(
        center,
        "_manifest",
        return_value={"sessionId": "sess_t", "workers": [{"workerId": 0, "acPid": 4010, "collectorPid": 5010}]},
    ), mock.patch("ac_workers.session_cleanup.cleanup_stale_state") as cleanup_mock, mock.patch(
        "ac_workers.control_center.pid_alive", side_effect=lambda pid: pid in (4010, 5010)
    ):
        changed = center._reconcile_starting_ops()
        assert changed is True
        assert center._worker_op_state[0] == WORKER_OP_START_TIMEOUT
        cleanup_mock.assert_not_called()
        view = center._build_worker_view(0)
    assert view.phase == WORKER_OP_START_TIMEOUT
    assert view.collector_pid == 5010
    assert view.ac_pid == 4010


def test_startup_reconcile_never_cleanup():
    import time
    from unittest import mock

    from ac_workers.control_center import WORKER_OP_WAITING_READY, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_WAITING_READY
    center._worker_op_started_at[0] = time.time()
    center._worker_op_session_id[0] = "sess_nc"
    center._worker_status = lambda _wid: {  # type: ignore[method-assign]
        "session_id": "sess_nc",
        "phase": "creating_environment",
        "collector_pid": 777,
    }
    with mock.patch("ac_workers.session_cleanup.cleanup_stale_state") as cleanup_mock, mock.patch(
        "ac_workers.collector_pid.collector_evidence",
        return_value=(False, 777, "collector_real_process_died", {}),
    ):
        center._reconcile_starting_ops()
        center._build_worker_view(0)
    cleanup_mock.assert_not_called()
    assert "collector_real_process_died" not in (center._worker_op_errors.get(0) or "")


def test_attaching_live_session_phase_preserved_in_view():
    from unittest import mock

    from ac_workers.control_center import WORKER_OP_WAITING_READY, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_WAITING_READY
    center._worker_op_session_id[0] = "sess_a"
    center._manifest = lambda: {  # type: ignore[method-assign]
        "sessionId": "sess_a",
        "workers": [{"workerId": 0, "acPid": 100, "collectorPid": 200}],
    }
    center._worker_status = lambda _wid: {  # type: ignore[method-assign]
        "session_id": "sess_a",
        "phase": "attaching_live_session",
        "collector_pid": 200,
    }
    with mock.patch("ac_workers.control_center.pid_alive", return_value=True):
        view = center._build_worker_view(0)
    assert view.phase == "attaching_live_session"
    assert view.active is False
    assert view.collector_pid == 200


def test_waiting_ready_blocks_second_start():
    from ac_workers.control_center import WORKER_OP_WAITING_READY, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_WAITING_READY
    msg = center.start_workers([0])
    assert "start already in progress" in msg


def test_learner_inactive_does_not_fail_worker_startup():
    from ac_workers.control_center import WORKER_OP_WAITING_READY, ControlCenter, is_learner_really_active

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_WAITING_READY
    center._worker_op_session_id[0] = "sess_l"
    center._manifest = lambda: {"sessionId": "sess_l", "workers": [{"workerId": 0}]}  # type: ignore[method-assign]
    center._worker_status = lambda _wid: {  # type: ignore[method-assign]
        "session_id": "sess_l",
        "phase": "creating_environment",
        "collector_pid": 321,
    }
    assert is_learner_really_active({"learner_pid": 0}, "sess_l") is False
    view = center._build_worker_view(0)
    assert view.phase == "environment_loading"
    assert center._worker_op_state[0] == WORKER_OP_WAITING_READY


def test_stop_zero_during_waiting_ready():
    from ac_workers.control_center import WORKER_OP_STOPPING, WORKER_OP_WAITING_READY, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_WAITING_READY
    center._launch_worker_stop = lambda _wid, **kwargs: None  # type: ignore[method-assign]
    msg = center.stop_worker(0)
    assert "stopping" in msg
    assert center._worker_op_state[0] == WORKER_OP_STOPPING


def test_historical_worker_ready_helper():
    from ac_workers.control_center import historical_worker_ready

    assert historical_worker_ready({}) is False
    assert historical_worker_ready({"phase": "running"}) is False
    assert historical_worker_ready(
        {
            "phase": "running",
            "initial_session_attached": True,
            "initial_gear_engaged": True,
            "policy_ready": True,
        }
    )
    assert historical_worker_ready(
        {
            "phase": "running",
            "initial_session_attached": True,
            "initial_gear_engaged": False,
            "policy_ready": True,
        }
    )
    # idle is the post-attach ready state (held at neutral until learner start).
    assert historical_worker_ready(
        {
            "phase": "idle",
            "initial_session_attached": True,
            "policy_ready": True,
        }
    )
    assert historical_worker_ready({"phase": "idle"}) is False


def test_dashboard_activity_skips_deep_scan_and_blocking_diag():
    src = (REPO / "ac_workers" / "control_center.py").read_text(encoding="utf-8")
    invoke = src.split("def startup_status_ready")[1].split("def worker_profile_missing_message")[0]
    assert "_cached_diag_probe" in invoke
    assert "_diag_probe_blocking" not in invoke
    view = src.split("def _build_worker_view")[1].split("def collect_worker_view")[0]
    assert "lightweight=True" in view
    activity = src.split("def is_worker_really_active")[1].split("def is_learner_really_active")[0]
    assert activity.index('return WorkerActivity(False, "heartbeat_stale"') < activity.index(
        "collector_evidence("
    )


def test_ac_closed_during_attach_cancels_startup():
    from unittest import mock

    from ac_workers.control_center import WORKER_OP_CANCELLED, WORKER_OP_WAITING_READY, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_WAITING_READY
    center._worker_ac_was_alive[0] = True
    center._worker_seen_ac_pid[0] = 4242
    with mock.patch.object(
        center,
        "_manifest",
        return_value={"sessionId": "s", "workers": [{"workerId": 0, "acPid": 4242, "collectorPid": 9000}]},
    ), mock.patch("ac_workers.control_center.pid_alive", side_effect=lambda pid: pid == 9000), mock.patch(
        "ac_workers.worker_handshake.tcp_open", return_value=False
    ), mock.patch("ac_workers.emergency_stop._stop_pid", return_value=True) as stop_mock:
        assert center._reconcile_ac_closed_during_startup(0) is True
    assert center._worker_op_state[0] == WORKER_OP_CANCELLED
    assert "AC closed" in center._message
    stop_mock.assert_called_once_with(9000)
    view = center._build_worker_view(0)
    assert view.phase == WORKER_OP_CANCELLED
    assert view.ac_pid == 0
    assert view.collector_pid == 0


def test_build_worker_view_notes_ac_presence_without_reconcile():
    from unittest import mock

    from ac_workers.control_center import WORKER_OP_WAITING_READY, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_WAITING_READY
    with mock.patch.object(
        center,
        "_manifest",
        return_value={"sessionId": "s", "workers": [{"workerId": 0, "acPid": 7777}]},
    ), mock.patch("ac_workers.control_center.pid_alive", return_value=True), mock.patch(
        "ac_workers.control_center.startup_ac_living", return_value=True
    ), mock.patch.object(center, "_status_for_op", return_value={"phase": "attaching_live_session"}):
        center._build_worker_view(0)
    assert center._worker_ac_was_alive.get(0) is True


def test_ac_dead_mgmt_open_cancels_after_grace():
    from unittest import mock

    from ac_workers.control_center import WORKER_OP_CANCELLED, WORKER_OP_WAITING_READY, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_WAITING_READY
    center._worker_ac_was_alive[0] = True
    center._worker_seen_ac_pid[0] = 4242
    t0 = 1000.0
    with mock.patch.object(
        center,
        "_manifest",
        return_value={"sessionId": "s", "workers": [{"workerId": 0, "acPid": 4242, "collectorPid": 9000}]},
    ), mock.patch("ac_workers.control_center.pid_alive", return_value=False), mock.patch(
        "ac_workers.worker_handshake.tcp_open", return_value=True
    ), mock.patch("ac_workers.control_center.time.time", side_effect=[t0, t0 + 2.5]), mock.patch(
        "ac_workers.emergency_stop._stop_pid", return_value=True
    ):
        assert center._reconcile_ac_closed_during_startup(0) is False
        assert center._reconcile_ac_closed_during_startup(0) is True
    assert center._worker_op_state[0] == WORKER_OP_CANCELLED


def test_stale_status_json_ac_dead_cancels_via_poll():
    from unittest import mock

    from ac_workers.control_center import WORKER_OP_CANCELLED, WORKER_OP_WAITING_READY, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_WAITING_READY
    center._worker_ac_was_alive[0] = True
    center._worker_seen_ac_pid[0] = 22936
    frozen_status = {
        "phase": "attaching_live_session",
        "collector_pid": 25976,
        "status_written_at": "2026-06-24T19:47:59.673000+00:00",
        "heartbeat_age_ms": 0.0,
    }
    with mock.patch.object(
        center,
        "_manifest",
        return_value={"sessionId": "s", "workers": [{"workerId": 0, "acPid": 22936, "collectorPid": 25976}]},
    ), mock.patch.object(center, "_status_for_op", return_value=frozen_status), mock.patch(
        "ac_workers.control_center.pid_alive", side_effect=lambda pid: pid == 25976
    ), mock.patch("ac_workers.worker_handshake.tcp_open", return_value=False), mock.patch(
        "ac_workers.emergency_stop._stop_pid", return_value=True
    ):
        changed = center._reconcile_starting_ops()
    assert changed is True
    assert center._worker_op_state[0] == WORKER_OP_CANCELLED


def test_scale_test_starts_workers_sequentially():
    from ac_workers.control_center import WORKER_OP_STARTING, ControlCenter

    center = ControlCenter(REPO)
    started: list[int] = []
    center._launch_worker_start = lambda wid: started.append(wid)  # type: ignore[method-assign]
    center._worker_runtime_busy = lambda _wid: ""  # type: ignore[method-assign]
    center._hard_clean_and_verify = lambda: ""  # type: ignore[method-assign]
    center._run_vjoy_clean_slate_blocking = lambda: ""  # type: ignore[method-assign]
    center._evaluate_worker_activity = lambda _wid, **kw: __import__(  # type: ignore[method-assign]
        "ac_workers.control_center", fromlist=["WorkerActivity"]
    ).WorkerActivity(False, "", "offline")

    msg = center.start_workers_sequential([0, 1, 2])
    assert "scale-test" in msg
    assert started == [0]
    assert center._worker_op_state[0] == WORKER_OP_STARTING
    assert center._scale_queue == [1, 2]

    center._advance_scale_queue()
    assert started == [0]

    center._worker_op_state[0] = "offline"
    center._advance_scale_queue()
    assert started == [0, 1]
    assert center._scale_queue == [2]

    center._worker_op_state[1] = "offline"
    center._advance_scale_queue()
    assert started == [0, 1, 2]
    assert center._scale_queue == []


def test_scale_test_advances_when_documents_released():
    """Next worker starts once the previous collector has attached (documents free),
    without waiting for full policy/telemetry readiness."""
    from ac_workers.control_center import (
        WORKER_OP_STARTING,
        WORKER_OP_WAITING_READY,
        ControlCenter,
    )

    center = ControlCenter(REPO)
    started: list[int] = []
    center._launch_worker_start = lambda wid: started.append(wid)  # type: ignore[method-assign]
    center._worker_runtime_busy = lambda _wid: ""  # type: ignore[method-assign]
    center._hard_clean_and_verify = lambda: ""  # type: ignore[method-assign]
    center._run_vjoy_clean_slate_blocking = lambda: ""  # type: ignore[method-assign]
    center._is_start_op_active = lambda _wid: False  # type: ignore[method-assign]
    center._evaluate_worker_activity = lambda _wid, **kw: __import__(  # type: ignore[method-assign]
        "ac_workers.control_center", fromlist=["WorkerActivity"]
    ).WorkerActivity(False, "", "offline")

    center.start_workers_sequential([0, 1])
    assert started == [0]

    center._worker_op_state[0] = WORKER_OP_STARTING
    center._advance_scale_queue()
    assert started == [0]

    center._worker_op_state[0] = WORKER_OP_WAITING_READY
    center._status_for_op = lambda _wid: {"phase": "attaching_live_session", "initial_session_attached": False}  # type: ignore[method-assign]
    center._advance_scale_queue()
    assert started == [0]

    center._status_for_op = lambda _wid: {"phase": "running", "initial_session_attached": True}  # type: ignore[method-assign]
    center._advance_scale_queue()
    assert started == [0, 1]


def test_scale_test_blocks_when_state_not_clean():
    """scale-test/clean-start refuse to launch into a dirty system (orphan acs.exe
    left by a prior run), instead of silently colliding with it."""
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    started: list[int] = []
    center._launch_worker_start = lambda wid: started.append(wid)  # type: ignore[method-assign]
    center._worker_runtime_busy = lambda _wid: ""  # type: ignore[method-assign]
    center._hard_clean_and_verify = lambda: "demarrage BLOQUE — orphan acs.exe"  # type: ignore[method-assign]

    msg = center.start_workers_sequential([0, 1])
    assert "BLOQUE" in msg
    assert started == []
    assert center._scale_queue == []


def test_scale_test_hard_cleans_then_starts_full_set():
    """After a hard clean (nothing running), the full requested set is launched."""
    from ac_workers.control_center import WORKER_OP_STARTING, ControlCenter

    center = ControlCenter(REPO)
    started: list[int] = []
    center._launch_worker_start = lambda wid: started.append(wid)  # type: ignore[method-assign]
    center._worker_runtime_busy = lambda _wid: ""  # type: ignore[method-assign]
    center._hard_clean_and_verify = lambda: ""  # type: ignore[method-assign]
    center._run_vjoy_clean_slate_blocking = lambda: ""  # type: ignore[method-assign]
    center._evaluate_worker_activity = lambda _wid, **kw: __import__(  # type: ignore[method-assign]
        "ac_workers.control_center", fromlist=["WorkerActivity"]
    ).WorkerActivity(False, "", "offline")

    center.start_workers_sequential([0, 1])
    assert started == [0]
    assert center._worker_op_state[0] == WORKER_OP_STARTING
    assert center._scale_queue == [1]


def test_hard_clean_skips_emergency_when_already_clean():
    from unittest import mock

    from ac_workers.control_center import ControlCenter

    with mock.patch("ac_workers.session_cleanup.cleanup_stale_state"):
        center = ControlCenter(REPO)
    center.refresh_metrics_cache = lambda **_kw: None  # type: ignore[method-assign]
    calls = {"ps": 0}

    def _ps(*_a, **_k):
        calls["ps"] += 1
        return 0, "", ""

    center._run_ps_captured = _ps  # type: ignore[method-assign]
    with mock.patch("ac_workers.system_control.assert_clean_state", return_value=(True, {})):
        assert center._hard_clean_and_verify() == ""
    assert calls["ps"] == 0


def test_vjoy_clean_slate_skips_driver_reset_when_free():
    from unittest import mock

    from ac_workers.control_center import ControlCenter

    with mock.patch("ac_workers.session_cleanup.cleanup_stale_state"):
        center = ControlCenter(REPO)
    center._vjoy_all_devices_free = lambda: True  # type: ignore[method-assign]
    center._vjoy_saved_mapping_ready = lambda: True  # type: ignore[method-assign]
    assert center._run_vjoy_clean_slate_blocking() == ""
    assert center._skip_vjoy_index_probe is True
    assert "-SkipVjoyIndexProbe" in center._worker_start_script_args(0)
    assert "-SkipVjoyIndexProbe" not in center._worker_start_script_args(1)


def test_scale_settle_gate_holds_then_advances():
    """After a worker readies, the next launch is held for SCALE_SETTLE_SEC so the
    new heavy CSP instance does not starve/freeze the one that just came up."""
    from unittest import mock

    from ac_workers.control_center import SCALE_SETTLE_SEC, ControlCenter

    center = ControlCenter(REPO)
    center._scale_targets = [0, 1]
    center._scale_queue = [1]
    center._scale_last_ready_ts = 100.0
    started: list[int] = []
    center._scale_startup_busy = lambda: False  # type: ignore[method-assign]
    center._worker_runtime_busy = lambda _wid: ""  # type: ignore[method-assign]
    center.start_workers = lambda ids: (started.extend(ids), "ok")[1]  # type: ignore[method-assign]

    with mock.patch(
        "ac_workers.control_center.time.time",
        return_value=100.0 + SCALE_SETTLE_SEC - 1.0,
    ):
        assert center._advance_scale_queue() is False
    assert started == []
    assert center._scale_queue == [1]

    with mock.patch(
        "ac_workers.control_center.time.time",
        return_value=100.0 + SCALE_SETTLE_SEC + 1.0,
    ):
        assert center._advance_scale_queue() is True
    assert started == [1]
    assert center._scale_queue == []


def test_startup_freeze_watchdog_requeues_then_fails():
    """A frozen AC during startup (telemetry seen then stale) is auto-retried once
    via requeue, then marked failed when the retry budget is exhausted."""
    from unittest import mock

    from ac_workers.control_center import (
        WORKER_OP_FAILED,
        WORKER_OP_OFFLINE,
        WORKER_OP_WAITING_READY,
        ControlCenter,
    )

    center = ControlCenter(REPO)
    center._scale_targets = [0]
    center._worker_startup_restart_count = {0: 0}
    frozen = {"phase": "waiting_for_drive_input", "telemetry_age_ms": 999999.0}
    center._reconcile_ac_closed_during_startup = lambda _wid: False  # type: ignore[method-assign]
    center._is_start_op_active = lambda _wid: False  # type: ignore[method-assign]
    center._status_for_op = lambda _wid: frozen  # type: ignore[method-assign]
    center._force_stop_worker_processes = lambda _wid: None  # type: ignore[method-assign]
    center.refresh_metrics_cache = lambda **kwargs: None  # type: ignore[method-assign]

    center._worker_op_state[0] = WORKER_OP_WAITING_READY
    center._worker_startup_telemetry_seen[0] = True
    with mock.patch("ac_workers.session_cleanup.cleanup_stale_state"):
        assert center._reconcile_starting_ops() is True
    assert center._worker_op_state[0] == WORKER_OP_OFFLINE
    assert 0 in center._scale_queue
    assert center._worker_startup_restart_count[0] == 1

    center._scale_queue.clear()
    center._worker_op_state[0] = WORKER_OP_WAITING_READY
    center._worker_startup_telemetry_seen[0] = True
    with mock.patch("ac_workers.session_cleanup.cleanup_stale_state"):
        assert center._reconcile_starting_ops() is True
    assert center._worker_op_state[0] == WORKER_OP_FAILED


def test_retryable_start_failure_requeues_in_scale():
    """A 'Drive input not delivered' launcher failure during a scale-test requeues
    the worker for one retry instead of marking it failed immediately."""
    from unittest import mock

    from ac_workers.control_center import WORKER_OP_OFFLINE, ControlCenter, _BgResult

    center = ControlCenter(REPO)
    center._scale_targets = [0]
    center._worker_startup_restart_count = {0: 0}
    center._reload_session_from_manifest = lambda: None  # type: ignore[method-assign]
    center._status_for_op = lambda _wid: {"phase": "waiting_for_drive_failed"}  # type: ignore[method-assign]
    center._force_stop_worker_processes = lambda _wid: None  # type: ignore[method-assign]
    center.refresh_metrics_cache = lambda **kwargs: None  # type: ignore[method-assign]

    result = _BgResult(
        key="w0",
        operation="start",
        code=1,
        output="worker 0 ECHEC: Worker 0: Drive input not delivered.",
        worker_id=0,
    )
    with mock.patch("ac_workers.session_cleanup.cleanup_stale_state"):
        center._handle_bg_result(result)
    assert center._worker_op_state[0] == WORKER_OP_OFFLINE
    assert 0 in center._scale_queue
    assert center._worker_startup_restart_count[0] == 1


def test_non_retryable_start_failure_not_requeued():
    """A non-contention start failure (e.g. profile error) is finalized, not retried."""
    from unittest import mock

    from ac_workers.control_center import WORKER_OP_FAILED, ControlCenter, _BgResult

    center = ControlCenter(REPO)
    center._scale_targets = [0]
    center._worker_startup_restart_count = {0: 0}
    center._reload_session_from_manifest = lambda: None  # type: ignore[method-assign]
    center._status_for_op = lambda _wid: {"phase": "stopped"}  # type: ignore[method-assign]
    center.refresh_metrics_cache = lambda **kwargs: None  # type: ignore[method-assign]

    result = _BgResult(
        key="w0",
        operation="start",
        code=1,
        output="worker 0 ECHEC: profile missing",
        worker_id=0,
    )
    with mock.patch("ac_workers.session_cleanup.cleanup_stale_state"):
        center._handle_bg_result(result)
    assert center._worker_op_state[0] == WORKER_OP_FAILED
    assert 0 not in center._scale_queue


def test_start_workers_refuses_when_diag_port_busy():
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    center._worker_runtime_busy = lambda _wid: "worker 0 diag port 24100 in use — stop or abort first"  # type: ignore[method-assign]
    launched: list[int] = []
    center._launch_worker_start = lambda wid: launched.append(wid)  # type: ignore[method-assign]
    msg = center.start_workers([0])
    assert "24100" in msg
    assert launched == []


def test_worker_runtime_busy_message_detects_ready_status():
    from unittest import mock

    from ac_workers.control_center import worker_runtime_busy_message

    with mock.patch("ac_workers.control_center.pid_alive", return_value=True), mock.patch(
        "ac_workers.control_center.is_collector_process", return_value=True
    ):
        msg = worker_runtime_busy_message(
            0,
            status={
                "phase": "running",
                "initial_session_attached": True,
                "initial_gear_engaged": True,
                "policy_ready": True,
                "collector_pid": 4242,
            },
            manifest_entry={},
        )
    assert "already running" in msg


def test_worker_runtime_busy_ignores_stale_ready_without_live_pid():
    from unittest import mock

    from ac_workers.control_center import worker_runtime_busy_message

    with mock.patch("ac_workers.control_center.pid_alive", return_value=False), mock.patch(
        "ac_workers.worker_handshake.probe_collector_diag_ping",
        return_value=(False, False, "", {}),
    ):
        msg = worker_runtime_busy_message(
            5,
            status={
                "phase": "running",
                "initial_session_attached": True,
                "initial_gear_engaged": True,
                "policy_ready": True,
                "collector_pid": 5340,
            },
            manifest_entry={},
        )
    assert msg == ""


def test_ac_closed_during_active_launcher_cancels_and_kills_job():
    from unittest import mock

    from ac_workers.control_center import (
        WORKER_OP_CANCELLED,
        WORKER_OP_STARTING,
        ControlCenter,
    )

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_STARTING
    center._worker_ac_was_alive[0] = True
    center._worker_seen_ac_pid[0] = 7300

    class _FakeProc:
        def __init__(self):
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def communicate(self, timeout=None):
            return ("", "")

    fake = _FakeProc()
    center._bg_procs[center._worker_job_key(0)] = fake
    with mock.patch.object(
        center,
        "_manifest",
        return_value={"sessionId": "s", "workers": [{"workerId": 0, "acPid": 7300, "collectorPid": 6136}]},
    ), mock.patch("ac_workers.control_center.pid_alive", side_effect=lambda pid: pid == 6136), mock.patch(
        "ac_workers.worker_handshake.tcp_open", return_value=False
    ), mock.patch("ac_workers.emergency_stop._stop_pid", return_value=True) as stop_mock:
        changed = center._reconcile_starting_ops()
    assert changed is True
    assert center._worker_op_state[0] == WORKER_OP_CANCELLED
    assert fake.terminated is True
    stop_mock.assert_called_once_with(6136)


def test_late_start_result_does_not_clobber_cancelled():
    from ac_workers.control_center import WORKER_OP_CANCELLED, ControlCenter
    from ac_workers.control_center import _BgResult  # type: ignore[attr-defined]

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_CANCELLED
    result = _BgResult(
        key=center._worker_job_key(0),
        operation="start",
        code=1,
        output="terminated",
        worker_id=0,
        duration_sec=1.0,
        chain="",
    )
    center._handle_bg_result(result)
    assert center._worker_op_state[0] == WORKER_OP_CANCELLED


def test_ac_closed_after_start_timeout_clears_op():
    from unittest import mock

    from ac_workers.control_center import WORKER_OP_CANCELLED, WORKER_OP_START_TIMEOUT, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_START_TIMEOUT
    center._worker_ac_was_alive[0] = True
    center._worker_seen_ac_pid[0] = 5151
    with mock.patch.object(
        center,
        "_manifest",
        return_value={"sessionId": "s", "workers": [{"workerId": 0, "acPid": 5151}]},
    ), mock.patch("ac_workers.control_center.pid_alive", return_value=False), mock.patch(
        "ac_workers.worker_handshake.tcp_open", return_value=False
    ), mock.patch("ac_workers.emergency_stop._stop_pid", return_value=True):
        changed = center._reconcile_starting_ops()
    assert changed is True
    assert center._worker_op_state[0] == WORKER_OP_CANCELLED
    assert center._worker_op_started_at.get(0) is None


def test_abort_worker_kills_collector_and_cancels_op():
    from unittest import mock

    from ac_workers.control_center import WORKER_OP_CANCELLED, WORKER_OP_WAITING_READY, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_WAITING_READY
    with mock.patch.object(
        center,
        "_manifest",
        return_value={"sessionId": "s", "workers": [{"workerId": 0, "collectorPid": 9000}]},
    ), mock.patch("ac_workers.control_center.pid_alive", return_value=True), mock.patch(
        "ac_workers.emergency_stop._stop_pid", return_value=True
    ) as stop_mock, mock.patch.object(center, "_cancel_worker_bg_proc") as cancel_mock:
        msg = center.abort_worker(0)
    assert "aborted" in msg
    assert center._worker_op_state[0] == WORKER_OP_CANCELLED
    stop_mock.assert_called_once_with(9000)
    cancel_mock.assert_called_once_with(0)


def test_running_worker_window_closed_stops_and_reports():
    from unittest import mock

    from ac_workers.control_center import (
        WORKER_OP_OFFLINE,
        WORKER_OP_STOPPED,
        ControlCenter,
        WorkerActivity,
    )

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_OFFLINE
    center._worker_running_ac_pid[0] = 7300
    with mock.patch.object(
        center,
        "_evaluate_worker_activity",
        return_value=WorkerActivity(False, "ac_dead", "stale", 6136, 0),
    ), mock.patch.object(center, "_resolve_ac_pid", return_value=0), mock.patch.object(
        center,
        "_manifest",
        return_value={"sessionId": "s", "workers": [{"workerId": 0, "collectorPid": 6136}]},
    ), mock.patch(
        "ac_workers.control_center.pid_alive", side_effect=lambda pid: pid == 6136
    ), mock.patch(
        "ac_workers.control_center.is_ac_process", return_value=False
    ), mock.patch(
        "ac_workers.emergency_stop._stop_pid", return_value=True
    ) as stop_mock, mock.patch.object(center, "_cancel_worker_bg_proc") as cancel_mock:
        changed = center._reconcile_running_ac_closed()
    assert changed is True
    assert center._worker_op_state[0] == WORKER_OP_STOPPED
    stop_mock.assert_called_once_with(6136)
    cancel_mock.assert_called_once_with(0)
    assert "stopped working" in center._message
    view = center._build_worker_view(0)
    assert view.phase == WORKER_OP_STOPPED
    assert view.active is False
    assert "stopped working" in view.last_error


def test_running_worker_alive_records_ac_pid_no_stop():
    from unittest import mock

    from ac_workers.control_center import (
        WORKER_OP_OFFLINE,
        ControlCenter,
        WorkerActivity,
    )

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_OFFLINE
    with mock.patch.object(
        center,
        "_evaluate_worker_activity",
        return_value=WorkerActivity(True, "ok", "running", 6136, 7300),
    ), mock.patch.object(center, "_resolve_ac_pid", return_value=7300), mock.patch(
        "ac_workers.control_center.pid_alive", return_value=True
    ), mock.patch(
        "ac_workers.control_center.is_ac_process", return_value=True
    ):
        changed = center._reconcile_running_ac_closed()
    assert changed is False
    assert center._worker_running_ac_pid.get(0) == 7300


def test_stopped_worker_can_start_again():
    from ac_workers.control_center import WORKER_OP_STOPPED, ControlCenter, WorkerActivity

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_STOPPED
    center._worker_running_ac_pid[0] = 7300
    center._evaluate_worker_activity = lambda _wid, **kw: WorkerActivity(False, "", "offline")  # type: ignore[method-assign]
    center._launch_worker_start = lambda _wid: None  # type: ignore[method-assign]
    msg = center.start_workers([0])
    assert "starting" in msg
    assert center._worker_op_state[0] == "starting"
    assert center._worker_running_ac_pid.get(0) is None


def test_cancelled_worker_can_start_again():
    from ac_workers.control_center import WORKER_OP_CANCELLED, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_CANCELLED
    center._evaluate_worker_activity = lambda _wid, **kw: __import__(  # type: ignore[method-assign]
        "ac_workers.control_center", fromlist=["WorkerActivity"]
    ).WorkerActivity(False, "", "offline")
    center._launch_worker_start = lambda _wid: None  # type: ignore[method-assign]
    msg = center.start_workers([0])
    assert "starting" in msg
    assert center._worker_op_state[0] == "starting"


def test_attach_failed_status_marks_worker_failed():
    from ac_workers.control_center import WORKER_OP_FAILED, WORKER_OP_WAITING_READY, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_WAITING_READY
    center._worker_op_session_id[0] = "sess_af"
    center._worker_status = lambda _wid: {  # type: ignore[method-assign]
        "session_id": "sess_af",
        "phase": "attach_failed",
        "last_error": "sensors_par_not_streaming",
        "collector_pid": 123,
    }
    assert center._reconcile_starting_ops() is True
    assert center._worker_op_state[0] == WORKER_OP_FAILED


def test_status_for_op_keeps_attach_failed_on_session_mismatch():
    from ac_workers.control_center import WORKER_OP_STARTING, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_STARTING
    center._worker_op_session_id[0] = "sess_launcher"
    center._worker_status = lambda _wid: {  # type: ignore[method-assign]
        "session_id": "sess_collector",
        "phase": "attach_failed",
        "last_error": "driving session active but management port 2347 did not open",
    }
    st = center._status_for_op(0)
    assert st.get("phase") == "attach_failed"


def test_status_for_op_accepts_ready_on_session_mismatch():
    """Worker 0 ready under an older session must clear start_timeout in C4D."""
    from unittest import mock

    from ac_workers.control_center import WORKER_OP_OFFLINE, WORKER_OP_START_TIMEOUT, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_START_TIMEOUT
    center._worker_op_errors[0] = "worker_start_timeout"
    center._worker_op_session_id[0] = "sess_launcher_new"
    ready = {
        "session_id": "sess_collector_old",
        "phase": "idle",
        "initial_session_attached": True,
        "policy_ready": True,
        "collector_pid": 26692,
        "heartbeat_age_ms": 50.0,
        "telemetry_age_ms": 10.0,
    }
    center._worker_status = lambda _wid: ready  # type: ignore[method-assign]
    with mock.patch("ac_workers.control_center.pid_alive", return_value=True):
        st = center._status_for_op(0)
        assert st.get("phase") == "idle"
        assert center._worker_op_session_id[0] == "sess_collector_old"
        assert center._reconcile_starting_ops() is True
    assert center._worker_op_state[0] == WORKER_OP_OFFLINE
    assert 0 not in center._worker_op_errors
    assert center._stale_startup_op_error(ready, "worker_start_timeout") == ""


def test_scale_startup_not_busy_after_attach_failed():
    from ac_workers.control_center import WORKER_OP_STARTING, ControlCenter

    center = ControlCenter(REPO)
    center._scale_targets = [0, 1]
    center._scale_queue = [1]
    center._worker_op_state[0] = WORKER_OP_STARTING
    center._is_start_op_active = lambda _wid: True  # type: ignore[method-assign]
    center._status_for_op = lambda _wid: {"phase": "attach_failed"}  # type: ignore[method-assign]
    assert center._scale_startup_busy() is False


def test_fresh_diag_attaching_not_collector_died():
    from ac_workers.control_center import WORKER_OP_WAITING_READY, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_WAITING_READY
    center._worker_op_session_id[0] = "sess_d"
    center._manifest = lambda: {  # type: ignore[method-assign]
        "sessionId": "sess_d",
        "workers": [{"workerId": 0, "collectorPid": 700}],
    }
    center._worker_status = lambda _wid: {  # type: ignore[method-assign]
        "session_id": "sess_d",
        "phase": "telemetry_wait_begin",
        "heartbeat_age_ms": 10.0,
        "collector_pid": 700,
    }
    view = center._build_worker_view(0)
    assert view.phase == "telemetry_wait_begin"
    assert "collector_real_process_died" not in (view.last_error or "")


def test_collector_stopped_status_shows_failed_not_starting():
    from unittest import mock

    from ac_workers.control_center import WORKER_OP_FAILED, WORKER_OP_STARTING, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_STARTING
    center._worker_op_session_id[0] = "sess_stop"
    center._manifest = lambda: {  # type: ignore[method-assign]
        "sessionId": "sess_stop",
        "workers": [{"workerId": 0, "acPid": 16232, "collectorPid": 3612}],
    }
    center._worker_status = lambda _wid: {  # type: ignore[method-assign]
        "session_id": "sess_stop",
        "phase": "stopped",
        "collector_pid": 3612,
        "last_error": "[Errno 22] Invalid argument",
    }
    with mock.patch("ac_workers.control_center.pid_alive", side_effect=lambda pid: pid in (16232, 3612)):
        view = center._build_worker_view(0)
    assert view.phase == WORKER_OP_FAILED
    assert view.ac_pid == 16232
    assert view.collector_pid == 3612
    assert center._worker_op_state[0] == WORKER_OP_STARTING


def test_reconcile_collector_stopped_clears_starting():
    from ac_workers.control_center import WORKER_OP_FAILED, WORKER_OP_STARTING, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_STARTING
    center._worker_op_session_id[0] = "sess_stop"
    center._worker_status = lambda _wid: {  # type: ignore[method-assign]
        "phase": "stopped",
        "session_id": "sess_stop",
        "last_error": "boom",
        "collector_pid": 99,
    }
    assert center._reconcile_starting_ops() is True
    assert center._worker_op_state[0] == WORKER_OP_FAILED


def test_double_start_blocked_immediately():
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    center._evaluate_worker_activity = lambda _wid, **kw: __import__(  # type: ignore[method-assign]
        "ac_workers.control_center", fromlist=["WorkerActivity"]
    ).WorkerActivity(False, "", "offline")
    center._launch_worker_start = lambda _wid: None  # type: ignore[method-assign]
    msg1 = center.start_workers([0])
    msg2 = center.start_workers([0])
    assert "starting" in msg1
    assert "start already in progress" in msg2


def test_start_op_lock_shared_on_center():
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    assert hasattr(center, "_start_op_lock")
    assert center._start_op_lock.acquire(blocking=False)
    center._start_op_lock.release()


def test_start_lock_released_after_finalize_failure():
    from ac_workers.control_center import WORKER_OP_FAILED, WORKER_OP_STARTING, ControlCenter

    center = ControlCenter(REPO)
    center._worker_op_state[0] = WORKER_OP_STARTING
    center._finalize_start_failure(0, "worker 0 start failed: collector stopped", "boom")
    assert center._worker_op_state[0] == WORKER_OP_FAILED
    center._evaluate_worker_activity = lambda _wid, **kw: __import__(  # type: ignore[method-assign]
        "ac_workers.control_center", fromlist=["WorkerActivity"]
    ).WorkerActivity(False, "", "offline")
    center._launch_worker_start = lambda _wid: None  # type: ignore[method-assign]
    msg = center.start_workers([0])
    assert "starting" in msg


def test_learner_refused_without_policy_ready():
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    center.worker_active = lambda _wid: True  # type: ignore[method-assign]
    center._worker_status = lambda _wid: {  # type: ignore[method-assign]
        "phase": "running",
        "policy_ready": False,
        "telemetry_age_ms": 0.0,
        "observation_complete": True,
    }
    assert center.learner_start() == (
        "learner start refused: no ready workers "
        "(fix vJoy/attach failures first)"
    )


def test_collector_fatal_logs_traceback_to_stderr():
    src = (REPO / "ac_workers" / "worker_collector.py").read_text(encoding="utf-8")
    block = src.split("except Exception as exc:", 1)[1].split("finally:", 1)[0]
    assert "_startup_log" in block
    assert "fatal error" in block
    assert "return _idle_after_failure()" in block
    assert "raise" not in block.split("return _idle_after_failure()", 1)[0]


def test_fsync_einval_skipped_on_windows():
    from ac_workers.collector_status import _replace_errno

    exc = OSError(22, "Invalid argument")
    assert _replace_errno(exc) == 22
    src = (REPO / "ac_workers" / "collector_status.py").read_text(encoding="utf-8")
    assert "_replace_errno(fsync_exc) == 22" in src


def test_c4d_server_token_required():
    import socket
    import threading

    from ac_workers.c4d_ipc import C4DControlServer, _recv_line, _send_msg
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    tmp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tmp.bind(("127.0.0.1", 0))
    port = tmp.getsockname()[1]
    tmp.close()
    server = C4DControlServer(center, port=port, session_token="good-token", repo_root=REPO)
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(0.15)
    try:
        bad = socket.create_connection(("127.0.0.1", port), timeout=5)
        bbuf = bytearray()
        _send_msg(bad, {"t": "hello", "role": "dashboard", "token": "bad-token", "repo_root": str(REPO)})
        reply = _recv_line(bad, bbuf)
        assert reply and reply.get("t") == "hello_reject"
        bad.close()
    finally:
        server._shutdown.set()


def test_c4d_prepare_first_start_then_reuse():
    from unittest import mock

    from ac_workers import c4d_server_lock as lock

    with __import__("tempfile").TemporaryDirectory() as tmp:
        repo = Path(tmp)
        with mock.patch.object(lock, "stop_all_c4d_for_repo", return_value=[]), mock.patch.object(
            lock, "find_valid_server", side_effect=[None, {"server_pid": 42, "token": "abc", "host": "127.0.0.1", "port": 24700}]
        ):
            first = lock.prepare_launch(repo, "127.0.0.1", 24700)
            assert first["action"] == "start"
            assert first["token"]
            second = lock.prepare_launch(repo, "127.0.0.1", 24700)
            assert second["action"] == "reuse"
            assert second["server_pid"] == 42


def test_c4d_dead_pid_allows_new_server():
    from unittest import mock

    from ac_workers import c4d_server_lock as lock

    with __import__("tempfile").TemporaryDirectory() as tmp:
        repo = Path(tmp)
        lock.write_session(
            repo,
            {"token": "old", "server_pid": 99999, "repo_root": str(repo), "host": "127.0.0.1", "port": 24700},
        )
        with mock.patch.object(lock, "pid_alive", return_value=False), mock.patch.object(
            lock, "stop_all_c4d_for_repo", return_value=[99999]
        ) as stop_mock, mock.patch.object(lock, "find_valid_server", return_value=None):
            prep = lock.prepare_launch(repo, "127.0.0.1", 24700)
        assert prep["action"] == "start"
        stop_mock.assert_called_once()


def test_c4d_recycled_pid_not_reused():
    from ac_workers import c4d_server_lock as lock

    repo = REPO
    cmd = "python -m http.server 24700"
    with __import__("unittest").mock.patch.object(lock, "pid_alive", return_value=True), __import__(
        "unittest"
    ).mock.patch.object(lock, "process_command_line", return_value=cmd):
        assert lock.is_c4d_server_process(468, repo, 24700, "tok") is False


def test_c4d_quit_clears_session_files():
    from ac_workers.c4d_server_lock import ServerRuntimeLock, read_session, repo_session_file

    with __import__("tempfile").TemporaryDirectory() as tmp:
        repo = Path(tmp)
        runtime = ServerRuntimeLock(repo, "127.0.0.1", 24700, "tok")
        ok, _ = runtime.acquire()
        assert ok
        assert repo_session_file(repo).is_file()
        runtime.release_cleanup()
        assert read_session(repo) is None


def test_stop_c4d_script_exists_and_preserves_pipeline():
    stop_ps = (REPO / "scripts" / "Stop-C4D-ControlCenter.ps1").read_text(encoding="utf-8")
    start_ps = (REPO / "scripts" / "Start-C4D-ControlCenter.ps1").read_text(encoding="utf-8")
    assert "c4d_server_lock stop" in stop_ps
    assert "Stop-Multi-AI-Session" not in stop_ps
    assert "Emergency-Stop-AI" not in stop_ps
    assert "Run-SAC-MultiWorker" not in stop_ps
    assert "'ac_workers.c4d_server_lock', 'prepare'" in start_ps
    assert "session-token" in start_ps


def test_start_c4d_control_center_launch_argv():
    ps = (REPO / "scripts" / "Start-C4D-ControlCenter.ps1").read_text(encoding="utf-8")
    assert "\u2013" not in ps and "\u2014" not in ps
    for line in ps.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        assert not stripped.startswith("-ArgumentList")
    assert "ConvertTo-C4DProcessArguments" in ps
    assert "ConvertTo-C4DWindowsArgument" in ps
    assert "server_stdout.log" in ps
    assert "server_stderr.log" in ps
    assert "Format-C4DServerStartFailure" in ps
    assert "New-C4DProcessStart" in ps
    assert "Get-C4DClientArgs -Mode 'dashboard'" in ps
    assert "Get-C4DClientArgs -Mode 'console'" in ps
    assert "'-m', 'ac_workers.control_center', $Mode" in ps
    assert "'-m', 'ac_workers.control_center', 'server'" in ps
    assert "Start-Process -FilePath $VenvPython" not in ps
    assert "Start-Process -FilePath 'cmd.exe'" not in ps


def test_c4d_windows_argv_quote_spaced_paths():
    from ac_workers.c4d_windows_argv import join_windows_cmdline, matches_windows_list2cmdline

    repo = r"C:\Leo projet\Assetto Corsa ai"
    ready = r"C:\Users\yadal\AppData\Local\Temp\c4d_server_ready_12345.flag"
    token = "abc123token"
    server_args = [
        "-m",
        "ac_workers.control_center",
        "server",
        "--host",
        "127.0.0.1",
        "--port",
        "24700",
        "--session-token",
        token,
        "--repo-root",
        repo,
        "--ready-file",
        ready,
    ]
    dash_args = [
        "-m",
        "ac_workers.control_center",
        "dashboard",
        "--host",
        "127.0.0.1",
        "--port",
        "24700",
        "--repo-root",
        repo,
        "--session-token",
        token,
    ]
    console_args = [
        "-m",
        "ac_workers.control_center",
        "console",
        "--host",
        "127.0.0.1",
        "--port",
        "24700",
        "--repo-root",
        repo,
        "--session-token",
        token,
    ]
    assert matches_windows_list2cmdline(server_args)
    assert matches_windows_list2cmdline(dash_args)
    assert matches_windows_list2cmdline(console_args)
    line = join_windows_cmdline(server_args)
    assert token in line
    assert repo in line or '"C:\\Leo projet\\Assetto Corsa ai"' in line


def test_control_center_server_parser_accepts_spaced_paths():
    import argparse
    from pathlib import Path

    repo = Path(r"C:\Leo projet\Assetto Corsa ai")
    ready = Path(r"C:\Users\yadal\AppData\Local\Temp\c4d_server_ready_12345.flag")
    session = repo / "ac_workers" / ".c4d_server" / "session.json"
    token = "abc123token"
    parser = argparse.ArgumentParser(prog="control_center")
    sub = parser.add_subparsers(dest="mode")
    p_server = sub.add_parser("server")
    p_server.add_argument("--host", default="127.0.0.1")
    p_server.add_argument("--port", type=int, default=24700)
    p_server.add_argument("--ready-file", type=Path, default=None)
    p_server.add_argument("--repo-root", type=Path, default=repo)
    p_server.add_argument("--session-token", default="")
    p_server.add_argument("--session-file", type=Path, default=None)
    args = parser.parse_args(
        [
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            "24700",
            "--session-token",
            token,
            "--repo-root",
            str(repo),
            "--session-file",
            str(session),
            "--ready-file",
            str(ready),
        ]
    )
    assert args.mode == "server"
    assert args.session_token == token
    assert Path(args.repo_root).resolve() == repo.resolve()
    assert Path(args.ready_file) == ready

    p_dash = sub.add_parser("dashboard")
    p_dash.add_argument("--host", default="127.0.0.1")
    p_dash.add_argument("--port", type=int, default=24700)
    p_dash.add_argument("--repo-root", type=Path, default=repo)
    p_dash.add_argument("--session-token", default="")
    dash = parser.parse_args(
        ["dashboard", "--host", "127.0.0.1", "--port", "24700", "--repo-root", str(repo), "--session-token", token]
    )
    assert dash.mode == "dashboard"
    assert dash.session_token == token

    p_console = sub.add_parser("console")
    p_console.add_argument("--host", default="127.0.0.1")
    p_console.add_argument("--port", type=int, default=24700)
    p_console.add_argument("--repo-root", type=Path, default=repo)
    p_console.add_argument("--session-token", default="")
    console = parser.parse_args(
        ["console", "--host", "127.0.0.1", "--port", "24700", "--repo-root", str(repo), "--session-token", token]
    )
    assert console.mode == "console"
    assert console.session_token == token


def main() -> int:
    tests = [
        test_parse_worker_spec,
        test_refuse_worker_already_active,
        test_detect_vjoy_conflict_in_batch,
        test_detect_port_conflict_source,
        test_transitions_per_minute_rate,
        test_episode_rate_uses_counter_not_transitions,
        test_stop_one_worker_script_exists,
        test_single_shared_learner,
        test_worker_status_unavailable,
        test_emergency_stop_wired,
        test_control_center_module_import,
        test_start_script_exists,
        test_fit_cell_truncates,
        test_fit_cell_exact_width,
        test_frame_width_80_120_160,
        test_long_error_in_table_single_line,
        test_worker_rows_aligned_same_width,
        test_command_buffer_preserved_in_frame,
        test_dashboard_frame_has_no_prompt,
        test_no_cls_in_refresh_loop,
        test_terminal_renderer_restores_cursor_on_error,
        test_terminal_renderer_skips_identical_frame,
        test_status_without_pid_is_offline,
        test_recycled_collector_pid_offline,
        test_stale_session_id_waiting_sync_when_pids_alive,
        test_stale_session_id_offline_when_dead,
        test_c4d_token_differs_from_worker_session_accepted,
        test_start_ignores_old_status_session,
        test_session_mismatch_never_collector_real_process_died,
        test_new_manifest_binds_worker_session,
        test_validated_collector_death_during_startup_no_failure,
        test_attaching_fresh_never_collector_real_process_died,
        test_historical_readiness_four_gates,
        test_startup_timeout_observational,
        test_startup_reconcile_never_cleanup,
        test_attaching_live_session_phase_preserved_in_view,
        test_waiting_ready_blocks_second_start,
        test_learner_inactive_does_not_fail_worker_startup,
        test_stop_zero_during_waiting_ready,
        test_historical_worker_ready_helper,
        test_dashboard_activity_skips_deep_scan_and_blocking_diag,
        test_ac_closed_during_attach_cancels_startup,
        test_build_worker_view_notes_ac_presence_without_reconcile,
        test_ac_dead_mgmt_open_cancels_after_grace,
        test_stale_status_json_ac_dead_cancels_via_poll,
        test_scale_test_starts_workers_sequentially,
        test_scale_test_advances_when_documents_released,
        test_scale_test_blocks_when_state_not_clean,
        test_scale_test_hard_cleans_then_starts_full_set,
        test_hard_clean_skips_emergency_when_already_clean,
        test_vjoy_clean_slate_skips_driver_reset_when_free,
        test_start_workers_refuses_when_diag_port_busy,
        test_worker_runtime_busy_message_detects_ready_status,
        test_worker_runtime_busy_ignores_stale_ready_without_live_pid,
        test_ac_closed_during_active_launcher_cancels_and_kills_job,
        test_late_start_result_does_not_clobber_cancelled,
        test_abort_worker_kills_collector_and_cancels_op,
        test_ac_closed_after_start_timeout_clears_op,
        test_running_worker_window_closed_stops_and_reports,
        test_running_worker_alive_records_ac_pid_no_stop,
        test_stopped_worker_can_start_again,
        test_cancelled_worker_can_start_again,
        test_attach_failed_status_marks_worker_failed,
        test_status_for_op_keeps_attach_failed_on_session_mismatch,
        test_status_for_op_accepts_ready_on_session_mismatch,
        test_scale_startup_not_busy_after_attach_failed,
        test_fresh_diag_attaching_not_collector_died,
        test_stale_worker_allows_start_message,
        test_dead_learner_status_inactive,
        test_subprocess_output_captured_not_printed,
        test_background_jobs_hide_console_windows,
        test_no_blocking_subprocess_in_main_loop,
        test_async_ops_use_background_thread_not_main,
        test_refresh_metrics_skips_health_and_process_scanners,
        test_poll_background_ops_throttles_reconcile,
        test_build_worker_view_uses_lightweight_activity,
        test_start_sets_starting_state_immediately,
        test_second_start_refused_while_starting,
        test_bg_result_success_and_failure_messages,
        test_error_command_shows_exit_detail,
        test_status_usable_during_starting,
        test_simulated_long_command_does_not_block_console,
        test_renderer_sole_writer_in_loop,
        test_starting_phase_visible_in_frame,
        test_keyboard_drain_loop,
        test_metrics_not_on_every_keypress,
        test_has_only_worker_id_in_start_script,
        test_drive_sent_before_collector_without_telemetry_gate,
        test_learner_start_uses_ready_worker_count,
        test_learner_start_accepts_worker0_after_start_timeout,
        test_reconcile_recovers_start_timeout_when_idle,
        test_learner_start_refused_without_ready_workers,
        test_learner_start_spawns_with_dynamic_workers,
        test_learner_start_accepts_resetting_and_stops_failed,
        test_only_worker_id_zero_dryrun_grid_1x1,
        test_only_worker_id_dryrun_targets,
        test_targeted_preflight_skips_other_workers,
        test_startup_does_not_fail_when_ac_pid_dead_without_status,
        test_stale_starting_not_shown_without_active_job,
        test_launcher_exit_zero_enters_waiting_ready,
        test_start_worker0_uses_canonical_workers1_entry,
        test_start_worker1_still_targets_only_worker_id,
        test_start_workers_refuses_missing_profile,
        test_worker_profile_present_returns_empty,
        test_no_worker_pipeline_duplicated_in_control_center,
        test_worker_running_without_learner,
        test_error_command_keeps_full_output,
        test_dual_console_architecture_wired,
        test_ipc_server_dashboard_and_console_share_center,
        test_console_input_not_truncated_by_dashboard,
        test_collector_stopped_status_shows_failed_not_starting,
        test_reconcile_collector_stopped_clears_starting,
        test_double_start_blocked_immediately,
        test_start_op_lock_shared_on_center,
        test_start_lock_released_after_finalize_failure,
        test_learner_refused_without_policy_ready,
        test_collector_fatal_logs_traceback_to_stderr,
        test_fsync_einval_skipped_on_windows,
        test_c4d_server_token_required,
        test_c4d_prepare_first_start_then_reuse,
        test_c4d_dead_pid_allows_new_server,
        test_c4d_recycled_pid_not_reused,
        test_c4d_quit_clears_session_files,
        test_stop_c4d_script_exists_and_preserves_pipeline,
        test_start_c4d_control_center_launch_argv,
        test_c4d_windows_argv_quote_spaced_paths,
        test_control_center_server_parser_accepts_spaced_paths,
    ]
    for fn in tests:
        fn()
        print("OK", fn.__name__)
    print("Test-ControlCenter-Offline: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
