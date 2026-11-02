"""Centralized vJoy device pool + deterministic worker<->device ownership.

This module is the single source of truth for "which worker drives which vJoy
device" and for the per-worker vJoy failure state machine. It replaces the
``worker_id + 1`` literals that were scattered across the codebase so that scaling
from a 3-worker mini-test to the full 6-worker system is a configuration fact, not
an implicit assumption baked into call sites.

Design constraints (multi-PROCESS, not multi-thread):
  - Each collector runs in its own OS process, so a single in-memory pool object
    with a ``threading.Lock`` cannot enforce ownership across workers. The
    authoritative, crash-safe cross-process registry is the ``vjoy_ledger``
    (device -> owning pid/worker on disk). This module layers strict mapping +
    fail-fast guards on top of that registry.
  - Mapping is STRICT and DETERMINISTIC: worker ``w`` owns device ``w + 1`` for
    ``w`` in ``0..MAX_WORKERS-1``. A worker never borrows another worker's device,
    because the device is bound to that worker's gear preset (``Vjoy_W{w}.ini``)
    and lane; cross-worker reassignment would break gear mapping and collide.
  - Recovery is LOCAL ONLY at runtime: relinquish + reacquire the SAME device. The
    global ``ResetVJD`` (bounces the shared PDO -> all devices) is denied while any
    worker is live and is reserved for the offline janitor / emergency stop.
"""
from __future__ import annotations

from typing import Any, Callable

# Full production pool. A mini-test with workers 0..2 is simply the natural subset
# {1, 2, 3}; nothing else changes when scaling to the full set.
MAX_WORKERS = 6
VJOY_DEVICE_POOL: tuple[int, ...] = tuple(w + 1 for w in range(MAX_WORKERS))

# Per-worker vJoy failure state machine (consecutive runtime write failures).
#   0                     -> active
#   >= DEGRADED (1)       -> degraded   (one glitch; keep running)
#   >= RECOVERING (3)     -> recovering (local relinquish+reacquire via reset)
#   >= DEAD (5)           -> dead       (give up; stop the worker)
VJOY_DEGRADED_FAILS = 1
VJOY_RECOVERING_FAILS = 3
VJOY_DEAD_FAILS = 5

STATUS_ACTIVE = "active"
STATUS_DEGRADED = "degraded"
STATUS_RECOVERING = "recovering"
STATUS_DEAD = "dead"


class VJoyOwnershipError(RuntimeError):
    """Raised on a strict-ownership violation (fail-fast)."""


def pool_devices() -> list[int]:
    return list(VJOY_DEVICE_POOL)


def device_for_worker(worker_id: int) -> int:
    """Deterministic device id owned by ``worker_id`` (strict 1:1)."""
    wid = int(worker_id)
    if wid < 0 or wid >= MAX_WORKERS:
        raise VJoyOwnershipError(
            "worker_id out of range 0-{0}: {1}".format(MAX_WORKERS - 1, wid)
        )
    return wid + 1


def worker_for_device(device_id: int) -> int:
    dev = int(device_id)
    if dev not in VJOY_DEVICE_POOL:
        raise VJoyOwnershipError(
            "device_id not in pool {0}: {1}".format(list(VJOY_DEVICE_POOL), dev)
        )
    return dev - 1


def validate_pool_mapping(worker_ids: list[int]) -> dict[int, int]:
    """Fail-fast check that a launch set maps 1:1 onto distinct pool devices.

    Returns ``{worker_id: device_id}``. Raises ``VJoyOwnershipError`` on any
    duplicate worker, out-of-range worker, or device collision.
    """
    ids = [int(w) for w in worker_ids]
    if len(ids) != len(set(ids)):
        raise VJoyOwnershipError("duplicate worker_id in set: {0}".format(ids))
    mapping: dict[int, int] = {}
    seen_devices: dict[int, int] = {}
    for wid in ids:
        dev = device_for_worker(wid)
        if dev in seen_devices:
            raise VJoyOwnershipError(
                "device {0} claimed by workers {1} and {2}".format(
                    dev, seen_devices[dev], wid
                )
            )
        seen_devices[dev] = wid
        mapping[wid] = dev
    return mapping


def _default_pid_alive(pid: int) -> bool:
    try:
        from ac_workers.c4d_server_lock import pid_alive

        return bool(pid_alive(int(pid)))
    except Exception:
        return True


def assert_sole_owner(
    worker_id: int,
    device_id: int,
    *,
    self_pid: int,
    read_owner_fn: Callable[[int], dict | None] | None = None,
    pid_alive_fn: Callable[[int], bool] | None = None,
) -> None:
    """Fail-fast guard: this worker must hold exactly its mapped device, and no
    OTHER live process may own that device.

    Catches the "two workers touch the same device" race immediately instead of
    letting it degrade into a vJoy cascade. Pure/injectable for tests.
    """
    expected = device_for_worker(worker_id)
    if int(device_id) != expected:
        raise VJoyOwnershipError(
            "worker {0} bound to device {1} but pool maps it to {2}".format(
                worker_id, device_id, expected
            )
        )
    if read_owner_fn is None:
        from ac_workers.vjoy_ledger import read_owner as read_owner_fn  # type: ignore[assignment]
    pid_alive = pid_alive_fn or _default_pid_alive
    entry = read_owner_fn(int(device_id))
    if not entry:
        return
    owner_pid = int(entry.get("pid", -1))
    if owner_pid <= 0 or owner_pid == int(self_pid):
        return
    if pid_alive(owner_pid):
        raise VJoyOwnershipError(
            "device {0} already owned by live pid {1} (worker {2}); refusing to "
            "double-acquire".format(device_id, owner_pid, entry.get("worker_id"))
        )


def vjoy_worker_status(consecutive_fails: int) -> str:
    """Map a consecutive-failure count to a worker vJoy health state."""
    try:
        n = int(consecutive_fails)
    except (TypeError, ValueError):
        return STATUS_ACTIVE
    if n >= VJOY_DEAD_FAILS:
        return STATUS_DEAD
    if n >= VJOY_RECOVERING_FAILS:
        return STATUS_RECOVERING
    if n >= VJOY_DEGRADED_FAILS:
        return STATUS_DEGRADED
    return STATUS_ACTIVE


def vjoy_failure_is_dead(consecutive_fails: int) -> bool:
    """True once failures have persisted past local recovery -> stop the worker."""
    return vjoy_worker_status(consecutive_fails) == STATUS_DEAD


def reset_policy(worker_live: bool, severity: str = "") -> str:
    """Decide how a vJoy reset may be performed.

    LOCAL_RECOVERY_ONLY : a worker is live -> never bounce the shared PDO.
    ALLOW_GLOBAL_RESET  : nothing live and an emergency cleanup is requested.
    DENY                : otherwise.
    """
    if worker_live:
        return "LOCAL_RECOVERY_ONLY"
    if str(severity).upper() == "EMERGENCY":
        return "ALLOW_GLOBAL_RESET"
    return "DENY"
