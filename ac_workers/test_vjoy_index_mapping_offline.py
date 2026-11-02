"""Offline tests for the detected vJoy device -> JOY index mapping.

These cover the pure logic that decides whether a probed mapping is a trustworthy
bijection (and is therefore allowed to drive controls.ini), plus the persistence /
loader round-trip and the gear_shift guard rail. No vJoy hardware or live game is
touched, so they run anywhere.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "assetto_corsa_gym") not in sys.path:
    sys.path.insert(0, str(REPO / "assetto_corsa_gym"))

from ac_workers import vjoy_index_probe as vip

DEVICES = [1, 2, 3, 4, 5, 6]


def test_merge_complete_bijection_from_probe():
    probe = {1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}
    res = vip._merge_and_validate(probe, devices=DEVICES, prior=None)
    assert res["complete"] is True
    assert res["status"] == "ok"
    assert res["device_to_index"] == probe
    assert all(v == "probed" for v in res["methods"].values())


def test_merge_remapped_bijection_is_ok():
    # Real machine: enumeration order permuted vs device id. Still a valid bijection.
    probe = {1: 2, 2: 3, 3: 4, 4: 5, 5: 0, 6: 1}
    res = vip._merge_and_validate(probe, devices=DEVICES, prior=None)
    assert res["complete"] is True
    assert res["device_to_index"] == probe


def test_merge_fills_busy_device_from_prior():
    # device 1 busy (owned by a running collector) -> filled from prior session map.
    probe = {2: 1, 3: 2, 4: 3, 5: 4, 6: 5}
    prior = {1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}
    res = vip._merge_and_validate(probe, devices=DEVICES, prior=prior)
    assert res["complete"] is True
    assert res["device_to_index"][1] == 0
    assert res["methods"][1] == "prior"


def test_merge_elimination_single_missing():
    # device 1 not probed, no prior; exactly one index (0) left -> inferable.
    probe = {2: 1, 3: 2, 4: 3, 5: 4, 6: 5}
    res = vip._merge_and_validate(probe, devices=DEVICES, prior=None)
    assert res["complete"] is True
    assert res["device_to_index"][1] == 0
    assert res["methods"][1] == "elimination"


def test_merge_ambiguous_duplicate_index_flagged():
    # Two devices claim the same index -> not a bijection, must be rejected.
    probe = {1: 0, 2: 0, 3: 2, 4: 3, 5: 4, 6: 5}
    res = vip._merge_and_validate(probe, devices=DEVICES, prior=None)
    assert res["complete"] is False
    assert any("claimed by" in p for p in res["problems"])


def test_merge_incomplete_multiple_missing_flagged():
    # Two devices missing, no prior -> cannot infer, must be rejected.
    probe = {3: 2, 4: 3, 5: 4, 6: 5}
    res = vip._merge_and_validate(probe, devices=DEVICES, prior=None)
    assert res["complete"] is False
    assert any("no index detected" in p for p in res["problems"])


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    payload = {
        "schema": vip.MAPPING_SCHEMA,
        "device_to_index": {"1": 2, "2": 3, "3": 4, "4": 5, "5": 0, "6": 1},
        "status": "ok",
    }
    latest = tmp_path / "vjoy_mapping_latest.json"
    vip._atomic_write_json(latest, payload)
    monkeypatch.setattr(vip, "MAPPING_LATEST", latest)
    loaded = vip.load_saved_mapping()
    assert loaded == {1: 2, 2: 3, 3: 4, 4: 5, 5: 0, 6: 1}
    assert vip.detected_index_for_device(1) == 2
    assert vip.detected_index_for_device(5) == 0
    assert vip.detected_index_for_device(99) is None


def test_load_missing_mapping_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(vip, "MAPPING_LATEST", tmp_path / "nope.json")
    assert vip.load_saved_mapping() is None
    assert vip.detected_index_for_device(1) is None


def test_gear_shift_uses_detected_index_from_file(tmp_path, monkeypatch):
    import configparser

    import AssettoCorsaEnv.gear_shift as gs

    mapping = tmp_path / "vjoy_mapping_latest.json"
    mapping.write_text(
        json.dumps({"device_to_index": {"1": 2, "2": 3, "3": 4}}), encoding="utf-8"
    )
    monkeypatch.setattr(gs, "VJOY_MAPPING_PATH", mapping)
    # worker 1 -> device 2 -> detected index 3.
    assert gs.detected_joy_index_for_device(2) == 3

    controls = tmp_path / "controls.ini"
    controls.write_text(
        "[CONTROLLERS]\nCON0=vJoy Device\nPGUID0=4C035200-7051-11EB-8002-444553540000\n"
        "[STEER]\nJOY=0\nAXLE=0\n[GEARUP]\nJOY=0\nBUTTON=0\n",
        encoding="utf-8",
    )
    gs.patch_controls_vjoy_joy_index(controls, 1)
    parser = configparser.ConfigParser()
    parser.optionxform = str
    parser.read_string(controls.read_text(encoding="utf-8"))
    assert parser.get("STEER", "JOY").strip() == "3"
    assert parser.get("CONTROLLERS", "CON3").strip() == "vJoy Device"


def main() -> int:
    import tempfile

    test_merge_complete_bijection_from_probe()
    test_merge_remapped_bijection_is_ok()
    test_merge_fills_busy_device_from_prior()
    test_merge_elimination_single_missing()
    test_merge_ambiguous_duplicate_index_flagged()
    test_merge_incomplete_multiple_missing_flagged()
    print("Test-VjoyIndexMapping-Offline: OK (merge/validate)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
