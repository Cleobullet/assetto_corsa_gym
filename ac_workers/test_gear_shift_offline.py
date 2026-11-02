"""Offline tests for N->first gear shift and collector startup order."""
from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))

from AssettoCorsaEnv.gear_shift import (
    GEAR_FIRST,
    GEAR_NEUTRAL,
    ac_button_to_vjoy,
    auto_shift_neutral_to_first,
    is_first,
    is_neutral,
    pulse_shift_up,
    resolve_shift_up_mapping,
)
from ac_workers.collector_diag import CollectorDiagClient, CollectorDiagServer


def _valid_mapping(mask: int = 1, raw: int = 0) -> dict:
    return {
        "valid": True,
        "vjoy_device_id": 1,
        "vjoy_button_number": raw + 1,
        "vjoy_button_mask": mask,
        "configured_button_raw": raw,
        "active_preset_path": "Vjoy_W0.ini",
        "controls_ini_path": "controls.ini",
        "error": None,
    }


class _FakeLocalControls:
    def __init__(self):
        self.onButtons = 0
        self.calls: list = []

    def set_controls(self, **kwargs):
        self.calls.append(dict(kwargs))

    def update(self):
        pass


class _FakeControls:
    def __init__(self):
        self.local_controls = _FakeLocalControls()
        self.calls: list = []

    def set_controls(self, **kwargs):
        self.calls.append(dict(kwargs))

    def respond_to_server(self):
        self.calls.append({"respond": True})


class _FakeClient:
    def __init__(self):
        self.controls = _FakeControls()
        self._gear = GEAR_NEUTRAL
        self._step = 0

    def respond_to_server(self):
        self.controls.respond_to_server()

    def step_sim(self):
        self._step += 1
        if self._step >= 2:
            self._gear = GEAR_FIRST
        return {"actualGear": self._gear}


class _FakeEnv:
    def __init__(self, gear=GEAR_NEUTRAL):
        self.client = _FakeClient()
        self.client._gear = gear
        self.states = [{"actualGear": gear}]
        self._gear_shift_mapping = _valid_mapping()


def test_gear_one_is_neutral_not_first():
    assert is_neutral(1)
    assert not is_first(1)


def test_ac_button_index_conversion():
    mask, num = ac_button_to_vjoy(0)
    assert mask == 0x00000001
    assert num == 1
    mask2, num2 = ac_button_to_vjoy(1)
    assert mask2 == 0x00000002
    assert num2 == 2


def test_no_hardcoded_shift_button_constant():
    src = (REPO / "assetto_corsa_gym" / "AssettoCorsaEnv" / "gear_shift.py").read_text(encoding="utf-8")
    assert "SHIFT_BUTTON_NUMBER = 1" not in src
    assert "SHIFT_BUTTON_MASK = 0x00000001" not in src


def test_pulse_shift_uses_resolved_mask():
    client = _FakeClient()
    mapping = _valid_mapping(mask=0x00000002, raw=1)
    lc = client.controls.local_controls
    seen: list[int] = []
    orig = lc.update

    def track():
        seen.append(lc.onButtons)
        orig()

    lc.update = track
    pulse_shift_up(client, mapping)
    assert 0x00000002 in seen
    assert lc.onButtons == 0


def test_pulse_shift_releases_button():
    client = _FakeClient()
    pulse_shift_up(client, _valid_mapping())
    assert client.controls.calls[0]["shift_up"] is False
    assert client.controls.local_controls.onButtons == 0


def test_auto_shift_success_simulated():
    env = _FakeEnv(GEAR_NEUTRAL)
    ok, report = auto_shift_neutral_to_first(env)
    assert ok
    assert report.get("gear_raw_after") == GEAR_FIRST


def test_auto_shift_bounded_failure():
    env = _FakeEnv(GEAR_NEUTRAL)
    env.client.step_sim = mock.Mock(return_value={"actualGear": GEAR_NEUTRAL})
    clock = {"t": 0.0}

    def fake_time():
        clock["t"] += 0.05
        return clock["t"]

    with mock.patch("AssettoCorsaEnv.gear_shift.time") as mtime:
        mtime.time.side_effect = fake_time
        mtime.sleep = mock.Mock()
        ok, report = auto_shift_neutral_to_first(env)
    assert not ok
    assert report.get("error")


def test_mapping_invalid_no_impulse():
    env = _FakeEnv(GEAR_NEUTRAL)
    env._gear_shift_mapping = {"valid": False, "error": "controls.ini GEARUP not on vJoy"}
    ok, report = auto_shift_neutral_to_first(env)
    assert not ok
    assert "vJoy" in report.get("error", "")
    assert env.client._step == 0


def test_auto_shifter_skips_manual_initialization():
    env = _FakeEnv(GEAR_NEUTRAL)
    env._gear_shift_mapping = {**_valid_mapping(), "auto_shifter": True, "auto_clutch": True}
    with mock.patch("AssettoCorsaEnv.gear_shift.pulse_shift_up") as pulse_mock:
        ok, report = auto_shift_neutral_to_first(env)
    assert ok
    pulse_mock.assert_not_called()
    assert report.get("manual_skipped") is True
    assert report.get("error") is None


def test_auto_shifter_succeeds_with_gear_raw_1():
    env = _FakeEnv(GEAR_NEUTRAL)
    env.states = [{"actualGear": 1}]
    env._gear_shift_mapping = {**_valid_mapping(), "auto_shifter": True}
    with mock.patch("AssettoCorsaEnv.gear_shift.pulse_shift_up") as pulse_mock:
        ok, report = auto_shift_neutral_to_first(env)
    assert ok
    pulse_mock.assert_not_called()
    assert report.get("gear_raw_before") == 1


def test_auto_shifter_read_from_active_assists_ini():
    with tempfile.TemporaryDirectory() as tmp:
        assists = Path(tmp) / "assists.ini"
        assists.write_text("[ASSISTS]\nAUTO_CLUTCH=1\nAUTO_SHIFTER=1\n", encoding="utf-8")
        env = _FakeEnv(GEAR_NEUTRAL)
        env._gear_shift_mapping = {
            **_valid_mapping(),
            "auto_shifter": False,
            "assists_ini_path": str(assists),
        }
        with mock.patch("AssettoCorsaEnv.gear_shift.pulse_shift_up") as pulse_mock:
            ok, report = auto_shift_neutral_to_first(env)
        assert ok
        pulse_mock.assert_not_called()
        assert report["auto_shifter"] is True


def test_manual_mode_still_three_attempts_max():
    env = _FakeEnv(GEAR_NEUTRAL)
    env._gear_shift_mapping = {**_valid_mapping(), "auto_shifter": False}
    env.client.step_sim = mock.Mock(return_value={"actualGear": GEAR_NEUTRAL})
    clock = {"t": 0.0}

    def fake_time():
        clock["t"] += 0.05
        return clock["t"]

    with mock.patch("AssettoCorsaEnv.gear_shift.time") as mtime:
        mtime.time.side_effect = fake_time
        mtime.sleep = mock.Mock()
        ok, report = auto_shift_neutral_to_first(env)
    assert not ok
    assert len(report.get("attempts", [])) == 3


def test_resolve_mapping_from_simulated_ini():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        preset_dir = root / "cfg" / "controllers" / "savedsetups"
        preset_dir.mkdir(parents=True)
        (root / "cfg" / "assists.ini").write_text(
            "[ASSISTS]\nAUTO_CLUTCH=1\nAUTO_SHIFTER=0\n",
            encoding="utf-8",
        )
        (root / "cfg" / "controls.ini").write_text(
            "[CONTROLLERS]\nCON0=vJoy Device\n"
            "[GEARUP]\nJOY=0\nBUTTON=0\n",
            encoding="utf-8",
        )
        (preset_dir / "Vjoy_W0.ini").write_text(
            "[CONTROLLERS]\nCON0=vJoy Device\n"
            "[GEARUP]\nJOY=0\nBUTTON=0\n"
            "[SHIFTER]\nACTIVE=0\n",
            encoding="utf-8",
        )
        with _with_detected_mapping({1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}):
            with mock.patch(
                "AssettoCorsaEnv.gear_shift.ac_documents_candidates", return_value=[root]
            ):
                mapping = resolve_shift_up_mapping(0)
        assert mapping["valid"]
        assert mapping["configured_button_raw"] == 0
        assert mapping["vjoy_button_number"] == 1
        assert mapping["vjoy_button_mask"] == 0x00000001
        assert mapping["auto_clutch"] is True
        assert mapping["auto_shifter"] is False


def test_resolve_mapping_invalid_when_controls_unbound():
    backup = REPO / "ac_profiles" / "backups" / "multi_20260623_013318"
    if not backup.is_dir():
        return
    with mock.patch(
        "AssettoCorsaEnv.gear_shift.ac_documents_candidates",
        return_value=[backup],
    ):
        mapping = resolve_shift_up_mapping(0)
    assert not mapping["valid"]
    assert "GEARUP not on vJoy" in mapping["error"]


def test_patch_controls_vjoy_joy_index_uses_detected_index():
    """controls.ini must be patched to the DETECTED joystick index, not worker_id."""
    import configparser
    import tempfile

    from AssettoCorsaEnv.gear_shift import patch_controls_vjoy_joy_index

    with tempfile.TemporaryDirectory() as tmp:
        controls = Path(tmp) / "controls.ini"
        controls.write_text(
            "[CONTROLLERS]\nCON0=vJoy Device\nPGUID0=4C035200-7051-11EB-8002-444553540000\n"
            "[STEER]\nJOY=0\nAXLE=0\nSCALE=1\nLOCK=900\n"
            "[THROTTLE]\nJOY=0\nAXLE=1\n"
            "[GEARUP]\nJOY=0\nBUTTON=0\n",
            encoding="utf-8",
        )
        # worker 2 writes to device 3; suppose detection found device 3 -> index 4.
        patch_controls_vjoy_joy_index(controls, 2, joy_index=4)
        parser = configparser.ConfigParser()
        parser.optionxform = str
        parser.read_string(controls.read_text(encoding="utf-8"))
        assert parser.get("GEARUP", "JOY").strip() == "4"
        assert parser.get("THROTTLE", "JOY").strip() == "4"
        assert parser.get("STEER", "JOY").strip() == "4"
        assert parser.get("STEER", "MIN").strip() == "-1"
        assert parser.get("STEER", "MAX").strip() == "1"
        assert parser.get("CONTROLLERS", "CON4").strip() == "vJoy Device"
        assert parser.get("CONTROLLERS", "CON0").strip() == "vJoy Device"


def _with_detected_mapping(device_to_index: dict[int, int]):
    """Context manager: point gear_shift at a temp detected-mapping file."""
    import contextlib
    import json
    import tempfile

    import AssettoCorsaEnv.gear_shift as gs

    @contextlib.contextmanager
    def _ctx():
        old = gs.VJOY_MAPPING_PATH
        td = tempfile.mkdtemp()
        path = Path(td) / "vjoy_mapping_latest.json"
        path.write_text(
            json.dumps({"device_to_index": {str(k): v for k, v in device_to_index.items()}}),
            encoding="utf-8",
        )
        gs.VJOY_MAPPING_PATH = path
        try:
            yield
        finally:
            gs.VJOY_MAPPING_PATH = old

    return _ctx()


def test_patch_controls_vjoy_joy_index_guard_rail_without_mapping():
    """No detected mapping => hard error, never a silent JOY=worker_id assumption."""
    import tempfile

    import AssettoCorsaEnv.gear_shift as gs

    with tempfile.TemporaryDirectory() as tmp:
        controls = Path(tmp) / "controls.ini"
        controls.write_text(
            "[CONTROLLERS]\nCON0=vJoy Device\nPGUID0=X\n[GEARUP]\nJOY=0\nBUTTON=0\n",
            encoding="utf-8",
        )
        old = gs.VJOY_MAPPING_PATH
        gs.VJOY_MAPPING_PATH = Path(tmp) / "missing.json"
        try:
            gs.patch_controls_vjoy_joy_index(controls, 1)
            assert False, "expected VjoyIndexMappingError"
        except gs.VjoyIndexMappingError:
            pass
        finally:
            gs.VJOY_MAPPING_PATH = old


def test_prepare_worker_profile_controls_sync():
    import configparser
    import shutil
    import tempfile

    from AssettoCorsaEnv.gear_shift import prepare_worker_documents_controls

    profile = REPO / "ac_profiles" / "workers" / "worker_0"
    if not profile.is_dir():
        return
    with tempfile.TemporaryDirectory() as tmp:
        docs = Path(tmp)
        shutil.copytree(profile / "cfg", docs / "cfg")
        keyboard_gas = None
        for line in (docs / "cfg" / "controls.ini").read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("GAS="):
                keyboard_gas = line.strip()
                break
        assert keyboard_gas is not None

        with _with_detected_mapping({1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}):
            report = prepare_worker_documents_controls(0, docs)
        assert report["valid"], report.get("error")
        assert report["controls_gearup_joy"] == 0
        assert report["controls_gearup_button_raw"] == 0
        assert report["configured_button_raw"] == 0
        assert report["vjoy_button_number"] == 1
        assert report["gearbox_mode"] == "sequential"
        # AI workers rely on AC's auto gearbox for in-race shifts (policy has no
        # gear action), so sync forces AUTO_SHIFTER on regardless of gearbox mode.
        assert report["auto_shifter"] is True
        assert report["auto_clutch"] is True
        assert "vjoy" in str(report["controls_con0"]).lower()

        controls_text = (docs / "cfg" / "controls.ini").read_text(encoding="utf-8")
        parser = configparser.ConfigParser()
        parser.optionxform = str
        parser.read_string(controls_text)
        assert parser.get("GEARUP", "JOY").strip().split(";")[0].strip() == "0"
        assert parser.get("GEARUP", "BUTTON").strip().split(";")[0].strip() == "0"
        assert parser.get("CONTROLLERS", "CON0").strip() == "vJoy Device"
        assert keyboard_gas in controls_text

        assists = configparser.ConfigParser()
        assists.optionxform = str
        assists.read_string((docs / "cfg" / "assists.ini").read_text(encoding="utf-8"))
        assert assists.get("ASSISTS", "AUTO_SHIFTER") == "1"


def test_worker0_profile_resolves_valid_after_sync():
    from AssettoCorsaEnv.gear_shift import prepare_worker_documents_controls

    profile = REPO / "ac_profiles" / "workers" / "worker_0"
    if not profile.is_dir():
        return
    with _with_detected_mapping({1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}):
        report = prepare_worker_documents_controls(0, profile)
    assert report["valid"], report.get("error")
    assert report["controls_gearup_joy"] == 0
    assert report["controls_gearup_button_raw"] == 0


def test_collector_no_heavy_import_at_module_level():
    src = (REPO / "ac_workers" / "worker_collector.py").read_text(encoding="utf-8")
    assert "import AssettoCorsaEnv.assettoCorsa" not in src.split("def run_collector")[0]
    assert "CollectorPolicy" not in src.split("def run_collector")[0]
    assert "collector_safety" not in src.split("def run_collector")[0]


def test_collector_startup_markers_before_env():
    src = (REPO / "ac_workers" / "worker_collector.py").read_text(encoding="utf-8")
    assert "collector startup: initial status written" in src
    assert "collector startup: diagnostic listener confirmed" in src
    assert "collector startup: heartbeat started" in src
    assert "collector startup: creating environment" in src
    assert "collector startup: attaching to live session" in src
    assert "attach_live_session" in src
    assert src.index("diagnostic listener confirmed") < src.index("import AssettoCorsaEnv.assettoCorsa")
    assert src.index("diag.start()") < src.index("import AssettoCorsaEnv.assettoCorsa")


def test_diag_available_before_blocking_reset():
    from ac_workers.worker_collector import CollectorRuntime

    worker_id = None
    for wid in range(0, 6):
        port = 24100 + wid
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
            probe.close()
            worker_id = wid
            break
        except OSError:
            probe.close()
    assert worker_id is not None, "no free diag port in 24100-24105"

    runtime = CollectorRuntime(worker_id, 1, 2347)
    runtime.set_phase("starting")
    runtime.write_minimal_status()
    reset_done = threading.Event()

    diag = CollectorDiagServer(
        worker_id,
        get_snapshot=runtime.get_diag_snapshot,
        on_safe_action_test=lambda: {"ok": False},
        on_neutralize=lambda: {"ok": True},
        lock=runtime.lock,
    )
    diag.start()
    time.sleep(0.15)
    assert diag.wait_until_listening(timeout_sec=3.0), diag.thread_error()

    def slow_reset():
        runtime.set_phase("resetting")
        runtime.write_minimal_status()
        time.sleep(0.5)
        runtime.set_phase("gear_initialization")
        runtime.write_minimal_status()
        reset_done.set()

    threading.Thread(target=slow_reset, daemon=True).start()
    time.sleep(0.05)
    client = CollectorDiagClient(worker_id, timeout=1.0)
    snap = client.request("status")
    assert snap.get("phase") in ("resetting", "gear_initialization", "starting")
    assert snap.get("transition_count", 0) == 0
    assert snap.get("observation_complete") is False
    assert float(snap.get("heartbeat_age_ms", 9999)) < 2000.0
    assert reset_done.wait(2.0)
    diag.stop()


def test_collector_heartbeat_thread():
    src = (REPO / "ac_workers" / "worker_collector.py").read_text(encoding="utf-8")
    assert "heartbeat_thread" in src
    assert "write_minimal_status" in src


def main() -> int:
    tests = [
        test_gear_one_is_neutral_not_first,
        test_ac_button_index_conversion,
        test_no_hardcoded_shift_button_constant,
        test_pulse_shift_uses_resolved_mask,
        test_pulse_shift_releases_button,
        test_auto_shift_success_simulated,
        test_auto_shift_bounded_failure,
        test_mapping_invalid_no_impulse,
        test_auto_shifter_skips_manual_initialization,
        test_auto_shifter_succeeds_with_gear_raw_1,
        test_auto_shifter_read_from_active_assists_ini,
        test_manual_mode_still_three_attempts_max,
        test_resolve_mapping_from_simulated_ini,
        test_resolve_mapping_invalid_when_controls_unbound,
        test_patch_controls_vjoy_joy_index_uses_detected_index,
        test_patch_controls_vjoy_joy_index_guard_rail_without_mapping,
        test_prepare_worker_profile_controls_sync,
        test_worker0_profile_resolves_valid_after_sync,
        test_collector_no_heavy_import_at_module_level,
        test_collector_startup_markers_before_env,
        test_diag_available_before_blocking_reset,
        test_collector_heartbeat_thread,
    ]
    for fn in tests:
        fn()
        print("OK", fn.__name__)
    print("Test-GearShift-Offline: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
