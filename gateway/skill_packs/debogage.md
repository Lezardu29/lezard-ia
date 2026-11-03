---
id: debogage
name: Débogage méthodique
description: Trouver la cause racine avant de corriger, en quatre phases
source: Superpowers (Jesse Vincent)
url: https://github.com/obra/superpowers
license: MIT
license_file: superpowers-MIT.txt
group: Superpowers : développer avec méthode
---

# Débogage méthodique

Principe : trouve toujours la cause racine avant de corriger. Corriger un symptôme est un échec.

À appliquer surtout quand on est pressé, quand « une petite correction » semble évidente, ou quand une correction précédente n'a pas marché.

## Phase 1 : enquête (avant toute correction)
1. Lis les messages d'erreur en entier : ligne, fichier, code d'erreur.
2. Reproduis le problème de façon fiable ; sinon, rassemble plus de données au lieu de deviner.
3. Regarde ce qui a changé récemment (code, dépendances, configuration, environnement).
4. Dans un système à plusieurs composants, vérifie ce qui entre et sort de chacun pour savoir lequel casse.
5. Remonte le flux de données jusqu'à l'origine de la mauvaise valeur.

## Phase 2 : comparaison
Trouve un exemple qui marche, compare-le à celui qui casse, liste toutes les différences, même petites.

## Phase 3 : hypothèse
Formule une seule hypothèse (« X est la cause, parce que Y »), teste-la par le plus petit changement possible, une variable à la fois. Si elle tombe, formule-en une autre ; n'empile pas les corrections.

## Phase 4 : correction
1. Écris d'abord un test qui reproduit le bug.
2. Corrige la cause racine, une seule modification.
3. Vérifie que le test passe et que rien d'autre ne casse.
4. Après trois corrections ratées, arrête : remets en question l'architecture et parles-en à l'utilisateur.

Dans tes réponses, dis à quelle phase tu en es et ce qui prouve chaque conclusion.
