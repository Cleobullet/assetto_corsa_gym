"""Offline tests for ac_workers.direct_vjoy_smoke."""
from __future__ import annotations

import os
import struct
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]


class _FakeVJoy:
    def __init__(
        self,
        *,
        fail_update: bool = False,
        corrupt_axes: bool = False,
        fail_button_press: bool = False,
    ) -> None:
        self.reference = 1
        self.acquired = True
        self._fail_update = fail_update
        self._corrupt_axes = corrupt_axes
        self._fail_button_press = fail_button_press
        self._update_count = 0

    def open(self) -> bool:
        self.acquired = True
        return True

    def close(self) -> bool:
        self.acquired = False
        return True

    def generateJoystickPosition(
        self,
        wAxisX=0,
        wAxisY=0,
        wAxisZ=0,
        lButtons=0,
        **kwargs,
    ) -> bytes:
        if self._corrupt_axes:
            wAxisY = 99999
        return struct.pack(
            "BlllllllllllllllllllIIII",
            1,
            0,
            0,
            0,
            int(wAxisX),
            int(wAxisY),
            int(wAxisZ),
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            int(lButtons),
            0,
            0,
            0,
            0,
        )

    def update(self, packet: bytes) -> bool:
        self._update_count += 1
        if self._fail_update:
            return False
        if self._fail_button_press:
            axes = struct.unpack("BlllllllllllllllllllIIII", packet)
            if int(axes[19]):
                return False
        return True


class _FakeControls:
  SCALE = 16384

  def __init__(self, vj: _FakeVJoy | None = None) -> None:
      self.steer = 1.0
      self.acc = 0.0
      self.brake = 0.0
      self.steer_cmd = 0.0
      self.pedal_cmd = -1.0
      self.brake_cmd = -1.0
      self.onButtons = 0
      self.closed = False
      self.vj = vj or _FakeVJoy()

  def set_controls(self, steer, acc, brake, **kwargs) -> None:
      self.steer_cmd = float(steer)
      self.pedal_cmd = float(acc)
      self.brake_cmd = float(brake)
      self.steer = self.steer_cmd + 1.0
      self.acc = (self.pedal_cmd + 1.0) / 2.0
      self.brake = (self.brake_cmd + 1.0) / 2.0
      self.update()

  def update(self) -> None:
      pos = self.vj.generateJoystickPosition(
          wAxisX=int(self.steer * self.SCALE),
          wAxisY=int(self.acc * 2 * self.SCALE),
          wAxisZ=int(self.brake * 2 * self.SCALE),
          lButtons=int(self.onButtons),
      )
      self.vj.update(pos)

  def close(self) -> None:
      self.closed = True


def _gear_mapping_ok() -> dict:
    return {
        "valid": True,
        "vjoy_button_number": 1,
        "vjoy_button_mask": 0x00000001,
        "configured_button_raw": 0,
        "active_preset_path": "preset.ini",
    }


def _run_smoke(ctrl, **kwargs):
    from ac_workers import direct_vjoy_smoke as dvs

    defaults = {
        "open_controls_fn": lambda _d: ctrl,
        "vjoy_accessible_fn": lambda _d: True,
        "resolve_gear_fn": lambda _w: (True, "", _gear_mapping_ok()),
        "get_status_fn": lambda _d: dvs.VJD_STAT_OWN,
        "neutral_vjoy_fn": lambda _d: True,
        "acs_running_fn": lambda: True,
        "monotonic": lambda: 0.0,
        "sleep_fn": lambda _s: None,
    }
    defaults.update(kwargs)
    return dvs.run_direct_vjoy_smoke(0, **defaults)


def test_write_and_readback_match_ok():
    from ac_workers import direct_vjoy_smoke as dvs

    report = _run_smoke(_FakeControls())
    assert report["result"] == "ok"
    assert report["axis_write_verified"] is True
    assert report["device_acquired"] is True
    assert report["gear_up_pulse_sent"] is True
    assert report["commands_sent"] is True
    assert abs(float(report["during_readback"]["acc_pedal"]) - 0.20) < dvs.AXIS_TOLERANCE


def test_readback_mismatch_fails_explicitly():
    from ac_workers import direct_vjoy_smoke as dvs

    report = _run_smoke(_FakeControls(vj=_FakeVJoy(corrupt_axes=True)))
    assert report["result"] == "failed"
    assert report["commands_sent"] is False
    assert report["error"]
    assert "readback" in report["error"].lower()


def test_gear_up_press_then_release():
    from ac_workers import direct_vjoy_smoke as dvs

    report = _run_smoke(_FakeControls())
    assert report["gear_up_pulse_sent"] is True
    assert report["gear_up_button"] == 1


def test_ac_absent_sends_no_commands():
    from ac_workers import direct_vjoy_smoke as dvs

    opened = {"n": 0}
    report = dvs.run_direct_vjoy_smoke(
        0,
        acs_running_fn=lambda: False,
        open_controls_fn=lambda _d: opened.__setitem__("n", opened["n"] + 1) or _FakeControls(),
        vjoy_accessible_fn=lambda _d: True,
        resolve_gear_fn=lambda _w: (True, "", _gear_mapping_ok()),
    )
    assert report["ac_running"] is False
    assert report["error"] == dvs.AC_NOT_RUNNING_MSG
    assert report["commands_sent"] is False
    assert report["device_acquired"] is False
    assert opened["n"] == 0


def test_updatevjd_false_on_press_fails_explicitly():
    from ac_workers import direct_vjoy_smoke as dvs

    report = _run_smoke(_FakeControls(vj=_FakeVJoy(fail_button_press=True)))
    assert report["result"] == "failed"
    assert report["gear_up_pulse_sent"] is False
    assert report["commands_sent"] is False
    assert "gear up press UpdateVJD failed" in report["error"]
    assert report["neutralization_verified"] is True


def test_throttle_only_after_gear_verified():
    from ac_workers import direct_vjoy_smoke as dvs

    class _TrackControls(_FakeControls):
        def set_controls(self, steer, acc, brake, **kwargs):
            if float(acc) > -0.99 and not self._gear_done:
                raise AssertionError("throttle before gear pulse")
            return super().set_controls(steer, acc, brake, **kwargs)

    ctrl = _TrackControls()
    ctrl._gear_done = False
    original_pulse = dvs.pulse_gear_up

    def _pulse(c, mask, snapshot):
        result = original_pulse(c, mask, snapshot)
        ctrl._gear_done = True
        return result

    with mock.patch.object(dvs, "pulse_gear_up", side_effect=_pulse):
        report = _run_smoke(ctrl)
    assert report["result"] == "ok"
    assert report["commands_sent"] is True


def test_invalid_mapping_sends_no_throttle():
    from ac_workers import direct_vjoy_smoke as dvs

    opened = {"n": 0}
    report = dvs.run_direct_vjoy_smoke(
        0,
        acs_running_fn=lambda: True,
        open_controls_fn=lambda _d: opened.__setitem__("n", opened["n"] + 1) or _FakeControls(),
        vjoy_accessible_fn=lambda _d: False,
        resolve_gear_fn=lambda _w: (True, "", _gear_mapping_ok()),
        get_status_fn=lambda _d: dvs.VJD_STAT_OWN,
        neutral_vjoy_fn=lambda _d: True,
    )
    assert report["commands_sent"] is False
    assert report["result"] == "failed"
    assert opened["n"] == 0


def test_neutralization_confirmed_after_success():
    from ac_workers import direct_vjoy_smoke as dvs

    report = _run_smoke(_FakeControls())
    assert report["neutralization_verified"] is True
    assert abs(float(report["after_readback"]["acc_pedal"]) - 0.0) < dvs.AXIS_TOLERANCE
    assert abs(float(report["after_readback"]["brake_pedal"]) - 0.0) < dvs.AXIS_TOLERANCE
    assert int(report["after_readback"]["buttons"]) == 0
    assert abs(float(report["during_readback"]["acc_pedal"]) - 0.20) < dvs.AXIS_TOLERANCE
    assert abs(float(report["after_readback"]["acc_pedal"]) - float(report["during_readback"]["acc_pedal"])) > 0.1


def test_neutralization_confirmed_after_exception():
    from ac_workers import direct_vjoy_smoke as dvs

    class _BoomControls(_FakeControls):
        def set_controls(self, steer, acc, brake, **kwargs):
            if float(acc) > -0.99:
                raise RuntimeError("simulated vjoy failure")
            return super().set_controls(steer, acc, brake, **kwargs)

    neutral_calls: list[int] = []
    report = dvs.run_direct_vjoy_smoke(
        0,
        acs_running_fn=lambda: True,
        open_controls_fn=lambda _d: _BoomControls(),
        vjoy_accessible_fn=lambda _d: True,
        resolve_gear_fn=lambda _w: (True, "", _gear_mapping_ok()),
        get_status_fn=lambda _d: dvs.VJD_STAT_OWN,
        neutral_vjoy_fn=lambda d: neutral_calls.append(d) or True,
        monotonic=lambda: 0.0,
        sleep_fn=lambda _s: None,
    )
    assert report["result"] == "failed"
    assert neutral_calls == []
    assert report["neutralization_verified"] is True


def test_duration_bounded():
    from ac_workers import direct_vjoy_smoke as dvs

    clock = {"t": 0.0}
    _run_smoke(
        _FakeControls(),
        monotonic=lambda: clock["t"],
        sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s),
    )
    assert clock["t"] <= dvs.MAX_DURATION_SEC + dvs.MAX_GEAR_PULSE_WAIT_SEC + 0.1


def test_worker_one_refused():
    from ac_workers import direct_vjoy_smoke as dvs

    ok, reason, _mapping = dvs.validate_mapping(1, vjoy_accessible_fn=lambda _d: True)
    assert ok is False
    assert "worker 0" in reason


def test_resolve_worker_mapping_matches_ports():
    from ac_workers import direct_vjoy_smoke as dvs
    from ac_workers.worker_ports import worker_ports

    with mock.patch.dict(os.environ, {}, clear=False):
        mapping = dvs.resolve_worker_mapping(0)
    assert mapping["vjoy_device_id"] == worker_ports(0).vjoy


def test_decode_roundtrip():
    from ac_workers import direct_vjoy_smoke as dvs

    packet = struct.pack(
        "BlllllllllllllllllllIIII",
        1,
        0,
        0,
        0,
        16384,
        int(0.2 * 2 * 16384),
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        1,
        0,
        0,
        0,
        0,
    )
    logical = dvs.axes_to_logical(dvs.decode_joystick_position(packet))
    assert abs(float(logical["acc_pedal"]) - 0.2) < dvs.AXIS_TOLERANCE


def main() -> int:
    tests = [
        test_write_and_readback_match_ok,
        test_readback_mismatch_fails_explicitly,
        test_gear_up_press_then_release,
        test_ac_absent_sends_no_commands,
        test_updatevjd_false_on_press_fails_explicitly,
        test_throttle_only_after_gear_verified,
        test_invalid_mapping_sends_no_throttle,
        test_neutralization_confirmed_after_success,
        test_neutralization_confirmed_after_exception,
        test_duration_bounded,
        test_worker_one_refused,
        test_resolve_worker_mapping_matches_ports,
        test_decode_roundtrip,
    ]
    for fn in tests:
        fn()
        print("OK", fn.__name__)
    print("Test-DirectVJoySmoke-Offline: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
