"""Collector safety: neutral vJoy when telemetry/management/transition/learner drop."""
from __future__ import annotations

import logging
import socket
import time

from ac_workers.worker_ports import worker_ports
from AssettoCorsaEnv.ac_worker_runtime import _pid_alive, read_manifest
from common.transition_server import transition_server_port

logger = logging.getLogger(__name__)


def neutral_vjoy(device_id: int) -> bool:
    try:
        import AssettoCorsaPlugin.plugins.sensors_par.vjoy as vjoy_mod

        vj = vjoy_mod.vJoy(reference=device_id)
        vj.open()
        # Canonical released pedals: steer center, throttle 0% (Y=0), brake 0% (Z=0).
        pos = vj.generateJoystickPosition(wAxisX=16384, wAxisY=0, wAxisZ=0, lButtons=0)
        vj.update(pos)
        vj.close()
        return True
    except Exception:
        return False


def neutral_all_vjoy(max_device: int = 6) -> dict[int, bool]:
    out: dict[int, bool] = {}
    for dev in range(1, max_device + 1):
        out[dev] = neutral_vjoy(dev)
    return out


def _tcp_open(port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


class CollectorSafetyWatchdog:
    """Return safety cause string if vJoy must be neutralized."""

    def __init__(
        self,
        worker_id: int,
        ego_port: int | None = None,
        mgmt_port: int | None = None,
        vjoy_device: int | None = None,
        learner_timeout_sec: float = 8.0,
        link_timeout_sec: float = 3.0,
        telemetry_timeout_sec: float = 8.0,
    ):
        ports = worker_ports(worker_id)
        self.worker_id = worker_id
        self.ego_port = ego_port if ego_port is not None else ports.ego
        self.mgmt_port = mgmt_port if mgmt_port is not None else ports.mgmt
        self.vjoy_device = vjoy_device if vjoy_device is not None else ports.vjoy
        self.learner_timeout_sec = learner_timeout_sec
        self.link_timeout_sec = link_timeout_sec
        self.telemetry_timeout_sec = telemetry_timeout_sec
        self._last_ok = time.time()
        self._last_telemetry = time.time()
        self._tripped = False
        self._transition_port = transition_server_port()

    def note_telemetry(self) -> None:
        self._last_telemetry = time.time()

    def _learner_required(self) -> bool:
        manifest = read_manifest()
        pid = manifest.get("learnerPid")
        return bool(pid and _pid_alive(int(pid)))

    def _transition_server_ok(self) -> bool:
        try:
            from common.transition_server import TransitionClient

            TransitionClient().stats()
            return True
        except OSError:
            return _tcp_open(self._transition_port, timeout=0.4)

    def _learner_ok(self) -> bool:
        if not self._learner_required():
            return True
        try:
            from common.transition_server import TransitionClient

            stats = TransitionClient().stats()
            if not stats.get("learner_connected"):
                return False
            last = float(stats.get("learner_last_seen") or 0.0)
            if last <= 0:
                return False
            return (time.time() - last) <= self.learner_timeout_sec
        except OSError:
            return False

    def check_or_neutral(self, runtime=None) -> str:
        """Return empty if OK, else cause string (caller neutralizes vJoy)."""
        mgmt_ok = _tcp_open(self.mgmt_port, timeout=0.4)
        transition_ok = self._transition_server_ok()
        learner_ok = self._learner_ok()
        if runtime is not None:
            telem_age = runtime.telemetry_age_ms() / 1000.0
        else:
            telem_age = time.time() - self._last_telemetry
        telemetry_ok = telem_age <= self.telemetry_timeout_sec

        if mgmt_ok and transition_ok and learner_ok and telemetry_ok:
            self._last_ok = time.time()
            self._tripped = False
            return ""

        if (time.time() - self._last_ok) <= self.link_timeout_sec:
            return ""

        cause_parts = []
        if not telemetry_ok:
            cause_parts.append("telemetry_stale")
        if not mgmt_ok:
            cause_parts.append("management_down")
        if not transition_ok:
            cause_parts.append("transition_server_down")
        if not learner_ok:
            cause_parts.append("learner_stale")
        cause = ",".join(cause_parts) or "link_timeout"
        if not self._tripped:
            neutral_vjoy(self.vjoy_device)
            self._tripped = True
            logger.warning("collector safety trip worker=%s cause=%s", self.worker_id, cause)
        return cause


# After this hold, a parked car must teleport/reset instead of waiting forever.
# Training continues until the operator stops the learner.
STALE_STUCK_RESET_SEC = 8.0
STUCK_RESET_CAUSES = ("telemetry_stale", "management_down")


def should_reset_after_safety_hold(runtime, cause: str, *, now: float | None = None) -> bool:
    """True when a safety hold has parked the car long enough to request a reset."""
    now_m = time.monotonic() if now is None else float(now)
    cause = str(cause or "")
    stuck = any(token in cause for token in STUCK_RESET_CAUSES)
    if not stuck:
        setattr(runtime, "_safety_hold_since", 0.0)
        return False
    started = float(getattr(runtime, "_safety_hold_since", 0.0) or 0.0)
    if started <= 0:
        setattr(runtime, "_safety_hold_since", now_m)
        return False
    return (now_m - started) >= STALE_STUCK_RESET_SEC
