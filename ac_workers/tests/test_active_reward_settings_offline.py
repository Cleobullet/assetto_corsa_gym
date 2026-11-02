"""Every reward offered by the Windows editor changes the active calculation."""
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))
from AssettoCorsaEnv.progress_reward import progress_reward, CleanLapTracker


def reward(**overrides):
    previous = dict(timestamp_ac=1., NormalizedSplinePosition=.2,
                    world_position_x=0., world_position_y=0., speed=2.)
    state = dict(previous, timestamp_ac=1.04, NormalizedSplinePosition=.20008,
                 world_position_x=.08, out_of_track=True,
                 done=True, done_reason="out_of_track")
    config = dict(reward_progress_per_m=.1, reward_offtrack_per_step=.3,
                  reward_smoothness_coef=.005, reward_step_cost=.002,
                  reward_idle_speed_kmh=8., reward_idle_penalty=.08,
                  reward_oot_terminal_penalty=4.)
    on_track = overrides.pop("on_track", False)
    if on_track:
        state.update(out_of_track=False, done=False, done_reason="")
    config.update(overrides)
    result = progress_reward(previous, state, 1000., SimpleNamespace(**config), np.array([.2, .3, .1]))
    assert result["progress_valid"]
    return result


def lap(n=4, sector=10., bonus=100., reference=150.):
    tracker = CleanLapTracker(n, sector, bonus, reference)
    total_sector = total_lap = completed = 0
    for i in range(101):
        result = tracker.update(dict(NormalizedSplinePosition=(i % 100) / 100,
                                     timestamp_ac=i * 2), 0 if i == 0 else 1., True, 100.)
        total_sector += result["reward_clean_sector"]
        total_lap += result["reward_clean_lap"]
        completed += int(result["completed_clean_sector"])
    return total_sector, total_lap, completed


def test_all_active_fields():
    a = reward()
    for key, component, value in [
        ("reward_offtrack_per_step", "reward_oot_penalty", .6),
        ("reward_oot_terminal_penalty", "reward_oot_penalty", 8),
        ("reward_smoothness_coef", "reward_smoothness_penalty", .01),
        ("reward_step_cost", "reward_step_cost", .004),
        ("reward_idle_penalty", "reward_idle_penalty", .16),
        ("reward_idle_speed_kmh", "reward_idle_penalty", 3),
    ]:
        b = reward(**{key: value})
        assert b[component] != a[component] and b["reward"] != a["reward"], key
    assert reward(on_track=True, reward_progress_per_m=.2)["reward_progress"] > reward(on_track=True)["reward_progress"]
    base = lap()
    assert base[0] > 0 and base[1] > 0
    assert lap(n=8)[2] > base[2]
    assert lap(sector=20)[0] == base[0] * 2
    assert lap(bonus=200)[1] == base[1] * 2
    assert lap(reference=75)[1] < base[1]
    # Keep this check in sync with the actual UI catalog, including future additions.
    import re
    ui = (REPO / "desktop/AcAiControl/RewardConfig.cs").read_text(encoding="utf-8-sig")
    keys = set(re.findall(r'^        \("(reward_[^"]+)"', ui, flags=re.M))
    assert keys == {"reward_progress_per_m", "reward_offtrack_per_step", "reward_oot_terminal_penalty",
                    "reward_smoothness_coef", "reward_step_cost", "reward_idle_penalty", "reward_idle_speed_kmh",
                    "reward_n_sectors", "reward_clean_sector_bonus", "reward_clean_lap_bonus", "reward_clean_lap_time_ref_sec"}


if __name__ == "__main__":
    test_all_active_fields()
    print("PASS: all 11 visible reward settings affect the active reward or milestones")
