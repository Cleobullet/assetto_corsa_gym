"""Runtime configuration, locking and manifest helpers for multi-AC workers (C4B+)."""
from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKERS_CONFIG_DIR = REPO_ROOT / "config" / "workers"
MANIFEST_PATH = REPO_ROOT / "ac_workers" / "manifest.json"
LOCKS_DIR = REPO_ROOT / "ac_workers" / "locks"

ENV_WORKER_ID = "ACGYM_WORKER_ID"
ENV_EGO_PORT = "ACGYM_EGO_PORT"
ENV_OPPONENTS_PORT = "ACGYM_OPPONENTS_PORT"
ENV_MGMT_PORT = "ACGYM_MGMT_PORT"
ENV_VJOY_DEVICE = "ACGYM_VJOY_DEVICE"


@dataclass(frozen=True)
class WorkerConfig:
    worker_id: int
    remote_machine_ip: str
    ego_server_port: int
    opponents_server_port: int
    simulation_management_server_port: int
    vjoy_device_id: int
    log_dir: str
    lock_file: str

    @property
    def log_path(self) -> Path:
        return REPO_ROOT / self.log_dir / "ac_worker.log"

    @property
    def lock_path(self) -> Path:
        return REPO_ROOT / self.lock_file


def worker_config_path(worker_id: int) -> Path:
    return WORKERS_CONFIG_DIR / f"worker_{worker_id}.yml"


def load_worker_config(worker_id: int) -> WorkerConfig:
    path = worker_config_path(worker_id)
    if not path.is_file():
        raise FileNotFoundError(f"Worker config not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    cfg = WorkerConfig(
        worker_id=int(data["worker_id"]),
        remote_machine_ip=str(data.get("remote_machine_ip", "localhost")),
        ego_server_port=int(data["ego_server_port"]),
        opponents_server_port=int(data["opponents_server_port"]),
        simulation_management_server_port=int(data["simulation_management_server_port"]),
        vjoy_device_id=int(data["vjoy_device_id"]),
        log_dir=str(data["log_dir"]),
        lock_file=str(data["lock_file"]),
    )
    if cfg.worker_id != worker_id:
        raise ValueError(f"worker_id mismatch in {path}: expected {worker_id}, got {cfg.worker_id}")
    return cfg


def load_worker_configs(worker_ids: Iterable[int]) -> list[WorkerConfig]:
    return [load_worker_config(wid) for wid in worker_ids]


def validate_worker_set(configs: Iterable[WorkerConfig]) -> None:
    configs = list(configs)
    if not configs:
        raise ValueError("At least one worker config is required")

    ids = [c.worker_id for c in configs]
    if len(ids) != len(set(ids)):
        raise ValueError(f"Duplicate worker_id in set: {ids}")

    ego_ports = [c.ego_server_port for c in configs]
    opp_ports = [c.opponents_server_port for c in configs]
    mgmt_ports = [c.simulation_management_server_port for c in configs]
    vjoy_ids = [c.vjoy_device_id for c in configs]

    for label, values in (
        ("ego_server_port", ego_ports),
        ("opponents_server_port", opp_ports),
        ("simulation_management_server_port", mgmt_ports),
        ("vjoy_device_id", vjoy_ids),
    ):
        if len(values) != len(set(values)):
            raise ValueError(f"Duplicate {label} across workers: {values}")


def build_worker_env(cfg: WorkerConfig) -> dict[str, str]:
    return {
        ENV_WORKER_ID: str(cfg.worker_id),
        ENV_EGO_PORT: str(cfg.ego_server_port),
        ENV_OPPONENTS_PORT: str(cfg.opponents_server_port),
        ENV_MGMT_PORT: str(cfg.simulation_management_server_port),
        ENV_VJOY_DEVICE: str(cfg.vjoy_device_id),
    }


def apply_worker_env(cfg: WorkerConfig) -> dict[str, str | None]:
    """Set process environment for the current worker; returns previous values."""
    env = build_worker_env(cfg)
    previous: dict[str, str | None] = {}
    for key, value in env.items():
        previous[key] = os.environ.get(key)
        os.environ[key] = value
    return previous


def restore_worker_env(previous: dict[str, str | None]) -> None:
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _lock_payload(cfg: WorkerConfig, pid: int, role: str) -> dict:
    return {
        "worker_id": cfg.worker_id,
        "pid": pid,
        "role": role,
        "ports": {
            "ego": cfg.ego_server_port,
            "opponents": cfg.opponents_server_port,
            "mgmt": cfg.simulation_management_server_port,
        },
        "vjoy_device_id": cfg.vjoy_device_id,
        "acquired_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_worker_lock(cfg: WorkerConfig, pid: int, role: str = "ac") -> Path:
    LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = cfg.lock_path
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if lock_path.exists():
        try:
            existing = json.loads(lock_path.read_text(encoding="utf-8"))
            existing_pid = int(existing.get("pid", -1))
            if existing_pid > 0 and _pid_alive(existing_pid):
                raise RuntimeError(
                    f"Worker {cfg.worker_id} lock held by pid {existing_pid}: {lock_path}"
                )
        except json.JSONDecodeError:
            raise RuntimeError(f"Corrupt lock file: {lock_path}")
        lock_path.unlink(missing_ok=True)

    payload = _lock_payload(cfg, pid, role)
    tmp = lock_path.with_suffix(".lock.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, lock_path)
    return lock_path


def release_worker_lock(cfg: WorkerConfig) -> None:
    lock_path = cfg.lock_path
    if lock_path.is_file():
        lock_path.unlink()


def read_manifest() -> dict:
    if not MANIFEST_PATH.is_file():
        return {"schemaVersion": 1, "startedAtUtc": None, "aiInstallPath": None, "workers": []}
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8-sig"))


def write_manifest(manifest: dict) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    os.replace(tmp, MANIFEST_PATH)


def worker_manifest_entry(cfg: WorkerConfig, ac_pid: int | None, collector_pid: int | None = None) -> dict:
    return {
        "workerId": cfg.worker_id,
        "acPid": ac_pid,
        "collectorPid": collector_pid,
        "ports": {
            "ego": cfg.ego_server_port,
            "opponents": cfg.opponents_server_port,
            "mgmt": cfg.simulation_management_server_port,
        },
        "vjoyDeviceId": cfg.vjoy_device_id,
        "logPath": str(cfg.log_path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "lockPath": str(cfg.lock_path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "lastTelemetryMessage": None,
        "state": "running" if ac_pid else "prepared",
    }


def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def validate_manifest(manifest: dict) -> None:
    if manifest.get("schemaVersion") != 1:
        raise ValueError("manifest schemaVersion must be 1")
    workers = manifest.get("workers", [])
    ports_ego, ports_opp, ports_mgmt, vjoy_ids, worker_ids = [], [], [], [], []
    for w in workers:
        worker_ids.append(int(w["workerId"]))
        ports_ego.append(int(w["ports"]["ego"]))
        ports_opp.append(int(w["ports"]["opponents"]))
        ports_mgmt.append(int(w["ports"]["mgmt"]))
        vjoy_ids.append(int(w["vjoyDeviceId"]))
    validate_worker_set([
        WorkerConfig(
            worker_id=wid,
            remote_machine_ip="localhost",
            ego_server_port=ego,
            opponents_server_port=opp,
            simulation_management_server_port=mgmt,
            vjoy_device_id=vjoy,
            log_dir=".",
            lock_file=".",
        )
        for wid, ego, opp, mgmt, vjoy in zip(worker_ids, ports_ego, ports_opp, ports_mgmt, vjoy_ids)
    ])


def read_plugin_worker_config() -> dict:
    """Mirror of sensors_par/config.py runtime env resolution (for offline checks)."""
    defaults = {
        "worker_id": 0,
        "ego_server_port": 2345,
        "opponents_server_port": 2346,
        "simulation_management_server_port": 2347,
        "vjoy_device_id": 1,
    }
    defaults["worker_id"] = int(os.environ.get(ENV_WORKER_ID, "0"))
    if ENV_EGO_PORT in os.environ:
        defaults["ego_server_port"] = int(os.environ[ENV_EGO_PORT])
    if ENV_OPPONENTS_PORT in os.environ:
        defaults["opponents_server_port"] = int(os.environ[ENV_OPPONENTS_PORT])
    if ENV_MGMT_PORT in os.environ:
        defaults["simulation_management_server_port"] = int(os.environ[ENV_MGMT_PORT])
    if ENV_VJOY_DEVICE in os.environ:
        defaults["vjoy_device_id"] = int(os.environ[ENV_VJOY_DEVICE])
    return defaults


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write-temp-then-rename helper (policy weights, manifest, locks)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
