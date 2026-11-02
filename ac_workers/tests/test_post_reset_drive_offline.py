"""Offline tests for post-reset driving wait via management plugin."""
from __future__ import annotations

import time
from unittest import mock

REPO = __import__("pathlib").Path(__file__).resolve().parents[1]


class _FakeMgmt:
    def __init__(self, states: list[dict]) -> None:
        self._states = list(states)
        self.resume_calls = 0
        self.polls = 0

    def get_driving_state(self) -> dict:
        self.polls += 1
        if len(self._states) > 1:
            return dict(self._states[min(self.polls - 1, len(self._states) - 1)])
        return dict(self._states[0])

    def send_resume_drive(self) -> None:
        self.resume_calls += 1


def _drive_state(brake=0.0, **extra):
    """Build a plugin snapshot for the single truth source (sim_live + brake)."""
    base = {"driving": True, "sim_live": True, "physics_brake": brake, "reason": ""}
    base.update(extra)
    return base


def _menu_state(**extra):
    """Car parked in the Drive menu: live but brake pinned -> not driving."""
    base = {
        "driving": False,
        "sim_live": True,
        "physics_brake": 1.0,
        "reason": "session_menu_brake_hold",
    }
    base.update(extra)
    return base


def test_on_poll_observer_sees_every_plugin_state_and_never_breaks_wait():
    from ac_workers.post_reset_drive import wait_for_driving_after_reset

    mgmt = _FakeMgmt([_menu_state(), _menu_state(), _drive_state(brake=0.0)])
    seen: list[dict] = []

    def _obs(state):
        seen.append(state)
        raise RuntimeError("observer boom should be swallowed")

    ok, state = wait_for_driving_after_reset(mgmt, sleep_fn=lambda _s: None, on_poll=_obs)
    # The wait still succeeds despite the observer raising on every poll.
    assert ok is True
    assert state["driving"] is True
    # The observer saw the menu states (plugin alive) before the driving state.
    assert any(s.get("sim_live") and not s.get("driving") for s in seen)
    assert any(s.get("driving") for s in seen)


def test_paused_then_resume_confirmed():
    from ac_workers.post_reset_drive import wait_for_driving_after_reset

    mgmt = _FakeMgmt(
        [
            _menu_state(),
            _menu_state(),
            _drive_state(brake=0.0),
        ]
    )
    ok, state = wait_for_driving_after_reset(mgmt, sleep_fn=lambda _s: None)
    assert ok is True
    assert state["driving"] is True
    assert mgmt.resume_calls >= 1


def test_timeout_no_vjoy_commands_path():
    from ac_workers.post_reset_drive import wait_for_driving_after_reset

    mgmt = _FakeMgmt([_menu_state()])
    with mock.patch("ac_workers.post_reset_drive.MAX_WAIT_SEC", 0.6), mock.patch(
        "ac_workers.post_reset_drive.POLL_SEC", 0.1
    ):
        ok, state = wait_for_driving_after_reset(mgmt, sleep_fn=lambda _s: None)
    assert ok is False
    # Brake pinned -> single truth source reports we are still in the menu.
    assert "session_menu_brake_hold" in state.get("reason", "")


def test_telemetry_live_but_neutral_not_confirmed():
    from ac_workers.post_reset_drive import GEAR_NOT_ENGAGED_ERROR, wait_for_driving_after_reset

    mgmt = _FakeMgmt([_drive_state(brake=0.0)])
    with mock.patch("ac_workers.post_reset_drive.MAX_WAIT_SEC", 0.2), mock.patch(
        "ac_workers.post_reset_drive.POLL_SEC", 0.05
    ):
        ok, state = wait_for_driving_after_reset(
            mgmt,
            sleep_fn=lambda _s: None,
            get_gear_fn=lambda: 0,
        )
    assert ok is False
    assert GEAR_NOT_ENGAGED_ERROR in state.get("reason", "")


def test_neutral_allowed_when_live_attach_flag():
    from ac_workers.post_reset_drive import wait_for_driving_after_reset

    mgmt = _FakeMgmt([_drive_state(brake=0.0)])
    ok, state = wait_for_driving_after_reset(
        mgmt,
        sleep_fn=lambda _s: None,
        get_gear_fn=lambda: 1,
        require_non_neutral_gear=False,
    )
    assert ok is True
    assert state["gear_raw"] == 1


def test_brake_released_confirms_regardless_of_controls_live():
    """controls_live is no longer a truth signal: brake released = out of menu."""
    from ac_workers.post_reset_drive import wait_for_driving_after_reset

    mgmt = _FakeMgmt([_drive_state(brake=0.0, controls_live=False)])
    ok, state = wait_for_driving_after_reset(
        mgmt,
        sleep_fn=lambda _s: None,
        get_gear_fn=lambda: 1,
        require_non_neutral_gear=False,
    )
    assert ok is True
    assert state["gear_raw"] == 1


def test_menu_brake_hold_blocks_confirmation():
    """Brake pinned (menu) must never confirm drive, even if 'driving' flag is stale."""
    from ac_workers.post_reset_drive import wait_for_driving_after_reset

    mgmt = _FakeMgmt([{"driving": True, "sim_live": True, "physics_brake": 1.0}])
    with mock.patch("ac_workers.post_reset_drive.MAX_WAIT_SEC", 0.2):
        ok, state = wait_for_driving_after_reset(
            mgmt, sleep_fn=lambda _s: None, get_gear_fn=lambda: 2
        )
    assert ok is False
    assert "session_menu_brake_hold" in state.get("reason", "")


def test_vjoy_readback_required_for_confirmation():
    """If the collector reports vJoy readback NOT ok, drive is not confirmed."""
    from ac_workers.post_reset_drive import wait_for_driving_after_reset

    mgmt = _FakeMgmt([_drive_state(brake=0.0)])
    with mock.patch("ac_workers.post_reset_drive.MAX_WAIT_SEC", 0.2):
        ok, state = wait_for_driving_after_reset(
            mgmt,
            sleep_fn=lambda _s: None,
            get_gear_fn=lambda: 2,
            vjoy_readback_fn=lambda: False,
        )
    assert ok is False
    assert "vjoy_input_not_acknowledged" in state.get("reason", "")


def test_already_driving_no_extra_resume_spam():
    from ac_workers.post_reset_drive import wait_for_driving_after_reset

    mgmt = _FakeMgmt([_drive_state(brake=0.0)])
    ok, _state = wait_for_driving_after_reset(mgmt, sleep_fn=lambda _s: None)
    assert ok is True
    assert mgmt.resume_calls == 0


def test_no_windows_focus_dependency():
    src = (REPO / "ac_workers" / "post_reset_drive.py").read_text(encoding="utf-8")
    for token in ("SendKeys", "Focus-Window", "SetForegroundWindow", "GetForegroundWindow"):
        assert token not in src


def test_simulation_management_api_present():
    src = (REPO / "assetto_corsa_gym" / "AssettoCorsaEnv" / "ac_client.py").read_text(encoding="utf-8")
    assert "def get_driving_state" in src
    assert "def send_resume_drive" in src
    assert "get_driving_state" in src
    assert "resume_drive" in src


def test_plugin_management_commands_present():
    src = (
        REPO / "assetto_corsa_gym" / "AssettoCorsaPlugin" / "plugins" / "sensors_par" / "sensors_par.py"
    ).read_text(encoding="utf-8")
    assert "get_driving_state" in src
    assert "resume_drive" in src
    assert "tick_drive_state" in src


def test_get_driving_state_json_uses_cached_state_without_controls():
    import json
    import sys
    import types
    from unittest import mock

    gym = REPO / "assetto_corsa_gym"
    plugin_dir = gym / "AssettoCorsaPlugin" / "plugins" / "sensors_par"
    if str(plugin_dir) not in sys.path:
        sys.path.insert(0, str(plugin_dir))
    sim_info = types.ModuleType("sim_info")
    sim_info.AC_LIVE = 2
    sim_info.AC_OFF = 0
    sim_info.AC_PAUSE = 1
    sim_info.AC_REPLAY = 3
    sim_info.info = mock.Mock()
    sim_info.info.graphics = mock.Mock(iCurrentTime=0)
    sim_info.info.physics = mock.Mock(gas=0.0, brake=0.0)
    with mock.patch.dict(sys.modules, {"sim_info": sim_info}):
        import drive_state  # noqa: WPS433

        with drive_state._lock:
            drive_state._cached_state.clear()
            drive_state._cached_state.update(
                {
                    "driving": True,
                    "ac_status": 2,
                    "session_clock_ms": 1000,
                    "session_clock_advancing": True,
                    "controls_live": True,
                    "pending_resume_after_reset": False,
                    "resume_attempts": 0,
                    "reason": "",
                }
            )

        class _BrokenControls:
            def set_controls(self, *args, **kwargs):
                raise RuntimeError("controls must not be touched from mgmt thread")

        payload = json.loads(drive_state.get_driving_state_json(_BrokenControls()))
    assert payload["driving"] is True
    assert payload["controls_live"] is True


def test_driving_physical_truth_when_feeder_disabled():
    """LIVE + brake released = driving; clock is diagnostic only (not truth)."""
    import sys
    import types
    from unittest import mock

    gym = REPO / "assetto_corsa_gym"
    plugin_dir = gym / "AssettoCorsaPlugin" / "plugins" / "sensors_par"
    if str(plugin_dir) not in sys.path:
        sys.path.insert(0, str(plugin_dir))
    sim_info = types.ModuleType("sim_info")
    sim_info.AC_LIVE = 2
    sim_info.AC_OFF = 0
    sim_info.AC_PAUSE = 1
    sim_info.AC_REPLAY = 3
    sim_info.info = mock.Mock()
    sim_info.info.graphics = mock.Mock(iCurrentTime=1000, status=2)
    sim_info.info.physics = mock.Mock(gas=0.0, brake=0.0)
    with mock.patch.dict(sys.modules, {"sim_info": sim_info}):
        import drive_state  # noqa: WPS433

        drive_state.info.graphics.iCurrentTime = 1000
        drive_state.info.graphics.status = drive_state.AC_LIVE
        drive_state.info.physics.brake = 0.0
        drive_state._last_session_clock_ms = None
        drive_state._last_session_clock_wall = 0.0
        drive_state._pending_resume_after_reset = False

        class _Ctrl:
            def feeder_enabled(self):
                return False

            def set_controls(self, *args, **kwargs):
                raise AssertionError("plugin must not feed vJoy when feeder disabled")

        ctrl = _Ctrl()
        # Lap clock frozen but brake released -> driving (physical truth).
        snap = drive_state.evaluate_driving_state(controls=ctrl, now=100.0)
        assert snap["controls_live"] is None
        assert snap["session_clock_advancing"] is False
        assert snap["driving"] is True
        # Clock advancing is still reported for diagnostics.
        drive_state.info.graphics.iCurrentTime = 5000
        snap = drive_state.evaluate_driving_state(controls=ctrl, now=101.0)
        assert snap["session_clock_advancing"] is True
        assert snap["driving"] is True


def test_pending_resume_driving_without_clock_advance():
    """After ext_resetCar, driving must not wait for session_clock_advancing."""
    import sys
    import types
    from unittest import mock

    gym = REPO / "assetto_corsa_gym"
    plugin_dir = gym / "AssettoCorsaPlugin" / "plugins" / "sensors_par"
    if str(plugin_dir) not in sys.path:
        sys.path.insert(0, str(plugin_dir))
    sim_info = types.ModuleType("sim_info")
    sim_info.AC_LIVE = 2
    sim_info.AC_OFF = 0
    sim_info.AC_PAUSE = 1
    sim_info.AC_REPLAY = 3
    sim_info.info = mock.Mock()
    sim_info.info.graphics = mock.Mock(iCurrentTime=1000, status=2)
    sim_info.info.physics = mock.Mock(brake=0.0)
    with mock.patch.dict(sys.modules, {"sim_info": sim_info}):
        import drive_state  # noqa: WPS433

        drive_state._pending_resume_after_reset = True
        drive_state._last_session_clock_ms = 1000
        drive_state._last_session_clock_wall = 100.0
        snap = drive_state.evaluate_driving_state(controls=None, now=100.1)
        assert snap["session_clock_advancing"] is False
        assert snap["driving"] is True


def test_menu_brake_hold_blocks_driving_even_when_clock_advances():
    """HOTLAP menu: lap clock ticks but the car is parked brake-pinned -> not driving."""
    import sys
    import types
    from unittest import mock

    gym = REPO / "assetto_corsa_gym"
    plugin_dir = gym / "AssettoCorsaPlugin" / "plugins" / "sensors_par"
    if str(plugin_dir) not in sys.path:
        sys.path.insert(0, str(plugin_dir))
    sim_info = types.ModuleType("sim_info")
    sim_info.AC_LIVE = 2
    sim_info.AC_OFF = 0
    sim_info.AC_PAUSE = 1
    sim_info.AC_REPLAY = 3
    sim_info.info = mock.Mock()
    sim_info.info.graphics = mock.Mock(iCurrentTime=1000, status=2)
    sim_info.info.physics = mock.Mock(gas=0.0, brake=1.0)
    with mock.patch.dict(sys.modules, {"sim_info": sim_info}):
        import drive_state  # noqa: WPS433

        drive_state.info.graphics.status = drive_state.AC_LIVE
        drive_state.info.physics.brake = 1.0
        drive_state._last_session_clock_ms = None
        drive_state._last_session_clock_wall = 0.0
        drive_state._pending_resume_after_reset = False

        class _Ctrl:
            def feeder_enabled(self):
                return False

            def set_controls(self, *args, **kwargs):
                raise AssertionError("plugin must not feed vJoy when feeder disabled")

        ctrl = _Ctrl()
        # Lap clock is advancing in the menu, but brake is pinned -> still in the menu.
        drive_state.evaluate_driving_state(controls=ctrl, now=100.0)
        drive_state.info.graphics.iCurrentTime = 9000
        snap = drive_state.evaluate_driving_state(controls=ctrl, now=101.0)
        assert snap["session_clock_advancing"] is True
        assert snap["menu_brake_hold"] is True
        assert snap["driving"] is False
        assert "session_menu_brake_hold" in snap["reason"]
        # Brake releases (Drive clicked) -> now driving.
        drive_state.info.physics.brake = 0.0
        drive_state.info.graphics.iCurrentTime = 13000
        snap = drive_state.evaluate_driving_state(controls=ctrl, now=102.0)
        assert snap["menu_brake_hold"] is False
        assert snap["driving"] is True


def test_get_driving_state_json_source_avoids_controls_on_mgmt_thread():
    src = (
        REPO
        / "assetto_corsa_gym"
        / "AssettoCorsaPlugin"
        / "plugins"
        / "sensors_par"
        / "drive_state.py"
    ).read_text(encoding="utf-8")
    fn = src.split("def get_driving_state_json")[1].split("def _find_process_main_window")[0]
    assert "evaluate_driving_state(controls=None)" in fn
    assert "evaluate_driving_state(controls=controls)" not in fn


def test_drive_state_compatible_with_python33():
    src = (
        REPO
        / "assetto_corsa_gym"
        / "AssettoCorsaPlugin"
        / "plugins"
        / "sensors_par"
        / "drive_state.py"
    ).read_text(encoding="utf-8")
    import re
    annotations = re.findall(r'^\s+\w+\s*:\s*(list|dict|set|tuple|List|Dict|Set|Tuple|int|str|float|bool|Optional)\b', src, re.MULTILINE)
    assert not annotations, "Python 3.3 incompatible type annotations found: {0}".format(annotations)
    assert "-> bool" not in src, "Return type annotations not supported in Python 3.3"
    assert "-> int" not in src, "Return type annotations not supported in Python 3.3"


def test_collector_waits_before_smoke():
    src = (REPO / "ac_workers" / "worker_collector.py").read_text(encoding="utf-8")
    smoke_idx = src.index("run_control_smoke_test")
    attach_idx = src.index("attach_live_session")
    assert attach_idx < smoke_idx
    assert "run_episode_reset" in src
    assert "reset_failed" in src


def main() -> int:
    tests = [
        test_paused_then_resume_confirmed,
        test_timeout_no_vjoy_commands_path,
        test_telemetry_live_but_neutral_not_confirmed,
        test_neutral_allowed_when_live_attach_flag,
        test_brake_released_confirms_regardless_of_controls_live,
        test_menu_brake_hold_blocks_confirmation,
        test_vjoy_readback_required_for_confirmation,
        test_already_driving_no_extra_resume_spam,
        test_no_windows_focus_dependency,
        test_simulation_management_api_present,
        test_plugin_management_commands_present,
        test_get_driving_state_json_uses_cached_state_without_controls,
        test_driving_physical_truth_when_feeder_disabled,
        test_pending_resume_driving_without_clock_advance,
        test_get_driving_state_json_source_avoids_controls_on_mgmt_thread,
        test_drive_state_compatible_with_python33,
        test_collector_waits_before_smoke,
    ]
    for fn in tests:
        fn()
        print("OK", fn.__name__)
    print("Test-PostResetDrive-Offline: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
