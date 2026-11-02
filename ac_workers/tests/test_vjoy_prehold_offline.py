"""Offline tests for vJoy prehold before gym env construction."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "assetto_corsa_gym") not in sys.path:
    sys.path.insert(0, str(REPO / "assetto_corsa_gym"))


class _FakeVj:
    acquired = True

    def update(self, _packet):
        return True


class _FakeControls:
    def __init__(self):
        self.vj = _FakeVj()
        self._feeder_enabled = False

    def enable_feeder(self):
        self._feeder_enabled = True

    def feeder_enabled(self):
        return self._feeder_enabled

    def set_controls(self, *args, **kwargs):
        return None

    def update(self):
        return None


def test_prehold_starts_pulse_thread():
    from ac_workers import vjoy_prehold as vph

    controls = _FakeControls()
    prehold = vph.VjoyPrehold(controls, 3)
    with mock.patch.object(
        vph,
        "ensure_vjoy_feeder",
        return_value=(True, "", True),
        create=True,
    ), mock.patch("ac_workers.vjoy_prehold.ensure_vjoy_feeder", return_value=(True, "", True)):
        with mock.patch("ac_workers.vjoy_session.ensure_vjoy_feeder", return_value=(True, "", True)):
            ok, err = prehold.start()
    assert ok is True
    assert err == ""
    assert prehold.acquired is True
    assert prehold._thread is not None
    assert prehold._thread.is_alive()
    prehold.stop_pulsing()
    assert prehold._thread is None


def test_start_vjoy_prehold_invalid_mapping():
    from ac_workers import vjoy_prehold as vph

    with mock.patch(
        "AssettoCorsaEnv.gear_shift.resolve_shift_up_mapping",
        return_value={"valid": False, "error": "bad preset"},
    ):
        prehold, err = vph.start_vjoy_prehold(2)
    assert prehold is None
    assert "bad preset" in err


def test_worker_collector_starts_prehold_before_env():
    src = (REPO / "ac_workers" / "worker_collector.py").read_text(encoding="utf-8")
    env_idx = src.index("collector startup: creating environment")
    prehold_idx = src.index("vjoy_prehold_begin")
    inject_idx = src.index("injected preheld vJoy controls")
    assert prehold_idx < env_idx
    assert inject_idx > env_idx


def main() -> int:
    tests = [
        test_prehold_starts_pulse_thread,
        test_start_vjoy_prehold_invalid_mapping,
        test_worker_collector_starts_prehold_before_env,
    ]
    for fn in tests:
        fn()
        print("OK", fn.__name__)
    print("Test-VjoyPrehold-Offline: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
