---
id: verifier
name: Vérifier avant d'affirmer
description: Des preuves avant de dire que c'est fait, corrigé ou que ça marche
source: Superpowers (Jesse Vincent)
url: https://github.com/obra/superpowers
license: MIT
license_file: superpowers-MIT.txt
group: Superpowers : développer avec méthode
---

# Vérifier avant d'affirmer

Principe : des preuves avant les affirmations, toujours.

## Avant de dire « c'est fait », « ça marche », « c'est corrigé »
1. **Identifie** la commande ou l'observation qui prouve l'affirmation.
2. **Exécute-la** (ou demande à l'utilisateur de le faire) en entier, maintenant.
3. **Lis** tout le résultat : code de sortie, nombre d'échecs.
4. **Compare** : le résultat confirme-t-il vraiment l'affirmation ?
5. **Seulement alors**, affirme, en citant la preuve.

## Signaux d'alarme
« devrait », « probablement », « a l'air de » ; se réjouir avant d'avoir vérifié (« Parfait ! », « Terminé ! ») ; une vérification partielle ; faire confiance à un rapport sans l'avoir contrôlé.

## Cas courants
- **Tests** : « les tests passent » seulement après les avoir lancés et lu « 0 échec ».
- **Bug corrigé** : le test qui reproduisait le bug échouait avant et passe après.
- **Compilation** : la commande de build s'est terminée sans erreur.
- **Exigences** : relis la demande point par point et vérifie chacun.

Si tu ne peux rien exécuter toi-même, dis clairement ce qui n'a pas été vérifié et donne la commande qui le vérifiera.
