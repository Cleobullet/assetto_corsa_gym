"""One atomic file is the authoritative resume state; model-only exports are separate."""
import os
import random
import uuid
from pathlib import Path

import numpy as np
import torch

from common.training_contract import ACTION_SCHEMA, OBSERVATION_SCHEMA, REWARD_SCHEMA, config_fingerprint


def capture(trainer):
    from omegaconf import OmegaConf
    return dict(format_version=1, action_schema=ACTION_SCHEMA, observation_schema=OBSERVATION_SCHEMA,
                reward_schema=REWARD_SCHEMA, config_fingerprint=config_fingerprint(trainer.config),
                config=OmegaConf.to_container(trainer.config, resolve=True),
                algorithm=trainer.algo.training_state_dict(), replay=trainer.replay.state_dict(),
                steps=trainer.steps, updates=trainer.updates, policy_version=trainer._policy_version,
                rng=dict(python=random.getstate(), numpy=np.random.get_state(), torch=torch.get_rng_state(),
                         cuda=torch.cuda.get_rng_state_all() if torch.cuda.is_available() else None))


def save_atomic(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('wb') as stream:
            torch.save(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def is_compatible_resume(path, config) -> bool:
    path = Path(path)
    if not path.is_file():
        return False
    try:
        state = torch.load(path, map_location="cpu")
    except Exception:
        return False
    if not isinstance(state, dict):
        return False
    if (state.get("format_version") != 1 or state.get("action_schema") != ACTION_SCHEMA
            or state.get("observation_schema") != OBSERVATION_SCHEMA
            or state.get("reward_schema") != REWARD_SCHEMA):
        return False
    try:
        return state.get("config_fingerprint") == config_fingerprint(config)
    except Exception:
        return False


def restore(trainer, path):
    # Only local trusted training checkpoints are accepted, never downloaded pickle files.
    state = torch.load(path, map_location='cpu')
    if (state.get('format_version') != 1 or state.get('action_schema') != ACTION_SCHEMA
            or state.get('observation_schema') != OBSERVATION_SCHEMA or state.get('reward_schema') != REWARD_SCHEMA):
        raise ValueError('Contrat d entrainement change. Dans le C4D: train wipe puis learner start.')
    if state.get('config_fingerprint') != config_fingerprint(trainer.config):
        raise ValueError('Config changee (resume refuse). Dans le C4D: train wipe puis learner start.')
    if state.get('replay') is not None:
        trainer.replay.load_state_dict(state['replay'])
    trainer.algo.load_training_state_dict(state['algorithm'])
    trainer.steps, trainer.updates = int(state['steps']), int(state['updates'])
    trainer._policy_version = int(state['policy_version'])
    random.setstate(state['rng']['python'])
    np.random.set_state(state['rng']['numpy'])
    torch.set_rng_state(state['rng']['torch'])
    if torch.cuda.is_available() and state['rng']['cuda'] is not None:
        torch.cuda.set_rng_state_all(state['rng']['cuda'])
