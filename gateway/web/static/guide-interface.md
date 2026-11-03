# Interface personnalisée de Lezard IA — guide des connecteurs

Ce guide explique comment fabriquer un **pack d'interface** : un fichier `.zip` qui change l'apparence de Lezard IA
(couleurs, polices, images, formes, disposition). On l'importe dans **Apparence → Interface personnalisée →
Importer un .zip**. Il s'applique alors sur toutes les pages (chat, tableau de bord, modèles, connexion) et suit le
compte sur tous les appareils.

Le plus simple : téléchargez le **Modèle (.zip)** depuis le même panneau, modifiez `style.css`, rezippez, importez.

---

## 1. Contenu du .zip

```
mon-interface.zip
├── manifest.json      (obligatoire)
├── style.css          (obligatoire : la feuille appliquée)
├── autres.css         (facultatif, chargé par @import "autres.css";)
└── assets/            (facultatif : images et polices)
    ├── fond.webp
    └── police.woff2
```

Un zip d'un dossier (`mon-interface/manifest.json`…) est accepté aussi : le dossier racine est retiré.

### manifest.json

```json
{
  "name": "Forêt",
  "version": "1.0",
  "description": "Vert profond et bois clair",
  "author": "Tonio",
  "interface": "claude"
}
```

| Champ | Obligatoire | Rôle |
|---|---|---|
| `name` | oui | Nom affiché dans le panneau (60 caractères au plus) |
| `version`, `description`, `author` | non | Affichés sous le nom (200 caractères au plus chacun) |
| `interface` | non | `"claude"` ou `"classic"` : mise en page de base choisie avec le pack |

### Fichiers acceptés

`.css`, `.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`, `.svg`, `.ico`, `.woff`, `.woff2`, `.ttf`, `.otf`, `.json`,
`.md`, `.txt`.

### Limites

- 5 Mo pour le `.zip`, 10 Mo une fois décompressé, 4 Mo par fichier, 100 fichiers ;
- 5 interfaces importées par compte ;
- noms de fichiers : lettres, chiffres, `-`, `_` et `.` (pas d'espace ni d'accent).

### Ce qui est refusé, et pourquoi

- **Aucun script ni page** (`.js`, `.html`…) : un script importé tournerait dans la page et pourrait lire votre clé
  de connexion. Un pack ne peut donc changer que l'apparence, jamais le comportement.
- **Aucune adresse externe** dans les feuilles (`url(https://…)`, `@import "https://…"`, Google Fonts…) : la
  sécurité des pages les bloquerait sans rien dire. Mettez les images et les polices **dans le zip** et utilisez des
  adresses **relatives** : `url(assets/fond.webp)`. Elles sont résolues depuis `style.css`.

---

## 2. Règles d'or

1. **Les couleurs du thème se remplacent avec `!important`.** Le moteur de thème pose les variables de couleur
   directement sur `<html>` ; sans `!important`, votre valeur est ignorée.
   ```css
   :root { --accent: #2d6a4f !important; }
   ```
2. **Votre feuille passe en dernier**, après toutes celles de Lezard IA : à sélecteur égal, elle gagne.
3. **Ne masquez pas les connecteurs essentiels** (section 6) : la page en a besoin pour fonctionner.
4. **Clair et sombre** : `<html data-theme="light|dark">` dit lequel est affiché. Prévoyez les deux, ou forcez
   un mode dans le panneau.
5. **Téléphone** : testez à 390 px de large. Les règles `@media (max-width: 640px)` de Lezard IA y changent la
   disposition (tiroir, feuilles en bas de l'écran).
6. **Visez votre pack seulement** : `html[data-pack]` est présent quand une interface personnalisée est active.

---

## 3. Variables (le plus simple)

À redéfinir dans `:root { … !important; }`. Elles sont reprises partout.

| Variable | Rôle |
|---|---|
| `--bg` | Fond de la page |
| `--surface` | Fond des cartes, panneaux, saisie |
| `--surface-2` | Fond secondaire (survol, pastilles, onglets) |
| `--sidebar` | Fond de la barre latérale (interface Claude) |
| `--border` | Bordures |
| `--text` | Texte principal |
| `--text-muted` | Texte secondaire |
| `--accent` | Couleur d'action (boutons, liens, sélection) |
| `--accent-contrast` | Texte posé sur `--accent` |
| `--accent-soft` | Fond léger teinté de l'accent |
| `--bubble-user` | Bulle de vos messages |
| `--code-bg`, `--code-head` | Blocs de code et leur en-tête |
| `--danger`, `--danger-soft` | Erreurs, suppressions |
| `--success`, `--warning` | Réussites, avertissements |
| `--font` | Police du texte |
| `--font-serif` | Police des titres et des réponses (interface Claude) |
| `--mono` | Police du code |
| `--radius`, `--radius-sm` | Arrondis des cartes et des petits éléments |
| `--shadow` | Ombre des cartes et panneaux |
| `--preview-w` | Largeur du panneau d'aperçu (grand écran) |
| `--tk-kw`, `--tk-str`, `--tk-num`, `--tk-com`, `--tk-type`, `--tk-prop` | Coloration du code |

Police à vous :

```css
@font-face { font-family: "Ma police"; src: url(assets/police.woff2) format("woff2"); font-display: swap; }
:root { --font: "Ma police", system-ui, sans-serif; }
```

---

## 4. États de la page (attributs et classes)

Pour adapter le style à la situation.

### Sur `<html>`

| Attribut | Valeurs | Sens |
|---|---|---|
| `data-ui` | `claude`, `classic` | Mise en page de base |
| `data-theme` | `light`, `dark` | Mode affiché |
| `data-input` | `touch`, `mouse` | Écran tactile ou souris |
| `data-layout` | `compact`, `wide` | Écran étroit (≤ 640 px) ou large |
| `data-pack` | identifiant | Une interface personnalisée est active |

### Sur `<body>`

| Classe | Sens |
|---|---|
| `chat-page` | Page du chat (sinon : tableau de bord, modèles, connexion) |
| `has-messages` | La conversation a des messages (sinon : accueil) |
| `conv-docked` | Barre latérale ouverte et fixée à gauche |
| `conv-rail` | Barre latérale repliée en colonne d'icônes (interface Claude) |
| `preview-open` | Panneau d'aperçu ouvert à droite |
| `projects-mode` | Liste des projets affichée |
| `in-project` | Accueil d'un projet (conversation vide d'un projet) |
| `private-mode` | Conversation privée (IA locales seulement) |
| `drawer-open` | Tiroir de navigation ouvert (téléphone) |
| `share-page` | Page d'une conversation partagée par lien |

### Ailleurs

| Sélecteur | Sens |
|---|---|
| `.msg.streaming` | Réponse en train de s'écrire |
| `.conv-panel.open` | Barre latérale ouverte |
| `.conv-panel.searching` | Recherche ouverte dans la barre latérale |
| `.conv-item[aria-current="true"]` | Conversation affichée, dans la liste |
| `.mic.recording` | Dictée en cours |
| `[aria-checked="true"]`, `[aria-selected="true"]`, `[aria-pressed="true"]` | Choix actif (options, onglets) |
| `[hidden]` | Élément caché : ne le forcez pas à s'afficher |

---

## 5. Zones de la page du chat

### Barre du haut — `.topbar`

| Connecteur | Rôle |
|---|---|
| `#conv-btn` | Ouvre la barre latérale |
| `#conv-project` | Projet de la conversation (« Projet / ») |
| `#conv-current` | Titre de la conversation |
| `#new-chat` | Nouvelle conversation |
| `#export-wrap`, `#export-btn`, `#export-menu` | Export de la conversation |
| `#plan` | Offre (pastille) |
| `[data-drawer]` | Boutons rangés dans le menu du compte (interface Claude) ou le tiroir (téléphone) |
| `#settings-btn`, `#usage-btn`, `#theme-btn` | Paramètres, utilisation (contexte et plafonds), apparence |

### Barre latérale — `#conv-panel`

| Connecteur | Rôle |
|---|---|
| `.sb-head`, `#sb-toggle`, `.sb-brand` | En-tête : replier, marque |
| `.sb-nav`, `.sb-item`, `#sb-new`, `#sb-search`, `#sb-projects` | Nouvelle conversation, Rechercher, Projets |
| `.conv-head`, `#conv-new`, `#conv-projects`, `#conv-close` | En-tête de l'interface Lezard IA |
| `#conv-search` | Recherche dans les conversations |
| `#conv-list`, `.conv-section`, `.conv-item`, `.conv-open`, `.conv-title`, `.conv-date`, `.conv-snippet` | Liste (Favoris, Récents) |
| `.conv-menu` (`details.row-menu`), `.menu` | Menu « ⋯ » d'une conversation |
| `#conv-empty`, `#conv-foot`, `#conv-store`, `#conv-note` | Liste vide, enregistrement sur la gateway |
| `#sb-account`, `#acct-btn`, `#acct-avatar`, `#acct-name`, `#acct-plan`, `#acct-menu` | Menu du compte |
| `#conv-backdrop` | Fond assombri du tiroir |

### Accueil — `#welcome`

| Connecteur | Rôle |
|---|---|
| `.welcome h1`, `.welcome .gradient` | Salutation (« Bonsoir, … ») |
| `#suggestions`, `.suggestion` | Suggestions de questions |
| `#project-head`, `#ph-name`, `#ph-desc`, `#ph-back`, `#ph-edit`, `#ph-delete` | En-tête d'un projet |
| `#project-home`, `#ph-list`, `.ph-conv`, `.ph-card`, `#ph-instructions`, `#ph-files`, `#ph-add` | Page d'un projet |
| `#projects-list`, `#projects-grid`, `.pl-card`, `#project-create` | Liste des projets |

### Fil de la conversation — `#scroller` puis `#messages`

| Connecteur | Rôle |
|---|---|
| `.msg.user`, `.msg.assistant`, `.msg.compact` | Un message (vous, l'IA, résumé de compactage) |
| `.user-col`, `.avatar`, `.content`, `.body` | Colonnes et corps du message |
| `.reasoning`, `.reasoning-body` | Bloc « Réflexion » repliable |
| `.code`, `.code-head`, `.copy`, `.code.previewable`, `.preview-btn` | Blocs de code, copie, aperçu |
| `.tool-log`, `.tool-step` | Étapes des outils de la gateway |
| `.msg-actions`, `.msg-act`, `.msg-versions` | Copier, écouter, régénérer, variantes ‹ 1/2 › |
| `.thinking` | « Réflexion en cours… » |
| `.msg-error`, `.stopped` | Erreur, réponse interrompue |

### Saisie — `.composer-wrap` puis `#composer`

| Connecteur | Rôle |
|---|---|
| `#prompt` | Champ de saisie (**essentiel**) |
| `#send` | Envoyer / arrêter (**essentiel**) |
| `#attach`, `#attachments`, `.attach-sheet` | Pièces jointes (`.attach-sheet` : menu du « + » sur écran tactile) |
| `#mic`, `#dictation` | Dictée (`.mic.off` : indisponible, la raison s'affiche au clic) |
| `#toolbar`, `.chip`, `.auto-model` | Choix du modèle et du style |
| `.context-ring` | Jauge de contexte, visible à partir de 25 % de la fenêtre du modèle |
| `#chat-notice`, `.disclaimer` | Avis au-dessus de la saisie, mention « Les IA peuvent se tromper » |

### Panneaux et fenêtres

| Connecteur | Rôle |
|---|---|
| `#preview-panel`, `.preview-head`, `.preview-tabs`, `#preview-frame-wrap`, `#preview-code` | Panneau d'aperçu (page HTML, SVG, diagramme) |
| `.model-picker`, `.mp-option`, `.mp-group` | Sélecteur de modèle et de style |
| `.theme-panel` | Panneau Apparence |
| `#profile-dialog`, `#project-dialog`, `#move-dialog`, `#share-dialog` | Paramètres, projet, ranger, partager |
| `dialog` | Toutes les fenêtres |

### Autres pages

| Page | Classe sur `<body>` ou repère |
|---|---|
| Tableau de bord, Modèles | `.topbar`, `.nav`, `.dash`, `.panel`, `.card`, `.kpi` |
| Connexion | `.card`, `.brand` |
| Conversation partagée | `body.share-page`, `.share-top`, `.share-title`, `#messages` |

---

## 6. Connecteurs essentiels (à ne jamais masquer)

Masquer l'un d'eux (`display: none`, `visibility: hidden`, taille nulle, hors écran) empêche d'utiliser Lezard IA :

`#prompt`, `#send`, `#composer`, `#messages`, `#scroller`, `#conv-btn` (téléphone), `#conv-panel`, `#acct-btn`,
`#theme-btn` (pour revenir en arrière), `dialog`, `.theme-panel`.

Si un pack rend la page inutilisable : ajoutez `?pack=aucun` à l'adresse (par exemple
`https://ia.lezard-ia.fr/app?pack=aucun`) : l'interface personnalisée est retirée, sur ce compte. Ou, depuis un autre
appareil : **Apparence → Interface personnalisée → Aucune**.

---

## 7. Exemple complet

```css
/* style.css — interface « Forêt » */
@font-face { font-family: "Lora"; src: url(assets/lora.woff2) format("woff2"); font-display: swap; }

:root {
  --accent: #2d6a4f !important;
  --accent-contrast: #ffffff !important;
  --accent-soft: #dcefe4 !important;
  --radius: 20px;
  --font-serif: "Lora", Georgia, serif;
}
html[data-theme="light"] {
  --bg: #f4f1ea !important;
  --surface: #fffdf8 !important;
  --sidebar: #e9e4d8 !important;
  --bubble-user: #e3eee6 !important;
}
html[data-theme="dark"] {
  --bg: #14201a !important;
  --surface: #1b2a22 !important;
  --sidebar: #101912 !important;
  --bubble-user: #24382d !important;
}

/* fond discret derrière le fil */
body.chat-page { background-image: url(assets/motif.svg); background-size: 240px; }

/* saisie plus ronde, bouton d'envoi assorti */
#composer { border-radius: 30px; }
#send:not(:disabled) { background: var(--accent); }

/* vos messages alignés à droite, sans bulle */
.msg.user .body { background: transparent; border: 1px solid var(--border); }
```

---

## 8. Mettre au point

- Le panneau affiche le message exact si le zip est refusé (fichier interdit, adresse externe, manifeste incomplet).
- Réimportez après chaque modification, puis supprimez l'ancienne version (5 au plus).
- Les outils de développement du navigateur (F12) montrent quelles règles s'appliquent à un élément ; votre feuille
  s'appelle `style.css` et vient de `/ui-packs/…`.
