"""Policy loader for collectors — skip incompatible checkpoints."""
from __future__ import annotations

import logging
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))
sys.path.insert(0, str(REPO / "algorithm" / "discor"))

import numpy as np
import torch
from omegaconf import OmegaConf

from ac_workers.action_diagnostics import observation_hash, worker_policy_seed
from ac_workers.policy_metadata import read_metadata, read_pointer, validate_metadata
from discor.algorithm import SAC
from discor.replay_buffer import ReplayBuffer
from torch.utils.tensorboard import SummaryWriter

logger = logging.getLogger(__name__)
ARCHIVE_MARKER = ".incompatible_policy_archived"


class _NullWriter:
    """TensorBoard no-op used when a per-worker SummaryWriter can't be created."""

    def add_scalar(self, *args, **kwargs) -> None:
        pass


def archive_incompatible_policy_once(work_dir: Path, state_dim: int) -> bool:
    """Move stale 8-dim (or other) weights aside once; keep files in archive/."""
    marker = work_dir / ARCHIVE_MARKER
    if marker.is_file():
        return False
    model_dir = work_dir / "model"
    if not model_dir.is_dir():
        marker.write_text("no model dir\n", encoding="utf-8")
        return False
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive_root = work_dir / ("archive_incompatible_{0}".format(stamp))
    archive_root.mkdir(parents=True, exist_ok=True)
    shutil.move(str(model_dir), str(archive_root / "model"))
    marker.write_text(
        "archived={0} state_dim={1}\n".format(stamp, state_dim),
        encoding="utf-8",
    )
    logger.warning(
        "archived incompatible policy weights to %s (state_dim=%s)",
        archive_root,
        state_dim,
    )
    return True


def _resolve_device(config) -> torch.device:
    mode = OmegaConf.select(config, "device", default="cpu")
    if mode == "cuda" and torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


class CollectorPolicy:
    def __init__(
        self,
        config_path: Path,
        work_dir: Path,
        state_dim: int,
        action_dim: int = 3,
        *,
        worker_id: int = 0,
    ):
        self.config = OmegaConf.load(str(config_path))
        self.work_dir = work_dir
        self.model_dir = work_dir / "model"
        self.publish_dir = work_dir / "model" / "publish"
        self.latest_marker = work_dir / "model" / "latest.pt"
        self.state_dim = state_dim
        self.worker_id = int(worker_id)
        self.policy_seed = worker_policy_seed(int(self.config.seed), self.worker_id)
        self.device = _resolve_device(self.config)
        # Keep the worker's local SAC updates on ONE thread so they never contend
        # with the game/collector driving loop (same as the central learner).
        torch.set_num_threads(1)
        self._sac_cfg = OmegaConf.to_container(self.config.SAC)
        self.algo = self._build_algo()
        self.action_dim = action_dim
        # PBT flag: when enabled, the worker trains + keeps its OWN brain and
        # resyncs with the shared main brain at episode boundaries, instead of
        # hot-reloading the central learner's published policy.
        self.pbt_enabled = bool(
            OmegaConf.select(self.config, "Agent.pbt_enabled", default=True)
        )
        # PBT — per-worker local brain: each worker trains its OWN policy from its
        # OWN transitions (own replay buffer + own SAC updates), instead of only
        # driving a copy of the central learner's policy.
        self.local_replay = ReplayBuffer(
            memory_size=int(OmegaConf.select(self.config, "Agent.memory_size", default=50000)),
            state_shape=(state_dim,),
            action_shape=(action_dim,),
            gamma=float(OmegaConf.select(self.config, "SAC.gamma", default=0.992)),
            nstep=int(OmegaConf.select(self.config, "SAC.nstep", default=1)),
        )
        self.local_batch_size = int(OmegaConf.select(self.config, "Agent.batch_size", default=64))
        self.local_update_interval = int(
            OmegaConf.select(self.config, "Agent.pbt_local_update_interval", default=100)
        )
        self._local_transitions_since_update = 0
        self._local_updates = 0
        try:
            self._local_writer = SummaryWriter(
                log_dir=str(work_dir / ("summary_worker_{0}".format(worker_id)))
            )
        except Exception:
            self._local_writer = None
        # Per-worker action exploration: SAC.explore() is already stochastic, but
        # a small per-worker seeded noise guarantees distinct trajectories even
        # when all workers share the same published policy. Applied to the policy
        # action only (never to observations). Training-only.
        self.action_exploration_noise_std = float(
            OmegaConf.select(self.config, "Agent.action_exploration_noise_std", default=0.1)
        )
        self.action_exploration_throttle_bias = float(
            OmegaConf.select(self.config, "Agent.action_exploration_throttle_bias", default=0.0)
        )
        self.action_exploration_brake_bias = float(
            OmegaConf.select(self.config, "Agent.action_exploration_brake_bias", default=0.0)
        )
        self.exploration_seed = int(self.policy_seed)
        self._noise_rng = np.random.default_rng(self.exploration_seed)
        # deterministic_mode=False => stochastic training actions (explore()).
        # eval/inference can flip this to True to use the deterministic mean.
        self.deterministic_mode = False
        self.evaluation_frozen = False
        self.last_action_noise: list[float] = [0.0, 0.0, 0.0]
        self._last_mtime = 0.0
        self._start_steps = int(OmegaConf.select(self.config, "Agent.start_steps", default=1500))
        self._global_steps = 0
        self._weights_loaded = False
        self._load_error = ""
        self._metadata: dict | None = None
        self._checkpoint_path = ""
        self._schema_version: int | None = None
        self._training_step: int = 0
        self._reload_count: int = 0
        self._last_reload_ts: float = 0.0
        self._local_policy_version: int = 0
        # Robust hot-reload bookkeeping: a bad publish must never kill the worker.
        self._reload_failed_count: int = 0
        self._last_reload_error: str = ""
        self._last_good_policy_version: int = 0
        self._pending_policy_version: int = 0
        self._policy_file_size: int = 0
        # Episode-boundary swap: the active policy is only replaced between
        # episodes, never mid-drive. detect_pending_version() flags a newer
        # published version; apply_pending_policy() performs the real load at the
        # episode boundary.
        self._active_policy_version: int = 0
        self._force_policy_reset: bool = False
        self._detected_policy_version: int = 0
        self._swap_count: int = 0
        self._last_swap_episode: int = -1
        self.policy_apply_mode: str = "episode_boundary"

    def _build_algo(self) -> SAC:
        return SAC(
            state_dim=self.state_dim,
            action_dim=3,
            device=self.device,
            seed=self.policy_seed,
            **self._sac_cfg,
        )

    def is_compatible(self) -> bool:
        return bool(self._weights_loaded)

    def load_error(self) -> str:
        return str(self._load_error or "")

    def policy_status(self) -> dict:
        return {
            "policy_ready": bool(self._weights_loaded),
            "policy_state_dim": self.state_dim,
            "policy_action_dim": self.action_dim,
            "policy_schema_version": self._schema_version,
            "policy_training_step": self._training_step,
            "policy_checkpoint_path": self._checkpoint_path,
            "policy_load_error": self._load_error,
            "policy_seed": self.policy_seed,
            "worker_id": self.worker_id,
            "local_policy_version": int(self._local_policy_version),
            "policy_reload_count": int(self._reload_count),
            "last_policy_reload_age_sec": (
                time.time() - self._last_reload_ts if self._last_reload_ts else None
            ),
            "policy_reload_failed_count": int(self._reload_failed_count),
            "last_policy_reload_error": str(self._last_reload_error),
            "last_good_policy_version": int(self._last_good_policy_version),
            "pending_policy_version": int(self._pending_policy_version),
            "policy_file_size": int(self._policy_file_size),
            "active_policy_version": int(self._active_policy_version),
            "detected_policy_version": int(self._detected_policy_version),
            "policy_swap_pending": bool(self.has_pending_swap()),
            "policy_force_reset": bool(self._force_policy_reset),
            "policy_swap_count": int(self._swap_count),
            "last_swap_episode": int(self._last_swap_episode),
            "policy_apply_mode": str(self.policy_apply_mode),
            "deterministic_mode": bool(self.deterministic_mode),
            "exploration_seed": int(self.exploration_seed),
            "action_exploration_noise_std": float(self.action_exploration_noise_std),
            "action_exploration_throttle_bias": float(self.action_exploration_throttle_bias),
            "action_exploration_brake_bias": float(self.action_exploration_brake_bias),
            "last_action_noise": list(self.last_action_noise),
        }

    def available_published_version(self) -> int:
        """Latest fully-published version visible right now (0 if none)."""
        _src, version = self._resolve_source()
        return int(version)

    def detect_pending_version(self) -> bool:
        """Light check during driving: flag a newer published version.

        NEVER loads weights — that would swap the policy mid-episode. It only
        records that a newer version exists so apply_pending_policy() can load it
        at the next episode boundary.
        """
        if self.evaluation_frozen or self.pbt_enabled:
            return False
        try:
            available = self.available_published_version()
        except Exception:  # never disturb the driving loop
            return self.has_pending_swap()
        self._detected_policy_version = available
        if available > self._active_policy_version:
            self._pending_policy_version = available
        elif self._is_published_reset():
            # train wipe republishes version_00000000. That is older by number
            # but it is a new brain — must swap, not keep the 56k left-steer net.
            self._force_policy_reset = True
            self._pending_policy_version = available
        return self.has_pending_swap()

    def _is_published_reset(self) -> bool:
        if int(self._active_policy_version) <= 0 and int(self._training_step) <= 0:
            return False
        source_dir, _version = self._resolve_source()
        if source_dir is None:
            return False
        meta = read_metadata(source_dir) or {}
        created = str(meta.get("created_from", ""))
        step = int(meta.get("training_step", 0) or 0)
        return created in ("fresh", "wipe") and step == 0

    def has_pending_swap(self) -> bool:
        return (
            int(self._pending_policy_version) > int(self._active_policy_version)
            or bool(self._force_policy_reset)
        )

    def apply_pending_policy(self, episode_id: int = -1) -> bool:
        if self.evaluation_frozen or self.pbt_enabled:
            return False
        """Episode-boundary swap: load the newest published policy if one waits.

        Loads the latest available version (so if the learner published several
        times during the episode, only the most recent is applied). Uses the
        non-destructive loader, so a corrupt/partial publish keeps the current
        policy. Returns True if a swap happened.
        """
        if not self.has_pending_swap() and self._weights_loaded:
            return False
        before = self._active_policy_version
        before_algo = self.algo
        if self._force_policy_reset:
            self._last_mtime = 0.0
        loaded = self.reload_if_updated()
        swapped = bool(loaded) and (
            self._active_policy_version != before or self.algo is not before_algo
        )
        if swapped:
            self._swap_count += 1
            self._last_swap_episode = int(episode_id)
            self._pending_policy_version = self._active_policy_version
            self._force_policy_reset = False
            return True
        return False

    def note_global_steps(self, steps: int) -> None:
        self._global_steps = steps

    def _resolve_source(self) -> tuple[Path | None, int]:
        """Find the directory + version to load from.

        Prefers the atomic versioned pointer (latest.json). Falls back to the
        legacy in-place publish dir so older checkpoints still load.
        """
        pointer = read_pointer(self.model_dir)
        if pointer is not None:
            version_dir = self.model_dir / "publish_versions" / str(pointer.get("version_dir", ""))
            if version_dir.is_dir():
                return version_dir, int(pointer.get("policy_version", 0))
        if self.publish_dir.is_dir() and (self.publish_dir / "policy_net.pth").is_file():
            return self.publish_dir, 0
        return None, 0

    def reload_if_updated(self) -> bool:
        """Hot-reload the policy without ever destroying the working one.

        The new policy is loaded into a TEMPORARY SAC instance first. Only on a
        fully successful load do we swap it in. Any failure (empty/partial file,
        EOFError "Ran out of input", zip/pickle/RuntimeError, missing file,
        metadata mismatch) keeps the previous good policy and is recorded —
        never raised — so the worker keeps driving.
        """
        if self.pbt_enabled:
            return self._pbt_initialize()
        try:
            return self._reload_if_updated_impl()
        except Exception as exc:  # final safety net: never propagate to caller
            self._record_reload_failure(str(exc) or "policy_reload_unknown_error")
            return self._weights_loaded

    def _pbt_initialize(self) -> bool:
        """PBT startup: load the main brain if one exists, else keep the random
        in-memory nets (each worker starts as its own random brain)."""
        from ac_workers.pbt_resync import pbt_main_dir, reset_from_main

        if not self._weights_loaded:
            # Random in-memory nets are already built in __init__.
            self._weights_loaded = True
            self._load_error = ""
            self._checkpoint_path = "pbt_random_init"
        main_dir = pbt_main_dir(self.work_dir)
        try:
            if (main_dir / "policy_net.pth").is_file():
                if reset_from_main(self.work_dir, self.algo, worker_id=self.worker_id):
                    self._weights_loaded = True
                    self._load_error = ""
                    self._checkpoint_path = str(main_dir)
                    logger.info("pbt: worker=%s initialized from main brain", self.worker_id)
        except Exception as exc:
            logger.warning("pbt: init failed (worker=%s): %s", self.worker_id, exc)
        return self._weights_loaded

    def _record_reload_failure(self, err: str) -> None:
        self._reload_failed_count += 1
        self._last_reload_error = err
        self._load_error = err
        logger.warning(
            "policy reload kept old version (worker=%s): %s", self.worker_id, err
        )

    def _reload_if_updated_impl(self) -> bool:
        if self.evaluation_frozen:
            return self._weights_loaded
        source_dir, version = self._resolve_source()
        if source_dir is None:
            # Fresh train (`train new`): no publish yet. Drive with the
            # in-memory random nets already built in __init__ so scale-test
            # can idle and the learner can collect the first transitions.
            if not self._weights_loaded:
                if self.algo is None:
                    self.algo = self._build_algo()
                self._weights_loaded = True
                self._load_error = ""
                self._checkpoint_path = "random_init"
                self._last_mtime = 0.0
            return True

        # mtime gate: only attempt a reload when the marker advanced.
        if self.latest_marker.is_file():
            mtime = self.latest_marker.stat().st_mtime
        else:
            mtime = source_dir.stat().st_mtime
        if mtime <= self._last_mtime and self._weights_loaded:
            return True

        policy_path = source_dir / "policy_net.pth"
        if not policy_path.is_file():
            self._last_mtime = mtime
            if not self._weights_loaded:
                if self.algo is None:
                    self.algo = self._build_algo()
                self._weights_loaded = True
                self._load_error = ""
                self._checkpoint_path = "random_init"
            else:
                self._record_reload_failure("policy_checkpoint_missing")
            return self._weights_loaded
        try:
            file_size = policy_path.stat().st_size
        except OSError as exc:
            self._record_reload_failure("policy_stat_failed:{0}".format(exc))
            return self._weights_loaded
        if file_size <= 0:
            # Half-written file: leave mtime so we retry on the next bump.
            self._pending_policy_version = version
            self._record_reload_failure("policy_file_empty")
            return self._weights_loaded

        meta = read_metadata(source_dir)
        if meta is None:
            self._record_reload_failure("metadata_missing_start_new_training")
            return self._weights_loaded
        if meta is not None:
            ok_meta, meta_reason = validate_metadata(
                meta, expected_state_dim=self.state_dim, expected_action_dim=self.action_dim
            )
            if not ok_meta:
                self._last_mtime = mtime
                if meta_reason == "policy_state_dim_mismatch":
                    archive_incompatible_policy_once(self.work_dir, self.state_dim)
                self._record_reload_failure(meta_reason)
                return self._weights_loaded

        self._pending_policy_version = version
        # Load into a temp instance so a corrupt file can't poison the live net.
        temp_algo = self._build_algo()
        try:
            temp_algo.load_models(str(source_dir))
        except Exception as exc:  # EOFError/RuntimeError/zip/pickle/OSError/...
            err = str(exc)
            if "size mismatch" in err.lower() or "shape" in err.lower():
                self._last_mtime = mtime
                archive_incompatible_policy_once(self.work_dir, self.state_dim)
                self._record_reload_failure("policy_state_dim_mismatch")
            else:
                # Likely a mid-write read; keep mtime unchanged to retry shortly.
                self._record_reload_failure(err or "policy_load_failed")
            return self._weights_loaded

        # Success — atomically swap the live policy.
        self.algo = temp_algo
        self._last_mtime = mtime
        self._weights_loaded = True
        self._load_error = ""
        self._last_reload_error = ""
        self._checkpoint_path = str(source_dir)
        self._policy_file_size = file_size
        self._reload_count += 1
        self._last_reload_ts = time.time()
        if meta is not None:
            self._metadata = meta
            self._schema_version = int(meta.get("schema_version", -1))
            self._training_step = int(meta.get("training_step", 0))
        # Keep version 0 as 0 (train wipe). `version or reload_count` used to
        # hide a reset and leave workers on the old high-numbered brain.
        self._local_policy_version = int(version)
        self._last_good_policy_version = self._local_policy_version
        self._active_policy_version = self._local_policy_version
        if self._pending_policy_version < self._active_policy_version:
            self._pending_policy_version = self._active_policy_version
        return True

    # ---- PBT per-worker local training ----

    def local_append(self, obs, act, reward, nxt, terminated, done) -> None:
        """Append this worker's own transition to its local brain replay buffer."""
        try:
            self.local_replay.append(
                np.asarray(obs, dtype=np.float32).ravel(),
                np.asarray(act, dtype=np.float32).ravel(),
                float(reward),
                np.asarray(nxt, dtype=np.float32).ravel(),
                bool(terminated),
                episode_done=bool(done),
                stream_id=self.worker_id,
            )
            self._local_transitions_since_update += 1
        except Exception as exc:
            logger.warning("local_append failed (worker=%s): %s", self.worker_id, exc)

    def local_update(self) -> bool:
        """One local SAC gradient step from this worker's own replay buffer."""
        try:
            if len(self.local_replay) < self.local_batch_size:
                return False
            batch = self.local_replay.sample(self.local_batch_size, self.device)
            writer = self._local_writer if self._local_writer is not None else _NullWriter()
            self.algo.update_online_networks(batch, writer)
            self.algo.update_target_networks()
            self._local_updates += 1
            self._local_transitions_since_update = 0
            return True
        except Exception as exc:
            logger.warning("local_update failed (worker=%s): %s", self.worker_id, exc)
            return False

    def maybe_local_update(self) -> bool:
        """Local update when enough own transitions accumulated since last update."""
        if self._local_transitions_since_update < self.local_update_interval:
            return False
        return self.local_update()

    def local_updates(self) -> int:
        return int(self._local_updates)

    # ---- PBT resynchronization (Step 2 + Step 3) ----

    def pbt_resync(self, score: float) -> str:
        """PBT resync at an episode boundary: promote this brain if it beat the
        main brain, reset from the main brain if it did much worse, else keep it.
        On reset, perturb the exploration hyperparameters (Step 3)."""
        from ac_workers.pbt_resync import resync

        result = resync(
            self.work_dir,
            self.algo,
            float(score),
            self.worker_id,
            reset_margin=float(
                OmegaConf.select(self.config, "Agent.pbt_reset_margin", default=2.0)
            ),
        )
        if result == "reset":
            self._mutate_exploration()
        return result

    def _mutate_exploration(self) -> None:
        """PBT explore step: re-roll exploration hyperparameters after a reset so
        the reset worker explores differently from the main brain."""
        import random

        self.action_exploration_noise_std = random.uniform(0.0, 0.15)
        self.action_exploration_throttle_bias = random.uniform(-0.05, 0.05)
        self.action_exploration_brake_bias = random.uniform(-0.05, 0.05)
        logger.info(
            "pbt: worker=%s mutated exploration noise=%.3f throttle_bias=%.3f brake_bias=%.3f",
            self.worker_id,
            self.action_exploration_noise_std,
            self.action_exploration_throttle_bias,
            self.action_exploration_brake_bias,
        )

    def select_action(self, state: np.ndarray, env) -> np.ndarray:
        return np.asarray(self.select_action_traced(state, env)["action"], dtype=np.float32)

    def select_action_traced(self, state: np.ndarray, env) -> dict:
        state_arr = np.asarray(state, dtype=np.float32).ravel()
        neutral = np.array([0.0, -1.0, -1.0], dtype=np.float32)
        from ac_workers.canonical_controls import raw_action_to_physical

        trace: dict = {
            "policy_action_source": "neutral_fallback",
            "policy_raw_action": [0.0, -1.0, -1.0],
            "physical_action": [0.0, 0.0, 0.0],
            "action": neutral,
            "error": "",
            "policy_seed": self.policy_seed,
            "rng_seed": self.policy_seed,
            "observation_hash": observation_hash(state_arr),
            "worker_id": self.worker_id,
        }
        trace["deterministic_mode"] = bool(self.deterministic_mode)
        trace["exploration_seed"] = int(self.exploration_seed)
        trace["action_noise"] = [0.0, 0.0, 0.0]
        if not self._weights_loaded:
            trace["policy_action_source"] = "no_weights"
            trace["error"] = self.load_error() or "weights_not_loaded"
            return trace
        if self.deterministic_mode:
            action, _ = self.algo.exploit(state_arr)
            source = "sac_exploit"
        else:
            action, _ = self.algo.explore(state_arr)
            source = "sac_explore"
        raw = [float(x) for x in np.asarray(action, dtype=np.float32).ravel()[:3]]
        noise = [0.0, 0.0, 0.0]
        # Add per-worker exploration noise only in stochastic training mode.
        if not self.deterministic_mode:
            noise_arr = (self._noise_rng.normal(
                0.0, self.action_exploration_noise_std, size=3
            ) if self.action_exploration_noise_std > 0.0 else np.zeros(3))
            # Bias throttle up / brake down so exploration prefers moving forward.
            noise_arr = np.asarray(noise_arr, dtype=np.float64)
            if self.action_exploration_throttle_bias != 0.0:
                noise_arr[1] = float(noise_arr[1]) + float(self.action_exploration_throttle_bias)
            if self.action_exploration_brake_bias != 0.0:
                noise_arr[2] = float(noise_arr[2]) + float(self.action_exploration_brake_bias)
            noise = [float(x) for x in noise_arr]
            raw = [float(np.clip(raw[i] + noise[i], -1.0, 1.0)) for i in range(3)]
        self.last_action_noise = noise
        physical = raw_action_to_physical(raw)
        trace.update(
            {
                "policy_action_source": source,
                "policy_raw_action": raw,
                "physical_action": [float(physical[0]), float(physical[1]), float(physical[2])],
                "action": np.asarray(raw, dtype=np.float32),
                "action_noise": noise,
                "deterministic_mode": bool(self.deterministic_mode),
                "exploration_seed": int(self.exploration_seed),
            }
        )
        return trace
