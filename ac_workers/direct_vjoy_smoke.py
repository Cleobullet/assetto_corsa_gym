"""Standalone vJoy smoke test — no collector, telemetry, reset, or management server."""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))

from ac_workers.control_smoke_test import (  # noqa: E402
    MAX_DURATION_SEC,
    THROTTLE_PEDAL_FRACTION,
    build_requested_internal_action,
)
from ac_workers.vjoy_session import (  # noqa: E402
    VJD_STAT_OWN,
    VJOY_PATH,
    FeederReadback,
    apply_neutral_controls,
    attach_feeder_readback,
    axes_to_logical,
    decode_joystick_position,
    device_acquired,
    expected_logical_neutral,
    get_vjd_status,
    neutralize_controls_handle,
    pulse_gear_up,
    push_vjoy_state,
    verify_logical_match,
    write_and_verify,
)
from ac_workers.worker_ports import worker_ports  # noqa: E402
from AssettoCorsaEnv.ac_worker_runtime import load_worker_config, worker_config_path  # noqa: E402

logger = logging.getLogger(__name__)

WORKER_ID_ONLY = 0
MAX_LOOP_ITERATIONS = 200
MAX_GEAR_PULSE_WAIT_SEC = 0.3
AXIS_TOLERANCE = 0.02
AC_NOT_RUNNING_MSG = (
    "Assetto Corsa is not running; open a race manually before this test"
)

# Re-export for offline tests.
JOYSTICK_POSITION_FMT = "BlllllllllllllllllllIIII"
SCALE = 16384


def expected_logical_throttle() -> dict[str, float | int]:
    action = build_requested_internal_action()
    steer = float(action[0])
    acc = float(action[1])
    brake = float(action[2])
    return {
        "steer_vjoy": steer + 1.0,
        "acc_pedal": (acc + 1.0) / 2.0,
        "brake_pedal": (brake + 1.0) / 2.0,
        "buttons": 0,
    }


def acs_process_running(*, check_fn: Callable[[], bool] | None = None) -> bool:
    if check_fn is not None:
        return bool(check_fn())
    if os.name != "nt":
        return False
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq acs.exe", "/NH"],
            stderr=subprocess.DEVNULL,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "acs.exe" in out.lower()


def resolve_worker_mapping(worker_id: int) -> dict[str, Any]:
    cfg = load_worker_config(worker_id)
    ports = worker_ports(worker_id)
    if cfg.vjoy_device_id != ports.vjoy:
        raise ValueError(
            "vjoy_device_id mismatch worker {0}: config={1} ports={2}".format(
                worker_id, cfg.vjoy_device_id, ports.vjoy
            )
        )
    return {
        "worker_id": worker_id,
        "vjoy_device_id": int(cfg.vjoy_device_id),
        "config_path": str(worker_config_path(worker_id)),
        "vjoy_path": VJOY_PATH,
        "pedal_fraction": THROTTLE_PEDAL_FRACTION,
    }


def resolve_gear_mapping(worker_id: int) -> tuple[bool, str, dict[str, Any]]:
    from AssettoCorsaEnv.gear_shift import resolve_shift_up_mapping

    gear = resolve_shift_up_mapping(worker_id)
    if not gear.get("valid"):
        return False, gear.get("error") or "GEARUP mapping invalid", gear
    return True, "", gear


def validate_mapping(
    worker_id: int,
    *,
    vjoy_accessible_fn: Callable[[int], bool] | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    if worker_id != WORKER_ID_ONLY:
        return False, "only worker 0 is supported", {}
    try:
        mapping = resolve_worker_mapping(worker_id)
    except (FileNotFoundError, ValueError, KeyError, TypeError) as exc:
        return False, "invalid worker mapping: {0}".format(exc), {}
    device_id = int(mapping["vjoy_device_id"])
    check = vjoy_accessible_fn
    if check is None:
        from ac_workers.worker_handshake import vjoy_accessible

        check = vjoy_accessible
    if not check(device_id):
        return False, "vJoy device {0} not accessible".format(device_id), mapping
    return True, "", mapping


def open_controls(device_id: int) -> Any:
    os.environ["ACGYM_VJOY_DEVICE"] = str(device_id)
    from AssettoCorsaPlugin.plugins.sensors_par.car_control import Controls

    return Controls()


def neutralize(
    ctrl: Any | None,
    device_id: int,
    readback_snapshot: Callable[[], FeederReadback] | None = None,
    *,
    neutral_vjoy_fn: Callable[[int], bool] | None = None,
) -> tuple[dict[str, float | int] | None, FeederReadback | None]:
    rb: FeederReadback | None = None
    if ctrl is not None:
        ok, _reason, rb = neutralize_controls_handle(ctrl, readback_snapshot, close=True)
        if not ok and neutral_vjoy_fn is not None:
            try:
                neutral_vjoy_fn(device_id)
            except Exception:
                pass
    elif neutral_vjoy_fn is not None:
        try:
            neutral_vjoy_fn(device_id)
        except Exception:
            pass
    elif device_id > 0:
        try:
            from ac_workers.collector_safety import neutral_vjoy

            neutral_vjoy(device_id)
        except Exception:
            pass
    return expected_logical_neutral(), rb


def run_direct_vjoy_smoke(
    worker_id: int,
    *,
    open_controls_fn: Callable[[int], Any] | None = None,
    vjoy_accessible_fn: Callable[[int], bool] | None = None,
    resolve_gear_fn: Callable[[int], tuple[bool, str, dict[str, Any]]] | None = None,
    get_status_fn: Callable[[int], int] | None = None,
    neutral_vjoy_fn: Callable[[int], bool] | None = None,
    acs_running_fn: Callable[[], bool] | None = None,
    monotonic: Callable[[], float] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    monotonic = monotonic or time.monotonic
    sleep_fn = sleep_fn or time.sleep
    get_status_fn = get_status_fn or get_vjd_status
    resolve_gear_fn = resolve_gear_fn or resolve_gear_mapping

    report: dict[str, Any] = {
        "worker_id": worker_id,
        "commands_sent": False,
        "result": "failed",
        "error": "",
        "duration_sec": 0.0,
        "vjoy_path": VJOY_PATH,
        "ac_running": False,
        "device_acquired": False,
        "gear_up_button": -1,
        "gear_up_pulse_sent": False,
        "axis_write_verified": False,
        "neutralization_verified": False,
    }

    report["ac_running"] = acs_process_running(check_fn=acs_running_fn)
    if not report["ac_running"]:
        report["error"] = AC_NOT_RUNNING_MSG
        return report

    ok, reason, mapping = validate_mapping(worker_id, vjoy_accessible_fn=vjoy_accessible_fn)
    report["mapping"] = mapping
    if not ok:
        report["error"] = reason or "mapping invalid"
        return report

    gear_ok, gear_reason, gear_mapping = resolve_gear_fn(worker_id)
    report["gear_mapping"] = gear_mapping
    report["gear_up_button"] = int(gear_mapping.get("vjoy_button_number", -1))
    if not gear_ok:
        report["error"] = gear_reason or "GEARUP mapping invalid"
        return report

    device_id = int(mapping["vjoy_device_id"])
    gear_mask = int(gear_mapping.get("vjoy_button_mask", 0))
    opener = open_controls_fn or open_controls
    ctrl = None
    readback_snapshot: Callable[[], FeederReadback] | None = None
    started = monotonic()
    commands_sent = False
    axis_write_verified = False

    try:
        ctrl = opener(device_id)
        readback_snapshot = attach_feeder_readback(ctrl.vj)
        report["device_acquired"] = device_acquired(ctrl, device_id, get_status_fn=get_status_fn)
        if not report["device_acquired"]:
            report["error"] = "vJoy device {0} not acquired (status={1})".format(
                device_id, get_status_fn(device_id)
            )
            return report

        apply_neutral_controls(ctrl)
        report["before_requested"] = expected_logical_neutral()
        report["before_readback"] = dict(readback_snapshot().last_logical)
        ok, reason, _rb = write_and_verify(readback_snapshot, report["before_requested"])
        if not ok:
            report["error"] = "before neutral readback: {0}".format(reason)
            return report

        pulse_ok, pulse_reason = pulse_gear_up(ctrl, gear_mask, readback_snapshot)
        report["gear_up_pulse_sent"] = pulse_ok
        if not pulse_ok:
            report["error"] = pulse_reason or "gear up pulse failed"
            return report
        sleep_fn(min(MAX_GEAR_PULSE_WAIT_SEC, 0.3))

        throttle_expected = expected_logical_throttle()
        action = build_requested_internal_action()
        report["requested_action_internal"] = [float(x) for x in action.ravel()[:3]]
        ctrl.set_controls(float(action[0]), float(action[1]), float(action[2]))
        commands_sent = True
        report["during_requested"] = throttle_expected
        report["during_readback"] = dict(readback_snapshot().last_logical)
        ok, reason, _rb = write_and_verify(readback_snapshot, throttle_expected)
        if not ok:
            report["error"] = "throttle readback: {0}".format(reason)
            return report
        axis_write_verified = True

        deadline = started + MAX_DURATION_SEC
        iterations = 0
        while monotonic() < deadline and iterations < MAX_LOOP_ITERATIONS:
            iterations += 1
            sleep_fn(0.05)

        report["commands_sent"] = commands_sent
        report["axis_write_verified"] = axis_write_verified
        report["result"] = "ok"
    except Exception as exc:
        report["result"] = "failed"
        report["error"] = str(exc) or "direct vJoy smoke exception"
        logger.exception("direct vJoy smoke worker=%s failed", worker_id)
    finally:
        report["duration_sec"] = max(0.0, monotonic() - started)
        report["commands_sent"] = commands_sent
        report["axis_write_verified"] = axis_write_verified
        after_requested, after_rb = neutralize(
            ctrl,
            device_id,
            readback_snapshot,
            neutral_vjoy_fn=neutral_vjoy_fn,
        )
        if after_requested is not None:
            report["after_requested"] = after_requested
        if after_rb is not None:
            report["after_readback"] = dict(after_rb.last_logical)
            ok, reason = verify_logical_match(after_requested or {}, after_rb.last_logical)
            report["neutralization_verified"] = ok
            if not ok and not report.get("error"):
                report["error"] = "neutralization readback: {0}".format(reason)
        elif readback_snapshot is not None:
            report["after_readback"] = dict(readback_snapshot().last_logical)
            ok, reason = verify_logical_match(after_requested or {}, report["after_readback"])
            report["neutralization_verified"] = ok
            if not ok and not report.get("error"):
                report["error"] = "neutralization readback: {0}".format(reason)

        if report["result"] == "ok":
            if not report.get("ac_running"):
                report["result"] = "failed"
                report["error"] = report.get("error") or AC_NOT_RUNNING_MSG
            elif not report.get("device_acquired"):
                report["result"] = "failed"
                report["error"] = report.get("error") or "device not acquired"
            elif not report.get("gear_up_pulse_sent"):
                report["result"] = "failed"
                report["error"] = report.get("error") or "gear up pulse not verified"
            elif not report.get("axis_write_verified"):
                report["result"] = "failed"
                report["error"] = report.get("error") or "axis write not verified"
            elif not report.get("neutralization_verified"):
                report["result"] = "failed"
                report["error"] = report.get("error") or "neutralization not verified"
            elif not commands_sent:
                report["result"] = "failed"
                report["error"] = report.get("error") or "throttle command not sent"

        if report["result"] == "failed" and not report.get("error"):
            report["error"] = "direct vJoy smoke failed without explicit cause"

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Direct vJoy smoke (AC already on track; no collector/reset/telemetry)."
    )
    parser.add_argument("--worker-id", type=int, default=WORKER_ID_ONLY)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    report = run_direct_vjoy_smoke(args.worker_id)

    print("=== DIRECT VJOY SMOKE ===")
    print(
        "worker_id={0} result={1} ac_running={2} commands_sent={3} "
        "device_acquired={4} gear_up_pulse_sent={5} axis_write_verified={6} "
        "neutralization_verified={7}".format(
            report.get("worker_id"),
            report.get("result"),
            report.get("ac_running"),
            report.get("commands_sent"),
            report.get("device_acquired"),
            report.get("gear_up_pulse_sent"),
            report.get("axis_write_verified"),
            report.get("neutralization_verified"),
        )
    )
    for phase in ("before", "during", "after"):
        req = report.get("{0}_requested".format(phase))
        rb = report.get("{0}_readback".format(phase))
        if req:
            print("{0}_requested:".format(phase), req)
        if rb:
            print("{0}_readback:".format(phase), rb)
    if report.get("error"):
        print("error:", report["error"])
    print(json.dumps(report, indent=2, sort_keys=True))

    return 0 if report.get("result") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
