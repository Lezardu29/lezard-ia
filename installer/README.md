# Installateur Windows de Lezard IA (32.1)

Un fichier `LezardIA-Setup-<version>.exe` pour un client sans compétence technique : il n'a rien à installer
avant (Python et les dépendances sont dedans), aucun droit administrateur n'est demandé.

## Construire

```powershell
.\installer\build.ps1              # -> installer\build\out\LezardIA-Setup-1.0.0.exe
.\installer\build.ps1 -Test        # + suite de tests du projet avec les dépendances embarquées
.\installer\build.ps1 -UpdateLock  # regénère requirements.lock depuis requirements.txt (puis -Test)
```

Prérequis du PC de construction : git et un Python 3.10+ avec pip (3.13 pour `-Test`). Inno Setup 6.7.3 est
téléchargé et installé en mode portable dans `installer\build\tools` s'il est absent (rien dans le registre).

Construction reproductible, à commit égal :

| Entrée | Figée par |
|---|---|
| Python embarqué 3.13.17 (python.org) | empreinte SHA-256 dans `build.ps1` + signature Authenticode de `python.exe` |
| Inno Setup 6.7.3 | empreinte SHA-256 dans `build.ps1` |
| Dépendances | `requirements.lock` : versions exactes et empreintes (`pip --require-hashes`) |
| Code | `git archive HEAD gateway` : le commit, jamais les modifications en cours |

Tout ce qui est construit ou téléchargé reste dans `installer\build\` (ignoré par git). Installateur **non signé**
pour l'instant (32.2) : SmartScreen affiche « éditeur inconnu » (« Informations complémentaires » > « Exécuter
quand même »).

## Ce que fait l'installateur

- Copie dans `%LOCALAPPDATA%\Programs\Lezard IA` : `python\` (embarqué + dépendances), `gateway\`, `lezard.py`.
- Crée `.env` au premier passage seulement (jamais écrasé ensuite) : version gratuite (sans licence), clé
  d'administration (`GATEWAY_MASTER_KEY`), clé de chiffrement (`GATEWAY_ENCRYPTION_KEY`), port (`GATEWAY_PORT` :
  premier port libre à partir de 8000). Aucun secret n'est affiché.
- Raccourcis : menu Démarrer (Lezard IA, Copier ma clé d'administration, Désinstaller), bureau (au choix).
- Démarrage automatique (case cochée par défaut) : raccourci du dossier Démarrage, 30 s après l'ouverture de
  session. **Aucune tâche planifiée** n'est créée.
- **Outils à activer** (cases à cocher ; écrits dans `.env` par `lezard.py tools`, modifiables ensuite) :

  | Case | Effet | À télécharger |
  |---|---|---|
  | Recherche sur Internet (cochée) | `/web` ; `TOOLS_WEB=false` sinon | — |
  | Contrôle du PC | `/pc`, chaque action validée ; `TOOLS_PC=true` | — |
  | Dictée et lecture vocales | pack faster-whisper, Kokoro (`requirements-local.txt`) | ~90 Mo (+ modèles au 1er usage) |
  | Multi-agents CrewAI | pack `requirements-multi.txt` | ~220 Mo |

  Les packs sont téléchargés seulement s'ils sont cochés, depuis PyPI, versions et empreintes figées par
  `lock.py`. Un seul Python pour tout : ils vont dans `packs\<pack>`, ajoutés au chemin par `lezard-packs.pth`.
- **Aucune IA locale n'est installée.** Après l'installation, page Modèles : « Tester les performances de ce PC »
  (deux petits modèles de test, supprimés ensuite) donne la vitesse estimée de chaque modèle ; le client installe
  ceux qu'il veut. Si Ollama manque, la page l'installe (`lezard.py ollama-install` : installateur officiel figé,
  version publiée depuis au moins 14 jours, ~1,5 Go). Ollama installé par Lezard IA ne démarre plus de son côté :
  c'est le service de Lezard IA qui le lance (une seule application en arrière-plan) et le met à jour. Un Ollama
  déjà installé par le client n'est pas touché.
- Premier lancement : la clé d'administration est copiée dans le presse-papiers, avec une explication (« collez-la
  dans Clé d'accès »). Elle se recopie ensuite par le menu Démarrer.

`lezard.py` remplace `lancer.ps1` sur les PC des clients, dont il reprend le superviseur : relance de la gateway
si elle s'arrête (5 s, 10 s… 5 min), redémarrage demandé depuis l'interface (code 3), attente si le port est
occupé, démarrage d'Ollama s'il est installé mais arrêté, journaux dans `data\logs`. Une seule instance par
dossier d'installation. Les variables `GATEWAY_*` du compte Windows sont ignorées : seule compte `.env`.

```
python\python.exe lezard.py status | stop | setup [--port N]      (pythonw.exe : service, open, key)
```

Mise à jour : depuis le tableau de bord (version signée, téléchargée et vérifiée, installée en silencieux) ou en
relançant un installateur plus récent ; le service est arrêté avant la copie, `.env` et `data\` sont gardés, et les
composants déjà installés sont mis à jour (`lezard.py pack refresh`) si leur version a changé.

```
python\python.exe lezard.py pack install voix multi      composants ajoutés après l'installation
python\python.exe lezard.py pack remove multi            composant retiré (place libérée)
python\python.exe lezard.py ollama-install               Ollama (la page Modèles le fait aussi)
python\python.exe lezard.py tools web=on pc=off          outils activés (écrits dans .env)
```

## Désinstallation

Arrête le service, retire le programme, les raccourcis et le démarrage automatique. Elle demande ensuite
« Supprimer aussi vos données ? » (Non par défaut) : `data\` (conversations, documents, base) et `.env` (clés,
dont celle qui chiffre la base) restent ensemble, et une réinstallation les retrouve. En silencieux
(`unins000.exe /VERYSILENT`), les données sont gardées, sauf avec `/DELETEDATA=1`.

## Essayer sans toucher à une installation existante

De préférence dans Windows Sandbox ou une machine virtuelle. Sur un PC qui fait déjà tourner Lezard IA : un autre
dossier, un autre port, sans démarrage automatique ni raccourcis :

```powershell
$t = "$env:TEMP\essai-lezard"
.\installer\build\out\LezardIA-Setup-1.0.0.exe /VERYSILENT /CURRENTUSER /DIR="$t" /PORT=8765 /MERGETASKS="!autostart,!desktopicon" /NOICONS
& "$t\python\pythonw.exe" "$t\lezard.py" service          # puis http://localhost:8765
& "$t\python\python.exe" "$t\lezard.py" stop
& "$t\unins000.exe" /VERYSILENT /DELETEDATA=1
```

Options de l'installateur : `/PORT=N` (premier passage), plus celles d'Inno Setup (`/SILENT`, `/VERYSILENT`,
`/DIR=…`, `/MERGETASKS=…`, `/NOICONS`, `/LOG=…`).

## Limites connues

- Recherche web privée (SearXNG), génération d'images (ComfyUI, plusieurs Go avec PyTorch), Paperless-ngx et
  Nextcloud (Docker) : pas encore proposés par l'installateur.
- Windows 10/11 64 bits seulement.
