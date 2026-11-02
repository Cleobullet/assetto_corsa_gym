"""Offline tests for the global lifecycle control layer (no real processes)."""
from __future__ import annotations

import socket

from ac_workers import system_control as sc


def test_all_known_ports_includes_transition_and_worker_ports():
    ports = sc.all_known_ports([0])
    assert sc.TRANSITION_PORT in ports
    # worker 0: ego 2345, opp 2346, mgmt 2347, diag 24100
    for p in (2345, 2346, 2347, 24100):
        assert p in ports
    assert ports == sorted(ports)


def test_port_in_use_detects_a_real_listener():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    used_port = srv.getsockname()[1]
    try:
        assert sc.port_in_use(used_port) is True
    finally:
        srv.close()
    # After close the port should be free again.
    assert sc.port_in_use(used_port) is False


def test_parse_listening_ports_extracts_only_listening():
    netstat = "\n".join(
        [
            "  Proto  Local Address          Foreign Address        State           PID",
            "  TCP    127.0.0.1:24050        0.0.0.0:0              LISTENING       1234",
            "  TCP    127.0.0.1:2347         127.0.0.1:55000        ESTABLISHED     5678",
            "  TCP    127.0.0.1:24100        0.0.0.0:0              TIME_WAIT       0",
            "  TCP    [::]:24104             [::]:0                 LISTENING       4321",
        ]
    )
    ports = sc.parse_listening_ports(netstat)
    assert 24050 in ports  # actively serving
    assert 24104 in ports  # IPv6 listener
    assert 2347 not in ports  # ESTABLISHED, not a fresh-start blocker
    assert 24100 not in ports  # TIME_WAIT must NOT count as busy


def test_busy_ports_ignores_time_wait_via_listening_set():
    # Only 24050 is actually LISTENING; the worker-0 diag port (24100) is e.g. in
    # TIME_WAIT after a kill and must not be reported busy.
    busy = sc.busy_ports([0], listening={sc.TRANSITION_PORT})
    assert busy == [sc.TRANSITION_PORT]


def test_busy_ports_clean_when_nothing_listening():
    assert sc.busy_ports([0], listening=set()) == []


def test_stray_official_ac_is_not_the_ai_clone(tmp_path):
    ai = tmp_path / "AssettoCorsa-AI"
    official = tmp_path / "Steam" / "steamapps" / "common" / "assettocorsa" / "AssettoCorsa.exe"
    assert sc.is_stray_official_ac(str(official), ai) is True
    assert sc.is_stray_official_ac(str(ai / "acs.exe"), ai) is False
    assert sc.is_ai_clone_ac(str(ai / "acs.exe"), ai) is True


def test_system_health_clean_when_all_scanners_empty():
    health = sc.system_health(
        acs_scan=lambda repo: [],
        port_scan=lambda wids: [],
        collector_scan=lambda wids: {},
    )
    assert health["clean"] is True
    assert health["orphan_acs"] == []
    assert health["busy_ports"] == []
    assert health["zombie_collectors"] == {}


def test_system_health_dirty_when_orphan_acs_present():
    health = sc.system_health(
        acs_scan=lambda repo: [1111, 2222],
        port_scan=lambda wids: [],
        collector_scan=lambda wids: {},
    )
    assert health["clean"] is False
    assert health["orphan_acs"] == [1111, 2222]


def test_assert_clean_state_blocks_when_ports_busy():
    clean, health = sc.assert_clean_state(
        acs_scan=lambda repo: [],
        port_scan=lambda wids: [24100],
        collector_scan=lambda wids: {},
    )
    assert clean is False
    assert health["busy_ports"] == [24100]


def test_kill_all_reports_killed_and_freed_then_clean():
    calls = {"n": 0}

    def fake_emergency(close_game=False):
        calls["close_game"] = close_game
        return {
            "stopped": [
                {"acPid": 4152, "worker": 0},
                {"collectorPid": 25028, "worker": 0},
                {"learnerPid": 24024},
            ]
        }

    # Dirty before (port + orphan), clean after the kill.
    states = [
        {"clean": False, "orphan_acs": [4152], "busy_ports": [24100, 2347], "zombie_collectors": {}},
        {"clean": True, "orphan_acs": [], "busy_ports": [], "zombie_collectors": {}},
    ]

    # First call (before) -> index 0; second call (after) -> index 1.
    def health_seq(repo, wids=range(6)):
        idx = calls["n"]
        calls["n"] += 1
        return states[min(idx, len(states) - 1)]

    report = sc.kill_all_systems(
        emergency_fn=fake_emergency,
        settle_sec=0.0,
        sleep_fn=lambda _s: None,
        health_fn=health_seq,
    )
    assert calls["close_game"] is True  # kill-all must close the game
    assert 4152 in report["acs_killed"]
    assert report["python_processes_killed"] == 2  # collector + learner
    assert 24100 in report["ports_freed"] and 2347 in report["ports_freed"]
    assert report["clean_after"] is True
    assert report["orphan_detected"] == []


def test_kill_all_swallows_emergency_errors():
    def boom(close_game=False):
        raise RuntimeError("kill failed")

    report = sc.kill_all_systems(
        emergency_fn=boom,
        settle_sec=0.0,
        sleep_fn=lambda _s: None,
        health_fn=lambda repo, wids=range(6): {
            "clean": False, "orphan_acs": [9], "busy_ports": [], "zombie_collectors": {}
        },
    )
    assert any("kill failed" in e for e in report["errors"])
    assert report["clean_after"] is False


def test_main_status_returns_zero(capsys):
    rc = sc.main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "System health" in out
