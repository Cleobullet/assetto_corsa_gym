"""Regression: incompatible policy must end the sequential startup wait."""
import sys
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ac_workers.control_center import (ControlCenter, startup_policy_error,
    WORKER_OP_STARTING, WORKER_OP_FAILED)


def test_incompatible_idle_stops_queue():
    c = ControlCenter.__new__(ControlCenter)
    c._worker_op_state = {0: WORKER_OP_STARTING}
    c._worker_op_errors = {}
    c._worker_op_started_at = {0: 1.0}
    c._scale_queue = [1, 2, 3, 4, 5]
    c._scale_boot_paused = False
    c._status_for_op = Mock(return_value={"phase": "idle", "policy_ready": False,
        "policy_load_error": "reward_schema_mismatch_start_new_training"})
    c._cancel_worker_bg_proc = Mock()
    c._reconcile_ac_closed_during_startup = Mock(side_effect=AssertionError("must fail before waiting on AC"))
    assert c._reconcile_starting_ops()
    assert c._worker_op_state[0] == WORKER_OP_FAILED
    assert not c._scale_queue and c._scale_boot_paused
    assert "train new" in c._worker_op_errors[0]
    assert "reward_schema_mismatch" in c._message


def test_normal_idle_and_transient_reload_do_not_fail():
    assert not startup_policy_error({"phase": "idle", "policy_ready": True})
    assert not startup_policy_error({"policy_ready": False, "policy_load_error": "missing_metadata"})
    assert not startup_policy_error({"policy_ready": True,
        "policy_load_error": "reward_schema_mismatch_start_new_training"})


if __name__ == "__main__":
    test_incompatible_idle_stops_queue()
    test_normal_idle_and_transient_reload_do_not_fail()
    print("2 startup policy regression tests passed")


def test_stop_all_cancels_queue_before_launching_stop_job():
    c = ControlCenter.__new__(ControlCenter)
    c._bg_procs = {}
    c._scale_queue = [1, 2, 3]
    c._scale_targets = [0, 1, 2, 3]
    c._scale_boot_paused = False
    c._worker_op_state = {0: WORKER_OP_STARTING}
    c._cancel_worker_bg_proc = Mock()
    def spawn(*args, **kwargs):
        assert not c._scale_queue
        assert c._scale_boot_paused
        assert not c._scale_targets
    c._spawn_ps_job = spawn
    assert c.stop_all_workers() == "stop-all: stopping..."


def test_scale_rejects_launch_while_stop_job_pending():
    c = ControlCenter.__new__(ControlCenter)
    c._bg_procs = {"stop-all": object()}
    assert "stop-all en cours" in c.start_workers_sequential([0, 1])
