"""Search reward and penalty weights on fixed driving situations.

Uses the same progress_reward function as the game. It does not drive the car
and does not change the YAML. The Windows tab applies a chosen result later.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))
from AssettoCorsaEnv.progress_reward import progress_reward  # noqa: E402

KEYS = (
    "reward_progress_per_m",
    "reward_offtrack_per_step",
    "reward_smoothness_coef",
    "reward_step_cost",
    "reward_idle_speed_kmh",
    "reward_n_sectors",
    "reward_clean_sector_bonus",
    "reward_clean_lap_bonus",
    "reward_clean_lap_time_ref_sec",
    "reward_oot_terminal_penalty",
    "reward_idle_penalty",
)

BOUNDS = {
    "reward_progress_per_m": (0.02, 0.40),
    "reward_offtrack_per_step": (0.05, 1.20),
    "reward_smoothness_coef": (0.0, 0.04),
    "reward_step_cost": (0.0, 0.01),
    "reward_idle_speed_kmh": (2.0, 12.0),
    "reward_n_sectors": (2.0, 8.0),
    "reward_clean_sector_bonus": (1.0, 40.0),
    "reward_clean_lap_bonus": (10.0, 250.0),
    "reward_clean_lap_time_ref_sec": (110.0, 180.0),
    "reward_oot_terminal_penalty": (1.0, 20.0),
    "reward_idle_penalty": (0.02, 0.40),
}

DT = 0.04
STEPS = 25
FALLBACK_TRACK_M = 5758.66064453125


def clamp(key, value):
    lo, hi = BOUNDS[key]
    value = min(hi, max(lo, float(value)))
    if key == "reward_n_sectors":
        return float(int(round(value)))
    return float(value)


def normalize(params):
    return {key: clamp(key, params.get(key, BOUNDS[key][0])) for key in KEYS}


def _pair(track_m, t0, fraction, delta_m, speed_mps, **flags):
    new_fraction = fraction + float(delta_m) / float(track_m)
    previous = {
        "timestamp_ac": t0,
        "NormalizedSplinePosition": fraction,
        "world_position_x": 0.0,
        "world_position_y": 0.0,
        "speed": speed_mps,
        "done": False,
    }
    state = {
        "timestamp_ac": t0 + DT,
        "NormalizedSplinePosition": new_fraction,
        "world_position_x": float(delta_m),
        "world_position_y": 0.0,
        "speed": speed_mps,
        "done": False,
        "out_of_track": False,
    }
    state.update(flags)
    return previous, state


def _clip(track_m, speed_kmh, delta_sign=1.0, **flags):
    speed_mps = speed_kmh / 3.6
    delta = delta_sign * speed_mps * DT
    fraction = 0.20
    steps = []
    for i in range(STEPS):
        steps.append(_pair(track_m, i * DT, fraction, delta, speed_mps, **flags))
        fraction += delta / track_m
    return steps


def situations(track_m):
    off_step = _pair(track_m, 0.0, 0.20, 0.5, 15.0, out_of_track=True)
    crash_prev, crash_state = _pair(
        track_m, 0.0, 0.20, 0.5, 15.0,
        out_of_track=True, done=True, done_reason="out_of_track",
    )
    return {
        "fast_clean": _clip(track_m, 120.0),
        "slow_clean": _clip(track_m, 25.0),
        "idle": _clip(track_m, 1.0),
        "reverse": _clip(track_m, 30.0, delta_sign=-1.0),
        "offtrack": _clip(track_m, 80.0, out_of_track=True),
        "offtrack_step": [off_step],
        "jerky": _clip(track_m, 120.0),
        "crash": [(crash_prev, crash_state)],
    }


def _actions(name):
    if name == "jerky":
        return np.array([1.5, 1.5, 1.5])
    return np.zeros(3)


def behavior_totals(params, track_m):
    cfg = SimpleNamespace(**normalize(params))
    totals = {}
    for name, steps in situations(track_m).items():
        actions = _actions(name)
        total = 0.0
        for previous, state in steps:
            total += float(progress_reward(previous, state, track_m, cfg, actions)["reward"])
        totals[name] = total
    return totals


def _band(value, low, high):
    if low <= value <= high:
        return 0.45
    return -0.7


def _drift(clean, origin):
    if origin is None:
        return 0.0
    origin = normalize(origin)
    total = 0.0
    for key in KEYS:
        low, high = BOUNDS[key]
        total += abs(clean[key] - origin[key]) / max(high - low, 1e-6)
    return total


def score_parts(params, track_m, origin=None):
    clean = normalize(params)
    totals = behavior_totals(clean, track_m)
    gaps = (
        totals["fast_clean"] - totals["slow_clean"],
        totals["slow_clean"] - totals["idle"],
        totals["slow_clean"] - totals["reverse"],
        totals["fast_clean"] - totals["offtrack"],
        totals["fast_clean"] - totals["jerky"],
        totals["offtrack_step"] - totals["crash"],
    )
    order = sum(math.tanh(gap / 0.20) for gap in gaps)
    fast = totals["fast_clean"]
    if 0.4 <= fast <= 5.0:
        scale = 0.5
    elif fast < 0.4:
        scale = -1.2
    else:
        scale = -min(1.6, (fast - 5.0) / 5.0)
    idle = 0.35 if totals["idle"] < -0.05 else -0.9
    ratio = abs(totals["crash"]) / max(fast, 1e-6)
    crash = 0.55 if 1.0 <= ratio <= 12.0 else -0.8
    jerk = 0.35 if totals["fast_clean"] > totals["jerky"] > totals["offtrack"] else -0.55
    lap_progress = clean["reward_progress_per_m"] * float(track_m)
    lap_ratio = clean["reward_clean_lap_bonus"] / max(lap_progress, 1e-6)
    sector_ratio = (
        clean["reward_clean_sector_bonus"] * clean["reward_n_sectors"]
    ) / max(lap_progress, 1e-6)
    shaping = _band(lap_ratio, 0.08, 0.35) + _band(sector_ratio, 0.04, 0.20)
    ref = clean["reward_clean_lap_time_ref_sec"]
    reference = 0.2 if 120.0 <= ref <= 170.0 else 0.0
    # Stay on the current weights unless a change clearly improves the situations.
    total = order + scale + idle + crash + jerk + shaping + reference - (0.45 * _drift(clean, origin))
    return total, totals, clean


def score(params, track_m, origin=None):
    return score_parts(params, track_m, origin)[0]


def _mutate(parent, rng):
    child = dict(parent)
    keys = list(KEYS)
    rng.shuffle(keys)
    for key in keys[: int(rng.integers(2, 5))]:
        lo, hi = BOUNDS[key]
        if key == "reward_n_sectors":
            child[key] = clamp(key, child[key] + float(rng.choice([-1.0, 1.0])))
            continue
        span = hi - lo
        if lo == 0.0:
            child[key] = clamp(key, child[key] + float(rng.normal(0.0, span * 0.12)))
        else:
            factor = float(math.exp(rng.normal(0.0, 0.22)))
            child[key] = clamp(key, child[key] * factor)
    return child


def _signature(params):
    clean = normalize(params)
    return tuple(round(clean[key], 5) if key != "reward_n_sectors" else int(clean[key]) for key in KEYS)


def search(current, track_m, generations=8, population=18, seed=1):
    rng = np.random.default_rng(seed)
    start = normalize(current)
    people = [start]
    for key in KEYS:
        lo, hi = BOUNDS[key]
        up = dict(start)
        down = dict(start)
        if key == "reward_n_sectors":
            up[key] = clamp(key, start[key] + 1)
            down[key] = clamp(key, start[key] - 1)
        else:
            up[key] = clamp(key, min(hi, start[key] * 1.35 + (hi - lo) * 0.02))
            down[key] = clamp(key, max(lo, start[key] * 0.7))
        people.append(up)
        people.append(down)
    while len(people) < population:
        people.append(_mutate(start, rng))

    def ranked(items):
        return sorted(items, key=lambda item: score(item, track_m, start), reverse=True)

    for _ in range(generations):
        elites = ranked(people)[:4]
        people = list(elites)
        while len(people) < population:
            parent = elites[int(rng.integers(0, len(elites)))]
            people.append(_mutate(parent, rng))
    ordered = ranked(people)
    unique = []
    seen = set()
    for item in ordered:
        sig = _signature(item)
        if sig in seen:
            continue
        seen.add(sig)
        unique.append(normalize(item))
        if len(unique) == 6:
            break
    return unique


def _round_param(key, value):
    if key == "reward_n_sectors":
        return int(round(value))
    if key == "reward_idle_speed_kmh":
        return round(float(value), 1)
    rounded = round(float(value), 4)
    if abs(rounded - round(rounded)) < 1e-9:
        return int(round(rounded))
    return rounded


def public_params(params):
    clean = normalize(params)
    return {key: _round_param(key, clean[key]) for key in KEYS}


def _same(a, b):
    left = public_params(a)
    right = public_params(b)
    return all(left[key] == right[key] for key in KEYS)


def _detail(totals):
    labels = (
        ("fast_clean", "vite"),
        ("slow_clean", "lent"),
        ("idle", "arrêt"),
        ("reverse", "marche arrière"),
        ("offtrack", "hors piste"),
        ("jerky", "saccadé"),
        ("crash", "sortie"),
    )
    return " | ".join(f"{label} {totals[key]:+.2f}" for key, label in labels)


def build_report(current, track_m):
    current_score, _, current_clean = score_parts(current, track_m, current)
    rows_params = search(current_clean, track_m)
    if not any(_same(item, current_clean) for item in rows_params):
        rows_params.append(current_clean)
    decorated = []
    for params in rows_params:
        value, totals, clean = score_parts(params, track_m, current_clean)
        decorated.append((value, clean, totals))
    decorated.sort(key=lambda item: item[0], reverse=True)
    best_score, best_params, _ = decorated[0]
    improved = best_score > current_score + 0.05 and not _same(best_params, current_clean)
    if improved:
        summary = (
            "Une combinaison ressort mieux sur les situations testées. "
            "Elle ne change la conduite qu’après un nouvel entraînement avec une mémoire vide."
        )
    else:
        summary = (
            "Les réglages actuels sont déjà dans la meilleure zone testée. "
            "Les modifier encore à la main ne change presque rien : le modèle garde ses habitudes "
            "tant qu’un nouvel entraînement à mémoire vide n’a pas recommencé."
        )
    rows = []
    for index, (value, clean, totals) in enumerate(decorated[:6], start=1):
        rows.append({
            "rank": index,
            "score": round(value, 3),
            "is_current": _same(clean, current_clean),
            "params": public_params(clean),
            "detail": _detail(totals),
        })
    return {
        "summary": summary,
        "track_m": round(float(track_m), 1),
        "current_score": round(current_score, 3),
        "best_score": round(best_score, 3),
        "improved": bool(improved),
        "rows": rows,
    }


def load_config(path):
    with open(path, encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    section = data["AssettoCorsa"]
    params = {key: float(section[key]) for key in KEYS}
    track = str(section.get("track") or "monza")
    tracks_path = REPO / "assetto_corsa_gym" / "AssettoCorsaConfigs" / "tracks" / "config.yaml"
    track_m = FALLBACK_TRACK_M
    if tracks_path.exists():
        with open(tracks_path, encoding="utf-8") as handle:
            tracks = yaml.safe_load(handle) or {}
        entry = tracks.get(track) or {}
        if entry.get("TrackLength"):
            track_m = float(entry["TrackLength"])
    return params, track_m


def main(argv=None):
    parser = argparse.ArgumentParser(description="Cherche des récompenses et des malus plus clairs.")
    parser.add_argument("--config", default=str(REPO / "config_first_sac_cpu_v3.yml"))
    args = parser.parse_args(argv)
    params, track_m = load_config(args.config)
    json.dump(build_report(params, track_m), sys.stdout, ensure_ascii=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
