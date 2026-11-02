"""Helpers for logging and comparing per-worker policy actions."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np

try:
    from ac_workers.canonical_controls import PEDAL_MAPPING_MODE
except Exception:  # pragma: no cover - keep diagnostics importable standalone
    PEDAL_MAPPING_MODE = "relu_zero_neutral"

WORKER_SEED_STRIDE = 10007


def worker_policy_seed(base_seed: int, worker_id: int) -> int:
    return int(base_seed) + int(worker_id) * WORKER_SEED_STRIDE


def observation_hash(obs: Any, *, decimals: int = 4) -> str:
    arr = np.asarray(obs, dtype=np.float32).ravel()
    if arr.size == 0:
        return "empty"
    q = np.round(arr, decimals=decimals)
    return hashlib.md5(q.tobytes()).hexdigest()[:12]


def action_hash(action: Any, *, decimals: int = 5) -> str:
    arr = np.asarray(action, dtype=np.float32).ravel()[:3]
    if arr.size == 0:
        return "empty"
    q = np.round(arr, decimals=decimals)
    return hashlib.md5(q.tobytes()).hexdigest()[:12]


def _read_worker_status(repo: Path, worker_id: int) -> dict[str, Any]:
    path = repo / "ac_workers" / "status" / ("worker_{0}.json".format(worker_id))
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def summarize_live_worker_actions(repo: Path, worker_ids: list[int] | None = None) -> str:
    """Build a C4D-friendly summary of raw actions across workers."""
    if worker_ids is None:
        worker_ids = list(range(6))
    rows: list[dict[str, Any]] = []
    for wid in worker_ids:
        st = _read_worker_status(repo, wid)
        raw = st.get("policy_raw_action") or st.get("last_requested_action") or []
        phys = st.get("physical_action") or st.get("last_executed_action") or []
        raw_brake = float(raw[2]) if raw and len(raw) >= 3 else None
        phys_brake = float(phys[2]) if phys and len(phys) >= 3 else None
        brake_forced = bool(
            st.get("brake_forced_by_safety", False)
            or st.get("safe_stop_active", False)
        )
        rows.append(
            {
                "worker_id": wid,
                "action_source": st.get("policy_action_source", ""),
                "policy_seed": st.get("policy_seed"),
                "exploration_seed": st.get("exploration_seed"),
                "deterministic_mode": st.get("deterministic_mode", False),
                "action_noise": st.get("action_noise", []),
                "safe_stop_active": st.get("safe_stop_active", False),
                "brake_forced_by_safety": st.get("brake_forced_by_safety", False),
                "observation_hash": st.get("observation_hash", ""),
                "raw_action": raw,
                "physical_action": phys,
                "raw_action_hash": action_hash(raw),
                "physical_action_hash": action_hash(phys),
                # Brake-specific diagnostics (raw vs physical, mapping, forcing).
                "raw_brake": raw_brake,
                "physical_brake": phys_brake,
                "brake_mapping_mode": PEDAL_MAPPING_MODE,
                "brake_forced_by_any_system": brake_forced,
                "brake_force_reason": st.get("safe_stop_reason", "") if brake_forced else "",
                "brake_source": st.get("policy_action_source", ""),
                "action_before_mapping": raw,
                "action_after_mapping": phys,
            }
        )
    raw_hashes = [r["raw_action_hash"] for r in rows if r["raw_action_hash"] != "empty"]
    phys_hashes = [r["physical_action_hash"] for r in rows if r["physical_action_hash"] != "empty"]
    obs_hashes = [r["observation_hash"] for r in rows if r["observation_hash"]]
    unique_raw = len(set(raw_hashes))
    unique_phys = len(set(phys_hashes))
    unique_obs = len(set(obs_hashes))
    seeds = [r["exploration_seed"] for r in rows if r.get("exploration_seed") is not None]
    unique_seeds = len(set(seeds))
    any_deterministic = any(bool(r.get("deterministic_mode")) for r in rows)
    training_mode = "deterministic" if any_deterministic else "stochastic"
    any_brake_forced = any(bool(r.get("brake_forced_by_any_system")) for r in rows)
    phys_brakes = [r["physical_brake"] for r in rows if r.get("physical_brake") is not None]
    brake_min = min(phys_brakes) if phys_brakes else None
    brake_max = max(phys_brakes) if phys_brakes else None
    lines = [
        "action diversity (live worker status):",
        "  workers={0} raw_unique={1} physical_unique={2} observation_unique={3} "
        "unique_exploration_seeds={4} training_mode={5}".format(
            len(rows), unique_raw, unique_phys, unique_obs, unique_seeds, training_mode
        ),
        "  brake_mapping_mode={0} brake_forced_by_any_system={1} phys_brake_min={2} phys_brake_max={3}".format(
            PEDAL_MAPPING_MODE,
            any_brake_forced,
            round(brake_min, 4) if brake_min is not None else "?",
            round(brake_max, 4) if brake_max is not None else "?",
        ),
    ]
    for row in rows:
        lines.append(
            "  W{wid} source={src} seed={seed} det={det} safe_stop={ss} "
            "brake_safety={bf} obs={obs} raw={raw} noise={noise} phys={phys} raw_hash={rh}".format(
                wid=row["worker_id"],
                src=row["action_source"] or "?",
                seed=row.get("exploration_seed", row["policy_seed"]),
                det=row.get("deterministic_mode", False),
                ss=row.get("safe_stop_active", False),
                bf=row.get("brake_forced_by_safety", False),
                obs=row["observation_hash"] or "?",
                raw=[round(float(x), 4) for x in row["raw_action"][:3]] if row["raw_action"] else [],
                noise=[round(float(x), 4) for x in row["action_noise"][:3]] if row.get("action_noise") else [],
                phys=[round(float(x), 4) for x in row["physical_action"][:3]] if row["physical_action"] else [],
                rh=row["raw_action_hash"],
            )
        )
        lines.append(
            "     brake: raw={rb} physical={pb} mapping={mm} forced={bf} reason={br} source={bs}".format(
                rb=round(row["raw_brake"], 4) if row.get("raw_brake") is not None else "?",
                pb=round(row["physical_brake"], 4) if row.get("physical_brake") is not None else "?",
                mm=row.get("brake_mapping_mode", PEDAL_MAPPING_MODE),
                bf=row.get("brake_forced_by_any_system", False),
                br=row.get("brake_force_reason", "") or "-",
                bs=row.get("brake_source", "") or "?",
            )
        )
    if unique_raw <= 1 and len(raw_hashes) > 1:
        lines.append("  WARN: all workers share the same raw_action hash")
    if any_brake_forced:
        lines.append("  WARN: a safety system is forcing brake on some worker")
    return "\n".join(lines)
