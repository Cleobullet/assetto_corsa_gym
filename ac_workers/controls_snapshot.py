"""Per-worker controls.ini snapshot: prove which JOY index each AC instance read.

controls.ini lives in the SHARED ``Documents/Assetto Corsa/cfg`` folder and is
rewritten for every worker just before its ``acs.exe`` is launched. Reading the
live file afterwards only ever shows the LAST writer — which is exactly why
``worker_identity`` previously reported ``controls.ini = 1`` for every worker even
though each worker was patched with a different index. That global read is
misleading and cannot prove per-worker binding.

The only trustworthy record of what each instance read is a snapshot captured at
the instant that worker's controls.ini was finalized, *before* its AC process
started. This module owns that snapshot end to end:

  * ``write_snapshot``    — called from gear_shift right after the JOY patch.
  * ``record_ac_launch``  — called from PowerShell right after acs.exe launches,
                            so the report can prove the AC pid started AFTER the
                            patch that produced the snapshot.
  * ``load_meta``         — used by worker_identity to build a per-worker verdict.
  * ``clear_snapshots``   — called once at session start so stale snapshots from a
                            previous run can never masquerade as the current one.

It is intentionally dependency-light (stdlib only) so gear_shift can import it
lazily without pulling heavy modules into the AC plugin import path.
"""
from __future__ import annotations

import configparser
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]

# Same JOY-bearing sections gear_shift patches; STEER is checked first because it
# is the canonical axis every controls.ini defines.
_JOY_SECTIONS = ("STEER", "THROTTLE", "BRAKES", "CLUTCH", "GEARUP", "GEARDN", "HANDBRAKE")


def snapshot_dir() -> Path:
    """Snapshot output dir (overridable via env for tests/sandboxing)."""
    override = os.environ.get("AC_CONTROLS_SNAPSHOT_DIR")
    if override:
        return Path(override)
    return REPO / "outputs" / "diagnostics" / "controls_snapshots"


def snapshot_controls_path(worker_id: int) -> Path:
    return snapshot_dir() / "worker_{0}_controls.ini".format(int(worker_id))


def meta_path(worker_id: int) -> Path:
    return snapshot_dir() / "worker_{0}_meta.json".format(int(worker_id))


def _read_joy_index(controls_path: Path) -> int | None:
    """Return the JOY enumeration index actually written in this controls file."""
    try:
        parser = configparser.ConfigParser()
        parser.optionxform = str
        parser.read(controls_path, encoding="utf-8-sig")
    except (OSError, configparser.Error):
        return None
    for section in _JOY_SECTIONS:
        if parser.has_option(section, "JOY"):
            raw = parser.get(section, "JOY").split(";")[0].strip()
            try:
                return int(raw)
            except ValueError:
                continue
    return None


def _verdict(joy_written: int | None, detected: int | None) -> str:
    if joy_written is None:
        return "NO_SNAPSHOT_JOY"
    if detected is None:
        return "NO_DETECTION"
    return "OK" if int(joy_written) == int(detected) else "MISMATCH"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))


def write_snapshot(
    worker_id: int,
    controls_path: Path | str,
    detected_joy_index: int | None,
    *,
    session_id: str = "",
) -> dict[str, Any]:
    """Copy the just-patched controls.ini and record what JOY index it carries.

    Returns the meta dict. The verdict (``validation_status``) is OK only when the
    JOY value physically present in the snapshot equals the detected enumeration
    index for this worker's vJoy device (worker_id + 1).
    """
    wid = int(worker_id)
    device_id = wid + 1
    out_dir = snapshot_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    snap_ini = snapshot_controls_path(wid)
    shutil.copyfile(str(controls_path), str(snap_ini))
    joy_written = _read_joy_index(snap_ini)
    meta: dict[str, Any] = {
        "worker_id": wid,
        "vjoy_device_id": device_id,
        "detected_joy_index": detected_joy_index,
        "joy_index_written": joy_written,
        "expected_old_joy_index": wid,
        "controls_snapshot_path": str(snap_ini),
        "patch_ts_utc": datetime.now(timezone.utc).isoformat(),
        "patch_ts_epoch": time.time(),
        "session_id": session_id or os.environ.get("AC_AI_SESSION_ID", ""),
        "ac_pid": None,
        "ac_launched_ts_epoch": None,
        "ac_launched_after_patch": None,
        "validation_status": _verdict(joy_written, detected_joy_index),
    }
    _atomic_write_json(meta_path(wid), meta)
    return meta


def record_ac_launch(worker_id: int, ac_pid: int) -> dict[str, Any]:
    """Stamp the AC process id that was launched AFTER this worker's patch.

    This is the temporal proof that the snapshot we captured is the one the AC
    instance actually read (patch -> snapshot -> launch).
    """
    meta = load_meta(worker_id)
    if not meta:
        meta = {"worker_id": int(worker_id)}
    meta["ac_pid"] = int(ac_pid)
    meta["ac_launched_ts_epoch"] = time.time()
    meta["ac_launched_ts_utc"] = datetime.now(timezone.utc).isoformat()
    patch_ts = meta.get("patch_ts_epoch")
    if isinstance(patch_ts, (int, float)):
        meta["ac_launched_after_patch"] = bool(meta["ac_launched_ts_epoch"] > float(patch_ts))
    _atomic_write_json(meta_path(worker_id), meta)
    return meta


def load_meta(worker_id: int) -> dict[str, Any]:
    path = meta_path(worker_id)
    try:
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def clear_snapshots() -> int:
    """Remove all worker snapshots/metas (call once at session start). Returns count."""
    out_dir = snapshot_dir()
    if not out_dir.is_dir():
        return 0
    removed = 0
    for pattern in ("worker_*_controls.ini", "worker_*_meta.json"):
        for path in out_dir.glob(pattern):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Per-worker controls.ini snapshot tooling")
    parser.add_argument("--record-ac-launch", action="store_true", help="stamp AC pid for a worker")
    parser.add_argument("--worker", type=int, default=None)
    parser.add_argument("--pid", type=int, default=None)
    parser.add_argument("--clear", action="store_true", help="remove all snapshots (session start)")
    args = parser.parse_args(argv)

    if args.clear:
        n = clear_snapshots()
        print("cleared {0} controls snapshot file(s)".format(n))
        return 0
    if args.record_ac_launch:
        if args.worker is None or args.pid is None:
            print("--record-ac-launch requires --worker and --pid", file=sys.stderr)
            return 2
        meta = record_ac_launch(args.worker, args.pid)
        print(
            "worker {0}: ac_pid={1} after_patch={2} verdict={3}".format(
                args.worker,
                meta.get("ac_pid"),
                meta.get("ac_launched_after_patch"),
                meta.get("validation_status"),
            )
        )
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
