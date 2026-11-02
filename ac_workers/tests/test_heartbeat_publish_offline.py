"""Offline tests for the collector heartbeat liveness write.

Regression guard for the false ``heartbeat_stale`` bug: under heavy load the
main loop stalls behind ``env.step()`` and network probes, so ``status_written_at``
must be refreshed by the dedicated heartbeat thread via ``heartbeat_publish()``
(in-memory only, no network). The dashboard's ``status_age_ms`` must then see a
live collector as fresh even when the main loop has not published recently.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest import mock


_WID = 3
_STATUS_FILE = "worker_3.json"


def _runtime(tmp_dir: Path):
    from ac_workers.worker_collector import CollectorRuntime

    runtime = CollectorRuntime(_WID, 1, 2347)
    return runtime


def test_heartbeat_publish_writes_fresh_status(tmp_path):
    import ac_workers.worker_collector as wc

    with mock.patch.object(wc, "STATUS_DIR", tmp_path):
        runtime = _runtime(tmp_path)
        runtime.set_phase("running")
        runtime.heartbeat_publish()

        path = tmp_path / _STATUS_FILE
        assert path.exists(), "heartbeat_publish must write the status file"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "status_written_at" in data
        assert data["phase"] == "running"


def test_heartbeat_publish_keeps_status_fresh_for_dashboard(tmp_path):
    """A live collector that has not run publish_status() recently must not be
    declared heartbeat_stale: heartbeat_publish keeps status_written_at current."""
    import ac_workers.worker_collector as wc
    from ac_workers.control_center import MAX_HEARTBEAT_MS, status_age_ms

    with mock.patch.object(wc, "STATUS_DIR", tmp_path):
        runtime = _runtime(tmp_path)
        runtime.set_phase("running")
        runtime.heartbeat_publish()

        data = json.loads((tmp_path / _STATUS_FILE).read_text(encoding="utf-8"))
        age = status_age_ms(data)
        assert age < MAX_HEARTBEAT_MS, (
            "fresh heartbeat write must read as live, got age_ms=%s" % age
        )


def test_heartbeat_publish_preserves_network_probed_fields(tmp_path):
    """heartbeat_publish must not clobber richer fields set by publish_status
    (learner_connected, vjoy_ready, ...) that minimal_snapshot does not produce."""
    import ac_workers.worker_collector as wc

    with mock.patch.object(wc, "STATUS_DIR", tmp_path):
        runtime = _runtime(tmp_path)
        runtime.set_phase("running")
        with runtime.lock:
            runtime._cached_snapshot = {
                "learner_connected": True,
                "transition_server_ok": True,
                "vjoy_ready": True,
                "phase": "stale_old_phase",
            }
        runtime.heartbeat_publish()

        data = json.loads((tmp_path / _STATUS_FILE).read_text(encoding="utf-8"))
        assert data.get("learner_connected") is True
        assert data.get("transition_server_ok") is True
        assert data.get("vjoy_ready") is True
        assert data["phase"] == "running"


def test_heartbeat_publish_advances_written_at(tmp_path):
    import ac_workers.worker_collector as wc

    with mock.patch.object(wc, "STATUS_DIR", tmp_path):
        runtime = _runtime(tmp_path)
        runtime.heartbeat_publish()
        first = json.loads((tmp_path / _STATUS_FILE).read_text(encoding="utf-8"))[
            "status_written_at"
        ]
        time.sleep(0.02)
        runtime.heartbeat_publish()
        second = json.loads((tmp_path / _STATUS_FILE).read_text(encoding="utf-8"))[
            "status_written_at"
        ]
        assert second >= first
        assert second != first


def main() -> int:
    import tempfile

    failures = 0
    for fn in (
        test_heartbeat_publish_writes_fresh_status,
        test_heartbeat_publish_keeps_status_fresh_for_dashboard,
        test_heartbeat_publish_preserves_network_probed_fields,
        test_heartbeat_publish_advances_written_at,
    ):
        with tempfile.TemporaryDirectory() as d:
            try:
                fn(Path(d))
                print("PASS", fn.__name__)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print("FAIL", fn.__name__, exc)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
