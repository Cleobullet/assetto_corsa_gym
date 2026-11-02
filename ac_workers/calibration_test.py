#!/usr/bin/env python
"""vJoy + car-command calibration test.

For each worker, drives a scripted sequence and VERIFIES every action through
the live telemetry (speed / world position / steer angle) read back via the
authoritative ``diagnostic_vjoy_pulse`` channel (the same writer the collector
uses, bypassing policy/learner/replay):

  1. DRIVE forward ~2 m  (throttle) -> speed rose AND position moved >= 1.5 m
  2. BRAKE hard          (brake)    -> speed dropped toward 0
  3. STEER LEFT          (steer<0)  -> steer angle went negative (left)
  4. STEER RIGHT         (steer>0)  -> steer angle went positive (right)

A worker passes only if ALL four actions verify; otherwise it reports which
action failed and the measured deltas. Workers run SEQUENTIALLY (a worker that
passes hands over to the next).
"""
from __future__ import annotations

import argparse
import math
import sys
import time

from ac_workers.identity_test import _diag_pulse, _identity_probe
from ac_workers.collector_diag import CollectorDiagClient

DRIVE_MIN_MOVE_M = 1.5
DRIVE_MIN_SPEED_MS = 0.5
BRAKE_SPEED_DROP_MS = 1.0
BRAKE_STOPPED_MS = 0.5
STEER_MIN_DELTA = 0.05

READY_PHASES = ("idle", "running")


def _delta(before, after, key):
    return float(after.get(key, 0.0) or 0.0) - float(before.get(key, 0.0) or 0.0)


def _pos_move(before, after):
    dx = _delta(before, after, "pos_x")
    dy = _delta(before, after, "pos_y")
    dz = _delta(before, after, "pos_z")
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def _pulse(wid, payload):
    resp = _diag_pulse(wid, dict(payload))
    return resp, (resp.get("telemetry_before") or {}), (resp.get("telemetry_after") or {})


def run_calibration(wid):
    results = []

    # 1. drive forward ~2 m (long hold: the car must engage 1st gear first)
    resp, before, after = _pulse(wid, {"steer": 0.0, "throttle": 1.0, "brake": 0.0, "hold_sec": 6.0})
    move = _pos_move(before, after)
    speed_after = float(after.get("speed", 0.0) or 0.0)
    ok = bool(resp.get("write_ok")) and move >= DRIVE_MIN_MOVE_M and speed_after >= DRIVE_MIN_SPEED_MS
    results.append(("drive", ok, {"move_m": round(move, 2), "speed_ms": round(speed_after, 2)}))
    time.sleep(0.3)

    # 2. brake hard
    resp, before, after = _pulse(wid, {"steer": 0.0, "throttle": 0.0, "brake": 1.0, "hold_sec": 2.5})
    speed_before = float(before.get("speed", 0.0) or 0.0)
    speed_after = float(after.get("speed", 0.0) or 0.0)
    speed_drop = speed_before - speed_after
    ok = bool(resp.get("write_ok")) and (speed_drop >= BRAKE_SPEED_DROP_MS or speed_after <= BRAKE_STOPPED_MS)
    results.append(("brake", ok, {"speed_before": round(speed_before, 2), "speed_after": round(speed_after, 2)}))
    time.sleep(0.3)

    # 3. steer left
    resp, before, after = _pulse(wid, {"steer": -0.6, "throttle": 0.0, "brake": 0.0, "hold_sec": 1.0})
    sd = _delta(before, after, "steer")
    ok = bool(resp.get("write_ok")) and sd <= -STEER_MIN_DELTA
    results.append(("steer_left", ok, {"steer_before": round(float(before.get("steer", 0.0) or 0.0), 3),
                                       "steer_after": round(float(after.get("steer", 0.0) or 0.0), 3),
                                       "delta": round(sd, 3)}))
    time.sleep(0.3)

    # 4. steer right
    resp, before, after = _pulse(wid, {"steer": 0.6, "throttle": 0.0, "brake": 0.0, "hold_sec": 1.0})
    sd = _delta(before, after, "steer")
    ok = bool(resp.get("write_ok")) and sd >= STEER_MIN_DELTA
    results.append(("steer_right", ok, {"steer_before": round(float(before.get("steer", 0.0) or 0.0), 3),
                                        "steer_after": round(float(after.get("steer", 0.0) or 0.0), 3),
                                        "delta": round(sd, 3)}))
    return results


def _cal_enter(wid):
    try:
        return CollectorDiagClient(wid, timeout=5).request("calibration_enter")
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _cal_exit(wid):
    try:
        return CollectorDiagClient(wid, timeout=5).request("calibration_exit")
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def run_all(worker_ids):
    report = {}
    for wid in worker_ids:
        ident = _identity_probe(wid)
        phase = ident.phase
        if phase not in READY_PHASES:
            report[wid] = {"phase": phase, "ok": False, "results": []}
            continue
        # Enter real calibration mode: the collector stops writing BOTH the
        # policy action and NEUTRAL, so the test has EXCLUSIVE vJoy control and
        # an acceleration/steer stays applied for the whole hold. `calibration_exit`
        # (in finally) always neutralizes, even on error.
        _cal_enter(wid)
        try:
            results = run_calibration(wid)
            all_ok = all(r[1] for r in results)
            report[wid] = {"phase": phase, "ok": all_ok,
                           "results": [{"action": n, "ok": o, "data": d} for n, o, d in results]}
        except Exception as exc:
            report[wid] = {"phase": phase, "ok": False, "error": str(exc)}
        finally:
            _cal_exit(wid)
    return report


def format_report(report, worker_ids):
    lines = ["calibration vJoy (avancer 2 m -> freiner -> braquer gauche/droite):"]
    for wid in worker_ids:
        r = report.get(wid, {})
        phase = r.get("phase")
        if r.get("error"):
            lines.append("  W{0} (device {1}) phase={2} : ERREUR {3}".format(
                wid, wid + 1, phase, r["error"]))
            continue
        ok = bool(r.get("ok"))
        lines.append("  W{0} (device {1}) phase={2} : {3}".format(
            wid, wid + 1, phase, "OK" if ok else "ECHEC"))
        for res in r.get("results", []):
            mark = "OK " if res["ok"] else "ECHEC"
            lines.append("      {0:12} {1} {2}".format(res["action"], mark, res["data"]))
    ok_count = sum(1 for w in worker_ids if report.get(w, {}).get("ok"))
    lines.append("BILAN: {0}/{1} voitures calibrées OK".format(ok_count, len(worker_ids)))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", default="0,1,2,3,4,5")
    ap.add_argument("--json", action="store_true", help="print a JSON report instead of text")
    args = ap.parse_args()
    workers = [int(x) for x in args.workers.split(",") if x.strip()]
    report = run_all(workers)
    if args.json:
        import json
        ok_count = sum(1 for w in workers if report.get(w, {}).get("ok"))
        print(json.dumps({"ok_workers": ok_count, "total": len(workers), "workers": report}))
    else:
        print(format_report(report, workers))
    ok_count = sum(1 for w in workers if report.get(w, {}).get("ok"))
    sys.exit(0 if ok_count == len(workers) else 1)


if __name__ == "__main__":
    main()
