"""Test hors-ligne du LiDAR à 5 rayons contre la géométrie réelle de Monza."""
import sys
import os
import numpy as np
import pandas as pd

REPO = r"C:\Leo projet\Assetto Corsa ai"
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "assetto_corsa_gym"))

from AssettoCorsaEnv.lidar import LiDAR5Rays, MAX_RANGE_M, RAY_ANGLES_DEG

TRACK = os.path.join(REPO, "assetto_corsa_gym", "AssettoCorsaConfigs", "tracks", "monza.csv")
RL = os.path.join(REPO, "assetto_corsa_gym", "AssettoCorsaConfigs", "tracks", "monza-racing_line.csv")

track = pd.read_csv(TRACK)
rl = pd.read_csv(RL)
print("colonnes monza.csv:", list(track.columns))
print("lignes:", len(track))

lidar = LiDAR5Rays(
    track.left_border_x.values,
    track.left_border_y.values,
    track.right_border_x.values,
    track.right_border_y.values,
)
print("LiDAR initialisé, segments:", lidar.walls.shape[0], "| angles(deg):", RAY_ANGLES_DEG)

# Cap (yaw) local le long de la ligne de course : tangente de la ligne centrale.
pos = np.vstack([track.pos_x.values, track.pos_y.values]).T
tangent = np.diff(pos, axis=0)
yaw = np.arctan2(tangent[:, 1], tangent[:, 0])

# Test sur 5 points répartis le long du circuit.
for idx in (0, 500, 1500, 2500, 3500):
    x, y = pos[idx]
    d = lidar.cast(x, y, yaw[idx])
    assert d.shape == (5,), d.shape
    assert np.all(np.isfinite(d)), d
    assert np.all((d >= 0.0) & (d <= 1.0)), d
    print(
        "point %4d (x=%8.1f y=%8.1f yaw=%+5.2f rad) -> distances (m): %s"
        % (idx, x, y, yaw[idx], " ".join("%6.1f" % (v * MAX_RANGE_M) for v in d))
    )

print("\nOK: 5 rayons valides, bornés [0,1], sur 5 points de Monza.")
