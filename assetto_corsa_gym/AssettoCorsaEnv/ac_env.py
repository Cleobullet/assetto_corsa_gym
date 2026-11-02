import os
import sys
import numpy as np
import pandas as pd
import pickle
from gym import spaces
from gym import Env
from gym.spaces import Box
from gym import utils as gym_utils
from datetime import datetime
import time
import yaml
import json
from pathlib import Path

from AssettoCorsaEnv.ac_client import Client
from AssettoCorsaEnv.track import Track
from AssettoCorsaEnv.reference_lap import ReferenceLap
import AssettoCorsaEnv.sensors_ray_casting as sensors_ray_casting
from AssettoCorsaEnv.sensors_ray_casting import MAX_RAY_LEN
from AssettoCorsaEnv.gap import get_gap
from AssettoCorsaEnv.progress_reward import (
    CleanLapTracker,
    is_currently_off_track,
    progress_reward,
    spline_delta_m,
    update_wrong_way,
)
from AssettoCorsaEnv.training_safety import (
    apply_safe_control_limits,
    apply_v3_control_limits,
    auto_shift_neutral_to_first,
    clip_safe_action_delta,
    executed_action_for_buffer,
    policy_steer_to_internal,
    sample_safe_exploration_action,
    sample_safe_exploration_action_v3,
    SAFE_ACC_PEDAL_MAX_DEFAULT,
    SAFE_BRAKE_PEDAL_MAX_DEFAULT,
    SAFE_EXPLORATION_STEER_DEG_DEFAULT,
    STEER_HARD_FAIL_DEG,
    STEER_SAFE_DEG_DEFAULT,
)

import torch

import logging
logger = logging.getLogger(__name__)

# filter the racing line to this circle
RACING_LINE_DIAMETER_CROP = 300.
PAST_ACTIONS_WINDOW = 3

# Curvature look ahead
CURV_LOOK_AHEAD_DISTANCE = 300. # METERS
CURV_LOOK_AHEAD_VECTOR_SIZE = 12 # SIZE OF THE VECTOR
CURV_NORMALIZATION_CONSTANT = 0.1

# target speed
TARG_SPEED_LOOK_AHEAD_VECTOR_SIZE = CURV_LOOK_AHEAD_VECTOR_SIZE
TARG_SPEED_LOOK_AHEAD_DISTANCE = CURV_LOOK_AHEAD_DISTANCE

# episode termination
TERMINAL_LIMIT_PROGRESS_SPEED  = 1.0  # [m/s], episode terminates if car is running slower than this limit
TERMINAL_JUDGE_TIMEOUT = 10.       # If after this number of seconds still no progress, terminate the episode. In seconds

TOP_SPEED_MS = 80.

def get_date_timestemp():
    return datetime.now().strftime('%Y%m%d_%H%M%S.%f')[:-3]

def load_yaml(file_path):
    with open(file_path, 'r') as file:
        return yaml.safe_load(file)

def to_pickle(obj, file, verbose=False):
    if verbose: print('saving to..', file)
    with open(file, 'wb') as f: pickle.dump(obj, f)

def convert_to_seconds(time_str):
    """
    Convert time from "minutes:seconds:milliseconds" format to seconds with decimals.

    :param time_str: A string representing the time in "minutes:seconds:milliseconds" format.
    :return: The time converted to seconds (as a float).
    """
    parts = time_str.split(":")
    if len(parts) != 3:
        raise ValueError("Time string must be in 'minutes:seconds:milliseconds' format")

    minutes = int(parts[0])
    seconds = int(parts[1])
    milliseconds = int(parts[2])

    return minutes * 60 + seconds + milliseconds / 1000

_REWARD_OVERRIDE_KEYS = (
    "reward_progress_per_m",
    "reward_offtrack_per_step",
    "reward_smoothness_coef",
    "reward_step_cost",
    "reward_idle_speed_kmh",
    "reward_n_sectors",
    "reward_clean_sector_bonus",
    "reward_clean_lap_bonus",
    "reward_clean_lap_time_ref_sec",
    "reward_oot_terminal_penalty",
    "reward_idle_penalty",
)
_reward_override_mtime = None
_reward_override_values = None


def _apply_reward_override(config):
    """Live search writes ac_workers/status/reward_override.json. Collectors pick it up without a restart."""
    global _reward_override_mtime, _reward_override_values
    path = Path(__file__).resolve().parents[2] / "ac_workers" / "status" / "reward_override.json"
    try:
        mtime = path.stat().st_mtime if path.is_file() else None
    except OSError:
        mtime = None
    if mtime != _reward_override_mtime:
        _reward_override_mtime = mtime
        _reward_override_values = None
        if mtime is not None:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                params = payload.get("params") if isinstance(payload, dict) else None
                if isinstance(params, dict):
                    _reward_override_values = {
                        key: float(params[key]) for key in _REWARD_OVERRIDE_KEYS if key in params
                    }
            except (OSError, ValueError, TypeError):
                _reward_override_values = None
    if not _reward_override_values:
        return
    for key, value in _reward_override_values.items():
        setattr(config, key, value)


class TaskIdsIndexer:
    def __init__(self, tracks, cars):
        self.tracks = tracks
        self.cars = cars
        self.combination_indices = {}
        self._generate_indices()
        self.num_tasks = self.get_number_of_tasks()

    def _generate_indices(self):
        """Private method to generate indices for each track and car combination."""
        index = 0
        for track in self.tracks:
            for car in self.cars:
                key = f"{track}_{car}"
                self.combination_indices[key] = index
                index += 1

    def get_index(self, track, car):
        """Method to get the index of a specific track and car combination."""
        key = f"{track}_{car}"
        return self.combination_indices.get(key, -1)  # Return -1 if the combination does not exist

    def get_number_of_tasks(self):
        """Method to return the total number of unique track and car combinations."""
        return len(self.combination_indices)

    def get_task_one_hot(self, task_id):
        """Method to get the one-hot encoding of a specific track and car combination."""
        assert task_id != -1, "Invalid task ID"
        task_one_hot = np.zeros(self.num_tasks)
        task_one_hot[task_id] = 1.
        return task_one_hot

class AssettoCorsaEnv(Env, gym_utils.EzPickle):
    # List of tracks and cars when using task IDs
    tracks = [
        "ks_barcelona-layout_gp",
        "ks_red_bull_ring-layout_gp",
        "monza",
        "indianapolis_sp"
    ]

    cars = [
        "dallara_f317",
        #"bmw_z4_gt3"
    ]

    obs_channels_info = {
        'speed': TOP_SPEED_MS,  # km/h
        'gap': 10.,
        'LastFF': 1.,
        'RPM': 10000.,
        'accelX': 5.,
        'accelY': 5.,

        'angular_velocity_x': np.pi,
        'angular_velocity_y': np.pi,
        'angular_velocity_z': np.pi,
        'local_velocity_x': TOP_SPEED_MS,
        'local_velocity_y': 20.,
        'local_velocity_z': 5.,
        'SlipAngle_fl': 25.,
        'SlipAngle_fr': 25.,
        'SlipAngle_rl': 25.,
        'SlipAngle_rr': 25.,
        'wheel_speed_rr': TOP_SPEED_MS / 3.6,
        'wheel_speed_rl': TOP_SPEED_MS / 3.6,
        'wheel_speed_fr': TOP_SPEED_MS / 3.6,
        'wheel_speed_fl': TOP_SPEED_MS / 3.6,
        'tyre_slip_ratio_fl': 1.,
        'tyre_slip_ratio_fr': 1.,
        'tyre_slip_ratio_rl': 1.,
        'tyre_slip_ratio_rr': 1.,
        'Dy_rr': 2.,    # lateral grip
        'Dy_rl': 2.,    # very noisy. Goes to zero when the wheel is spinning
        'Dy_fr': 2.,
        'Dy_fl': 2.,
        'LapCount': 1.,
        'LapDist': 1.,
        # commands feedback
        'steerAngle': 450,
        'accStatus': 1.,
        'brakeStatus': 1.,
        'actualGear': 8.,
    }

    obs_enabled_channels = [
        'local_velocity_x',
        'local_velocity_y',
        'angular_velocity_y',
    ]

    obs_extra_enabled_channels = [
        'local_velocity_x',
        'local_velocity_y',
        'angular_velocity_y',
        'steerAngle',
        'accStatus',
        'brakeStatus',
        'world_position_x',
        'world_position_y',
        'yaw'
    ]


    def __init__(self, config,
                 output_path: None,
                 ac_configs_path= None,
                 torch_device = None,
                 enable_out_of_track_calculation=True,
                 max_episode_steps=None,
                 max_gap=None,
                 gap_const = 12., # gap penalization
                 penalize_gap = True,
                 save_observations=True,
                 verbose=False
                 ):
        """ Assetto Corsa External Driver.
        """
        gym_utils.EzPickle.__init__(self)
        self.config = config
        self.output_path = output_path
        self.ctrl_rate = self.config.ego_sampling_freq # check
        self.track_name = self.config.track
        self.car_name = self.config.car
        self.ac_configs_path = ac_configs_path
        self.use_target_speed = self.config.use_target_speed
        self.torch_device = torch_device
        self.use_relative_actions = self.config.use_relative_actions
        self.enable_sensors = self.config.enable_sensors
        self.oot_mode = str(getattr(self.config, 'oot_mode', 'local_gap'))
        self.oot_gap_m = float(getattr(self.config, 'oot_gap_m', 12.0))
        self.oot_soft_gap_m = float(getattr(self.config, 'oot_soft_gap_m', 8.0))
        self.enable_out_of_track_calculation = self.oot_mode == 'occupancy'
        self._max_episode_steps = max_episode_steps
        self.enable_low_speed_termination = self.config.enable_low_speed_termination
        self.max_gap = max_gap
        self.gap_const = gap_const
        self.penalize_gap = penalize_gap
        self.save_observations = save_observations
        self.recover_car_on_done = self.config.recover_car_on_done
        self.enable_out_of_track_termination = self.config.enable_out_of_track_termination
        self.add_previous_obs_to_state = self.config.add_previous_obs_to_state
        self.send_reset_at_start = self.config.send_reset_at_start
        self.max_steer_rate = self.config.max_steer_rate
        self.use_obs_extra = self.config.use_obs_extra
        self.use_reference_line_in_reward = self.config.use_reference_line_in_reward

        # from the config
        self.use_ac_out_of_track = self.config.use_ac_out_of_track
        self.enable_out_of_track_penalty = self.config.enable_out_of_track_penalty # oot penalization in the reward function

        self.penalize_actions_diff = config.penalize_actions_diff
        self.penalize_actions_diff_coef = config.penalize_actions_diff_coef

        self.max_laps_number = self.config.max_laps_number

        self.tracks_path = os.path.join(self.ac_configs_path, "tracks")
        self.cars_paths = os.path.join(self.ac_configs_path, "cars")

        self.tracks_config = load_yaml(os.path.join(self.tracks_path, "config.yaml"))
        self.set_track(self.track_name)

        # task id
        self.tasks_ids = TaskIdsIndexer(self.tracks, self.cars)
        self.current_task_id = self.tasks_ids.get_index(self.track_name, self.car_name)

        self.states = []      # some extra channels calculated. Without initialization
        self.history_obs = []  # history of observations
        self.episodes_stats = []
        self.info = {}

        self.total_steps = 0
        self.n_episodes = 0
        self.ep_steps = 0
        self.ep_reward = 0.
        self.stats_saved = False

        self.client = Client(self.config)
        self.static_info = {}
        self.ac_mod_config = {}

        if self.output_path:
            self.laps_path = self.output_path + os.sep + "laps" + os.sep
            os.makedirs(self.laps_path, exist_ok=True)

        self.dt = 1. / self.ctrl_rate

        # load track  # TODO: use pytorch?
        # Occupancy grid is ~4 GiB; only load it for oot_mode=occupancy.
        grid_file = self.track_grid_file
        if not self.enable_out_of_track_calculation:
            grid_file = None
        self.track = Track(track_file_path=self.track_file, track_grid_file=grid_file,
                           torch_device=None)

        #
        self.ref_lap = ReferenceLap(self.ref_lap_file, use_target_speed=self.use_target_speed)
        self.racing_line = self.ref_lap.get_racing_line_time()
        if torch_device:
            self.racing_line_torch =  torch.tensor(self.racing_line, device=torch_device, dtype=torch.float)

        if self.use_target_speed:
            self.target_speed = self.ref_lap.get_channel_time("target_speed")

        self.ref_lap_dist_channel_time = self.ref_lap.get_channel_time("lapDistance")

        if self.enable_sensors:
            from AssettoCorsaEnv.lidar import LiDAR5Rays
            self.lidar = LiDAR5Rays(self.track.left_border_x, self.track.left_border_y,
                                    self.track.right_border_x, self.track.right_border_y)

        #
        # for gym
        #
        # Define physical and simulator parameters for each channel.
        steer_map_file = Path(self.cars_paths) / self.car_name / 'steer_map.csv'
        self.max_steer_deg = pd.read_csv(steer_map_file).values[1,1]
        self.norm_steer_at_max = pd.read_csv(steer_map_file).values[1,0]

        # Original controls_rate_limit (in deg/s scaled per time step)
        self.controls_rate_limit = np.array([[-self.max_steer_rate, self.max_steer_rate],
                                              [-1200/100, 1200/100],  # pedal: (falling edge)
                                              [-1200/100, 1200/100],  # brake: (release/press)
                                             ]) * (1/self.ctrl_rate)


        # Conversion factor (deg per normalized unit) for steering.
        self.steering_scale_factor = self.max_steer_deg / self.norm_steer_at_max
        # For pedal and brake: full range is already [0,1].
        # Create a per-channel scale factor vector.
        self.controls_scale_factor = np.array([self.steering_scale_factor, 1.0, 1.0])
        # Adjust the rate-limit vector per channel (element-wise division along the second axis)
        self.adjusted_controls_rate_limit = self.controls_rate_limit / self.controls_scale_factor[:, np.newaxis]

        # Now, set the absolute limits for the simulator.
        # For steering, use the normalized value corresponding to max steering.
        self.controls_min_values = np.array([-self.norm_steer_at_max, -1.0, -1.0])
        self.controls_max_values = np.array([ self.norm_steer_at_max,  1.0,  1.0])

        self.safe_training_mode = getattr(self.config, 'safe_training_mode', False)
        self.auto_shift_to_first = getattr(self.config, 'auto_shift_to_first', False)
        self.safe_steer_deg = float(getattr(self.config, 'safe_steer_deg', STEER_SAFE_DEG_DEFAULT))
        self.safe_acc_pedal_max = float(getattr(self.config, 'safe_acc_pedal_max', SAFE_ACC_PEDAL_MAX_DEFAULT))
        self.safe_brake_pedal_max = float(getattr(self.config, 'safe_brake_pedal_max', SAFE_BRAKE_PEDAL_MAX_DEFAULT))
        self.safe_exploration_steer_deg = float(
            getattr(self.config, 'safe_exploration_steer_deg', SAFE_EXPLORATION_STEER_DEG_DEFAULT))
        self.steer_control_v3 = bool(getattr(self.config, 'steer_control_v3', False))
        self.exploration_v3 = bool(getattr(self.config, 'exploration_v3', False))
        self.steer_limit_deg = float(getattr(self.config, 'steer_limit_deg', 20.0))
        self.steer_max_delta_deg = float(getattr(self.config, 'steer_max_delta_deg', 3.0))
        self.steer_soft_zone_deg = float(getattr(self.config, 'steer_soft_zone_deg', 2.0))
        self.steer_center_deadband_deg = float(getattr(self.config, 'steer_center_deadband_deg', 1.0))
        self.steer_center_decay = float(getattr(self.config, 'steer_center_decay', 0.5))
        self.reward_v3 = bool(getattr(self.config, 'reward_v3', False))
        self.reward_gap_penalty_coef = float(getattr(self.config, 'reward_gap_penalty_coef', 0.0))
        self.reward_gap_penalty_cap = float(getattr(self.config, 'reward_gap_penalty_cap', 0.0))
        self.reward_steer_penalty_coef = float(getattr(self.config, 'reward_steer_penalty_coef', 0.0))
        self.reward_oot_terminal_penalty = float(getattr(self.config, 'reward_oot_terminal_penalty', 0.0))
        self.reward_idle_penalty = float(getattr(self.config, 'reward_idle_penalty', 0.0))
        self.reward_brake_penalty_coef = float(getattr(self.config, 'reward_brake_penalty_coef', 0.0))
        self.reward_brake_penalty_speed_kmh = float(
            getattr(self.config, 'reward_brake_penalty_speed_kmh', 40.0)
        )
        self.reward_wrong_way_steer_coef = float(
            getattr(self.config, 'reward_wrong_way_steer_coef', 0.0)
        )
        self.reward_centerline_bonus_coef = float(
            getattr(self.config, 'reward_centerline_bonus_coef', 0.0)
        )
        self.reward_centerline_bonus_width = float(
            getattr(self.config, 'reward_centerline_bonus_width', 15.0)
        )
        self.reward_correct_steer_bonus_coef = float(
            getattr(self.config, 'reward_correct_steer_bonus_coef', 0.0)
        )
        self.reward_gap_factor_sigma = float(
            getattr(self.config, 'reward_gap_factor_sigma', 0.0)
        )
        self._last_reward_components = {}
        self.executed_policy_action = np.array([0.0, -1.0, -1.0])
        self.oot_grace_sec = float(getattr(self.config, 'oot_grace_sec', 3.0))
        self.oot_hold_sec = float(getattr(self.config, 'oot_hold_sec', 1.5))
        self.stuck_timeout_sec = float(getattr(self.config, 'stuck_timeout_sec', 3.0))
        self.stuck_speed_kmh = float(getattr(self.config, 'stuck_speed_kmh', 6.0))
        self.stuck_move_eps_m = float(getattr(self.config, 'stuck_move_eps_m', 3.0))
        self.no_progress_timeout_sec = float(getattr(self.config, 'no_progress_timeout_sec', 8.0))
        self.no_progress_credit_m = float(getattr(self.config, 'no_progress_credit_m', 2.0))
        self.moved_speed_kmh = float(getattr(self.config, 'moved_speed_kmh', 8.0))
        self.wrong_way_timeout_sec = float(getattr(self.config, 'wrong_way_timeout_sec', 5.0))
        self.wrong_way_speed_kmh = float(getattr(self.config, 'wrong_way_speed_kmh', 8.0))
        self.wrong_way_confirm_m = float(getattr(self.config, 'wrong_way_confirm_m', 8.0))
        self._clean_lap_tracker = CleanLapTracker(
            lap_bonus=float(getattr(self.config, 'reward_clean_lap_bonus', 20.0)),
        )
        self.low_speed_grace_sec = float(getattr(self.config, 'low_speed_grace_sec', 0.0))
        self.low_speed_timeout_sec = float(
            getattr(self.config, 'low_speed_timeout_sec', TERMINAL_JUDGE_TIMEOUT))
        self.low_speed_threshold_kmh = float(
            getattr(self.config, 'low_speed_threshold_kmh', TERMINAL_LIMIT_PROGRESS_SPEED * 3.6))
        self._exploration_rng = np.random.default_rng()
        self._safe_emergency_active = False
        self._safe_recover_used = False
        self._force_episode_done = False
        self._pending_shift_fail = False
        self._reset_termination_trackers()

        # actions space is always -1 to 1 for most algorithms
        self.action_dim = 3
        self.action_space = Box(low=np.array([-1.0, -1.0, -1.0]), high=np.array([1.0, 1.0, 1.0]))

        state_dim = len(self.obs_enabled_channels)  # 3 télémétries
        if self.enable_sensors:
            state_dim += 5  # 5 rayons LiDAR (LiDAR5Rays)
        self.previous_obs_to_state_dim = state_dim
        self.state_dim = state_dim  # = 8

        self.obs_shape = (self.state_dim,)
        if self.use_obs_extra:
            self.obs_extra_shape = (len(self.obs_extra_enabled_channels),)  # (features,)
        else:
            self.obs_extra_shape = None

        if verbose:
            logger.info(f"enable_sensors {self.enable_sensors}")
            logger.info(f"use_relative_actions {self.use_relative_actions}")
            logger.info(f"use_target_speed {self.use_target_speed}")
            logger.info(f"use_ac_out_of_track: {self.use_ac_out_of_track}")
            logger.info(f"recover_car_on_done: {self.recover_car_on_done}")
            logger.info(f"enable_out_of_track_termination: {self.enable_out_of_track_termination}")
            logger.info(f"enable_low_speed_termination: {self.enable_low_speed_termination}")
            logger.info(f"enable_out_of_track_penalty: {self.enable_out_of_track_penalty}")
            logger.info(f"enable_task_id_in_obs: {self.config.enable_task_id_in_obs}")
            logger.info(f"track: {self.config.track}")
            logger.info(f"car: {self.config.car}")
            if self.config.enable_task_id_in_obs:
                logger.info(f"task_id: {self.current_task_id} num_tasks: {self.tasks_ids.get_number_of_tasks()}")

        logger.info(f"state_dim {self.state_dim}")
        logger.info(f"action_space: {self.action_space}")

        high = np.full((self.state_dim, ), np.inf)
        low = np.full((self.state_dim, ), -np.inf)
        self.observation_space = Box(low=low, high=high)

        # scale and obs channels
        self.obs_channels_scales = np.array([self.obs_channels_info[ch] for ch in self.obs_enabled_channels])
        if verbose:
            logger.info("Channels in the observation")
            for ch in self.obs_enabled_channels:
                logger.info("%s: scale %f" % (ch, self.obs_channels_info[ch]))

        # needed for stable baselines
        self.reward_range = (-np.inf, np.inf)
        self.metadata = {'render.modes': ['human', 'rgb_array'], 'video.frames_per_second': 30}

        self.is_metaworld = False

    def _reset_termination_trackers(self):
        self._termination_stamp = None
        self._driving_elapsed = 0.0
        self._oot_hold_steps = 0
        self._episode_has_moved = False
        self._stuck_hold_steps = 0
        self._stuck_last_xy = None
        self._no_progress_hold_steps = 0
        self._no_progress_m = 0.0
        self._wrong_way_hold_steps = 0
        self._wrong_way_meters = 0.0
        self._prev_spline = None
        if getattr(self, "_clean_lap_tracker", None) is not None:
            self._clean_lap_tracker.reset()

    def set_reset_state(self, send_reset_at_start):
        self.send_reset_at_start = send_reset_at_start

    def update_racing_line(self, x, y):
        # use a cropped version of the racing line => update the gpu as well
        self.racing_line = self.ref_lap.get_racing_line_time()
        self.racing_line[np.sqrt( (self.racing_line[:,0] - x)**2 + (self.racing_line[:,1] - y)**2 ) < RACING_LINE_DIAMETER_CROP]
        if self.torch_device:
            self.racing_line_torch = torch.tensor(self.racing_line, device=self.torch_device, dtype=torch.float)

    def sample_action(self):
        if self.safe_training_mode and not self.use_relative_actions:
            if self.exploration_v3:
                return sample_safe_exploration_action_v3(
                    self._exploration_rng,
                    self.norm_steer_at_max,
                    self.max_steer_deg,
                )
            return sample_safe_exploration_action(
                self._exploration_rng,
                self.norm_steer_at_max,
                self.max_steer_deg,
                exploration_steer_deg=self.safe_exploration_steer_deg,
            )
        action = self.action_space.sample()
        if self.safe_training_mode and self.use_relative_actions:
            action = clip_safe_action_delta(
                action,
                self.current_actions,
                self.adjusted_controls_rate_limit,
                self.norm_steer_at_max,
                self.max_steer_deg,
                steer_safe_deg=self.safe_steer_deg,
                acc_pedal_max=self.safe_acc_pedal_max,
                brake_pedal_max=self.safe_brake_pedal_max,
            )
        return action

    def _safe_limit_kwargs(self):
        return {
            "steer_safe_deg": self.safe_steer_deg,
            "acc_pedal_max": self.safe_acc_pedal_max,
            "brake_pedal_max": self.safe_brake_pedal_max,
        }

    def _v3_limit_kwargs(self):
        return {
            "steer_limit_deg": self.steer_limit_deg,
            "max_delta_deg": self.steer_max_delta_deg,
            "soft_zone_deg": self.steer_soft_zone_deg,
            "center_deadband_deg": self.steer_center_deadband_deg,
            "center_decay": self.steer_center_decay,
            "acc_pedal_max": self.safe_acc_pedal_max,
            "brake_pedal_max": self.safe_brake_pedal_max,
            "acc_max_delta_pedal": float(getattr(self.config, "acc_max_delta_pedal", 0.0)),
            "brake_max_delta_pedal": float(getattr(self.config, "brake_max_delta_pedal", 0.0)),
        }

    def policy_action_to_internal(self, raw) -> np.ndarray:
        """Scale SAC steer [-1, 1] into AC internal units for ±steer_limit_deg."""
        out = np.array([0.0, -1.0, -1.0], dtype=np.float64)
        values = np.asarray(raw, dtype=np.float64).ravel()[:3]
        out[: len(values)] = values
        limit = self.steer_limit_deg if self.steer_control_v3 else self.safe_steer_deg
        out[0] = policy_steer_to_internal(
            out[0], self.norm_steer_at_max, self.max_steer_deg, limit
        )
        # Policy pedals use ReLU; all subsequent limit/actuator code uses [-1,1].
        # Brake reaches 100% at raw=0.75 (SAC tanh rarely hits +1).
        from ac_workers.canonical_controls import BRAKE_FULL_AT

        out[1] = 2.0 * float(np.clip(out[1], 0.0, 1.0)) - 1.0
        brake_pedal = float(np.clip(max(0.0, out[2]) / BRAKE_FULL_AT, 0.0, 1.0))
        out[2] = 2.0 * brake_pedal - 1.0
        return out

    def _apply_safe_training_controls(self, target_actions: np.ndarray) -> np.ndarray:
        if self.steer_control_v3:
            return apply_v3_control_limits(
                target_actions,
                self.current_actions,
                self.norm_steer_at_max,
                self.max_steer_deg,
                **self._v3_limit_kwargs(),
            )
        return apply_safe_control_limits(
            target_actions,
            self.norm_steer_at_max,
            self.max_steer_deg,
            **self._safe_limit_kwargs(),
        )

    def get_executed_action_for_buffer(self) -> np.ndarray:
        return np.array(self.raw_actions, dtype=np.float32).copy()

    def _trigger_safe_emergency(self, reason: str):
        if self._safe_emergency_active:
            return
        self._safe_emergency_active = True
        self._force_episode_done = True
        logger.warning("Safe training emergency: %s", reason)
        try:
            self.client.controls.set_controls(steer=0.0, acc=-1.0, brake=-1.0)
            self.client.respond_to_server()
        except Exception as exc:
            logger.error("Safe training: failed to release controls: %s", exc)
        if not self._safe_recover_used:
            self._safe_recover_used = True
            try:
                self.recover_car()
            except Exception as exc:
                logger.error("Safe training: recover_car failed: %s", exc)

    def preprocess_actions(self, actions, current_actions):
        if self.use_relative_actions:
            # Fully vectorized update: scale each action by the adjusted per-channel rate limit.
            new_actions = current_actions + actions * self.adjusted_controls_rate_limit[:, 1]
        else:
            new_actions = actions
        return np.clip(new_actions, self.controls_min_values, self.controls_max_values)

    def inverse_preprocess_actions(self, prev_abs_actions, current_abs_actions):
        if self.use_relative_actions:
            # Use the adjusted rate limit's upper bound for each channel.
            max_delta = self.adjusted_controls_rate_limit[:, 1]
            actions = (current_abs_actions - prev_abs_actions) / max_delta
            return np.clip(actions, -1.0, 1.0)
        else:
            return np.clip(current_abs_actions, self.controls_min_values, self.controls_max_values)

    def set_actions(self, actions):
        """
        Apply the actions to the sim right away. The step function can be called later
        """
        # get state from the sim
        self.raw_actions = actions.copy()

        if getattr(self.config, 'action_schema_version', '') == 'normalized_policy_relu_v2':
            self.current_actions = self.policy_action_to_internal(actions)
        else:
            self.current_actions = self.preprocess_actions(actions, self.current_actions)
        if self.safe_training_mode:
            self.current_actions = self._apply_safe_training_controls(self.current_actions)
        self.executed_policy_action = self.get_executed_action_for_buffer()
        self.actions = self.current_actions

        self.client.controls.set_controls(steer=self.actions[0], acc=self.actions[1], brake=self.actions[2])
        self.client.respond_to_server()

    def step(self, action=None):
        """
        If actions is None, the the policy should set the actions before by calling set_actions

        Blocks until the sim returns the next state
        """
        self.ep_steps += 1
        self.total_steps += 1

        if action is not None:
            self.set_actions(action)

        try:
            state = self.client.step_sim()
        except Exception as exc:
            if self.safe_training_mode:
                logger.error("step_sim failed: %s", exc)
                self._trigger_safe_emergency(f"telemetrie perdue: {exc}")
                if self.states:
                    state = self.states[-1].copy()
                else:
                    state = {
                        "speed": 0.0,
                        "done": 0,
                        "steerAngle": 0.0,
                        "world_position_x": 0.0,
                        "world_position_y": 0.0,
                        "NormalizedSplinePosition": 0.0,
                        "numberOfTyresOut": 0,
                        "currentTime": 0.0,
                        "yaw": 0.0,
                    }
            else:
                raise

        state["timestamp_env"] = time.perf_counter()

        self.state, buf_infos = self.expand_state(state)

        if self.safe_training_mode and not self._safe_emergency_active:
            steer_angle = abs(float(state.get("steerAngle", 0.0)))
            if steer_angle > STEER_HARD_FAIL_DEG:
                self._trigger_safe_emergency(
                    f"steerAngle {steer_angle:.1f} > {STEER_HARD_FAIL_DEG}"
                )
                self.state["done"] = 1
                buf_infos["terminated"] = True

        # add the current absolute actions to the state
        for i in range(self.action_dim):
            self.state[f'current_action_abs_{i:01d}'] = self.current_actions[i]

        # save policy-space actions actually executed (for replay buffer / logs)
        for i in range(self.action_dim):
            self.state[f'actions_{i:01d}'] = self.executed_policy_action[i]

        # create obs
        obs, actions_diff = self.get_obs(state, self.states)

        #
        #   Reward
        #
        self.state["reward"] = self.get_reward(self.state, actions_diff).item()
        if self._last_reward_components:
            self.state.update(self._last_reward_components)
            buf_infos["reward_components"] = dict(self._last_reward_components)

        if (self.ep_steps % 50) == 0:
            logger.debug(f't: {self.ep_steps} speed: {state["speed"]:.2f}, oot: {state["out_of_track"]} '
                         f's: {self.actions[0]:.2f} a: {self.actions[1]:.2f} b: {self.actions[2]:.2f} '
                         f'reward: {state["reward"]:.3f} '
                         f'done: {state["done"]:.0f} LapDist: {state["LapDist"]:.0f} gap: {state["gap"]:.1f} '
                         )

        self.info = buf_infos
        self.ep_reward += self.state["reward"]
        self.states.append(self.state.copy())
        return obs, self.state["reward"], self.state["done"], buf_infos

    def expand_state(self, state):
        state["currentTime"] = state["currentTime"] / 1000.

        # search for the position of the car 2 segments behind and 2 segments ahead
        x = state["world_position_x"]
        y = state["world_position_y"]

        # normalize distance to the racing line multiplied to the track length
        state["LapDist"] = self.track_length * state["NormalizedSplinePosition"]

        self.update_racing_line(x, y)   # get a smaller version to decrease gap computation time

        #
        #   Gap and distance with respect to the middle of the track
        #
        point = np.array([[x, y]])
        gap, rl_point, closest_points, _ = get_gap(point, self.racing_line)

        # closest point in the racing line
        state['rl_point'] = rl_point.item()
        state['gap'] = gap[0]

        state['out_of_track_ac'] = 1.0 if state['numberOfTyresOut'] > 2. else 0.0
        if self.enable_out_of_track_calculation:
            state['out_of_track_calc'] = 0.0 if self.track.track_occupancy_grid.is_inside_grid(point).item() > 0. else 1.0
        else:
            # LapInvalidated stays true for the rest of the lap after a kerb.
            # Using it alone teleports cars that already recovered onto the straight.
            state['out_of_track_calc'] = 1.0 if is_currently_off_track(
                gap[0],
                float(state.get('LapInvalidated') or 0.0) > 0.0,
                self.oot_gap_m,
                self.oot_soft_gap_m,
            ) else 0.0

        if self.oot_mode == 'ac_shared' or (self.use_ac_out_of_track and self.oot_mode != 'local_gap'):
            state['out_of_track'] = state['out_of_track_ac']
        else:
            state['out_of_track'] = state['out_of_track_calc']

        # sensors
        if self.enable_sensors:
            state["sensors"] = self.get_sensors(state)
            state["dist_to_border"] = state["sensors"].min()
            if state['out_of_track']:
                # if out of track the car can have the track exactly behind it.
                # In this case, the sensors will not detect the track. Get the sensors on the other side
                new_state = state.copy()
                new_state["yaw"] += np.pi # get the sensors on the other side
                new_sensors = self.get_sensors(new_state)
                state["dist_to_border"] = min(state["dist_to_border"], new_sensors.min())

        state["going_backwards"] = 0.
        state["_spline_delta_m"] = 0.0
        try:
            frac = float(state["NormalizedSplinePosition"])
            prev = self._prev_spline
            self._prev_spline = frac
            track_len = float(getattr(self, "track_length", 0.0) or 0.0)
            if prev is not None and track_len > 0.0 and 0.0 <= frac <= 1.0 and 0.0 <= prev <= 1.0:
                delta_m = spline_delta_m(prev, frac, track_len)
                state["_spline_delta_m"] = float(delta_m)
                self._wrong_way_meters, going = update_wrong_way(
                    self._wrong_way_meters, delta_m, self.wrong_way_confirm_m,
                )
                if going:
                    state["going_backwards"] = 1.0
        except (TypeError, ValueError, KeyError):
            state["going_backwards"] = 0.

        #
        #   Check episode termination
        #
        # end of episode
        done = 0
        done_reason = ""
        buf_infos = {}
        buf_infos['terminated'] = False  # used by TD MPC
        buf_infos['TimeLimit.truncated'] = False
        # Vitesse réelle (m/s) pour le snapshot du collecteur : le champ était
        # absent de `info` depuis la refonte neuroévolution, donc le collecteur
        # lisait 0.0 en permanence (voitures « immobiles » + détection de blocage
        # faussée).
        buf_infos['speed'] = float(state.get('speed', 0.0))

        # Count WALL-CLOCK driving time. The game's `timestamp_ac` telemetry jumps
        # (dropped packets -> negative/huge deltas, seen as dt min=-116s std=28.8s)
        # and silently stalls the 30 s timer: episodes ran 120 s instead of 30 s.
        # The machine clock is stable AND identical across the 6 workers, so the
        # respawns stay synchronized.
        stamp = time.monotonic()
        previous_stamp = getattr(self, "_termination_stamp", None)
        elapsed = stamp - previous_stamp if previous_stamp is not None else 0.0
        self._termination_stamp = stamp
        elapsed = elapsed if 0.0 < elapsed <= 1.0 else 0.0
        self._driving_elapsed = getattr(self, "_driving_elapsed", 0.0) + elapsed
        elapsed_steps = elapsed * self.ctrl_rate
        in_post_reset_grace = self._driving_elapsed <= float(getattr(self, "oot_grace_sec", 3.0))

        if self._force_episode_done or self._pending_shift_fail:
            logger.info("Terminate episode. Safe training forced stop")
            done = 1
            done_reason = "safe_emergency"
            buf_infos['terminated'] = True

        if state["done"]:
            logger.info("Terminate. Lap ended by Assetto Corsa")
            done = 1
            done_reason = done_reason or "ac_session_end"

        if not in_post_reset_grace and state['going_backwards'] > 0.:
            speed_kmh = float(state['speed']) * 3.6
            if speed_kmh >= self.wrong_way_speed_kmh:
                self._wrong_way_hold_steps += elapsed_steps
            hold_needed = max(1, int(self.wrong_way_timeout_sec * self.ctrl_rate))
            if self._wrong_way_hold_steps >= hold_needed:
                logger.info(
                    "Terminate episode. Going backwards for %.1fs",
                    self.wrong_way_timeout_sec,
                )
                buf_infos['terminated'] = True
                done = 1
                done_reason = done_reason or "going_backwards"
        elif state.get('going_backwards', 0.) <= 0.:
            self._wrong_way_hold_steps = 0

        if self.enable_out_of_track_termination and state['out_of_track'] and not in_post_reset_grace:
            self._oot_hold_steps += elapsed_steps
            hold_needed = max(1, int(self.oot_hold_sec * self.ctrl_rate))
            if self._oot_hold_steps >= hold_needed:
                logger.info("Terminate episode. is_out_of_track")
                logger.info(f"out_of_track. N wheels out: {state['numberOfTyresOut']}. LapDist: {state['LapDist']} x: {x:.2f} y: {y:.2f}")
                buf_infos['terminated'] = True
                done = 1
                done_reason = done_reason or "out_of_track"
        else:
            self._oot_hold_steps = 0

        if self._max_episode_steps is not None:
            # Épisode en TEMPS RÉEL (30 s) plutôt qu'en nombre de pas : le nombre
            # de pas varie avec le FPS du jeu (15-26 Hz), donc un comptage par pas
            # ferait respawner les voitures à des moments différents. En temps
            # réel, les 6 voitures respawnent ensemble toutes les 30 s.
            ep_time_sec = float(getattr(self, "_driving_elapsed", 0.0))
            if ep_time_sec >= float(getattr(self.config, "max_episode_py_time", 30.0)):
                logger.info(
                    "Terminate episode. Max real time %.1fs/%ds",
                    ep_time_sec, float(getattr(self.config, "max_episode_py_time", 30.0)),
                )
                done = 1
                done_reason = done_reason or "max_steps"
                buf_infos['TimeLimit.truncated'] = True

        if self.max_laps_number and self.get_lap_count(self.states) > self.max_laps_number:
            logger.info(f"Terminate episode. Max number of laps reached {self.max_laps_number}")
            done = 1
            done_reason = done_reason or "max_laps"
            buf_infos['TimeLimit.truncated'] = True

        # Episode terminates if the progress of agent is small
        if self.enable_low_speed_termination:
            grace_steps = int(self.low_speed_grace_sec * self.ctrl_rate)
            timeout_steps = int(self.low_speed_timeout_sec * self.ctrl_rate)
            threshold_ms = self.low_speed_threshold_kmh / 3.6
            from ac_workers.drive_truth import pedal_from_state, throttle_commanded_but_unacked

            raw_throttle = 0.0
            if getattr(self, "current_actions", None) is not None and len(self.current_actions) > 1:
                raw_throttle = float(np.clip((self.current_actions[1] + 1.0) / 2.0, 0.0, 1.0))
            unacked = throttle_commanded_but_unacked(raw_throttle, pedal_from_state(state))
            speed_kmh = float(state['speed']) * 3.6
            if speed_kmh >= self.moved_speed_kmh:
                self._episode_has_moved = True
            if self._driving_elapsed <= self.low_speed_grace_sec:
                self.termination_counter = timeout_steps
            elif unacked and not self._episode_has_moved:
                # Pedal never reached this car: teleporting loops forever.
                # Once the car has already driven, a wall-stop must still reset.
                self.termination_counter = timeout_steps
            elif state['speed'] < threshold_ms:
                self.termination_counter -= elapsed_steps
                if self.termination_counter <= 0:
                    logger.info("Race stopped. Speed too low")
                    buf_infos['terminated'] = True
                    done = 1
                    done_reason = done_reason or "low_speed"
            else:
                self.termination_counter = timeout_steps

            pedal_dead = unacked and not self._episode_has_moved
            if not in_post_reset_grace:
                last_xy = self._stuck_last_xy
                if last_xy is None:
                    self._stuck_last_xy = (float(x), float(y))
                    self._stuck_hold_steps = 0
                else:
                    moved = float(np.hypot(float(x) - last_xy[0], float(y) - last_xy[1]))
                    if (
                        not pedal_dead
                        and speed_kmh < self.stuck_speed_kmh
                        and moved < self.stuck_move_eps_m
                    ):
                        self._stuck_hold_steps += elapsed_steps
                        if self._stuck_hold_steps >= max(1, int(self.stuck_timeout_sec * self.ctrl_rate)):
                            logger.info(
                                "Race stopped. Car stuck (speed=%.1f km/h moved=%.2fm)",
                                speed_kmh,
                                moved,
                            )
                            buf_infos['terminated'] = True
                            done = 1
                            done_reason = done_reason or "stuck"
                    else:
                        self._stuck_hold_steps = 0
                        self._stuck_last_xy = (float(x), float(y))
                if not pedal_dead:
                    self._no_progress_m += max(0.0, float(state.get("_spline_delta_m") or 0.0))
                    if self._no_progress_m >= self.no_progress_credit_m:
                        self._no_progress_m = 0.0
                        self._no_progress_hold_steps = 0
                    else:
                        self._no_progress_hold_steps += elapsed_steps
                        if self._no_progress_hold_steps >= max(
                            1, int(self.no_progress_timeout_sec * self.ctrl_rate)
                        ):
                            logger.info(
                                "Race stopped. No track progress for %.1fs",
                                self.no_progress_timeout_sec,
                            )
                            buf_infos['terminated'] = True
                            done = 1
                            done_reason = done_reason or "stuck"
                else:
                    self._no_progress_hold_steps = 0
                    self._no_progress_m = 0.0
            else:
                self._stuck_hold_steps = 0
                self._stuck_last_xy = (float(x), float(y))
                self._no_progress_hold_steps = 0
                self._no_progress_m = 0.0

        # check gap
        if not in_post_reset_grace and self.max_gap and np.abs(gap) > self.max_gap:
            logger.info(f"Race stopped. Gap too big ({gap})")
            done = 1
            done_reason = done_reason or "max_gap"

        # extra channels in the info variable
        if self.use_obs_extra:
            buf_infos['obs_extra'] = self.get_extra_observations(state)
        buf_infos['done_reason'] = done_reason
        buf_infos['episode_steps'] = self.ep_steps
        if done:
            # used by SB3 save final observation where user can get it, then reset
            #buf_infos['terminal_observation'] = obs.copy()
            #obs = self.reset()  # only needed when using vecEnv

            if self.recover_car_on_done:
                self.recover_car()

        state.update(buf_infos)
        state["done"] = done
        return state, buf_infos

    def get_extra_observations(self, state):
        return np.array([state[channel] for channel in self.obs_extra_enabled_channels], dtype=np.float32)

    def get_extra_obs_index(self, channel_name):
        return self.obs_extra_enabled_channels.index(channel_name)

    def get_reward(self, state, actions_diff):
        if getattr(self.config, 'reward_mode', '') == 'signed_progress_v1':
            _apply_reward_override(self.config)
            components = progress_reward(
                getattr(self, '_progress_previous_state', None), state,
                float(self.track_length), self.config, actions_diff,
                tracker=self._clean_lap_tracker)
            self._progress_previous_state = dict(state)
            self._last_reward_components = components
            if components.get("completed_clean_sector"):
                logger.info("Clean sector bonus %.1f", components.get("reward_clean_sector", 0.0))
            if components.get("completed_clean_lap"):
                logger.info(
                    "Clean lap bonus %.1f (lap_time=%.1fs)",
                    components.get("reward_clean_lap", 0.0),
                    components.get("clean_lap_time_sec", 0.0),
                )
            return np.array([components['reward']])
        speed = 3.6 * np.array(state['speed'])

        r_progress = speed
        if self.use_reference_line_in_reward:
            abs_gap = float(np.abs(state["gap"]))
            if self.reward_gap_factor_sigma > 0.0:
                # Smooth Lorentzian: never zero, always some progress signal.
                gap_factor = 1.0 / (1.0 + (abs_gap / self.reward_gap_factor_sigma) ** 2)
            else:
                gap_factor = max(0.0, 1.0 - abs_gap / 12.0)
            r_progress = r_progress * gap_factor
        r_progress = r_progress / 300.0

        gap_penalty = 0.0
        steer_penalty = 0.0
        oot_penalty = 0.0
        idle_penalty = 0.0
        brake_penalty = 0.0
        wrong_way_penalty = 0.0
        centerline_bonus = 0.0
        correct_steer_bonus = 0.0

        if self.reward_v3:
            gap_f = float(state["gap"])
            abs_gap_f = abs(gap_f)
            gap_penalty = min(
                self.reward_gap_penalty_coef * abs_gap_f,
                self.reward_gap_penalty_cap,
            )
            steer_angle = float(state.get("steerAngle", 0.0))
            steer_abs = abs(steer_angle)
            steer_ref_deg = self.steer_limit_deg if self.steer_control_v3 else self.safe_steer_deg
            steer_penalty = self.reward_steer_penalty_coef * (steer_abs / steer_ref_deg) ** 2
            if state.get("done") and state.get("out_of_track"):
                oot_penalty = self.reward_oot_terminal_penalty
            speed_kmh = float(np.asarray(speed).reshape(-1)[0])
            if self.reward_idle_penalty > 0.0:
                if speed_kmh < self.low_speed_threshold_kmh:
                    idle_penalty = self.reward_idle_penalty
            if self.reward_brake_penalty_coef > 0.0:
                brake = float(state.get("brakeStatus", 0.0) or 0.0)
                if brake > 0.0:
                    scale = 1.0 if speed_kmh < self.reward_brake_penalty_speed_kmh else 0.25
                    brake_penalty = self.reward_brake_penalty_coef * (brake ** 2) * scale
            # Same-sign gap×steer = turning further off the line.
            if self.reward_wrong_way_steer_coef > 0.0 and gap_f * steer_angle > 0.0:
                wrong_way_penalty = (
                    self.reward_wrong_way_steer_coef
                    * min(abs_gap_f, self.reward_gap_penalty_cap * 20.0)
                    * min(steer_abs / max(steer_ref_deg, 1e-3), 1.0)
                )
            # Positive reward for being near the racing line (carrot).
            if self.reward_centerline_bonus_coef > 0.0:
                centerline_bonus = self.reward_centerline_bonus_coef * max(
                    0.0, 1.0 - abs_gap_f / self.reward_centerline_bonus_width
                )
            # Positive reward for steering TOWARD the line (opposite-sign gap×steer).
            if self.reward_correct_steer_bonus_coef > 0.0 and gap_f * steer_angle < 0.0:
                correct_steer_bonus = (
                    self.reward_correct_steer_bonus_coef
                    * min(abs_gap_f / 12.0, 1.0)
                    * min(steer_abs / max(steer_ref_deg, 1e-3), 1.0)
                )

        r = (
            r_progress
            + centerline_bonus
            + correct_steer_bonus
            - gap_penalty
            - steer_penalty
            - oot_penalty
            - idle_penalty
            - brake_penalty
            - wrong_way_penalty
        )

        if self.penalize_actions_diff:
            action_difference_penalty = np.linalg.norm(actions_diff, ord=2)
            r -= action_difference_penalty * self.penalize_actions_diff_coef

        self._last_reward_components = {
            "reward_progress": float(np.asarray(r_progress).reshape(-1)[0]),
            "reward_centerline_bonus": float(centerline_bonus),
            "reward_correct_steer_bonus": float(correct_steer_bonus),
            "reward_gap_penalty": float(gap_penalty),
            "reward_steer_penalty": float(steer_penalty),
            "reward_oot_penalty": float(oot_penalty),
            "reward_idle_penalty": float(idle_penalty),
            "reward_brake_penalty": float(brake_penalty),
            "reward_wrong_way_steer_penalty": float(wrong_way_penalty),
            "reward": float(np.asarray(r).reshape(-1)[0]),
        }

        r = np.asarray(r).reshape(-1)
        return r

    def recover_car(self):
        logger.info("Recover car")
        self.client.controls.set_defaults()
        self.client.respond_to_server()
        self.client.simulation_management.send_reset()

    def reset(self, seed=None, verbose=False):
        self.end_of_episode_stats()
        self._progress_previous_state = None
        self.stats_saved = False

        self.n_episodes += 1
        logger.info(f"Reset AC. Episode {self.n_episodes} total_steps: {self.total_steps}")

        if self.n_episodes == 1:
            # get static info only once
            self.static_info = self.client.simulation_management.get_static_info()
            self.track_length = self.static_info["TrackLength"]
            self.ac_mod_config = self.client.simulation_management.get_config()
            if verbose:
                logger.info("Static info:")
                for i in self.static_info:
                    logger.info(f"{i}: {self.static_info[i]}")
                logger.info("AC Mod config:")
                for i in self.ac_mod_config:
                    logger.info(f"{i}: {self.ac_mod_config[i]}")

            if self.config.screen_capture_enable:
                assert self.config.final_image_height == self.ac_mod_config["final_image_height"],\
                    "Config and AC Mod config do not match for final_image_height. Got {} and {}".format(self.config.final_image_height,
                                                                                                        self.ac_mod_config["final_image_height"])
                assert self.config.final_image_width == self.ac_mod_config["final_image_width"],\
                    "Config and AC Mod config do not match for final_image_width. Got {} and {}".format(self.config.final_image_width,
                                                                                                        self.ac_mod_config["final_image_width"])
                assert self.config.color_mode == self.ac_mod_config["color_mode"], \
                    "Config and AC Mod config do not match for color_mode. Got {} and {}".format(self.config.color_mode,
                                                                                                  self.ac_mod_config["color_mode"])

            assert self.config.ego_sampling_freq == self.ac_mod_config["ego_sampling_freq"], "Ego sampling frequency mismatch"
            assert self.static_info["TrackFullName"] == self.track_name, f"Track name mismatch. Running: {self.static_info['TrackFullName']} Configured: {self.track_name}"
            assert self.static_info["CarName"] == self.car_name, f"Track name mismatch. Running: {self.static_info['CarName']} Configured: {self.car_name}"

        self.client.reset(self.send_reset_at_start)

        self._safe_emergency_active = False
        self._safe_recover_used = False
        self._force_episode_done = False
        self._pending_shift_fail = False

        self.termination_counter = int(self.low_speed_timeout_sec * self.ctrl_rate)
        self._reset_termination_trackers()
        self.episode_saved = False
        self.is_out_of_track = False
        self.current_actions = np.array( [0.0, -1.0, -1.0] )
        self.start_actions = np.array( [0.0, -1.0, -1.0] )

        self.ep_steps = 0  # reset steps after flushing the actions

        # flush a few steps. Otherwise the history state will be incomplete
        for _ in range(2):
            obs, _, _, _ = self.step(self.start_actions)

        self.states = []
        obs, _, _, info = self.step(self.start_actions)
        self.info = info

        if self.auto_shift_to_first:
            if not auto_shift_neutral_to_first(self):
                self._pending_shift_fail = True
                self._force_episode_done = True
                logger.error("Reset: N->1ere impossible, episode will terminate cleanly")
            obs, _, _, info = self.step(self.start_actions)
            self.info = info

        self.ep_reward = 0.
        self.ep_steps = 0  # reset steps after flushing the actions
        self._reset_termination_trackers()
        return obs

    def get_info(self):
        return self.info.copy()

    def close(self):
        return self.end()

    def end(self):
        ep_stats = self.end_of_episode_stats()
        self.client.close()
        return ep_stats

    def get_current_image(self):
        return self.client.get_current_image()

    def rand_act(self):
        return torch.from_numpy(self.action_space.sample().astype(np.float32))

    def get_obs(self, state, history=None):
        """Observation réduite : 3 télémétries + 5 rayons LiDAR = 8 valeurs."""
        obs = np.array([state[ch] for ch in self.obs_enabled_channels], dtype=np.float32)
        obs = obs / self.obs_channels_scales

        if self.enable_sensors:
            rays = self.lidar.cast(
                state["world_position_x"], state["world_position_y"], state["yaw"]
            )
            obs = np.hstack([obs, rays])

        return obs, np.zeros(self.action_dim, dtype=np.float32)

    def render(self):
        pass

    def seed(self, seed=None):
        pass

    def get_task_id(self):
        return self.current_task_id

    def get_sensors(self, state, do_plots=False):
        return self.lidar.cast(
            state["world_position_x"], state["world_position_y"], state["yaw"]
        )

    def show_debug_state(self, state, do_plots=True):
        if self.enable_sensors:
            sensors = self.get_sensors(state, do_plots)
            logger.info(f"sensors: {sensors}")
        logger.info(f"gap: {state['gap']}")
        logger.info(f"VehicleSpeed: {state['speed']}")
        logger.info(f"reward: {state['reward']}")

    def end_of_episode_stats(self, verbose=True):
        save_csv = False

        if len(self.states) and self.stats_saved is False:
            ep = pd.DataFrame(self.states)
            if self.save_observations:
                timestamp = f"{self.laps_path}/{get_date_timestemp()}"
                df_states = pd.DataFrame(self.states)
                states_save_path = f"{timestamp}_states.parquet"
                df_states.to_parquet(states_save_path, engine="pyarrow", index=False)
                logger.info(f"Saved raw data to: {states_save_path}")

                if len(self.episodes_stats) == 0:
                    with open(f"{self.laps_path}/{'static_info.json'}", "w") as f:
                        json.dump(self.static_info, f, indent=4)  # Use indent=4 for readability

                if save_csv:
                    save_csv_channels = ['steps', 'currentTime', 'done',
                                         'speed', 'reward', 'gap',
                                         'world_position_y',
                                         'world_position_x',
                                         'RPM',
                                         'steerAngle', 'brakeStatus', 'accStatus', 'actualGear',
                                         'packetId', 'velocity_x', 'velocity_y', 'velocity_z',
                                         'yaw', 'roll', 'angular_velocity_y', 'angular_velocity_x',
                                         'LapCount',  'LapDist', 'going_backwards',
                                         'current_action_abs_0', 'current_action_abs_1', 'current_action_abs_2',
                                         'actions_0', 'actions_1', 'actions_2', 'rl_point', 'out_of_track',
                                         ]
                    ep[save_csv_channels].to_csv(f"{timestamp}_raw_data.csv", index=False)

            # calculate steps lost in the communication with the server
            differences = np.diff(ep.steps.values)
            number_packages_lost = np.sum(differences[differences > 1])
            gap_abs_max = np.abs(ep.gap).max()
            r = { "ep_count": self.n_episodes,
                  "ep_steps":len(ep),
                  "total_steps": self.total_steps,
                  "packages_lost": number_packages_lost,
                  "ep_reward": ep.reward.sum(),
                  "speed_mean": ep.speed.mean(),
                  "speed_max": ep.speed.max(),
                  #"completedLaps": ep.completedLaps.values[-1],
                  "BestLap": ep['BestLap'].values[-1] / 1000.,
                  "terminated": ep.terminated.values[-1]
            }
            for i, lapCount in enumerate(list(set( ep.LapCount ))):
                r[f"LapNo_{i}"] = ep[ep.LapCount == lapCount]["iLastTime"].values[-1] / 1000 # to seconds
            #  BestLap from the dictionary, excluding lap times that are 0 (incomplete laps)
            r["ep_bestLapTime"] = min((time for key, time in r.items() if key.startswith("LapNo_") and time > 0), default=0)
            if verbose:
                logger.info(f"total_steps: {self.total_steps} ep_steps: {self.ep_steps} ep_reward: {r['ep_reward']:6.1f} "
                            f"LapDist: {self.state['LapDist']:6.2f} packages lost {number_packages_lost} BestLap: {r['BestLap']}")
                for i, lapCount in enumerate(list(set( ep.LapCount ))):
                    logger.info(f"LapNo_{i}: {r[f'LapNo_{i}']:6.2f}")
                logger.info(f"ep_bestLapTime: {r['ep_bestLapTime']:6.2f}")
                logger.info(f"speed_mean: {r['speed_mean']:6.2f} speed_max: {r['speed_max']:6.2f} max_abs_gap: {gap_abs_max:6.2f} ep_laps: {len(set(ep.LapCount))}")
                if len(ep) > 10:
                    dt = np.diff( ep.currentTime.values )[1:]
                    logger.info(f"dt avr: {dt.mean():.3f} std: {dt.std():.3f} min: {dt.min():.3f} max: {dt.max():.3f}")
            if number_packages_lost:
                logger.warning(f"Packages lost in the communication with the server. {number_packages_lost} packages lost")

            self.episodes_stats.append(r.copy())
            pd.DataFrame(self.episodes_stats).to_csv(self.output_path + os.sep + "episodes_stats.csv", index=False)
            self.stats_saved = True
            return r
        else:
            return {}

    def get_lap_count(self, states):
        lap_count = sorted( list(set(i["LapCount"] for i in states)) )
        return len(lap_count)

    def get_history(self):
        states = self.states
        return pd.DataFrame(states)

    def load_history(self, file_path):
        """Loads trajectory data, inferring format from file extension."""
        file_path = Path(file_path)
        if file_path.suffix == ".parquet":
            trajectory = pd.read_parquet(file_path, engine="pyarrow").to_dict(orient="records")

            # Load static info from JSON
            static_info_path = file_path.parent / "static_info.json"
            assert static_info_path.exists(), f"Static info file not found: {static_info_path}"
            with open(static_info_path, "r") as f:
                static_info = json.load(f)
        elif file_path.suffix == ".pkl":
            with open(file_path, "rb") as f:
                data = pickle.load(f)
                trajectory = data["states"]
                static_info = data["static_info"]
        else:
            raise ValueError(f"Unsupported file format: {file_path.suffix}")

        return trajectory, static_info

    def set_track(self, track_name):
        logger.info(f"Setting track {track_name}")
        self.track_config = self.tracks_config[track_name]
        self.track_length = self.track_config["TrackLength"]
        self.track_file = os.path.join(self.tracks_path, self.track_config["track_file"])
        self.ref_lap_file = os.path.join(self.tracks_path, self.track_config["ref_lap_file"])
        self.track_grid_file = os.path.join(self.tracks_path, self.track_config["track_grid_file"])

    def set_eval_mode(self):
        self.max_laps_number = self.config.eval_number_of_laps
