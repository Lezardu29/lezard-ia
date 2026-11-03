---
id: tdd
name: Développement piloté par les tests
description: Écrire le test qui échoue avant le code, puis le code minimal qui le fait passer
source: Superpowers (Jesse Vincent)
url: https://github.com/obra/superpowers
license: MIT
license_file: superpowers-MIT.txt
group: Superpowers : développer avec méthode
---

# Développement piloté par les tests (TDD)

Principe : si tu n'as pas vu le test échouer, tu ne sais pas s'il teste la bonne chose.

**À utiliser pour** : nouvelles fonctions, corrections de bugs, refactorisations, changements de comportement. Exceptions à demander à l'utilisateur : prototypes jetables, code généré, fichiers de configuration.

## La règle d'or
Pas de code de production sans un test qui échoue d'abord. Du code écrit avant son test se supprime et se réécrit à partir du test.

## Rouge, vert, refactorisation
1. **Rouge** : un test pour un seul comportement, au nom clair, sur du vrai code (pas de simulacres sauf nécessité).
2. **Constater l'échec** (obligatoire) : le test échoue (pas une erreur de syntaxe), avec le message attendu, parce que la fonction manque. S'il passe déjà, il teste un comportement existant : corrige le test.
3. **Vert** : le code le plus simple qui fait passer ce test, rien de plus.
4. **Constater la réussite** (obligatoire) : ce test passe, les autres aussi, sans erreur ni avertissement. S'il échoue, corrige le code, pas le test.
5. **Refactoriser** : nettoyer en gardant tout au vert.

## Signaux d'alarme
« Je teste après », « c'est trop simple pour un test », « je garde ce code comme référence », un test qui passe du premier coup : reviens à l'étape 1.

Dans tes réponses, montre le test avant le code, et la commande pour le lancer.
