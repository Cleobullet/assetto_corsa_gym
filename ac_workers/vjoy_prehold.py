"""Acquire vJoy and feed neutral before slow gym env construction.

Assetto Corsa binds vJoy via DirectInput while the session loads. The collector
process spends several seconds importing gym/numba and building the track model
before ``attach_live_session`` runs. Pre-hold acquires the worker device
immediately after the diagnostic listener is up and keeps neutral axes until the
full environment takes over the same Controls handle.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO / "assetto_corsa_gym" / "AssettoCorsaPlugin" / "plugins" / "sensors_par"

PULSE_INTERVAL_SEC = 0.05
PREHOLD_ACQUIRE_RETRIES = 30
PREHOLD_ACQUIRE_DELAY_SEC = 0.1


def _ensure_plugin_path() -> None:
    plugin = str(PLUGIN_DIR)
    if plugin not in sys.path:
        sys.path.insert(0, plugin)


class VjoyPrehold:
    def __init__(self, controls: Any, device_id: int):
        self.controls = controls
        self.device_id = int(device_id)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.acquired = False

    def start(self) -> tuple[bool, str]:
        from ac_workers.vjoy_session import apply_neutral_controls, ensure_vjoy_feeder

        ok, err, acquired = ensure_vjoy_feeder(
            self.controls,
            self.device_id,
            retries=PREHOLD_ACQUIRE_RETRIES,
            delay_sec=PREHOLD_ACQUIRE_DELAY_SEC,
        )
        if not ok or not acquired:
            return False, err or "vJoy prehold acquire failed for device {0}".format(self.device_id)
        self.acquired = True
        self._thread = threading.Thread(target=self._pulse_loop, name="vjoy-prehold", daemon=True)
        self._thread.start()
        return True, ""

    def _pulse_loop(self) -> None:
        from ac_workers.vjoy_session import apply_neutral_controls

        while not self._stop.is_set():
            try:
                apply_neutral_controls(self.controls)
                if hasattr(self.controls, "update"):
                    self.controls.update()
            except Exception:
                pass
            self._stop.wait(PULSE_INTERVAL_SEC)

    def stop_pulsing(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


def start_vjoy_prehold(worker_id: int) -> tuple[VjoyPrehold | None, str]:
    from AssettoCorsaEnv.gear_shift import resolve_shift_up_mapping

    from ac_workers.vjoy_pool import device_for_worker

    mapping = resolve_shift_up_mapping(worker_id)
    if not mapping.get("valid"):
        return None, str(mapping.get("error") or "invalid gear mapping")
    device_id = int(mapping.get("vjoy_device_id", device_for_worker(worker_id)))
    os.environ["ACGYM_VJOY_DEVICE"] = str(device_id)
    _ensure_plugin_path()
    from car_control import Controls  # noqa: WPS433

    controls = Controls()
    prehold = VjoyPrehold(controls, device_id)
    ok, err = prehold.start()
    if not ok:
        return None, err
    return prehold, ""
