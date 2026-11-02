"""Offline tests for the worker identity map + isolation invariants."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))


def test_map_is_one_to_one_and_clean():
    from ac_workers.worker_identity import build_worker_map, check_isolation_invariants

    identities = build_worker_map(include_status=False, include_ledger=False)
    assert len(identities) == 6
    # Every address must be unique across the full worker set.
    assert len({i.ego_port for i in identities}) == 6
    assert len({i.opponents_port for i in identities}) == 6
    assert len({i.mgmt_port for i in identities}) == 6
    assert len({i.diag_port for i in identities}) == 6
    assert len({i.vjoy_device for i in identities}) == 6
    assert len({i.expected_policy_seed for i in identities}) == 6
    assert check_isolation_invariants(identities) == []


def test_strict_vjoy_mapping():
    from ac_workers.worker_identity import build_worker_map

    identities = build_worker_map(include_status=False, include_ledger=False)
    for i in identities:
        assert i.vjoy_device == i.worker_id + 1


def test_config_matches_canonical_map():
    """config/workers/worker_N.yml must agree with worker_ports / vjoy_pool."""
    from ac_workers.worker_identity import build_worker_map

    identities = build_worker_map(include_status=False, include_ledger=False)
    for i in identities:
        assert i.cfg_present, "missing config for worker {0}".format(i.worker_id)
        assert i.cfg_mismatch == [], i.cfg_mismatch


def test_status_overlay_reads_live_fields():
    from ac_workers.worker_identity import build_worker_map

    with tempfile.TemporaryDirectory() as td:
        status_dir = Path(td)
        status_dir.joinpath("worker_2.json").write_text(
            json.dumps(
                {
                    "collector_pid": 4321,
                    "phase": "running",
                    "control_mode": "async_local_actor",
                    "policy_action_source": "sac_explore",
                    "observation_hash": "deadbeef0001",
                    "policy_raw_action": [0.12, -0.3, -0.9],
                    "physical_action": [0.12, 0.0, 0.05],
                    "exploration_seed": 20014,
                    "vjoy_device": 3,
                    "diag_port": 24102,
                }
            ),
            encoding="utf-8",
        )
        identities = build_worker_map(
            [2], status_dir=status_dir, include_ledger=False
        )
        i = identities[0]
        assert i.status_present
        assert i.collector_pid == 4321
        assert i.last_observation_hash == "deadbeef0001"
        assert i.action_source == "sac_explore"
        assert i.status_vjoy_device == 3


def test_detects_status_vjoy_crosswire():
    """A worker reporting a vJoy device that is not its own is a hard violation."""
    from ac_workers.worker_identity import build_worker_map, check_isolation_invariants

    with tempfile.TemporaryDirectory() as td:
        status_dir = Path(td)
        # Worker 1 (map vjoy=2) wrongly reports vjoy_device=1 -> cross-wire.
        status_dir.joinpath("worker_1.json").write_text(
            json.dumps({"vjoy_device": 1, "diag_port": 24101}), encoding="utf-8"
        )
        identities = build_worker_map([1], status_dir=status_dir, include_ledger=False)
        violations = check_isolation_invariants(identities)
        assert any("status vjoy_device" in v for v in violations), violations


def test_detects_ledger_owner_mismatch():
    from ac_workers.worker_identity import (
        WorkerIdentity,
        check_isolation_invariants,
    )

    # Build a synthetic identity where the ledger says device 2 is owned by worker 0.
    ident = WorkerIdentity(
        worker_id=1,
        ego_port=2355,
        opponents_port=2356,
        mgmt_port=2357,
        diag_port=24101,
        vjoy_device=2,
        transition_port=24050,
        expected_policy_seed=10007,
        ledger_present=True,
        ledger_owner_pid=999,
        ledger_owner_worker_id=0,
    )
    violations = check_isolation_invariants([ident])
    assert any("owned in ledger by worker" in v for v in violations), violations


def test_detects_two_pids_on_same_device():
    from ac_workers.worker_identity import (
        WorkerIdentity,
        check_isolation_invariants,
    )

    a = WorkerIdentity(
        worker_id=0, ego_port=2345, opponents_port=2346, mgmt_port=2347,
        diag_port=24100, vjoy_device=1, transition_port=24050,
        expected_policy_seed=0, ledger_present=True, ledger_owner_pid=100,
        ledger_owner_worker_id=0,
    )
    # Same device id reported again under a different pid (corrupt/duplicate).
    b = WorkerIdentity(
        worker_id=0, ego_port=2345, opponents_port=2346, mgmt_port=2347,
        diag_port=24100, vjoy_device=1, transition_port=24050,
        expected_policy_seed=0, ledger_present=True, ledger_owner_pid=200,
        ledger_owner_worker_id=0,
    )
    violations = check_isolation_invariants([a, b])
    assert any("two pids" in v for v in violations), violations


def test_sequential_snapshots_accumulate_without_clear(tmp_path, monkeypatch):
    """Simulate C4D one-by-one launches: each worker snapshot must survive."""
    monkeypatch.setenv("AC_CONTROLS_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    from ac_workers import controls_snapshot as cs

    mapping = {0: 2, 1: 3, 2: 4, 3: 5, 4: 0, 5: 1}
    for wid, joy in mapping.items():
        controls = tmp_path / "controls_{0}.ini".format(wid)
        controls.write_text("[STEER]\nJOY={0}\n".format(joy), encoding="utf-8")
        cs.write_snapshot(wid, controls, detected_joy_index=joy)
        cs.record_ac_launch(wid, ac_pid=20000 + wid)
    for wid in range(6):
        meta = cs.load_meta(wid)
        assert meta.get("joy_index_written") == mapping[wid], "worker {0}".format(wid)


def test_snapshot_proof_fails_on_no_snapshot():
    from ac_workers.worker_identity import (
        WorkerIdentity,
        check_controls_snapshot_proof,
    )

    ident = WorkerIdentity(
        worker_id=0,
        ego_port=2345,
        opponents_port=2346,
        mgmt_port=2347,
        diag_port=24100,
        vjoy_device=1,
        transition_port=24050,
        expected_policy_seed=0,
        detected_joy_index=2,
        mapping_status="NO_SNAPSHOT",
    )
    problems = check_controls_snapshot_proof([ident])
    assert problems
    assert "NO_SNAPSHOT" in problems[0]


def test_snapshot_proof_ok_when_written_matches():
    from ac_workers.worker_identity import (
        WorkerIdentity,
        check_controls_snapshot_proof,
    )

    ident = WorkerIdentity(
        worker_id=0,
        ego_port=2345,
        opponents_port=2346,
        mgmt_port=2347,
        diag_port=24100,
        vjoy_device=1,
        transition_port=24050,
        expected_policy_seed=0,
        detected_joy_index=2,
        joy_index_written=2,
        mapping_status="OK(remapped)",
    )
    assert check_controls_snapshot_proof([ident]) == []


def test_format_worker_map_smoke():
    from ac_workers.worker_identity import build_worker_map, format_worker_map

    text = format_worker_map(build_worker_map(include_status=False, include_ledger=False))
    assert "worker identity map" in text
    for wid in range(6):
        assert "\n  {0} ".format(wid) in text or "  {0} ".format(wid) in text
