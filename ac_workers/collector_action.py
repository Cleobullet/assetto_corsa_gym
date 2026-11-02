"""C4D action helpers — neutral check only."""
from __future__ import annotations

from typing import Any

import numpy as np

NEUTRAL_ACTION = np.array([0.0, -1.0, -1.0], dtype=np.float32)


def is_neutral_action(action: Any) -> bool:
    a = np.asarray(action, dtype=np.float32).ravel()[:3]
    return bool(np.allclose(a, NEUTRAL_ACTION, atol=1e-4))


def read_vjoy_axes_safe(env: Any) -> tuple[dict[str, float], bool]:
    from ac_workers.vjoy_session import logical_to_internal_axes, read_logical_from_controls

    logical, available = read_logical_from_controls(env.client.controls)
    if not available:
        return {"steer": 0.0, "throttle": -1.0, "brake": -1.0}, False
    return logical_to_internal_axes(logical), True
