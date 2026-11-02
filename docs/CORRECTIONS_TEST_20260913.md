# Corrections pour le prochain test

Les corrections sont dans `C:/Leo projet/Assetto Corsa ai/.worktrees/training-fixes`. Aucun entraînement lancé et aucune sauvegarde remplacée lors de leur application.

- Évaluations : chargement de la politique avant gel, refus d'un chargement incomplet, identifiant enregistré, reprise de l'état de pause précédent. Distance hors piste non créditée et temps immobile enregistré.
- Départ/reset : première vitesse confirmée sur deux paquets frais consécutifs ; le réglage boîte automatique seul ne suffit plus à déclarer le départ réussi. Échec explicite si le rapport ne s'engage pas.
- Observations de reset : les paquets répétés ou signalés hors piste ne valident plus la reprise. Une observation incohérente fait échouer le reset au lieu de seulement produire un avertissement.
- Immobilité : diagnostic séparant point mort, freinage, accélérateur non reçu et voiture coincée. Une récupération de vitesse réussie n'impose plus une téléportation. Ses commandes scriptées sont exclues de l'apprentissage.
- Récompenses : suppression du bonus de vitesse indépendant ; progression signée conservée. Pénalités d'arrêt, de durée et de hors piste proportionnelles au temps entre mesures (anciens coefficients interprétés à la référence de 25 Hz). Bonus de secteur seulement avec progression valide et couverture du secteur ; état invalidé après une discontinuité.
- Délais : grâce et compteurs de blocage/mauvais sens/hors piste avancent avec le temps de télémétrie. Suppression de la seconde suppression de fin d'épisode dans le collecteur, qui pouvait conserver une pénalité terminale tout en masquant `done`.
- Journal : `reset_begin` inclut worker, cause et tentative ; diagnostics de rapport, gaz et frein avant récupération.

## Préparer le test

1. Arrêter proprement le learner et les workers depuis C4D, puis redémarrer le centre de contrôle pour charger le nouveau code. Attendre la fin effective de l'arrêt avant une relance.
2. Dans le centre de contrôle mis à jour : `train new progression_v3`. La commande archive automatiquement le modèle précédent. Ne pas utiliser `train wipe` : inutile de supprimer volontairement l'historique.
3. Lancer `scale-test 0` pour vérifier le départ d'une voiture, puis `learner start` quand elle est prête. Après ce contrôle court, arrêter avant de préparer le test à six (`scale-test 0-5`).
4. Tester 15 minutes à six seulement lorsque les départs sont fiables. Vérifier que les transitions et mises à jour augmentent, que le worker reprend après chaque évaluation et que les rapports identifient les modèles successifs.

Le schéma de récompense est maintenant `signed_progress_v2_elapsed`. Les anciens modèles/replays ne sont pas chargés silencieusement : une nouvelle expérience est nécessaire pour ne pas mélanger les anciens et nouveaux signaux. Les poids et sauvegardes existants sont laissés intacts.

Limites : les tests hors jeu ne démontrent pas que le bouton de vitesse est correctement mappé dans chaque instance réelle. Si `first_gear_not_confirmed` apparaît, conserver ce diagnostic et vérifier le mapping ; ne pas supprimer la validation. La géométrie de détection hors piste existante n'a pas été remplacée par un nouveau modèle de piste.
