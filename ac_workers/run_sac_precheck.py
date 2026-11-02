"""Pre-flight checks before Run-SAC (collector diag + worker status)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))

from ac_workers.run_sac_status import validate_worker_for_training
from ac_workers.worker_handshake import run_worker_handshake
from ac_workers.transition_server_process import ping_transition_server


def precheck_workers(worker_ids: list[int], require_transitions: bool = True) -> int:
    ok_all = True
    if not ping_transition_server(timeout=1.0):
        print("Run-SAC precheck FAIL: transition server health ping failed")
        return 1
    for wid in worker_ids:
        result = run_worker_handshake(wid, require_transition=require_transitions)
        if require_transitions and wid == 0:
            ok_st, errs = validate_worker_for_training(wid)
            if not ok_st:
                result["ok"] = False
                result.setdefault("errors", []).extend(errs)
        if not result.get("ok"):
            ok_all = False
            if require_transitions and wid == 0 and int(result.get("transition_count", 0)) <= 0:
                print("training refused: worker 0 has no valid transitions")
            print("Run-SAC precheck FAIL worker {0}".format(wid))
            for err in result.get("errors", []):
                print("  ERROR: {0}".format(err))
        else:
            tc = int(result.get("transition_count", 0))
            if tc <= 0:
                try:
                    from ac_workers.run_sac_status import read_worker_status

                    tc = int(read_worker_status(wid).get("transition_count", 0))
                except Exception:
                    pass
            if require_transitions and tc <= 0:
                ok_all = False
                print("training refused: worker 0 has no valid transitions")
                print("Run-SAC precheck FAIL worker {0}".format(wid))
                continue
            print("Run-SAC precheck OK worker {0} transitions={1}".format(wid, tc))
    return 0 if ok_all else 1

def _parse_worker_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        ids.append(int(token))
    return ids


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--worker-ids",
        default="",
        help="explicit comma-separated worker ids (degraded mode, tolerates holes e.g. 0,1,3,5)",
    )
    parser.add_argument("--no-require-transitions", action="store_true")
    args = parser.parse_args()
    if args.worker_ids.strip():
        ids = _parse_worker_ids(args.worker_ids)
    else:
        ids = list(range(args.workers))
    if not ids:
        print("Run-SAC precheck FAIL: empty worker id set")
        return 1
    return precheck_workers(ids, require_transitions=not args.no_require_transitions)


if __name__ == "__main__":
    raise SystemExit(main())
