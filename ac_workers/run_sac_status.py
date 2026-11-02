"""Read worker status JSON for Run-SAC prechecks."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

from ac_workers.collector_status import validate_snapshot


def read_worker_status(worker_id: int = 0) -> dict:
    path = REPO / "ac_workers" / "status" / ("worker_{0}.json".format(worker_id))
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def validate_worker_for_training(
    worker_id: int = 0,
    session_id: str | None = None,
    collector_pid: int | None = None,
) -> tuple[bool, list[str]]:
    st = read_worker_status(worker_id)
    if not st:
        return False, ["worker_{0}.json absent".format(worker_id)]
    ok, errors, _details = validate_snapshot(
        st,
        session_id=session_id,
        collector_pid=collector_pid,
        require_transitions=True,
    )
    return ok, errors


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-id", type=int, default=0)
    parser.add_argument("--require-transitions", action="store_true")
    args = parser.parse_args()
    st = read_worker_status(args.worker_id)
    print(json.dumps(st, indent=2))
    if args.require_transitions:
        ok, errors = validate_worker_for_training(args.worker_id)
        if not ok:
            for e in errors:
                print("ERROR:", e)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
