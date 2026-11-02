"""Spawn slots and live scores, without Assetto Corsa."""
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "ac_workers"))
from spawn_points import spawn_fraction
import reward_live_search as live


def test_six_corners():
    rng = random.Random(1)
    seen = set()
    for wid in range(6):
        fixed = spawn_fraction(wid, randomize=False)
        assert abs(fixed - (wid + 0.5) / 6.0) < 1e-9
        rolled = spawn_fraction(wid, rng)
        assert wid / 6.0 <= rolled < (wid + 1) / 6.0
        seen.add(wid)
    assert seen == set(range(6))


def test_drive_score_prefers_forward():
    assert live.drive_score(100, 0, 0) > live.drive_score(20, 30, 4)
    samples = [(0.0, 10.0), (0.01, 10.0), (0.02, 10.0)]
    forward, reverse, idle = live.integrate(samples, 1000.0)
    assert forward > 0 and reverse == 0 and idle == 0
    jump = [(0.10, 5.0), (0.90, 5.0)]
    forward, reverse, idle = live.integrate(jump, 1000.0)
    assert forward == 0 and reverse == 0
    stuck = [(0.2, 10.0), (0.2, 10.0)]
    forward, reverse, idle = live.integrate(stuck, 1000.0)
    assert forward == 5.0 and idle == 0.0


def test_spawn_pose_rejects_unready_track():
    import sys
    from pathlib import Path

    plugin = Path(__file__).resolve().parents[1] / "assetto_corsa_gym" / "AssettoCorsaPlugin" / "plugins" / "sensors_par"
    sys.path.insert(0, str(plugin))
    from spawn_pose import spawn_pose

    assert spawn_pose((0.0, 0.0, 0.0), (12.0, 0.0, 0.0)) is None
    pose = spawn_pose((100.0, 2.0, 40.0), (112.0, 2.5, 40.0))
    assert pose is not None
    pos, direction = pose
    assert abs(pos[1] - 2.4) < 1e-6
    assert abs(direction[1]) < 1e-9
    assert abs((direction[0] ** 2 + direction[2] ** 2) - 1.0) < 1e-6


def test_search_drive_flag_clears():
    from reward_search_drive import search_drive_active, set_search_drive

    set_search_drive(True)
    try:
        assert search_drive_active(now=10_000.0)
    finally:
        set_search_drive(False)
    assert not search_drive_active(now=20_000.0)


if __name__ == "__main__":
    test_six_corners()
    test_drive_score_prefers_forward()
    test_search_drive_flag_clears()
    test_spawn_pose_rejects_unready_track()
    print("PASS: six spawn slots and live drive score")
