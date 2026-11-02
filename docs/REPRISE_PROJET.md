**Note de reprise — Assetto Corsa AI — 9 septembre 2026**

Ce bilan repose sur le code du projet, les configurations, l'historique Git, les rapports d'essais, les journaux et les métadonnées des modèles présents dans le dossier. Les exécutables du jeu, les bibliothèques tierces et toutes les anciennes sauvegardes n'ont pas été audités fichier par fichier. Aucun jeu ni entraînement réel n'a été lancé pendant cette analyse.

Le projet possède déjà une infrastructure d'apprentissage par renforcement, un centre de contrôle et des sauvegardes issues d'un entraînement avec six instances du jeu. La reprise doit porter d'abord sur la fiabilité du démarrage et sur la validité des données d'apprentissage. Les éléments consultés ne démontrent pas encore un pilote capable d'enchaîner des tours propres.

**Ce que le projet cherche à faire**

Il s'agit d'apprendre à une IA à conduire dans Assetto Corsa, actuellement avec la Lotus Exos 125 à Monza. Le socle provient du dépôt Assetto Corsa Gym de dasGringuen. Le travail local ajoute notamment l'adaptation à Windows et au calcul CPU, une installation AI séparée, les profils de jeu, les scripts de lancement, le pilotage de six instances, les diagnostics et la supervision de l'entraînement.

L'apprentissage actuel utilise des valeurs numériques fournies par le jeu : vitesse, état de la voiture, écart à la trajectoire de référence, distances aux bords et historique récent. La capture d'images est désactivée dans la configuration utilisée. Les fenêtres du jeu servent à observer les voitures ; les pixels ne sont pas l'entrée du modèle dans cette configuration.

**La boucle de fonctionnement**

1. Content Manager et les scripts démarrent une session de la copie AI du jeu.
2. Le plug-in `sensors_par`, installé dans cette copie, expose la télémétrie et les commandes de gestion de la simulation.
3. Un programme Python appelé « collector » appartient à chaque instance. Il lit la télémétrie et construit une observation de 125 nombres via l'environnement Gym.
4. Le réseau de décision, appelé « policy », choisit trois valeurs : direction, accélérateur et frein. Des traitements de commande et des limites interviennent avant l'envoi à vJoy, le contrôleur virtuel vu par le jeu. La boîte de vitesses et le passage initial en première sont gérés par une logique séparée : le réseau n'a que trois sorties.
5. Après l'action, le collector enregistre l'état précédent, l'action, la récompense, l'état suivant et l'éventuelle fin de l'essai. Cet enregistrement est une « transition ».
6. Les collectors transmettent leurs transitions au serveur central. Le programme d'entraînement, le « learner », les place dans une mémoire d'expériences et entraîne SAC, pour Soft Actor-Critic.
7. Le learner publie de nouvelles versions du modèle. Dans le chemin principal actuel, chaque collector prend ses décisions localement et adopte le nouveau modèle entre deux épisodes.

Un épisode correspond à un essai de conduite entre deux réinitialisations ; ce n'est pas nécessairement un tour. Une étape correspond à une interaction enregistrée, et une mise à jour SAC à une opération d'apprentissage du réseau. Ces trois compteurs mesurent des choses différentes.

Les six instances sont six sessions de simulation alimentant un apprentissage commun. Le mode principal est `async_local_actor`. Un autre mode, `async_actor`, permet de demander les actions au learner par le réseau pour le diagnostic.

**Comment l'IA reçoit sa note**

La récompense de base augmente avec la vitesse et diminue quand la voiture s'éloigne de la ligne de référence. La version actuelle ajoute des pénalités pour l'écart à cette ligne, le braquage important et une fin d'épisode hors piste. Elle n'utilise pas de vitesse cible.

Les épisodes peuvent se terminer pour une sortie de piste, une vitesse durablement trop basse, un déplacement à contresens ou une autre condition de fin. Des délais après réinitialisation limitent les fins immédiates et les boucles de remise en piste.

**Repères dans le dossier**

| Élément | Rôle |
|---|---|
| [AssettoCorsa-AI](<C:/Leo projet/Assetto Corsa ai/AssettoCorsa-AI>) | Copie du jeu destinée aux essais AI. |
| [ContentManager-AI](<C:/Leo projet/Assetto Corsa ai/ContentManager-AI>) | Content Manager dédié au lancement de cette copie. |
| [ac_profiles](<C:/Leo projet/Assetto Corsa ai/ac_profiles>) | Profils clean, AI et workers, captures de réglages et sauvegardes avant bascule. Les fichiers Documents d'Assetto Corsa restent une ressource partagée que les scripts préparent au lancement. |
| [AssettoCorsaPlugin](<C:/Leo projet/Assetto Corsa ai/assetto_corsa_gym/AssettoCorsaPlugin>) | Source du plug-in exécuté dans le jeu et dépendances embarquées. |
| [ac_env.py](<C:/Leo projet/Assetto Corsa ai/assetto_corsa_gym/AssettoCorsaEnv/ac_env.py>) | Construction des observations, récompense, limites, fin d'épisode et réinitialisation. |
| [AssettoCorsaConfigs](<C:/Leo projet/Assetto Corsa ai/assetto_corsa_gym/AssettoCorsaConfigs>) | Géométrie des pistes, ligne de référence et calibration des voitures. Les données Monza sont présentes. |
| [algorithm/discor](<C:/Leo projet/Assetto Corsa ai/algorithm/discor>) | Réseaux, algorithme SAC et mémoire d'expériences ; le dossier contient aussi DisCor. |
| [control_center.py](<C:/Leo projet/Assetto Corsa ai/ac_workers/control_center.py>) | Centre de contrôle C4D : tableau de bord terminal, console, démarrage, arrêt, diagnostics et suivi des processus. |
| [worker_collector.py](<C:/Leo projet/Assetto Corsa ai/ac_workers/worker_collector.py>) | Boucle de conduite et de collecte propre à chaque worker. |
| [multi_sac_trainer.py](<C:/Leo projet/Assetto Corsa ai/ac_workers/multi_sac_trainer.py>) | Entraînement central, sauvegarde, reprise et publication des modèles. |
| [transition_server.py](<C:/Leo projet/Assetto Corsa ai/common/transition_server.py>) | File centrale de transitions, statistiques et échanges avec le learner. |
| [scripts](<C:/Leo projet/Assetto Corsa ai/scripts>) | Installation, sauvegarde, lancement, arrêt, vérifications et analyses des premiers entraînements. |
| [config_first_sac_cpu_v3.yml](<C:/Leo projet/Assetto Corsa ai/config_first_sac_cpu_v3.yml>) et [config/workers](<C:/Leo projet/Assetto Corsa ai/config/workers>) | Réglages d'apprentissage et attribution des ports et périphériques aux six workers. |
| [outputs](<C:/Leo projet/Assetto Corsa ai/outputs>) | Modèles, états d'entraînement, statistiques, événements TensorBoard et diagnostics. |
| [ac_workers/logs](<C:/Leo projet/Assetto Corsa ai/ac_workers/logs>) et [ac_workers/status](<C:/Leo projet/Assetto Corsa ai/ac_workers/status>) | Journaux des lancements et derniers états enregistrés. Un fichier de statut ancien ne prouve pas qu'un processus tourne encore. |
| [ac_backups](<C:/Leo projet/Assetto Corsa ai/ac_backups>) | Sauvegardes des étapes d'installation et de configuration. |
| [train.py](<C:/Leo projet/Assetto Corsa ai/train.py>) | Point d'entrée historique pour une seule instance, désormais compatible CPU. |
| [data](<C:/Leo projet/Assetto Corsa ai/data>) et les notebooks à la racine | Documentation des démonstrations humaines, conversion de télémétrie et exemples Gym. Le chargement des démonstrations est désactivé dans la configuration actuelle. |

Le sous-dossier `ac_workers` contient également les composants de chargement des modèles, d'identification des workers, de correspondance vJoy, de nettoyage des sessions et de tests hors jeu. Une grande partie du travail récent concerne cette infrastructure.

**Configuration de référence actuelle**

| Réglage | Valeur observée |
|---|---|
| Voiture / circuit | Lotus Exos 125 / Monza |
| Calcul du modèle | CPU |
| Environnement installé | Python 3.9.13, PyTorch 1.12.1+cpu, Gym 0.21.0, NumPy 1.23.3 |
| Observations / actions | 125 valeurs / 3 sorties |
| Fréquence cible des interactions | 25 Hz par instance ; la fréquence réelle dépend du fonctionnement du jeu et des processus |
| Nombre maximal de workers | 6, identifiés de 0 à 5 |
| Début des mises à jour SAC | Après 1 500 transitions globales et disponibilité de données dans la mémoire |
| Capacité de la mémoire | 50 000 expériences |
| Taille d'un lot d'apprentissage | 64 |
| Réseaux policy et Q | Trois couches cachées de 256 neurones |
| Sauvegarde périodique | Toutes les 2 000 étapes |
| Durée de l'entraînement | Le YAML indique 10 000 étapes, mais le centre de contrôle demande le mode continu par défaut |
| Accélérateur / frein maximaux configurés | 100 % / 100 % |
| Limite de braquage configurée | 35 degrés |
| Modèle initial / données humaines | Pas de chargement de démonstrations dans ce scénario ; reprise d'un modèle local possible |

La note historique mentionne un Ryzen 7 9800X3D, environ 32 Go de RAM et une Radeon RX 9070 XT. Le calcul CPU est confirmé par la configuration et la version de PyTorch installée ; le matériel n'a pas été réinventorié pendant cette analyse.

Pour chaque worker N : télémétrie UDP sur `2345 + 10*N`, gestion TCP sur `2347 + 10*N`, diagnostic sur `24100 + N`, périphérique vJoy prévu `N + 1`. Le serveur de transitions utilise `24050`, le centre de contrôle `24700`. Voir [PORT_ROLES.md](<C:/Leo projet/Assetto Corsa ai/ac_workers/PORT_ROLES.md>).

Chaque collector doit garder la propriété de sa connexion de télémétrie et de son périphérique vJoy. Le document sur les ports décrit deux problèmes historiques : un second client de télémétrie pouvait déconnecter le collector, et deux serveurs centraux distincts pouvaient laisser le learner sans données.

**Ce qui a déjà été obtenu**

| Période | Preuve conservée et résultat |
|---|---|
| 17–19 juin | Séparation Steam/AI, lancement via Content Manager, installation du plug-in et de vJoy, lecture de télémétrie, validation Gym puis accélérateur/frein/première. |
| Premier entraînement CPU | Le rapport compte 1 736 étapes et 7 épisodes. La voiture restait trop lente : freinage dominant et direction souvent en butée. |
| Deuxième entraînement CPU | Le rapport compte 5 059 étapes et 17 épisodes. Les voitures avançaient, mais la direction SAC saturait fréquemment ; le comportement guidé initial obtenait de meilleurs résultats. |
| Essai mono-instance v3 | Le CSV conservé montre des épisodes extrêmement courts et une voiture quasiment immobile. Il ne démontre pas une amélioration de conduite. |
| 23–28 juin | L'historique Git enregistre plusieurs jalons de contrôle C4D puis six instances en grille 3 × 2. Le dernier commit décrit un entraînement à six workers stabilisé à ce moment-là. |
| 29 juin | Le dernier statut d'entraînement archivé consulté rapporte 16 614 étapes globales, 15 107 mises à jour SAC et des contributions des six workers. |
| 13 juillet | Derniers essais de lancement consultés : échecs de connexion de certains collectors au port de gestion de leur instance. |

Le [dernier statut d'entraînement archivé](<C:/Leo projet/Assetto Corsa ai/outputs/multi_sac_cpu_v3/learner_status_archive_20260629_014548.json>) rapporte aussi 7 fins d'épisode, toutes classées `out_of_track`, pour une durée moyenne d'environ 19,7 secondes. Ces compteurs concernent ce statut de session et ne résument pas tous les essais du projet.

Le [checkpoint step_00016000](<C:/Leo projet/Assetto Corsa ai/outputs/multi_sac_cpu_v3/model/checkpoints/step_00016000>) existe, ainsi qu'un dossier `resume`. Les métadonnées indiquent 125 observations et 3 actions. Une version publiée correspond à l'étape 16 614, mais le dernier état de reprise enregistré est à 16 000. Le modèle publié pour conduire et le checkpoint utilisé pour reprendre l'entraînement sont deux objets distincts.

La mémoire d'expériences reste en RAM : elle n'est pas restaurée par la reprise actuelle. Reprendre un modèle conserve des réseaux et des compteurs, sans retrouver toutes les expériences précédentes.

**Ce qui reste à fiabiliser**

1. **La connexion au jeu au démarrage.** Les [statuts du worker 0](<C:/Leo projet/Assetto Corsa ai/ac_workers/status/worker_0.json>) et du [worker 2](<C:/Leo projet/Assetto Corsa ai/ac_workers/status/worker_2.json>), datés du 13 juillet, indiquent `attach_failed` : ports de gestion 2347 et 2367 non ouverts malgré une session décrite comme active. Le journal du worker 1 montre, lui, une connexion à la télémétrie et une session attachée. La cause exacte des échecs reste à reproduire dans une session réelle.

2. **L'identité de chaque instance et de chaque contrôleur.** Le code récent ajoute des identifiants de démarrage, des tests de correspondance et des captures de réglages. Le [diagnostic vJoy du 13 juillet](<C:/Leo projet/Assetto Corsa ai/outputs/diagnostics/vjoy_mapping_latest.json>) montre que le numéro du périphérique n'est pas simplement l'index JOY du jeu plus un. Par exemple, le périphérique 2 y correspond à l'index 3. Cette correspondance doit être vérifiée au démarrage ; le fichier ancien n'est pas une mesure actuelle.

3. **La cohérence des commandes et des anciens modèles.** Les modifications locales ont changé le passage des sorties de policy aux pédales : une sortie brute nulle commande maintenant une pédale relâchée, avec activation sur la partie positive de la sortie. L'ancien calcul transformait une sortie nulle en 50 %. Les plafonds configurés ont aussi changé. Les métadonnées vérifient notamment les dimensions des réseaux, sans identifier cette convention de commande. Un ancien modèle peut donc se charger tout en produisant un comportement différent. Il faut valider cette compatibilité avant de prolonger son entraînement. Le module `fresh_start.py` permet d'archiver l'ancien état et d'initialiser un nouveau modèle ; il n'a pas été exécuté ici.

4. **Un défaut de mélange entre workers dans l'apprentissage sur trois étapes.** La configuration utilise `nstep: 3`, mais le learner insère les transitions de tous les workers dans un seul `ReplayBuffer`, dont la file de calcul sur trois étapes ne distingue pas les workers. Une reproduction hors jeu a injecté une transition de chacun des workers 0, 1 et 2, avec les récompenses 1, 10 et 100. Le code a créé une expérience reliant l'état du worker 0 à l'état suivant du worker 2, avec une récompense cumulée de 109,3264. Ce mélange est incorrect pour trois trajectoires indépendantes. Il faut isoler ce calcul par worker/épisode, ou valider provisoirement un apprentissage à une étape, avant de considérer l'entraînement multi-instance comme fiable. Aucune correction n'a été appliquée pendant ce bilan.

5. **Une évaluation de conduite explicite.** Les sauvegardes prouvent que des calculs d'apprentissage ont eu lieu. Il reste à mesurer des épisodes reproductibles : distance parcourue, tours propres, durée avant sortie, vitesse et écart à la ligne. `eval_interval` vaut 0 dans la configuration actuelle. Aucun résultat consulté ne justifie de présenter le modèle local comme un pilote abouti.

**État Git et documentation**

La branche actuelle est `fix/c4d-collector-startup`. Le dernier commit est `f0ec8cb`, daté du 28 juin 2026. De nombreuses modifications et de nouveaux fichiers locaux existent au-delà de ce commit, principalement dans le centre de contrôle, les collectors, le learner et leurs tests. Ils font partie du projet présent sur le disque, mais pas de ce dernier point de sauvegarde Git.

[PROJECT_STATUS.md](<C:/Leo projet/Assetto Corsa ai/PROJECT_STATUS.md>) s'arrête à la phase C2B du 19 juin. Sa mention d'un entraînement bloqué par l'absence de CUDA est dépassée : le code prend maintenant en charge le CPU et des entraînements CPU ont été exécutés. Le README décrit surtout le projet d'origine. Pour la reprise locale, la présente note, le code actuel et les journaux récents donnent une image plus complète.

**Vérifications réalisées pour ce bilan**

42 tests hors jeu ont réussi en 14,50 secondes, avec deux avertissements de dépréciation TensorBoard/distutils. Ils couvrent la chaîne d'actions, l'exploration, la diversité entre workers, le changement de policy entre épisodes et les objectifs de reprise de l'entraînement. Il s'agit d'une sélection de tests, pas de la totalité de la suite et pas d'une validation du jeu en fonctionnement.

Le défaut de mélange sur trois étapes a été reproduit séparément avec des transitions synthétiques dans un répertoire temporaire. Les modèles existants n'ont pas été modifiés.

Commande de la sélection de tests, depuis la racine du projet :

```powershell
& '.\.venv-acgym\Scripts\python.exe' -B -m pytest -q -p no:cacheprovider ac_workers/test_action_chain_offline.py ac_workers/test_action_exploration_offline.py ac_workers/test_policy_episode_boundary_offline.py ac_workers/test_learner_resume_target_offline.py ac_workers/test_worker_exploration_diversity_offline.py
```

**Point d'entrée pour une prochaine session**

Le lanceur actuel du centre de contrôle est [Start-C4D-ControlCenter.ps1](<C:/Leo projet/Assetto Corsa ai/scripts/Start-C4D-ControlCenter.ps1>). Il ouvre un tableau de bord terminal et une console de commandes :

```powershell
Set-Location 'C:\Leo projet\Assetto Corsa ai'
.\scripts\Start-C4D-ControlCenter.ps1
```

| Commande dans la console C4D | Fonction |
|---|---|
| `help` | Afficher les commandes disponibles. |
| `health` / `workers` | Consulter l'état du système et des workers. |
| `start 0` | Démarrer une seule instance pour le diagnostic initial. |
| `diag-check 0` / `error 0` | Examiner le diagnostic et les erreurs du worker 0. |
| `scale-test 0-5` | Démarrer progressivement les six workers. |
| `learner start` | Démarrer l'apprentissage sur les workers prêts ; reprise automatique si le checkpoint attendu existe. |
| `learner status` | Examiner les compteurs d'apprentissage. |
| `learner save` / `learner stop` | Demander une sauvegarde, ou un arrêt normal du learner. |
| `stop-all` | Arrêter les workers. |

Ces commandes ont été relevées dans le code ; elles n'ont pas été exécutées pendant le bilan. La séquence de reprise proposée est : conserver l'état de travail actuel, remettre le worker 0 en fonctionnement, vérifier que ses commandes atteignent bien sa voiture, corriger le mélange des expériences, puis remonter progressivement à plusieurs instances et mesurer la conduite.
