"""Offline tests for the observer-only pipeline timeline."""
from __future__ import annotations

import pytest

from ac_workers import pipeline_timeline as pt


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # Route every write/read to a temp status dir and clear in-process state.
    pt.reset_state_for_tests()
    monkeypatch.setattr(pt, "STATUS_DIR", tmp_path)
    yield
    pt.reset_state_for_tests()


def _marks(worker_id):
    return pt.read_timeline(worker_id)


def test_step_recorded_only_once_per_cycle():
    assert pt.mark(0, pt.START) is True
    # Same step again in the same (startup) cycle must be ignored.
    assert pt.mark(0, pt.START) is False
    assert pt.mark(0, pt.PLUGIN_CONNECTED) is True
    steps = [r["step"] for r in _marks(0)]
    assert steps == [pt.START, pt.PLUGIN_CONNECTED]


def test_timestamps_are_monotonic():
    pt.mark(1, pt.START)
    pt.mark(1, pt.PLUGIN_CONNECTED)
    pt.mark(1, pt.FIRST_TELEMETRY)
    pt.mark(1, pt.IDLE_READY)
    elapsed = [r["elapsed"] for r in _marks(1)]
    assert elapsed == sorted(elapsed)
    assert elapsed[0] == 0.0  # first mark is the t0 anchor


def test_reset_cycle_rerecords_each_episode():
    pt.mark(2, pt.START)
    pt.begin_reset_cycle(2, 1)
    assert pt.mark(2, pt.RESET_REQUESTED) is True
    # Same reset step in the same episode cycle is deduped...
    assert pt.mark(2, pt.RESET_REQUESTED) is False
    # ...but a new episode opens a fresh cycle and re-records it.
    pt.begin_reset_cycle(2, 2)
    assert pt.mark(2, pt.RESET_REQUESTED) is True
    cycles = [r["cycle"] for r in _marks(2) if r["step"] == pt.RESET_REQUESTED]
    assert cycles == ["reset:1", "reset:2"]


def test_summary_matches_last_completed_step():
    pt.mark(3, pt.START)
    pt.mark(3, pt.PLUGIN_CONNECTED)
    pt.mark(3, pt.FIRST_TELEMETRY)
    s = pt.worker_summary(3)
    assert s["last_step"] == pt.FIRST_TELEMETRY
    assert s["next_expected"] == pt.DRIVE_CONFIRMED
    assert s["complete"] is False


def test_completed_worker_reports_idle_ready():
    for step in pt.STARTUP_ORDER:
        if step == pt.AC_PROCESS_STARTED:
            continue  # launcher-side step, not emitted by the collector
        pt.mark(4, step)
    s = pt.worker_summary(4)
    assert s["last_step"] == pt.IDLE_READY
    assert s["complete"] is True
    assert s["next_expected"] == ""


def test_reset_failure_summary_carries_reason():
    pt.begin_reset_cycle(5, 1)
    pt.mark(5, pt.RESET_REQUESTED)
    pt.mark(5, pt.RESET_NEUTRALIZED)
    pt.mark(5, pt.RESET_FAILED, detail="AC session not driving after episode reset")
    s = pt.worker_summary(5)
    assert s["last_step"] == pt.RESET_FAILED
    assert s["failed"] is True
    assert "not driving" in s["detail"]


def test_component_defaults_and_override():
    # Collector step defaults to collector attribution.
    pt.mark(6, pt.RESET_REQUESTED)
    # Plugin steps default to plugin attribution from the step map.
    pt.begin_reset_cycle(6, 1)
    pt.mark(6, pt.RESET_REQUESTED)
    pt.mark(6, pt.PLUGIN_SIM_LIVE)
    pt.mark(6, pt.PLUGIN_DRIVING_TRUE)
    by_step = {r["step"]: r["component"] for r in _marks(6)}
    assert by_step[pt.RESET_REQUESTED] == pt.COMPONENT_COLLECTOR
    assert by_step[pt.PLUGIN_SIM_LIVE] == pt.COMPONENT_PLUGIN
    assert by_step[pt.PLUGIN_DRIVING_TRUE] == pt.COMPONENT_PLUGIN


def test_plugin_stall_visible_when_driving_never_reported():
    # Plugin came back alive after teleport but never declared driving -> the
    # collector then fails its truth gate. The trace makes the culprit obvious.
    pt.begin_reset_cycle(7, 1)
    pt.mark(7, pt.RESET_REQUESTED)
    pt.mark(7, pt.RESET_NEUTRALIZED)
    pt.mark(7, pt.RESET_TELEPORT_SENT)
    pt.mark(7, pt.RESET_TELEMETRY_FRESH)
    pt.mark(7, pt.PLUGIN_SIM_LIVE)
    pt.mark(7, pt.RESET_FAILED, detail="AC session not driving after episode reset")
    steps = [r["step"] for r in _marks(7)]
    assert pt.PLUGIN_SIM_LIVE in steps
    assert pt.PLUGIN_DRIVING_TRUE not in steps  # the plugin never flipped driving
    s = pt.worker_summary(7)
    assert s["failed"] is True


def test_reset_id_is_globally_unique_and_increasing():
    a = pt.begin_reset_cycle(0, 1)
    b = pt.begin_reset_cycle(1, 1)  # different worker, same episode number
    c = pt.begin_reset_cycle(0, 2)
    assert a < b < c
    assert pt.current_reset_id(0) == c
    assert pt.current_reset_id(1) == b


def test_events_carry_active_reset_id():
    rid = pt.begin_reset_cycle(0, 5)
    pt.mark(0, pt.RESET_REQUESTED)
    pt.mark(0, pt.PLUGIN_SIM_LIVE)
    ids = {r["step"]: r["reset_id"] for r in _marks(0) if not r.get("orphan")}
    assert ids[pt.RESET_REQUESTED] == rid
    assert ids[pt.PLUGIN_SIM_LIVE] == rid


def test_no_cross_reset_event_bleeding():
    """Events from reset N must never appear in reset N+1's scope.

    A plugin reading tagged with the previous epoch (expected_reset_id) lands
    AFTER a new reset cycle has opened: it must be flagged as an orphan, counted
    in RESET_EPOCH_MISMATCH_COUNT, and excluded from the live epoch's summary.
    """
    rid_n = pt.begin_reset_cycle(0, 1)
    pt.mark(0, pt.RESET_REQUESTED, expected_reset_id=rid_n)
    pt.mark(0, pt.PLUGIN_SIM_LIVE, expected_reset_id=rid_n)

    # A new reset epoch opens for the same worker...
    rid_n1 = pt.begin_reset_cycle(0, 2)
    pt.mark(0, pt.RESET_REQUESTED, expected_reset_id=rid_n1)
    # ...and a stale plugin event from epoch N arrives late.
    wrote = pt.mark(0, pt.PLUGIN_DRIVING_TRUE, expected_reset_id=rid_n)
    assert wrote is False  # rejected as a real step
    assert pt.epoch_mismatch_count() == 1

    # The latest epoch's summary must not contain the stale event.
    epoch_records = [
        r for r in _marks(0)
        if int(r.get("reset_id", -1)) == rid_n1 and not r.get("orphan")
    ]
    steps_n1 = {r["step"] for r in epoch_records}
    assert pt.PLUGIN_DRIVING_TRUE not in steps_n1
    # And the orphan is preserved on disk, flagged, for forensics.
    orphans = [r for r in _marks(0) if r.get("orphan")]
    assert len(orphans) == 1
    assert orphans[0]["step"] == pt.PLUGIN_DRIVING_TRUE
    assert orphans[0]["expected_reset_id"] == rid_n


def test_reset_step_without_open_cycle_is_orphan():
    # A reset step recorded while still in the startup cycle (reset_id 0) is a
    # causality violation: there is no open reset epoch to attribute it to.
    wrote = pt.mark(0, pt.RESET_DRIVING_CONFIRMED)
    assert wrote is False
    assert pt.epoch_mismatch_count() == 1
    assert all(r.get("orphan") for r in _marks(0))


def test_summary_uses_latest_epoch_only():
    # An earlier reset completed; the current one stalls. The summary must show
    # the stalled current epoch, not the earlier success.
    pt.begin_reset_cycle(0, 1)
    for step in (pt.RESET_REQUESTED, pt.RESET_NEUTRALIZED, pt.RESET_TELEPORT_SENT,
                 pt.RESET_TELEMETRY_FRESH, pt.PLUGIN_SIM_LIVE, pt.PLUGIN_DRIVING_TRUE,
                 pt.RESET_DRIVING_CONFIRMED, pt.RESET_VJOY_VALIDATED, pt.RESET_RUNNING):
        pt.mark(0, step)
    rid2 = pt.begin_reset_cycle(0, 2)
    pt.mark(0, pt.RESET_REQUESTED)
    pt.mark(0, pt.RESET_TELEPORT_SENT)
    s = pt.worker_summary(0)
    assert s["reset_id"] == rid2
    assert s["last_step"] == pt.RESET_TELEPORT_SENT
    assert s["complete"] is False


def test_component_seq_is_monotonic_per_component():
    pt.begin_reset_cycle(0, 1)
    pt.mark(0, pt.RESET_REQUESTED)              # collector seq 1
    pt.mark(0, pt.PLUGIN_SIM_LIVE)             # plugin seq 1
    pt.mark(0, pt.RESET_TELEPORT_SENT)         # collector seq 2
    pt.mark(0, pt.PLUGIN_DRIVING_TRUE)         # plugin seq 2
    by = {(r["component"], r["step"]): r.get("seq") for r in _marks(0) if not r.get("orphan")}
    assert by[(pt.COMPONENT_COLLECTOR, pt.RESET_REQUESTED)] == 1
    assert by[(pt.COMPONENT_COLLECTOR, pt.RESET_TELEPORT_SENT)] == 2
    assert by[(pt.COMPONENT_PLUGIN, pt.PLUGIN_SIM_LIVE)] == 1
    assert by[(pt.COMPONENT_PLUGIN, pt.PLUGIN_DRIVING_TRUE)] == 2


def test_component_seq_resets_each_epoch():
    pt.begin_reset_cycle(0, 1)
    pt.mark(0, pt.RESET_REQUESTED)
    pt.mark(0, pt.RESET_TELEPORT_SENT)
    pt.begin_reset_cycle(0, 2)
    pt.mark(0, pt.RESET_REQUESTED)
    first = [r for r in _marks(0) if r.get("reset_id") and r["step"] == pt.RESET_REQUESTED]
    # Each epoch's first collector event restarts the counter at 1.
    assert all(r.get("seq") == 1 for r in first)


def test_validate_clean_timeline_has_no_anomalies():
    pt.begin_reset_cycle(0, 1)
    pt.mark(0, pt.RESET_REQUESTED)
    pt.mark(0, pt.PLUGIN_SIM_LIVE)
    pt.mark(0, pt.RESET_TELEPORT_SENT)
    assert pt.validate_timeline(0) == []


def test_shm_collision_detected_when_workers_share_packet_id():
    pt.record_shm_fingerprint(0, packet_id=5000, clock_ms=12000)
    pt.record_shm_fingerprint(1, packet_id=5000, clock_ms=12000)  # same -> collision
    pt.record_shm_fingerprint(2, packet_id=9999, clock_ms=12010)
    report = pt.shm_collision_report([0, 1, 2])
    assert "COLLISION DETECTED" in report
    assert "workers 0,1" in report


def test_shm_no_collision_when_packet_ids_distinct():
    pt.record_shm_fingerprint(0, packet_id=100)
    pt.record_shm_fingerprint(1, packet_id=200)
    pt.record_shm_fingerprint(2, packet_id=300)
    report = pt.shm_collision_report([0, 1, 2])
    assert "no collision" in report


def test_summary_table_lists_each_worker_once():
    pt.mark(0, pt.START)
    pt.mark(0, pt.IDLE_READY)
    pt.begin_reset_cycle(1, 1)
    pt.mark(1, pt.RESET_REQUESTED)
    table = pt.summarize([0, 1, 2])
    lines = table.splitlines()
    assert lines[0].startswith("W |")
    body = lines[2:]
    assert body[0].startswith("0 |") and pt.IDLE_READY in body[0]
    assert body[1].startswith("1 |") and pt.RESET_REQUESTED in body[1]
    assert body[2].startswith("2 |") and "no timeline" in body[2]
