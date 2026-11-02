# Centre de contrôle Windows

Ouvrir **Application AI.lnk**, ou le raccourci **Assetto Corsa AI** du bureau. Les deux utilisent `scripts/Open-AI-App.ps1`, qui reconstruit l’application si ses sources ont changé. Le binaire actif est dans `desktop/AcAiControl/bin/Release/net8.0-windows/`.

## Utilisation

Les onglets sont Workers, Sauvegardes, Paramètres, Récompenses et Recherche. Le test guidé a été retiré de l’application. Les outils de diagnostic Python et leurs rapports sont conservés pour les réparations.

Avant de modifier les paramètres, arrêter l’apprentissage et les workers. Enregistrer, puis redémarrer les workers et l’apprentissage. Une modification ne s’applique pas aux processus déjà lancés.

## Paramètres réellement utilisés

| Réglage | Application |
|---|---|
| Circuit et voiture | Configuration du moteur + profils race.ini ; seules les ressources installées pour l’IA avec données de piste / calibration sont proposées |
| CPU / CUDA | Ressource de calcul du learner ; CUDA proposé seulement si PyTorch le détecte |
| Taille de batch | Nombre de transitions utilisées par mise à jour ; entier de 1 à 50 000 |
| Bruit supplémentaire | Bruit ajouté aux actions pendant l’entraînement, entre 0 et 1 ; SAC reste stochastique à zéro |
| Biais gaz / frein | Décalage des actions pendant l’entraînement, de −1 à 1, même avec bruit nul ; exclus de l’évaluation déterministe |
| Résolution | Profils video.ini et dimensions transmises au lancement ; les préréglages changent la résolution uniquement |
| Son et volume | audio.ini de chaque profil, copié au démarrage ; volume entre 0 et 1 |

Les options caméra, capteurs, historique et fréquence ont été retirées de l’éditeur : elles ne sont pas compatibles avec la chaîne actuelle de modèles à 125 observations et télémétrie à 25 Hz. Cela ne désactive pas les capteurs actuellement utilisés.

Les nombres invalides, les choix indisponibles et les tailles hors limites sont refusés. Les fichiers sont préparés ensemble et les modifications déjà écrites sont restaurées si une écriture suivante échoue.

## Récompenses

L’éditeur expose les onze réglages actifs du mode `signed_progress_v1` : progression, hors-piste, fluidité, coût du temps, seuil et pénalité de lenteur, sortie terminale, secteurs, bonus de secteur et de tour propre, temps de référence du tour. Les anciens bonus ignorés par ce mode ne sont plus proposés.

Les coûts affichés « / 0,04 s » sont proportionnels au temps réellement écoulé. Le nombre de secteurs doit être un entier entre 2 et 100. Les autres coefficients sont des nombres finis non négatifs ; le temps de référence doit être strictement positif.

Changer les récompenses nécessite un nouveau test avec mémoire vide pour éviter de réutiliser des transitions rémunérées différemment. Les paramètres restent globaux dans cette version : charger une sauvegarde ne restaure pas automatiquement sa configuration. Le profil progression-v5 et ses deux configurations archivées sont conservés.

L’onglet Recherche compare les onze réglages sur des situations fixes (avance rapide, lenteur, arrêt, marche arrière, hors-piste, conduite saccadée, sortie). Il propose un autre jeu de valeurs seulement s’il sépare mieux ces situations que les réglages actuels. Appliquer une ligne écrit le YAML ; la conduite ne change qu’après un nouvel entraînement à mémoire vide.

## Vérifications hors jeu

- `dotnet run --project desktop/AcAiControl.Tests` : écritures des paramètres dans un projet temporaire, sélection du contenu compatible, valeurs invalides et restauration après erreur.
- `.venv-acgym/Scripts/python.exe ac_workers/test_active_reward_settings_offline.py` : effet des onze récompenses proposées.
- `.venv-acgym/Scripts/python.exe ac_workers/test_action_exploration_offline.py` : actions, biais et évaluation sans bruit.

Les tests hors jeu ne remplacent pas une validation dans Assetto Corsa.
