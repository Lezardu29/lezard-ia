---
id: plan
name: Plan d'implémentation
description: Découper un travail en petites étapes vérifiables, avec fichiers, tests et commandes exacts
source: Superpowers (Jesse Vincent)
url: https://github.com/obra/superpowers
license: MIT
license_file: superpowers-MIT.txt
group: Superpowers : développer avec méthode
---

# Plan d'implémentation

Quand on te demande un plan, écris-le pour quelqu'un de compétent qui ne connaît pas le projet : il doit pouvoir l'exécuter sans deviner.

## En-tête
- **Objectif** : une phrase.
- **Architecture** : 2 ou 3 phrases sur l'approche.
- **Technologies** : les principales.
- **Contraintes** : ce qui ne doit pas changer.

## Structure
- Des unités aux frontières claires, un rôle par fichier ; les fichiers qui changent ensemble vivent ensemble.
- Dans un projet existant, suis ses conventions.
- Un travail trop gros se découpe en plusieurs plans livrables séparément.

## Chaque tâche
- **Fichiers** : à créer, à modifier (avec les lignes si possible), de test, avec leurs chemins exacts.
- **Interfaces** : ce qu'elle utilise des tâches précédentes, ce qu'elle fournit aux suivantes (noms et signatures exacts).
- **Étapes** : chacune est une seule action au résultat vérifiable, par exemple :
  1. Écrire le test qui échoue (avec son code).
  2. Le lancer et constater l'échec (commande exacte, échec attendu).
  3. Écrire le code minimal (avec son code).
  4. Relancer les tests et constater la réussite.
  5. Enregistrer (commit).

## Règles
- Du code réel dans le plan, pas « ajouter la validation ».
- Pas de trous (« TODO », « etc. ») : tout ce qui est nécessaire est écrit.
- Signale les risques et ce qu'il faudra relire en priorité.
