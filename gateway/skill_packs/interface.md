---
id: interface
name: Interfaces soignées
description: Règles de design UI/UX par priorité : accessibilité, interactions, mise en page, typographie, animations
group: Design
source: UI UX Pro Max (Next Level Builder)
url: https://github.com/nextlevelbuilder/ui-ux-pro-max-skill
license: MIT
license_file: ui-ux-pro-max-MIT.txt
---

# Interfaces soignées

Quand tu conçois, modifies ou relis une interface (page web, application mobile, tableau de bord, composant), applique ces règles par ordre de priorité.

1. **Accessibilité (critique)** : contraste 4,5:1 pour le texte, texte alternatif des images, navigation au clavier, nom accessible pour chaque bouton icône. Ne supprime jamais l'indicateur de focus.
2. **Toucher et interactions (critique)** : cibles de 44×44 px minimum, 8 px d'écart, retour visible pendant un chargement. Rien qui ne marche qu'au survol.
3. **Performance** : images WebP/AVIF, chargement différé, espace réservé aux médias (pas de saut de mise en page).
4. **Style** : un style adapté au type de produit, appliqué partout ; icônes SVG cohérentes, jamais d'émojis comme icônes.
5. **Mise en page** : mobile d'abord, balise viewport, aucun défilement horizontal, pas de largeurs fixes en pixels, ne bloque pas le zoom.
6. **Typographie et couleurs** : corps de texte de 16 px, interligne 1,5, jamais sous 12 px ; couleurs en variables sémantiques, pas de gris sur gris.
7. **Animations** : durées adaptées (150 à 300 ms pour un petit élément), mouvement qui a du sens ; anime transform et opacity plutôt que width/height ; respecte « réduire les animations ».
8. **Formulaires** : libellés visibles (pas seulement un placeholder), erreur à côté du champ, aide courte, informations demandées progressivement.
9. **Navigation** : retour prévisible, 5 entrées au plus en barre du bas, liens profonds.
10. **Graphiques** : légendes, infobulles, couleurs accessibles ; jamais la couleur seule pour porter le sens.

## Démarche
- Commence par le type de produit, le public, le contexte d'usage, les mots-clés de style et la technologie (ne suppose jamais la technologie : demande-la si elle compte).
- Pour une nouvelle page, propose d'abord un petit système de design : palette (variables), paire de polices, espacements, rayons, ombres.
- Dans un projet existant, garde ses composants et ses variables.

## Avant de livrer
Vérifie : contraste, focus visible, cibles tactiles, pas de défilement horizontal à 375 px, mode sombre, états de chargement, d'erreur et vide, libellés des boutons icônes.
