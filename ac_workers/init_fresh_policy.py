"""Initialize a fresh SAC policy checkpoint compatible with the current environment."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))
sys.path.insert(0, str(REPO / "algorithm" / "discor"))

from omegaconf import OmegaConf

from ac_workers.policy_metadata import (
    OBSERVATION_SCHEMA_VERSION,
    atomic_publish_versioned,
    build_metadata,
    read_metadata,
    validate_metadata,
    write_metadata,
)
from discor.algorithm import SAC


def _resolve_device(config):
    import torch

    mode = OmegaConf.select(config, "device", default="cpu")
    if mode == "cuda" and torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


def init_fresh_policy(
    *,
    config_path: Path,
    work_dir: Path,
    state_dim: int = 50,
    action_dim: int = 3,
    created_from: str = "fresh",
) -> dict:
    config = OmegaConf.load(str(config_path))
    device = _resolve_device(config)
    sac_cfg = OmegaConf.to_container(config.SAC)
    algo = SAC(
        state_dim=state_dim,
        action_dim=action_dim,
        device=device,
        seed=int(config.seed),
        **sac_cfg,
    )

    from ac_workers.training_sessions import wipe_live_weights

    work_dir.mkdir(parents=True, exist_ok=True)
    wipe_live_weights(work_dir)
    (work_dir / "model").mkdir(parents=True, exist_ok=True)
    (work_dir / "model" / "checkpoints").mkdir(parents=True, exist_ok=True)
    (work_dir / "model" / "publish").mkdir(parents=True, exist_ok=True)

    model_dir = work_dir / "model"
    latest_marker = model_dir / "latest.pt"
    meta = build_metadata(
        state_dim=state_dim,
        action_dim=action_dim,
        training_step=0,
        created_from=created_from,
    )
    # Versioned + atomic so a worker that polls during init never sees a partial
    # checkpoint. Version 0 is the initial published policy.
    version_dir, _pointer = atomic_publish_versioned(algo, model_dir, meta, 0)
    # Legacy marker (mtime trigger) for back-compat with older readers.
    latest_marker.write_bytes(b"v3")

    initial_ckpt = model_dir / "checkpoints" / "step_00000000"
    algo.save_models(str(initial_ckpt))
    write_metadata(initial_ckpt, meta)

    saved_meta = read_metadata(version_dir)
    ok, reason = validate_metadata(
        saved_meta, expected_state_dim=state_dim, expected_action_dim=action_dim
    )
    if not ok:
        raise RuntimeError("fresh policy validation failed: {0}".format(reason))

    print("fresh policy initialized:")
    print("  state_dim={0}".format(state_dim))
    print("  action_dim={0}".format(action_dim))
    print("  observation_schema={0}".format(OBSERVATION_SCHEMA_VERSION))
    print("  version_dir={0}".format(version_dir))
    print("  latest_marker={0}".format(latest_marker))

    return {
        "state_dim": state_dim,
        "action_dim": action_dim,
        "publish_dir": str(version_dir),
        "latest_marker": str(latest_marker),
        "metadata": meta,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Initialize fresh SAC policy for C4D.")
    parser.add_argument("--state-dim", type=int, default=50)
    parser.add_argument("--action-dim", type=int, default=3)
    parser.add_argument("--config", default=str(REPO / "config_first_sac_cpu_v3.yml"))
    parser.add_argument("--work-dir", default=str(REPO / "outputs" / "multi_sac_cpu_v3"))
    args = parser.parse_args(argv)
    init_fresh_policy(
        config_path=Path(args.config),
        work_dir=Path(args.work_dir),
        state_dim=args.state_dim,
        action_dim=args.action_dim,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
