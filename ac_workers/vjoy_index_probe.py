"""Empirically map vJoy device id -> joystick enumeration index (no AC needed).

This answers the one question files cannot: when a worker writes to vJoy device N,
which *enumeration index* does the game see move? AC's controls.ini binds a car to
a controller by product GUID (identical across all vJoy devices) + an index
(JOY=N). The whole worker->car identity therefore rests on:

    enumeration_index(vjoy_device N+1) == N      (for worker N, JOY=N)

We prove or disprove it directly: for each FREE vJoy device, feed a known value on
the X axis via vJoyInterface.dll, then read every joystick's X position via the
Windows winmm API and see which index moved. Busy/owned devices (a live collector
feeder) are skipped untouched. Every device is reset to center and relinquished
afterwards, so this never leaves a device in a driving state.

CAVEAT: winmm enumeration order is the legacy joystick order. AC uses DirectInput.
On the vast majority of systems the two orders agree, but a mismatch here is hard
evidence the ordering assumption is unsafe; an identity mapping here is necessary
(though not 100% sufficient) for the controls.ini JOY index to be correct.
"""
from __future__ import annotations

import ctypes
import json
import os
import struct
import sys
import time
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "assetto_corsa_gym") not in sys.path:
    sys.path.insert(0, str(REPO / "assetto_corsa_gym"))

from ac_workers.vjoy_pool import MAX_WORKERS
from ac_workers.vjoy_session import (
    JOYSTICK_POSITION_FMT,
    VJD_STAT_FREE,
    VJOY_DLL,
    get_vjd_status,
    relinquish_vjd,
    vjd_status_label,
)

AXIS_CENTER = 16384
AXIS_MAX = 32767
SETTLE_SEC = 0.25

# Persisted detected mapping (single source of truth for controls.ini JOY index).
DIAG_DIR = REPO / "outputs" / "diagnostics"
MAPPING_LATEST = DIAG_DIR / "vjoy_mapping_latest.json"
MAPPING_SCHEMA = 1


# ----------------------------- winmm joystick read ------------------------- #
class _JOYINFOEX(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("dwXpos", wintypes.DWORD), ("dwYpos", wintypes.DWORD), ("dwZpos", wintypes.DWORD),
        ("dwRpos", wintypes.DWORD), ("dwUpos", wintypes.DWORD), ("dwVpos", wintypes.DWORD),
        ("dwButtons", wintypes.DWORD), ("dwButtonNumber", wintypes.DWORD),
        ("dwPOV", wintypes.DWORD), ("dwReserved1", wintypes.DWORD), ("dwReserved2", wintypes.DWORD),
    ]


_JOY_RETURNALL = 0x000000FF


def _winmm():
    return ctypes.WinDLL("winmm")


def read_all_joystick_x(winmm) -> dict[int, int]:
    """Map present joystick index -> current X position (winmm)."""
    out: dict[int, int] = {}
    for i in range(int(winmm.joyGetNumDevs())):
        info = _JOYINFOEX()
        info.dwSize = ctypes.sizeof(_JOYINFOEX)
        info.dwFlags = _JOY_RETURNALL
        if winmm.joyGetPosEx(i, ctypes.byref(info)) == 0:
            out[i] = int(info.dwXpos)
    return out


# ----------------------------- vJoy feeder (DLL) --------------------------- #
def _load_vjoy_dll() -> ctypes.CDLL | None:
    if not Path(VJOY_DLL).is_file():
        return None
    dll = ctypes.CDLL(VJOY_DLL)
    dll.AcquireVJD.argtypes = [ctypes.c_uint]
    dll.AcquireVJD.restype = ctypes.c_bool
    dll.UpdateVJD.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    dll.UpdateVJD.restype = ctypes.c_bool
    return dll


def _position_packet(device_id: int, axis_x: int) -> bytes:
    """Build a JOYSTICK_POSITION packet with X set, all other axes centered."""
    fields = [0] * 24
    fields[0] = int(device_id)          # bDevice
    fields[1] = AXIS_CENTER             # wThrottle
    fields[2] = AXIS_CENTER             # wRudder
    fields[3] = AXIS_CENTER             # wAileron
    fields[4] = int(axis_x)             # wAxisX  <-- the one we move
    fields[5] = AXIS_CENTER             # wAxisY
    fields[6] = AXIS_CENTER             # wAxisZ
    return struct.pack(JOYSTICK_POSITION_FMT, *fields)


def _feed(dll, device_id: int, axis_x: int) -> bool:
    packet = _position_packet(device_id, axis_x)
    buf = ctypes.create_string_buffer(packet, len(packet))
    return bool(dll.UpdateVJD(device_id, buf))


def probe_device_to_index(
    device_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Map each FREE vJoy device to the joystick index that moves when it's fed."""
    if sys.platform != "win32":
        return {"error": "winmm/vJoy probe is Windows-only"}
    if device_ids is None:
        device_ids = [w + 1 for w in range(MAX_WORKERS)]

    dll = _load_vjoy_dll()
    if dll is None:
        return {"error": "vJoyInterface.dll not found at {0}".format(VJOY_DLL)}
    winmm = _winmm()

    mapping: dict[int, int | None] = {}
    skipped: dict[int, str] = {}
    deltas: dict[int, dict[int, int]] = {}

    for dev in device_ids:
        status = get_vjd_status(dev)
        if status != VJD_STAT_FREE:
            skipped[dev] = vjd_status_label(status)
            continue
        acquired = False
        try:
            if not dll.AcquireVJD(dev):
                skipped[dev] = "acquire_failed"
                continue
            acquired = True
            # Baseline: center the axis, let winmm settle, snapshot.
            _feed(dll, dev, AXIS_CENTER)
            time.sleep(SETTLE_SEC)
            base = read_all_joystick_x(winmm)
            # Drive the X axis hard, snapshot again.
            _feed(dll, dev, AXIS_MAX)
            time.sleep(SETTLE_SEC)
            moved = read_all_joystick_x(winmm)
            d = {idx: abs(moved.get(idx, 0) - base.get(idx, 0)) for idx in moved}
            deltas[dev] = d
            best_idx = max(d, key=lambda k: d[k]) if d else None
            # Require a real swing to call it a match.
            mapping[dev] = best_idx if (best_idx is not None and d[best_idx] > 4000) else None
        finally:
            if acquired:
                _feed(dll, dev, AXIS_CENTER)
                relinquish_vjd(dev)
            time.sleep(0.05)

    return {"mapping": mapping, "skipped": skipped, "deltas": deltas}


def analyze(result: dict[str, Any]) -> list[str]:
    """Flag any device whose enumeration index != device_id - 1 (the assumption)."""
    violations: list[str] = []
    mapping: dict[int, int | None] = result.get("mapping", {})
    # Detect two devices resolving to the same index (ambiguous ordering).
    seen: dict[int, int] = {}
    for dev, idx in mapping.items():
        if idx is None:
            violations.append(
                "vJoy device {0}: no joystick index moved -> feeder not reaching this device".format(dev)
            )
            continue
        expected = dev - 1
        if idx != expected:
            violations.append(
                "vJoy device {0} drives enumeration index {1}, but controls.ini JOY assumes "
                "index {2} -> worker {2} would control the WRONG car".format(dev, idx, expected)
            )
        if idx in seen:
            violations.append(
                "enumeration index {0} is driven by BOTH vJoy device {1} and {2}".format(
                    idx, seen[idx], dev
                )
            )
        else:
            seen[idx] = dev
    return violations


def format_result(result: dict[str, Any]) -> str:
    if "error" in result:
        return "probe error: {0}".format(result["error"])
    lines = ["vJoy device -> joystick enumeration index (empirical, free devices only):"]
    mapping: dict[int, int | None] = result.get("mapping", {})
    skipped: dict[int, str] = result.get("skipped", {})
    for dev in sorted(set(list(mapping.keys()) + list(skipped.keys()))):
        if dev in skipped:
            lines.append(
                "  vJoy device {0}: SKIPPED ({1}) — owned/busy, not touched".format(dev, skipped[dev])
            )
            continue
        idx = mapping.get(dev)
        expected = dev - 1
        if idx is None:
            lines.append("  vJoy device {0} -> (no index moved)".format(dev))
        else:
            tag = "OK" if idx == expected else "MISMATCH (expected {0})".format(expected)
            lines.append("  vJoy device {0} -> enumeration index {1}   [{2}]".format(dev, idx, tag))
    violations = analyze(result)
    lines.append("")
    if violations:
        lines.append("VIOLATIONS:")
        for v in violations:
            lines.append("  ! " + v)
        lines.append("")
        lines.append(
            "Interpretation: the controls.ini JOY=N index does NOT line up with vJoy device N+1\n"
            "on this machine -> worker->car wiring is broken at the vJoy enumeration layer."
        )
    else:
        lines.append(
            "All probed devices map device d -> index d-1 (identity ordering). Consistent with\n"
            "controls.ini JOY=N -> vJoy device N+1 (winmm order; DirectInput usually matches)."
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# detection + persistence (single source of truth for controls.ini JOY index)
# --------------------------------------------------------------------------- #
def load_saved_mapping() -> dict[int, int] | None:
    """Return the last saved device_id -> joy_index map, or None."""
    try:
        if not MAPPING_LATEST.is_file():
            return None
        data = json.loads(MAPPING_LATEST.read_text(encoding="utf-8"))
        raw = data.get("device_to_index") or {}
        return {int(k): int(v) for k, v in raw.items()}
    except (OSError, ValueError, TypeError):
        return None


def detected_index_for_device(device_id: int) -> int | None:
    mapping = load_saved_mapping()
    if not mapping:
        return None
    return mapping.get(int(device_id))


def _merge_and_validate(
    probe_mapping: dict[int, int | None],
    *,
    devices: list[int],
    prior: dict[int, int] | None,
) -> dict[str, Any]:
    """Combine a fresh probe with prior knowledge into a validated bijection.

    Busy/owned devices (no fresh probe) are filled from the prior saved mapping
    when available, otherwise inferred by elimination if exactly one device and
    one index remain. The result is only ``ok`` when every requested device maps
    to a distinct index — anything ambiguous/incomplete is reported, never guessed.
    """
    n = len(devices)
    expected_indices = set(range(n))
    device_to_index: dict[int, int] = {}
    methods: dict[int, str] = {}

    # 1) take fresh probe results that produced a real index.
    for dev in devices:
        idx = probe_mapping.get(dev)
        if idx is not None:
            device_to_index[dev] = int(idx)
            methods[dev] = "probed"

    # 2) fill the rest from prior knowledge (busy devices from a prior session start).
    prior = prior or {}
    for dev in devices:
        if dev in device_to_index:
            continue
        if dev in prior and prior[dev] not in device_to_index.values():
            device_to_index[dev] = int(prior[dev])
            methods[dev] = "prior"

    # 3) last-resort elimination: one device and one index left.
    missing_devices = [d for d in devices if d not in device_to_index]
    used = set(device_to_index.values())
    free_indices = sorted(expected_indices - used)
    if len(missing_devices) == 1 and len(free_indices) == 1:
        device_to_index[missing_devices[0]] = free_indices[0]
        methods[missing_devices[0]] = "elimination"
        missing_devices = []

    # 4) validate strict bijection over the requested device set.
    problems: list[str] = []
    seen: dict[int, int] = {}
    for dev, idx in device_to_index.items():
        if idx in seen:
            problems.append(
                "index {0} claimed by devices {1} and {2}".format(idx, seen[idx], dev)
            )
        else:
            seen[idx] = dev
    if missing_devices:
        problems.append("no index detected for device(s) {0}".format(missing_devices))
    covered = set(device_to_index.values())
    if device_to_index and covered != expected_indices and not missing_devices:
        problems.append(
            "indices {0} != expected {1}".format(sorted(covered), sorted(expected_indices))
        )

    complete = len(device_to_index) == n and not problems
    return {
        "device_to_index": device_to_index,
        "methods": methods,
        "complete": complete,
        "problems": problems,
        "status": "ok" if complete else "incomplete",
    }


def detect_and_save_mapping(
    *,
    devices: list[int] | None = None,
    session_id: str | None = None,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    """Probe device->index, merge with the last saved map, validate, persist.

    GUARD RAIL: on an incomplete/ambiguous result with no usable prior mapping,
    nothing is written and ``ok`` is False so the launcher refuses to start.
    """
    if sys.platform != "win32":
        return {"ok": False, "status": "error", "error": "Windows-only probe"}
    if devices is None:
        devices = [w + 1 for w in range(MAX_WORKERS)]
    out_dir = out_dir or DIAG_DIR
    session_id = session_id or os.environ.get("AC_AI_SESSION_ID", "")

    probe = probe_device_to_index(devices)
    if "error" in probe:
        return {"ok": False, "status": "error", "error": probe["error"]}

    prior = load_saved_mapping()
    merged = _merge_and_validate(probe.get("mapping", {}), devices=devices, prior=prior)

    if not merged["complete"]:
        # Reuse a previously-saved COMPLETE bijection rather than failing, only if
        # it still covers every requested device (e.g. mid-session relaunch where
        # sibling devices are busy and could not be re-probed now).
        if prior and all(d in prior for d in devices) and len(set(prior.values())) == len(devices):
            return {
                "ok": True,
                "status": "reused_prior",
                "device_to_index": dict(prior),
                "problems": merged["problems"],
                "path": str(MAPPING_LATEST),
            }
        return {
            "ok": False,
            "status": "incomplete",
            "device_to_index": merged["device_to_index"],
            "problems": merged["problems"],
            "skipped": probe.get("skipped", {}),
        }

    payload = {
        "schema": MAPPING_SCHEMA,
        "detected_at_utc": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "device_to_index": {str(k): int(v) for k, v in merged["device_to_index"].items()},
        "method": {str(k): v for k, v in merged["methods"].items()},
        "status": "ok",
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    ts_path = out_dir / "vjoy_mapping_{0}.json".format(stamp)
    _atomic_write_json(ts_path, payload)
    _atomic_write_json(out_dir / "vjoy_mapping_latest.json", payload)
    return {
        "ok": True,
        "status": "ok",
        "device_to_index": merged["device_to_index"],
        "methods": merged["methods"],
        "path": str(ts_path),
    }


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Map vJoy device id -> joystick enumeration index")
    parser.add_argument("--devices", type=str, default="", help="comma list of device ids, default 1..6")
    parser.add_argument(
        "--save",
        action="store_true",
        help="detect + persist the mapping (outputs/diagnostics) for use by controls.ini patching",
    )
    args = parser.parse_args(argv)
    if args.devices.strip():
        device_ids = [int(x) for x in args.devices.split(",") if x.strip() != ""]
    else:
        device_ids = [w + 1 for w in range(MAX_WORKERS)]

    if args.save:
        res = detect_and_save_mapping(devices=device_ids)
        if res.get("ok"):
            m = res.get("device_to_index", {})
            print("vJoy index mapping {0}: {1}".format(
                res.get("status"),
                ", ".join("dev{0}->idx{1}".format(d, m[d]) for d in sorted(m)),
            ))
            if res.get("path"):
                print("saved: {0}".format(res["path"]))
            return 0
        print("vJoy index mapping DETECTION FAILED ({0}):".format(res.get("status")))
        for p in res.get("problems", []) or []:
            print("  ! " + p)
        if res.get("error"):
            print("  ! " + str(res["error"]))
        print("  -> training must NOT start: worker->car binding cannot be guaranteed.")
        return 1

    result = probe_device_to_index(device_ids)
    print(format_result(result))
    return 1 if ("error" in result or analyze(result)) else 0


if __name__ == "__main__":
    raise SystemExit(main())
