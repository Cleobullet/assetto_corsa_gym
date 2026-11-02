"""LiDAR virtuel à 5 rayons — nouvelle architecture à 8 observations.

Cinq rayons à angles fixes dans le repère local de la voiture :
    -60°, -30°, 0° (droit devant), +30°, +60°
(convention : négatif = gauche, positif = droite).

Portée maximale : 100 m. Les obstacles sont les limites de piste
(bordures gauche et droite) chargées depuis la géométrie du circuit.

Normalisation : chaque rayon renvoie une valeur dans [0, 1] où 1 signifie
qu'aucune limite n'est détectée dans les 100 m, et 0 que la voiture touche
la bordure. Une mesure invalide (non-finie ou de forme inattendue) lève une
erreur explicite au lieu d'être silencieusement remplacée par une valeur
fictive — conformément au cahier des charges (pas de simulation de mesure).
"""
from __future__ import annotations

import numpy as np

from AssettoCorsaEnv.sensors_ray_casting import (
    SpatialHash,
    ray_casting_vectorized_v2,
)

# Angles des 5 rayons, en degrés, dans le repère local de la voiture.
RAY_ANGLES_DEG = (-60.0, -30.0, 0.0, +30.0, +60.0)
N_RAYS = len(RAY_ANGLES_DEG)
MAX_RANGE_M = 100.0


class LiDAR5Rays:
    """LiDAR à 5 rayons fondé sur les limites de piste (segments)."""

    def __init__(self, left_border_x, left_border_y, right_border_x, right_border_y):
        left = self._to_segments(left_border_x, left_border_y)
        right = self._to_segments(right_border_x, right_border_y)
        self.walls = np.vstack([left, right]).astype(np.float64)
        if self.walls.ndim != 2 or self.walls.shape[1] != 4 or self.walls.shape[0] == 0:
            raise ValueError("LiDAR: aucune limite de piste valide chargée")
        self.scene = SpatialHash(self.walls)
        self.angles_rad = np.deg2rad(RAY_ANGLES_DEG)

    @staticmethod
    def _to_segments(x, y) -> np.ndarray:
        x = np.asarray(x, dtype=np.float64).reshape(-1)
        y = np.asarray(y, dtype=np.float64).reshape(-1)
        if x.shape[0] < 2 or x.shape != y.shape:
            raise ValueError("LiDAR: séquence de points de bordure invalide")
        return np.vstack([x[:-1], y[:-1], x[1:], y[1:]]).T

    def cast(self, x: float, y: float, yaw: float) -> np.ndarray:
        """Lance les 5 rayons depuis (x, y), cap `yaw` (radians, repère monde).

        Retourne un ndarray float64 de forme (5,) dans [0, 1].
        """
        x = float(x)
        y = float(y)
        yaw = float(yaw)

        # Angles dans le repère monde = cap de la voiture + angle local.
        angles = yaw + self.angles_rad
        point = np.array([[x, y]], dtype=np.float64)

        # Fenêtre de recherche : MAX_RANGE_M de chaque côté du point.
        rect = [x - MAX_RANGE_M, y - MAX_RANGE_M, x + MAX_RANGE_M, y + MAX_RANGE_M]
        wall_indices = self.scene.query(rect)
        if not wall_indices:
            # Aucune bordure dans la fenêtre de 100 m : tout est dégagé.
            return np.ones(N_RAYS, dtype=np.float64)

        walls = self.walls[wall_indices]
        _, dists = ray_casting_vectorized_v2(point, angles, walls, MAX_RANGE_M)
        dists = np.asarray(dists, dtype=np.float64).reshape(-1)
        if dists.shape == (1,):
            # La fonction renvoie un seul max_distance quand AUCUN des 5 rayons
            # ne touche de bordure : tout est dégagé → 5 valeurs à 1.0.
            return np.ones(N_RAYS, dtype=np.float64)
        if dists.shape != (N_RAYS,):
            raise RuntimeError(
                "LiDAR: mesure de forme inattendue shape={0}".format(dists.shape)
            )
        norm = np.clip(dists / MAX_RANGE_M, 0.0, 1.0)
        if not np.all(np.isfinite(norm)):
            raise RuntimeError("LiDAR: mesure non-finie détectée")
        return norm
