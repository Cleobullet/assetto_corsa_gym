"""Calcule les 6 points de spawn fixes de Monza depuis la géométrie réelle.

Chaque spawn = un point de la ligne centrale (surface praticable) + le cap
(orientation dans le sens de circulation). Les 6 points sont répartis
uniformément sur le circuit (fractions 0/6 ... 5/6 de la spline).
"""
import json
import os
import sys

import numpy as np
import pandas as pd

REPO = r"C:\Leo projet\Assetto Corsa ai"
TRACK = os.path.join(REPO, "assetto_corsa_gym", "AssettoCorsaConfigs", "tracks", "monza.csv")
OUT = os.path.join(REPO, "config", "spawns_monza.json")

track = pd.read_csv(TRACK)
x = track.pos_x.values
y = track.pos_y.values
n = len(x)

# Tangente de la ligne centrale (cap) : vecteur vers le point suivant.
def heading(i):
    j = (i + 1) % n
    dx = x[j] - x[i]
    dy = y[j] - y[i]
    return float(np.degrees(np.arctan2(dy, dx)))

spawns = {}
for w in range(6):
    idx = int(round(w * n / 6)) % n
    spawns[f"worker_{w}"] = {
        "pos_x": float(x[idx]),
        "pos_y": float(y[idx]),
        "yaw_deg": heading(idx),
        "fraction": round(idx / n, 6),
    }

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(spawns, f, indent=2)

print("Spawns Monza (6 points fixes, répartis sur le circuit) :")
for w in sorted(spawns):
    s = spawns[w]
    print(
        "  %s : fraction=%.3f  pos=(%8.1f, %8.1f)  yaw=%+7.2f°"
        % (w, s["fraction"], s["pos_x"], s["pos_y"], s["yaw_deg"])
    )
print("\nÉcrit dans:", OUT)
