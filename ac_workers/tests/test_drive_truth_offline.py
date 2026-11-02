"""Offline tests for the single Drive truth source (ac_workers/drive_truth.py)."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from ac_workers.drive_truth import (  # noqa: E402
    REASON_MENU_BRAKE_HOLD,
    REASON_NO_STATE,
    REASON_NOT_LIVE,
    REASON_OK,
    REASON_TELEMETRY_STALE,
    REASON_VJOY_NOT_ACK,
    is_drive_really_active,
    is_gas_without_movement,
    menu_brake_hold,
    movement_watchdog_tripped,
)


def _live_drive(brake=0.0):
    return {"sim_live": True, "physics_brake": brake}


def test_all_conditions_true_is_active():
    ok, reason = is_drive_really_active(
        _live_drive(brake=0.0), vjoy_readback_ok=True, telemetry_age_sec=0.1
    )
    assert ok is True
    assert reason == REASON_OK


def test_no_state_is_not_active():
    ok, reason = is_drive_really_active({}, vjoy_readback_ok=True, telemetry_age_sec=0.0)
    assert ok is False
    assert reason == REASON_NO_STATE


def test_not_live_is_not_active():
    ok, reason = is_drive_really_active(
        {"sim_live": False, "physics_brake": 0.0},
        vjoy_readback_ok=True,
        telemetry_age_sec=0.0,
    )
    assert ok is False
    assert reason == REASON_NOT_LIVE


def test_menu_brake_hold_blocks():
    ok, reason = is_drive_really_active(
        _live_drive(brake=1.0), vjoy_readback_ok=True, telemetry_age_sec=0.0
    )
    assert ok is False
    assert reason == REASON_MENU_BRAKE_HOLD


def test_vjoy_readback_required():
    ok, reason = is_drive_really_active(
        _live_drive(brake=0.0), vjoy_readback_ok=False, telemetry_age_sec=0.0
    )
    assert ok is False
    assert reason == REASON_VJOY_NOT_ACK


def test_stale_telemetry_blocks():
    ok, reason = is_drive_really_active(
        _live_drive(brake=0.0), vjoy_readback_ok=True, telemetry_age_sec=5.0
    )
    assert ok is False
    assert reason == REASON_TELEMETRY_STALE


def test_ac_status_fallback_when_no_sim_live_key():
    # Older plugin build without sim_live: fall back to ac_status == AC_LIVE (2).
    ok, _r = is_drive_really_active(
        {"ac_status": 2, "physics_brake": 0.0},
        vjoy_readback_ok=True,
        telemetry_age_sec=0.0,
    )
    assert ok is True
    ok2, reason2 = is_drive_really_active(
        {"ac_status": 3, "physics_brake": 0.0},  # AC_PAUSE
        vjoy_readback_ok=True,
        telemetry_age_sec=0.0,
    )
    assert ok2 is False
    assert reason2 == REASON_NOT_LIVE


def test_missing_brake_treated_as_menu():
    # Fail safe: unknown brake -> assume still in menu (never start the policy blind).
    assert menu_brake_hold(None) is True
    ok, reason = is_drive_really_active(
        {"sim_live": True}, vjoy_readback_ok=True, telemetry_age_sec=0.0
    )
    assert ok is False
    assert reason == REASON_MENU_BRAKE_HOLD


def test_pedal_from_state_prefers_acc_status():
    from ac_workers.drive_truth import pedal_from_state, throttle_commanded_but_unacked

    assert pedal_from_state({"accStatus": 0.7, "gas": 0.0}) == 0.7
    assert pedal_from_state({"gas": 0.4}) == 0.4
    assert pedal_from_state({}) == 0.0
    assert throttle_commanded_but_unacked(0.8, 0.0) is True
    assert throttle_commanded_but_unacked(0.8, 0.5) is False
    assert throttle_commanded_but_unacked(-1.0, 0.0) is False


def test_movement_watchdog_gas_without_movement():
    # Real throttle but car not moving -> gas-without-movement.
    assert is_gas_without_movement(0.8, 0.0) is True
    assert is_gas_without_movement(0.8, 0.1) is True
    # Moving -> not stalled.
    assert is_gas_without_movement(0.8, 3.0) is False
    # No throttle -> not a stall (car may simply be coasting/braking).
    assert is_gas_without_movement(0.0, 0.0) is False


def test_movement_watchdog_trip_threshold():
    assert movement_watchdog_tripped(0) is False
    assert movement_watchdog_tripped(39) is False
    assert movement_watchdog_tripped(40) is True
    assert movement_watchdog_tripped(100) is True


def main() -> int:
    tests = [
        test_all_conditions_true_is_active,
        test_no_state_is_not_active,
        test_not_live_is_not_active,
        test_menu_brake_hold_blocks,
        test_vjoy_readback_required,
        test_stale_telemetry_blocks,
        test_ac_status_fallback_when_no_sim_live_key,
        test_missing_brake_treated_as_menu,
        test_pedal_from_state_prefers_acc_status,
        test_movement_watchdog_gas_without_movement,
        test_movement_watchdog_trip_threshold,
    ]
    for fn in tests:
        fn()
        print("OK", fn.__name__)
    print("Test-DriveTruth-Offline: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
