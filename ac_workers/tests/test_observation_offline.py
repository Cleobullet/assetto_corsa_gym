"""Test hors-ligne de l'observation à 8 entrées (3 télémétries + 5 rayons)."""
import sys
import os
import numpy as np
import pandas as pd

REPO = r"C:\Leo projet\Assetto Corsa ai"
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "assetto_corsa_gym"))

from AssettoCorsaEnv import ac_env
from AssettoCorsaEnv.lidar import LiDAR5Rays, N_RAYS

TRACK = os.path.join(REPO, "assetto_corsa_gym", "AssettoCorsaConfigs", "tracks", "monza.csv")
track = pd.read_csv(TRACK)

# 3 canaux de télémétrie attendus, dans l'ordre.
channels = ac_env.AssettoCorsaEnv.obs_enabled_channels
print("canaux:", channels)
assert channels == ["local_velocity_x", "local_velocity_y", "angular_velocity_y"], channels
assert len(channels) == 3

# Échelles de normalisation (signe préservé : division par une échelle > 0).
scales = np.array([ac_env.AssettoCorsaEnv.obs_channels_info[ch] for ch in channels])
print("échelles:", scales)

lidar = LiDAR5Rays(
    track.left_border_x.values, track.left_border_y.values,
    track.right_border_x.values, track.right_border_y.values,
)

# État synthétique cohérent avec la télémétrie attendue.
pos = np.vstack([track.pos_x.values, track.pos_y.values]).T
tangent = np.diff(pos, axis=0)
yaw = np.arctan2(tangent[:, 1], tangent[:, 0])

for idx in (0, 700, 1400, 2100, 2800):
    state = {
        "local_velocity_x": 12.0,
        "local_velocity_y": -1.5,
        "angular_velocity_y": 0.3,
        "world_position_x": pos[idx][0],
        "world_position_y": pos[idx][1],
        "yaw": yaw[idx],
    }
    tele = np.array([state[ch] for ch in channels], dtype=np.float32) / scales
    rays = lidar.cast(state["world_position_x"], state["world_position_y"], state["yaw"])
    obs = np.hstack([tele, rays])

    assert obs.shape == (8,), obs.shape
    assert np.all(np.isfinite(obs)), obs
    assert np.all(rays >= 0.0) and np.all(rays <= 1.0), rays
    # La vitesse longitudinale signée doit garder son signe après normalisation.
    assert np.sign(tele[0]) == np.sign(12.0)
    print("idx %4d -> obs[8] = %s" % (idx, " ".join("%+6.3f" % v for v in obs)))

print("\nOK: observation = 8 valeurs (3 télémétries signées + 5 rayons [0,1]).")
