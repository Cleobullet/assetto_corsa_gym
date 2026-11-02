"""Score random reward settings on the six running workers.

Each trial applies one parameter set, teleports every car to its sixth of the
lap, lets them drive, then scores the real movement. The best set is kept for
a later training run.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "ac_workers"))

import reward_param_search as search
from collector_resume import request_collector_resume
from reward_search_drive import set_search_drive
from spawn_points import spawn_fraction

STATUS = REPO / "ac_workers" / "status"
OVERRIDE = STATUS / "reward_override.json"
BEST = REPO / "outputs" / "reward_search" / "best.json"
TRIALS = 6
TRIAL_SEC = 30
SETTLE_SEC = 3


def mgmt_port(worker_id):
    return 2345 + int(worker_id) * 10 + 2


def send_spawn(worker_id, fraction):
    payload = "spawn {0:.4f}".format(float(fraction)).encode()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(2.0)
        sock.connect(("127.0.0.1", mgmt_port(worker_id)))
        sock.sendall(payload)


def read_worker(worker_id):
    path = STATUS / "worker_{0}.json".format(int(worker_id))
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    snap = data.get("telemetry_snapshot") or {}
    try:
        age = float(data.get("telemetry_age_ms", 999999))
        spline = float(snap.get("spline", 0.0) or 0.0)
        speed = float(snap.get("speed", 0.0) or 0.0)
    except (TypeError, ValueError):
        return None
    return {"age_ms": age, "spline": spline, "speed": speed}


def workers_ready():
    rows = []
    for wid in range(6):
        row = read_worker(wid)
        if row is None or row["age_ms"] > 3000:
            return False, rows
        rows.append(row)
    return True, rows


def splines_spread(min_gap=0.05):
    values = []
    for wid in range(6):
        row = read_worker(wid)
        if row is None:
            return False
        values.append(row["spline"])
    return max(values) - min(values) >= float(min_gap)


def separate_cars():
    for wid in range(6):
        try:
            send_spawn(wid, spawn_fraction(wid, randomize=False))
        except OSError as exc:
            print("worker {0}: teleport non envoyé ({1})".format(wid, exc), file=sys.stderr)


def cars_moving(min_workers=2, min_speed=1.5):
    moving = 0
    for wid in range(6):
        row = read_worker(wid)
        if row is not None and row["age_ms"] <= 3000 and row["speed"] >= min_speed:
            moving += 1
    return moving >= min_workers


def wait_until_moving(seconds):
    deadline = time.monotonic() + float(seconds)
    while time.monotonic() < deadline:
        if cars_moving():
            return True
        time.sleep(0.5)
    return False


def write_override(params):
    STATUS.mkdir(parents=True, exist_ok=True)
    payload = {"params": search.public_params(params)}
    tmp = OVERRIDE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(OVERRIDE)


def clear_override():
    try:
        OVERRIDE.unlink()
    except OSError:
        pass


def sample_params(current, rng):
    params = search.normalize(current)
    for _ in range(4):
        params = search._mutate(params, rng)
    return params


def integrate(samples, track_m, dt=0.5):
    """samples: list of (spline, speed_mps) about twice a second."""
    forward = reverse = idle = 0.0
    for prev, nxt in zip(samples, samples[1:]):
        delta = ((nxt[0] - prev[0] + 0.5) % 1.0 - 0.5) * float(track_m)
        speed = abs(float(nxt[1]))
        if abs(delta) > 80.0:
            continue
        # Older collectors do not publish the spline. Speed still shows movement.
        if abs(delta) < 0.05 and speed > 1.0:
            delta = speed * float(dt)
        if delta >= 0.0:
            forward += delta
        else:
            reverse += -delta
        if speed * 3.6 < 3.0:
            idle += float(dt)
    return forward, reverse, idle


def drive_score(forward_m, reverse_m, idle_s):
    return float(forward_m) - float(reverse_m) - 5.0 * float(idle_s)


def run_trial(params, track_m):
    write_override(params)
    for wid in range(6):
        try:
            send_spawn(wid, spawn_fraction(wid, randomize=False))
        except OSError as exc:
            print("worker {0}: teleport non envoyé ({1})".format(wid, exc), file=sys.stderr)
    time.sleep(SETTLE_SEC)
    series = {wid: [] for wid in range(6)}
    deadline = time.monotonic() + TRIAL_SEC
    while time.monotonic() < deadline:
        for wid in range(6):
            row = read_worker(wid)
            if row is None:
                continue
            series[wid].append((row["spline"], row["speed"]))
        time.sleep(0.5)
    forward = reverse = idle = 0.0
    for wid in range(6):
        f_m, r_m, idle_s = integrate(series[wid], track_m)
        forward += f_m
        reverse += r_m
        idle += idle_s
    value = drive_score(forward, reverse, idle)
    detail = "avance {0:.0f} m | recule {1:.0f} m | arrêt {2:.0f} s".format(forward, reverse, idle)
    return value, detail, forward


def build_live_report(trials=TRIALS):
    ready, _ = workers_ready()
    if not ready:
        raise SystemExit("Les 6 workers doivent déjà rouler, avec une télémétrie récente.")
    current, track_m = search.load_config(REPO / "config_first_sac_cpu_v3.yml")
    rng = np.random.default_rng()
    rows = []
    set_search_drive(True)
    for wid in range(6):
        request_collector_resume(wid)
    try:
        separate_cars()
        time.sleep(3)
        if not splines_spread():
            raise SystemExit(
                "Les 6 voitures sont au même endroit et se bloquent. Relance les 6 workers, puis relance la recherche."
            )
        if not wait_until_moving(8):
            print("BESOIN_APPRENTISSAGE", file=sys.stderr, flush=True)
            if not wait_until_moving(50):
                raise SystemExit(
                    "Les voitures n'ont pas bougé. Relance les 6 workers une fois, puis relance la recherche."
                )
        for index in range(1, trials + 1):
            params = sample_params(current, rng) if index > 1 else search.normalize(current)
            print("Essai {0}/{1}".format(index, trials), file=sys.stderr, flush=True)
            value, detail, forward = run_trial(params, track_m)
            if index == 1 and forward < 15.0:
                raise SystemExit(
                    "Les voitures n'ont presque pas avancé ({0:.0f} m). Relance les 6 workers, puis relance la recherche.".format(forward)
                )
            rows.append({
                "rank": 0,
                "score": round(value, 1),
                "is_current": index == 1,
                "params": search.public_params(params),
                "detail": detail,
            })
    finally:
        clear_override()
        set_search_drive(False)
    rows.sort(key=lambda row: row["score"], reverse=True)
    for index, row in enumerate(rows, start=1):
        row["rank"] = index
    best = rows[0]
    BEST.parent.mkdir(parents=True, exist_ok=True)
    BEST.write_text(json.dumps(best, indent=2), encoding="utf-8")
    summary = (
        "Meilleur essai : {0}. Ces paramètres sont gardés pour un entraînement plus tard. "
        "Applique la ligne, puis crée un nouvel entraînement à mémoire vide."
    ).format(best["detail"])
    return {
        "summary": summary,
        "track_m": round(float(track_m), 1),
        "current_score": next(row["score"] for row in rows if row["is_current"]),
        "best_score": best["score"],
        "improved": best["score"] > next(row["score"] for row in rows if row["is_current"]),
        "rows": rows,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=TRIALS)
    parser.add_argument("--config", default="")
    args = parser.parse_args(argv)
    json.dump(build_live_report(max(1, min(8, args.trials))), sys.stdout, ensure_ascii=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
