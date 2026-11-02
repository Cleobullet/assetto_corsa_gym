"""Frozen-policy driving evaluation, separate from the training replay.

Only runs with matching actual starts/configuration compete for a best-policy slot.
No clean lap is inferred merely from crossing the start/finish line.
"""
import hashlib
import json
import time
import uuid
from pathlib import Path

from ac_workers.collector_status import atomic_write_status_json
from ac_workers.policy_metadata import build_metadata, write_metadata
from common.training_contract import config_fingerprint


class EvaluationRun:
    def __init__(self, policy, config, initial_state, track_length):
        self.policy = policy
        self.config = config
        self.track_length = float(track_length)
        self.target_episodes = int(config.Agent.num_eval_episodes)
        if self.target_episodes < 1:
            raise ValueError('num_eval_episodes must be positive')
        self.root = policy.work_dir / 'evaluations'
        self.directory = self.root / (time.strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8])
        self.directory.mkdir(parents=True)
        self.old_deterministic = policy.deterministic_mode
        self.training_step = int(policy._training_step)
        self.policy_version = int(policy._active_policy_version)
        self.episodes = []
        self.finished = False
        # Save before freezing: a failed disk write cannot leave a worker in eval mode.
        policy.algo.save_models(str(self.directory / 'policy'))
        write_metadata(self.directory / 'policy', build_metadata(state_dim=policy.state_dim,
                       training_step=self.training_step, created_from='evaluation'))
        self.policy.evaluation_frozen = True
        self.policy.deterministic_mode = True
        self.begin_episode(initial_state)

    def begin_episode(self, state):
        self.initial = dict(state)
        self.started_at = time.monotonic()
        self.progress = 0.
        self.reward = 0.
        self.steps = 0
        self.invalid_steps = 0
        self.outside = bool(state.get("out_of_track", False))
        self.stopped_seconds = 0.0
        self._last_stamp = float(state.get("timestamp_ac", 0.0))

    def observe(self, state, reward, done, info):
        components = info.get('reward_components', {})
        self.steps += 1
        stamp = float(state.get("timestamp_ac", self._last_stamp))
        elapsed = stamp - self._last_stamp
        self._last_stamp = stamp
        if 0 < elapsed <= 1 and abs(float(state.get("speed", 0))) < 1 / 3.6:
            self.stopped_seconds += elapsed
        self.reward += float(reward)
        self.outside |= bool(state.get('out_of_track', False))
        if components.get('progress_valid', False):
            delta = float(components.get('signed_progress_m', 0.))
            self.progress += min(delta, 0.) if self.outside else delta
        else:
            self.invalid_steps += 1
        clean_lap = self.progress >= self.track_length and not self.outside
        timeout = time.monotonic() - self.started_at >= float(self.config.AssettoCorsa.max_episode_py_time)
        done = bool(done or clean_lap or timeout)
        if done:
            self.episodes.append(dict(distance_m=max(0., self.progress), clean_lap=clean_lap,
                out_of_track=self.outside, duration_seconds=time.monotonic()-self.started_at,
                reward=self.reward, steps=self.steps, invalid_steps=self.invalid_steps,
                stopped_seconds=self.stopped_seconds,
                initial_spline=float(self.initial.get('NormalizedSplinePosition', -1)),
                initial_speed=float(self.initial.get('speed', -1)), initial_yaw=float(self.initial.get('yaw', 0)),
                done_reason='clean_lap' if clean_lap else info.get('done_reason') or 'evaluation_timeout'))
            if len(self.episodes) >= self.target_episodes:
                self.finish()
        return done

    def finish(self, aborted=False):
        self.policy.evaluation_frozen = False
        self.policy.deterministic_mode = self.old_deterministic
        self.finished = True
        starts = [(round(e['initial_spline'], 3), round(e['initial_speed'], 1), round(e['initial_yaw'], 2))
                  for e in self.episodes]
        fingerprint = config_fingerprint(self.config)
        protocol = hashlib.sha256(json.dumps([fingerprint, self.track_length, starts]).encode()).hexdigest()
        complete = not aborted and len(self.episodes) == self.target_episodes
        reliable = complete and all(e['invalid_steps'] <= 1 for e in self.episodes)
        clean = sum(e['clean_lap'] for e in self.episodes)
        score = [clean, sum(e['distance_m'] for e in self.episodes)/max(1,len(self.episodes)),
                 -sum(e['duration_seconds'] for e in self.episodes) if clean == len(self.episodes) else 0.]
        result = dict(complete=complete, reliable=reliable, training_step=self.training_step,
                      deterministic=True, policy_version=self.policy_version, episodes=self.episodes, score=score,
                      config_fingerprint=fingerprint, protocol=protocol,
                      policy_path=str(self.directory / 'policy'))
        atomic_write_status_json(self.directory / 'summary.json', result)
        atomic_write_status_json(self.root / 'latest.json', result)
        best_path = self.root / 'best_by_protocol.json'
        best = json.loads(best_path.read_text()) if best_path.exists() else {}
        if reliable and (protocol not in best or score > best[protocol]['score']):
            best[protocol] = result
            atomic_write_status_json(best_path, best)
        return result


def start_evaluation(runtime, config, state, track_length, episode_id):
    """Load before freezing. Refuse a stale model rather than silently re-test it."""
    policy = runtime.policy
    policy.detect_pending_version()
    policy.apply_pending_policy(episode_id)
    if policy.has_pending_swap():
        raise RuntimeError("evaluation_policy_reload_failed")
    runtime.evaluation = EvaluationRun(policy, config, state, track_length)
    return runtime.evaluation


def finish_evaluation(runtime):
    runtime.evaluation = None
    runtime._policy_paused = runtime._evaluation_restore_paused
