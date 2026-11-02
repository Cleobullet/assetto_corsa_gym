"""Central SAC learner — attaches to existing transition server only (C4D)."""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))
sys.path.insert(0, str(REPO / "algorithm" / "discor"))

import numpy as np
import torch
from omegaconf import OmegaConf
from torch.utils.tensorboard import SummaryWriter

from AssettoCorsaEnv.ac_worker_runtime import _pid_alive, read_manifest, write_manifest
from common.transition_server import TransitionClient
from ac_workers.collector_status import atomic_write_status_json
from ac_workers.policy_metadata import (
    atomic_publish_versioned,
    build_metadata,
    read_metadata,
    validate_metadata,
    write_metadata,
)
from discor.algorithm import SAC
from discor.replay_buffer import ReplayBuffer


def _resolve_device(config) -> torch.device:
    mode = OmegaConf.select(config, "device", default="cpu")
    if mode == "cuda" and torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


def _assert_transition_server(client: TransitionClient) -> None:
    try:
        client.stats()
    except OSError as exc:
        raise RuntimeError(
            "transition server not reachable — lancez Start-Multi-AI-Session d'abord"
        ) from exc


def _assert_single_learner() -> None:
    manifest = read_manifest()
    pid = manifest.get("learnerPid")
    if pid and _pid_alive(int(pid)) and int(pid) != os.getpid():
        print("learner already running pid={0}".format(pid))
        raise SystemExit(1)


def _register_learner_pid() -> None:
    manifest = read_manifest()
    manifest["learnerPid"] = os.getpid()
    write_manifest(manifest)


class _LearnerHeartbeatThread:
    """Keep learner_last_seen fresh while SAC update/publish blocks the main loop."""

    def __init__(self, client: TransitionClient, interval_sec: float = 1.0) -> None:
        self._client = client
        self._interval = float(interval_sec)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="learner-heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._client.learner_ping()
            except OSError:
                pass
            self._stop.wait(self._interval)


class MultiSacTrainer:
    def __init__(
        self,
        config_path: Path,
        work_dir: Path,
        max_steps: int | None = None,
        resume: bool = False,
        session_id: str | None = None,
        continuous: bool = True,
    ):
        self.config = OmegaConf.load(str(config_path))
        self.work_dir = work_dir
        work_dir.mkdir(parents=True, exist_ok=True)
        environment_path = work_dir / "training_environment.json"
        environment_path.write_text(json.dumps({
            "car": self.config.AssettoCorsa.get("car"),
            "track": self.config.AssettoCorsa.get("track"),
            "source": "trainer_config"
        }, indent=2), encoding="utf-8")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        (self.work_dir / "model").mkdir(parents=True, exist_ok=True)
        (self.work_dir / "model" / "checkpoints").mkdir(parents=True, exist_ok=True)
        (self.work_dir / "model" / "publish").mkdir(parents=True, exist_ok=True)
        self.device = _resolve_device(self.config)
        agent_cfg = OmegaConf.to_container(self.config.Agent)
        self.batch_size = int(agent_cfg.pop("batch_size", 64))
        self.memory_size = int(agent_cfg.pop("memory_size", 50000))
        self.update_interval = int(agent_cfg.pop("update_interval", 1))
        self.start_steps = int(agent_cfg.pop("start_steps", 1500))
        self.checkpoint_freq = int(agent_cfg.pop("checkpoint_freq", 2000))
        self.publish_every_updates = int(agent_cfg.pop("publish_every_updates", 1))
        self._base_num_steps = int(agent_cfg.pop("num_steps", 10000))
        if max_steps is not None:
            self._base_num_steps = int(max_steps)
            self.continuous_mode = False
        else:
            self.continuous_mode = bool(continuous)
        self.num_steps = self._base_num_steps
        self._target_steps: int | None = None
        self._checkpoint_loaded_step = 0
        self._target_finalized = False
        sac_cfg = OmegaConf.to_container(self.config.SAC)
        self.gamma = float(sac_cfg.get("gamma", 0.992))
        self.nstep = int(sac_cfg.get("nstep", 3))
        self.state_dim: int | None = None
        self.action_dim = 3
        self.algo: SAC | None = None
        self.replay: ReplayBuffer | None = None
        self.writer: SummaryWriter | None = None
        self.steps = 0
        self.updates = 0
        self.training_started = False
        self._reward_sum = 0.0
        self._reward_n = 0
        self._last_action = [0.0, 0.0, 0.0]
        self._last_checkpoint = ""
        self._resume = resume
        self._checkpoint_safe = False
        self._status_path = self.work_dir / "learner_status.json"
        self._stop_request_path = self.work_dir / "learner_stop.request"
        self._save_request_path = self.work_dir / "learner_save.request"
        self._last_weights_mtime = 0.0
        self._last_checkpoint_step = 0
        self._last_checkpoint_saved_path = ""
        self._last_checkpoint_ts = 0.0
        self._last_publish_path = ""
        self._last_publish_ts = 0.0
        self._policy_published_count = 0
        self._updates_since_last_publish = 0
        self._publish_in_progress = False
        self._last_publish_complete = True
        self._last_publish_error = ""
        self._last_publish_version = 0
        self._actor_requests_served = 0
        self._checkpoint_loaded_path = ""
        self._policy_version = 0
        self._last_error = ""
        self._last_transition_worker_id = -1
        self._transitions_by_worker: dict[int, int] = {}
        self._learner_pid = os.getpid()
        self._session_id = session_id or time.strftime("%Y%m%d_%H%M%S")
        self._started_at = time.time()
        self._done_reasons: dict[str, int] = {}
        self._episode_ages: list[float] = []
        self._episodes_total = 0
        self._stream_positions = {}
        self._rejected_transitions = 0
        self._episode_progress_m: list[float] = []
        self._current_episode_progress: dict[int, float] = {}
        self._best_score = float("-inf")
        self.eval_interval = int(agent_cfg.get("eval_interval", 0))
        self._next_eval_step = self.eval_interval
        self._episodes_last_log = 0
        self._episodes_last_log_ts = time.time()
        self._archive_stale_status()

    def _archive_stale_status(self) -> None:
        if not self._status_path.is_file():
            return
        try:
            old = json.loads(self._status_path.read_text(encoding="utf-8"))
            old_sid = old.get("session_id", "")
            if old_sid and old_sid == self._session_id:
                return
            stamp = time.strftime("%Y%m%d_%H%M%S")
            archive = self._status_path.with_name("learner_status_archive_{0}.json".format(stamp))
            self._status_path.rename(archive)
        except (json.JSONDecodeError, OSError, KeyError):
            try:
                self._status_path.unlink()
            except OSError:
                pass

    def _init_networks(self, state_dim: int) -> None:
        if self.algo is not None:
            return
        self.state_dim = state_dim
        sac_cfg = OmegaConf.to_container(self.config.SAC)
        self.algo = SAC(
            state_dim=state_dim,
            action_dim=self.action_dim,
            device=self.device,
            seed=int(self.config.seed),
            **sac_cfg,
        )
        self.replay = ReplayBuffer(
            memory_size=self.memory_size,
            state_shape=(state_dim,),
            action_shape=(self.action_dim,),
            gamma=self.gamma,
            nstep=self.nstep,
        )
        self.writer = SummaryWriter(log_dir=str(self.work_dir / "summary"))
        ckpt = self.work_dir / "model" / "checkpoints" / "resume"
        if self._resume:
            from ac_workers.training_checkpoint import restore
            bundle = ckpt / "training_state.pt"
            if not bundle.is_file():
                raise ValueError(
                    "Ancien checkpoint sans etat complet. Dans le C4D: train wipe puis learner start."
                )
            restore(self, bundle)
            self._checkpoint_loaded_path = str(ckpt)
            print("resume: restored full training state steps={0} replay={1}".format(self.steps, len(self.replay)))
        self._finalize_training_target()
        self._checkpoint_safe = True

    def _remaining_steps(self) -> int | None:
        if self._target_steps is None:
            return None
        return max(0, self._target_steps - self.steps)

    def _finalize_training_target(self) -> None:
        if self._target_finalized:
            return
        self._checkpoint_loaded_step = int(self.steps)
        if self.continuous_mode:
            self._target_steps = None
            print(
                "training target: continuous_mode=True checkpoint_loaded_step={0} "
                "target_steps=unlimited remaining_steps=unlimited".format(
                    self._checkpoint_loaded_step
                )
            )
        else:
            if self._resume or self._checkpoint_loaded_step > 0:
                self._target_steps = self._checkpoint_loaded_step + self._base_num_steps
            else:
                self._target_steps = self._base_num_steps
            print(
                "training target: continuous_mode=False checkpoint_loaded_step={0} "
                "target_steps={1} remaining_steps={2}".format(
                    self._checkpoint_loaded_step,
                    self._target_steps,
                    self._remaining_steps(),
                )
            )
        self._target_finalized = True

    def _should_continue_training(self) -> bool:
        if not self._target_finalized:
            return True
        if self._target_steps is None:
            return True
        return self.steps < self._target_steps

    def _ingest_transition(self, tr: dict) -> bool:
        from common.training_contract import ACTION_SCHEMA
        worker = int(tr.get("worker_id", -1))
        identity = (tr.get("collector_session_id"), tr.get("episode_id"))
        step = int(tr.get("step_id", 0))
        previous = self._stream_positions.get(worker)
        if self.replay is not None and previous != (identity, step - 1):
            self.replay.reset_stream(worker)
        self._stream_positions[worker] = (identity, step)
        if tr.get("action_schema_version") != ACTION_SCHEMA or not tr.get("transition_valid", True):
            if self.replay is not None:
                self.replay.reset_stream(worker)
            self._rejected_transitions += 1
            return False
        obs = np.asarray(tr["obs"], dtype=np.float32).ravel()
        nxt = np.asarray(tr.get("next_state") or tr.get("next_obs") or tr["obs"], dtype=np.float32).ravel()
        act = np.asarray(tr["action"], dtype=np.float32).ravel()
        if self.algo is None:
            self._init_networks(obs.shape[0])
        assert self.replay is not None and self.algo is not None
        reward = float(tr.get("reward", 0.0))
        if (obs.size != self.state_dim or nxt.size != self.state_dim or act.size != self.action_dim
                or not all(np.isfinite(x).all() for x in (obs, nxt, act, reward))
                or np.any(np.abs(act) > 1.000001)):
            self.replay.reset_stream(worker)
            self._rejected_transitions += 1
            return False
        done = bool(tr.get("done", False))
        self.replay.append(obs, act, reward, nxt, bool(tr.get("terminated", done)), episode_done=done,
                           stream_id=int(tr.get("worker_id", -1)))
        self._reward_sum += reward
        self._reward_n += 1
        if act.size >= 3:
            self._last_action = [float(act[0]), float(act[1]), float(act[2])]
        if done:
            reason = tr.get("done_reason", "unknown")
            self._done_reasons[reason] = self._done_reasons.get(reason, 0) + 1
            age = float(tr.get("episode_age_seconds", 0.0))
            if age > 0:
                self._episode_ages.append(age)
            self._episodes_total += 1
        wid = int(tr.get("worker_id", -1))
        self._last_transition_worker_id = wid
        self._transitions_by_worker[wid] = self._transitions_by_worker.get(wid, 0) + 1
        if not self.training_started:
            self.training_started = True
            print("first valid transition ingested — training counter active")
        self._note_drive_metrics(tr)
        return True

    def _note_drive_metrics(self, tr: dict) -> None:
        wid = int(tr.get("worker_id", -1))
        comps = tr.get("reward_components") or {}
        delta = float(comps.get("signed_progress_m", 0.0) or 0.0)
        if comps.get("progress_valid", True):
            self._current_episode_progress[wid] = (
                self._current_episode_progress.get(wid, 0.0) + delta
            )
        if not tr.get("done"):
            return
        travelled = self._current_episode_progress.pop(wid, 0.0)
        self._episode_progress_m.append(travelled)
        if len(self._episode_progress_m) > 200:
            self._episode_progress_m = self._episode_progress_m[-200:]
        self._maybe_save_best()

    def _maybe_save_best(self) -> None:
        if len(self._episode_progress_m) < 8 or self.algo is None or self.state_dim is None:
            return
        score = float(np.mean(self._episode_progress_m[-20:]))
        oot = float(self._done_reasons.get("out_of_track", 0))
        episodes = max(1, self._episodes_total)
        oot_rate = oot / episodes
        # Prefer more valid metres and fewer immediate offs.
        combined = score - 20.0 * oot_rate
        if combined <= self._best_score:
            return
        self._best_score = combined
        best = self.work_dir / "model" / "best"
        self.algo.save_models(str(best))
        write_metadata(
            best,
            build_metadata(
                state_dim=self.state_dim,
                action_dim=self.action_dim,
                training_step=self.steps,
                created_from="best_drive",
            ),
        )
        print(
            "best_model_saved score={0:.2f} progress_m={1:.1f} oot_rate={2:.2f} steps={3}".format(
                combined, score, oot_rate, self.steps
            )
        )

    def _serve_action_requests(self, client: TransitionClient, max_n: int = 64) -> int:
        """Async actor-learner: answer pending per-worker action requests with the
        live SAC policy. Each response is routed back by (worker_id, request_id) so
        no worker waits on another. Harmless in async_local mode (no requests)."""
        try:
            requests = client.pull_action_requests(max_n)
        except OSError:
            return 0
        if not requests:
            return 0
        responses: list[dict] = []
        for req in requests:
            try:
                obs = np.asarray(req["obs"], dtype=np.float32).ravel()
            except (KeyError, TypeError, ValueError):
                continue
            if obs.size == 0:
                continue
            if self.algo is None:
                self._init_networks(obs.shape[0])
                self._finalize_training_target()
            assert self.algo is not None
            action, _ = self.algo.explore(obs)
            raw = [float(x) for x in np.asarray(action, dtype=np.float32).ravel()[:3]]
            responses.append(
                {
                    "worker_id": int(req.get("worker_id", -1)),
                    "request_id": str(req.get("request_id", "")),
                    "action": raw,
                    "policy_version": int(self.updates),
                    "action_source": "learner_rpc",
                }
            )
        if responses:
            try:
                client.put_action_responses(responses)
                self._actor_requests_served += len(responses)
            except OSError:
                return 0
        return len(responses)

    def _maybe_update(self) -> float | None:
        if self.algo is None or self.replay is None or self.writer is None:
            return None
        if not self.training_started or self.steps < self.start_steps:
            if self.algo is not None:
                self.algo.update_target_networks()
            return None
        if self.steps % self.update_interval != 0:
            self.algo.update_target_networks()
            return None
        # n-step replay may buffer the first transitions without committing them;
        # resume can also leave steps >= start_steps while replay is still empty.
        if len(self.replay) < max(self.batch_size, self.start_steps):
            self.algo.update_target_networks()
            return None
        t0 = time.perf_counter()
        batch = self.replay.sample(self.batch_size, self.device, include_discounts=True)
        self.algo.update_online_networks(batch, self.writer)
        self.algo.update_target_networks()
        self.updates += 1
        self._updates_since_last_publish += 1
        self._policy_version = max(int(self._policy_version) + 1, int(self.steps))
        return time.perf_counter() - t0

    def _maybe_publish(self) -> None:
        """Publish the policy to workers after every N SAC updates."""
        if self._updates_since_last_publish < self.publish_every_updates:
            return
        self._publish()
        self._updates_since_last_publish = 0
        self._policy_published_count += 1

    def _verify_network_dims(self) -> int:
        """Return the actual state_dim from the policy network; raise on mismatch."""
        assert self.algo is not None and self.state_dim is not None
        first_layer = self.algo._policy_net.net[0]
        actual = int(first_layer.in_features)
        if actual != self.state_dim:
            raise RuntimeError(
                "network state_dim={0} != trainer state_dim={1}".format(actual, self.state_dim)
            )
        return actual

    def _publish(self) -> None:
        if self.algo is None or self.state_dim is None or not self._checkpoint_safe:
            return
        verified_dim = self._verify_network_dims()
        model_dir = self.work_dir / "model"
        latest = model_dir / "latest.pt"
        meta = build_metadata(
            state_dim=verified_dim,
            action_dim=self.action_dim,
            training_step=self.steps,
            created_from="training",
        )
        self._publish_in_progress = True
        try:
            version_dir, pointer = atomic_publish_versioned(
                self.algo, model_dir, meta, int(self._policy_version)
            )
        except Exception as exc:  # verification failed, disk error, etc.
            # A failed publish must never crash the learner; the workers simply
            # keep their last good policy until the next successful publish.
            self._publish_in_progress = False
            self._last_publish_complete = False
            self._last_publish_error = str(exc)
            print("policy_publish FAILED policy_version={0}: {1}".format(
                int(self._policy_version), exc
            ))
            return
        # Flip the legacy marker (mtime trigger) atomically for back-compat.
        tmp = latest.with_suffix(latest.suffix + ".tmp")
        tmp.write_bytes(b"v3")
        os.replace(tmp, latest)
        self._publish_in_progress = False
        self._last_publish_complete = True
        self._last_publish_error = ""
        self._last_publish_version = int(self._policy_version)
        self._last_weights_mtime = latest.stat().st_mtime
        self._last_publish_path = str(version_dir)
        self._last_publish_ts = time.time()
        print(
            "policy_published version_dir={0} policy_version={1} global_steps={2}".format(
                version_dir.name, int(self._policy_version), self.steps
            )
        )

    def _save_checkpoint(self) -> None:
        if self.algo is None or self.replay is None or self.state_dim is None or not self._checkpoint_safe:
            return
        verified_dim = self._verify_network_dims()
        ckpt = self.work_dir / "model" / "checkpoints" / ("step_{0:08d}".format(self.steps))
        from ac_workers.training_checkpoint import capture, save_atomic
        complete_state = capture(self)
        save_atomic(ckpt / "training_state.pt", complete_state)
        self.algo.save_models(str(ckpt))
        meta = build_metadata(
            state_dim=verified_dim,
            action_dim=self.action_dim,
            training_step=self.steps,
            created_from="training",
        )
        write_metadata(ckpt, meta)
        resume = self.work_dir / "model" / "checkpoints" / "resume"
        save_atomic(resume / "training_state.pt", complete_state)
        self.algo.save_models(str(resume))
        write_metadata(resume, meta)
        state = {
            "steps": self.steps,
            "updates": self.updates,
            "replay_size": len(self.replay),
            "last_checkpoint": str(ckpt),
        }
        self._last_checkpoint = str(ckpt)
        self._last_checkpoint_saved_path = str(ckpt)
        self._last_checkpoint_step = int(self.steps)
        self._last_checkpoint_ts = time.time()
        self._policy_version = max(int(self._policy_version), int(self.steps))
        (self.work_dir / "trainer_state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
        print(
            "checkpoint_saved path={0} replay={1} updates={2} policy_version={3}".format(
                self._last_checkpoint_saved_path,
                len(self.replay),
                self.updates,
                self._policy_version,
            )
        )

    def _maybe_request_evaluation(self):
        if not self.eval_interval or self.steps < self._next_eval_step or not self.updates:
            return
        from ac_workers.collector_diag import CollectorDiagClient
        worker = int(OmegaConf.select(self.config, 'Agent.eval_worker_id', default=0))
        try:
            result = CollectorDiagClient(worker).request('evaluate')
            print('evaluation requested worker={0} accepted={1}'.format(worker, result.get('ok', False)))
        except Exception as exc:
            print('evaluation unavailable: {0}'.format(exc))
        self._next_eval_step = self.steps + self.eval_interval

    def _stop_requested(self) -> bool:
        return self._stop_request_path.is_file()

    def _save_requested(self) -> bool:
        return self._save_request_path.is_file()

    def _clear_stop_request(self) -> None:
        try:
            if self._stop_request_path.is_file():
                self._stop_request_path.unlink()
        except OSError:
            pass

    def _clear_save_request(self) -> None:
        try:
            if self._save_request_path.is_file():
                self._save_request_path.unlink()
        except OSError:
            pass

    def _handle_save_request(self) -> None:
        if not self._save_requested():
            return
        self._save_checkpoint()
        self._publish()
        self._clear_save_request()
        print(
            "learner save: checkpoint_saved={0} policy_version={1}".format(
                self._last_checkpoint_saved_path, self._policy_version
            )
        )

    def _write_status(self, client: TransitionClient | None, update_ms: float | None) -> None:
        stats = client.stats() if client is not None else {}
        hb_age = None
        last = stats.get("learner_last_seen")
        if last:
            hb_age = time.time() - float(last)
        warmup_required = int(self.start_steps)
        replay_size = len(self.replay) if self.replay else 0
        updates_started = bool(self.updates > 0)
        training_step = int(self.updates)
        status_note = ""
        if not self.training_started:
            status_note = "waiting_for_first_transition"
        elif self.steps < warmup_required:
            status_note = "warmup_not_reached steps={0}/{1}".format(self.steps, warmup_required)
        elif not updates_started:
            status_note = "warmup_reached_updates_pending"
        payload = {
            "session_id": self._session_id,
            "started_at": self._started_at,
            "global_steps": self.steps,
            "learner_global_steps": self.steps,
            "replay_size": replay_size,
            "updates": self.updates,
            "training_started": self.training_started,
            "mean_reward": (self._reward_sum / self._reward_n) if self._reward_n else 0.0,
            "weights_mtime": self._last_weights_mtime,
            "heartbeat_age_sec": hb_age,
            "worker_transitions": stats.get("workers", {}),
            "last_update_ms": update_ms,
            "ts": time.time(),
            "heartbeat": time.time(),
            "learner_running": True,
            "learner_pid": self._learner_pid,
            "learner_training_step": training_step,
            "rejected_transitions": self._rejected_transitions,
            "learner_replay_size": replay_size,
            "learner_warmup_required": warmup_required,
            "learner_updates_started": updates_started,
            "learner_last_checkpoint_step": self._last_checkpoint_step,
            "learner_last_error": self._last_error,
            "learner_status_note": status_note,
            "episodes_total": self._episodes_total,
            "avg_episode_length_sec": (
                sum(self._episode_ages) / len(self._episode_ages)
                if self._episode_ages else 0.0
            ),
            "done_reason_histogram": dict(self._done_reasons),
            "mean_episode_progress_m": (
                float(np.mean(self._episode_progress_m[-20:]))
                if self._episode_progress_m else 0.0
            ),
            "best_drive_score": self._best_score if self._best_score > float("-inf") else None,
            "checkpoint_saved_path": self._last_checkpoint_saved_path,
            "checkpoint_loaded_path": self._checkpoint_loaded_path,
            "policy_version": int(self._policy_version),
            "continuous_mode": self.continuous_mode,
            "checkpoint_loaded_step": int(self._checkpoint_loaded_step),
            "target_steps": self._target_steps,
            "remaining_steps": self._remaining_steps(),
            "control_mode": str(stats.get("control_mode", "")),
            "last_checkpoint_path": self._last_checkpoint_saved_path,
            "last_publish_path": self._last_publish_path,
            "last_checkpoint_age_sec": (
                time.time() - self._last_checkpoint_ts if self._last_checkpoint_ts else None
            ),
            "last_publish_age_sec": (
                time.time() - self._last_publish_ts if self._last_publish_ts else None
            ),
            "actor_requests_total": int(stats.get("actor_requests_total", 0)),
            "actor_responses_total": int(stats.get("actor_responses_total", 0)),
            "actor_timeouts_total": int(stats.get("actor_timeouts_total", 0)),
            "actor_requests_served": int(self._actor_requests_served),
            "publish_every_updates": int(self.publish_every_updates),
            "policy_published_count": int(self._policy_published_count),
            "last_publish_version": int(self._last_publish_version),
            "last_publish_complete": bool(self._last_publish_complete),
            "last_publish_error": str(self._last_publish_error),
            "publish_in_progress": bool(self._publish_in_progress),
            "last_transition_worker_id": int(self._last_transition_worker_id),
            "transitions_by_worker": {str(k): v for k, v in self._transitions_by_worker.items()},
            "updates_since_start": int(self.updates),
        }
        try:
            atomic_write_status_json(self._status_path, payload)
        except OSError as exc:
            # C4D / PowerShell often have this file open on Windows; a status
            # write must never kill the learner.
            print("WARN: learner_status write skipped: {0}".format(exc))

    def _wait_first_transition(self, client: TransitionClient, timeout_sec: float = 120.0) -> None:
        deadline = time.time() + timeout_sec
        print("waiting for first valid transition on transition server...")
        self._write_status(client, None)
        last_log = 0.0
        while time.time() < deadline:
            if self._stop_requested():
                self._save_checkpoint()
                self._publish()
                raise SystemExit(0)
            self._handle_save_request()
            client.learner_ping()
            # In async_actor mode the first transitions only appear AFTER we start
            # answering action requests, so serve them in this tight pre-train loop.
            self._serve_action_requests(client)
            batch = client.pull_batch(256)
            for tr in batch:
                if not self._ingest_transition(tr):
                    continue
                self.steps += 1
                self._maybe_update()
                if not self._should_continue_training():
                    break
            if self.training_started:
                self._write_status(client, None)
                return
            if time.time() - last_log >= 1.0:
                stats = client.stats()
                workers = stats.get("workers", {})
                print(
                    "transition server workers={0} queue={1} actor_requests={2}".format(
                        workers, stats.get("queue_size"), stats.get("actor_requests_total", 0)
                    )
                )
                if not workers and not stats.get("actor_requests_total"):
                    print("ERROR: no worker activity — verify collector is running (Run-SAC precheck)")
                self._write_status(client, None)
                last_log = time.time()
            time.sleep(0.01)
        self._last_error = "no transition received within {0}s".format(timeout_sec)
        self._write_status(client, None)
        raise TimeoutError(self._last_error)

    def run(self, client: TransitionClient) -> None:
        self._clear_stop_request()
        client.learner_ping()
        heartbeat = _LearnerHeartbeatThread(client, interval_sec=1.0)
        heartbeat.start()
        try:
            self._run_training_loop(client)
        finally:
            heartbeat.stop()

    def _run_training_loop(self, client: TransitionClient) -> None:
        print(
            "Starting SAC learner pid={0} continuous_mode={1}".format(
                self._learner_pid, self.continuous_mode
            )
        )
        self._write_status(client, None)
        self._wait_first_transition(client)
        last_log = time.time()
        stopped_by_request = False
        while self._should_continue_training():
            if self._stop_requested():
                stopped_by_request = True
                print("learner stop requested — saving final checkpoint")
                break
            self._handle_save_request()
            client.learner_ping()
            # Serve async actor-learner action requests first so per-worker action
            # latency stays low even while we ingest/learn (single-threaded inter-
            # leave keeps the SAC networks consistent without a lock).
            served = self._serve_action_requests(client)
            batch = client.pull_batch(256)
            update_ms = None
            for tr in batch:
                if not self._ingest_transition(tr):
                    continue
                self.steps += 1
                um = self._maybe_update()
                if um is not None:
                    update_ms = um * 1000.0
                    self._maybe_publish()
                if self.checkpoint_freq and self.steps % self.checkpoint_freq == 0:
                    self._save_checkpoint()
                    if self._updates_since_last_publish > 0:
                        self._publish()
                        self._updates_since_last_publish = 0
                        self._policy_published_count += 1
                if not self._should_continue_training():
                    break
            self._maybe_request_evaluation()
            if not batch and not served:
                time.sleep(0.005)
            if time.time() - last_log >= 2.0:
                stats = client.stats()
                mean_r = (self._reward_sum / self._reward_n) if self._reward_n else 0.0
                w0 = stats.get("workers", {}).get("0", stats.get("workers", {}).get(0, {}))
                w0_tr = w0.get("transitions_received", 0) if isinstance(w0, dict) else 0
                note = ""
                if self.steps < self.start_steps:
                    note = " warmup={0}/{1}".format(self.steps, self.start_steps)
                avg_ep_len = (
                    sum(self._episode_ages) / len(self._episode_ages)
                    if self._episode_ages else 0.0
                )
                now_ts = time.time()
                dt = now_ts - self._episodes_last_log_ts
                ep_delta = self._episodes_total - self._episodes_last_log
                ep_per_min = (ep_delta / dt * 60.0) if dt > 0 else 0.0
                self._episodes_last_log = self._episodes_total
                self._episodes_last_log_ts = now_ts
                print(
                    "steps={0} replay={1} updates={2} mean_reward={3:.4f}{4} "
                    "steer={5:.3f} throttle={6:.3f} brake={7:.3f} "
                    "worker0_transitions={8} checkpoint={9} weights={10} "
                    "episodes={11} avg_ep_len={12:.1f}s ep/min={13:.1f} "
                    "done_reasons={14} policy_v={15} published={16}".format(
                        self.steps,
                        len(self.replay) if self.replay else 0,
                        self.updates,
                        mean_r,
                        note,
                        self._last_action[0],
                        self._last_action[1],
                        self._last_action[2],
                        w0_tr,
                        self._last_checkpoint or "none",
                        self._last_weights_mtime,
                        self._episodes_total,
                        avg_ep_len,
                        ep_per_min,
                        dict(self._done_reasons),
                        int(self._policy_version),
                        int(self._policy_published_count),
                    )
                )
                self._write_status(client, update_ms)
                last_log = time.time()
        self._save_checkpoint()
        self._publish()
        self._clear_stop_request()
        self._clear_save_request()
        payload = {
            "session_id": self._session_id,
            "learner_running": False,
            "learner_pid": self._learner_pid,
            "learner_global_steps": self.steps,
            "learner_training_step": int(self.updates),
            "learner_last_checkpoint_step": self._last_checkpoint_step,
            "checkpoint_saved_path": self._last_checkpoint_saved_path,
            "checkpoint_loaded_path": self._checkpoint_loaded_path,
            "policy_version": int(self._policy_version),
            "replay_size": len(self.replay) if self.replay else 0,
            "updates": self.updates,
            "continuous_mode": self.continuous_mode,
            "checkpoint_loaded_step": int(self._checkpoint_loaded_step),
            "target_steps": self._target_steps,
            "remaining_steps": self._remaining_steps(),
            "ts": time.time(),
        }
        try:
            atomic_write_status_json(self._status_path, payload)
        except OSError as exc:
            print("WARN: learner_status shutdown write skipped: {0}".format(exc))
        if stopped_by_request:
            print(
                "learner stopped steps={0} updates={1} checkpoint_saved={2}".format(
                    self.steps, self.updates, self._last_checkpoint_saved_path
                )
            )
        elif not self.continuous_mode and self._target_steps is not None and self.steps >= self._target_steps:
            print(
                "training complete steps={0} updates={1} checkpoint_saved={2}".format(
                    self.steps, self.updates, self._last_checkpoint_saved_path
                )
            )
        else:
            print(
                "learner exited steps={0} updates={1} checkpoint_saved={2}".format(
                    self.steps, self.updates, self._last_checkpoint_saved_path
                )
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(REPO / "config_first_sac_cpu_v3.yml"))
    parser.add_argument("--work-dir", default=str(REPO / "outputs" / "multi_sac_cpu_v3"))
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument(
        "--continuous",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="run until learner stop (default); on resume, do not exit when steps >= config num_steps",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--session-id", default=None, help="session identifier for status tracking")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--start-server",
        action="store_true",
        help="DEPRECATED — use transition_server_main via Start-Multi-AI-Session",
    )
    args = parser.parse_args()

    # The SAC networks are tiny (2×256): PyTorch's default multi-threading (one
    # thread per core) spends more time on thread spawn/sync than on the actual
    # matmuls, inflating each update to ~200 ms single-core and burning 6-8 cores.
    # Pin to 1 thread so the update runs in ~5 ms and leaves the cores to the games.
    torch.set_num_threads(1)

    if args.start_server:
        print("WARN: --start-server ignored; attach to existing transition server")

    client = TransitionClient()
    work_dir = Path(args.work_dir)

    if args.dry_run:
        import tempfile
        from common.transition_server import TransitionServer

        server = TransitionServer(port=24053)
        server.start()
        time.sleep(0.5)
        client = TransitionClient(port=24053)
        client.learner_ping()
        with tempfile.TemporaryDirectory() as _dry_tmp:
            dry_work = Path(_dry_tmp) / "dry_run"
            trainer = MultiSacTrainer(
                Path(args.config), dry_work, max_steps=10, continuous=False
            )
            dummy = {
                "obs": [0.0] * 50,
                "action": [0.0, 0.0, 0.0],
                "reward": 0.0,
                "next_state": [0.0] * 50,
                "done": False,
            }
            trainer._ingest_transition(dummy)
            trainer._save_checkpoint()
            trainer._publish()
            trainer._save_checkpoint()
            print("dry-run publish OK (checkpoint safe before init replay)")
        server.stop()
        return 0

    _assert_single_learner()
    _assert_transition_server(client)
    _register_learner_pid()

    trainer = MultiSacTrainer(
        Path(args.config), work_dir,
        max_steps=args.max_steps,
        resume=args.resume,
        session_id=args.session_id,
        continuous=args.continuous,
    )
    try:
        trainer.run(client)
    except KeyboardInterrupt:
        trainer._save_checkpoint()
        trainer._publish()
    finally:
        manifest = read_manifest()
        if manifest.get("learnerPid") == os.getpid():
            manifest["learnerPid"] = None
            write_manifest(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
