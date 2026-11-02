# Assetto Corsa AI — Neuroévolution

Une IA qui apprend à piloter une **Lotus Exos 125** sur le circuit de **Monza** dans
Assetto Corsa, par **neuroévolution** (pas de gradient, pas de données de démonstration).

## Ce que fait le projet

- **Neuroévolution** : une population de 120 candidats (réseaux `8 → 256 → 256 → 3`,
  sortie tanh) est évaluée par **6 instances du jeu en parallèle**, puis sélectionnée
  (élitisme) et mutée à chaque génération.
- **App Windows** (`desktop/AcAiControl/`, C# / .NET 8) : pilotage complet **sans
  terminal** — lancer/arrêter les workers, suivre la génération en cours, régler les
  paramètres, arrêt d'urgence.
- **Multi-worker** : 6 jeux Assetto Corsa pilotés en parallèle via vJoy, coordonnés par
  un centre de contrôle (C4D). Épisodes de 30 s avec un respawn unique et synchronisé.

## Architecture

| Dossier | Rôle |
|---|---|
| `ac_workers/` | Collecteurs, centre de contrôle (C4D), neuroévolution, orchestration |
| `assetto_corsa_gym/` | Environnement Gym + plugin in-game (`sensors_par`) |
| `desktop/AcAiControl/` | App Windows (WPF / .NET 8) |
| `scripts/` | Scripts PowerShell de lancement et de test |
| `common/` | Modules partagés (logger, contrat d'entraînement, serveur de transitions) |
| `config/` + `config_first_sac_cpu_v3.yml` | Configuration active |
| `docs/` | Documentation détaillée |

## Démarrage rapide (Windows)

1. Double-cliquer sur le raccourci **« Assetto Corsa AI »** (bureau).
2. Au besoin, **« Arrêt d'urgence »** (tout arrêter proprement).
3. **« Démarrer les 6 workers »** → **« Démarrer l'apprentissage »**.

L'app affiche la version à côté du titre (V1, V2, …) — vérifier qu'elle correspond à la
dernière build avant de signaler un problème.

## Documentation

- `docs/STRUCTURE.md` — où vit quoi, ce qu'il ne faut pas toucher.
- `docs/REPRISE_PROJET.md` — architecture, flux de données, carte des ports.
- `docs/ETAT_FONCTIONNEL_*.md` — l'état de marche consigné (les solutions qui ont marché).

---

*Fork de [assetto_corsa_gym](https://github.com/dasGringuen/assetto_corsa_gym) (baseline
SAC), fortement personnalisé pour la neuroévolution multi-worker et l'app Windows.*
