"""Test hors-ligne de l'orchestrateur de générations + de la politique candidat."""
import sys
import os
import numpy as np

REPO = r"C:\Leo projet\Assetto Corsa ai"
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "assetto_corsa_gym"))

from ac_workers.neuroevolution import NeuroEvolution, CandidatePolicy
from ac_workers.evolution_runner import EvolutionRunner

# Population réduite pour le test (12 candidats, 6 workers = 2 lots).
evo = NeuroEvolution(population_size=12, elitism=2, parents=6)
evo.init_generation(seed=1)
runner = EvolutionRunner(evo, n_workers=6)

# 1. CandidatePolicy : forward MLP → 3 actions tanh.
net = evo.candidate_net(0)
pol = CandidatePolicy(net, worker_id=0)
tr = pol.select_action_traced(np.zeros(8, dtype=np.float32), None)
assert tr["policy_action_source"] == "neuroevolution", tr
assert np.all(np.abs(tr["policy_raw_action"]) <= 1.0), tr
print("CandidatePolicy action=%s physique=%s" % (tr["policy_raw_action"], tr["physical_action"]))

# 2. Boucle de génération simulée : assigner + scorer + avancer.
rng = np.random.default_rng(0)
advanced = None
for _ in range(10):
    for w in range(6):
        c = runner.assign_next(w)
        if c is not None:
            runner.record_score(c, float(rng.uniform(0, 100)))
    advanced = runner.advance_if_done()
    if advanced is not None:
        break

assert advanced is not None, "la génération aurait dû avancer"
assert evo.generation == 1
print("génération avancée:", advanced["generation"], "| meilleur score:", round(advanced["best_score"], 2))
print("meilleur candidat:", advanced["best_index"], "(élite en tête de la nouvelle population)")

# 3. La génération suivante est prête à être ré-évaluée.
assert not evo.all_evaluated()
print("candidats réévaluables:", sum(1 for i in range(12) if not evo.evaluated[i]))

print("\nOK: runner (assignation + sélection) et CandidatePolicy vérifiés.")
