# Lezard IA

Lezard IA rassemble les modèles d'IA installés sur votre PC (Ollama, LM Studio…) dans une seule interface :
discussion, documents, voix, et un agent qui peut utiliser votre PC. Vos conversations restent sur votre machine.

Le logiciel est **open source**. Il fonctionne avec vos modèles locaux, sans compte et sans Internet. Lezard IA ne
fournit aucun modèle : vous installez ceux de votre choix, et vous respectez leurs licences.

## Installation sur Windows (le plus simple)

Téléchargez **[LezardIA-Setup.exe](https://github.com/Lezardu29/lezard-ia-telecharger/raw/main/LezardIA-Setup.exe)**
(Windows 10 et 11, environ 29 Mo) et lancez-le : rien à installer avant, aucun terminal, aucun droit administrateur.
L'installateur n'est pas encore signé : si Windows affiche « Windows a protégé votre ordinateur », cliquez sur
« Informations complémentaires », puis « Exécuter quand même ». Guide pas à pas : <https://lezard-ia.fr/aide.html>.

Chaque installation crée ses propres clés (clé d'administration, clé de chiffrement) : aucun identifiant n'est
fourni ni partagé. Toutes les versions : [Releases](https://github.com/Lezardu29/lezard-ia/releases).

## Démarrage avec Docker (macOS, Linux, développeurs)

Prérequis : [Docker](https://www.docker.com/products/docker-desktop/) et [Ollama](https://ollama.com) (ou LM Studio)
avec au moins un modèle installé (`ollama pull llama3.2`).

```bash
git clone https://github.com/Lezardu29/lezard-ia.git
cd lezard-ia
cp .env.example .env
# générer la clé maître (le mot de passe de l'administration) et la coller dans .env :
docker compose run --rm gateway python -m gateway.crypto genkey
docker compose up -d
```

Puis ouvrir <http://localhost:8000> et se connecter avec la clé maître. Les modèles d'Ollama installés sur le PC
sont détectés automatiquement (`host.docker.internal`).

### Les téléphones de la maison (Wi-Fi)

Dans `.env`, décommenter `GATEWAY_BIND=0.0.0.0`, puis `docker compose up -d`. Le téléphone ouvre
`http://<adresse-du-PC>:8000`. Une clé est alors exigée pour tout accès depuis le réseau.

### Mise à jour et sauvegarde

- Mise à jour : `git pull` puis `docker compose up -d --build`.
- Données (conversations, documents, réglages, clé de chiffrement) : dans le volume Docker de la passerelle.
  Sauvegardez-le avant une mise à jour importante. La clé de chiffrement est indispensable pour relire les données :
  ne la perdez pas.

## Démarrage sans Docker (développeurs)

```bash
python -m venv .venv && . .venv/bin/activate        # Windows : .venv\Scripts\activate
pip install -r requirements.txt
export GATEWAY_MASTER_KEY="$(python -m gateway.crypto genkey)"
uvicorn gateway.main:app --host 127.0.0.1 --port 8000
```

Tests : `pip install -r requirements-dev.txt`, puis `python -m pytest`.

## Ce que fait Lezard IA

- Discussion avec vos modèles : historique, recherche, export, choix du modèle à chaque message.
- Documents : questions sur vos PDF et textes, avec les sources citées.
- Voix : dictée (Whisper) et lecture à voix haute.
- Agent du PC : fichiers, applications, recherche, sous votre contrôle et avec validation des actions sensibles.
- Plusieurs modèles ensemble sur une même question.
- Commandes du chat : `/pc`, `/web`, `/modele`, `/mode`, `/effort`, `/multi`, `/prive`… (`/aide` pour la liste).

## Recherche web privée avec SearXNG

Par défaut, `/web` cherche avec DuckDuckGo. Pour que vos recherches ne partent chez personne d'autre, hébergez
[SearXNG](https://docs.searxng.org/) et indiquez son adresse dans `.env` : `SEARXNG_URL=http://localhost:8080`.
Dans son `settings.yml`, activez le format JSON (`search: formats: [html, json]`). Si SearXNG est injoignable,
Lezard IA le signale et ne se rabat pas sur un autre moteur. Installé dans un dossier avec son environnement
(`venv`, `src`, `settings.yml`), SearXNG peut être lancé par Lezard IA lui-même, dans le même terminal que la
passerelle : `SEARXNG_HOME=C:\chemin\vers\searxng`. Il est alors relancé s'il s'arrête.

## Génération d'images avec ComfyUI

Installez [ComfyUI](https://github.com/comfyanonymous/ComfyUI) avec un modèle d'image (checkpoint, par exemple
Stable Diffusion) et indiquez son adresse dans `.env` : `COMFYUI_URL=http://localhost:8188`. Dans le chat, tapez
`/image un lézard vert sur une pierre au soleil` ; d'autres logiciels peuvent utiliser l'API `/v1/images/generations`
(format OpenAI) avec une clé `sk-app-…`. Avec `COMFYUI_HOME=C:\chemin\vers\ComfyUI` (dossier contenant `venv` et
`main.py`), Lezard IA lance ComfyUI dans le même terminal, sur 127.0.0.1 seulement, et le relance s'il s'arrête. Une image est générée à la fois, par la carte graphique du PC, qui est
partagée avec vos modèles de langage : la génération peut ralentir les réponses du chat pendant quelques secondes.

## Serveurs MCP (outils pour l'IA)

Lezard IA peut brancher vos serveurs [MCP](https://modelcontextprotocol.io) (fichiers, agenda, GitHub, domotique…) :
leurs outils deviennent utilisables par l'IA avec la commande `/mcp`. Déclarez-les dans `data/mcp.json`, puis
redémarrez :

```json
{"servers": {
  "fichiers": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/Users/moi/Documents"]},
  "meteo": {"url": "http://localhost:9000/mcp", "headers": {"Authorization": "Bearer …"}}
}}
```

Sûreté : seul l'administrateur déclare les serveurs ; un serveur lancé par une commande ne reçoit pas les secrets de
Lezard IA (clé maître, clés d'API) ; **chaque appel d'outil demande votre validation** (sauf `"trust": true` pour un
serveur de confiance) ; `"local_only": true` interdit l'outil avec un modèle cloud. N'ajoutez que des serveurs dont
vous maîtrisez la source : un serveur lancé par commande s'exécute avec vos droits sur le PC.

## Vos documents : Paperless-ngx et Nextcloud

L'IA peut chercher et lire (en lecture seule) vos documents avec la commande `/docs`, par exemple
`/docs trouve ma dernière facture d'électricité`. Réglages dans `.env`, puis redémarrage :

- **Paperless-ngx** : `PAPERLESS_URL=http://localhost:8000` et `PAPERLESS_TOKEN=` (votre profil, « Jeton d'API »).
- **Nextcloud** : `NEXTCLOUD_URL=https://cloud.exemple.fr`, `NEXTCLOUD_USER=` et `NEXTCLOUD_APP_PASSWORD=` (Paramètres,
  Sécurité, « Mot de passe d'application » : jamais le mot de passe du compte). PDF, Word, Excel et texte sont lus.

Ces documents sont privés : les outils sont **refusés avec un modèle cloud** (ils restent donc sur vos IA locales),
sauf si vous ajoutez `SOURCES_ALLOW_CLOUD=true`. Seule la clé maître y a accès.

## Accès depuis n'importe où

Tout le logiciel est ouvert, y compris l'accès depuis Internet par votre propre tunnel. Le site de Lezard IA propose en
plus un accès clé en main, même PC éteint, gratuit : créez un compte puis votre licence, qui arrive dans
« Accès par le site ».

## Licence

Lezard IA est distribué sous licence [GNU AGPL v3](LICENSE). Vous pouvez l'utiliser, le modifier et le redistribuer ;
si vous proposez une version modifiée à d'autres personnes, y compris par le réseau, vous devez aussi leur donner
son code source.
