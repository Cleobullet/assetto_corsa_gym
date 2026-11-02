# Organisation du projet

## Au quotidien

| Élément | Emplacement |
|---|---|
| Application Windows | `desktop/AcAiControl/` + raccourci bureau **Assetto Corsa AI** |
| Workers / IPC / learner | `ac_workers/` |
| Scripts actifs (start/stop/urgence) | `scripts/` |
| Tests scripts | `scripts/tests/` |
| Config entraînement actuel | `config_first_sac_cpu_v3.yml`, `config/workers/` |
| Documentation locale | `docs/` |
| Outils (MoTeC, etc.) | `tools/` |
| Ancien code | `legacy/` (voir `legacy/README.md`) |

## Racine volontairement légère

La racine garde surtout : config active, raccourcis, `requirements.txt`, README upstream, `AGENTS.md`.

## Ne pas toucher (installations lourdes)

`AssettoCorsa-AI/`, `ContentManager-AI/`, `.venv-acgym/`, `ac_backups/`, `ac_profiles/`, `outputs/`.
