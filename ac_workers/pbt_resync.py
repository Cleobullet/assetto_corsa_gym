"""PBT resynchronization — a shared "main brain" that promotes good workers and
resets bad ones at episode boundaries (teleports).

The main brain is stored on disk under ``<work_dir>/pbt_main/``:
  - ``policy_net.pth`` / ``online_q_net.pth`` / ``target_q_net.pth``  (SAC weights)
  - ``score.json``  (``best_score`` — the episode score of the promoted worker)

Each worker, at the end of an episode, calls :func:`resync` with its episode
score. The decision is:
  - score > main brain's score            -> PROMOTE (this worker becomes the main brain)
  - score < main brain's score - margin   -> RESET (load the main brain's weights)
  - otherwise                             -> KEEP (worker keeps its own brain)

This is deliberately simple and file-based so it works with N independent worker
processes and no extra server. All writes are best-effort: a failed promote/reset
never crashes the worker — it just keeps its current brain.
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAIN_DIRNAME = "pbt_main"
SCORE_FILENAME = "score.json"
SCORE_KEY = "best_score"
TRIALS_FILENAME = "trials.json"
MAX_TRIALS = 10


def pbt_main_dir(work_dir: Any) -> Path:
    return Path(work_dir) / MAIN_DIRNAME


def record_trial(work_dir: Any, worker_id: int, score: float, result: str) -> None:
    """Append one trial outcome to the trials log, keeping only the last 10.
    Lives OUTSIDE ``pbt_main/`` (as ``<work_dir>/trials.json``) so the atomic
    promote swap never wipes the trial history."""
    trials_path = Path(work_dir) / TRIALS_FILENAME
    try:
        trials_path.parent.mkdir(parents=True, exist_ok=True)
        trials: list[dict[str, Any]] = []
        if trials_path.is_file():
            try:
                trials = json.loads(trials_path.read_text(encoding="utf-8"))
                if not isinstance(trials, list):
                    trials = []
            except Exception:
                trials = []
        trials.append(
            {
                "worker_id": int(worker_id),
                "score": float(score),
                "result": str(result),
                "ts": time.time(),
            }
        )
        trials = trials[-MAX_TRIALS:]
        trials_path.write_text(json.dumps(trials, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("pbt: record_trial failed: %s", exc)


def read_main_score(work_dir: Any) -> float:
    """The main brain's best score, or ``-inf`` when no main brain exists yet."""
    score_path = pbt_main_dir(work_dir) / SCORE_FILENAME
    try:
        data = json.loads(score_path.read_text(encoding="utf-8"))
        return float(data.get(SCORE_KEY, float("-inf")))
    except Exception:
        return float("-inf")


def _read_main_meta(work_dir: Any) -> dict[str, Any]:
    try:
        return json.loads(
            (pbt_main_dir(work_dir) / SCORE_FILENAME).read_text(encoding="utf-8")
        )
    except Exception:
        return {}


def promote_to_main(work_dir: Any, algo: Any, score: float, *, worker_id: int = 0) -> bool:
    """Save this worker's weights as the new main brain (best score so far)."""
    main_dir = pbt_main_dir(work_dir)
    tmp_dir = main_dir.with_name(main_dir.name + ".tmp")
    try:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        algo.save_models(str(tmp_dir))
        (tmp_dir / SCORE_FILENAME).write_text(
            json.dumps(
                {
                    SCORE_KEY: float(score),
                    "promoted_by_worker": int(worker_id),
                    "ts": time.time(),
                }
            ),
            encoding="utf-8",
        )
        # Swap: write fully to a temp dir first, then move into place so a
        # concurrent reader never sees a half-written brain.
        if main_dir.exists():
            shutil.rmtree(main_dir, ignore_errors=True)
        tmp_dir.rename(main_dir)
        logger.info("pbt: worker=%s PROMOTED to main brain score=%.3f", worker_id, score)
        return True
    except Exception as exc:
        logger.warning("pbt: promote_to_main failed (worker=%s): %s", worker_id, exc)
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return False


def reset_from_main(work_dir: Any, algo: Any, *, worker_id: int = 0) -> bool:
    """Load the main brain's weights into this worker's algo (reset)."""
    main_dir = pbt_main_dir(work_dir)
    try:
        if not (main_dir / "policy_net.pth").is_file():
            return False
        algo.load_models(str(main_dir))
        logger.info("pbt: worker=%s RESET to main brain", worker_id)
        return True
    except Exception as exc:
        logger.warning("pbt: reset_from_main failed (worker=%s): %s", worker_id, exc)
        return False


def resync(
    work_dir: Any,
    algo: Any,
    score: float,
    worker_id: int,
    *,
    reset_margin: float = 2.0,
) -> str:
    """Run one PBT resync decision. Returns 'promoted', 'reset' or 'kept'."""
    main_score = read_main_score(work_dir)
    if score > main_score:
        promote_to_main(work_dir, algo, score, worker_id=worker_id)
        # Only GOOD trials are stored (bad laps are not sent to storage).
        record_trial(work_dir, worker_id, score, "promoted")
        return "promoted"
    if score < main_score - reset_margin:
        reset_from_main(work_dir, algo, worker_id=worker_id)
        return "reset"
    return "kept"
