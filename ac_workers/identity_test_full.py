#!/usr/bin/env python
"""Full identity test: per-axis (throttle/brake/steer) + full cross-talk matrix.

Waits for every worker to reach an active-driving phase ('idle'/'running'), then
for each car verifies, live:
  1. its OWN vJoy device makes it react on all three axes (throttle, brake, steer);
  2. the OTHER five devices do NOT control it (cross-talk matrix).

Reads the live car state via the authoritative ``diagnostic_vjoy_pulse``
``telemetry_before``/``telemetry_after`` fields (the ONLY live steer/gas/brake
source), so no stale snapshot is used. A neutral read (0,0,0, hold 0.05s) is a
brief, harmless coast that the policy resumes immediately after.
"""
from __future__ import annotations

import argparse
import sys
import time

from ac_workers.collector_diag import CollectorDiagClient
from ac_workers.identity_test import _diag_pulse, _identity_probe

# Axis thresholds (steer in degrees, gas/brake normalized 0..1).
STEER_MIN = 3.0        # own steering pulse must move the wheel by > 3 deg
GAS_MIN = 0.10         # own throttle pulse must raise gas by > 0.10
BRAKE_MIN = 0.10       # own brake pulse must raise brake by > 0.10
# Cross-talk signature: a full-brake + hard-steer pulse on the WRONG device must
# NOT move this car more than this (a real cross-talk would spike far past it).
XTALK_BRAKE_MAX = 0.40
XTALK_STEER_MAX = 8.0

NEUTRAL_READ = {"steer": 0.0, "throttle": 0.0, "brake": 0.0, "hold_sec": 0.05}
STEER_PULSE = {"steer": 0.6, "throttle": 0.0, "brake": 0.0, "hold_sec": 0.45}
THROTTLE_PULSE = {"steer": 0.0, "throttle": 0.8, "brake": 0.0, "hold_sec": 0.45}
BRAKE_PULSE = {"steer": 0.0, "throttle": 0.0, "brake": 1.0, "hold_sec": 0.45}
XTALK_PULSE = {"steer": 0.6, "throttle": 0.0, "brake": 1.0, "hold_sec": 0.5}

READY_PHASES = ("idle", "running")


def _state(resp):
    return resp.get("telemetry_before") or {}, resp.get("telemetry_after") or {}


def _delta(before, after, key):
    return abs(float(after.get(key, 0.0) or 0.0) - float(before.get(key, 0.0) or 0.0))


def read_car(wid):
    """Live-read car `wid` state; returns (before, resp)."""
    resp = _diag_pulse(wid, dict(NEUTRAL_READ))
    return resp.get("telemetry_before") or {}, resp


def phase_of(wid):
    try:
        return _identity_probe(wid).phase
    except Exception:
        return None


def wait_all_ready(workers, timeout_s):
    t0 = time.time()
    last = {}
    phases = {}
    while time.time() - t0 < timeout_s:
        phases = {w: phase_of(w) for w in workers}
        ready = [w for w in workers if phases[w] in READY_PHASES]
        if phases != last:
            print("  phases: {0}".format({w: phases[w] for w in workers}))
            last = phases
        if len(ready) == len(workers):
            print("  OK: les 6 voitures sont en conduite active.\n")
            return True
        time.sleep(3)
    print("  TIMEOUT: voitures jamais toutes actives: {0}".format(
        {w: phases.get(w, phase_of(w)) for w in workers}))
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", default="0,1,2,3,4,5")
    ap.add_argument("--wait-timeout", type=float, default=600.0)
    args = ap.parse_args()
    workers = [int(x) for x in args.workers.split(",") if x.strip()]

    print("Attente que les {0} voitures soient en conduite active...".format(len(workers)))
    if not wait_all_ready(workers, args.wait_timeout):
        sys.exit(2)

    own = {}       # wid -> {axis: (delta, ok)}
    xtalk = {}     # (car, device) -> {"brake_delta": .., "steer_delta": .., "suspicious": bool}
    failures = []

    for n in workers:
        print("=== Voiture W{0} (device {1}) ===".format(n, n + 1))
        # --- OWN: 3 axes ---
        own[n] = {}
        for axis, pulse in (("steer", STEER_PULSE), ("throttle", THROTTLE_PULSE),
                            ("brake", BRAKE_PULSE)):
            resp = _diag_pulse(n, dict(pulse))
            before, after = _state(resp)
            if not resp.get("write_ok"):
                own[n][axis] = (0.0, False, "write_ok=False: {0}".format(resp.get("error") or "?"))
                failures.append("W{0}.{1}: {2}".format(n, axis, own[n][axis][2]))
                continue
            if axis == "steer":
                d = _delta(before, after, "steer")
                ok = d > STEER_MIN
            elif axis == "throttle":
                d = _delta(before, after, "gas")
                ok = d > GAS_MIN
            else:
                d = _delta(before, after, "brake")
                ok = d > BRAKE_MIN
            own[n][axis] = (round(d, 2), ok, "")
            mark = "OK " if ok else "ECHEC"
            print("  {0:9} delta={1:>7}  {2}".format(axis, round(d, 2), mark))
            if not ok:
                failures.append("W{0}.{1}: delta {2} trop faible".format(n, axis, round(d, 2)))
            time.sleep(0.15)

        # --- CROSS-TALK: pulse each OTHER device, watch this car ---
        for m in workers:
            if m == n:
                continue
            before, _ = read_car(n)
            _diag_pulse(m, dict(XTALK_PULSE))
            after, _ = read_car(n)
            bd = _delta(before, after, "brake")
            sd = _delta(before, after, "steer")
            suspicious = bd > XTALK_BRAKE_MAX or sd > XTALK_STEER_MAX
            xtalk[(n, m + 1)] = {"brake_delta": round(bd, 3), "steer_delta": round(sd, 2),
                                 "suspicious": suspicious}
            flag = "CROSS-TALK !!" if suspicious else "ok"
            print("    device {0} -> W{1}: brake_delta={2} steer_delta={3}  {4}".format(
                m + 1, n, round(bd, 3), round(sd, 2), flag))
            if suspicious:
                failures.append(
                    "CROSS-TALK: device {0} bouge W{1} (brake_delta={2}, steer_delta={3})".format(
                        m + 1, n, round(bd, 3), round(sd, 2)))
            time.sleep(0.15)
        print()

    # --- Summary ---
    print("\n===== BILAN =====")
    for n in workers:
        row = ["W{0} (dev{1}):".format(n, n + 1)]
        for axis in ("steer", "throttle", "brake"):
            v = own[n][axis]
            row.append("{0}={1}{2}".format(axis, v[0], "OK" if v[1] else "!!"))
        print("  " + " ".join(row))
    print()
    print("Cross-talk (doit être 'ok' partout):")
    for n in workers:
        cells = []
        for m in workers:
            if m == n:
                cells.append("  X  ")
            else:
                cells.append("ok" if not xtalk[(n, m + 1)]["suspicious"] else "XTALK")
        print("  W{0}: {1}".format(n, " ".join("{0:>6}".format(c) for c in cells)))
    print()
    if failures:
        print("RESULTAT: ECHEC — {0} probleme(s)".format(len(failures)))
        for f in failures:
            print("  - {0}".format(f))
        sys.exit(1)
    print("RESULTAT: OK — chaque device pilote SA voiture, sur les 3 axes, sans cross-talk.")
    sys.exit(0)


if __name__ == "__main__":
    main()
