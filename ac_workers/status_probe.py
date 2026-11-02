"""Runtime metrics for Get-Multi-AI-Status (C4D)."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _pid_cpu_percent(pid: int) -> float | None:
    if pid <= 0:
        return None
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        GetProcessTimes = ctypes.windll.kernel32.GetProcessTimes
        OpenProcess = ctypes.windll.kernel32.OpenProcess
        CloseHandle = ctypes.windll.kernel32.CloseHandle
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        create, exit_, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        ok = GetProcessTimes(handle, ctypes.byref(create), ctypes.byref(exit_), ctypes.byref(kernel), ctypes.byref(user))
        CloseHandle(handle)
        if not ok:
            return None
        total = (kernel.dwHighDateTime << 32) + kernel.dwLowDateTime
        total += (user.dwHighDateTime << 32) + user.dwLowDateTime
        return total / 10000000.0
    except Exception:
        return None


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def collect_status(manifest_path: Path) -> dict:
    out: dict = {"workers": {}, "learner": {}, "metrics": {}}
    manifest = _read_json(manifest_path)
    runtime = REPO / "ac_workers" / "runtime"
    learner_status = _read_json(REPO / "outputs" / "multi_sac_cpu_v3" / "learner_status.json")
    out["learner"] = learner_status

    try:
        from common.transition_server import TransitionClient

        out["transition"] = TransitionClient().stats()
    except Exception as exc:
        out["transition"] = {"error": str(exc)}

    t0 = time.time()
    for w in manifest.get("workers", []):
        wid = int(w.get("workerId", -1))
        st = _read_json(runtime / ("worker_{0}_status.json".format(wid)))
        ac_pid = int(w.get("acPid") or 0)
        col_pid = int(w.get("collectorPid") or 0)
        st["ac_cpu_sec"] = _pid_cpu_percent(ac_pid)
        st["collector_cpu_sec"] = _pid_cpu_percent(col_pid)
        out["workers"][str(wid)] = st
    out["metrics"]["probe_ms"] = (time.time() - t0) * 1000.0
    if learner_status:
        out["metrics"]["global_steps"] = learner_status.get("global_steps")
        out["metrics"]["replay_size"] = learner_status.get("replay_size")
        out["metrics"]["learner_updates"] = learner_status.get("updates")
        out["metrics"]["mean_reward"] = learner_status.get("mean_reward")
        out["metrics"]["heartbeat_age_sec"] = learner_status.get("heartbeat_age_sec")
        out["metrics"]["last_update_ms"] = learner_status.get("last_update_ms")
    return out


def main() -> int:
    manifest = REPO / "ac_workers" / "manifest.json"
    data = collect_status(manifest)
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
