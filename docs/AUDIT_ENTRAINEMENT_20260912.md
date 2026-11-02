# Audit de l’apprentissage — 12 septembre 2026

**Le dernier essai montre une instabilité de l’apprentissage, avec des défauts concrets dans la chaîne des actions. Continuer plus longtemps ou remplacer SAC par une sélection génétique, sans corriger ces défauts, ne constitue pas une bonne première expérience.** Une approche par générations est pertinente ensuite, en complément de l’apprentissage actuel.

Cette analyse porte sur les sources, les sauvegardes, les journaux et les mesures enregistrées. Aucun entraînement ni jeu n’a été lancé. Les modèles, la branche active et la sauvegarde Git temporaire n’ont pas été modifiés. Le correctif de production reste à réaliser sur la version de travail récente ; les fichiers créés ici sont un rapport, une figure et des diagnostics hors jeu.

**Quelle version a été analysée ?**

Le dossier est actuellement sur `main`, commit `ef0fa50`, la version amont de 2025. Le journal Git indique un passage depuis `fix/c4d-collector-startup` vers `main` le 12 septembre à 13 h 37. Le développement récent, dont les corrections de septembre, est conservé dans `stash@{0}`, révision `f663ff9c4452cfa05229b13d3e856c3e66a3a3aa`. Il n’est donc pas perdu, mais les scripts multi-instance ne figurent plus dans les fichiers actifs.

J’ai extrait une copie des sources pertinentes dans [le dossier d’audit](<C:/Leo projet/Assetto Corsa ai/outputs/diagnostics/audit_20260912/source>). Les références au code ci-dessous désignent cette copie. Les logs ne contiennent pas une empreinte complète du code et des réglages utilisés à chaque instant : cette copie représente la dernière version conservée, sans garantir que chaque ancien essai utilisait exactement cette révision.

**Ce qui existe déjà**

- SAC, un algorithme d’apprentissage par renforcement, apprend à choisir la direction, l’accélérateur et le frein à partir de 125 valeurs numériques. Il ne regarde pas les images des fenêtres.
- Six instances d’Assetto Corsa fournissent des expériences à **un seul apprentissage commun**. Ce ne sont pas six lignées concurrentes : les voitures chargent les versions publiées du même réseau, avec une exploration différente.
- Le scénario récent utilise la Lotus Exos 125 à Monza, sur CPU. Les réseaux ont trois couches de 256 neurones, le lot d’apprentissage contient 64 expériences et la mémoire en conserve au maximum 50 000.
- Les travaux récents ont notamment corrigé des problèmes de connexion, de contrôleurs vJoy, de diagnostic, de reprise des compteurs et de mélange des trajectoires entre workers.

La séparation du calcul des récompenses sur trois étapes par worker est bien présente et a été revérifiée ici avec des trajectoires synthétiques. Elle ne doit pas être présentée comme un défaut encore ouvert. Voir le [rapport des réparations du 9 septembre](<C:/Leo projet/Assetto Corsa ai/outputs/diagnostics/reparations_entrainement_20260909.md>) et les [vérifications de cet audit](<C:/Leo projet/Assetto Corsa ai/outputs/diagnostics/audit_20260912/verified_findings.json>).

**Ce que montre le dernier entraînement**

Le journal du processus 7360 indique un démarrage sans reprise, puis 49 620 transitions et 44 621 mises à jour sur environ 32 minutes de lancement. Il rapporte **560 fins d’épisode, toutes pour sortie de piste**, et une durée moyenne de 9,9 secondes. Ces chiffres concernent cet essai, pas toute l’histoire du projet. La durée moyenne inclut les mécanismes de temporisation et ne mesure pas à elle seule la qualité de conduite. [Journal](<C:/Leo projet/Assetto Corsa ai/ac_workers/logs/learner_start_last.log>), [état final](<C:/Leo projet/Assetto Corsa ai/outputs/multi_sac_cpu_v3/trainer_state.json>).

Les mesures TensorBoard du même processus montrent une forte divergence :

| Mise à jour d’apprentissage | Erreur des réseaux Q | Valeur Q1 moyenne | Coefficient alpha |
|---:|---:|---:|---:|
| 10 | 5,18 | 0,93 | 0,997 |
| 5 000 | 1,96 | 10,66 | 0,239 |
| 10 000 | 9,44 | 14,30 | 0,082 |
| 20 000 | 9 791 636 | 15 405 | 2,64 |
| 30 000 | 675 783 552 | 96 199 | 44,24 |
| 44 620 | 27 108 907 008 | 909 732 | 2 075,93 |

Q représente l’estimation du résultat futur d’une décision ; alpha pondère l’exploration dans l’objectif SAC. Ces valeurs restent numériques, mais leur envolée montre que l’essai ne se contente pas de progresser lentement. Elles ne prouvent pas, à elles seules, quelle modification déclenche la divergence. [Mesures extraites](<C:/Leo projet/Assetto Corsa ai/outputs/diagnostics/audit_20260912/tensorboard_metrics.json>).

![Divergence du dernier entraînement](<C:/Leo projet/Assetto Corsa ai/docs/audit_entrainement_20260912.png>)

**Priorité 1 — Mettre toutes les actions dans le même repère**

Défaut confirmé par lecture du code et reproduction hors jeu. Le réseau produit trois nombres entre −1 et 1. Le collector transforme la direction en unités du contrôleur, limite ses variations, applique des règles aux pédales, puis stocke les commandes physiques dans la mémoire. Mais SAC évalue directement les sorties brutes du réseau avec les réseaux Q entraînés sur ces commandes physiques.

Exemple avec la calibration Lotus conservée : la décision `[1, −0,5, −0,5]` devient `[0,01333, 0, 0]` lors du premier pas après une direction centrée. La première valeur est limitée par les 6 degrés de variation autorisés ; les deux pédales sont relâchées. Ce sont pourtant les premières valeurs, dans un autre repère, que SAC présente à Q lorsqu’il optimise sa politique. La direction exécutée reste comprise approximativement entre −0,07778 et +0,07778 avec la limite de 35 degrés, alors que l’optimisation de la politique peut interroger Q jusqu’à −1 et +1.

Le modèle apprend donc la valeur d’actions exprimées dans un repère et recherche les meilleures décisions dans un autre. **C’est une explication plausible et importante de la divergence observée**, à confirmer par un entraînement comparatif après correction. La formulation de SAC utilise un même espace d’action pour les transitions et les évaluations des décisions. [Description de SAC et équations](https://spinningup.openai.com/en/latest/algorithms/sac.html).

Correction proposée : définir un contrat unique `policy_action` pour les trois valeurs normalisées ; conserver dans le replay la commande demandée après le bruit d’exploration et avant la conversion physique ; conserver séparément les commandes réellement envoyées pour les diagnostics. Les conversions et limites appartiennent à l’environnement. Les observations doivent rendre accessibles les états des commandes dont dépend ce traitement, notamment la commande de direction précédente. Les transitions pendant une intervention de sécurité ou une observation invalide doivent être distinguées et traitées explicitement.

Les deux règles de pédales doivent également être unifiées. Un composant utilise encore `pédale = (valeur + 1) / 2`, alors que le chemin canonique utilise `pédale = max(0, valeur)`. Reproduction : configurer un plafond d’accélérateur à 30 % et demander une sortie brute de 0,8 aboutit actuellement à **0 %**, et non 30 %. Le plafond actuel est à 100 %, mais le défaut empêcherait un curriculum fondé naïvement sur une réduction de ce paramètre.

Points concernés : [conversion et exécution](<C:/Leo projet/Assetto Corsa ai/outputs/diagnostics/audit_20260912/source/ac_workers/worker_collector.py:1829>), [action envoyée au learner](<C:/Leo projet/Assetto Corsa ai/outputs/diagnostics/audit_20260912/source/ac_workers/worker_collector.py:2007>), [insertion dans le replay](<C:/Leo projet/Assetto Corsa ai/outputs/diagnostics/audit_20260912/source/ac_workers/multi_sac_trainer.py:287>), [optimisation SAC](<C:/Leo projet/Assetto Corsa ai/outputs/diagnostics/audit_20260912/source/algorithm/discor/discor/algorithm/sac.py:124>), [limites des pédales](<C:/Leo projet/Assetto Corsa ai/outputs/diagnostics/audit_20260912/source/assetto_corsa_gym/AssettoCorsaEnv/training_safety.py:210>).

**Autre contrôle préalable — Vérifier l’isolation de la télémétrie des six instances**

Le plug-in combine des informations locales obtenues par `ac.getCarState` avec des informations lues dans les zones de mémoire Windows nommées `acpmf_physics`, `acpmf_graphics` et `acpmf_static`, sans identifiant de worker. Ces dernières fournissent notamment l’orientation, l’horloge et le nombre de roues hors piste. Les mêmes noms sont présents dans les sources conservées et dans la copie installée du plug-in. [Ouverture de la mémoire](<C:/Leo projet/Assetto Corsa ai/outputs/diagnostics/audit_20260912/source/assetto_corsa_gym/AssettoCorsaPlugin/plugins/sensors_par/sim_info.py:205>), [construction de la télémétrie](<C:/Leo projet/Assetto Corsa ai/outputs/diagnostics/audit_20260912/source/assetto_corsa_gym/AssettoCorsaPlugin/plugins/sensors_par/structures.py:31>).

Sous Windows, ouvrir une zone portant un nom existant donne accès à cette zone. Cela crée un risque concret de mélange entre instances si elles partagent le même espace de noms ; des ports réseau distincts ne suffisent pas à l’éviter. [Documentation de `mmap`](https://docs.python.org/3/library/mmap.html#mmap.mmap).

**La contamination effective n’a pas été démontrée ici.** Le rapprochement limité de paquets enregistrés n’a pas trouvé de quadruplets orientation/horloge strictement identiques entre voitures éloignées. Les reculs d’horloge relevés restent donc un signal à investiguer, pas une preuve de mélange. Avant de comparer des lignées, tester une voiture, puis deux dans des états volontairement distincts, et vérifier que la remise en piste ou la sortie de l’une ne modifie pas les observations de l’autre. Si nécessaire, remplacer les lectures par des données locales à chaque instance ou isoler réellement les simulations. Renommer seulement le lecteur de mémoire ne suffit pas : le producteur doit écrire dans la même zone isolée.

**Priorité 2 — Reprendre réellement tout l’apprentissage**

La sauvegarde actuelle contient les trois réseaux et les compteurs. Elle ne conserve pas la mémoire d’expériences, les états des optimiseurs Adam, ni le coefficient alpha appris. À la reprise, les réseaux Q sont rechargés mais alpha repart de 1. Une mémoire vide est recréée.

En outre, le seuil de 5 000 étapes vérifie le compteur global conservé, et non le remplissage du nouveau replay. Une reproduction avec une seule expérience stockée et un compteur repris à 49 621 a déclenché une mise à jour sur un lot de 64 duplications de cette même expérience.

Correction proposée : sauvegarde atomique complète des réseaux, optimiseurs, alpha, compteurs, états aléatoires, mémoire et configuration effective. Si la mémoire manque, attendre un nouveau remplissage suffisant, fondé sur `len(replay)`. Ne pas reprendre silencieusement une expérience avec une convention d’actions ou une récompense différente : inscrire leurs versions dans les métadonnées.

Ce défaut concerne les reprises. **Il n’explique pas à lui seul le dernier essai**, dont le journal indique `Resume=False`. [Chargement](<C:/Leo projet/Assetto Corsa ai/outputs/diagnostics/audit_20260912/source/ac_workers/multi_sac_trainer.py:194>), [seuil de mise à jour](<C:/Leo projet/Assetto Corsa ai/outputs/diagnostics/audit_20260912/source/ac_workers/multi_sac_trainer.py:357>), [sauvegarde SAC](<C:/Leo projet/Assetto Corsa ai/outputs/diagnostics/audit_20260912/source/algorithm/discor/discor/algorithm/sac.py:210>).

**Priorité 3 — Simplifier ce que l’on récompense et mesurer les progrès**

La récompense appelée « progression » dépend de la vitesse et de la proximité de la ligne, sans mesurer directement l’avancement dans le bon sens. Les bonus et pénalités se sont accumulés. La fonction accorde par exemple +0,0867 par pas à 2 km/h sur la ligne ; inverser uniquement l’orientation ne change pas ce calcul. Cela décrit la fonction de récompense, pas la viabilité physique d’une telle trajectoire. Le bonus de proximité rend néanmoins les comportements très lents intéressants, et le signe `écart × braquage` ne suffit pas à définir une bonne correction dans tous les virages.

Je propose une première récompense plus lisible : avancement signé le long du circuit, pénalité de sortie/collision et faible coût de commandes brusques. Exclure les téléportations et gérer le passage de la ligne d’arrivée dans le calcul d’avancement. Ajouter un objectif de vitesse progressif si nécessaire, après validation de la conversion des pédales. Conserver la même définition lors d’une comparaison entre modèles. [Récompense actuelle](<C:/Leo projet/Assetto Corsa ai/outputs/diagnostics/audit_20260912/source/assetto_corsa_gym/AssettoCorsaEnv/ac_env.py:844>).

La configuration désactive les évaluations périodiques et le learner multi-instance examiné n’intègre pas de boucle d’évaluation du meilleur modèle. Prévoir des essais à politique figée, sans bruit ajouté, sur les mêmes situations de départ. Suivre : proportion de secteurs terminés sans sortie, distance valide, sorties par kilomètre, puis temps au tour uniquement pour les tours propres. Garder un meilleur modèle validé séparément du dernier modèle calculé.

**Priorité 4 — Apprendre des situations utiles**

SAC explore déjà de manière aléatoire. Le code ajoute en permanence un bruit d’écart-type 0,55, un biais de +0,75 à l’accélérateur et de −0,75 au frein, puis limite les résultats. Cela modifie fortement les décisions. Ce n’est pas une faute algorithmique en soi avec un replay cohérent, mais cela peut rendre les essais difficiles à interpréter et raréfier les freinages utiles.

Après correction des actions, comparer une base avec l’exploration SAC seule à une variante avec un bruit supplémentaire modéré. Ne pas changer simultanément la récompense, le véhicule et toutes les constantes. Une première base à une étape (`nstep: 1`) simplifierait aussi le diagnostic avant de réintroduire trois étapes.

Introduire une difficulté progressive : tenir la ligne à vitesse modérée, réussir plusieurs virages, relier les secteurs, finir des tours propres, puis augmenter la vitesse. Démarrer avant les secteurs problématiques donne davantage d’exemples utiles que répéter seulement les situations déjà faciles. Le relevé récent montre notamment que les workers 2 et 5 repartent souvent autour de 16,4 % du circuit ; il ne faut pas supposer que les six voitures repartent toujours des stands.

Une phase d’imitation est une autre piste : enregistrer des passages propres sur la même voiture et le même circuit, apprendre les commandes du pilote, puis améliorer ce point de départ avec SAC. Le projet amont fournit des données humaines et étudie leur utilisation ; leur disponibilité ne garantit pas la compatibilité avec la Lotus et les nouvelles conventions locales. Dans le learner multi-instance actuel, activer simplement un drapeau YAML ne suffit pas à ajouter cette phase : il faut intégrer le chargement, la conversion et l’entraînement correspondants. [Article du projet](https://arxiv.org/abs/2407.16680).

**Ton idée de générations : une évolution possible du système**

Le principe que tu décris correspond à un algorithme évolutionnaire : plusieurs candidats, une évaluation commune, sélection des meilleurs, création de variantes et nouvelle génération. Il peut faire évoluer les poids d’un réseau, des paramètres d’un contrôleur ou les réglages d’apprentissage.

Pour ce projet, je recommande de commencer par une **population de modèles SAC**. Chaque candidat apprend pendant sa vie ; les meilleurs transmettent leur état appris et certains réglages à la génération suivante. Cette famille de méthodes est appelée *Population Based Training*. C’est une proposition d’adaptation au projet, pas une garantie de performance. [Article original](https://arxiv.org/abs/1711.09846).

Exemple compatible avec six simulations :

| Groupe | Ressources | Rôle |
|---|---|---|
| A | 2 voitures, 1 learner, 1 replay | Réglages de référence |
| B | 2 voitures, 1 learner, 1 replay | Variante du rythme d’apprentissage |
| C | 2 voitures, 1 learner, 1 replay | Variante de l’exploration |

Les trois candidats partent d’initialisations aléatoires distinctes, ou d’une même base issue de démonstrations, avec leurs propres expériences et états. Un budget égal d’interactions par candidat rend la première comparaison lisible. À intervalles convenus, l’apprentissage s’interrompt pour une évaluation sans bruit sur les mêmes départs.

On conserve un champion intact ; on remplace le plus faible par une copie complète d’un candidat meilleur, puis on fait varier légèrement quelques réglages, par exemple le taux d’apprentissage d’un facteur 0,8 ou 1,2. On conserve aussi une autre lignée pour maintenir de la diversité. L’évaluation doit porter sur plusieurs essais, avec confirmation sur des situations réservées, pour éviter de sélectionner un passage chanceux.

Je commencerais par des mutations des réglages, sans croiser arbitrairement les poids de deux grands réseaux. Les poids, les réseaux Q, alpha et les optimiseurs forment un état coordonné ; une recombinaison naïve peut casser ce qui a été appris. Une recherche génétique pure sur un petit contrôleur reste possible comme expérience séparée, mais son coût en simulations doit être mesuré avant de remplacer SAC.

Cette architecture nécessite plusieurs identifiants de candidat, des dossiers et publications séparés, un routage des transitions vers le bon learner, des sauvegardes complètes et un évaluateur commun. Les six workers actuels ne réalisent pas déjà cette sélection. Trois learners augmenteraient la charge CPU : mesurer le débit et les délais, puis essayer deux candidats ou une exécution alternée si nécessaire.

**Ordre de travail conseillé**

1. Reconstituer la version récente sur une branche dédiée en préservant le stash, puis enregistrer précisément sources et configuration de chaque essai.
2. Corriger le contrat d’actions et les conversions de pédales ; ajouter une vérification de bout en bout reliant la décision à l’expérience utilisée par Q. Valider la télémétrie à une puis deux instances avant de repasser à six.
3. Corriger les sauvegardes et la reprise ; ajouter les mesures et l’évaluation du meilleur modèle. Après changement du contrat, lancer une expérience distincte : les anciens critiques entraînés dans le mauvais repère ne constituent pas une reprise fiable.
4. Comparer à budget fixé une base SAC cohérente, puis une variante de récompense et de difficulté. Confirmer l’amélioration sur plusieurs initialisations et sur la conduite, pas seulement sur les pertes numériques.
5. Ajouter imitation et/ou population avec sélection lorsque cette base est stable.

La publication des trois réseaux à chaque mise à jour est également coûteuse : le dernier essai rapporte 44 621 publications. Tester une cadence moins fréquente, par exemple toutes les 100 mises à jour ou selon une période temporelle, est raisonnable ; le gain doit être mesuré. Cela améliore potentiellement le débit, sans résoudre les incohérences d’apprentissage.

**Vérifications et limites**

Le [script de reproduction](<C:/Leo projet/Assetto Corsa ai/outputs/diagnostics/audit_20260912/verify_findings.py>) a vérifié cinq points hors jeu : décalage des actions, plafond de pédale incorrect, récompense indépendante de l’orientation dans l’exemple, mise à jour après reprise avec une seule expérience, et correction antérieure du mélange entre workers. Il exécute les fonctions extraites des sources conservées, sans démarrer les contrôleurs.

```powershell
& '.\.venv-acgym\Scripts\python.exe' -B outputs/diagnostics/audit_20260912/verify_findings.py
```

Un échantillon complémentaire couvre les six derniers fichiers de télémétrie de chacun des six workers, soit 2 660 lignes. Certaines horloges `currentTime` reculent à l’intérieur des fichiers, même en ignorant les trois premières lignes ; les compteurs `steps` examinés ne reculent pas. Ce signal demande un contrôle de l’horodatage, des remises en piste et du lien action–observation avant d’attribuer la cause à des paquets périmés. [Détail de l’échantillon](<C:/Leo projet/Assetto Corsa ai/outputs/diagnostics/audit_20260912/recent_telemetry_summary.json>).

L’audit ne mesure pas la performance d’un correctif en conduite réelle. Il établit des défauts reproductibles et un plan d’expériences pour tester leurs effets. L’entraînement par générations reste une évolution à implémenter après ces corrections.
