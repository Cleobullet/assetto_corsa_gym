"""Offline tests for the centralized vJoy device pool + ownership guards."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from ac_workers.vjoy_pool import (  # noqa: E402
    MAX_WORKERS,
    STATUS_ACTIVE,
    STATUS_DEAD,
    STATUS_DEGRADED,
    STATUS_RECOVERING,
    VJoyOwnershipError,
    assert_sole_owner,
    device_for_worker,
    pool_devices,
    reset_policy,
    validate_pool_mapping,
    vjoy_failure_is_dead,
    vjoy_worker_status,
    worker_for_device,
)


def test_pool_is_full_six_worker_set():
    assert MAX_WORKERS == 6
    assert pool_devices() == [1, 2, 3, 4, 5, 6]


def test_deterministic_mapping_round_trips_all_workers():
    for wid in range(MAX_WORKERS):
        dev = device_for_worker(wid)
        assert dev == wid + 1
        assert worker_for_device(dev) == wid


def test_mapping_rejects_out_of_range_worker():
    for bad in (-1, MAX_WORKERS, 99):
        try:
            device_for_worker(bad)
        except VJoyOwnershipError:
            continue
        raise AssertionError("expected VJoyOwnershipError for worker {0}".format(bad))


def test_worker_for_device_rejects_unknown_device():
    for bad in (0, 7, 99):
        try:
            worker_for_device(bad)
        except VJoyOwnershipError:
            continue
        raise AssertionError("expected VJoyOwnershipError for device {0}".format(bad))


def test_validate_mapping_three_worker_subset():
    # A 3-worker mini-test is just the natural subset of the 6-device pool.
    assert validate_pool_mapping([0, 1, 2]) == {0: 1, 1: 2, 2: 3}


def test_validate_mapping_full_six():
    assert validate_pool_mapping(list(range(6))) == {0: 1, 1: 2, 2: 3, 3: 4, 4: 5, 5: 6}


def test_validate_mapping_rejects_duplicate_worker():
    try:
        validate_pool_mapping([0, 1, 1])
    except VJoyOwnershipError:
        return
    raise AssertionError("expected duplicate-worker rejection")


def test_sole_owner_ok_when_unowned():
    assert_sole_owner(
        1,
        2,
        self_pid=1000,
        read_owner_fn=lambda _d: None,
        pid_alive_fn=lambda _p: True,
    )


def test_sole_owner_ok_when_owned_by_self():
    assert_sole_owner(
        1,
        2,
        self_pid=1000,
        read_owner_fn=lambda _d: {"pid": 1000, "worker_id": 1},
        pid_alive_fn=lambda _p: True,
    )


def test_sole_owner_ok_when_prior_owner_dead():
    # Stale ledger entry from a crashed prior run -> not a conflict.
    assert_sole_owner(
        1,
        2,
        self_pid=1000,
        read_owner_fn=lambda _d: {"pid": 555, "worker_id": 1},
        pid_alive_fn=lambda _p: False,
    )


def test_sole_owner_fails_fast_on_live_foreign_owner():
    try:
        assert_sole_owner(
            1,
            2,
            self_pid=1000,
            read_owner_fn=lambda _d: {"pid": 2222, "worker_id": 3},
            pid_alive_fn=lambda _p: True,
        )
    except VJoyOwnershipError:
        return
    raise AssertionError("expected fail-fast on live foreign owner")


def test_sole_owner_rejects_wrong_device_for_worker():
    try:
        assert_sole_owner(
            1,
            5,  # worker 1 must own device 2, not 5
            self_pid=1000,
            read_owner_fn=lambda _d: None,
            pid_alive_fn=lambda _p: True,
        )
    except VJoyOwnershipError:
        return
    raise AssertionError("expected rejection of wrong device for worker")


def test_state_machine_thresholds():
    assert vjoy_worker_status(0) == STATUS_ACTIVE
    assert vjoy_worker_status(1) == STATUS_DEGRADED
    assert vjoy_worker_status(2) == STATUS_DEGRADED
    assert vjoy_worker_status(3) == STATUS_RECOVERING
    assert vjoy_worker_status(4) == STATUS_RECOVERING
    assert vjoy_worker_status(5) == STATUS_DEAD
    assert vjoy_worker_status(99) == STATUS_DEAD


def test_failure_is_dead_only_at_threshold():
    assert vjoy_failure_is_dead(4) is False
    assert vjoy_failure_is_dead(5) is True


def test_reset_policy_local_only_while_live():
    assert reset_policy(worker_live=True, severity="EMERGENCY") == "LOCAL_RECOVERY_ONLY"
    assert reset_policy(worker_live=True) == "LOCAL_RECOVERY_ONLY"


def test_reset_policy_global_only_offline_emergency():
    assert reset_policy(worker_live=False, severity="EMERGENCY") == "ALLOW_GLOBAL_RESET"
    assert reset_policy(worker_live=False, severity="") == "DENY"


def main() -> int:
    tests = [
        test_pool_is_full_six_worker_set,
        test_deterministic_mapping_round_trips_all_workers,
        test_mapping_rejects_out_of_range_worker,
        test_worker_for_device_rejects_unknown_device,
        test_validate_mapping_three_worker_subset,
        test_validate_mapping_full_six,
        test_validate_mapping_rejects_duplicate_worker,
        test_sole_owner_ok_when_unowned,
        test_sole_owner_ok_when_owned_by_self,
        test_sole_owner_ok_when_prior_owner_dead,
        test_sole_owner_fails_fast_on_live_foreign_owner,
        test_sole_owner_rejects_wrong_device_for_worker,
        test_state_machine_thresholds,
        test_failure_is_dead_only_at_threshold,
        test_reset_policy_local_only_while_live,
        test_reset_policy_global_only_offline_emergency,
    ]
    for fn in tests:
        fn()
        print("OK", fn.__name__)
    print("Test-VJoyPool-Offline: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
