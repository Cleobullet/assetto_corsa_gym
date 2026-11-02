"""Port and vJoy mapping for workers 0-5 (C4D)."""
from __future__ import annotations

from dataclasses import dataclass

from ac_workers.vjoy_pool import MAX_WORKERS, device_for_worker


@dataclass(frozen=True)
class WorkerPorts:
    worker_id: int
    ego: int
    opponents: int
    mgmt: int
    vjoy: int
    diag: int


def worker_ports(worker_id: int) -> WorkerPorts:
    if worker_id < 0 or worker_id >= MAX_WORKERS:
        raise ValueError(f"worker_id out of range 0-{MAX_WORKERS - 1}: {worker_id}")
    ego = 2345 + worker_id * 10
    return WorkerPorts(
        worker_id=worker_id,
        ego=ego,
        opponents=ego + 1,
        mgmt=ego + 2,
        vjoy=device_for_worker(worker_id),
        diag=24100 + worker_id,
    )


def worker_ports_dict(worker_id: int) -> dict[str, int]:
    p = worker_ports(worker_id)
    return {
        "ego": p.ego,
        "opponents": p.opponents,
        "mgmt": p.mgmt,
        "vjoy": p.vjoy,
        "diag": p.diag,
        "transition": 24050,
    }
