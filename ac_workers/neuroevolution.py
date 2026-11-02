"""Neuroévolution — population de candidats, générations, élitisme + mutation.

Réseau : 8 entrées → 256 → 256 → 3 sorties (tanh → [-1, +1]).
Chaque candidat est un réseau identifié par un index stable. La population
est évaluée par les 6 workers (par groupes de 6), puis classée, sélectionnée
(élitisme + parents) et mutée pour produire la génération suivante.

Persistance : un unique fichier ``population.npz`` (poids, scores, parenté)
écrit atomiquement (fichier temporaire + rename), plus ``generation.json``
pour les métadonnées rejouables. Reprise possible après interruption.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

import numpy as np
import torch
import torch.nn as nn

STATE_DIM = 8
ACTION_DIM = 3
HIDDEN = (256, 256)

DEFAULT_POPULATION = 120
DEFAULT_ELITISM = 2
DEFAULT_PARENTS = 10
DEFAULT_MUTATION_RATE = 0.05   # fraction des poids mutés par candidat
DEFAULT_MUTATION_INTENSITY = 0.03  # écart-type du bruit gaussien


class CandidateMLP(nn.Module):
    """MLP 8 → 256 → 256 → 3 avec sortie tanh dans [-1, +1]."""

    def __init__(self, state_dim=STATE_DIM, action_dim=ACTION_DIM, hidden=HIDDEN):
        super().__init__()
        self.state_dim = int(state_dim)
        self.action_dim = int(action_dim)
        self.hidden = [int(h) for h in hidden]
        self.fc1 = nn.Linear(self.state_dim, self.hidden[0])
        self.fc2 = nn.Linear(self.hidden[0], self.hidden[1])
        self.fc3 = nn.Linear(self.hidden[1], self.action_dim)

    def forward(self, x):
        x = torch.relu(self.fc1(x))
        x = torch.relu(self.fc2(x))
        return torch.tanh(self.fc3(x))

    def param_shapes(self):
        return [tuple(p.shape) for p in self.parameters()]

    def num_params(self):
        return sum(p.numel() for p in self.parameters())

    def flat_weights(self) -> np.ndarray:
        return np.concatenate(
            [p.detach().cpu().numpy().ravel() for p in self.parameters()]
        ).astype(np.float32)

    def load_flat_weights(self, flat: np.ndarray):
        flat = np.asarray(flat, dtype=np.float32)
        idx = 0
        for p in self.parameters():
            n = p.numel()
            p.data.copy_(
                torch.from_numpy(flat[idx:idx + n].reshape(p.shape))
            )
            idx += n
        if idx != flat.size:
            raise ValueError("NeuroEvolution: taille de poids incohérente")


class NeuroEvolution:
    """Population évolutionnaire de réseaux identiques, sélection + mutation."""

    def __init__(
        self,
        state_dim: int = STATE_DIM,
        action_dim: int = ACTION_DIM,
        hidden: tuple = HIDDEN,
        population_size: int = DEFAULT_POPULATION,
        elitism: int = DEFAULT_ELITISM,
        parents: int = DEFAULT_PARENTS,
        mutation_rate: float = DEFAULT_MUTATION_RATE,
        mutation_intensity: float = DEFAULT_MUTATION_INTENSITY,
        work_dir: Optional[str] = None,
    ):
        self.state_dim = int(state_dim)
        self.action_dim = int(action_dim)
        self.hidden = tuple(int(h) for h in hidden)
        self.population_size = int(population_size)
        self.elitism = int(elitism)
        self.parents = int(parents)
        self.mutation_rate = float(mutation_rate)
        self.mutation_intensity = float(mutation_intensity)

        if not (0 <= self.elitism < self.parents <= self.population_size):
            raise ValueError("NeuroEvolution: il faut 0 <= élitisme < parents <= population")

        self.work_dir = work_dir
        self._template = CandidateMLP(self.state_dim, self.action_dim, self.hidden)
        self.num_params = self._template.num_params()

        self.generation = 0
        self.seed = 0
        self.weights: np.ndarray = np.zeros((0, self.num_params), dtype=np.float32)
        self.scores: np.ndarray = np.zeros(0, dtype=np.float32)
        self.parent_of: np.ndarray = np.zeros(0, dtype=np.int32)
        self.evaluated: np.ndarray = np.zeros(0, dtype=bool)

    # ---- génération ----
    def init_generation(self, seed: int = 0, scale: float = 0.5):
        """Crée une population diversifiée de réseaux aléatoires."""
        rng = np.random.default_rng(int(seed))
        self.seed = int(seed)
        self.generation = 0
        self.weights = rng.normal(0.0, scale, (self.population_size, self.num_params)).astype(np.float32)
        # Initialisation par couche plus propre : Xavier, replié en plat.
        self.weights = self._xavier_init(rng)
        self.scores = np.full(self.population_size, -np.inf, dtype=np.float32)
        self.parent_of = np.full(self.population_size, -1, dtype=np.int32)
        self.evaluated = np.zeros(self.population_size, dtype=bool)
        return self

    def _xavier_init(self, rng) -> np.ndarray:
        flat = np.zeros((self.population_size, self.num_params), dtype=np.float32)
        idx = 0
        for shape in self._template.param_shapes():
            n = int(np.prod(shape))
            fan_in = shape[1] if len(shape) == 2 else 1
            bound = np.sqrt(6.0 / max(fan_in + (shape[0] if len(shape) == 2 else 0), 1.0))
            vals = rng.uniform(-bound, bound, (self.population_size, n))
            flat[:, idx:idx + n] = vals
            idx += n
        return flat

    def set_score(self, index: int, score: float):
        self.scores[int(index)] = float(score)
        self.evaluated[int(index)] = True

    def all_evaluated(self) -> bool:
        return bool(self.evaluated.all())

    def candidate_net(self, index: int) -> CandidateMLP:
        net = CandidateMLP(self.state_dim, self.action_dim, self.hidden)
        net.load_flat_weights(self.weights[int(index)])
        return net

    # ---- sélection + mutation ----
    def select_and_mutate(self, seed: Optional[int] = None) -> dict:
        """Classe, applique l'élitisme, sélectionne les parents et mute.

        Retourne un résumé : rang, score et parent de chaque candidat.
        """
        rng = np.random.default_rng(seed if seed is not None else self.seed + self.generation + 1)
        order = np.argsort(self.scores)[::-1]  # meilleur d'abord
        new_weights = np.empty_like(self.weights)
        new_parent = np.full(self.population_size, -1, dtype=np.int32)

        # Élitisme : les meilleurs passent inchangés.
        for i in range(self.elitism):
            src = order[i]
            new_weights[i] = self.weights[src]
            new_parent[i] = src

        # Le reste : enfant = parent (parmi les `parents` meilleurs) + mutation.
        for i in range(self.elitism, self.population_size):
            pick = order[rng.integers(0, self.parents)]
            new_weights[i] = self._mutate(self.weights[pick], rng)
            new_parent[i] = pick

        summary = {
            "generation": int(self.generation),
            "best_index": int(order[0]),
            "best_score": float(self.scores[order[0]]),
            "ranking": [int(i) for i in order],
            "parent_of": [int(p) for p in new_parent],
        }
        self.weights = new_weights
        self.parent_of = new_parent
        self.scores = np.full(self.population_size, -np.inf, dtype=np.float32)
        self.evaluated = np.zeros(self.population_size, dtype=bool)
        self.generation += 1
        return summary

    def _mutate(self, w: np.ndarray, rng) -> np.ndarray:
        mask = rng.random(w.shape) < self.mutation_rate
        noise = rng.normal(0.0, self.mutation_intensity, w.shape)
        return (w + mask * noise).astype(np.float32)

    # ---- persistance atomique ----
    def save(self):
        if not self.work_dir:
            raise ValueError("NeuroEvolution: work_dir non défini")
        os.makedirs(self.work_dir, exist_ok=True)
        tmp = os.path.join(self.work_dir, "population.tmp.npz")
        np.savez_compressed(
            tmp,
            weights=self.weights,
            scores=self.scores,
            parent_of=self.parent_of,
            evaluated=self.evaluated,
            generation=np.array([self.generation]),
            seed=np.array([self.seed]),
        )
        os.replace(tmp, os.path.join(self.work_dir, "population.npz"))
        with open(os.path.join(self.work_dir, "generation.json"), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "generation": self.generation,
                    "seed": self.seed,
                    "population_size": self.population_size,
                    "elitism": self.elitism,
                    "parents": self.parents,
                    "mutation_rate": self.mutation_rate,
                    "mutation_intensity": self.mutation_intensity,
                    "state_dim": self.state_dim,
                    "action_dim": self.action_dim,
                    "hidden": list(self.hidden),
                },
                f,
                indent=2,
            )
        # Résumé lisible par l'UI (AcAiControl) sans parser le .npz.
        self.write_summary()

    def write_summary(self):
        """Écrit UNIQUEMENT le résumé lisible par l'UI (léger, sans le .npz)."""
        if not self.work_dir:
            return
        _best = int(np.argmax(self.scores)) if self.scores.size else -1
        payload = {
            "generation": self.generation,
            "evaluated": int(self.evaluated.sum()),
            "population_size": self.population_size,
            "best_index": _best,
            "best_score": float(self.scores[_best]) if _best >= 0 else None,
        }
        # Écriture ATOMIQUE (temp + os.replace + retry) : l'app C# lit
        # summary.json à chaque tick de l'UI, et un open(..., "w") concurrent
        # verrouille le fichier → PermissionError → le thread d'évolution crashe
        # et la génération se fige (vu : génération bloquée à 11/120).
        path = os.path.join(self.work_dir, "summary.json")
        tmp = os.path.join(self.work_dir, "summary.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        for _attempt in range(8):
            try:
                os.replace(tmp, path)
                return
            except OSError:
                time.sleep(0.05)
        # Dernier essai : laisser l'exception remonter si le fichier reste verrouillé.
        os.replace(tmp, path)

    def load(self) -> bool:
        path = os.path.join(self.work_dir, "population.npz")
        if not os.path.isfile(path):
            return False
        data = np.load(path, allow_pickle=False)
        self.weights = data["weights"]
        self.scores = data["scores"]
        self.parent_of = data["parent_of"]
        self.evaluated = data["evaluated"]
        self.generation = int(data["generation"][0])
        self.seed = int(data["seed"][0])
        return True


class CandidatePolicy:
    """Politique déterministe d'un candidat : forward MLP (8→256→256→3).

    Interface compatible avec le collecteur (``select_action_traced``). La
    mutation de la neuroévolution remplace le bruit d'exploration : ici la
    politique est déterministe (pas de bruit, pas de SAC, pas de gradient).
    """

    def __init__(self, net, worker_id=0):
        self.net = net
        self.worker_id = int(worker_id)
        self.policy_seed = 0
        self.exploration_seed = 0
        self.deterministic_mode = True
        self._weights_loaded = True
        self._load_error = ""
        self.action_exploration_noise_std = 0.0
        self.action_exploration_throttle_bias = 0.0
        self.action_exploration_brake_bias = 0.0

    def select_action(self, state, env):
        return np.asarray(
            self.select_action_traced(state, env)["action"], dtype=np.float32
        )

    def reload_if_updated(self):
        return True

    def is_compatible(self):
        return True

    def detect_pending_version(self):
        pass

    def apply_pending_policy(self, episode_id=None):
        pass

    def policy_status(self):
        """État de politique compatible avec le snapshot du collecteur."""
        return {
            "policy_ready": bool(self._weights_loaded),
            "local_policy_version": 0,
            "last_policy_reload_age_sec": None,
            "policy_reload_count": 0,
            "policy_reload_failed_count": 0,
            "last_policy_reload_error": self._load_error,
            "last_good_policy_version": 0,
            "policy_file_size": 0,
            "active_policy_version": 0,
            "pending_policy_version": 0,
            "policy_swap_pending": False,
            "policy_swap_count": 0,
            "last_swap_episode": -1,
            "policy_apply_mode": "episode_boundary",
            "action_exploration_noise_std": self.action_exploration_noise_std,
            "policy_source": "neuroevolution",
        }

    def load_error(self):
        return self._load_error

    def select_action_traced(self, state, env):
        from ac_workers.canonical_controls import raw_action_to_physical

        state_arr = np.asarray(state, dtype=np.float32).ravel()
        neutral = np.array([0.0, -1.0, -1.0], dtype=np.float32)
        trace = {
            "policy_action_source": "neuroevolution",
            "policy_raw_action": [0.0, -1.0, -1.0],
            "physical_action": [0.0, 0.0, 0.0],
            "action": neutral,
            "error": "",
            "policy_seed": self.policy_seed,
            "rng_seed": self.policy_seed,
            "observation_hash": state_arr[:8].tobytes().hex()[:16],
            "worker_id": self.worker_id,
            "deterministic_mode": True,
            "exploration_seed": self.exploration_seed,
            "action_noise": [0.0, 0.0, 0.0],
        }
        try:
            with torch.no_grad():
                out = self.net(
                    torch.from_numpy(state_arr[:8].astype(np.float32)).unsqueeze(0)
                ).numpy()[0]
            raw = [float(np.clip(x, -1.0, 1.0)) for x in out[:3]]
        except Exception as exc:  # ne jamais faire planter la boucle de conduite
            trace["policy_action_source"] = "policy_error"
            trace["error"] = str(exc)
            return trace
        physical = raw_action_to_physical(raw)
        trace.update(
            {
                "policy_action_source": "neuroevolution",
                "policy_raw_action": raw,
                "physical_action": [float(x) for x in physical],
                "action": np.asarray(raw, dtype=np.float32),
            }
        )
        return trace
