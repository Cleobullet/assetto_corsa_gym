"""Shared vJoy feeder readback helpers (collector + direct_vjoy_smoke)."""
from __future__ import annotations

import ctypes
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from AssettoCorsaEnv.training_safety import BRAKE_INTERNAL_OFF

AXIS_TOLERANCE = 0.02
SCALE = 16384
VJD_STAT_OWN = 0
VJD_STAT_FREE = 1
VJD_STAT_BUSY = 2
VJD_STAT_MISS = 3
# Returned by get_vjd_status when vJoyInterface.dll is unavailable (not a driver code).
VJD_STAT_DLL = 4
VJOY_DLL = r"C:\Program Files\vJoy\x64\vJoyInterface.dll"
JOYSTICK_POSITION_FMT = "BlllllllllllllllllllIIII"

# Retries apply to every worker/device — acquisition races are common during
# scale-test (sequential launches) and after episode resets.
DEFAULT_VJOY_ACQUIRE_RETRIES = 12
DEFAULT_VJOY_ACQUIRE_DELAY_SEC = 0.25
RESET_VJOY_ACQUIRE_RETRIES = 20
RESET_VJOY_ACQUIRE_DELAY_SEC = 0.30

VJOY_PATH = (
    "AssettoCorsaPlugin.plugins.sensors_par.car_control.Controls.set_controls"
    " -> setJoy(wAxisX=steer*SCALE, wAxisY=acc*2*SCALE, wAxisZ=brake*2*SCALE)"
    " -> UpdateVJD (readback via last feeder packet + GetVJDStatus)"
)


def vjd_status_label(status: int) -> str:
    return {
        VJD_STAT_OWN: "OWN",
        VJD_STAT_FREE: "FREE",
        VJD_STAT_BUSY: "BUSY",
        VJD_STAT_MISS: "MISS",
        VJD_STAT_DLL: "DLL",
    }.get(int(status), "UNK({0})".format(status))


@dataclass
class FeederReadback:
    last_axes: dict[str, int]
    last_logical: dict[str, float | int]
    update_ok: bool

    @classmethod
    def empty(cls) -> FeederReadback:
        return cls(last_axes={}, last_logical={}, update_ok=False)


def decode_joystick_position(packet: bytes) -> dict[str, int]:
    fields = struct.unpack(JOYSTICK_POSITION_FMT, packet)
    return {
        "wAxisX": int(fields[4]),
        "wAxisY": int(fields[5]),
        "wAxisZ": int(fields[6]),
        "lButtons": int(fields[19]),
    }


def axes_to_logical(axes: dict[str, int]) -> dict[str, float | int]:
    return {
        "steer_vjoy": float(axes["wAxisX"]) / float(SCALE),
        "acc_pedal": float(axes["wAxisY"]) / float(2 * SCALE),
        "brake_pedal": float(axes["wAxisZ"]) / float(2 * SCALE),
        "buttons": int(axes["lButtons"]),
    }


def expected_logical_neutral() -> dict[str, float | int]:
    return {
        "steer_vjoy": 1.0,
        "acc_pedal": 0.0,
        "brake_pedal": 0.0,
        "buttons": 0,
    }


def verify_logical_match(
    expected: dict[str, float | int],
    actual: dict[str, float | int],
    *,
    tolerance: float = AXIS_TOLERANCE,
) -> tuple[bool, str]:
    for key in ("steer_vjoy", "acc_pedal", "brake_pedal"):
        exp = float(expected[key])
        got = float(actual.get(key, -999.0))
        if abs(got - exp) > tolerance:
            return False, "{0} expected {1:.4f} readback {2:.4f}".format(key, exp, got)
    exp_btn = int(expected.get("buttons", 0))
    got_btn = int(actual.get("buttons", -1))
    if exp_btn != got_btn:
        return False, "buttons expected 0x{0:08x} readback 0x{1:08x}".format(exp_btn, got_btn)
    return True, ""


def _vjoy_dll() -> ctypes.CDLL | None:
    if not Path(VJOY_DLL).is_file():
        return None
    return ctypes.CDLL(VJOY_DLL)


def get_vjd_status(device_id: int) -> int:
    dll = _vjoy_dll()
    if dll is None:
        return VJD_STAT_DLL
    dll.GetVJDStatus.argtypes = [ctypes.c_uint]
    dll.GetVJDStatus.restype = ctypes.c_int
    return int(dll.GetVJDStatus(device_id))


def relinquish_vjd(device_id: int) -> bool:
    dll = _vjoy_dll()
    if dll is None:
        return False
    dll.RelinquishVJD.argtypes = [ctypes.c_uint]
    dll.RelinquishVJD.restype = ctypes.c_bool
    return bool(dll.RelinquishVJD(device_id))


def reset_vjd(device_id: int) -> bool:
    dll = _vjoy_dll()
    if dll is None:
        return False
    dll.ResetVJD.argtypes = [ctypes.c_uint]
    dll.ResetVJD.restype = ctypes.c_bool
    return bool(dll.ResetVJD(device_id))


def prep_vjoy_device(device_id: int, *, reset_free: bool = False) -> None:
    """Best-effort cleanup before AcquireVJD.

    Avoid ResetVJD on FREE devices unless ``reset_free`` — AC may hold the device via
    DirectInput without vJoy ownership; resetting a FREE device then breaks AcquireVJD
    (status=FREE, acquire fails) for workers 1..N-1 during scale-test.
    """
    status = get_vjd_status(device_id)
    if status == VJD_STAT_OWN:
        relinquish_vjd(device_id)
    if reset_free or status not in (VJD_STAT_FREE, VJD_STAT_MISS):
        reset_vjd(device_id)


def resolve_controls_handle(ctrl: Any) -> Any | None:
    lc = getattr(ctrl, "local_controls", None)
    if lc is not None:
        return lc
    if hasattr(ctrl, "vj"):
        return ctrl
    return None


def resolve_local_controls(ctrl: Any) -> Any | None:
    """Return sensors_par Controls with an open .vj handle."""
    handle = resolve_controls_handle(ctrl)
    if handle is None or not hasattr(handle, "vj"):
        return None
    return handle


def attach_feeder_readback(vj: Any) -> Callable[[], FeederReadback]:
    existing = getattr(vj, "_ac_feeder_readback_fn", None)
    if existing is not None:
        return existing
    state = {"readback": FeederReadback.empty()}
    original_update = vj.update

    def wrapped_update(packet: bytes) -> bool:
        ok = bool(original_update(packet))
        axes = decode_joystick_position(packet)
        state["readback"] = FeederReadback(
            last_axes=axes,
            last_logical=axes_to_logical(axes),
            update_ok=ok,
        )
        return ok

    vj.update = wrapped_update

    def snapshot() -> FeederReadback:
        return state["readback"]

    vj._ac_feeder_readback_fn = snapshot
    return snapshot


def ensure_feeder_readback(ctrl: Any) -> Callable[[], FeederReadback] | None:
    lc = resolve_local_controls(ctrl)
    if lc is None:
        return None
    return attach_feeder_readback(lc.vj)


def logical_to_internal_axes(logical: dict[str, float | int]) -> dict[str, float]:
    steer_vjoy = float(logical.get("steer_vjoy", 1.0))
    acc_pedal = float(logical.get("acc_pedal", 0.0))
    brake_pedal = float(logical.get("brake_pedal", 0.0))
    return {
        "steer": steer_vjoy - 1.0,
        "throttle": acc_pedal * 2.0 - 1.0,
        "brake": brake_pedal * 2.0 - 1.0,
    }


def read_logical_from_controls(ctrl: Any) -> tuple[dict[str, float | int], bool]:
    snapshot_fn = ensure_feeder_readback(ctrl)
    if snapshot_fn is not None:
        rb = snapshot_fn()
        if rb.last_logical:
            return dict(rb.last_logical), True
    handle = resolve_controls_handle(ctrl)
    if handle is not None and hasattr(handle, "steer"):
        return {
            "steer_vjoy": float(getattr(handle, "steer", 1.0)),
            "acc_pedal": float(getattr(handle, "acc", 0.0)),
            "brake_pedal": float(getattr(handle, "brake", 0.0)),
            "buttons": int(getattr(handle, "onButtons", 0)),
        }, True
    return {}, False


def _close_vjoy_handle(vj: Any) -> None:
    if not getattr(vj, "acquired", False):
        return
    try:
        vj.close()
    except Exception:
        pass


def try_acquire_vjoy(
    vj: Any,
    device_id: int,
    *,
    get_status_fn: Callable[[int], int] | None = None,
    allow_global_reset: bool = True,
) -> tuple[bool, str]:
    """Single attempt to own vJoy device_id for this feeder handle.

    ``allow_global_reset`` gates the last-resort ResetVJD. ResetVJD bounces the
    SHARED vJoy raw PDO and disrupts every device at once, so live paths (runtime
    reacquire, episode-reset recovery) pass False to avoid cascading
    AcquireVJD(FREE) failures onto sibling workers; the privileged janitor/reaper
    performs global resets safely when no device has a live owner.

    vJoy ownership is per-PROCESS: once this collector AcquireVJD's the device at
    startup it stays owned for the life of the process. If the live handle still
    reports ``acquired`` AND the driver still reports VJD_STAT_OWN, REUSE it: do
    not RelinquishVJD + AcquireVJD again. The self-inflicted relinquish drops a
    working ownership and the immediate re-AcquireVJD then races the driver's PDO
    teardown, which is exactly what surfaces as "AcquireVJD failed status=1
    (FREE)" during episode resets. Only relinquish + reacquire when the device was
    genuinely lost (status != OWN).
    """
    status_fn = get_status_fn or get_vjd_status
    status = int(status_fn(device_id))
    if getattr(vj, "acquired", False) and status == VJD_STAT_OWN:
        return True, ""
    _close_vjoy_handle(vj)
    status = int(status_fn(device_id))
    if status == VJD_STAT_DLL:
        return False, (
            "vJoy driver unavailable (vJoyInterface.dll not loaded; "
            "reinstall/restart vJoy or reduce parallel device pressure)"
        )
    if status == VJD_STAT_MISS:
        return False, "vJoy device {0} missing (enable in Configure vJoy)".format(device_id)

    if status == VJD_STAT_BUSY:
        return False, "vJoy device {0} busy (another feeder)".format(device_id)

    def _try_open() -> tuple[bool, str]:
        try:
            if vj.open():
                status_now = int(status_fn(device_id))
                if getattr(vj, "acquired", False) and status_now in (VJD_STAT_OWN, VJD_STAT_FREE):
                    return True, ""
        except Exception as exc:
            return False, "AcquireVJD exception: {0}".format(exc)
        return False, ""

    ok, err = _try_open()
    if ok:
        return True, ""
    if err:
        return False, err

    # FREE but not acquirable: relinquish only (no ResetVJD yet).
    if status == VJD_STAT_FREE:
        relinquish_vjd(device_id)
        ok, err = _try_open()
        if ok:
            return True, ""
        if err:
            return False, err

    # Last resort: global ResetVJD. Only safe when no sibling worker is live.
    if allow_global_reset:
        prep_vjoy_device(device_id, reset_free=True)
        ok, err = _try_open()
        if ok:
            return True, ""
        if err:
            return False, err

    status = int(status_fn(device_id))
    return False, "AcquireVJD failed status={0} ({1})".format(status, vjd_status_label(status))


def acquire_vjoy_device(
    ctrl: Any,
    device_id: int,
    *,
    get_status_fn: Callable[[int], int] | None = None,
    retries: int = DEFAULT_VJOY_ACQUIRE_RETRIES,
    delay_sec: float = DEFAULT_VJOY_ACQUIRE_DELAY_SEC,
    allow_global_reset: bool = True,
) -> tuple[bool, str]:
    """Retry vJoy acquisition for any worker device (shared by startup + reset)."""
    from ac_workers.vjoy_ledger import device_acquire_lock

    lc = resolve_local_controls(ctrl)
    if lc is None:
        return False, "local_controls unavailable"
    last_err = ""
    attempts = max(1, int(retries))
    try:
        with device_acquire_lock(device_id):
            for attempt in range(attempts):
                ok, err = try_acquire_vjoy(
                    lc.vj,
                    device_id,
                    get_status_fn=get_status_fn,
                    allow_global_reset=allow_global_reset,
                )
                if ok:
                    return True, ""
                last_err = err
                status_fn = get_status_fn or get_vjd_status
                if int(status_fn(device_id)) == VJD_STAT_MISS:
                    return False, err
                if attempt + 1 < attempts:
                    time.sleep(delay_sec)
    except TimeoutError as exc:
        return False, str(exc)
    return False, last_err


def device_feeds_successfully(ctrl: Any) -> bool:
    """Functional check: neutral UpdateVJD + readback succeeds."""
    readback = ensure_feeder_readback(ctrl)
    if readback is None:
        return False
    apply_neutral_controls(ctrl)
    ok, _, _ = write_and_verify(readback, expected_logical_neutral())
    return ok


def reacquire_vjoy_device(
    ctrl: Any,
    device_id: int,
    *,
    get_status_fn: Callable[[int], int] | None = None,
    retries: int = RESET_VJOY_ACQUIRE_RETRIES,
    delay_sec: float = RESET_VJOY_ACQUIRE_DELAY_SEC,
    allow_global_reset: bool = False,
) -> tuple[bool, str]:
    # Reacquire always happens during a live session (runtime vJoy glitch or
    # episode reset) where sibling workers may be active, so the global ResetVJD
    # is disabled by default to avoid cascading AcquireVJD(FREE) across workers.
    return acquire_vjoy_device(
        ctrl,
        device_id,
        get_status_fn=get_status_fn,
        retries=retries,
        delay_sec=delay_sec,
        allow_global_reset=allow_global_reset,
    )


def ensure_vjoy_feeder(
    ctrl: Any,
    device_id: int,
    *,
    get_status_fn: Callable[[int], int] | None = None,
    retries: int = DEFAULT_VJOY_ACQUIRE_RETRIES,
    delay_sec: float = DEFAULT_VJOY_ACQUIRE_DELAY_SEC,
    allow_global_reset: bool = True,
) -> tuple[bool, str, bool]:
    """Acquire (with retries) then verify feeder can write neutral controls.

    Returns (ok, error, device_acquired_flag).
    """
    ok, err = acquire_vjoy_device(
        ctrl,
        device_id,
        get_status_fn=get_status_fn,
        retries=retries,
        delay_sec=delay_sec,
        allow_global_reset=allow_global_reset,
    )
    if not ok:
        return False, err, False
    lc = resolve_local_controls(ctrl)
    if lc is not None and hasattr(lc, "enable_feeder"):
        lc.enable_feeder()
    if device_feeds_successfully(ctrl):
        return True, "", True
    # Driver reports ownership but UpdateVJD failed — one more acquire cycle.
    ok, err = acquire_vjoy_device(
        ctrl,
        device_id,
        get_status_fn=get_status_fn,
        retries=max(3, retries // 2),
        delay_sec=delay_sec,
        allow_global_reset=allow_global_reset,
    )
    if not ok:
        return False, err or "feeder functional check failed", False
    if device_feeds_successfully(ctrl):
        return True, "", True
    return False, "vJoy device {0} acquired but neutral feed failed".format(device_id), False


def apply_neutral_controls(ctrl: Any) -> None:
    ctrl.onButtons = 0
    ctrl.set_controls(0.0, -1.0, BRAKE_INTERNAL_OFF)


def write_and_verify(
    readback_snapshot: Callable[[], FeederReadback],
    expected: dict[str, float | int],
) -> tuple[bool, str, FeederReadback]:
    rb = readback_snapshot()
    if not rb.update_ok:
        return False, "UpdateVJD returned false", rb
    ok, reason = verify_logical_match(expected, rb.last_logical)
    if not ok:
        return False, reason, rb
    return True, "", rb


def push_vjoy_state(
    ctrl: Any,
    readback_snapshot: Callable[[], FeederReadback],
    expected: dict[str, float | int],
) -> tuple[bool, str, FeederReadback]:
    ctrl.update()
    return write_and_verify(readback_snapshot, expected)


def pulse_gear_up(
    ctrl: Any,
    mask: int,
    readback_snapshot: Callable[[], FeederReadback],
) -> tuple[bool, str]:
    apply_neutral_controls(ctrl)
    ok, reason, _rb = write_and_verify(readback_snapshot, expected_logical_neutral())
    if not ok:
        return False, "gear up baseline: {0}".format(reason)

    expected_press = expected_logical_neutral()
    expected_press["buttons"] = int(mask)
    ctrl.onButtons = int(mask)
    ok, reason, _rb = push_vjoy_state(ctrl, readback_snapshot, expected_press)
    if not ok:
        if reason == "UpdateVJD returned false":
            return False, "gear up press UpdateVJD failed"
        return False, "gear up press readback: {0}".format(reason)

    ctrl.onButtons = 0
    apply_neutral_controls(ctrl)
    ok, reason, _rb = write_and_verify(readback_snapshot, expected_logical_neutral())
    if not ok:
        if reason == "UpdateVJD returned false":
            return False, "gear up release UpdateVJD failed"
        return False, "gear up release readback: {0}".format(reason)
    return True, ""


def device_acquired(ctrl: Any, device_id: int, *, get_status_fn: Callable[[int], int] | None = None) -> bool:
    lc = resolve_local_controls(ctrl)
    if lc is None:
        return False
    if not getattr(lc.vj, "acquired", False):
        return False
    status_fn = get_status_fn or get_vjd_status
    status = int(status_fn(device_id))
    if status == VJD_STAT_OWN:
        return True
    # Some drivers briefly report FREE while this feeder still updates successfully.
    if status in (VJD_STAT_FREE, VJD_STAT_BUSY):
        return device_feeds_successfully(ctrl)
    return False


def neutralize_controls_handle(
    ctrl: Any,
    readback_snapshot: Callable[[], FeederReadback] | None = None,
    *,
    close: bool = False,
    device_id: int | None = None,
    allow_reacquire: bool = False,
    diag: dict[str, Any] | None = None,
) -> tuple[bool, str, FeederReadback | None]:
    rb: FeederReadback | None = None
    attempts = 0
    if diag is not None:
        diag.setdefault("reset_update_vjd_attempts", 0)
        diag.setdefault("reset_vjoy_reacquire_attempted", False)
        diag.setdefault("reset_vjoy_reacquire_succeeded", False)
    try:
        for attempt in range(2 if allow_reacquire else 1):
            attempts += 1
            if diag is not None:
                diag["reset_update_vjd_attempts"] = attempts
            apply_neutral_controls(ctrl)
            if readback_snapshot is not None:
                ok, reason, rb = write_and_verify(readback_snapshot, expected_logical_neutral())
                if ok:
                    if diag is not None:
                        diag["reset_neutralization_verified"] = True
                    break
                if reason != "UpdateVJD returned false" or not allow_reacquire or attempt > 0:
                    if diag is not None:
                        diag["reset_neutralization_verified"] = False
                        diag["reset_last_error"] = reason
                    return False, reason, rb
                if device_id is None:
                    lc = resolve_local_controls(ctrl)
                    device_id = int(getattr(lc.vj, "reference", 1)) if lc is not None else 1
                if diag is not None:
                    diag["reset_vjoy_reacquire_attempted"] = True
                re_ok, re_err = reacquire_vjoy_device(ctrl, device_id)
                if diag is not None:
                    diag["reset_vjoy_reacquire_succeeded"] = re_ok
                if not re_ok:
                    if diag is not None:
                        diag["reset_last_error"] = re_err
                    return False, re_err, rb
                continue
            break
        else:
            if diag is not None:
                diag["reset_neutralization_verified"] = False
            return False, "UpdateVJD returned false", rb
    except Exception as exc:
        if diag is not None:
            diag["reset_last_error"] = str(exc)
        return False, str(exc), rb
    if close:
        try:
            ctrl.close()
        except Exception:
            pass
    return True, "", rb


def controls_from_env(env: Any) -> Any:
    return env.client.controls.local_controls


def enable_env_local_vjoy(env: Any) -> None:
    controls = getattr(getattr(env, "client", None), "controls", None)
    if controls is not None and hasattr(controls, "enable_local_vjoy_feeder"):
        controls.enable_local_vjoy_feeder()


def release_env_local_vjoy(env: Any, device_id: int) -> None:
    controls = getattr(getattr(env, "client", None), "controls", None)
    if controls is not None and hasattr(controls, "local_controls"):
        lc = controls.local_controls
        if lc is not None and hasattr(lc, "vj"):
            _close_vjoy_handle(lc.vj)
    prep_vjoy_device(device_id)
