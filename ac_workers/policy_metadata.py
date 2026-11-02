"""Policy checkpoint metadata — schema validation and atomic publishing."""
from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
ALGORITHM = "SAC"
METADATA_FILENAME = "policy_metadata.json"
from common.training_contract import ACTION_SCHEMA, OBSERVATION_SCHEMA, REWARD_SCHEMA
OBSERVATION_SCHEMA_VERSION = OBSERVATION_SCHEMA


SCHEMA_REQUIRED_STATE_DIM: dict[str, int] = {
    "c4d_125_v1": 125,
    OBSERVATION_SCHEMA: 50,
}


def build_metadata(
    *,
    state_dim: int,
    action_dim: int = 3,
    training_step: int = 0,
    created_from: str = "fresh",
    observation_schema_version: str = OBSERVATION_SCHEMA_VERSION,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    required = SCHEMA_REQUIRED_STATE_DIM.get(observation_schema_version)
    if required is not None and int(state_dim) != required:
        raise ValueError(
            "observation_schema_version={0} requires state_dim={1}, got {2}".format(
                observation_schema_version, required, state_dim
            )
        )
    meta: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "state_dim": int(state_dim),
        "action_dim": int(action_dim),
        "algorithm": ALGORITHM,
        "observation_schema_version": observation_schema_version,
        "action_schema_version": ACTION_SCHEMA,
        "reward_schema_version": REWARD_SCHEMA,
        "training_step": int(training_step),
        "created_from": str(created_from),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        meta.update(extra)
    return meta


def write_metadata(directory: Path, meta: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / METADATA_FILENAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_metadata(directory: Path) -> dict[str, Any] | None:
    path = directory / METADATA_FILENAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def validate_metadata(
    meta: dict[str, Any] | None,
    *,
    expected_state_dim: int,
    expected_action_dim: int = 3,
) -> tuple[bool, str]:
    if meta is None:
        return False, "metadata_missing"
    if int(meta.get("schema_version", -1)) != SCHEMA_VERSION:
        return False, "schema_version_mismatch"
    if int(meta.get("state_dim", -1)) != expected_state_dim:
        return False, "policy_state_dim_mismatch"
    if int(meta.get("action_dim", -1)) != expected_action_dim:
        return False, "policy_action_dim_mismatch"
    if str(meta.get("algorithm", "")) != ALGORITHM:
        return False, "algorithm_mismatch"
    if meta.get("action_schema_version") != ACTION_SCHEMA:
        return False, "action_schema_mismatch_start_new_training"
    if meta.get("reward_schema_version") != REWARD_SCHEMA:
        return False, "reward_schema_mismatch_start_new_training"
    if meta.get("observation_schema_version") != OBSERVATION_SCHEMA:
        return False, "observation_schema_mismatch_start_new_training"
    return True, ""


def atomic_publish_with_metadata(
    algo: Any,
    publish_dir: Path,
    latest_marker: Path,
    meta: dict[str, Any],
) -> None:
    """Save all model files + metadata atomically.

    Backward-compatible wrapper. Prefer atomic_publish_versioned() for the hot
    path: it writes to a versioned dir, verifies integrity, then flips an atomic
    pointer so workers never read a half-written checkpoint.
    """
    publish_dir.mkdir(parents=True, exist_ok=True)
    algo.save_models(str(publish_dir))
    write_metadata(publish_dir, meta)
    tmp = latest_marker.with_suffix(latest_marker.suffix + ".tmp")
    tmp.write_bytes(b"v2")
    os.replace(tmp, latest_marker)


# Files every published SAC policy must contain to be considered complete.
REQUIRED_POLICY_FILES = (
    "policy_net.pth",
    "online_q_net.pth",
    "target_q_net.pth",
)
POINTER_FILENAME = "latest.json"
VERSIONS_DIRNAME = "publish_versions"


def _verify_version_dir(version_dir: Path) -> tuple[bool, str]:
    """Confirm every required weight file exists, is non-empty, and re-loads.

    torch.load on a truncated/empty file raises EOFError("Ran out of input")
    or a zip RuntimeError; we catch broadly so a bad publish is reported, never
    propagated to a crash.
    """
    import torch

    for name in REQUIRED_POLICY_FILES:
        fp = version_dir / name
        if not fp.is_file():
            return False, "missing_{0}".format(name)
        try:
            if fp.stat().st_size <= 0:
                return False, "empty_{0}".format(name)
        except OSError as exc:
            return False, "stat_failed_{0}:{1}".format(name, exc)
        try:
            torch.load(str(fp), map_location="cpu")
        except Exception as exc:  # EOFError, RuntimeError, pickle, zip, ...
            return False, "unreadable_{0}:{1}".format(name, exc)
    if read_metadata(version_dir) is None:
        return False, "missing_metadata"
    return True, ""


def _atomic_replace_retry(tmp: Path, dest: Path, attempts: int = 8) -> None:
    """os.replace with retries.

    On Windows, os.replace raises PermissionError (WinError 5) if a reader has
    the destination open at that instant. Workers read the pointer constantly,
    so we retry briefly instead of failing the whole publish.
    """
    last: OSError | None = None
    for i in range(attempts):
        try:
            os.replace(tmp, dest)
            return
        except PermissionError as exc:  # WinError 5 transient sharing violation
            last = exc
            time.sleep(0.01 * (i + 1))
    if last is not None:
        raise last


def read_pointer(model_dir: Path) -> dict[str, Any] | None:
    """Read the atomic publish pointer (latest.json), or None if absent/invalid."""
    path = model_dir / POINTER_FILENAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or not data.get("complete"):
        return None
    return data


def prune_old_versions(
    versions_root: Path,
    keep: int = 5,
    protect: Path | None = None,
) -> None:
    """Keep only the newest `keep` version dirs to bound disk usage.

    Never delete `protect` (the version just published). Otherwise publishing
    version_00000000 next to old version_00056xxx deletes the new brain and
    leaves latest.json pointing at a missing folder.
    """
    if not versions_root.is_dir():
        return
    dirs = sorted(
        (d for d in versions_root.iterdir() if d.is_dir() and d.name.startswith("version_")),
        key=lambda d: d.name,
    )
    protect_name = protect.name if protect is not None else None
    stale_list = dirs[:-keep] if keep > 0 else list(dirs)
    for stale in stale_list:
        if stale.name == protect_name:
            continue
        try:
            shutil.rmtree(stale, ignore_errors=True)
        except OSError:
            pass


def atomic_publish_versioned(
    algo: Any,
    model_dir: Path,
    meta: dict[str, Any],
    policy_version: int,
    *,
    keep_versions: int = 5,
) -> tuple[Path, dict[str, Any]]:
    """Publish a policy safely for concurrent reading workers.

    Steps:
      1. Write all weight files + metadata into a fresh versioned dir.
      2. Verify every file exists, is non-empty, and re-loads.
      3. Atomically flip latest.json to point at the verified version.

    Returns (version_dir, pointer_dict). Raises ValueError if verification
    fails (caller decides how to surface it without killing the learner).
    """
    versions_root = model_dir / VERSIONS_DIRNAME
    version_dir = versions_root / "version_{0:08d}".format(int(policy_version))
    # Fresh dir so a previously aborted publish never leaves stale files behind.
    if version_dir.exists():
        shutil.rmtree(version_dir, ignore_errors=True)
    version_dir.mkdir(parents=True, exist_ok=True)
    algo.save_models(str(version_dir))
    write_metadata(version_dir, meta)

    ok, reason = _verify_version_dir(version_dir)
    if not ok:
        raise ValueError("publish_verification_failed:{0}".format(reason))

    pointer = {
        "complete": True,
        "policy_version": int(policy_version),
        "version_dir": version_dir.name,
        "training_step": int(meta.get("training_step", 0)),
        "timestamp": time.time(),
    }
    pointer_path = model_dir / POINTER_FILENAME
    tmp = pointer_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(pointer), encoding="utf-8")
    _atomic_replace_retry(tmp, pointer_path)

    prune_old_versions(versions_root, keep=keep_versions, protect=version_dir)
    return version_dir, pointer
