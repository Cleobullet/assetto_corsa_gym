"""Offline tests for the vJoy garbage-collection stack: ledger, janitor client, reaper."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))


# --------------------------------------------------------------------------- ledger
def test_ledger_record_read_clear(tmp_path):
    import ac_workers.vjoy_ledger as ledger

    with mock.patch.object(ledger, "LEDGER_DIR", tmp_path):
        assert ledger.read_owner(3) is None
        assert ledger.record_owner(3, pid=1234, session_id="s1", worker_id=2) is True
        entry = ledger.read_owner(3)
        assert entry["device_id"] == 3
        assert entry["pid"] == 1234
        assert entry["worker_id"] == 2
        assert len(ledger.read_all()) == 1
        # Wrong pid must not clear someone else's ownership.
        assert ledger.clear_owner(3, pid=9999) is False
        assert ledger.read_owner(3) is not None
        # Correct pid clears it.
        assert ledger.clear_owner(3, pid=1234) is True
        assert ledger.read_owner(3) is None


def test_ledger_force_clear(tmp_path):
    import ac_workers.vjoy_ledger as ledger

    with mock.patch.object(ledger, "LEDGER_DIR", tmp_path):
        ledger.record_owner(5, pid=4321)
        assert ledger.clear_owner(5, pid=1, force=True) is True
        assert ledger.read_owner(5) is None


# --------------------------------------------------------------------------- janitor
def test_janitor_unavailable_when_no_heartbeat(tmp_path):
    import ac_workers.vjoy_janitor as jan

    with mock.patch.object(jan, "JANITOR_DIR", tmp_path), \
            mock.patch.object(jan, "HEARTBEAT_PATH", tmp_path / "heartbeat.json"), \
            mock.patch.object(jan, "REQUEST_PATH", tmp_path / "request.json"), \
            mock.patch.object(jan, "RESULT_PATH", tmp_path / "result.json"):
        assert jan.janitor_alive() is False
        res = jan.request_vjoy_reset([1, 2, 3])
        assert res["ok"] is False
        assert res["reason"] == "janitor_unavailable"


def test_janitor_request_result_round_trip(tmp_path):
    import ac_workers.vjoy_janitor as jan

    hb = tmp_path / "heartbeat.json"
    req = tmp_path / "request.json"
    res = tmp_path / "result.json"
    with mock.patch.object(jan, "JANITOR_DIR", tmp_path), \
            mock.patch.object(jan, "HEARTBEAT_PATH", hb), \
            mock.patch.object(jan, "REQUEST_PATH", req), \
            mock.patch.object(jan, "RESULT_PATH", res):
        assert jan.write_heartbeat(pid=4242) is True
        assert jan.janitor_alive() is True

        # Simulate the service answering as soon as a request lands.
        original_read = jan._read_json

        def fake_read(path):
            if path == res:
                pending = original_read(req)
                if pending:
                    return {
                        "request_id": pending["request_id"],
                        "ok": True,
                        "verified": True,
                        "message": "reset 1 node; verify exit=0",
                    }
                return None
            return original_read(path)

        with mock.patch.object(jan, "_read_json", side_effect=fake_read):
            out = jan.request_vjoy_reset([1, 2], timeout_sec=5, poll_sec=0.05)
        assert out["ok"] is True
        assert out["verified"] is True


# --------------------------------------------------------------------------- reaper
def test_reaper_healthy_owner_is_left_alone():
    from ac_workers import vjoy_reaper

    entries = [{"device_id": 1, "pid": 100}]
    summary = vjoy_reaper.sweep(
        entries=entries,
        pid_alive_fn=lambda pid: True,
        probe_fn=lambda dev: (_ for _ in ()).throw(AssertionError("must not probe live owner")),
        clear_fn=lambda *a, **k: True,
    )
    assert summary["healthy"] == [1]
    assert summary["leaked"] == []
    assert summary["needs_global_reset"] is False
    assert summary["safe_to_reset"] is False


def test_reaper_recovers_clean_device_after_dead_owner():
    from ac_workers import vjoy_reaper

    cleared = []
    entries = [{"device_id": 2, "pid": 200}]
    summary = vjoy_reaper.sweep(
        entries=entries,
        pid_alive_fn=lambda pid: False,
        probe_fn=lambda dev: True,  # device acquirable again -> clean
        clear_fn=lambda dev, **k: cleared.append(dev) or True,
    )
    assert summary["recovered"] == [2]
    assert summary["leaked"] == []
    assert cleared == [2]
    assert summary["needs_global_reset"] is False


def test_reaper_flags_leak_and_safe_reset_when_no_live_owner():
    from ac_workers import vjoy_reaper

    entries = [{"device_id": 3, "pid": 300}, {"device_id": 4, "pid": 400}]
    summary = vjoy_reaper.sweep(
        entries=entries,
        pid_alive_fn=lambda pid: False,  # both owners dead
        probe_fn=lambda dev: False,  # both still stuck
        clear_fn=lambda *a, **k: True,
    )
    assert summary["leaked"] == [3, 4]
    assert summary["needs_global_reset"] is True
    assert summary["safe_to_reset"] is True


def test_reaper_leak_not_safe_when_a_live_owner_present():
    from ac_workers import vjoy_reaper

    entries = [{"device_id": 5, "pid": 500}, {"device_id": 6, "pid": 600}]

    def alive(pid):
        return pid == 600  # device 6 has a live owner

    summary = vjoy_reaper.sweep(
        entries=entries,
        pid_alive_fn=alive,
        probe_fn=lambda dev: False,  # device 5 stuck
        clear_fn=lambda *a, **k: True,
    )
    assert summary["leaked"] == [5]
    assert summary["healthy"] == [6]
    assert summary["needs_global_reset"] is True
    # A global reset would bounce device 6 (live owner) -> not safe.
    assert summary["safe_to_reset"] is False
