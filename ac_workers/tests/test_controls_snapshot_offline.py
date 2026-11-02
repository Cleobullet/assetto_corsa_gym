"""Offline tests for the per-worker controls.ini snapshot proof and the honest
pulse preflight. No live game, no vJoy, no learner — all pure logic / files.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "assetto_corsa_gym") not in sys.path:
    sys.path.insert(0, str(REPO / "assetto_corsa_gym"))

from ac_workers import controls_snapshot as cs

_CONTROLS = (
    "[CONTROLLERS]\nCON0=vJoy Device\nPGUID0=4C035200-7051-11EB-8002-444553540000\n"
    "[STEER]\nJOY={joy}\nAXLE=0\n[GEARUP]\nJOY={joy}\nBUTTON=0\n"
)


def _write_controls(path: Path, joy: int) -> None:
    path.write_text(_CONTROLS.format(joy=joy), encoding="utf-8")


def test_snapshot_verdict_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_CONTROLS_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    controls = tmp_path / "controls.ini"
    _write_controls(controls, joy=2)
    # worker 0 -> device 1, detected enumeration index 2, snapshot also has JOY=2.
    meta = cs.write_snapshot(0, controls, detected_joy_index=2)
    assert meta["joy_index_written"] == 2
    assert meta["detected_joy_index"] == 2
    assert meta["validation_status"] == "OK"
    assert Path(meta["controls_snapshot_path"]).is_file()


def test_snapshot_verdict_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_CONTROLS_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    controls = tmp_path / "controls.ini"
    _write_controls(controls, joy=1)  # written 1 but detected says 2
    meta = cs.write_snapshot(0, controls, detected_joy_index=2)
    assert meta["validation_status"] == "MISMATCH"


def test_snapshot_verdict_no_detection(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_CONTROLS_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    controls = tmp_path / "controls.ini"
    _write_controls(controls, joy=3)
    meta = cs.write_snapshot(1, controls, detected_joy_index=None)
    assert meta["validation_status"] == "NO_DETECTION"


def test_record_ac_launch_after_patch(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_CONTROLS_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    controls = tmp_path / "controls.ini"
    _write_controls(controls, joy=2)
    cs.write_snapshot(0, controls, detected_joy_index=2)
    meta = cs.record_ac_launch(0, ac_pid=4321)
    assert meta["ac_pid"] == 4321
    assert meta["ac_launched_after_patch"] is True
    reloaded = cs.load_meta(0)
    assert reloaded["ac_pid"] == 4321


def test_clear_snapshots(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_CONTROLS_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    controls = tmp_path / "controls.ini"
    _write_controls(controls, joy=2)
    cs.write_snapshot(0, controls, detected_joy_index=2)
    cs.write_snapshot(1, controls, detected_joy_index=2)
    removed = cs.clear_snapshots()
    assert removed >= 2
    assert cs.load_meta(0) == {}


def test_worker_identity_reads_snapshot_verdict(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_CONTROLS_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    controls = tmp_path / "controls.ini"
    _write_controls(controls, joy=2)
    cs.write_snapshot(0, controls, detected_joy_index=2)

    # Point the live probe at a mapping where device 1 -> index 2 (matches snapshot).
    from ac_workers import vjoy_index_probe as vip

    mapping = tmp_path / "vjoy_mapping_latest.json"
    mapping.write_text(json.dumps({"device_to_index": {"1": 2}}), encoding="utf-8")
    monkeypatch.setattr(vip, "MAPPING_LATEST", mapping)

    from ac_workers import worker_identity as wi

    identities = wi.build_worker_map([0], include_status=False, include_ledger=False)
    ident = identities[0]
    assert ident.detected_joy_index == 2
    assert ident.joy_index_written == 2
    assert ident.mapping_status == "OK(remapped)"


def test_worker_identity_no_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_CONTROLS_SNAPSHOT_DIR", str(tmp_path / "empty"))
    from ac_workers import vjoy_index_probe as vip

    mapping = tmp_path / "m.json"
    mapping.write_text(json.dumps({"device_to_index": {"1": 2}}), encoding="utf-8")
    monkeypatch.setattr(vip, "MAPPING_LATEST", mapping)

    from ac_workers import worker_identity as wi

    ident = wi.build_worker_map([0], include_status=False, include_ledger=False)[0]
    assert ident.mapping_status == "NO_SNAPSHOT"


# --------------------------------------------------------------------------- #
# identity_test authoritative pulse: identity reconciliation + verdicts
# --------------------------------------------------------------------------- #
def test_identity_offline_when_nothing_running(monkeypatch):
    import ac_workers.identity_test as it

    monkeypatch.setattr(it, "_status_via_diag", lambda wid, timeout=2.0: {})
    monkeypatch.setattr(it, "_read_status_file", lambda wid: {})
    ident = it._identity_probe(3)
    assert ident.state == "OFFLINE"
    assert ident.diag_reachable is False


def test_identity_diag_state_mismatch_pid(monkeypatch):
    """C4D status file shows a pid, diag answers with a DIFFERENT pid -> mismatch."""
    import ac_workers.identity_test as it

    monkeypatch.setattr(
        it, "_status_via_diag",
        lambda wid, timeout=2.0: {"ok": True, "worker_id": wid, "collector_pid": 999,
                                  "boot_id": "abc", "phase": "idle"},
    )
    monkeypatch.setattr(
        it, "_read_status_file",
        lambda wid: {"collector_pid": 111, "heartbeat_age_ms": 100.0},
    )
    ident = it._identity_probe(0)
    assert ident.state == "DIAG_STATE_MISMATCH"


def test_identity_diag_state_mismatch_unreachable(monkeypatch):
    """C4D thinks worker runs (fresh file + pid) but diag is unreachable -> mismatch."""
    import ac_workers.identity_test as it

    monkeypatch.setattr(it, "_status_via_diag", lambda wid, timeout=2.0: {})
    monkeypatch.setattr(
        it, "_read_status_file",
        lambda wid: {"collector_pid": 222, "heartbeat_age_ms": 100.0},
    )
    ident = it._identity_probe(1)
    assert ident.state == "DIAG_STATE_MISMATCH"


def test_identity_live(monkeypatch):
    import ac_workers.identity_test as it

    monkeypatch.setattr(
        it, "_status_via_diag",
        lambda wid, timeout=2.0: {"ok": True, "worker_id": wid, "collector_pid": 500,
                                  "boot_id": "boot1", "phase": "idle",
                                  "vjoy_device": 1, "vjoy_device_acquired": True,
                                  "telemetry_age_ms": 50.0},
    )
    monkeypatch.setattr(it, "_read_status_file", lambda wid: {"collector_pid": 500, "heartbeat_age_ms": 80.0})
    ident = it._identity_probe(0)
    assert ident.state == "LIVE"


def _isolate_pulse_side_effects(it, monkeypatch):
    """Stop pulse() from touching real diag ports for pause/resume/neutralize."""
    monkeypatch.setattr(it, "_diag_cmd_simple", lambda w, c: {"ok": True})
    monkeypatch.setattr(it, "_diag_cmd_neutralize", lambda w: None)


def _mock_gate_ready(it, monkeypatch, worker_ids, phase="running"):
    """Bypass gate_binding_test so unit tests can exercise pulse() in isolation."""

    def _gate(ids):
        identities = {
            w: it.Identity(
                worker_id=w,
                diag_reachable=True,
                state="LIVE",
                collector_pid=700 + w,
                boot_id="boot{0}".format(w),
                phase=phase,
                vjoy_acquired=True,
                vjoy_device_id=w + 1,
            )
            for w in ids
        }
        return it.BindingGateResult(worker_ids=list(ids), ready=True, identities=identities)

    monkeypatch.setattr(it, "gate_binding_test", _gate)


def test_pulse_write_failed_verdict(monkeypatch):
    """Diag reachable + idle, but the vJoy write fails -> PULSE_WRITE_FAILED."""
    import ac_workers.identity_test as it

    monkeypatch.setattr(
        it, "_status_via_diag",
        lambda wid, timeout=2.0: {"collector_pid": 7, "boot_id": "b", "phase": "idle",
                                  "telemetry_age_ms": 99999.0, "telemetry_snapshot": {}},
    )
    monkeypatch.setattr(it, "_read_status_file", lambda wid: {})
    monkeypatch.setattr(
        it, "_diag_pulse",
        lambda wid, payload=None, timeout=6.0: {"write_ok": False, "error": "UpdateVJD failed",
                                                "collector_pid": 7, "vjoy_device_id": wid + 1},
    )
    _isolate_pulse_side_effects(it, monkeypatch)
    _mock_gate_ready(it, monkeypatch, [0])
    res = it.pulse([0], pulse_sec=0.05, sample_hz=20.0)
    assert res.targets[0].verdict == it.V_WRITE_FAILED
    out = it.format_pulse(res)
    assert "WRITE_FAILED" in out
    # Crucial: no successful write => NEVER a binding problem.
    assert "BINDING PROBLEM" not in out
    assert res.violations == []


def test_pulse_socket_error_is_not_write_failed(monkeypatch):
    """[Errno 22]/10054 are comms errors, NOT write failures and NOT binding problems."""
    import ac_workers.identity_test as it

    monkeypatch.setattr(
        it, "_status_via_diag",
        lambda wid, timeout=2.0: {"collector_pid": 7, "boot_id": "b", "phase": "idle",
                                  "telemetry_age_ms": 50.0, "telemetry_snapshot": {}},
    )
    monkeypatch.setattr(it, "_read_status_file", lambda wid: {})
    monkeypatch.setattr(
        it, "_diag_pulse",
        lambda wid, payload=None, timeout=6.0: {
            "write_ok": False, "unreachable": True,
            "error": "collector worker {0} diagnostic error: [Errno 22] Invalid argument".format(wid),
        },
    )
    _isolate_pulse_side_effects(it, monkeypatch)
    _mock_gate_ready(it, monkeypatch, [0, 1])
    res = it.pulse([0, 1], pulse_sec=0.05, sample_hz=20.0)
    assert res.targets[0].verdict == it.V_DIAG_COMMS
    assert res.targets[1].verdict == it.V_DIAG_COMMS
    out = it.format_pulse(res)
    assert "BINDING PROBLEM" not in out
    assert "DIAG_COMMS_ERROR" in out or "DIAG_STATE_MISMATCH" in out
    assert res.violations == []


def test_pulse_all_write_failed_global_verdict(monkeypatch):
    """Every worker fails to write => global verdict WRITE_FAILED, never BINDING PROBLEM."""
    import ac_workers.identity_test as it

    monkeypatch.setattr(
        it, "_status_via_diag",
        lambda wid, timeout=2.0: {"collector_pid": 7, "boot_id": "b", "phase": "running",
                                  "telemetry_age_ms": 50.0, "telemetry_snapshot": {}},
    )
    monkeypatch.setattr(it, "_read_status_file", lambda wid: {})
    monkeypatch.setattr(
        it, "_diag_pulse",
        lambda wid, payload=None, timeout=6.0: {"write_ok": False, "error": "UpdateVJD failed"},
    )
    _isolate_pulse_side_effects(it, monkeypatch)
    _mock_gate_ready(it, monkeypatch, [0, 1, 2])
    res = it.pulse([0, 1, 2], pulse_sec=0.05, sample_hz=20.0)
    out = it.format_pulse(res)
    assert "VERDICT: WRITE_FAILED" in out
    assert "BINDING PROBLEM" not in out


def test_pulse_env_not_ready_verdict(monkeypatch):
    """Collector up but env/vjoy not bound -> ENV_NOT_READY, not a write/binding failure."""
    import ac_workers.identity_test as it

    monkeypatch.setattr(
        it, "_status_via_diag",
        lambda wid, timeout=2.0: {"ok": True, "worker_id": wid, "collector_pid": 7,
                                  "boot_id": "b", "phase": "running",
                                  "telemetry_age_ms": 50.0, "telemetry_snapshot": {},
                                  "vjoy_device_acquired": True},
    )
    monkeypatch.setattr(it, "_read_status_file", lambda wid: {})
    monkeypatch.setattr(
        it, "_diag_pulse",
        lambda wid, payload=None, timeout=6.0: {
            "write_ok": False, "env_ready": False,
            "error": "ENV_NOT_READY: collector has not bound its vJoy write handle yet",
        },
    )
    _isolate_pulse_side_effects(it, monkeypatch)
    _mock_gate_ready(it, monkeypatch, [0])
    res = it.pulse([0], pulse_sec=0.05, sample_hz=20.0)
    assert res.targets[0].verdict == it.V_ENV_NOT_READY
    assert res.violations == []


def test_gate_blocks_starting_phase(monkeypatch):
    """Pulse must refuse while workers are still in startup phase (clean-start incomplete)."""
    import ac_workers.identity_test as it

    monkeypatch.setattr(
        it, "_status_via_diag",
        lambda wid, timeout=2.0: {"ok": True, "worker_id": wid, "collector_pid": 7,
                                  "boot_id": "b", "phase": "starting",
                                  "vjoy_device_acquired": True, "telemetry_age_ms": 99999.0},
    )
    monkeypatch.setattr(it, "_read_status_file", lambda wid: {})
    _isolate_pulse_side_effects(it, monkeypatch)
    res = it.pulse([0], pulse_sec=0.05)
    assert res.gate_blocked is True
    out = it.format_pulse(res)
    assert "PREFLIGHT_BLOCKED" in out
    assert "BINDING PROBLEM" not in out


def test_pulse_wrong_car_is_binding_problem(monkeypatch):
    """A SUCCESSFUL write that moves another worker's car IS a binding problem."""
    import ac_workers.identity_test as it

    # Worker 1's car (the OTHER car) moves only WHILE worker 0 is being pulsed.
    # That models a wrong/cross-wired binding: pulsing W0 drives W1's car.
    pulsing = {"on": False}

    def fake_status(wid, timeout=2.0):
        steer = 0.9 if (wid == 1 and pulsing["on"]) else 0.0
        return {"collector_pid": 7, "boot_id": "b", "phase": "running",
                "telemetry_age_ms": 50.0, "telemetry_snapshot": {"steer": steer}}

    monkeypatch.setattr(it, "_status_via_diag", fake_status)
    monkeypatch.setattr(it, "_read_status_file", lambda wid: {})

    def slow_pulse(wid, payload=None, timeout=6.0):
        # Mimic the collector holding the action so the concurrent sampler observes
        # the other worker's car moving during W0's pulse only.
        if wid == 0:
            pulsing["on"] = True
            time.sleep(0.2)
            pulsing["on"] = False
        return {"write_ok": True, "car_moved": False, "car_motion": 0.0,
                "telemetry_fresh": True, "collector_pid": 7, "vjoy_device_id": wid + 1}

    monkeypatch.setattr(it, "_diag_pulse", slow_pulse)
    _isolate_pulse_side_effects(it, monkeypatch)
    _mock_gate_ready(it, monkeypatch, [0, 1])
    res = it.pulse([0, 1], pulse_sec=0.2, sample_hz=100.0)
    assert res.targets[0].verdict == it.V_WRONG_CAR
    out = it.format_pulse(res)
    assert "BINDING PROBLEM" in out


def test_pulse_own_car_moved_verdict(monkeypatch):
    """Write OK + this worker's own car telemetry moves -> OWN_CAR (diagonal OK)."""
    import ac_workers.identity_test as it

    state = {"n": 0}

    def fake_status(wid, timeout=2.0):
        # Fresh telemetry; worker 0's own car shows steer growing during the pulse.
        state["n"] += 1
        steer = 0.0 if state["n"] < 2 else 0.9
        return {"collector_pid": 7, "boot_id": "b", "phase": "idle",
                "telemetry_age_ms": 50.0, "telemetry_snapshot": {"steer": steer}}

    monkeypatch.setattr(it, "_status_via_diag", fake_status)
    monkeypatch.setattr(it, "_read_status_file", lambda wid: {})
    monkeypatch.setattr(
        it, "_diag_pulse",
        lambda wid, payload=None, timeout=6.0: {"write_ok": True, "car_moved": True,
                                                "car_motion": 0.9, "telemetry_fresh": True,
                                                "collector_pid": 7, "vjoy_device_id": wid + 1,
                                                "last_vjoy_write_device": wid + 1},
    )
    _isolate_pulse_side_effects(it, monkeypatch)
    _mock_gate_ready(it, monkeypatch, [0])
    res = it.pulse([0], pulse_sec=0.05, sample_hz=20.0)
    assert res.targets[0].verdict == it.V_OWN_CAR
    assert res.violations == []


def test_pulse_diag_state_mismatch_short_circuits(monkeypatch):
    import ac_workers.identity_test as it

    monkeypatch.setattr(it, "_status_via_diag", lambda wid, timeout=2.0: {})
    monkeypatch.setattr(it, "_read_status_file", lambda wid: {"collector_pid": 9, "heartbeat_age_ms": 10.0})
    # _diag_pulse must NOT be called for a mismatched worker.
    called = {"pulse": False}

    def _no_pulse(wid, payload=None, timeout=6.0):
        called["pulse"] = True
        return {}

    monkeypatch.setattr(it, "_diag_pulse", _no_pulse)
    _isolate_pulse_side_effects(it, monkeypatch)
    res = it.pulse([0], pulse_sec=0.05)
    assert res.gate_blocked is True
    assert any("DIAG_STATE_MISMATCH" in b for b in res.gate_blockers)
    assert called["pulse"] is False


def test_pulse_uses_normal_driving_writer(monkeypatch):
    """GUARANTEE: diagnostic_vjoy_pulse writes vJoy via the SAME function the main
    driving loop uses (apply_canonical_controls). If this ever diverges, the pulse
    could fail while normal driving works (the exact regression we are fixing)."""
    import types

    import ac_workers.canonical_controls as cc
    import ac_workers.worker_collector as wc

    calls = []

    def fake_apply(local_controls, steer, throttle, brake, *, device_id=None, allow_reacquire=True):
        calls.append({"device_id": device_id, "steer": steer, "throttle": throttle, "brake": brake})
        return {"ok": True, "update_vjd_result": True, "vjoy_device_id": device_id,
                "canonical_controls_called": True}

    monkeypatch.setattr(cc, "apply_canonical_controls", fake_apply)

    runtime = types.SimpleNamespace(
        worker_id=0, collector_pid=123, env=None, telemetry_snapshot={"steer": 0.0},
        last_vjoy_write_device=None, last_vjoy_pulse={},
        telemetry_age_ms=lambda: 50.0,
    )
    neutral_called = {"n": 0}

    def _neutral():
        neutral_called["n"] += 1

    resp = wc.perform_vjoy_pulse(
        runtime, object(), 3, steer=0.6, throttle=0.0, brake=1.0, hold_sec=0.05,
        neutralize=_neutral,
    )
    assert resp["writer"] == "apply_canonical_controls"
    assert len(calls) == 1
    assert calls[0]["device_id"] == 3
    assert resp["write_ok"] is True
    assert neutral_called["n"] == 1  # always neutralizes after


def test_pulse_failure_captures_full_trace(monkeypatch):
    """A write exception must yield a precise error + stacktrace, not a black box."""
    import types

    import ac_workers.canonical_controls as cc
    import ac_workers.worker_collector as wc

    def boom(*a, **k):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(cc, "apply_canonical_controls", boom)
    runtime = types.SimpleNamespace(
        worker_id=2, collector_pid=99, env=None, telemetry_snapshot={},
        last_vjoy_write_device=None, last_vjoy_pulse={},
        telemetry_age_ms=lambda: 50.0,
    )
    resp = wc.perform_vjoy_pulse(
        runtime, object(), 3, steer=0.6, throttle=0.0, brake=1.0, hold_sec=0.01,
        neutralize=lambda: None,
    )
    assert resp["write_ok"] is False
    assert "Invalid argument" in resp["error"]
    assert resp["trace"]  # full stacktrace captured
    assert resp["action_sent"]["steer"] == 0.6


def test_selftest_uses_normal_driving_writer(monkeypatch):
    import types

    import ac_workers.canonical_controls as cc
    import ac_workers.worker_collector as wc

    calls = []

    def fake_apply(local_controls, steer, throttle, brake, *, device_id=None, allow_reacquire=True):
        calls.append("apply")
        return {"ok": True, "update_vjd_result": True, "vjoy_device_id": device_id,
                "physical_action": [steer, throttle, brake]}

    def fake_neutral(local_controls, *, device_id=None, allow_reacquire=True):
        calls.append("neutral")
        return {"ok": True, "update_vjd_result": True, "vjoy_device_id": device_id}

    monkeypatch.setattr(cc, "apply_canonical_controls", fake_apply)
    monkeypatch.setattr(cc, "neutralize_canonical", fake_neutral)

    runtime = types.SimpleNamespace(
        worker_id=1, collector_pid=55, telemetry_snapshot={}, last_vjoy_selftest={},
    )
    resp = wc.perform_vjoy_selftest(runtime, object(), 2, neutralize=lambda: None)
    assert resp["write_ok"] is True
    assert calls == ["neutral", "apply", "neutral"]


def test_direct_pulse_verdicts(monkeypatch):
    import ac_workers.identity_test as it

    monkeypatch.setattr(
        it, "_status_via_diag",
        lambda wid, timeout=2.0: {"collector_pid": 7, "boot_id": "b", "phase": "idle",
                                  "telemetry_age_ms": 50.0},
    )
    monkeypatch.setattr(it, "_read_status_file", lambda wid: {})
    monkeypatch.setattr(it, "_diag_cmd_neutralize", lambda wid: None)
    monkeypatch.setattr(
        it, "_diag_pulse",
        lambda wid, payload=None, timeout=6.0: {"write_ok": True, "car_moved": True,
                                                "car_motion": 0.8, "vjoy_device_id": wid + 1},
    )
    res = it.direct_vjoy_pulse([0, 1], repeats=1)
    assert res.verdict[0] == it.V_OWN_CAR
    assert res.verdict[1] == it.V_OWN_CAR
    assert "binding OK" in it.format_direct(res)


def main() -> int:
    print("Test-ControlsSnapshot-Offline: use pytest (fixtures/monkeypatch required)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
