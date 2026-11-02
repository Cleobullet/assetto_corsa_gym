# Reprise 2 workers — 23 octobre 2026

## Objectif

Préparer un entraînement fiable sur **2 workers** (au lieu de 6) avec la récompense simplifiée, sans lancer le jeu.

## Diagnostic confirmé

Le problème n'était pas la puissance (Ryzen 7 9800X3D + RX 9070 XT + 32 Go). L'ancien entraînement **divergeait** (valeurs Q qui explosent). La cause principale — le repère d'action incohérent — est **déjà corrigée** dans le code actif : les transitions stockent l'action normalisée du réseau (`policy_raw_action`) et la commande physique est gardée séparément (`physical_action`). L'isolation des workers pour n-step, la reprise complète et la récompense simplifiée sont aussi en place.

## Changements appliqués (config `config_first_sac_cpu_v3.yml`)

| Réglage | Avant | Après | Raison |
|---|---|---|---|
| `SAC.nstep` | 3 | **1** | Signal d'apprentissage plus simple (recommandé par l'audit pour diagnostiquer) |
| `Agent.publish_every_updates` | 1 | **100** | Réduit l'I/O (publication du modèle toutes les 100 mises à jour, au lieu de chaque mise à jour) |

La récompense active est `signed_progress_v1` → `progress_reward()` (progression signée + pénalité sortie + faible coût des à-coups). Elle est déjà la version simplifiée.

## Lancement (application Windows, pas le terminal)

Ouvre l'application **Assetto Corsa AI** (raccourci bureau).

1. Onglet **Paramètres** → « Nombre de workers » = **2** (par défaut), puis **Enregistrer**.
2. Onglet **Sauvegardes** → **Nouvel entraînement** (nomme-le, ex. `sac2w`) — archive l'ancien modèle divergent.
3. Bouton **« Démarrer les 2 workers »** (en haut).
4. Bouton **« Démarrer l'apprentissage »**.
5. Suivre la progression dans l'onglet **Workers** et la ligne « Apprentissage ».
6. **« Arrêter les workers »** pour tout arrêter proprement.

Le nombre de workers se règle dans **Paramètres → Nombre de workers** (1 à 6). Le bouton du haut affiche et lance ce nombre automatiquement.

## Vérifications hors-jeu effectuées le 23 octobre

- Config chargée : `nstep=1`, `reward_mode=signed_progress_v1`, `publish_every_updates=100`, `eval_interval=10000`.
- Récompense : progression avant positive (+0,158 pour 1,6 m), marche arrière négative, hors-piste non récompensée, téléportation rejetée.
- `multi_sac_trainer` s'importe et lit `nstep` depuis la config SAC.

## Reste à faire (hors portée de cette session)

- `config/local-paths.json` est vide : à vérifier avant le lancement. Le script `Get-AcLocalPaths.ps1` tente une détection automatique (Steam/défaut `AssettoCorsa-AI\assettocorsa`/Documents), mais il faudra confirmer que les chemins sont trouvés au premier `health`.
- `pytest` n'est plus installé dans `.venv-acgym` (les tests hors-ligne n'ont pas pu être relancés ; vérifications faites par script manuel).
