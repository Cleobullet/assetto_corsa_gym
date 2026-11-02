# État fonctionnel — 30 octobre 2026 (version V11)

Ce document consigne la solution qui **fonctionne enfin**, pour ne pas la reperdre
(c'est la 3e ou 4e fois que le scale-test se cassait).

## Ce qui a été corrigé (et pourquoi ça marchait pas)

### La vraie cause racine : le drapeau `policy_ready`

Quand un worker atteint « idle » (jeu chargé + attaché), le serveur C4D vérifie un
drapeau **`policy_ready`** pour le déclarer « prêt ». Ce drapeau était rempli par
l'**ancien système SAC**, mais **la neuroévolution ne le remplissait pas**.

→ Le C4D voyait un worker chargé mais le considérait « pas prêt » → il attendait →
le timeout (ou l'anti-gel) le **tuait**. D'où le « s'ouvre → se referme » permanent.

**Correctif** : le candidat neuroévolution (`CandidatePolicy`) renvoie maintenant
`policy_ready = true` dans son statut.

### Autres correctifs (tous nécessaires)

1. **Raccourci** : doit lancer `scripts/Open-AI-App.ps1` (rebuild + copie), pas
   l'ancien exe. Le titre affiche la **version** (`V11`) pour vérifier.
2. **Anti-gel** : le serveur ne confond plus « pas encore de télémétrie » (valeur
   `999999`) avec « télémétrie gelée ». Seul un vrai gel (5 min) tue un worker.
3. **Timeout de chargement** : 1 minute par worker. Si ça dépasse, il est fermé et
   on passe au suivant.
4. **Statuts JSON valides** : les valeurs infinies (`-inf`, `NaN`) sont converties
   en `null`, sinon l'app .NET rejette le fichier (workers « absent »).
5. **Boutons** : « Démarrer l'apprentissage » → `evolve start`, « Arrêter » →
   `evolve stop` (l'ancien `learner` ne faisait rien).
6. **Steam hors-ligne** : démarre en mode hors-ligne (plus de pic Internet).
7. **Arrêt d'urgence** : tue aussi le serveur C4D (sinon il relançait tout).

## Réglages actuels

| Réglage | Valeur | Rôle |
|---|---|---|
| Pause entre lancements | 5 secondes | vitesse de démarrage |
| Délai anti-gel | 5 minutes | tolérance aux pauses CPU |
| Timeout de chargement | 1 minute | fermer + passer au suivant |

## Outil de diagnostic

Si un worker se referme encore, lance (dans le terminal) :

```
python -m ac_workers.debug_worker_crash
```

Ça lit le journal `ac_workers/logs/worker_events.log` et donne la **raison exacte**
de chaque fermeture (au lieu de deviner).

## Comment redémarrer proprement

1. Fermer l'app (la croix), rouvrir le raccourci.
2. Vérifier que le titre affiche **V11** (la dernière version).
3. « Démarrer les 6 workers ».
4. « Démarrer l'apprentissage » (onglet Évolution pour la progression).

Pas besoin de redémarrer l'ordinateur : les changements s'appliquent au
redémarrage de l'app (le serveur se relance tout seul).
