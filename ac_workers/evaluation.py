"""Score d'évaluation d'un candidat — privilégie tours > temps > progression > régularité.

Ne plus classer les cerveaux selon la récompense par seconde : un score
exceptionnel sur une courte ligne droite ne doit jamais faire un champion.
L'ordre de priorité est strictement :
  1. nombre de tours complets valides ;
  2. temps du meilleur tour (plus vite = mieux) ;
  3. à défaut de tour, la progression valide maximale ;
  4. la régularité sur plusieurs essais (moyenne de progression).
"""
from __future__ import annotations

import math
from typing import Iterable, Mapping

# Poids lexicographiques : un tour complet écrase toujours le reste.
LAP_WEIGHT = 1000.0
PROGRESS_WEIGHT = 0.1
REGULARITY_WEIGHT = 0.05


def evaluation_score(episodes: Iterable[Mapping]) -> float:
    """Calcule le score d'un candidat à partir de ses épisodes d'évaluation.

    Chaque épisode doit fournir :
      - ``laps`` : nombre de tours complets valides (int >= 0) ;
      - ``lap_time_sec`` : temps du meilleur tour de l'épisode (float, > 0
        seulement si ``laps > 0``) ;
      - ``progress_m`` : progression valide maximale de l'épisode (float >= 0).
    """
    eps = list(episodes)
    if not eps:
        return float("-inf")

    total_laps = int(sum(int(e.get("laps", 0)) for e in eps))
    lap_times = [float(e["lap_time_sec"]) for e in eps if int(e.get("laps", 0)) > 0]
    progress = [max(0.0, float(e.get("progress_m", 0.0))) for e in eps]

    # Priorité 1 : tours complets (dominant).
    lap_term = total_laps * LAP_WEIGHT
    # Priorité 2 : temps du meilleur tour (négatif, plus vite = moins négatif).
    time_term = -min(lap_times) if lap_times else 0.0
    # Priorité 3 : progression maximale.
    progress_term = max(progress) * PROGRESS_WEIGHT
    # Priorité 4 : régularité (moyenne de progression).
    regular_term = (sum(progress) / len(progress)) * REGULARITY_WEIGHT

    return lap_term + time_term + progress_term + regular_term
