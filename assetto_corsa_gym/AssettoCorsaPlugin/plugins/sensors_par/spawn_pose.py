"""Safe track pose for a one-shot teleport. No Assetto Corsa imports."""
import math


def spawn_pose(pos, ahead, lift=0.4):
    """Return (position, flat direction), or None when the spline point is unusable."""
    try:
        px, py, pz = float(pos[0]), float(pos[1]), float(pos[2])
        ax, az = float(ahead[0]), float(ahead[2])
    except (TypeError, ValueError, IndexError):
        return None
    if not all(math.isfinite(v) for v in (px, py, pz, ax, az)):
        return None
    # The world origin means the track spline is not ready yet.
    if abs(px) < 1.0 and abs(pz) < 1.0:
        return None
    dx = ax - px
    dz = az - pz
    norm = math.hypot(dx, dz)
    if norm < 0.5:
        return None
    return (px, py + float(lift), pz), (dx / norm, 0.0, dz / norm)
