from types import SimpleNamespace
from unittest.mock import patch
import numpy as np


def test_learner_session_independent_of_worker_launch():
    from ac_workers.control_center import is_learner_really_active
    with patch("ac_workers.control_center.pid_alive", return_value=True), patch("ac_workers.control_center.is_learner_process", return_value=True), patch("ac_workers.control_center.learner_heartbeat_age_sec", return_value=0):
        assert is_learner_really_active({"learner_pid": 123, "session_id": "learner", "learner_running": True}, "worker")
        assert not is_learner_really_active({"learner_pid": 123, "learner_running": False}, "worker")


def test_reset_duplicates_preserve_fresh_sample_streak():
    from ac_workers.collector_live_attach import wait_for_consistent_reset_obs
    class Env:
        def __init__(self):
            self.stamps = iter([1, 2, 2, 3])
            self.client = SimpleNamespace(discard_pending_telemetry=lambda: None)
        def step(self, action):
            self.state = {"timestamp_ac": next(self.stamps)}
            return np.zeros(3), 0, False, {}
    assert wait_for_consistent_reset_obs(Env())[0]


def test_reset_connection_failure_is_bounded_and_reported():
    from ac_workers.worker_collector import _complete_episode_reset
    from unittest.mock import Mock
    env = SimpleNamespace(state={}, reset=Mock(side_effect=ConnectionRefusedError("offline")))
    runtime = SimpleNamespace(last_reset_reason="stuck", reset_fail_streak=0, set_phase=Mock())
    with patch("ac_workers.collector_live_attach.neutralize_env_controls"), patch("ac_workers.worker_collector.time.sleep"):
        assert _complete_episode_reset(env, runtime, 2) == (False, None)
    assert env.reset.call_count > 1
    assert runtime.set_phase.call_args.args[0] == "reset_failed"
    assert "reset_connection_failed" in runtime.last_reset_reason


def test_post_reset_timeout_is_retried():
    from ac_workers.worker_collector import _complete_episode_reset
    from unittest.mock import Mock
    env = SimpleNamespace(state={}, reset=Mock(return_value=None))
    runtime = SimpleNamespace(last_reset_reason="stuck", reset_fail_streak=0, phase="running", set_phase=Mock())
    with patch("ac_workers.collector_live_attach.neutralize_env_controls"), patch("ac_workers.worker_collector.time.sleep"), patch("ac_workers.collector_live_attach.run_episode_reset", side_effect=[TimeoutError("offline"), (True, "", [1])]) as reset:
        assert _complete_episode_reset(env, runtime, 2) == (True, [1])
    assert reset.call_count == 2


def test_management_closes_silent_client_and_accepts_next():
    import ast
    import socket
    from pathlib import Path
    from unittest.mock import Mock
    source = Path("assetto_corsa_gym/AssettoCorsaPlugin/plugins/sensors_par/sensors_par.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "simulation_management_server_task")
    first, second, server = Mock(), Mock(), Mock()
    first.recv.side_effect = socket.timeout()
    second.recv.return_value = b""
    class StopServer(BaseException):
        pass
    server.accept.side_effect = [(first, None), (second, None), StopServer()]
    # Stop at the third accept; the outer handler logs and returns.
    namespace = {"socket": SimpleNamespace(socket=Mock(return_value=server), AF_INET=socket.AF_INET, SOCK_STREAM=socket.SOCK_STREAM, timeout=socket.timeout), "config": SimpleNamespace(simulation_management_server_host_name="localhost", simulation_management_server_port=1), "logger": Mock()}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "server", "exec"), namespace)
    import pytest
    namespace["logger"].exception.side_effect = [None, StopServer(), StopServer()]
    with pytest.raises(StopServer):
        namespace[fn.name]()
    first.settimeout.assert_called_once_with(2.0)
    first.close.assert_called_once()
    second.close.assert_called_once()


def test_collector_does_not_force_first_gear():
    import ast
    from pathlib import Path
    tree = ast.parse(Path("ac_workers/worker_collector.py").read_text(encoding="utf-8"))
    assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "verify_first_before_driving" for n in ast.walk(tree))
