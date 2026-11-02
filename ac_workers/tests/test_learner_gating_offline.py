"""Offline tests for learner-active gating of collector policy driving.

Verifies that workers only drive when the SAC learner is alive (recent
learner_ping on the transition server) and brake to a safe stop otherwise,
without being killed or reset.
"""
from __future__ import annotations

import sys
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))
sys.path.insert(0, str(REPO / "algorithm" / "discor"))


class _FakeClient:
    def __init__(self, last_seen: float):
        self._last_seen = last_seen

    def stats(self):
        return {"learner_last_seen": self._last_seen}


def _runtime():
    from ac_workers.worker_collector import CollectorRuntime

    rt = CollectorRuntime(worker_id=0, vjoy_device=1, mgmt_port=2347)
    return rt


def test_learner_running_allows_policy():
    from ac_workers import worker_collector as wc

    rt = _runtime()
    rt._transition_client = _FakeClient(last_seen=time.time())
    active = rt.refresh_learner_state(force=True)
    assert active is True
    assert rt._learner_active is True
    assert rt.policy_block_reason() == ""
    assert rt._learner_heartbeat_age_ms <= wc.LEARNER_HEARTBEAT_TIMEOUT_MS


def test_learner_not_running_blocks_policy_with_safe_stop():
    rt = _runtime()
    # No learner ever pinged (last_seen=0) -> not active -> safe stop.
    rt._transition_client = _FakeClient(last_seen=0.0)
    rt.refresh_learner_state(force=True)
    assert rt._learner_active is False
    assert rt.policy_block_reason() == "learner_not_running"


def test_stale_heartbeat_blocks_policy():
    from ac_workers import worker_collector as wc

    rt = _runtime()
    stale = time.time() - (wc.LEARNER_HEARTBEAT_TIMEOUT_MS / 1000.0) - 2.0
    rt._transition_client = _FakeClient(last_seen=stale)
    rt.refresh_learner_state(force=True)
    assert rt._learner_active is False
    assert rt.policy_block_reason() == "learner_not_running"


def test_learner_recovery_resumes_policy():
    rt = _runtime()
    rt._transition_client = _FakeClient(last_seen=0.0)
    rt.refresh_learner_state(force=True)
    assert rt.policy_block_reason() == "learner_not_running"
    # Learner comes back: fresh ping -> policy resumes.
    rt._transition_client = _FakeClient(last_seen=time.time())
    rt.refresh_learner_state(force=True)
    assert rt._learner_active is True
    assert rt.policy_block_reason() == ""


def test_manual_pause_takes_priority():
    rt = _runtime()
    rt._transition_client = _FakeClient(last_seen=time.time())
    rt.refresh_learner_state(force=True)
    rt._policy_paused = True
    assert rt.policy_block_reason() == "manual_pause"


def test_snapshot_exposes_gating_fields():
    rt = _runtime()
    rt._transition_client = _FakeClient(last_seen=0.0)
    rt.refresh_learner_state(force=True)
    snap = rt.minimal_snapshot()
    assert snap["learner_required"] is True
    assert snap["learner_active"] is False
    assert snap["policy_paused"] is True
    assert snap["policy_paused_reason"] == "learner_not_running"
    assert "learner_heartbeat_age_ms" in snap


def test_refresh_is_throttled():
    rt = _runtime()
    rt._transition_client = _FakeClient(last_seen=time.time())
    rt.refresh_learner_state(force=True)
    assert rt._learner_active is True
    # Swap to a dead learner but do NOT force: throttled call keeps cached value.
    rt._transition_client = _FakeClient(last_seen=0.0)
    rt.refresh_learner_state(force=False)
    assert rt._learner_active is True  # cached, not yet refreshed


def test_safe_stop_never_forces_brake():
    """A blocked worker (no learner / paused) coasts at NEUTRAL, never brakes.

    The old forced-full-brake safe-stop constants are removed entirely: nothing
    in the worker may command a 100% brake on its own.
    """
    from ac_workers import worker_collector as wc

    assert not hasattr(wc, "LEARNER_STOP_RAW_ACTION")
    assert not hasattr(wc, "LEARNER_STOP_PHYSICAL_ACTION")


def test_poll_collector_telemetry_updates_age():
    from ac_workers.worker_collector import CollectorRuntime, poll_collector_telemetry

    class _FakeClient:
        state = {"steer": 0.1, "gas": 0.0, "brake": 0.5, "speed": 12.0}

        def get_servers_input(self):
            return None

        def respond_to_server(self):
            return None

    class _FakeEnv:
        client = _FakeClient()

    rt = CollectorRuntime(worker_id=0, vjoy_device=1, mgmt_port=2347)
    rt.last_telemetry_ts = time.time() - 10.0
    assert rt.telemetry_age_ms() > 2000.0
    ok = poll_collector_telemetry(_FakeEnv(), rt)
    assert ok is True
    assert rt.telemetry_age_ms() < 500.0
    assert rt.telemetry_snapshot["speed"] == 12.0


def test_safety_hold_requests_reset_after_timeout():
    from ac_workers.collector_safety import STALE_STUCK_RESET_SEC, should_reset_after_safety_hold
    from ac_workers.worker_collector import CollectorRuntime

    rt = CollectorRuntime(worker_id=0, vjoy_device=1, mgmt_port=2347)
    assert should_reset_after_safety_hold(rt, "telemetry_stale", now=10.0) is False
    assert should_reset_after_safety_hold(rt, "telemetry_stale", now=10.0 + STALE_STUCK_RESET_SEC - 0.1) is False
    assert should_reset_after_safety_hold(rt, "telemetry_stale", now=10.0 + STALE_STUCK_RESET_SEC) is True
    assert should_reset_after_safety_hold(rt, "learner_stale", now=100.0) is False
    assert rt._safety_hold_since == 0.0


if __name__ == "__main__":
    tests = [
        test_learner_running_allows_policy,
        test_learner_not_running_blocks_policy_with_safe_stop,
        test_stale_heartbeat_blocks_policy,
        test_learner_recovery_resumes_policy,
        test_manual_pause_takes_priority,
        test_snapshot_exposes_gating_fields,
        test_refresh_is_throttled,
        test_safe_stop_never_forces_brake,
        test_poll_collector_telemetry_updates_age,
        test_safety_hold_requests_reset_after_timeout,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed out of {len(tests)} tests")
    sys.exit(1 if failed else 0)
