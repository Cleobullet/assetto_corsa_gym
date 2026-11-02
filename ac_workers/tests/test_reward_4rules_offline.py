"""Test hors-ligne de la récompense à 4 règles."""
import sys
import os
import numpy as np
from types import SimpleNamespace

REPO = r"C:\Leo projet\Assetto Corsa ai"
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "assetto_corsa_gym"))

from AssettoCorsaEnv.progress_reward import progress_reward, CleanLapTracker

TRACK = 10000.0  # m
cfg = SimpleNamespace(
    reward_progress_per_m=0.10, reward_time_per_sec=0.02, reward_collision_penalty=10.0
)


def st(frac, t, x, y, speed, done=False, reason=""):
    return {
        "timestamp_ac": t, "NormalizedSplinePosition": frac,
        "world_position_x": x, "world_position_y": y, "speed": speed,
        "done": done, "done_reason": reason, "teleported": False,
        "out_of_track": False, "going_backwards": 0.0,
    }


z = np.zeros(3)

# 1. Avancer 10 m en 1 s : +0.10*10 - 0.02*1 = +0.98
prev = st(0.0, 0.0, 0.0, 0.0, 10.0)
cur = st(0.001, 1.0, 10.0, 0.0, 10.0)
r = progress_reward(prev, cur, TRACK, cfg, z)
print("1. avancer 10m/1s : reward=%.4f (attendu ~+0.98)" % r["reward"])
assert abs(r["reward"] - 0.98) < 0.05, r["reward"]

# 2. Marche arrière (frac diminue) : progression à 0 (anti-exploitation).
prev = st(0.001, 0.0, 10.0, 0.0, 5.0)
cur = st(0.0, 1.0, 0.0, 0.0, 5.0)
r = progress_reward(prev, cur, TRACK, cfg, z)
print("2. marche arrière  : reward=%.4f (progression=%+.2f, attendu progression 0)" % (r["reward"], r["signed_progress_m"]))
assert r["reward_progress"] == 0.0, r["reward_progress"]

# 3. Collision / sortie de piste terminale : -10.
prev = st(0.001, 0.0, 10.0, 0.0, 8.0)
cur = st(0.0015, 1.0, 15.0, 0.0, 0.0, done=True, reason="out_of_track")
cur["out_of_track"] = True
r = progress_reward(prev, cur, TRACK, cfg, z)
print("3. collision       : reward=%.4f (collision=%s, attendu -10)" % (r["reward"], r["reward_collision_penalty"]))
assert r["reward_collision_penalty"] == -10.0, r

# 4. Tour complet valide : +20 (test direct du tracker de tour).
tracker = CleanLapTracker(lap_bonus=20.0)
frac = 0.0
for i in range(95):
    old = frac
    frac = (i + 1) * 0.01  # 0.01 ... 0.95
    delta = (frac - old) * TRACK  # 100 m par pas
    s = {"out_of_track": False, "going_backwards": 0.0, "NormalizedSplinePosition": frac}
    tracker.update(s, delta, True, TRACK)
# Franchissement de la ligne : frac 0.95 -> 0.01.
res = tracker.update(
    {"out_of_track": False, "going_backwards": 0.0, "NormalizedSplinePosition": 0.01},
    600.0, True, TRACK,
)
print("4. tour complet    : reward_clean_lap=%s completed=%s (attendu +20 / True)" % (res["reward_clean_lap"], res["completed_clean_lap"]))
assert res["completed_clean_lap"] is True and res["reward_clean_lap"] == 20.0, res

# 5. Tour fauté (hors-piste) : pas de bonus.
tracker = CleanLapTracker(lap_bonus=20.0)
frac = 0.0
for i in range(95):
    old = frac
    frac = (i + 1) * 0.01
    delta = (frac - old) * TRACK
    s = {"out_of_track": False, "going_backwards": 0.0, "NormalizedSplinePosition": frac}
    tracker.update(s, delta, True, TRACK)
res = tracker.update(
    {"out_of_track": True, "going_backwards": 0.0, "NormalizedSplinePosition": 0.01},
    600.0, True, TRACK,
)
print("5. tour fauté      : completed=%s (attendu False)" % res["completed_clean_lap"])
assert res["completed_clean_lap"] is False, res

print("\nOK: 4 règles vérifiées (progression, temps, collision, tour) + anti-exploitation.")
