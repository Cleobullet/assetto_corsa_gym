"""Offline tests for the async actor-learner mode.

Spins a real TransitionServer on a dedicated port and exercises the per-worker
action request/response RPC: routing by (worker_id, request_id), independence
between workers, timeouts, and control_mode switching.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))
sys.path.insert(0, str(REPO / "algorithm" / "discor"))

from common.transition_server import TransitionServer, TransitionClient

PORT = 24561


def _server():
    srv = TransitionServer(port=PORT)
    srv.start()
    time.sleep(0.2)
    return srv


def test_action_request_routes_back_to_requesting_worker():
    srv = _server()
    try:
        worker = TransitionClient(port=PORT)
        learner = TransitionClient(port=PORT)
        result: dict = {}

        def _ask():
            result["resp"] = worker.request_action(0, [0.1] * 4, "r1", wait_ms=3000)

        t = threading.Thread(target=_ask)
        t.start()
        # Act as the learner: pull the pending request and answer worker 0.
        deadline = time.time() + 3.0
        served = False
        while time.time() < deadline and not served:
            reqs = learner.pull_action_requests()
            for req in reqs:
                assert req["worker_id"] == 0
                assert req["request_id"] == "r1"
                learner.put_action_responses(
                    [{"worker_id": 0, "request_id": "r1", "action": [0.5, -0.5, 0.0], "policy_version": 7}]
                )
                served = True
            time.sleep(0.01)
        t.join(timeout=3.0)
        resp = result["resp"]
        assert resp["ok"] is True
        assert resp["ready"] is True
        assert resp["action"] == [0.5, -0.5, 0.0]
        assert resp["policy_version"] == 7
    finally:
        srv.stop()


def test_slow_worker_does_not_block_other_worker():
    srv = _server()
    try:
        w0 = TransitionClient(port=PORT)
        w1 = TransitionClient(port=PORT)
        learner = TransitionClient(port=PORT)
        results: dict = {}

        def _ask(client, wid, rid, wait):
            results[wid] = client.request_action(wid, [0.0] * 4, rid, wait_ms=wait)

        # Worker 1 will get no answer (acts slow/late). Worker 0 gets answered fast.
        t1 = threading.Thread(target=_ask, args=(w1, 1, "w1r", 400))
        t0 = threading.Thread(target=_ask, args=(w0, 0, "w0r", 3000))
        t1.start()
        t0.start()
        deadline = time.time() + 3.0
        answered0 = False
        while time.time() < deadline and not answered0:
            for req in learner.pull_action_requests():
                if req["worker_id"] == 0:
                    learner.put_action_responses(
                        [{"worker_id": 0, "request_id": req["request_id"], "action": [0.1, 0.2, 0.3], "policy_version": 1}]
                    )
                    answered0 = True
            time.sleep(0.01)
        t0.join(timeout=3.0)
        t1.join(timeout=3.0)
        # Worker 0 succeeded independently; worker 1 timed out (ready False).
        assert results[0]["ready"] is True
        assert results[0]["action"] == [0.1, 0.2, 0.3]
        assert results[1]["ready"] is False
    finally:
        srv.stop()


def test_mismatched_request_id_does_not_satisfy():
    srv = _server()
    try:
        worker = TransitionClient(port=PORT)
        learner = TransitionClient(port=PORT)
        # Stale response for a different request_id must not satisfy the new one.
        learner.put_action_responses(
            [{"worker_id": 0, "request_id": "OLD", "action": [9.0, 9.0, 9.0], "policy_version": 0}]
        )
        resp = worker.request_action(0, [0.0] * 4, "NEW", wait_ms=300)
        assert resp["ready"] is False
    finally:
        srv.stop()


def test_timeout_increments_actor_timeouts_total():
    srv = _server()
    try:
        worker = TransitionClient(port=PORT)
        stats0 = worker.stats()
        worker.request_action(3, [0.0] * 4, "t1", wait_ms=150)
        stats1 = worker.stats()
        assert int(stats1["actor_timeouts_total"]) == int(stats0["actor_timeouts_total"]) + 1
        assert int(stats1["actor_requests_total"]) >= int(stats0["actor_requests_total"]) + 1
    finally:
        srv.stop()


def test_control_mode_set_and_reported():
    srv = _server()
    try:
        client = TransitionClient(port=PORT)
        assert client.stats()["control_mode"] == "async_local_actor"
        client.set_control_mode("async_actor")
        assert client.stats()["control_mode"] == "async_actor"
        client.set_control_mode("async_local_actor")
        assert client.stats()["control_mode"] == "async_local_actor"
    finally:
        srv.stop()


def test_trainer_serves_action_with_sac_policy():
    """End-to-end: trainer._serve_action_requests answers a worker request with a
    real SAC action and the worker receives action_source=learner_rpc."""
    import tempfile
    from ac_workers.multi_sac_trainer import MultiSacTrainer

    config = REPO / "config_first_sac_cpu_v3.yml"
    if not config.is_file():
        print("  SKIP  test_trainer_serves_action_with_sac_policy (no config)")
        return
    srv = _server()
    try:
        worker = TransitionClient(port=PORT)
        learner = TransitionClient(port=PORT)
        with tempfile.TemporaryDirectory() as td:
            trainer = MultiSacTrainer(config, Path(td), continuous=True)
            result: dict = {}

            def _ask():
                result["resp"] = worker.request_action(2, [0.0] * 125, "rpc1", wait_ms=4000)

            t = threading.Thread(target=_ask)
            t.start()
            deadline = time.time() + 4.0
            served = 0
            while time.time() < deadline and served == 0:
                served = trainer._serve_action_requests(learner)
                time.sleep(0.01)
            t.join(timeout=4.0)
            resp = result["resp"]
            assert resp["ready"] is True
            assert resp["action_source"] == "learner_rpc"
            assert len(resp["action"]) == 3
    finally:
        srv.stop()


if __name__ == "__main__":
    tests = [
        test_action_request_routes_back_to_requesting_worker,
        test_slow_worker_does_not_block_other_worker,
        test_mismatched_request_id_does_not_satisfy,
        test_timeout_increments_actor_timeouts_total,
        test_control_mode_set_and_reported,
        test_trainer_serves_action_with_sac_policy,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            import traceback

            print(f"  FAIL  {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed out of {len(tests)} tests")
    sys.exit(1 if failed else 0)
