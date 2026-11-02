"""Orchestrateur de générations — assigne les candidats aux workers, collecte
les scores, puis sélectionne + mute à la fin de chaque génération.

La logique d'état est volontairement séparée de l'IPC (C4D) : le runner ne
sait pas comment les workers évaluent, il gère seulement « quel candidat va
sur quel worker » et « quand avancer la génération ».
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from ac_workers.neuroevolution import NeuroEvolution

logger = logging.getLogger(__name__)


class EvolutionRunner:
    """Machine d'état d'une génération : candidats → workers → scores → sélection."""

    def __init__(self, evo: NeuroEvolution, n_workers: int = 6):
        self.evo = evo
        self.n_workers = int(n_workers)
        # worker_id -> (candidate_index, in_progress)
        self.assignment: dict[int, int] = {}
        self.summary: Optional[dict] = None

    def pending_candidates(self) -> list[int]:
        """Indices des candidats pas encore évalués ni en cours."""
        busy = set(self.assignment.values())
        return [i for i in range(self.evo.population_size)
                if not self.evo.evaluated[i] and i not in busy]

    def assign_next(self, worker_id: int) -> Optional[int]:
        """Assigne le prochain candidat libre au worker. Retourne son index
        ou None s'il n'y a plus rien à évaluer."""
        pending = self.pending_candidates()
        if not pending:
            return None
        cand = pending[0]
        self.assignment[int(worker_id)] = cand
        return cand

    def record_score(self, candidate_index: int, score: float):
        self.evo.set_score(int(candidate_index), float(score))
        # libère le worker qui évaluait ce candidat
        for wid, cand in list(self.assignment.items()):
            if cand == int(candidate_index):
                del self.assignment[wid]
        # Résumé léger pour l'UI (le .npz complet n'est sauvé qu'à la génération).
        self.evo.write_summary()

    def advance_if_done(self) -> Optional[dict]:
        """Si tous les candidats sont évalués : sélection + mutation + save.
        Retourne le résumé, ou None si la génération n'est pas finie."""
        if not self.evo.all_evaluated():
            return None
        self.summary = self.evo.select_and_mutate()
        if self.evo.work_dir:
            self.evo.save()
        self.assignment = {}
        return self.summary


# ---- Orchestration (boucle de générations, côté C4D) ----

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATUS_DIR = os.path.join(_REPO, "ac_workers", "status")


def _send_load_candidate(worker_id: int, candidate: int) -> bool:
    from ac_workers.collector_diag import CollectorDiagClient

    try:
        CollectorDiagClient(worker_id, timeout=5).request(
            "load_candidate", {"candidate": int(candidate)}
        )
        return True
    except Exception as exc:  # worker pas encore prêt / injoignable
        logger.warning("evolution: load_candidate worker=%s failed: %s", worker_id, exc)
        return False


def _read_worker_status(worker_id: int) -> Optional[dict]:
    try:
        import json

        with open(
            os.path.join(_STATUS_DIR, "worker_{0}.json".format(worker_id)),
            "r",
            encoding="utf-8",
        ) as fh:
            return json.load(fh)
    except Exception:
        return None


def _score_of(status: dict) -> float:
    """Score neuroévolution d'un worker, ou -inf si absent/non évalué.

    `collector_status._sanitize_json` convertit NaN/±Infinity en `null` (None
    après json.load) — ne pas laisser un `None` fuiter vers une comparaison.
    """
    s = status.get("neuroevolution_score")
    if s is None:
        return float("-inf")
    try:
        return float(s)
    except (TypeError, ValueError):
        return float("-inf")


def run_evolution(
    evo: NeuroEvolution,
    n_workers: int = 6,
    poll_interval: float = 1.0,
    max_generations: Optional[int] = None,
    stop_event=None,
) -> int:
    """Orchestre les générations : assigne les candidats, collecte les scores
    depuis les statuts des workers, avance à la fin de chaque génération.

    Retourne le nombre de générations terminées. S'arrête sur `stop_event`
    (threading.Event) ou après `max_generations`.
    """
    import time

    runner = EvolutionRunner(evo, n_workers=n_workers)
    # Premier lot : un candidat par worker.
    for w in range(n_workers):
        cand = runner.assign_next(w)
        if cand is not None:
            _send_load_candidate(w, cand)

    generations_done = 0
    pending_load = set()
    while stop_event is None or not stop_event.is_set():
        if max_generations is not None and generations_done >= max_generations:
            break
        # Attend que les workers aient appliqué le load_candidate avant de les
        # re-considérer. On vérifie le CANDIDAT affiché (mis à jour immédiatement
        # par load_candidate), pas le score : le score peut rester bloqué sur une
        # valeur périmée (phase idle + write_minimal_status) et laisser le worker
        # en pending_load pour toujours → candidat ré-évalué deux fois.
        for w in list(pending_load):
            status = _read_worker_status(w)
            if status is not None and status.get("neuroevolution_candidate") == runner.assignment.get(w):
                pending_load.discard(w)
        # Collecte les scores des candidats déjà évalués + assigne la suite.
        for w in list(runner.assignment.keys()):
            if w in pending_load:
                continue
            cand = runner.assignment[w]
            status = _read_worker_status(w)
            if not status:
                continue
            score = _score_of(status)
            if score > float("-inf"):
                runner.record_score(cand, score)
                nxt = runner.assign_next(w)
                if nxt is not None:
                    _send_load_candidate(w, nxt)
                    pending_load.add(w)
        summary = runner.advance_if_done()
        if summary is not None:
            generations_done += 1
            logger.info(
                "evolution: generation %d terminée, meilleur candidat %d score %.3f",
                summary["generation"], summary["best_index"], summary["best_score"],
            )
            pending_load.clear()
            for w in range(n_workers):
                cand = runner.assign_next(w)
                if cand is not None:
                    _send_load_candidate(w, cand)
                    pending_load.add(w)
        time.sleep(poll_interval)
    return generations_done
