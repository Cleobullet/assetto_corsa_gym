"""Clean fresh-start for the SAC learner.

Archive the old (possibly polluted) model + replay state instead of deleting it,
initialise a brand-new policy, publish it atomically, and clear the resume
checkpoint so the next ``learner start`` runs WITHOUT resume.

This does NOT touch the workers or the transition server. After running it,
restart the learner from C4D (it will start fresh because no resume checkpoint
remains). To also pick up the no-scripted-startup code, ``clean-start`` the
workers first.

The replay buffer is in-memory only (never persisted), so it is reset
automatically the moment a new learner process starts.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))
sys.path.insert(0, str(REPO / "algorithm" / "discor"))

from ac_workers.init_fresh_policy import init_fresh_policy
from ac_workers.policy_metadata import read_pointer
from AssettoCorsaEnv.ac_worker_runtime import (  # noqa: E402
    _pid_alive,
    read_manifest,
    write_manifest,
)

# Artifacts that carry learned state and must be archived for a true fresh start.
_MODEL_ARCHIVE_ITEMS = (
    "checkpoints",
    "publish_versions",
    "latest.json",
    "latest.pt",
    "publish",
)
_WORKDIR_ARCHIVE_ITEMS = (
    "learner_status.json",
)


def _request_learner_stop(work_dir: Path, *, timeout_sec: float = 12.0) -> str:
    """Ask any running learner to stop via its stop-request sentinel, then wait
    for the manifest learnerPid to die. Returns a human-readable note."""
    stop_req = work_dir / "learner_stop.request"
    stop_req.parent.mkdir(parents=True, exist_ok=True)
    stop_req.write_text("stop\n", encoding="utf-8")

    manifest = read_manifest()
    pid = int(manifest.get("learnerPid") or 0)
    if pid <= 0 or not _pid_alive(pid):
        return "no live learner in manifest (stop-request written anyway)"

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return "learner pid={0} stopped gracefully".format(pid)
        time.sleep(0.3)
    return "learner pid={0} still alive after {1:.0f}s (force-kill it manually)".format(
        pid, timeout_sec
    )


def _archive(work_dir: Path, stamp: str) -> Path:
    archive_root = work_dir / "archive" / "fresh_{0}".format(stamp)
    (archive_root / "model").mkdir(parents=True, exist_ok=True)
    model_dir = work_dir / "model"
    moved: list[str] = []
    for name in _MODEL_ARCHIVE_ITEMS:
        src = model_dir / name
        if src.exists():
            shutil.move(str(src), str(archive_root / "model" / name))
            moved.append("model/" + name)
    for name in _WORKDIR_ARCHIVE_ITEMS:
        src = work_dir / name
        if src.exists():
            shutil.move(str(src), str(archive_root / name))
            moved.append(name)
    (archive_root / "ARCHIVED_ITEMS.txt").write_text(
        "\n".join(moved) + "\n", encoding="utf-8"
    )
    print("archived {0} item(s) -> {1}".format(len(moved), archive_root))
    for m in moved:
        print("  + {0}".format(m))
    return archive_root


def _clear_manifest_learner() -> None:
    manifest = read_manifest()
    if manifest.get("learnerPid") is not None:
        manifest["learnerPid"] = None
        write_manifest(manifest)


def fresh_start(
    *,
    config_path: Path,
    work_dir: Path,
    state_dim: int = 50,
    action_dim: int = 3,
    stop_timeout_sec: float = 12.0,
) -> dict:
    work_dir = work_dir.resolve()
    print("== fresh-start: {0} ==".format(work_dir))

    stop_note = _request_learner_stop(work_dir, timeout_sec=stop_timeout_sec)
    print("stop: {0}".format(stop_note))

    stamp = time.strftime("%Y%m%d_%H%M%S")
    archive_root = _archive(work_dir, stamp)

    result = init_fresh_policy(
        config_path=config_path,
        work_dir=work_dir,
        state_dim=state_dim,
        action_dim=action_dim,
    )

    _clear_manifest_learner()
    # Leave no resume checkpoint -> next learner start runs WITHOUT --resume.
    resume_ckpt = work_dir / "model" / "checkpoints" / "resume"
    result["resume_checkpoint_present"] = resume_ckpt.exists()

    pointer = read_pointer(work_dir / "model")
    result["published_pointer"] = pointer
    result["archive_dir"] = str(archive_root)

    print("\n== fresh-start complete ==")
    print("  published_version={0}".format(
        pointer.get("policy_version") if pointer else None
    ))
    print("  resume_checkpoint_present={0} (must be False)".format(
        result["resume_checkpoint_present"]
    ))
    print("  archive_dir={0}".format(archive_root))
    print("\nNext steps in C4D:")
    print("  1) clean-start 0-5     # restart workers with the no-scripted-startup code")
    print("  2) learner start       # fresh (no resume), continuous mode")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Archive old model + start a fresh SAC policy.")
    parser.add_argument("--state-dim", type=int, default=50)
    parser.add_argument("--action-dim", type=int, default=3)
    parser.add_argument("--config", default=str(REPO / "config_first_sac_cpu_v3.yml"))
    parser.add_argument("--work-dir", default=str(REPO / "outputs" / "multi_sac_cpu_v3"))
    parser.add_argument("--stop-timeout", type=float, default=12.0)
    args = parser.parse_args(argv)
    fresh_start(
        config_path=Path(args.config),
        work_dir=Path(args.work_dir),
        state_dim=args.state_dim,
        action_dim=args.action_dim,
        stop_timeout_sec=args.stop_timeout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
