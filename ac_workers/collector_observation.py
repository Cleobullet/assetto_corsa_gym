"""C4D observation validation for collector status and policy gating."""
from __future__ import annotations

from typing import Any

import numpy as np

EXPECTED_STATE_DIM = 8
PAST_HISTORY_REQUIRED = 3


def assess_observation(
    obs: list[float] | np.ndarray,
    *,
    phase: str,
    telemetry_ready: bool,
    telemetry_age_ms: float,
    telemetry_snapshot: dict[str, Any],
    history_frames: int = 0,
) -> dict[str, Any]:
    arr = np.asarray(obs, dtype=np.float64).ravel()
    length = int(arr.size)
    missing: list[str] = []

    if length == 0:
        missing.append("observation_empty")
    elif length != EXPECTED_STATE_DIM:
        missing.append("wrong_length:{0}!={1}".format(length, EXPECTED_STATE_DIM))
    if length > 0 and not np.all(np.isfinite(arr)):
        missing.append("non_finite")

    if not telemetry_ready:
        missing.append("telemetry_not_ready")
    if telemetry_age_ms >= 2000.0:
        missing.append("telemetry_stale")
    if not telemetry_snapshot:
        missing.append("telemetry_snapshot_empty")
    if phase not in ("running", "idle"):
        missing.append("phase_not_running:{0}".format(phase))

    valid_for_policy = (
        length == EXPECTED_STATE_DIM
        and length > 0
        and bool(np.all(np.isfinite(arr)))
    )
    complete = (
        phase in ("running", "idle")
        and telemetry_ready
        and valid_for_policy
        and bool(telemetry_snapshot)
        and telemetry_age_ms < 2000.0
    )
    return {
        "observation_length": length,
        "observation_expected_length": EXPECTED_STATE_DIM,
        "observation_missing_components": missing,
        "observation_history_frames": int(history_frames),
        "observation_history_required": PAST_HISTORY_REQUIRED,
        "observation_valid_for_policy": bool(valid_for_policy),
        "observation_complete": bool(complete),
    }
