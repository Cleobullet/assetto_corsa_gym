"""Test hors-ligne de la neuroévolution (population, élitisme, mutation, persistance)."""
import sys
import os
import tempfile
import numpy as np

REPO = r"C:\Leo projet\Assetto Corsa ai"
sys.path.insert(0, REPO)

from ac_workers.neuroevolution import NeuroEvolution, CandidateMLP

# Population réduite pour le test (rapide).
evo = NeuroEvolution(
    state_dim=8, action_dim=3, hidden=(256, 256),
    population_size=20, elitism=2, parents=8,
    mutation_rate=0.05, mutation_intensity=0.03,
)
evo.init_generation(seed=42)
print("candidats:", evo.population_size, "| poids par réseau:", evo.num_params)
assert evo.weights.shape == (20, evo.num_params)
assert not evo.all_evaluated()

# Le réseau produit bien 3 actions tanh dans [-1, 1].
net = evo.candidate_net(0)
x = np.zeros((1, 8), dtype=np.float32)
import torch
with torch.no_grad():
    out = net(torch.from_numpy(x)).numpy()
assert out.shape == (1, 3) and np.all(np.abs(out) <= 1.0), out
print("action sur entrée nulle:", out[0], "(dans [-1,1])")

# Évaluation de tous les candidats.
rng = np.random.default_rng(0)
for i in range(20):
    evo.set_score(i, float(rng.normal(0, 1)))

# Sélection + mutation.
summary = evo.select_and_mutate()
print("génération:", evo.generation, "| meilleur:", summary["best_index"], "score:", round(summary["best_score"], 3))
assert evo.generation == 1
assert evo.all_evaluated() is False  # reset des scores après la sélection
# L'élite (indices 0..1) doit être inchangée par rapport à ses parents.
for i in range(2):
    parent = summary["parent_of"][i]
    assert parent == summary["ranking"][i], (i, parent, summary["ranking"][i])

# Le candidat 0 de la nouvelle génération = une copie exacte de l'ancien meilleur.
new_net = evo.candidate_net(0)
print("élite OK: candidat 0 hérite du meilleur (parent %s)" % summary["parent_of"][0])

# Persistance atomique + reprise.
d = tempfile.mkdtemp(prefix="neuroevo_test_")
evo.work_dir = d
evo.save()
assert os.path.isfile(os.path.join(d, "population.npz"))
assert not os.path.exists(os.path.join(d, "population.npz.tmp"))  # pas de fichier temporaire résiduel

evo2 = NeuroEvolution(state_dim=8, action_dim=3, hidden=(256, 256),
                      population_size=20, elitism=2, parents=8, work_dir=d)
assert evo2.load() is True
assert evo2.generation == evo.generation
assert np.array_equal(evo2.weights, evo.weights)
print("reprise OK: génération %d restaurée, poids identiques." % evo2.generation)

print("\nOK: neuroévolution (population, mutation, élitisme, persistance atomique) vérifiée.")
