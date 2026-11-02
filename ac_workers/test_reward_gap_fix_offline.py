"""Offline checks for progress floor, smooth gap factor, centerline & correct-steer bonuses."""
from __future__ import annotations

import numpy as np


def progress_factor_linear(gap: float) -> float:
    return max(0.0, 1.0 - (abs(gap) / 12.00))


def progress_factor_smooth(gap: float, sigma: float = 8.0) -> float:
    return 1.0 / (1.0 + (abs(gap) / sigma) ** 2)


def wrong_way(gap: float, steer: float, coef: float = 0.20, cap: float = 0.12) -> float:
    if coef <= 0.0 or gap * steer <= 0.0:
        return 0.0
    return coef * min(abs(gap), cap * 20.0) * min(abs(steer) / 35.0, 1.0)


def centerline_bonus(gap: float, coef: float = 0.08, width: float = 15.0) -> float:
    return coef * max(0.0, 1.0 - abs(gap) / width)


def correct_steer_bonus(gap: float, steer: float, coef: float = 0.10) -> float:
    if coef <= 0.0 or gap * steer >= 0.0:
        return 0.0
    return coef * min(abs(gap) / 12.0, 1.0) * min(abs(steer) / 35.0, 1.0)


# ---------- pytest tests ----------

def test_progress_linear_floor():
    assert progress_factor_linear(0.0) == 1.0
    assert progress_factor_linear(6.0) == 0.5
    assert progress_factor_linear(12.0) == 0.0
    assert progress_factor_linear(20.0) == 0.0


def test_progress_smooth_never_zero():
    assert progress_factor_smooth(0.0) == 1.0
    assert progress_factor_smooth(8.0) == 0.5
    assert progress_factor_smooth(16.0) > 0.0
    assert progress_factor_smooth(50.0) > 0.0


def test_wrong_way_penalty():
    assert wrong_way(-10.0, -35.0) > 0.0
    assert wrong_way(10.0, 35.0) > 0.0
    assert wrong_way(-10.0, 20.0) == 0.0
    assert wrong_way(10.0, -20.0) == 0.0


def test_centerline_bonus_proximity():
    assert centerline_bonus(0.0) == 0.08
    assert centerline_bonus(7.5) == 0.04
    assert centerline_bonus(15.0) == 0.0
    assert centerline_bonus(20.0) == 0.0


def test_correct_steer_bonus():
    assert correct_steer_bonus(-5.0, 20.0) > 0.0
    assert correct_steer_bonus(5.0, -20.0) > 0.0
    assert correct_steer_bonus(-5.0, -20.0) == 0.0
    assert correct_steer_bonus(5.0, 20.0) == 0.0
    assert correct_steer_bonus(0.0, 20.0) == 0.0


def test_correct_vs_wrong_contrast():
    """Steering the right way must give significantly better reward than wrong way."""
    gap = -5.0
    good = correct_steer_bonus(gap, 20.0) - wrong_way(gap, 20.0)
    bad = correct_steer_bonus(gap, -20.0) - wrong_way(gap, -20.0)
    assert good > 0.0
    assert bad < 0.0
    assert good - bad > 0.15


def test_old_negative_progress_fixed():
    speed_kmh = 60.0
    r_old = speed_kmh * (1.0 - 20.0 / 12.0) / 300.0
    r_smooth = speed_kmh * progress_factor_smooth(20.0) / 300.0
    assert r_old < 0.0
    assert r_smooth > 0.0


def main() -> int:
    test_progress_linear_floor()
    test_progress_smooth_never_zero()
    test_wrong_way_penalty()
    test_centerline_bonus_proximity()
    test_correct_steer_bonus()
    test_correct_vs_wrong_contrast()
    test_old_negative_progress_fixed()
    print("OK all reward tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
