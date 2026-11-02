"""Versioned semantics shared by collectors, replay, checkpoints and evaluation."""
import hashlib
import json

ACTION_SCHEMA = "normalized_policy_relu_v2"
OBSERVATION_SCHEMA = "c4d_50_commands_v2"
REWARD_SCHEMA = "signed_progress_v2_elapsed"


def config_fingerprint(config):
    from omegaconf import OmegaConf
    data = OmegaConf.to_container(config, resolve=True)
    # Operational cadence/budget may change on resume; learning semantics may not.
    semantic = {key: data.get(key) for key in ("SAC", "AssettoCorsa", "seed")}
    semantic["Agent"] = {key: data.get("Agent", {}).get(key) for key in (
        "batch_size", "memory_size", "update_interval", "action_exploration_noise_std",
        "action_exploration_throttle_bias", "action_exploration_brake_bias")}
    return hashlib.sha256(json.dumps(semantic, sort_keys=True).encode()).hexdigest()
