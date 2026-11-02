"""Offline tests for shared vJoy acquisition helpers."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))


class _FakeVJoy:
    def __init__(self, reference: int = 1) -> None:
        self.reference = reference
        self.acquired = False
        self._open_attempts = 0
        self._close_attempts = 0

    def open(self) -> bool:
        self._open_attempts += 1
        self.acquired = True
        return True

    def close(self) -> bool:
        self._close_attempts += 1
        self.acquired = False
        return True


class _FakeControls:
    def __init__(self) -> None:
        self.vj = _FakeVJoy()
        self.onButtons = 0
        self.steer = 1.0
        self.acc = 0.0
        self.brake = 0.0

    def set_controls(self, steer, acc, brake, **kwargs) -> None:
        self.steer = steer + 1.0
        self.acc = (acc + 1) / 2
        self.brake = (brake + 1) / 2
        self.onButtons = 0

    def update(self) -> None:
        pass


def test_acquire_retries_until_own():
    from ac_workers import vjoy_session as vs

    ctrl = _FakeControls()
    calls = {"n": 0}

    def _status(_device: int) -> int:
        calls["n"] += 1
        if calls["n"] < 3:
            return vs.VJD_STAT_BUSY
        return vs.VJD_STAT_OWN

    ok, err = vs.acquire_vjoy_device(
        ctrl,
        2,
        get_status_fn=_status,
        retries=5,
        delay_sec=0.0,
    )
    assert ok, err
    assert ctrl.vj.acquired
    assert calls["n"] >= 3


def test_device_acquired_accepts_functional_free_status():
    from ac_workers import vjoy_session as vs

    ctrl = _FakeControls()
    ctrl.vj.acquired = True
    with mock.patch.object(vs, "get_vjd_status", return_value=vs.VJD_STAT_FREE), mock.patch.object(
        vs, "device_feeds_successfully", return_value=True
    ):
        assert vs.device_acquired(ctrl, 2) is True


def test_ensure_vjoy_feeder_fails_when_neutral_feed_fails():
    from ac_workers import vjoy_session as vs

    ctrl = _FakeControls()
    with mock.patch.object(
        vs, "acquire_vjoy_device", return_value=(True, "")
    ), mock.patch.object(vs, "device_feeds_successfully", return_value=False):
        ok, err, acquired = vs.ensure_vjoy_feeder(ctrl, 3, retries=2, delay_sec=0.0)
    assert not ok
    assert acquired is False
    assert "neutral feed failed" in err


def main() -> int:
    tests = [
        test_acquire_retries_until_own,
        test_device_acquired_accepts_functional_free_status,
        test_ensure_vjoy_feeder_fails_when_neutral_feed_fails,
        test_prep_not_called_on_immediate_free_acquire,
        test_prep_called_after_free_acquire_failure,
        test_no_global_reset_on_free_failure_when_disabled,
        test_owned_handle_is_reused_without_relinquish,
        test_reacquire_reuses_owned_handle_without_relinquish,
        test_lost_handle_still_relinquishes_then_reacquires,
        test_reacquire_defaults_to_no_global_reset,
    ]
    for fn in tests:
        fn()
        print("OK", fn.__name__)
    print("Test-vJoySession-Offline: OK")
    return 0


def test_prep_not_called_on_immediate_free_acquire():
    from ac_workers import vjoy_session as vs

    ctrl = _FakeControls()
    ctrl.vj.acquired = False
    with mock.patch.object(vs, "prep_vjoy_device") as prep:
        ok, err = vs.try_acquire_vjoy(ctrl.vj, 1, get_status_fn=lambda _d: vs.VJD_STAT_FREE)
    assert ok is True, err
    prep.assert_not_called()


def test_prep_called_after_free_acquire_failure():
    from ac_workers import vjoy_session as vs

    ctrl = _FakeControls()
    ctrl.vj.acquired = False

    def _fail_open() -> bool:
        return False

    ctrl.vj.open = _fail_open
    with mock.patch.object(vs, "prep_vjoy_device") as prep, mock.patch.object(
        vs, "relinquish_vjd", return_value=True
    ):
        ok, err = vs.try_acquire_vjoy(ctrl.vj, 1, get_status_fn=lambda _d: vs.VJD_STAT_FREE)
    assert ok is False
    prep.assert_called_once_with(1, reset_free=True)


def test_no_global_reset_on_free_failure_when_disabled():
    """Live paths must NOT call the global ResetVJD: it bounces the shared PDO and
    cascades AcquireVJD(FREE) onto sibling workers."""
    from ac_workers import vjoy_session as vs

    ctrl = _FakeControls()
    ctrl.vj.acquired = False
    ctrl.vj.open = lambda: False
    with mock.patch.object(vs, "prep_vjoy_device") as prep, mock.patch.object(
        vs, "relinquish_vjd", return_value=True
    ):
        ok, err = vs.try_acquire_vjoy(
            ctrl.vj,
            1,
            get_status_fn=lambda _d: vs.VJD_STAT_FREE,
            allow_global_reset=False,
        )
    assert ok is False
    prep.assert_not_called()
    assert "AcquireVJD failed" in err


def test_owned_handle_is_reused_without_relinquish():
    """Root-cause guard: an already-OWN handle must NOT be relinquished+reacquired.

    The self-inflicted RelinquishVJD + AcquireVJD on a healthy ownership is what
    produced "AcquireVJD failed status=1 (FREE)" on workers during episode resets.
    """
    from ac_workers import vjoy_session as vs

    ctrl = _FakeControls()
    ctrl.vj.acquired = True
    ok, err = vs.try_acquire_vjoy(
        ctrl.vj,
        2,
        get_status_fn=lambda _d: vs.VJD_STAT_OWN,
    )
    assert ok is True, err
    assert ctrl.vj.acquired is True
    assert ctrl.vj._close_attempts == 0  # never relinquished a live ownership
    assert ctrl.vj._open_attempts == 0  # no redundant AcquireVJD


def test_reacquire_reuses_owned_handle_without_relinquish():
    """reacquire on an OWN device must short-circuit (reuse), not drop ownership."""
    from ac_workers import vjoy_session as vs

    ctrl = _FakeControls()
    ctrl.vj.acquired = True
    ok, err = vs.reacquire_vjoy_device(
        ctrl,
        2,
        get_status_fn=lambda _d: vs.VJD_STAT_OWN,
        retries=3,
        delay_sec=0.0,
    )
    assert ok is True, err
    assert ctrl.vj._close_attempts == 0
    assert ctrl.vj._open_attempts == 0


def test_lost_handle_still_relinquishes_then_reacquires():
    """If the device was genuinely lost (FREE) we still relinquish+reacquire."""
    from ac_workers import vjoy_session as vs

    ctrl = _FakeControls()
    ctrl.vj.acquired = True  # stale flag; driver says FREE below
    with mock.patch.object(vs, "relinquish_vjd", return_value=True):
        ok, err = vs.try_acquire_vjoy(
            ctrl.vj,
            2,
            get_status_fn=lambda _d: vs.VJD_STAT_FREE,
        )
    assert ok is True, err
    assert ctrl.vj._close_attempts == 1  # genuine loss -> clean reacquire
    assert ctrl.vj._open_attempts >= 1


def test_reacquire_defaults_to_no_global_reset():
    """reacquire_vjoy_device (always live) must not trigger the global ResetVJD."""
    from ac_workers import vjoy_session as vs

    ctrl = _FakeControls()
    ctrl.vj.acquired = False
    ctrl.vj.open = lambda: False
    with mock.patch.object(vs, "prep_vjoy_device") as prep, mock.patch.object(
        vs, "relinquish_vjd", return_value=True
    ):
        ok, _err = vs.reacquire_vjoy_device(
            ctrl,
            1,
            get_status_fn=lambda _d: vs.VJD_STAT_FREE,
            retries=2,
            delay_sec=0.0,
        )
    assert ok is False
    prep.assert_not_called()


if __name__ == "__main__":
    raise SystemExit(main())
