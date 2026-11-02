# Rôles des ports C4D (multi-worker)

## Schéma worker N (N = 0..5)

| Port | Protocole | Qui bind | Qui envoie | Qui reçoit | Premier message |
|------|-----------|----------|------------|------------|-----------------|
| `2345+N×10` ego | UDP | **sensors_par EgoServer** (plugin AC) | Client unique : **collector** via `AssettoCorsaEnv.Client` | EgoServer | Client → `connect` ; serveur → `identified` puis états JSON/eval |
| `2346+N×10` opponents | UDP | OpponentsServer (plugin) | Client optionnel | OpponentsServer | `connect` / `identified` |
| `2347+N×10` mgmt | TCP | **simulation_management_server_task** (plugin) | Client test/mgmt | Plugin | Client → `get_static_info` ; serveur → JSON |
| `24100+N` diag | TCP localhost | **worker_collector** `CollectorDiagServer` | Test-AI-Control-Link, status | Collector | Client → `{"cmd":"status"}\n` ; serveur → JSON |
| vJoy `N+1` | HID | vJoy driver | **collector** seul (`car_control.Controls`) | AC (controls.ini) | — |
| `24050` transition | TCP localhost | **transition_server_main** (orchestrateur) | collectors push, learner pull | TransitionServer | JSON newline-delimited |

## Propriété exclusive

- **Télémétrie UDP ego** : un seul client UDP par worker. Le collector tient la session persistante (`connect` → `identified` → ticks).  
  Un second client (`telemetry_udp.udp_connect_and_sample`, test direct) **déconnecte** le collector → Windows **WinError 10054** sur `recvfrom`.
- **vJoy** : collector seul ; tests via canal diag `safe_action_test` / `neutralize`.
- **Transition server** : une instance lancée par `Start-Multi-AI-Session` ; le learner (`Run-SAC-MultiWorker`) s’y attache sans en créer un second.

## Cause 10054

Windows remonte `ConnectionResetError` quand le EgoServer accepte un nouveau client UDP ou envoie vers un socket client fermé/remplacé. Le client du collector perd la session si un autre processus envoie `connect` sur le même port ego.

## Cause steps=0

Deux transition servers : `Start-Multi-AI-Session` lançait learner+server ; `Run-SAC-MultiWorker --start-server` en créait un second isolé. Collectors pushaient vers le premier ; le second learner voyait `workers={}`.
