"""Canonical low-level vJoy control helper — same path as direct_vjoy_smoke."""
from __future__ import annotations

import threading
from typing import Any

import numpy as np

from ac_workers.vjoy_session import (
    DEFAULT_VJOY_ACQUIRE_RETRIES,
    attach_feeder_readback,
    get_vjd_status,
    reacquire_vjoy_device,
)

SCALE = 16384
VJOY_WRITE_LOCK = threading.RLock()


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def physical_to_internal(steer: float, throttle: float, brake: float) -> tuple[float, float, float]:
    return (
        float(steer),
        float(throttle) * 2.0 - 1.0,
        float(brake) * 2.0 - 1.0,
    )


def internal_to_physical(steer: float, acc: float, brake: float) -> tuple[float, float, float]:
    return (
        float(steer),
        (float(acc) + 1.0) / 2.0,
        (float(brake) + 1.0) / 2.0,
    )


# Pedal mapping mode (exposed in diagnostics). A RELEASED pedal is 0%. The policy
# output is in [-1, 1]; only the POSITIVE half presses the pedal, so the network's
# neutral output (~0) maps to 0% pedal — NOT 50%. This makes throttle and brake a
# real action the AI must actively choose, never a default/biased value.
#   raw <= 0 -> 0% pedal
#   raw  > 0 -> progressive, 100% at raw=1
PEDAL_MAPPING_MODE = "relu_zero_neutral"
# SAC tanh almost never reaches +1. Treat a committed brake (~0.75) as 100%.
BRAKE_FULL_AT = 0.75


def raw_to_pedal(raw_value: float, full_at: float = 1.0) -> float:
    """Map a single raw action axis in [-1,1] to a pedal fraction in [0,1].

    Released (raw<=0) is 0%; pressing is linear from 0 at raw=0 to 100% at raw=full_at.
    """
    span = max(float(full_at), 1e-6)
    return _clamp(max(0.0, float(raw_value)) / span, 0.0, 1.0)


def raw_action_to_physical(raw: Any) -> tuple[float, float, float]:
    values = [float(x) for x in np.asarray(raw, dtype=np.float32).ravel()[:3]]
    steer = _clamp(values[0], -1.0, 1.0)
    throttle = raw_to_pedal(values[1])
    brake = raw_to_pedal(values[2], full_at=BRAKE_FULL_AT)
    return steer, throttle, brake


def get_local_controls(env: Any) -> Any:
    ctrl = getattr(getattr(env, "client", None), "controls", None)
    if ctrl is None:
        return None
    lc = getattr(ctrl, "local_controls", None)
    if lc is not None and hasattr(lc, "set_controls"):
        return lc
    if hasattr(ctrl, "set_controls") and hasattr(ctrl, "vj"):
        return ctrl
    return None


def bind_canonical_controls(local_controls: Any, device_id: int) -> dict[str, Any]:
    """Attach feeder readback once; return stable session ids for startup/policy tracing."""
    if local_controls is None or not hasattr(local_controls, "vj"):
        return {
            "local_controls_id": None,
            "vjoy_id": None,
            "device_id": int(device_id),
        }
    attach_feeder_readback(local_controls.vj)
    return {
        "local_controls_id": id(local_controls),
        "vjoy_id": id(local_controls.vj),
        "device_id": int(device_id),
    }


def _feeder_update_ok(local_controls: Any) -> bool:
    vj = getattr(local_controls, "vj", None)
    if vj is None:
        return False
    snapshot_fn = getattr(vj, "_ac_feeder_readback_fn", None)
    if snapshot_fn is not None:
        return bool(snapshot_fn().update_ok)
    return bool(getattr(vj, "acquired", False))


def _resolve_device_id(local_controls: Any, device_id: int | None) -> int | None:
    if device_id is not None:
        return int(device_id)
    vj = getattr(local_controls, "vj", None)
    if vj is None:
        return None
    return int(getattr(vj, "reference", 1))


def apply_canonical_controls(
    local_controls: Any,
    steer: float,
    throttle: float,
    brake: float,
    *,
    device_id: int | None = None,
    allow_reacquire: bool = True,
) -> dict[str, Any]:
    """Send physical steer/throttle/brake via Controls.set_controls (feeder readback for UpdateVJD)."""
    base: dict[str, Any] = {
        "ok": False,
        "error": "",
        "physical_action": [float(steer), float(throttle), float(brake)],
        "canonical_controls_called": False,
        "canonical_controls_object_id": None,
        "canonical_local_controls_object_id": None,
        "device_status_before_update": None,
        "update_vjd_result": False,
        "vjoy_device_id": None,
        "vjoy_object_id": None,
    }
    if local_controls is None or not hasattr(local_controls, "set_controls"):
        base["error"] = "local_controls_not_available"
        return base
    resolved_device = _resolve_device_id(local_controls, device_id)
    base["vjoy_device_id"] = resolved_device
    base["canonical_local_controls_object_id"] = id(local_controls)
    base["canonical_controls_object_id"] = id(local_controls)
    base["vjoy_object_id"] = id(getattr(local_controls, "vj", None)) if getattr(local_controls, "vj", None) else None
    steer_i, acc_i, brake_i = physical_to_internal(steer, throttle, brake)
    if resolved_device is not None:
        base["device_status_before_update"] = get_vjd_status(resolved_device)
    reacquired = False
    with VJOY_WRITE_LOCK:
        try:
            def _write_once() -> bool:
                local_controls.set_controls(steer=steer_i, acc=acc_i, brake=brake_i)
                return _feeder_update_ok(local_controls)

            update_ok = _write_once()
            if not update_ok and allow_reacquire and resolved_device is not None:
                re_ok, re_err = reacquire_vjoy_device(
                    local_controls,
                    resolved_device,
                    retries=DEFAULT_VJOY_ACQUIRE_RETRIES,
                )
                reacquired = re_ok
                if not re_ok:
                    base["error"] = re_err or "reacquire_failed"
                    base["canonical_controls_called"] = True
                    return base
                update_ok = _write_once()
        except Exception as exc:
            base["error"] = str(exc)
            base["canonical_controls_called"] = True
            return base
    base["canonical_controls_called"] = True
    base["update_vjd_result"] = bool(update_ok)
    if not update_ok:
        base["error"] = (
            "canonical_vjoy_update_failed_after_reacquire"
            if reacquired
            else "UpdateVJD failed"
        )
        return base
    return {
        "ok": True,
        "error": "",
        "physical_action": [float(steer), float(throttle), float(brake)],
        "steer_internal": steer_i,
        "acc_internal": acc_i,
        "brake_internal": brake_i,
        "canonical_controls_called": True,
        "canonical_controls_object_id": id(local_controls),
        "canonical_local_controls_object_id": id(local_controls),
        "device_status_before_update": base["device_status_before_update"],
        "update_vjd_result": True,
        "vjoy_device_id": resolved_device,
        "vjoy_object_id": id(getattr(local_controls, "vj", None)) if getattr(local_controls, "vj", None) else None,
    }


def apply_physical_controls(
    env: Any,
    steer: float,
    throttle: float,
    brake: float,
    *,
    device_id: int | None = None,
) -> dict[str, Any]:
    return apply_canonical_controls(
        get_local_controls(env),
        steer,
        throttle,
        brake,
        device_id=device_id,
    )


def neutralize_canonical(local_controls: Any, *, device_id: int | None = None) -> dict[str, Any]:
    return apply_canonical_controls(
        local_controls,
        0.0,
        0.0,
        0.0,
        device_id=device_id,
        allow_reacquire=True,
    )


def expected_vjoy_for_internal(steer: float, acc: float, brake: float) -> dict[str, int]:
    vjoy_steer = steer + 1.0
    vjoy_acc = (acc + 1.0) / 2.0
    vjoy_brake = (brake + 1.0) / 2.0
    return {
        "wAxisX": int(vjoy_steer * SCALE),
        "wAxisY": int(vjoy_acc * 2 * SCALE),
        "wAxisZ": int(vjoy_brake * 2 * SCALE),
        "lButtons": 0,
    }
