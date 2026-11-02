"""Periodic vJoy garbage collector (reaper).

Reads the crash-safe ownership ledger and, for each recorded device:
  - owner process still alive  -> healthy, leave it alone;
  - owner dead, device acquirable now -> recovered, clear the ledger entry;
  - owner dead, device still stuck (FREE-but-unacquirable) -> leaked.

It then decides whether a global vJoy driver reset is needed AND safe. A reset
bounces the shared raw PDO -> ALL devices at once, so it is only safe when no
healthy (live-owner) device would be disrupted. The caller (Control Center) is
responsible for actually requesting the privileged janitor reset; the reaper stays
pure and fully injectable for testing.
"""
from __future__ import annotations

from typing import Any, Callable

from ac_workers.vjoy_ledger import clear_owner, read_all


def _default_pid_alive(pid: int) -> bool:
    try:
        from ac_workers.control_center import pid_alive

        return bool(pid_alive(int(pid)))
    except Exception:
        return True


def _default_probe(device_id: int) -> bool:
    """Best-effort: can device_id be acquired right now (i.e. it is clean)?"""
    try:
        import os
        import sys
        from importlib import reload
        from pathlib import Path

        repo = Path(__file__).resolve().parents[1]
        plugin = repo / "assetto_corsa_gym" / "AssettoCorsaPlugin" / "plugins" / "sensors_par"
        for p in (repo, repo / "assetto_corsa_gym", plugin):
            sp = str(p)
            if sp not in sys.path:
                sys.path.insert(0, sp)
        from ac_workers.vjoy_session import try_acquire_vjoy

        os.environ["ACGYM_VJOY_DEVICE"] = str(int(device_id))
        import vjoy as vjmod  # noqa: WPS433

        reload(vjmod)
        vj = vjmod.vJoy(reference=int(device_id))
        # NO global side effects: the reaper probe runs periodically in the
        # background while workers may be training. allow_global_reset=False so a
        # FREE-but-unacquirable probe never fires ResetVJD (which bounces the shared
        # PDO -> all devices) and silently kills live siblings.
        acquired, _err = try_acquire_vjoy(vj, int(device_id), allow_global_reset=False)
        try:
            vj.close()
        except Exception:
            pass
        return bool(acquired)
    except Exception:
        # If we cannot even probe, do not claim the device is clean.
        return False


def sweep(
    *,
    pid_alive_fn: Callable[[int], bool] | None = None,
    probe_fn: Callable[[int], bool] | None = None,
    clear_fn: Callable[..., bool] | None = None,
    entries: list[dict] | None = None,
) -> dict[str, Any]:
    """Classify ledger devices and decide if a global reset is needed + safe.

    Returns a dict:
      healthy:   devices with a live owner
      recovered: dead-owner devices that became acquirable (ledger cleared)
      leaked:    dead-owner devices still stuck
      needs_global_reset: bool (there is at least one leaked device)
      safe_to_reset:      bool (no healthy/live-owner device to disrupt)
    """
    pid_alive = pid_alive_fn or _default_pid_alive
    probe = probe_fn or _default_probe
    clear = clear_fn or clear_owner
    ledger = entries if entries is not None else read_all()

    healthy: list[int] = []
    recovered: list[int] = []
    leaked: list[int] = []

    for entry in ledger:
        try:
            device_id = int(entry.get("device_id"))
            pid = int(entry.get("pid", -1))
        except (TypeError, ValueError):
            continue
        if pid > 0 and pid_alive(pid):
            healthy.append(device_id)
            continue
        # Owner is gone — is the device clean (acquirable) or leaked?
        if probe(device_id):
            clear(device_id, force=True)
            recovered.append(device_id)
        else:
            leaked.append(device_id)

    needs_global_reset = bool(leaked)
    safe_to_reset = needs_global_reset and not healthy
    return {
        "healthy": sorted(set(healthy)),
        "recovered": sorted(set(recovered)),
        "leaked": sorted(set(leaked)),
        "needs_global_reset": needs_global_reset,
        "safe_to_reset": safe_to_reset,
    }
