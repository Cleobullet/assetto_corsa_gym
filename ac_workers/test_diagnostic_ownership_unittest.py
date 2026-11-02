import unittest
from unittest.mock import patch, Mock
from ac_workers.control_center import ControlCenter, live_diagnostic, _BgResult, WORKER_OP_OFFLINE

class DiagnosticOwnershipTests(unittest.TestCase):
    def test_alive_required(self):
        status={'collector_pid':123,'control_smoke_test':{'enabled':True},'phase':'resetting'}
        with patch('ac_workers.control_center.pid_alive',return_value=True):
            self.assertTrue(live_diagnostic(status))
            self.assertFalse(live_diagnostic({'collector_pid':123}))
        with patch('ac_workers.control_center.pid_alive',return_value=False):
            self.assertFalse(live_diagnostic(status))
    def test_recovery_does_not_kill_diagnostic(self):
        center=ControlCenter.__new__(ControlCenter)
        center._worker_status=Mock(return_value={'collector_pid':123,'control_smoke_test':{'enabled':True}})
        center._force_stop_worker_processes=Mock()
        with patch('ac_workers.control_center.pid_alive',return_value=True):
            self.assertFalse(center._attempt_startup_recovery(0,'telemetry stale'))
        center._force_stop_worker_processes.assert_not_called()
    def test_background_failure_does_not_restart_diagnostic(self):
        from types import SimpleNamespace
        center=ControlCenter.__new__(ControlCenter)
        center._worker_status=Mock(return_value={'collector_pid':123,'control_smoke_test':{'enabled':True}})
        center._short_script_notice=Mock(return_value='failed')
        center._reload_session_from_manifest=Mock()
        center._worker_op_state={};center._worker_op_started_at={};center._worker_op_errors={}
        center._finalize_start_failure=Mock()
        with patch('ac_workers.control_center.pid_alive',return_value=True):
            center._handle_bg_result(SimpleNamespace(operation='start',worker_id=0,code=1,output='handshake failed'))
        center._finalize_start_failure.assert_not_called()
        self.assertEqual(center._worker_op_state[0],WORKER_OP_OFFLINE)

if __name__=='__main__': unittest.main()
