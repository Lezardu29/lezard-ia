# Ajouter des fiches à la marketplace sans nouvelle version

Depuis la 1.0.23, chaque Lezard IA installé lit un « supplément de catalogue » : un fichier signé, publié à côté des
versions. Il sert à ajouter une fiche, à corriger une fiche existante (version montée, texte) ou à en retirer une
(paquet devenu dangereux). Aucune nouvelle version de Lezard IA n'est nécessaire.

Les PC le relisent à chaque lancement puis toutes les six heures. Dans l'application, la route
`POST /v1/admin/marketplace/refresh` (clé maître) le relit tout de suite.

## Les quatre étapes

1. Modifier `catalogue/supplement.json` et **augmenter `serial` de 1** (les PC ignorent un numéro qui n'avance pas).
2. Vérifier : `python -m gateway.marketplace_extra check catalogue/supplement.json`
3. Signer : `python -m gateway.marketplace_extra sign --key data/update-signing.key --in catalogue/supplement.json --out dist/catalogue.json`
4. Publier `dist/catalogue.json` à la racine du dépôt `lezard-ia-telecharger` (sous le nom `catalogue.json`), puis
   mettre à jour la marketplace du site : `python -m gateway.marketplace_export`, commit, envoi sur `main`.

Avant de publier une fiche qui s'installe : l'essayer en vrai (installer, utiliser, désinstaller), comme pour le
catalogue du programme. Une version n'est retenue que 14 jours après sa publication.

## La clé

`data/update-signing.key` signe les versions ET ce supplément. Sans elle, plus rien ne peut être ajouté ; si elle
fuit, n'importe qui peut faire installer un programme sur tous les PC. Elle reste hors du dépôt, avec une copie de
secours hors du PC (clé USB ou coffre), jamais envoyée par message.

## Ce qu'une fiche peut contenir

Aucune commande libre. Une recette est une liste d'étapes typées ; tout autre champ fait refuser le fichier.

| Type | Champs | Ce que Lezard IA lance |
| --- | --- | --- |
| `npm` | `package`, `version` | `npm install --global paquet@version` |
| `uv` | `package`, `version`, `python` (facultatif) | `uv tool install --force paquet==version` |
| `winget` | `id` | `winget install --id … --exact --silent` |
| `vscode` | `extension` | `code --install-extension éditeur.nom` |
| `claude-plugin` | `source` (dépôt GitHub), `plugin` | `claude plugin marketplace add …` puis `claude plugin install …` |
| `setup` | `url` (fichier d'une version GitHub), `sha256`, `size`, puis `silent` (.exe, .msi) ou `folder` (.zip) | téléchargement, empreinte vérifiée, installation |

Avec `winget` ou un installateur `.exe` : ajouter `windows_name` (le nom dans Paramètres > Applications) et, pour
un installateur, `uninstall_name`.

## Exemples

Un outil installé par npm :

```json
{
  "id": "mon-outil",
  "name": "Mon outil",
  "description": "Ce qu'il fait, en une phrase.",
  "url": "https://github.com/editeur/mon-outil",
  "license": {"name": "MIT", "copyright": "Éditeur", "url": "https://github.com/editeur/mon-outil/blob/main/LICENSE"},
  "needs": "Node.js.",
  "commands": [{"use": "mon-outil --aide", "what": "Affiche l'aide.", "where": "terminal"}],
  "recipe": {
    "steps": [{"type": "npm", "package": "mon-outil", "version": "1.2.3"}],
    "size": "~2 Mo",
    "after": "Dans un terminal : mon-outil --aide."
  }
}
```

Un serveur MCP (dans `"mcp"`) :

```json
{
  "id": "meteo",
  "name": "Météo",
  "description": "L'IA lit la météo d'une ville.",
  "package": "@editeur/server-meteo",
  "version": "1.4.2",
  "size": "moins de 1 Mo",
  "license": {"name": "MIT", "copyright": "Éditeur", "url": "https://github.com/editeur/server-meteo"},
  "example": "/mcp quel temps fait-il à Brest ?"
}
```

Champs facultatifs d'un serveur MCP : `needs_folder`, `folder_env`, `args` (options simples), `env`,
`only_tools`, et `secret` (`env`, `label`, `url`) avec `service` (où partent les données).

Une fiche sans `recipe` est une simple présentation (lien, licence, commandes), à installer soi-même.

Retirer une fiche (elle reste affichée avec la raison, ne s'installe plus, se désinstalle encore) :

```json
"removed": [{"key": "tool:mon-outil", "reason": "paquet compromis le 2027-01-02."}]
```

Corriger une fiche du programme : la réécrire en entier avec le même `id` ; elle remplace l'ancienne.
`where` d'une commande : `chat`, `claude`, `terminal`, `app`, `vscode`, `browser` ou `python`.
