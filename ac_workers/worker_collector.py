"""Per-worker collector: diag first, sole UDP/vJoy owner (C4D)."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __name__ == "__main__":
    # Own the log files: the launcher's redirected pipes disappear when it exits.
    for _stream_name in ("stdout", "stderr"):
        _log_file = os.environ.get("AC_WORKER_" + _stream_name.upper() + "_LOG")
        if _log_file:
            setattr(sys, _stream_name, open(_log_file, "a", encoding="utf-8", buffering=1))

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))

from AssettoCorsaEnv.ac_worker_runtime import apply_worker_env, load_worker_config, restore_worker_env
from ac_workers.collector_diag import CollectorDiagServer
from ac_workers.collector_shutdown import clear_collector_shutdown, collector_shutdown_requested
from ac_workers.collector_resume import clear_collector_resume, collector_resume_requested
from ac_workers.reward_search_drive import search_drive_active
from ac_workers.drive_truth import (
    REASON_VJOY_NOT_REACHING_CAR,
    is_gas_without_movement,
    movement_watchdog_tripped,
)
from ac_workers.vjoy_pool import vjoy_failure_is_dead, vjoy_worker_status
from ac_workers import pipeline_timeline
from ac_workers.collector_status import safe_write_status_json
from ac_workers.vjoy_ledger import clear_owner as _clear_vjoy_owner
from ac_workers.vjoy_ledger import record_owner as _record_vjoy_owner
from ac_workers.worker_ports import worker_ports
from common.training_contract import ACTION_SCHEMA

logger = logging.getLogger(__name__)
STATUS_DIR = REPO / "ac_workers" / "status"
_STATUS_IO_LOCK = threading.RLock()

# Large blobs belong in the status file, not on the diag wire (keeps responses fast
# and avoids blocking the diag thread behind runtime.lock).
_DIAG_HANDOFF_STRIP = frozenset(
    {
        "last_observation",
        "telemetry_snapshot",
        "observation_missing_components",
        "policy_raw_action",
        "physical_action",
        "raw_action",
        "last_requested_action",
        "last_executed_action",
        "startup_requested_internal_action",
        "initial_gear_samples",
        "startup_final_neutral_readback",
        "action_noise",
        "last_action_noise",
        "vjoy_startup",
        "control_smoke_test",
        "gear_mapping",
    }
)

# After each successful episode reset, hold neutral controls briefly so random
# exploration does not instantly steer off track (-5 terminal OOT reward).
POST_RESET_GRACE_STEPS = 25
MAX_EPISODE_RESET_ATTEMPTS = 5
POST_RESET_GRACE_ACTION = [0.0, -1.0, -1.0]
ANTI_DOUBLE_RESET_STEPS = 50  # min steps before allowing another done=True after reset (~2s at 25Hz)
# Learner-active gating: the SAC learner pings the transition server every loop
# iteration; if no ping arrives within this window the learner is considered dead.
# Collectors then hold NEUTRAL (coast) — they never force a brake. The learner
# also pings on a 1s background thread during long SAC/publish work; keep a modest
# margin above that interval for transition-server jitter.
LEARNER_HEARTBEAT_TIMEOUT_MS = 10000.0
LEARNER_CHECK_INTERVAL_SEC = 0.5
# Async actor-learner control modes.
CONTROL_MODE_LOCAL_ACTOR = "async_local_actor"  # primary: local actor with hot-reloaded policy
CONTROL_MODE_LOCAL = "async_local"              # alias kept for compat
CONTROL_MODE_ACTOR = "async_actor"              # debug fallback: RPC per action
# Per-action long-poll window: worker waits this long for the learner to answer
# before applying a safe coast action and counting a timeout.
ACTOR_REQUEST_WAIT_MS = 250.0
# Safe coast on a transient action timeout (learner alive but slow): steer 0,
# throttle 0, brake 0 -> raw [0,-1,-1]. Not a hard brake; the learner-not-running
# gate handles the hard safe stop separately.
LEARNER_TIMEOUT_RAW_ACTION = [0.0, -1.0, -1.0]


def pedal_telemetry(state) -> dict[str, float]:
    from ac_workers.drive_truth import pedal_from_state
    from AssettoCorsaEnv.gear_shift import gear_raw

    return {
        "steer": float(state.get("steerAngle", state.get("steer", 0.0)) or 0.0),
        "gas": pedal_from_state(state),
        "brake": float(state.get("brakeStatus", state.get("brake", 0.0)) or 0.0),
        "speed": float(state.get("speed", 0.0) or 0.0),
        "gear": float(gear_raw(state)),
        "spline": float(state.get("NormalizedSplinePosition", 0.0) or 0.0),
    }


def poll_collector_telemetry(env: Any, runtime: CollectorRuntime) -> bool:
    """Keep telemetry fresh while idle or waiting for the learner (no env.step)."""
    try:
        env.client.get_servers_input()
        env.client.respond_to_server()
    except (OSError, AttributeError, KeyError, TypeError, ValueError):
        return False
    with runtime.lock:
        runtime.last_telemetry_ts = time.time()
        snap = pedal_telemetry(env.client.state)
        runtime.telemetry_snapshot = {
            "steer": snap["steer"],
            "gas": snap["gas"],
            "brake": snap["brake"],
            "speed": snap["speed"],
            "gear": snap["gear"],
            "spline": snap.get("spline", 0.0),
        }
        runtime._telemetry_ready = True
    return True


def _status_path(worker_id: int) -> Path:
    return STATUS_DIR / ("worker_{0}.json".format(worker_id))


from ac_workers.collector_startup_log import configure_startup_log, startup_log as _startup_log


class CollectorRuntime:
    def __init__(self, worker_id: int, vjoy_device: int, mgmt_port: int):
        self.worker_id = worker_id
        self.vjoy_device = vjoy_device
        self.mgmt_port = mgmt_port
        self.ports = worker_ports(worker_id)
        self.lock = threading.RLock()
        self.session_id = os.environ.get("AC_AI_SESSION_ID", "")
        self.collector_pid = os.getpid()
        # Unique per process start: lets identity_test prove it is talking to THIS
        # live collector (not a stale port from a previous run / wrong process).
        self.boot_id = uuid.uuid4().hex[:12]
        # Last vJoy device this collector physically wrote to (authoritative pulse /
        # control loop). Proves which device the worker's own handle drives.
        self.last_vjoy_write_device: int | None = None
        self.last_vjoy_pulse: dict = {}
        self.last_vjoy_selftest: dict = {}
        self._diag_server: Any = None  # CollectorDiagServer, set once diag thread starts
        self._diag_bind_error: str = ""
        self._diag_handoff: dict = {}
        self._diag_handoff_lock = threading.Lock()
        self.collector_started_at = datetime.now(timezone.utc).isoformat()
        self.last_telemetry_ts = 0.0
        self.last_observation: list[float] = []
        self.last_requested_action = [0.0, 0.0, 0.0]
        self.last_executed_action = [0.0, 0.0, 0.0]
        self.telemetry_snapshot: dict = {}
        self.transition_count = 0
        self.telemetry_frame_count = 0
        self.episode_total_reward = 0.0
        self.last_reward = 0.0
        # Neuroévolution : métriques d'évaluation de l'épisode en cours.
        self.episode_laps = 0
        self.episode_progress_m = 0.0
        self.episode_best_lap_time = 0.0
        self._last_candidate_score = float("-inf")
        self._current_candidate = -1
        self._neuroevolution = None
        self.heartbeat_ts = time.time()
        self.heartbeat_monotonic = time.monotonic()
        self.env = None
        self.policy = None
        self.phase = "starting"
        self._control_enabled = False
        self._last_safety_cause = ""
        self._cached_snapshot: dict = {}
        self._telemetry_ready = False
        self.last_error = ""
        self.current_reset_id = 0
        self.gear_mapping: dict = {}
        self.control_smoke_test_report: dict = {}
        self.control_smoke_test_result = ""
        self.drive_state_report: dict = {}
        self.vjoy_startup_report: dict = {}
        self.initial_session_attached = False
        self.vjoy_device_acquired = False
        self.vjoy_axis_verified = False
        self.initial_gear_engaged = False
        self.vjoy_neutralization_verified = False
        self.controls_live_reported = None
        self.controls_live_bypassed_for_initial_attach = False
        self.initial_gear_before_raw = None
        self.initial_gear_after_raw = None
        self.initial_gear_samples: list = []
        self.startup_throttle_assist_used = False
        self.startup_throttle_assist_fraction = 0.0
        self.startup_telemetry_fresh = False
        self.startup_controls_object_id = None
        self.startup_override_active = False
        self.startup_requested_internal_action: list = []
        self.startup_requested_pedal_fraction = 0.0
        self.startup_readback_pedal_fraction = 0.0
        self.startup_control_write_count = 0
        self.startup_neutral_overwrite_detected = False
        self.startup_last_control_writer = ""
        self.startup_final_neutral_readback: dict = {}
        self.startup_override_active_final = False
        self.reset_controls_object_id = None
        self.reset_vjoy_device_status_before = None
        self.reset_vjoy_reacquire_attempted = False
        self.reset_vjoy_reacquire_succeeded = False
        self.reset_update_vjd_attempts = 0
        self.reset_neutralization_verified = False
        self.reset_gear_before_raw = None
        self.reset_gear_after_raw = None
        self.reset_last_error = ""
        self.observation_length = 0
        self.observation_expected_length = 50
        self.observation_missing_components: list[str] = []
        self.observation_history_frames = 0
        self.observation_history_required = 3
        self.observation_valid_for_policy = False
        self.policy_action_source = ""
        self.policy_raw_action = [0.0, -1.0, -1.0]
        self.physical_action = [0.0, 0.0, 0.0]
        self.policy_seed = 0
        self.observation_hash = ""
        self.canonical_controls_called = False
        self.canonical_controls_object_id = None
        self.canonical_local_controls_object_id = None
        self.canonical_update_vjd_result = False
        self.startup_local_controls_object_id = None
        self.policy_local_controls_object_id = None
        self.startup_vjoy_object_id = None
        self.policy_vjoy_object_id = None
        self.device_status_before_update = None
        self.episode_count = 0
        self.episode_started_at = time.monotonic()
        self.current_episode_id = 0
        self.steps_since_episode_start = 0
        # Exploration / safety transparency (filled from policy trace + branches).
        self.last_action_noise = [0.0, 0.0, 0.0]
        self.deterministic_mode = False
        self.exploration_seed = 0
        self.safe_stop_active = False
        self.safe_stop_reason = ""
        self.brake_forced_by_safety = False
        self.last_episode_duration_seconds = 0.0
        self.average_episode_duration_seconds = 0.0
        self.reset_count = 0
        self.last_reset_reason = ""
        self.reset_fail_streak = 0
        self.post_reset_grace_steps = 0
        self._policy_paused = False
        self._calibration_mode = False
        self._evaluation_restore_paused = False
        self._evaluation_requested = False
        self.evaluation = None
        # Learner-active gating: collectors must only drive with the policy while
        # the SAC learner is alive (recent learner_ping on the transition server).
        # When the learner stops or its heartbeat goes stale, the worker brakes to
        # a safe stop instead of continuing to drive a frozen local policy.
        self.learner_required = True
        self._learner_active = False
        self._learner_heartbeat_age_ms = 999999.0
        self._learner_check_ts = 0.0
        self.policy_paused_reason = ""
        # Async actor-learner runtime state. control_mode is mirrored from the
        # transition server so `control-mode <mode>` switches live without restart.
        self.control_mode = CONTROL_MODE_LOCAL_ACTOR
        self._action_req_seq = 0
        self.last_action_request_id = ""
        self.last_action_policy_version = 0
        self.last_action_latency_ms = 0.0
        self.learner_rpc_ok = False
        self.learner_rpc_timeout_count = 0
        # Workers stay idle (car at neutral) after attach and only begin driving
        # with the policy once the learner starts them ("resume").
        self._started = False
        self._reset_requested = False
        self._safety_hold_since = 0.0
        self._reset_reason_pending = ""
        self._pending_candidate_idx = None
        self._transition_client = None
        self._probe_cache_ts = 0.0
        self._probe_cache: dict[str, Any] = {}
        # Movement watchdog: consecutive steps where the policy commanded real
        # throttle but the car did not move -> vJoy input not reaching AC.
        self._gas_no_move_steps = 0
        self._stall_recover_pulses = 0
        # Consecutive runtime vJoy write failures (UpdateVJD/AcquireVJD). Recover
        # via episode reset for the first few; only stop after sustained failure.
        self._vjoy_update_fail_steps = 0
        self.update_vjd_success_count = 0
        self.update_vjd_fail_count = 0
        self.policy_control_write_count = 0

    def touch_heartbeat(self) -> None:
        self.heartbeat_ts = time.time()
        self.heartbeat_monotonic = time.monotonic()

    def transition_client(self):
        if self._transition_client is None:
            from common.transition_server import TransitionClient

            self._transition_client = TransitionClient()
        return self._transition_client

    def refresh_learner_state(self, force: bool = False) -> bool:
        """Update cached learner liveness from the transition server heartbeat.

        The SAC learner calls learner_ping() on every loop iteration, so a recent
        learner_last_seen means the learner is actively training. We throttle the
        stats query so the per-step driving loop does not add a TCP round-trip
        every tick.
        """
        now = time.monotonic()
        if not force and (now - self._learner_check_ts) < LEARNER_CHECK_INTERVAL_SEC:
            return self._learner_active
        self._learner_check_ts = now
        age_ms = 999999.0
        active = False
        try:
            stats = self.transition_client().stats()
            last_seen = float(stats.get("learner_last_seen") or 0.0)
            if last_seen > 0:
                age_ms = max(0.0, (time.time() - last_seen) * 1000.0)
                active = age_ms <= LEARNER_HEARTBEAT_TIMEOUT_MS
            mode = str(stats.get("control_mode") or "")
            if mode:
                self.control_mode = mode
        except (OSError, ValueError, TypeError, KeyError):
            active = False
        self._learner_heartbeat_age_ms = age_ms
        self._learner_active = active
        return active

    def request_learner_action(self, state: Any) -> dict:
        """Async actor mode: ask the central learner for this worker's next action.

        Returns a trace dict compatible with apply_action_trace(). On timeout it
        falls back to a safe coast action and counts a learner_rpc timeout — it
        never reuses a stale dangerous action.
        """
        from ac_workers.action_diagnostics import observation_hash

        state_arr = np.asarray(state, dtype=np.float32).ravel()
        self._action_req_seq += 1
        request_id = "{0}-{1}".format(self.worker_id, self._action_req_seq)
        obs_hash = observation_hash(state_arr)
        t0 = time.monotonic()
        resp: dict = {}
        try:
            resp = self.transition_client().request_action(
                self.worker_id,
                state_arr.tolist(),
                request_id,
                wait_ms=ACTOR_REQUEST_WAIT_MS,
            )
        except OSError:
            resp = {}
        latency_ms = (time.monotonic() - t0) * 1000.0
        self.last_action_request_id = request_id
        self.last_action_latency_ms = latency_ms
        if resp.get("ok") and resp.get("ready"):
            raw = [float(x) for x in (resp.get("action") or [0.0, -1.0, -1.0])][:3]
            self.learner_rpc_ok = True
            self.last_action_policy_version = int(resp.get("policy_version", 0) or 0)
            return {
                "policy_action_source": "learner_rpc",
                "policy_raw_action": raw,
                "action": np.asarray(raw, dtype=np.float32),
                "policy_version": self.last_action_policy_version,
                "request_id": request_id,
                "latency_ms": latency_ms,
                "observation_hash": obs_hash,
                "policy_seed": int(self.policy_seed),
            }
        self.learner_rpc_ok = False
        self.learner_rpc_timeout_count += 1
        return {
            "policy_action_source": "learner_timeout",
            "policy_raw_action": list(LEARNER_TIMEOUT_RAW_ACTION),
            "action": np.asarray(LEARNER_TIMEOUT_RAW_ACTION, dtype=np.float32),
            "policy_version": self.last_action_policy_version,
            "request_id": request_id,
            "latency_ms": latency_ms,
            "observation_hash": obs_hash,
            "policy_seed": int(self.policy_seed),
        }

    def policy_block_reason(self) -> str:
        """Why policy driving is currently blocked ("" means drive normally)."""
        if self._calibration_mode:
            return ""
        if getattr(self, "_neuroevolution", None) is not None:
            # La neuroévolution conduit avec sa propre politique, sans learner.
            return ""
        if self._policy_paused:
            return "manual_pause"
        if self.learner_required and not self._learner_active and self.evaluation is None:
            # A reward search drives the saved model without training.
            if not search_drive_active():
                return "learner_not_running"
        return ""

    def set_phase(self, phase: str, error: str = "") -> None:
        self.phase = phase
        if error:
            self.last_error = error
        elif phase in ("running", "idle"):
            self.last_error = ""
        self.touch_heartbeat()

    def telemetry_age_ms(self) -> float:
        if self.last_telemetry_ts <= 0:
            return 999999.0
        return (time.time() - self.last_telemetry_ts) * 1000.0

    def observation_complete(self) -> bool:
        from ac_workers.collector_observation import assess_observation

        assess = assess_observation(
            self.last_observation,
            phase=self.phase,
            telemetry_ready=self._telemetry_ready,
            telemetry_age_ms=self.telemetry_age_ms(),
            telemetry_snapshot=self.telemetry_snapshot,
            history_frames=self.observation_history_frames,
        )
        self.observation_length = int(assess["observation_length"])
        self.observation_expected_length = int(assess["observation_expected_length"])
        self.observation_missing_components = list(assess["observation_missing_components"])
        self.observation_valid_for_policy = bool(assess["observation_valid_for_policy"])
        return bool(assess["observation_complete"])

    def note_observation_state(self, obs: list[float], history_frames: int = 0) -> None:
        from ac_workers.collector_observation import assess_observation

        assess = assess_observation(
            obs,
            phase=self.phase,
            telemetry_ready=self._telemetry_ready,
            telemetry_age_ms=self.telemetry_age_ms(),
            telemetry_snapshot=self.telemetry_snapshot,
            history_frames=history_frames,
        )
        self.observation_length = int(assess["observation_length"])
        self.observation_expected_length = int(assess["observation_expected_length"])
        self.observation_missing_components = list(assess["observation_missing_components"])
        self.observation_history_frames = int(history_frames)
        self.observation_valid_for_policy = bool(assess["observation_valid_for_policy"])

    def apply_action_trace(self, trace: dict, canonical_result: dict) -> None:
        self.policy_action_source = str(trace.get("policy_action_source", ""))
        self.policy_raw_action = list(trace.get("policy_raw_action", [0.0, -1.0, -1.0]))
        self.physical_action = list(canonical_result.get("physical_action", [0.0, 0.0, 0.0]))
        self.policy_seed = int(trace.get("policy_seed", 0) or 0)
        self.last_action_noise = list(trace.get("action_noise", [0.0, 0.0, 0.0]))
        self.deterministic_mode = bool(trace.get("deterministic_mode", False))
        self.exploration_seed = int(trace.get("exploration_seed", 0) or 0)
        # Normal driving from the local actor: not a safety stop.
        self.safe_stop_active = False
        self.safe_stop_reason = ""
        self.brake_forced_by_safety = False
        self.observation_hash = str(trace.get("observation_hash", "") or "")
        if "policy_version" in trace:
            self.last_action_policy_version = int(trace.get("policy_version", 0) or 0)
        if "request_id" in trace:
            self.last_action_request_id = str(trace.get("request_id", "") or "")
        if "latency_ms" in trace:
            self.last_action_latency_ms = float(trace.get("latency_ms", 0.0) or 0.0)
        self.canonical_controls_called = bool(canonical_result.get("canonical_controls_called", False))
        self.canonical_controls_object_id = canonical_result.get("canonical_controls_object_id")
        self.canonical_local_controls_object_id = canonical_result.get("canonical_local_controls_object_id")
        self.canonical_update_vjd_result = bool(canonical_result.get("update_vjd_result", False))
        self.policy_local_controls_object_id = canonical_result.get("canonical_local_controls_object_id")
        self.policy_vjoy_object_id = canonical_result.get("vjoy_object_id")
        self.device_status_before_update = canonical_result.get("device_status_before_update")

    def note_episode_completed(self, reason: str) -> None:
        duration = max(0.0, time.monotonic() - float(self.episode_started_at))
        self.episode_count += 1
        self.last_episode_duration_seconds = duration
        n = self.episode_count
        prev = float(self.average_episode_duration_seconds)
        self.average_episode_duration_seconds = (
            (prev * (n - 1) + duration) / n if n > 0 else duration
        )
        self.last_reset_reason = reason
        self.reset_count += 1
        self.episode_started_at = time.monotonic()

    def minimal_snapshot(self) -> dict:
        telemetry_frames = int(self.telemetry_frame_count)
        env = self.env
        if env is not None:
            client = getattr(env, "client", None)
            if client is not None:
                telemetry_frames = int(getattr(client, "_telemetry_frame_count", telemetry_frames))
        snap = {
            "worker_id": self.worker_id,
            "session_id": self.session_id,
            "collector_pid": self.collector_pid,
            "boot_id": self.boot_id,
            "last_vjoy_write_device": self.last_vjoy_write_device,
            "collector_started_at": self.collector_started_at,
            "status_written_at": datetime.now(timezone.utc).isoformat(),
            "phase": self.phase,
            "diag_port": self.ports.diag,
            "ego_port": self.ports.ego,
            "mgmt_port": self.ports.mgmt,
            "vjoy_device": self.vjoy_device,
            "transition_port": 24050,
            "telemetry_age_ms": self.telemetry_age_ms(),
            "last_observation": list(self.last_observation),
            "last_requested_action": list(self.last_requested_action),
            "last_executed_action": list(self.last_executed_action),
            "transition_count": self.transition_count,
            "telemetry_frame_count": telemetry_frames,
            "last_reward": self.last_reward,
            "heartbeat_age_ms": (time.time() - self.heartbeat_ts) * 1000.0,
            "heartbeat_monotonic": self.heartbeat_monotonic,
            "telemetry_snapshot": dict(self.telemetry_snapshot),
            "observation_complete": self.observation_complete(),
            "observation_length": self.observation_length,
            "observation_expected_length": self.observation_expected_length,
            "observation_missing_components": list(self.observation_missing_components),
            "observation_history_frames": self.observation_history_frames,
            "observation_history_required": self.observation_history_required,
            "observation_valid_for_policy": self.observation_valid_for_policy,
            "policy_action_source": self.policy_action_source,
            "policy_raw_action": list(self.policy_raw_action),
            "physical_action": list(self.physical_action),
            "policy_seed": int(self.policy_seed),
            "observation_hash": str(self.observation_hash),
            "canonical_controls_called": bool(self.canonical_controls_called),
            "canonical_controls_object_id": self.canonical_controls_object_id,
            "canonical_local_controls_object_id": self.canonical_local_controls_object_id,
            "canonical_update_vjd_result": bool(self.canonical_update_vjd_result),
            "startup_local_controls_object_id": self.startup_local_controls_object_id,
            "policy_local_controls_object_id": self.policy_local_controls_object_id,
            "startup_vjoy_object_id": self.startup_vjoy_object_id,
            "policy_vjoy_object_id": self.policy_vjoy_object_id,
            "device_status_before_update": self.device_status_before_update,
            "episode_count": int(self.episode_count),
            "neuroevolution_candidate": int(self._current_candidate),
            "neuroevolution_score": float(self._last_candidate_score),
            "neuroevolution_laps": int(self.episode_laps),
            "neuroevolution_progress_m": float(self.episode_progress_m),
            "episode_started_at": float(self.episode_started_at),
            "last_episode_duration_seconds": float(self.last_episode_duration_seconds),
            "average_episode_duration_seconds": float(self.average_episode_duration_seconds),
            "reset_count": int(self.reset_count),
            "last_reset_reason": str(self.last_reset_reason),
            "policy_paused": bool(self._policy_paused or self.policy_block_reason()),
            "policy_paused_reason": str(self.policy_paused_reason or self.policy_block_reason()),
            "learner_required": bool(self.learner_required),
            "learner_active": bool(self._learner_active),
            "learner_heartbeat_age_ms": float(self._learner_heartbeat_age_ms),
            "control_mode": str(self.control_mode),
            "local_actor_enabled": bool(
                self.control_mode in (CONTROL_MODE_LOCAL_ACTOR, CONTROL_MODE_LOCAL)
            ),
            "action_source": str(self.policy_action_source),
            "local_policy_version": int(
                self.policy.policy_status().get("local_policy_version", 0) if self.policy else 0
            ),
            "last_loaded_policy_version": int(
                self.policy.policy_status().get("local_policy_version", 0) if self.policy else 0
            ),
            "last_policy_reload_age_sec": (
                self.policy.policy_status().get("last_policy_reload_age_sec") if self.policy else None
            ),
            "policy_reload_count": int(
                self.policy.policy_status().get("policy_reload_count", 0) if self.policy else 0
            ),
            "policy_reload_failed_count": int(
                self.policy.policy_status().get("policy_reload_failed_count", 0) if self.policy else 0
            ),
            "last_policy_reload_error": str(
                self.policy.policy_status().get("last_policy_reload_error", "") if self.policy else ""
            ),
            "last_good_policy_version": int(
                self.policy.policy_status().get("last_good_policy_version", 0) if self.policy else 0
            ),
            "policy_file_size": int(
                self.policy.policy_status().get("policy_file_size", 0) if self.policy else 0
            ),
            "active_policy_version": int(
                self.policy.policy_status().get("active_policy_version", 0) if self.policy else 0
            ),
            "pending_policy_version": int(
                self.policy.policy_status().get("pending_policy_version", 0) if self.policy else 0
            ),
            "policy_swap_pending": bool(
                self.policy.policy_status().get("policy_swap_pending", False) if self.policy else False
            ),
            "policy_swap_count": int(
                self.policy.policy_status().get("policy_swap_count", 0) if self.policy else 0
            ),
            "last_swap_episode": int(
                self.policy.policy_status().get("last_swap_episode", -1) if self.policy else -1
            ),
            "policy_apply_mode": str(
                self.policy.policy_status().get("policy_apply_mode", "episode_boundary")
                if self.policy else "episode_boundary"
            ),
            "episode_id": int(self.current_episode_id),
            "steps_since_episode_start": int(self.steps_since_episode_start),
            "raw_action": list(self.policy_raw_action),
            "action_noise": list(self.last_action_noise),
            "exploration_seed": int(self.exploration_seed),
            "deterministic_mode": bool(self.deterministic_mode),
            "safe_stop_active": bool(self.safe_stop_active),
            "safe_stop_reason": str(self.safe_stop_reason),
            "brake_forced_by_safety": bool(self.brake_forced_by_safety),
            "action_exploration_noise_std": float(
                self.policy.policy_status().get("action_exploration_noise_std", 0.0)
                if self.policy else 0.0
            ),
            "policy_load_error": str(
                self.policy.load_error() if self.policy else ""
            ),
            "worker_seed": int(self.policy_seed),
            "last_action_request_id": str(self.last_action_request_id),
            "last_action_policy_version": int(self.last_action_policy_version),
            "last_action_latency_ms": float(self.last_action_latency_ms),
            "learner_rpc_ok": bool(self.learner_rpc_ok),
            "learner_rpc_timeout_count": int(self.learner_rpc_timeout_count),
            "started": bool(self._started),
            "control_enabled": self._control_enabled,
            "last_safety_cause": self._last_safety_cause,
            "last_error": self.last_error,
            "gear_mapping": dict(self.gear_mapping) if self.gear_mapping else {},
            "initial_session_attached": bool(self.initial_session_attached),
            "vjoy_device_acquired": bool(self.vjoy_device_acquired),
            "vjoy_axis_verified": bool(self.vjoy_axis_verified),
            "initial_gear_engaged": bool(self.initial_gear_engaged),
            "vjoy_neutralization_verified": bool(self.vjoy_neutralization_verified),
            "controls_live_reported": self.controls_live_reported,
            "controls_live_bypassed_for_initial_attach": bool(
                self.controls_live_bypassed_for_initial_attach
            ),
            "initial_gear_before_raw": self.initial_gear_before_raw,
            "initial_gear_after_raw": self.initial_gear_after_raw,
            "initial_gear_samples": list(self.initial_gear_samples),
            "startup_throttle_assist_used": bool(self.startup_throttle_assist_used),
            "startup_throttle_assist_fraction": float(self.startup_throttle_assist_fraction),
            "startup_telemetry_fresh": bool(self.startup_telemetry_fresh),
            "startup_controls_object_id": self.startup_controls_object_id,
            "startup_override_active": bool(self.startup_override_active),
            "startup_requested_internal_action": list(self.startup_requested_internal_action),
            "startup_requested_pedal_fraction": float(self.startup_requested_pedal_fraction),
            "startup_readback_pedal_fraction": float(self.startup_readback_pedal_fraction),
            "startup_control_write_count": int(self.startup_control_write_count),
            "startup_neutral_overwrite_detected": bool(self.startup_neutral_overwrite_detected),
            "startup_last_control_writer": self.startup_last_control_writer,
            "startup_final_neutral_readback": dict(self.startup_final_neutral_readback),
            "startup_override_active_final": bool(self.startup_override_active_final),
            "reset_controls_object_id": self.reset_controls_object_id,
            "reset_vjoy_device_status_before": self.reset_vjoy_device_status_before,
            "reset_vjoy_reacquire_attempted": bool(self.reset_vjoy_reacquire_attempted),
            "reset_vjoy_reacquire_succeeded": bool(self.reset_vjoy_reacquire_succeeded),
            "reset_update_vjd_attempts": int(self.reset_update_vjd_attempts),
            "reset_neutralization_verified": bool(self.reset_neutralization_verified),
            "reset_gear_before_raw": self.reset_gear_before_raw,
            "reset_gear_after_raw": self.reset_gear_after_raw,
            "reset_last_error": self.reset_last_error,
            "ts": time.time(),
            "update_vjd_success_count": int(self.update_vjd_success_count),
            "update_vjd_fail_count": int(self.update_vjd_fail_count),
            "policy_control_write_count": int(self.policy_control_write_count),
        }
        if self.policy is not None:
            snap.update(self.policy.policy_status())
        else:
            snap["policy_ready"] = False
            snap["policy_load_error"] = self.last_error if "policy" in self.last_error else ""
        if self.vjoy_startup_report:
            snap["vjoy_startup"] = dict(self.vjoy_startup_report)
        if self.control_smoke_test_report:
            snap["control_smoke_test"] = dict(self.control_smoke_test_report)
            snap["control_smoke_test_result"] = str(
                self.control_smoke_test_report.get("result")
                or self.control_smoke_test_result
                or "failed"
            )
        return snap

    def _diag_health_fields(self) -> dict[str, Any]:
        diag_srv = self._diag_server
        if diag_srv is not None:
            terr = diag_srv.thread_error()
            thread_alive = bool(diag_srv.thread_is_alive())
            listener_ready = bool(diag_srv.listener_ready() and thread_alive)
            out = {
                "diag_listener_ready": listener_ready,
                "diag_thread_alive": thread_alive,
                "diag_thread_error": str(terr) if terr else "",
                "diag_bind_error": str(self._diag_bind_error or ""),
            }
            out.update(diag_srv.runtime_health())
            if listener_ready and not thread_alive:
                out["diag_thread_error"] = out.get("diag_thread_error") or "diag thread died after listen()"
            return out
        return {
            "diag_listener_ready": False,
            "diag_thread_alive": False,
            "diag_thread_error": "",
            "diag_bind_error": str(self._diag_bind_error or ""),
            "diag_accept_loop_alive": False,
            "diag_last_accept_at": 0.0,
            "diag_last_error": "",
            "diag_socket_closed": False,
            "diag_last_successful_response_at": 0.0,
            "diag_last_request_error": "",
            "diag_last_request_traceback": "",
        }

    def _strip_for_diag_handoff(self, snap: dict[str, Any]) -> dict[str, Any]:
        slim = {k: v for k, v in snap.items() if k not in _DIAG_HANDOFF_STRIP}
        telem = snap.get("telemetry_snapshot") or {}
        if telem:
            slim["telemetry_summary"] = {
                k: telem.get(k) for k in ("steer", "gas", "brake", "speed", "gear")
            }
        slim.update(self._diag_health_fields())
        return slim

    def _refresh_diag_handoff(self, snap: dict[str, Any]) -> None:
        handoff = self._strip_for_diag_handoff(snap)
        for key, value in self._probe_cache.items():
            handoff.setdefault(key, value)
        handoff["vjoy_ready"] = bool(self.vjoy_device_acquired)
        handoff["phase"] = self.phase
        handoff["heartbeat_monotonic"] = self.heartbeat_monotonic
        handoff["heartbeat_age_ms"] = (time.time() - self.heartbeat_ts) * 1000.0
        handoff["telemetry_age_ms"] = self.telemetry_age_ms()
        handoff["last_error"] = self.last_error
        handoff["observation_complete"] = self.observation_complete()
        handoff["transition_count"] = self.transition_count
        with self._diag_handoff_lock:
            self._diag_handoff = handoff

    def _emergency_diag_snapshot(self) -> dict[str, Any]:
        snap = {
            "worker_id": self.worker_id,
            "session_id": self.session_id,
            "collector_pid": self.collector_pid,
            "boot_id": self.boot_id,
            "phase": self.phase,
            "diag_port": self.ports.diag,
            "vjoy_device": self.vjoy_device,
            "vjoy_device_acquired": bool(self.vjoy_device_acquired),
            "initial_session_attached": bool(self.initial_session_attached),
            "policy_ready": bool((self._cached_snapshot or {}).get("policy_ready", False)),
            "heartbeat_age_ms": (time.time() - self.heartbeat_ts) * 1000.0,
            "telemetry_age_ms": self.telemetry_age_ms(),
            "last_error": self.last_error,
        }
        snap.update(self._diag_health_fields())
        return snap

    def write_minimal_status(self) -> None:
        with self.lock:
            snap = self.minimal_snapshot()
            snap.update(self._diag_health_fields())
            self._cached_snapshot = snap
            self._refresh_diag_handoff(snap)
        safe_write_status_json(_status_path(self.worker_id), snap, _STATUS_IO_LOCK)

    def heartbeat_publish(self) -> None:
        """Cheap liveness write from the heartbeat thread (no network probes).

        The dashboard judges collector liveness by status_written_at age. That
        field is otherwise only refreshed by publish_status() in the main loop,
        which blocks behind env.step() and network probes; under CPU saturation
        the main loop can stall >MAX_HEARTBEAT_MS and the collector is falsely
        declared heartbeat_stale though the process is alive. Writing a fresh
        minimal snapshot here (in-memory only) keeps status_written_at current
        independently of the main loop. Richer network-probed fields from the
        last publish_status() are preserved by merging over the cached snapshot.
        """
        with self.lock:
            fresh = self.minimal_snapshot()
            if self._cached_snapshot:
                merged = dict(self._cached_snapshot)
                merged.update(fresh)
            else:
                merged = fresh
            merged.update(self._diag_health_fields())
            self._cached_snapshot = merged
            self._refresh_diag_handoff(merged)
        safe_write_status_json(_status_path(self.worker_id), merged, _STATUS_IO_LOCK)

    def get_diag_snapshot(self) -> dict:
        """Lock-free handoff for the diagnostic port; never blocks on runtime.lock."""
        with self._diag_handoff_lock:
            if self._diag_handoff:
                return dict(self._diag_handoff)
        return self._emergency_diag_snapshot()

    def refresh_snapshot(self) -> dict:
        """Refresh cached status (blocking I/O); not called from diag handler."""
        from ac_workers.worker_handshake import tcp_open, vjoy_accessible

        learner_connected = False
        transition_server_ok = False
        management_connected = False
        vjoy_ready = bool(self.vjoy_device_acquired)
        now = time.monotonic()
        if now - self._probe_cache_ts < 2.0 and self._probe_cache:
            learner_connected = bool(self._probe_cache.get("learner_connected"))
            transition_server_ok = bool(self._probe_cache.get("transition_server_ok"))
            management_connected = bool(self._probe_cache.get("management_connected"))
            if self._probe_cache.get("vjoy_ready") is not None:
                vjoy_ready = bool(self._probe_cache.get("vjoy_ready"))
        else:
            try:
                stats = self.transition_client().stats()
                learner_connected = bool(stats.get("learner_connected"))
                transition_server_ok = True
            except OSError:
                pass
            management_connected = tcp_open("127.0.0.1", self.mgmt_port)
            if not vjoy_ready:
                vjoy_ready = vjoy_accessible(self.vjoy_device)
            self._probe_cache = {
                "learner_connected": learner_connected,
                "transition_server_ok": transition_server_ok,
                "management_connected": management_connected,
                "vjoy_ready": vjoy_ready,
            }
            self._probe_cache_ts = now
        snap = self.minimal_snapshot()
        snap.update(
            {
                "management_connected": management_connected,
                "learner_connected": learner_connected,
                "transition_server_ok": transition_server_ok,
                "transition_server_connected": transition_server_ok,
                "vjoy_ready": vjoy_ready,
            }
        )
        with self.lock:
            self._cached_snapshot = snap
            self._refresh_diag_handoff(snap)
        return snap

    def get_snapshot(self) -> dict:
        with self.lock:
            if self._cached_snapshot:
                snap = dict(self._cached_snapshot)
                snap["phase"] = self.phase
                snap["heartbeat_monotonic"] = self.heartbeat_monotonic
                snap["heartbeat_age_ms"] = (time.time() - self.heartbeat_ts) * 1000.0
                snap["telemetry_age_ms"] = self.telemetry_age_ms()
                snap["last_error"] = self.last_error
                snap["observation_complete"] = self.observation_complete()
                snap["transition_count"] = self.transition_count
                return snap
        return self.refresh_snapshot()

    def build_status(self) -> dict:
        return self.get_snapshot()

    def publish_status(self) -> None:
        snap = self.refresh_snapshot()
        safe_write_status_json(_status_path(self.worker_id), snap, _STATUS_IO_LOCK)


def _build_config(worker_id: int):
    from omegaconf import OmegaConf

    from ac_workers.collector_startup_log import normalize_loopback_host
    from ac_workers.worker_ports import worker_ports

    config = OmegaConf.load(str(REPO / "config_first_sac_cpu_v3.yml"))
    wcfg = load_worker_config(worker_id)
    ports = worker_ports(worker_id)
    config.work_dir = str(REPO / "outputs" / ("collector_worker_{0}".format(worker_id)))
    ac = config.AssettoCorsa
    ac.remote_machine_ip = normalize_loopback_host(wcfg.remote_machine_ip)
    ac.ego_server_port = int(ports.ego)
    ac.opponents_server_port = int(ports.opponents)
    ac.simulation_management_server_port = int(ports.mgmt)
    return config


def _complete_episode_reset(env: Any, runtime: Any, worker_id: int) -> tuple[bool, Any]:
    """Reset episode with vJoy re-validation; retry before declaring reset_failed."""
    from ac_workers.collector_live_attach import neutralize_env_controls, run_episode_reset

    last_err = ""
    state = None
    for attempt in range(MAX_EPISODE_RESET_ATTEMPTS):
        neutralize_env_controls(env)
        reason = runtime.last_reset_reason or "episode_boundary"
        logger.info("reset_begin worker=%s reason=%s attempt=%s", worker_id, reason, attempt + 1)
        env.state["reset_reason"] = reason
        try:
            state = env.reset()
        except OSError as exc:
            last_err = "reset_connection_failed: {0}".format(exc)
            runtime.reset_fail_streak += 1
            runtime.set_phase("reset_waiting_for_session", last_err)
            neutralize_env_controls(env)
            if attempt + 1 < MAX_EPISODE_RESET_ATTEMPTS:
                time.sleep(0.35 * (attempt + 1))
            continue
        if getattr(env, "_pending_shift_fail", False):
            last_err = runtime.last_error or "N->1ere impossible"
            logger.warning(
                "reset gear shift fail worker=%s attempt=%s/%s: %s (retry)",
                worker_id,
                attempt + 1,
                MAX_EPISODE_RESET_ATTEMPTS,
                last_err,
            )
            if attempt + 1 < MAX_EPISODE_RESET_ATTEMPTS:
                time.sleep(0.35 * (attempt + 1))
                continue
            runtime.set_phase("gear_initialization_failed", last_err)
            return False, None
        if runtime.phase == "gear_mapping_invalid":
            runtime.set_phase(
                "gear_initialization_failed",
                runtime.last_error or "N->1ere impossible",
            )
            return False, None
        try:
            ok_reset, reset_err, reset_state = run_episode_reset(env, runtime, worker_id)
        except OSError as exc:
            ok_reset, reset_err, reset_state = False, "reset_connection_failed: {0}".format(exc), None
            neutralize_env_controls(env)
        if ok_reset:
            runtime.reset_fail_streak = 0
            runtime.post_reset_grace_steps = POST_RESET_GRACE_STEPS
            if reset_state is not None:
                state = reset_state
            return True, state
        last_err = reset_err or "episode reset failed"
        runtime.reset_fail_streak += 1
        if attempt + 1 < MAX_EPISODE_RESET_ATTEMPTS:
            time.sleep(0.35 * (attempt + 1))
    runtime.set_phase("reset_failed", last_err)
    runtime.last_reset_reason = last_err
    neutralize_env_controls(env)
    return False, None


def _read_car_state(runtime: Any) -> dict[str, Any]:
    """Read the car telemetry this worker observes, best-effort.

    Includes steer/gas/brake (existing callers) PLUS speed + world position +
    gear, so the calibration test can verify drive-forward / brake / steer.

    Refreshes the live state first: in the idle phase the env's step loop is
    paused, so ``client.state`` would otherwise stay at its attach-time (all
    zero) values and every diagnostic pulse would read a phantom stationary car.
    """
    client = None
    env = None
    refresh_info = "n/a"
    try:
        env = runtime.env
        if env is not None:
            client = getattr(env, "client", None)
    except Exception as exc:
        client = None
        refresh_info = "env_get_exc:{0}".format(type(exc).__name__)
    if client is not None and hasattr(client, "get_servers_input"):
        try:
            client.get_servers_input(deadline=time.monotonic() + 0.5)
            refresh_info = "refreshed"
        except Exception as exc:
            refresh_info = "refresh_exc:{0}".format(type(exc).__name__)
    dbg = {
        "env_ok": env is not None,
        "client_ok": client is not None,
        "refresh": refresh_info,
        "state_keys": None,
        "raw_speed": None,
        "raw_posx": None,
        "raw_steer": None,
    }
    try:
        if client is not None:
            state = client.state
            dbg["state_keys"] = sorted(state.keys()) if isinstance(state, dict) else type(state).__name__
            dbg["raw_speed"] = state.get("speed") if isinstance(state, dict) else None
            dbg["raw_posx"] = state.get("world_position_x") if isinstance(state, dict) else None
            dbg["raw_steer"] = state.get("steerAngle") if isinstance(state, dict) else None
            out = {
                "steer": float(state.get("steerAngle", state.get("steer", 0.0)) or 0.0),
                "gas": float(state.get("accStatus", state.get("gas", 0.0)) or 0.0),
                "brake": float(state.get("brakeStatus", state.get("brake", 0.0)) or 0.0),
                "speed": float(state.get("speed", 0.0) or 0.0),
                "pos_x": float(state.get("world_position_x", 0.0) or 0.0),
                "pos_y": float(state.get("world_position_y", 0.0) or 0.0),
                "pos_z": float(state.get("world_position_z", 0.0) or 0.0),
                "gear": float(state.get("actualGear", 0.0) or 0.0),
            }
            out["_dbg"] = dbg
            return out
    except Exception as exc:
        dbg["read_exc"] = type(exc).__name__
    snap = dict(getattr(runtime, "telemetry_snapshot", {}) or {})
    dbg["snap_keys"] = sorted(snap.keys())
    out = {
        "steer": float(snap.get("steer", 0.0) or 0.0),
        "gas": float(snap.get("gas", 0.0) or 0.0),
        "brake": float(snap.get("brake", 0.0) or 0.0),
        "speed": float(snap.get("speed", 0.0) or 0.0),
        "pos_x": float(snap.get("pos_x", 0.0) or 0.0),
        "pos_y": float(snap.get("pos_y", 0.0) or 0.0),
        "pos_z": float(snap.get("pos_z", 0.0) or 0.0),
        "gear": float(snap.get("gear", 0.0) or 0.0),
    }
    out["_dbg"] = dbg
    return out


def perform_vjoy_pulse(
    runtime: Any,
    local_controls: Any,
    device_id: int,
    *,
    steer: float,
    throttle: float,
    brake: float,
    hold_sec: float,
    neutralize: Any,
) -> dict[str, Any]:
    """Authoritative diagnostic pulse using the EXACT normal-driving vJoy writer.

    The ONLY function that writes vJoy here is ``apply_canonical_controls`` — the
    same writer the main driving loop calls every tick (worker_collector.run_collector
    -> apply_canonical_controls). No policy, no learner, no replay write. On any
    failure we attach FULL diagnostic context (traceback, worker_id, collector_pid,
    vjoy_device_id, vjoy handle status, the exact action and writer used) so a
    PULSE_WRITE_FAILED is never a black box.

    Returns write_ok (did UpdateVJD succeed), car_moved / car_motion (did the
    observed car react), and the failure context. Always neutralizes at the end.
    """
    from ac_workers.canonical_controls import apply_canonical_controls

    resp: dict[str, Any] = {
        "writer": "apply_canonical_controls",
        "write_ok": False,
        "car_moved": None,
        "car_motion": 0.0,
        "action_sent": {"steer": float(steer), "throttle": float(throttle), "brake": float(brake)},
        "vjoy_device_id": int(device_id),
        "error": "",
        "trace": "",
    }
    before = _read_car_state(runtime)
    try:
        result = apply_canonical_controls(
            local_controls, steer, throttle, brake, device_id=device_id
        )
        resp["write_ok"] = bool(result.get("ok") and result.get("update_vjd_result"))
        resp["vjoy_device_id"] = int(result.get("vjoy_device_id") or device_id)
        resp["canonical_called"] = bool(result.get("canonical_controls_called"))
        resp["update_vjd_result"] = bool(result.get("update_vjd_result"))
        resp["device_status_before_update"] = result.get("device_status_before_update")
        if resp["write_ok"]:
            runtime.last_vjoy_write_device = resp["vjoy_device_id"]
        else:
            resp["error"] = str(result.get("error") or "UpdateVJD failed")
            resp["canonical_result"] = {
                k: result.get(k)
                for k in (
                    "ok",
                    "update_vjd_result",
                    "canonical_controls_called",
                    "physical_action",
                    "steer_internal",
                    "acc_internal",
                    "brake_internal",
                    "vjoy_device_id",
                    "vjoy_object_id",
                )
            }
        hold = max(0.05, min(8.0, float(hold_sec)))
        # Re-assert the pulse controls throughout the hold. The collector's
        # driving loop keeps writing the policy's action every tick, so a single
        # write gets overwritten before the "after" read (and in the idle phase
        # the game ignores a lone write). Re-writing every ~40 ms keeps the pulse
        # action as the most recent write so the car actually reacts to it.
        end = time.monotonic() + hold
        while time.monotonic() < end:
            try:
                re_result = apply_canonical_controls(
                    local_controls, steer, throttle, brake, device_id=device_id
                )
                if re_result.get("ok") and re_result.get("update_vjd_result"):
                    resp["write_ok"] = True
                    resp["update_vjd_result"] = True
            except Exception:
                pass
            time.sleep(0.02)
        after = _read_car_state(runtime)
        motion = max(abs(after[k] - before[k]) for k in ("steer", "gas", "brake"))
        resp["car_motion"] = float(motion)
        fresh = runtime.telemetry_age_ms() < 2000.0
        resp["telemetry_fresh"] = fresh
        if fresh:
            resp["car_moved"] = bool(motion > 0.02)
        resp["telemetry_before"] = before
        resp["telemetry_after"] = after
    except Exception as pulse_exc:
        resp["error"] = "{0}: {1}".format(type(pulse_exc).__name__, pulse_exc)
        resp["trace"] = traceback.format_exc()
        try:
            from ac_workers.vjoy_session import get_vjd_status

            resp["vjoy_handle_status"] = get_vjd_status(int(device_id))
        except Exception:
            resp["vjoy_handle_status"] = None
        logger.exception(
            "diagnostic_vjoy_pulse failed worker=%s pid=%s device=%s action=(%.3f,%.3f,%.3f)",
            getattr(runtime, "worker_id", "?"),
            getattr(runtime, "collector_pid", "?"),
            device_id, steer, throttle, brake,
        )
    finally:
        try:
            neutralize()
        except Exception:
            logger.exception("pulse neutralize failed worker=%s", getattr(runtime, "worker_id", "?"))
    runtime.last_vjoy_pulse = dict(resp)
    return resp


# Phases where the collector has bound env + canonical vJoy and can honestly write.
BINDING_READY_PHASES = frozenset({"idle", "running", "waiting_for_compatible_policy"})


def perform_vjoy_selftest(
    runtime: Any,
    local_controls: Any,
    device_id: int,
    *,
    neutralize: Any,
) -> dict[str, Any]:
    """Minimal vJoy write self-test: neutral -> micro-action -> neutral.

    Uses the same ``apply_canonical_controls`` / ``neutralize_canonical`` path as
    normal driving. No telemetry, no learner, no car-motion judgement — only
    proves the collector can UpdateVJD on its device right now.
    """
    from ac_workers.canonical_controls import apply_canonical_controls, neutralize_canonical

    resp: dict[str, Any] = {
        "cmd": "diag_selftest_vjoy_write",
        "writer": "apply_canonical_controls",
        "write_ok": False,
        "vjoy_device_id": int(device_id),
        "steps": [],
        "error": "",
        "trace": "",
    }
    try:
        from ac_workers.vjoy_session import get_vjd_status

        resp["vjoy_handle_status"] = get_vjd_status(int(device_id))
        resp["local_controls_id"] = id(local_controls)
        vj = getattr(local_controls, "vj", None)
        resp["vjoy_object_id"] = id(vj) if vj is not None else None
        resp["vjoy_acquired_flag"] = bool(getattr(vj, "acquired", False)) if vj else False

        r_neutral = neutralize_canonical(local_controls, device_id=device_id)
        resp["steps"].append(
            {
                "step": "neutral_before",
                "ok": bool(r_neutral.get("ok") and r_neutral.get("update_vjd_result")),
                "error": r_neutral.get("error", ""),
            }
        )
        if not (r_neutral.get("ok") and r_neutral.get("update_vjd_result")):
            resp["error"] = str(r_neutral.get("error") or "neutral_before failed")
            return resp

        # Tiny valid physical action (same units as the driving loop).
        r_pulse = apply_canonical_controls(
            local_controls, steer=0.05, throttle=0.0, brake=0.0, device_id=device_id
        )
        resp["steps"].append(
            {
                "step": "micro_action",
                "ok": bool(r_pulse.get("ok") and r_pulse.get("update_vjd_result")),
                "error": r_pulse.get("error", ""),
                "physical_action": r_pulse.get("physical_action"),
            }
        )
        if not (r_pulse.get("ok") and r_pulse.get("update_vjd_result")):
            resp["error"] = str(r_pulse.get("error") or "micro_action failed")
            return resp

        r_after = neutralize_canonical(local_controls, device_id=device_id)
        resp["steps"].append(
            {
                "step": "neutral_after",
                "ok": bool(r_after.get("ok") and r_after.get("update_vjd_result")),
                "error": r_after.get("error", ""),
            }
        )
        resp["write_ok"] = bool(r_after.get("ok") and r_after.get("update_vjd_result"))
        if not resp["write_ok"]:
            resp["error"] = str(r_after.get("error") or "neutral_after failed")
    except Exception as selftest_exc:
        resp["error"] = "{0}: {1}".format(type(selftest_exc).__name__, selftest_exc)
        resp["trace"] = traceback.format_exc()
        logger.exception(
            "diag_selftest_vjoy_write failed worker=%s pid=%s device=%s",
            getattr(runtime, "worker_id", "?"),
            getattr(runtime, "collector_pid", "?"),
            device_id,
        )
    finally:
        try:
            neutralize()
        except Exception:
            logger.exception("selftest neutralize failed worker=%s", getattr(runtime, "worker_id", "?"))
    runtime.last_vjoy_selftest = dict(resp)
    return resp


def run_collector(worker_id: int, dry_run: bool = False) -> int:
    ports = worker_ports(worker_id)
    runtime = CollectorRuntime(worker_id, ports.vjoy, ports.mgmt)
    cfg = None
    prev = None
    log_path = REPO / "ac_workers" / "logs" / ("worker_{0}.log".format(worker_id))
    watchdog = None
    diag: CollectorDiagServer | None = None
    env = None
    episode_id = 0
    step_id = 0
    heartbeat_stop = threading.Event()
    heartbeat_thread: threading.Thread | None = None
    canonical_local_controls = None
    prehold = None
    shutdown_event = threading.Event()
    # A previous run may have left a stale sentinel; start clean so we don't exit early.
    clear_collector_shutdown(worker_id)
    clear_collector_resume(worker_id)

    if dry_run:
        cfg = load_worker_config(worker_id)
        prev = apply_worker_env(cfg)
        from common.transition_server import TransitionClient

        client = TransitionClient()
        ok = client.push_transition(
            {
                "worker_id": worker_id,
                "episode_id": 0,
                "step_id": 0,
                "obs": [0.0],
                "action": [0.0, 0.0, 0.0],
                "reward": 0.0,
                "next_state": [0.0],
                "done": False,
            }
        )
        client.heartbeat(worker_id)
        print("DRY_RUN collector worker {0} push={1}".format(worker_id, ok))
        restore_worker_env(prev)
        return 0 if ok else 1

    def _on_phase(phase: str, error: str = "") -> None:
        runtime.set_phase(phase, error)
        runtime.write_minimal_status()

    def _on_diag_fatal(exc: Exception) -> None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        _startup_log("collector startup: diagnostic thread failed\n" + tb)
        runtime._diag_bind_error = str(exc)
        runtime.set_phase("diagnostic_failed", str(exc))
        runtime.write_minimal_status()

    def _on_diag_ping() -> dict[str, Any]:
        """Lock-free diag liveness; no telemetry, vJoy, or runtime.lock."""
        return {
            "ok": True,
            "cmd": "ping",
            "worker_id": runtime.worker_id,
            "collector_pid": runtime.collector_pid,
            "boot_id": runtime.boot_id,
            "diag_port": ports.diag,
            "ts": time.time(),
        }

    def _diag_identity() -> dict[str, Any]:
        return {
            "worker_id": runtime.worker_id,
            "collector_pid": runtime.collector_pid,
            "boot_id": runtime.boot_id,
        }

    def _on_diag_request_outcome(cmd: str, ok: bool, err: str, tb: str) -> None:
        """Called from diag thread after each response — must NEVER take runtime.lock."""
        runtime.touch_heartbeat()

    # 1) minimal status — no AssettoCorsaEnv, no heavy imports
    def _on_safe_action_test() -> dict:
        from ac_workers.worker_handshake import tcp_open, vjoy_accessible

        import numpy as np

        cmd = "safe_action_test"
        if runtime.env is None or runtime.phase != "running":
            return {"ok": False, "cmd": cmd, "error": "telemetry not ready"}
        if not runtime.observation_complete():
            return {"ok": False, "cmd": cmd, "error": "telemetry not ready"}
        if runtime.telemetry_age_ms() > 2000.0:
            return {"ok": False, "cmd": cmd, "error": "telemetry not ready"}
        if not tcp_open("127.0.0.1", cfg.simulation_management_server_port):
            return {"ok": False, "cmd": cmd, "error": "telemetry not ready"}
        if not vjoy_accessible(cfg.vjoy_device_id):
            return {"ok": False, "cmd": cmd, "error": "telemetry not ready"}
        if not hasattr(runtime.env, "current_actions"):
            return {"ok": False, "cmd": cmd, "error": "telemetry not ready"}
        try:
            base = dict(runtime.telemetry_snapshot)
            action = np.array([0.04, -0.80, -1.0], dtype=np.float32)
            runtime.last_requested_action = [0.04, -0.80, -1.0]
            runtime.env.set_actions(action)
            runtime.last_executed_action = [
                float(x) for x in runtime.env.get_executed_action_for_buffer().ravel()[:3]
            ]
            time.sleep(0.30)
            next_state, reward, done, info = runtime.env.step(action=None)
            runtime.last_telemetry_ts = time.time()
            runtime.last_observation = next_state.ravel().tolist()
            after = {
                "steer": float(runtime.env.client.state.get("steer", 0.0)),
                "gas": float(runtime.env.client.state.get("gas", 0.0)),
                "brake": float(runtime.env.client.state.get("brake", 0.0)),
                "speed": float(info.get("speed", 0.0)) if isinstance(info, dict) else 0.0,
            }
            runtime.telemetry_snapshot = after
            _neutral_vjoy()
            runtime.env.client.controls.set_defaults()
            runtime.env.client.controls.apply_local_controls()
            changed = any(
                abs(after.get(k, 0.0) - float(base.get(k, 0.0))) > 0.01 for k in ("steer", "gas", "brake")
            )
            return {
                "ok": changed,
                "cmd": cmd,
                "base": base,
                "after": after,
                "requested": runtime.last_requested_action,
                "executed": runtime.last_executed_action,
                "telemetry_changed": changed,
            }
        except Exception as exc:
            logger.exception("safe_action_test failed worker=%s", worker_id)
            return {"ok": False, "cmd": cmd, "error": str(exc)}

    def _resolve_local_controls() -> Any:
        lc = canonical_local_controls
        if lc is None and runtime.env is not None:
            try:
                from ac_workers.canonical_controls import get_local_controls

                lc = get_local_controls(runtime.env)
            except Exception:
                lc = None
        return lc

    def _diag_not_ready(cmd: str, reason: str) -> dict[str, Any]:
        return {
            "ok": False,
            "cmd": cmd,
            "write_ok": False,
            "worker_id": runtime.worker_id,
            "collector_pid": runtime.collector_pid,
            "boot_id": runtime.boot_id,
            "vjoy_device_id": int(cfg.vjoy_device_id),
            "vjoy_device_acquired": bool(runtime.vjoy_device_acquired),
            "phase": runtime.phase,
            "env_ready": runtime.env is not None,
            "error": reason,
        }

    def _on_vjoy_pulse(payload: dict | None = None) -> dict:
        """Authoritative vJoy pulse: the collector writes its OWN vJoy via the EXACT
        normal-driving writer (perform_vjoy_pulse -> apply_canonical_controls).

        Bypasses policy / learner / replay. Refuses early-startup writes so Errno 22
        from an unbound device is reported as ENV_NOT_READY, not a binding failure.
        """
        payload = payload or {}
        steer = float(payload.get("steer", 0.6))
        throttle = float(payload.get("throttle", 0.0))
        brake = float(payload.get("brake", 1.0))
        hold_sec = float(payload.get("hold_sec", 0.4))
        cmd = "diagnostic_vjoy_pulse"

        identity: dict[str, Any] = {
            "ok": False,
            "cmd": cmd,
            "worker_id": runtime.worker_id,
            "collector_pid": runtime.collector_pid,
            "boot_id": runtime.boot_id,
            "vjoy_device_id": int(cfg.vjoy_device_id),
            "last_vjoy_write_device": runtime.last_vjoy_write_device,
            "vjoy_device_acquired": bool(runtime.vjoy_device_acquired),
            "phase": runtime.phase,
            "env_ready": runtime.env is not None,
        }

        if runtime.phase not in BINDING_READY_PHASES:
            identity.update(
                _diag_not_ready(
                    cmd,
                    "ENV_NOT_READY: phase={0!r} (wait until idle/running) — binding NOT tested".format(
                        runtime.phase
                    ),
                )
            )
            identity["car_moved"] = None
            runtime.last_vjoy_pulse = dict(identity)
            return identity
        if not runtime.vjoy_device_acquired:
            identity.update(
                _diag_not_ready(
                    cmd,
                    "ENV_NOT_READY: vJoy device not acquired yet — binding NOT tested",
                )
            )
            identity["car_moved"] = None
            runtime.last_vjoy_pulse = dict(identity)
            return identity

        local_controls = _resolve_local_controls()
        if local_controls is None:
            identity.update(
                _diag_not_ready(
                    cmd,
                    "ENV_NOT_READY: canonical local_controls not bound "
                    "(phase={0}, env_ready={1}) — binding NOT tested".format(
                        runtime.phase, runtime.env is not None
                    ),
                )
            )
            identity["car_moved"] = None
            runtime.last_vjoy_pulse = dict(identity)
            return identity

        resp = perform_vjoy_pulse(
            runtime,
            local_controls,
            cfg.vjoy_device_id,
            steer=steer,
            throttle=throttle,
            brake=brake,
            hold_sec=hold_sec,
            neutralize=_neutral_vjoy,
        )
        merged = dict(identity)
        merged.update(resp)
        merged["last_vjoy_write_device"] = runtime.last_vjoy_write_device
        merged["ok"] = bool(resp.get("write_ok"))
        runtime.last_vjoy_pulse = dict(merged)
        return merged

    def _on_vjoy_selftest() -> dict:
        """diag_selftest_vjoy_write: prove this collector can UpdateVJD right now."""
        cmd = "diag_selftest_vjoy_write"
        if runtime.phase not in BINDING_READY_PHASES:
            return _diag_not_ready(
                cmd,
                "ENV_NOT_READY: phase={0!r} — wait until idle/running".format(runtime.phase),
            )
        if not runtime.vjoy_device_acquired:
            return _diag_not_ready(cmd, "ENV_NOT_READY: vJoy device not acquired yet")
        local_controls = _resolve_local_controls()
        if local_controls is None:
            return _diag_not_ready(cmd, "ENV_NOT_READY: canonical local_controls not bound")
        resp = perform_vjoy_selftest(
            runtime, local_controls, cfg.vjoy_device_id, neutralize=_neutral_vjoy
        )
        merged = {
            "worker_id": runtime.worker_id,
            "collector_pid": runtime.collector_pid,
            "boot_id": runtime.boot_id,
            "phase": runtime.phase,
            "vjoy_device_acquired": bool(runtime.vjoy_device_acquired),
        }
        merged.update(resp)
        merged["ok"] = bool(resp.get("write_ok"))
        return merged

    def _neutral_vjoy() -> None:
        from ac_workers.canonical_controls import neutralize_canonical

        if canonical_local_controls is not None:
            result = neutralize_canonical(
                canonical_local_controls, device_id=cfg.vjoy_device_id
            )
            runtime.vjoy_neutralization_verified = bool(result.get("ok"))
            return
        from ac_workers.collector_live_attach import neutralize_env_controls

        if runtime.env is not None:
            ok, _reason = neutralize_env_controls(runtime.env, device_id=cfg.vjoy_device_id)
            runtime.vjoy_neutralization_verified = ok
            return
        from ac_workers.collector_safety import neutral_vjoy

        neutral_vjoy(cfg.vjoy_device_id)

    def _on_neutralize() -> dict:
        _neutral_vjoy()
        if runtime.env:
            runtime.env.client.controls.set_defaults()
            try:
                runtime.env.client.controls.apply_local_controls()
            except Exception:
                pass
        return {"ok": True, "neutralized": True}

    def _on_worker_control(cmd: str, candidate=None) -> dict:
        if cmd == "evaluate":
            if runtime.evaluation is not None or runtime._evaluation_requested:
                return {"ok": False, "error": "evaluation already active"}
            if runtime.policy is None or not runtime.policy.is_compatible():
                return {"ok": False, "error": "policy not ready"}
            runtime._evaluation_restore_paused = runtime._policy_paused
            runtime._evaluation_requested = True
            runtime._reset_requested = True
            runtime._reset_reason_pending = "evaluation_start"
            runtime._policy_paused = False
            runtime._started = True
            return {"ok": True, "evaluation_requested": True}
        if cmd == "pause":
            runtime._evaluation_restore_paused = True
            runtime._policy_paused = True
            return {"ok": True, "cmd": cmd, "policy_paused": True}
        if cmd == "resume":
            runtime._evaluation_restore_paused = False
            runtime._policy_paused = False
            runtime._started = True
            return {"ok": True, "cmd": cmd, "policy_paused": False, "started": True}
        if cmd == "reset":
            runtime._reset_requested = True
            runtime._reset_reason_pending = "control_center_reset"
            return {"ok": True, "cmd": cmd, "reset_requested": True}
        if cmd == "calibration_enter":
            # Real calibration mode: the test takes EXCLUSIVE vJoy control. The
            # main loop still steps the env (so the game responds) but skips the
            # policy's action; the loop stops writing NEUTRAL too. Entering
            # neutralizes first for a clean start.
            runtime._calibration_mode = True
            runtime._started = True
            try:
                neutralize_canonical(canonical_local_controls, device_id=cfg.vjoy_device_id)
            except Exception:
                pass
            return {"ok": True, "cmd": cmd, "calibration_mode": True}
        if cmd == "calibration_exit":
            # Safety: ALWAYS return every command to neutral when leaving
            # calibration (normal end, error, or emergency). Never leave an
            # active throttle/steer.
            runtime._calibration_mode = False
            try:
                neutralize_canonical(canonical_local_controls, device_id=cfg.vjoy_device_id)
            except Exception:
                pass
            return {"ok": True, "cmd": cmd, "calibration_mode": False}
        if cmd == "load_candidate":
            _evo = getattr(runtime, "_neuroevolution", None)
            if _evo is None:
                return {"ok": False, "error": "neuroevolution not active"}
            try:
                _idx = int(candidate)
                # PAS de torch ici : ce handler tourne dans le thread diagnostic,
                # et candidate_net (torch) peut y bloquer → timeout "diagnostic
                # timeout on port 2410X" pour les 6 workers (vu). On stocke l'index
                # et le main loop applique candidate_net (torch) avant le reset.
                runtime._pending_candidate_idx = _idx
                runtime._current_candidate = _idx
                runtime._last_candidate_score = float("-inf")
                runtime.episode_laps = 0
                runtime.episode_progress_m = 0.0
                runtime.episode_best_lap_time = 0.0
                # Libère le worker de l'idle et démarre un épisode frais.
                runtime._started = True
                runtime._reset_requested = True
                runtime._reset_reason_pending = "load_candidate"
                return {"ok": True, "cmd": cmd, "candidate": _idx}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
        return {"ok": False, "cmd": cmd, "error": "unknown control cmd"}

    def _start_heartbeat() -> None:
        nonlocal heartbeat_thread

        def _heartbeat_loop() -> None:
            while True:
                runtime.touch_heartbeat()
                # Persist liveness independently of the main loop so a stalled
                # env.step() under heavy load cannot cause a false heartbeat_stale.
                try:
                    runtime.heartbeat_publish()
                except OSError:
                    pass
                if collector_shutdown_requested(worker_id):
                    shutdown_event.set()
                if collector_resume_requested(worker_id):
                    runtime._policy_paused = False
                    runtime._started = True
                    clear_collector_resume(worker_id)
                if search_drive_active():
                    runtime._policy_paused = False
                    runtime._started = True
                if heartbeat_stop.wait(0.5):
                    break

        heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True, name="collector-hb")
        heartbeat_thread.start()

    def _idle_after_failure() -> int:
        _neutral_vjoy()
        while not shutdown_event.is_set():
            runtime.touch_heartbeat()
            try:
                runtime.write_minimal_status()
            except OSError as exc:
                logger.warning("idle status write failed worker=%s: %s", worker_id, exc)
            time.sleep(0.5)
        _startup_log("collector worker {0} graceful shutdown (idle-after-failure)".format(worker_id))
        return 0

    runtime.set_phase("starting")
    runtime.write_minimal_status()
    _startup_log("collector startup: initial status written")

    diag = CollectorDiagServer(
        worker_id,
        get_snapshot=runtime.get_diag_snapshot,
        on_safe_action_test=_on_safe_action_test,
        on_neutralize=_on_neutralize,
        on_worker_control=_on_worker_control,
        on_vjoy_pulse=_on_vjoy_pulse,
        on_vjoy_selftest=_on_vjoy_selftest,
        on_ping=_on_diag_ping,
        get_identity=_diag_identity,
        on_request_outcome=_on_diag_request_outcome,
        lock=runtime.lock,
        on_thread_fatal=_on_diag_fatal,
    )
    runtime._diag_server = diag
    diag.start()
    _startup_log("collector startup: diagnostic thread started port={0}".format(diag.port))

    if not diag.wait_until_listening(timeout_sec=5.0):
        err = diag.thread_error()
        if err is not None:
            msg = str(err)
        elif not diag.thread_is_alive():
            msg = "diagnostic thread died before listen()"
        else:
            msg = "diagnostic listener timeout"
        runtime._diag_bind_error = msg
        runtime.set_phase("diagnostic_failed", msg)
        runtime.write_minimal_status()
        _startup_log("collector startup: diagnostic listener failed: {0}".format(msg))
        if err is not None:
            _startup_log("".join(traceback.format_exception(type(err), err, err.__traceback__)))
        heartbeat_stop.set()
        return 1

    _startup_log("collector startup: diagnostic listener confirmed")
    runtime.set_phase("diag_ready")
    with runtime.lock:
        runtime._refresh_diag_handoff(runtime.minimal_snapshot())
    runtime.write_minimal_status()

    _start_heartbeat()
    _startup_log("collector startup: heartbeat started")

    cfg = load_worker_config(worker_id)
    prev = apply_worker_env(cfg)
    configure_startup_log(os.environ.get("AC_WORKER_STDERR_LOG"))
    runtime.vjoy_device = cfg.vjoy_device_id
    log_path = cfg.log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)

    prehold = None
    try:
        from ac_workers.vjoy_prehold import start_vjoy_prehold

        runtime.set_phase("vjoy_prehold_begin")
        runtime.write_minimal_status()
        prehold, prehold_err = start_vjoy_prehold(worker_id)
        if prehold is None:
            err = prehold_err or "vJoy prehold failed"
            runtime.set_phase("vjoy_startup_failed", err)
            runtime.write_minimal_status()
            _startup_log("collector startup: vjoy prehold failed: {0}".format(err))
            heartbeat_stop.set()
            return 1
        runtime.set_phase("vjoy_prehold_active")
        runtime.vjoy_device_acquired = True
        runtime.write_minimal_status()
        # Fail-fast strict-ownership guard: this worker must hold exactly its mapped
        # device and no OTHER live process may own it. Catches a "two workers on the
        # same device" race immediately instead of letting it degrade into a vJoy
        # cascade.
        from ac_workers.vjoy_pool import VJoyOwnershipError, assert_sole_owner

        try:
            assert_sole_owner(worker_id, prehold.device_id, self_pid=os.getpid())
        except VJoyOwnershipError as exc:
            runtime.set_phase("vjoy_startup_failed", str(exc))
            runtime.write_minimal_status()
            _startup_log("collector startup: ownership conflict: {0}".format(exc))
            try:
                prehold.stop()
            except Exception:
                pass
            heartbeat_stop.set()
            return 1
        # Crash-safe ownership record: lets the reaper tell a leaked device (this pid
        # gone) from a healthy one (this pid alive) without relying on the manifest.
        _record_vjoy_owner(
            prehold.device_id,
            pid=os.getpid(),
            session_id=runtime.session_id,
            worker_id=worker_id,
        )
        _startup_log(
            "collector startup: vjoy prehold device {0} acquired".format(prehold.device_id)
        )
    except Exception as exc:
        err = "vJoy prehold exception: {0}".format(exc)
        runtime.set_phase("vjoy_startup_failed", err)
        runtime.write_minimal_status()
        _startup_log("collector startup: {0}".format(err))
        heartbeat_stop.set()
        return 1

    try:
        with log_path.open("a", encoding="utf-8") as log:
            log.write(
                "collector start worker={0} diag={1} ego={2} mgmt={3} vjoy={4}\n".format(
                    worker_id, ports.diag, ports.ego, ports.mgmt, ports.vjoy
                )
            )

        _startup_log("collector startup: creating environment")
        runtime.set_phase("environment_construct_begin")
        runtime.write_minimal_status()

        import AssettoCorsaEnv.assettoCorsa as assettoCorsa
        from AssettoCorsaEnv.gear_shift import resolve_shift_up_mapping

        runtime.gear_mapping = resolve_shift_up_mapping(worker_id)
        config = _build_config(worker_id)
        env = assettoCorsa.make_ac_env(cfg=config, work_dir=config.work_dir)
        runtime.env = env
        from ac_workers.collector_policy import worker_policy_seed
        from omegaconf import OmegaConf

        _train_cfg = OmegaConf.load(str(REPO / "config_first_sac_cpu_v3.yml"))
        _policy_seed = worker_policy_seed(int(_train_cfg.seed), worker_id)
        env._exploration_rng = np.random.default_rng(_policy_seed + 17)
        if prehold is not None:
            prehold.stop_pulsing()
            env.client.controls.local_controls = prehold.controls
            _startup_log(
                "collector startup: injected preheld vJoy controls device {0}".format(
                    prehold.device_id
                )
            )
        env._collector_phase_hook = _on_phase
        env._gear_shift_mapping = runtime.gear_mapping
        runtime.set_phase("environment_construct_complete")
        runtime.write_minimal_status()

        from ac_workers.collector_live_attach import attach_live_session, neutralize_env_controls

        runtime.set_phase("attaching_live_session")
        runtime.write_minimal_status()
        _startup_log("collector startup: attaching to live session (no env.reset)")

        ok_attach, attach_err, state = attach_live_session(env, runtime, worker_id)
        if not ok_attach:
            runtime.write_minimal_status()
            print("collector worker {0} live attach failed: {1}".format(worker_id, attach_err))
            return _idle_after_failure()

        if state is None:
            runtime.set_phase("attaching_live_session_failed", "no observation after live attach")
            runtime.write_minimal_status()
            return _idle_after_failure()

        runtime.last_telemetry_ts = time.time()
        runtime.last_observation = state.ravel().tolist() if hasattr(state, "ravel") else list(state)
        runtime.telemetry_snapshot = {
            "steer": float(env.client.state.get("steer", 0.0)),
            "gas": float(env.client.state.get("gas", 0.0)),
            "brake": float(env.client.state.get("brake", 0.0)),
            "speed": float(env.client.state.get("speed", 0.0)),
            "spline": float(env.client.state.get("NormalizedSplinePosition", 0.0) or 0.0),
        }
        runtime._telemetry_ready = True
        runtime.touch_heartbeat()
        from ac_workers.canonical_controls import bind_canonical_controls

        canonical_local_controls = env.client.controls.local_controls
        bound = bind_canonical_controls(canonical_local_controls, cfg.vjoy_device_id)
        runtime.startup_local_controls_object_id = bound.get("local_controls_id")
        runtime.startup_vjoy_object_id = bound.get("vjoy_id")
        runtime.episode_started_at = time.monotonic()
        runtime.publish_status()
        print("collector worker {0} telemetry ready".format(worker_id))

        from ac_workers.control_smoke_test import run_control_smoke_test

        # run_control_smoke_test owns the one-shot request file; do not call
        # is_enabled() first or the request is consumed and the test returns {}.
        smoke_report = run_control_smoke_test(
            worker_id=worker_id,
            runtime=runtime,
            env=env,
            cfg=cfg,
            neutral_vjoy_fn=_neutral_vjoy,
            publish_status=runtime.publish_status,
        )
        if smoke_report.get("enabled"):
            report_dir = REPO / "outputs" / "diagnostics" / "single_car"
            report_dir.mkdir(parents=True, exist_ok=True)
            (report_dir / ("test_{0}.json".format(time.time_ns()))).write_text(
                json.dumps(smoke_report, indent=2, ensure_ascii=False), encoding="utf-8")
            cycle_request = REPO / "ac_workers/status/reset_cycles.request"
            if cycle_request.exists():
                fresh = time.time() - cycle_request.stat().st_mtime < 600
                cycle_request.unlink()
                if fresh:
                    cycle_results = []
                    cycle_path = report_dir / ("cycles_{0}.json".format(time.time_ns()))
                    for cycle in range(1, 6):
                        runtime.last_reset_reason = "diagnostic_cycle"
                        ok_reset, _ = _complete_episode_reset(env, runtime, worker_id)
                        result = {"cycle": cycle, "reset_ok": ok_reset, "reset_error": runtime.last_error if not ok_reset else ""}
                        if ok_reset:
                            result["drive"] = run_control_smoke_test(
                                worker_id=worker_id, runtime=runtime, env=env, cfg=cfg,
                                neutral_vjoy_fn=_neutral_vjoy, publish_status=runtime.publish_status, force=True)
                        cycle_results.append(result)
                        cycle_path.write_text(json.dumps(cycle_results, indent=2, ensure_ascii=False), encoding="utf-8")
                        _neutral_vjoy()
                        if not ok_reset or shutdown_event.is_set():
                            break
            runtime.set_phase("diagnostic_complete")
            runtime.control_smoke_test_report = dict(smoke_report)
            runtime.publish_status()
            print(
                "collector worker {0} smoke test result={1}".format(
                    worker_id, smoke_report.get("result")
                )
            )
            return _idle_after_failure()
        else:
            print("collector worker {0} running (smoke test disabled)".format(worker_id))

        from ac_workers.collector_safety import CollectorSafetyWatchdog
        from omegaconf import OmegaConf

        watchdog = CollectorSafetyWatchdog(worker_id, vjoy_device=cfg.vjoy_device_id)
        if bool(OmegaConf.select(_train_cfg, "Agent.neuroevolution", default=False)):
            from ac_workers.neuroevolution import NeuroEvolution, CandidatePolicy

            _evo_dir = REPO / "outputs" / "neuroevolution"
            _evo = NeuroEvolution(
                state_dim=int(env.observation_space.shape[0]),
                action_dim=int(env.action_space.shape[0]),
                population_size=int(OmegaConf.select(_train_cfg, "Agent.neuroevolution_population", default=120)),
                elitism=int(OmegaConf.select(_train_cfg, "Agent.neuroevolution_elitism", default=2)),
                parents=int(OmegaConf.select(_train_cfg, "Agent.neuroevolution_parents", default=10)),
                mutation_rate=float(OmegaConf.select(_train_cfg, "Agent.neuroevolution_mutation_rate", default=0.05)),
                mutation_intensity=float(OmegaConf.select(_train_cfg, "Agent.neuroevolution_mutation_intensity", default=0.03)),
                work_dir=str(_evo_dir),
            )
            if not _evo.load():
                _evo.init_generation(seed=0)
                _evo.save()
            runtime.policy = CandidatePolicy(_evo.candidate_net(worker_id), worker_id=worker_id)
            runtime._neuroevolution = _evo
            runtime._current_candidate = int(worker_id)
            runtime.last_error = ""
        else:
            from ac_workers.collector_policy import CollectorPolicy

            runtime.policy = CollectorPolicy(
                REPO / "config_first_sac_cpu_v3.yml",
                REPO / "outputs" / "multi_sac_cpu_v3",
                state_dim=env.observation_space.shape[0],
                worker_id=worker_id,
            )
            runtime.policy.reload_if_updated()
            if not runtime.policy.is_compatible():
                runtime.set_phase(
                    "waiting_for_compatible_policy",
                    runtime.policy.load_error() or "policy_state_dim_mismatch",
                )
                runtime._control_enabled = False
                runtime.publish_status()
                print("collector worker {0} waiting for compatible policy".format(worker_id))
            else:
                runtime.last_error = ""

        from ac_workers.worker_handshake import tcp_open

        if tcp_open("127.0.0.1", cfg.simulation_management_server_port):
            print("collector worker {0} management ready".format(worker_id))
        try:
            runtime.transition_client().stats()
            print("collector worker {0} transition server ready".format(worker_id))
        except OSError:
            logger.warning("transition server not reachable at collector start")

        from ac_workers.canonical_controls import (
            apply_canonical_controls,
            neutralize_canonical,
            raw_action_to_physical,
        )

        client = runtime.transition_client()

        # Idle until the learner starts this worker. Nothing drives on its own:
        # the car is held at neutral and telemetry is kept fresh so the worker
        # stays ready/resumable. The policy only runs after a "resume" command.
        runtime.set_phase("idle")
        runtime._control_enabled = False
        while not runtime._started and not shutdown_event.is_set():
            neutralize_canonical(canonical_local_controls, device_id=cfg.vjoy_device_id)
            poll_collector_telemetry(env, runtime)
            if time.monotonic() - runtime._probe_cache_ts >= 2.0:
                runtime.refresh_snapshot()
            runtime.write_minimal_status()
            runtime.touch_heartbeat()
            time.sleep(0.1)
        if shutdown_event.is_set():
            _startup_log("collector worker {0} graceful shutdown (idle)".format(worker_id))
            return 0
        runtime.set_phase("running")
        runtime.episode_started_at = time.monotonic()
        runtime.last_telemetry_ts = time.time()
        runtime.publish_status()

        while not shutdown_event.is_set():
            run_step = False
            pending_reset = False
            runtime.refresh_learner_state()
            with runtime.lock:
                if runtime._reset_requested and runtime.phase in ("running", "resetting", "waiting_for_compatible_policy"):
                    pending_reset = True
                    runtime._reset_requested = False
                    runtime.last_reset_reason = runtime._reset_reason_pending or "control_center_reset"
                    runtime._reset_reason_pending = ""
                    runtime.reset_count += 1
                    runtime.set_phase("resetting")
                    runtime.publish_status()
                elif runtime.phase == "waiting_for_compatible_policy":
                    runtime.policy.reload_if_updated()
                    if runtime.policy.is_compatible():
                        runtime.set_phase("running")
                        runtime.last_error = ""
                        runtime.episode_started_at = time.monotonic()
                    else:
                        neutralize_canonical(
                            canonical_local_controls, device_id=cfg.vjoy_device_id
                        )
                        runtime.last_requested_action = [0.0, -1.0, -1.0]
                        runtime.last_executed_action = [0.0, -1.0, -1.0]
                        runtime.physical_action = [0.0, 0.0, 0.0]
                        runtime._control_enabled = False
                        runtime.publish_status()
                        time.sleep(0.5)
                elif runtime.policy_block_reason():
                    block_reason = runtime.policy_block_reason()
                    runtime.policy_paused_reason = block_reason
                    runtime._control_enabled = False
                    poll_collector_telemetry(env, runtime)
                    # No forced braking, ever. When driving is blocked (learner not
                    # running, or a manual pause), hold NEUTRAL (coast, zero brake)
                    # and keep _started so the worker resumes by itself once the
                    # block clears. The car never slams the brake on its own.
                    # EXCEPT in calibration mode: there the test owns vJoy
                    # exclusively, so we must NOT write neutral over its commands.
                    if not runtime._calibration_mode:
                        neutralize_canonical(
                            canonical_local_controls, device_id=cfg.vjoy_device_id
                        )
                    runtime.policy_action_source = block_reason
                    runtime.policy_raw_action = [0.0, -1.0, -1.0]
                    runtime.physical_action = [0.0, 0.0, 0.0]
                    runtime.last_requested_action = [0.0, -1.0, -1.0]
                    runtime.last_executed_action = [0.0, 0.0, 0.0]
                    runtime.safe_stop_active = True
                    runtime.safe_stop_reason = block_reason
                    runtime.brake_forced_by_safety = False
                    runtime.publish_status()
                    time.sleep(0.1)
                elif runtime._calibration_mode:
                    # Calibration: the test owns vJoy EXCLUSIVELY. Skip the
                    # policy's action (it would overwrite the test's commands)
                    # but still step the env so the game keeps responding.
                    runtime.policy_paused_reason = ""
                    runtime._control_enabled = True
                    runtime._last_safety_cause = ""
                    runtime._safety_hold_since = 0.0
                    runtime.policy_action_source = "calibration"
                    runtime.policy_raw_action = [0.0, -1.0, -1.0]
                    runtime.last_requested_action = [0.0, -1.0, -1.0]
                    run_step = True
                else:
                    runtime.policy_paused_reason = ""
                    cause = watchdog.check_or_neutral(runtime)
                    if cause:
                        from ac_workers.collector_safety import should_reset_after_safety_hold

                        runtime._control_enabled = False
                        runtime._last_safety_cause = cause
                        runtime.safe_stop_active = True
                        runtime.safe_stop_reason = "emergency_safety:{0}".format(cause)
                        runtime.brake_forced_by_safety = False
                        _neutral_vjoy()
                        poll_collector_telemetry(env, runtime)
                        logger.warning("safety neutral worker=%s cause=%s", worker_id, cause)
                        if should_reset_after_safety_hold(runtime, cause) and not runtime._reset_requested:
                            runtime._reset_requested = True
                            runtime._reset_reason_pending = "safety_hold:{0}".format(cause)
                            logger.warning(
                                "safety hold reset worker=%s cause=%s",
                                worker_id,
                                cause,
                            )
                        runtime.publish_status()
                        time.sleep(0.2)
                    else:
                        runtime._control_enabled = True
                        runtime._last_safety_cause = ""
                        runtime._safety_hold_since = 0.0

                        # Nothing is dictated at the start: the AI controls the car
                        # from the very first step (gear, throttle, brake, steer).
                        # We only keep a short countdown that relaxes the
                        # gas-without-movement watchdog while the AI's own throttle
                        # engages first gear, so it does not false-trip a reset.
                        if runtime.post_reset_grace_steps > 0:
                            runtime.post_reset_grace_steps -= 1
                        if runtime.control_mode == CONTROL_MODE_ACTOR and runtime.evaluation is None:
                            # Async actor-learner: the central learner computes the
                            # action; the local published policy is only a fallback.
                            trace = runtime.request_learner_action(state)
                        elif runtime.policy:
                            # Episode-boundary swap: only DETECT a newer policy
                            # while driving — never load it mid-episode (that would
                            # swap the brain under the car and cause teleport/reset
                            # loops). The pending policy is applied after the reset.
                            runtime.policy.detect_pending_version()
                            trace = runtime.policy.select_action_traced(state, env)
                        else:
                            sample = env.sample_action()
                            trace = {
                                "policy_action_source": "env_sample",
                                "policy_raw_action": [
                                    float(x) for x in np.asarray(sample, dtype=np.float32).ravel()[:3]
                                ],
                                "action": sample,
                            }
                        hist_len = len(getattr(env, "states", []) or [])
                        runtime.note_observation_state(
                            state.ravel().tolist() if hasattr(state, "ravel") else list(state),
                            history_frames=hist_len,
                        )
                        raw_action = list(trace.get("policy_raw_action", [0.0, -1.0, -1.0]))
                        if not runtime.observation_valid_for_policy:
                            raw_action = [0.0, -1.0, -1.0]
                        raw_arr = np.asarray(raw_action, dtype=np.float64)
                        if hasattr(env, "policy_action_to_internal"):
                            raw_arr = env.policy_action_to_internal(raw_arr)
                        if getattr(env, "safe_training_mode", False):
                            raw_arr = env._apply_safe_training_controls(raw_arr)
                            env.current_actions = raw_arr.copy()
                        raw_action = raw_arr.tolist()
                        from ac_workers.canonical_controls import internal_to_physical
                        steer, throttle, brake = internal_to_physical(*raw_action)
                        runtime.last_requested_action = list(raw_action)
                        canonical_result = apply_canonical_controls(
                            canonical_local_controls,
                            steer,
                            throttle,
                            brake,
                            device_id=cfg.vjoy_device_id,
                        )
                        runtime.apply_action_trace(trace, canonical_result)
                        runtime.policy_control_write_count += 1
                        if not canonical_result.get("ok") or not canonical_result.get("update_vjd_result"):
                            err = canonical_result.get("error") or "canonical_controls_failed"
                            runtime._vjoy_update_fail_steps += 1
                            runtime.update_vjd_fail_count += 1
                            neutralize_canonical(
                                canonical_local_controls, device_id=cfg.vjoy_device_id
                            )
                            vjoy_state = vjoy_worker_status(runtime._vjoy_update_fail_steps)
                            if vjoy_failure_is_dead(runtime._vjoy_update_fail_steps):
                                # Sustained failure: local recovery resets did not
                                # restore the input path -> give up.
                                runtime.set_phase("stopped", err)
                                runtime.publish_status()
                                return _idle_after_failure()
                            # Transient UpdateVJD/AcquireVJD(FREE) glitch: request an
                            # episode reset (LOCAL recovery — relinquish + reacquire
                            # the SAME device, no global ResetVJD) instead of killing
                            # the worker. Mirrors the movement-watchdog recovery path.
                            if not runtime._reset_requested:
                                runtime._reset_requested = True
                                runtime._reset_reason_pending = REASON_VJOY_NOT_REACHING_CAR
                            runtime.last_error = err
                            logger.warning(
                                "vjoy runtime failure worker=%s fail=%d state=%s: %s (local recovery)",
                                worker_id,
                                runtime._vjoy_update_fail_steps,
                                vjoy_state,
                                err,
                            )
                            runtime.publish_status()
                        else:
                            runtime.update_vjd_success_count += 1
                            if runtime._vjoy_update_fail_steps:
                                runtime._vjoy_update_fail_steps = 0
                                runtime.last_error = ""
                            env.current_actions = np.asarray(raw_action, dtype=np.float32)
                            env.raw_actions = np.asarray(trace['policy_raw_action'], dtype=np.float32)
                            env.executed_policy_action = env.raw_actions.copy()
                            runtime.last_executed_action = list(canonical_result["physical_action"])
                            run_step = True

            if pending_reset:
                # Applique le candidat en attente (torch) ICI, dans le main loop,
                # jamais dans le thread diagnostic (candidate_net y bloquait et
                # faisait expirer le load_candidate).
                _pc = getattr(runtime, "_pending_candidate_idx", None)
                if _pc is not None:
                    _evo = getattr(runtime, "_neuroevolution", None)
                    if _evo is not None:
                        try:
                            runtime.policy.net = _evo.candidate_net(int(_pc))
                        except Exception:
                            logger.exception("load_candidate: candidate_net failed idx=%s", _pc)
                    runtime._pending_candidate_idx = None
                ok_reset, reset_state = _complete_episode_reset(env, runtime, worker_id)
                if not ok_reset:
                    runtime.publish_status()
                    return _idle_after_failure()
                if reset_state is not None:
                    state = reset_state
                episode_id += 1
                step_id = 0
                # Resolve the newest version BEFORE EvaluationRun freezes reloads.
                if runtime.policy:
                    runtime.policy.detect_pending_version()
                    runtime.policy.apply_pending_policy(episode_id)
                if runtime._evaluation_requested:
                    from ac_workers.policy_evaluation import start_evaluation
                    runtime._evaluation_requested = False
                    try:
                        start_evaluation(runtime, _train_cfg, env.state, env.track_length, episode_id)
                        logger.info("evaluation started worker=%s policy_step=%s", worker_id,
                                    runtime.evaluation.training_step)
                    except Exception:
                        runtime._policy_paused = runtime._evaluation_restore_paused
                        logger.exception("evaluation could not start worker=%s", worker_id)
                runtime.set_phase("running")
                runtime.episode_started_at = time.monotonic()
                runtime.steps_since_episode_start = 0
                runtime.last_telemetry_ts = time.time()
                runtime.publish_status()
                continue

            if not run_step:
                if runtime._policy_paused:
                    time.sleep(0.2)
                continue

            next_state, reward, done, info = env.step(action=None)
            with runtime.lock:
                runtime.last_telemetry_ts = time.time()
                step_speed_ms = float(info.get("speed", 0.0)) if isinstance(info, dict) else 0.0
                runtime.telemetry_snapshot = pedal_telemetry(env.client.state)
                runtime.telemetry_snapshot["speed"] = step_speed_ms
                # Détection de voiture « coincée » supprimée : la SEULE fin
                # d'épisode / téléportation est le chronomètre de 30 s. Plus aucun
                # respawn déclenché par un verdict « stuck / pas de gaz / pédale
                # non reconnue » — le worker roule ses 30 s quoi qu'il arrive.
                runtime.last_observation = state.ravel().tolist()
                runtime.note_observation_state(
                    runtime.last_observation,
                    history_frames=len(getattr(env, "states", []) or []),
                )
                done_reason = ""
                if isinstance(info, dict):
                    done_reason = info.get("done_reason", "")
                episode_age_seconds = (
                    time.monotonic() - runtime.episode_started_at
                    if hasattr(runtime, "episode_started_at") and runtime.episode_started_at > 0
                    else 0.0
                )
                # Termination grace belongs to the environment. Do not suppress
                # done here while still recording its terminal penalty in replay.
                reward_components = (
                    info.get("reward_components", {})
                    if isinstance(info, dict) else {}
                )
                payload = {
                    "worker_id": worker_id,
                    "episode_id": episode_id,
                    "step_id": step_id,
                    "obs": state.tolist(),
                    "action": list(runtime.policy_raw_action),
                    "action_schema_version": ACTION_SCHEMA,
                    "collector_session_id": runtime.session_id,
                    "transition_valid": bool(runtime.observation_valid_for_policy
                        and not getattr(env, '_safe_emergency_active', False)
                        and reward_components.get('progress_valid', True)),
                    "terminated": bool(done and not info.get('TimeLimit.truncated', False)),
                    "reward": float(reward),
                    "next_state": next_state.tolist(),
                    "done": bool(done),
                    "done_reason": done_reason,
                    "episode_age_seconds": round(episode_age_seconds, 3),
                    "action_source": runtime.policy_action_source or "unknown",
                    "policy_raw_action": list(runtime.policy_raw_action),
                    "physical_action": list(runtime.physical_action),
                    "policy_seed": int(runtime.policy_seed),
                    "observation_hash": str(runtime.observation_hash),
                    "reward_components": reward_components,
                }
                if runtime.evaluation is not None:
                    done = runtime.evaluation.observe(env.state, reward, done, info)
                    if runtime.evaluation.finished:
                        from ac_workers.policy_evaluation import finish_evaluation
                        finish_evaluation(runtime)
                        logger.info("evaluation finished worker=%s collection_resumed=%s", worker_id,
                                    not runtime._policy_paused)
                else:
                    client.push_transition(payload)
                    runtime.transition_count += 1
                    # PBT — per-worker brain: feed this worker's OWN transition into
                    # its local replay buffer and run a local SAC update when due.
                    _local_policy = runtime.policy
                    if _local_policy is not None and getattr(_local_policy, "local_replay", None) is not None:
                        _local_policy.local_append(
                            state,
                            runtime.policy_raw_action,
                            float(reward),
                            next_state,
                            bool(payload.get("terminated")),
                            bool(done),
                        )
                        _local_policy.maybe_local_update()
                client.heartbeat(worker_id)
                runtime.last_reward = float(reward)
                runtime.episode_total_reward += float(reward)
                # Neuroévolution : accumule les métriques d'évaluation.
                _components = getattr(env, "_last_reward_components", None) or {}
                runtime.episode_progress_m += max(0.0, float(_components.get("signed_progress_m", 0.0)))
                if _components.get("completed_clean_lap"):
                    runtime.episode_laps += 1
                    _lt = float(_components.get("clean_lap_time_sec", 0.0) or 0.0)
                    if _lt > 0.0 and (runtime.episode_best_lap_time <= 0.0 or _lt < runtime.episode_best_lap_time):
                        runtime.episode_best_lap_time = _lt
                runtime.heartbeat_ts = time.time()
                runtime.heartbeat_monotonic = time.monotonic()
                step_id += 1
                runtime.current_episode_id = episode_id
                runtime.steps_since_episode_start = step_id
                state = next_state
                runtime.publish_status()
                if runtime.transition_count == 1:
                    print("collector worker {0} first transition ready".format(worker_id))
                if done:
                    logger.info(
                        "worker %s episode_done reason=%s age=%.1fs steps=%d reward=%.3f action_source=%s",
                        worker_id, done_reason, episode_age_seconds, step_id,
                        float(reward), runtime.policy_action_source,
                    )
                    if not done_reason and runtime.evaluation is not None and runtime.evaluation.episodes:
                        done_reason = runtime.evaluation.episodes[-1]["done_reason"]
                    runtime.last_reset_reason = done_reason or "evaluation_complete"
                    runtime.note_episode_completed(runtime.last_reset_reason)
                    # PBT — resynchronize with the main brain at this teleport.
                    # Training episodes only: evaluation keeps its frozen policy.
                    if runtime.evaluation is None:
                        _pbt_policy = runtime.policy
                        if _pbt_policy is not None and getattr(_pbt_policy, "pbt_resync", None) is not None:
                            # Score = récompense PAR SECONDE (= récompense/m × vitesse moyenne).
                            # Récompense la VITESSE et l'efficacité, pas la distance brute.
                            _pbt_seconds = max(float(episode_age_seconds), 0.5)
                            _pbt_score = runtime.episode_total_reward / _pbt_seconds
                            _pbt_result = _pbt_policy.pbt_resync(_pbt_score)
                            logger.info(
                                "pbt: worker=%s resync=%s score=%.4f (reward=%.3f over %.1fs)",
                                worker_id,
                                _pbt_result,
                                _pbt_score,
                                runtime.episode_total_reward,
                                _pbt_seconds,
                            )
                        elif getattr(runtime, "_neuroevolution", None) is not None:
                            from ac_workers.evaluation import evaluation_score

                            _score = evaluation_score([{
                                "laps": runtime.episode_laps,
                                "lap_time_sec": runtime.episode_best_lap_time,
                                "progress_m": runtime.episode_progress_m,
                            }])
                            runtime._last_candidate_score = _score
                            logger.info(
                                "neuroevolution: worker=%s candidate=%s score=%.3f laps=%d progress=%.1fm",
                                worker_id, getattr(runtime, "_current_candidate", -1),
                                _score, runtime.episode_laps, runtime.episode_progress_m,
                            )
                    runtime.episode_total_reward = 0.0
                    runtime.episode_laps = 0
                    runtime.episode_progress_m = 0.0
                    runtime.episode_best_lap_time = 0.0
                    # Épisode terminé (chronomètre 30 s) : on NE reset PAS ici.
                    # On attend le prochain load_candidate, qui posera
                    # _reset_requested et déclenchera LE reset (respawn) via le
                    # haut de boucle (pending_reset). Sinon la voiture se téléporte
                    # deux fois de suite (reset de fin d'épisode + reset candidat).
                    runtime.set_phase("idle")
                    runtime.publish_status()
                    _wait_deadline = time.monotonic() + 120.0
                    while (not runtime._reset_requested and not shutdown_event.is_set()
                           and time.monotonic() < _wait_deadline):
                        neutralize_canonical(canonical_local_controls, device_id=cfg.vjoy_device_id)
                        poll_collector_telemetry(env, runtime)
                        runtime.write_minimal_status()
                        runtime.touch_heartbeat()
                        time.sleep(0.1)
                    if shutdown_event.is_set():
                        break
                    if runtime._reset_requested:
                        runtime.set_phase("running")
                    runtime.publish_status()
        _startup_log("collector worker {0} graceful shutdown (running)".format(worker_id))
        return 0
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        _startup_log("collector worker {0} fatal error\n{1}".format(worker_id, tb))
        runtime.set_phase("stopped", str(exc))
        try:
            runtime.write_minimal_status()
        except OSError as write_exc:
            _startup_log("collector status write failed: {0}".format(write_exc))
        return _idle_after_failure()
    finally:
        if runtime.evaluation is not None:
            runtime.evaluation.finish(aborted=True)
        heartbeat_stop.set()
        if diag:
            diag.stop()
        if cfg is not None:
            if env is not None:
                from ac_workers.collector_live_attach import neutralize_env_controls

                ok, _reason = neutralize_env_controls(env)
                runtime.vjoy_neutralization_verified = ok
            else:
                from ac_workers.collector_safety import neutral_vjoy

                neutral_vjoy(cfg.vjoy_device_id)
            # Graceful vJoy release: relinquish the device THIS process owned so the
            # driver does not leak a stuck PDO handle (the AcquireVJD status=1/FREE bug)
            # for the next run. Effective on a clean exit (shutdown sentinel / normal
            # end); a hard TerminateProcess still skips this, which is why the
            # orchestrator requests a graceful stop before force-killing.
            try:
                from ac_workers.vjoy_session import relinquish_vjd

                lc = None
                controls = getattr(getattr(env, "client", None), "controls", None)
                if controls is not None:
                    lc = getattr(controls, "local_controls", None)
                if lc is None and prehold is not None:
                    lc = getattr(prehold, "controls", None)
                vj = getattr(lc, "vj", None)
                if vj is not None and getattr(vj, "acquired", False):
                    try:
                        vj.close()
                    except Exception:
                        pass
                relinquish_vjd(cfg.vjoy_device_id)
            except Exception:
                pass
            # Clean ownership record on graceful exit so the reaper does not later
            # mistake this (now released) device for a leak.
            try:
                _clear_vjoy_owner(cfg.vjoy_device_id, pid=os.getpid())
            except Exception:
                pass
        if env:
            try:
                env.close()
            except Exception:
                pass
        if prev is not None:
            restore_worker_env(prev)
        clear_collector_shutdown(worker_id)
        clear_collector_resume(worker_id)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-id", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return run_collector(args.worker_id, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
