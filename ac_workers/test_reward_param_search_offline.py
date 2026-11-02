"""The reward search prefers clear driving signals over empty or extreme weights."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "ac_workers"))
import reward_param_search as search


CURRENT = {
    "reward_progress_per_m": 0.1,
    "reward_offtrack_per_step": 0.3,
    "reward_smoothness_coef": 0.005,
    "reward_step_cost": 0.002,
    "reward_idle_speed_kmh": 3,
    "reward_n_sectors": 4,
    "reward_clean_sector_bonus": 10,
    "reward_clean_lap_bonus": 100,
    "reward_clean_lap_time_ref_sec": 150,
    "reward_oot_terminal_penalty": 4,
    "reward_idle_penalty": 0.08,
}
TRACK = 5758.66


def test_order_and_search():
    totals = search.behavior_totals(CURRENT, TRACK)
    assert totals["fast_clean"] > totals["slow_clean"] > totals["idle"]
    assert totals["slow_clean"] > totals["reverse"]
    assert totals["fast_clean"] > totals["offtrack"]
    assert totals["offtrack_step"] > totals["crash"]
    assert totals["fast_clean"] > totals["jerky"]

    zeros = {key: 0.0 for key in search.KEYS}
    zeros["reward_clean_lap_time_ref_sec"] = 150
    zeros["reward_n_sectors"] = 4
    assert search.score(CURRENT, TRACK, CURRENT) > search.score(zeros, TRACK, CURRENT)

    huge = dict(CURRENT)
    huge["reward_oot_terminal_penalty"] = 5000
    huge["reward_clean_lap_bonus"] = 5000
    huge["reward_progress_per_m"] = 0
    assert search.score(CURRENT, TRACK, CURRENT) > search.score(huge, TRACK, CURRENT)

    weak = dict(CURRENT)
    weak["reward_clean_lap_bonus"] = 250
    weak["reward_clean_sector_bonus"] = 40
    weak["reward_offtrack_per_step"] = 0.05
    moved = search.build_report(weak, TRACK)
    assert moved["improved"]
    assert moved["rows"][0]["params"]["reward_clean_lap_bonus"] < 250

    report = search.build_report(CURRENT, TRACK)
    assert report["best_score"] + 1e-9 >= report["current_score"]
    assert report["rows"][0]["is_current"]
    assert report["rows"][0]["score"] == report["best_score"]
    best = report["rows"][0]["params"]
    assert set(best) == set(search.KEYS)
    for key, (low, high) in search.BOUNDS.items():
        assert low - 1e-9 <= float(best[key]) <= high + 1e-9
    assert 2 <= int(best["reward_n_sectors"]) <= 8
    found = search.behavior_totals(best, TRACK)
    assert found["fast_clean"] > found["offtrack"]
    assert found["offtrack_step"] > found["crash"]
    assert found["fast_clean"] > 0 > found["idle"]


if __name__ == "__main__":
    test_order_and_search()
    print("PASS: reward search ranks clear settings above empty and extreme ones")
