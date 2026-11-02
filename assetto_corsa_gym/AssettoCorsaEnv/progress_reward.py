"""Récompense simplifiée à 4 règles — nouvelle architecture.

1. Progression valide sur le circuit : +reward_progress_per_m par mètre.
2. Temps écoulé : -reward_time_per_sec par seconde.
3. Collision invalidante / sortie de piste : -reward_collision_penalty (terminal).
4. Tour complet valide : +reward_clean_lap_bonus.

Anti-exploitation :
- la progression est mesurée sur la spline du circuit (pas la distance monde),
  donc marcher en arrière, osciller sur place ou couper la piste ne rapporte rien ;
- un tour n'est crédité que s'il couvre une fraction minimale du circuit
  sans faute (sortie de piste ou marche arrière l'invalide).
"""
import math

from AssettoCorsaEnv.telemetry_consistency import stationary_signal_inconsistent


def is_currently_off_track(gap, lap_invalidated=False, oot_gap_m=12.0, soft_gap_m=8.0):
    """True only while the car is away from the line now.

    LapInvalidated stays on for the rest of the lap after a kerb. It may
    confirm a current excursion (gap already large) but must not keep a
    recovered car marked off-track.
    """
    try:
        abs_gap = abs(float(gap))
    except (TypeError, ValueError):
        return False
    if abs_gap > float(oot_gap_m):
        return True
    return bool(lap_invalidated) and abs_gap > float(soft_gap_m)


def spline_delta_m(old_fraction, fraction, track_length):
    return ((float(fraction) - float(old_fraction) + 0.5) % 1.0 - 0.5) * float(track_length)


def update_wrong_way(meters, delta_m, confirm_m):
    """Accumulate reverse metres. True only while still driving the wrong way."""
    meters = float(meters) + float(delta_m)
    if meters > 0.0:
        return 0.0, False
    # Recovering toward the racing direction must not keep the reverse timer running.
    if float(delta_m) > 0.0:
        return meters, False
    if meters <= -abs(float(confirm_m)):
        return meters, True
    return meters, False


class CleanLapTracker:
    """Suit les tours complets valides. Une faute invalide le tour en cours."""

    def __init__(self, lap_bonus=20.0, min_lap_fraction=0.85):
        self.lap_bonus = float(lap_bonus)
        self.min_lap_fraction = float(min_lap_fraction)
        self.reset()

    def reset(self):
        self.lap_faulted = False
        self.last_fraction = None
        self.forward_m = 0.0

    def note_fault(self):
        self.lap_faulted = True

    def _empty(self):
        return {
            "reward_clean_sector": 0.0,
            "reward_clean_lap": 0.0,
            "completed_clean_sector": False,
            "completed_clean_lap": False,
            "clean_lap_time_sec": 0.0,
        }

    def update(self, state, signed_progress_m, progress_valid, track_length):
        if not progress_valid:
            self.note_fault()
            self.last_fraction = None
            self.forward_m = 0.0
            return self._empty()
        if (
            bool(state.get("out_of_track", False))
            or float(state.get("going_backwards") or 0.0) > 0.0
        ):
            self.note_fault()
        try:
            fraction = float(state["NormalizedSplinePosition"])
        except (KeyError, TypeError, ValueError):
            return self._empty()
        if not (0.0 <= fraction <= 1.0):
            return self._empty()
        if self.last_fraction is None:
            self.last_fraction = fraction
            return self._empty()
        if signed_progress_m > 0:
            self.forward_m += float(signed_progress_m)
        wrapped = (
            self.last_fraction > 0.70
            and fraction < 0.30
            and self.forward_m >= self.min_lap_fraction * float(track_length)
        )
        lap_r = 0.0
        completed_lap = False
        if wrapped:
            if not self.lap_faulted:
                lap_r = self.lap_bonus
                completed_lap = True
            self.lap_faulted = False
            self.forward_m = 0.0
        self.last_fraction = fraction
        result = self._empty()
        result["reward_clean_lap"] = float(lap_r)
        result["completed_clean_lap"] = bool(completed_lap)
        return result


def progress_reward(previous, state, track_length, config, actions_diff, tracker=None):
    """Récompense à 4 règles : progression + temps + collision + tour."""
    progress = 0.0
    dt = 0.0
    valid = False
    if previous is not None and track_length > 0:
        try:
            dt = float(state["timestamp_ac"]) - float(previous["timestamp_ac"])
            fraction = float(state["NormalizedSplinePosition"])
            old_fraction = float(previous["NormalizedSplinePosition"])
            delta = spline_delta_m(old_fraction, fraction, track_length)
            dx = float(state["world_position_x"]) - float(previous["world_position_x"])
            dy = float(state["world_position_y"]) - float(previous["world_position_y"])
            limit = (max(abs(float(state["speed"])), abs(float(previous["speed"]))) + 10.0) * dt + 2.0
            is_wrap = old_fraction > 0.70 and fraction < 0.30
            valid = (
                all(math.isfinite(x) for x in (dt, fraction, old_fraction, delta, dx, dy, limit))
                and 0 < dt <= 1.0
                and 0 <= fraction <= 1
                and 0 <= old_fraction <= 1
                and (abs(delta) <= limit or is_wrap)
                and math.hypot(dx, dy) <= limit
                and not previous.get("done")
                and not state.get("teleported", False)
                and not stationary_signal_inconsistent(previous, state)
            )
            if valid:
                progress = delta
        except (KeyError, ValueError, TypeError):
            valid = False

    # Règle 1 : progression valide (avant uniquement) — anti marche-arrière.
    movement = (
        float(getattr(config, "reward_progress_per_m", 0.10)) * max(progress, 0.0)
        if valid
        else 0.0
    )
    # Règle 2 : temps écoulé.
    time_cost = float(getattr(config, "reward_time_per_sec", 0.02)) * dt if valid else 0.0
    # Règle 3 : collision invalidante / sortie de piste (terminal).
    crash_done = bool(state.get("done")) and str(state.get("done_reason") or "") == "out_of_track"
    terminal = float(getattr(config, "reward_collision_penalty", 10.0)) if crash_done else 0.0
    # Règle 4 : tour complet valide.
    milestones = {
        "reward_clean_sector": 0.0,
        "reward_clean_lap": 0.0,
        "completed_clean_sector": False,
        "completed_clean_lap": False,
        "clean_lap_time_sec": 0.0,
    }
    if tracker is not None:
        milestones = tracker.update(state, progress, valid, track_length)

    reward = movement - time_cost - terminal + float(milestones.get("reward_clean_lap", 0.0))

    return {
        "reward": reward,
        "reward_progress": movement,
        "signed_progress_m": progress,
        "progress_valid": bool(valid),
        "reward_time_cost": -time_cost,
        "reward_collision_penalty": -terminal,
        **milestones,
    }
