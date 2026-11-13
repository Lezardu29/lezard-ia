"""Chef d'orchestre : python mcp_orchestre.py [dossier autorisé…]   (serveur MCP, entrée et sortie standard)
                      python mcp_orchestre.py --skill | --remove-skill   (méthode pour Claude Code)
Outils d'économie de tokens à part, chacun avec sa fiche de marketplace (même fichier : aucun module à importer) :
    python mcp_orchestre.py --serveur lezard [dossier…]       tous ces outils derrière un interrupteur (/lezard on, off)
    python mcp_orchestre.py --serveur resumes [dossier…]      mémoire des résumés de fichiers (jamais relus)
    python mcp_orchestre.py --serveur brouillons [dossier…]   contrôle sans modèle et retouche par un modèle local
    python mcp_orchestre.py --agents | --remove-agents        assistants Claude moins chers (Sonnet, Haiku)
    python mcp_orchestre.py --env NOM VALEUR | --env-remove NOM   Claude Code allégé (variable de l'utilisateur)

Une IA forte (Claude Fable ou Opus dans Claude Code, ou tout modèle qui parle MCP) garde le rôle difficile — découper
le travail, écrire la consigne de chaque étape, choisir le modèle de chaque étape, vérifier — et confie les étapes
simples à des modèles moins chers : ceux de l'Ollama de ce PC (gratuits), ou ceux de Lezard IA. Une étape que son
modèle ne sait pas faire revient « BLOQUÉE » avec la raison : l'IA forte précise la consigne, change de modèle ou
la fait elle-même. But : dépenser le moins possible de tokens du modèle fort.

Outils :
- `modeles` : les modèles disponibles, du plus léger au plus lourd, celui déjà chargé, celui pris par défaut.
- `bilan` : en fin de travail, les étapes confiées et la vérification finale à faire.
- `confier_etape` : une consigne complète, des fichiers à lire (le serveur les lit lui-même : leur contenu ne passe
  pas par l'IA qui appelle), le modèle voulu, et un fichier où écrire la réponse (elle ne repasse pas non plus par
  l'IA qui appelle : seul un reçu de quelques lignes lui revient).

Lancé comme un script (aucune dépendance, pas même le paquet gateway) ; un message JSON par ligne. Fichiers : lus et
écrits seulement dans les dossiers donnés en arguments (« . » : le dossier du projet ouvert) ; sans argument, aucun.
Réglages (variables d'environnement) : OLLAMA_HOST ; ORCHESTRE_MODELE (modèle par défaut) ; ORCHESTRE_FENETRE (tokens
de contexte des modèles d'Ollama ; sans réglage : 16 384, doublés tant que la demande n'y tient pas, jusqu'à 65 536
ou la fenêtre réelle du modèle) ; ORCHESTRE_DELAI (secondes par étape, 1 500) ; ORCHESTRE_MODE (local, eco, rapide,
equilibre : qui choisit le modèle des étapes, voir /lezard) ; ORCHESTRE_MEMOIRE (dossier des
résumés) ; LEZARD_IA_URL et LEZARD_IA_KEY (les étapes passent alors par Lezard IA : tous ses modèles, « auto »
compris, et son suivi des coûts).
"""

import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROTOCOL = "2024-11-05"
EMBEDDINGS = ("embed", "bge", "minilm", "e5-", "gte-")  # modèles de vecteurs : ils n'écrivent pas de texte
MAX_FILE_CHARS = 200_000  # par fichier lu ; au-delà, le début seulement (signalé au modèle)
DEFAULT_WINDOW = 16_384
SOLID_BYTES = 2_000_000_000  # en dessous (modèles de 1 milliard de paramètres), trop d'inventions pour une étape
CODE_NAMES = ("coder", "code", "devstral", "codestral")
BLOCKED = re.compile(r"^\W{0,3}BLOQU[EÉ]E?\b\W*", re.IGNORECASE)
WORKER_RULES = (
    "Tu exécutes UNE étape d'un travail plus grand, confiée par une IA qui orchestre. Fais exactement ce que la "
    "consigne demande, dans le format demandé, sans introduction ni conclusion. Tu ne vois rien d'autre que ce "
    "message. Si tu ne peux pas réussir l'étape avec ce qui t'est donné (information ou fichier manquant, consigne "
    "ambiguë, tâche au-dessus de tes moyens), n'invente rien : réponds seulement « BLOQUE: » suivi de la raison "
    "précise et de ce qu'il te faudrait. Ne cite jamais un nom (fonction, fichier, option) que tu ne vois pas "
    "écrit tel quel dans ce qui t'est donné."
)
NUMBERED_RULE = (
    "Dans chaque fichier donné, une ligne commence par son numéro, puis une tabulation : ce numéro ne fait pas "
    "partie du fichier. Pour citer une ligne, recopie le numéro écrit devant elle, ne le calcule jamais. "
    "N'écris pas ces numéros dans du code que tu produis."
)
SKILL = """---
name: orchestre
description: Use when the user asks to orchestrate a task, to delegate work to cheaper or local models, or to spend fewer tokens on a multi-step task ("/orchestre", "orchestre ça", "délègue aux petits modèles", "économise les tokens"). Requires the MCP server "orchestre" or "lezard" (tools modeles, confier_etape, bilan).
---

# Chef d'orchestre

Tu es le modèle fort et cher. Ton rôle : penser, découper, vérifier. Le travail simple part vers des modèles
moins chers avec l'outil `confier_etape` du serveur MCP « orchestre ». Objectif : le moins possible de TES tokens,
sans baisser la qualité du résultat.

## D'abord : est-ce que ça vaut le coup ?

L'orchestration ne fait gagner des tokens que s'il y a beaucoup à LIRE ou à ÉCRIRE : de gros fichiers (plus de
1 000 lignes en tout), de longs journaux, beaucoup de texte répétitif à produire. Mesuré : 57 % d'économie sur
5 000 lignes à documenter, rien du tout sur 600 lignes, et dix fois plus de temps dans les deux cas. Pour une petite
tâche, dis-le en une phrase à l'utilisateur et fais-la directement, même s'il a tapé /orchestre.

## Méthode

1. **Découpe** la demande en étapes courtes et vérifiables. Lis le moins possible toi-même : pour connaître un gros
   fichier, confie « résume ce fichier : rôle, fonctions, points d'entrée » plutôt que de le lire.
2. **Répartis.** Garde pour toi : architecture, décisions, raisonnement difficile, code délicat, sécurité,
   vérification finale. Confie : lire et résumer de longs fichiers, extraire, trier, reformuler, traduire,
   documenter, écrire du code répétitif ou des tests simples d'après un modèle clair.
3. **Choisis un modèle par étape** : appelle `modeles` une seule fois. S'il propose « auto », laisse « auto » :
   Lezard IA choisit selon le mode voulu par l'utilisateur (éco, rapide) ; ne nomme un modèle « cloud, payant »
   que si une étape reste bloquée. Sinon (modèles locaux seulement) prends le plus léger capable de l'étape
   et dont la fenêtre contient le fichier (`modeles` dit combien de lignes chacun lit ; au-delà, confie le fichier
   en plusieurs plages « fichier.py:1-800 », une étape par plage)
   (celui déjà chargé si possible : aucun rechargement). Un modèle « code » pour du code.
4. **Écris une consigne autonome par étape** : le modèle ne voit rien de la conversation. Donne l'objectif, le
   format de sortie exact, les contraintes, un exemple si utile. Pour tout ce qu'il tire d'un fichier, exige le
   nom exact tel qu'il est écrit et son numéro de ligne (« `nom` (ligne N) : … ») : un petit modèle invente des
   noms, et une ligne citée se vérifie d'une seule recherche. Passe les fichiers par `fichiers` (ne recopie pas
   leur contenu) et fais écrire les longues réponses avec `ecrire_dans` (tu ne reçois alors qu'un reçu).
5. **Annonce le plan** à l'utilisateur en un tableau court (étape, qui la fait, pourquoi), puis exécute sans
   attendre, sauf s'il a demandé à valider. **Deux étapes indépendantes ou plus : un seul appel `confier_lot`**,
   jamais plusieurs `confier_etape` à la suite. Chacun de tes appels te fait relire toute la conversation : mesuré,
   c'est là que part presque toute la facture (24 à 41 tours pour quatre fichiers). `confier_lot` confie, contrôle
   chaque morceau écrit (comme `controler` avec « recaler ») et assemble le tout (`assembler_dans`) : mets ce qui
   est commun dans `consigne_commune`, et un `ecrire_dans` différent par étape.
6. **Vérifie à bas coût** chaque résultat : lancer les tests, chercher un motif, contrôler quelques lignes
   citées. Ne relis pas tout. Un brouillon imparfait se corrige par une retouche, pas par une réécriture complète.
7. **Étape bloquée** (« ÉTAPE BLOQUÉE » avec la raison) ou résultat faux : dans l'ordre, (a) donne ce qui manquait
   ou précise la consigne et relance une fois ; (b) relance sur un modèle plus fort ; (c) fais l'étape toi-même.
   Jamais plus de deux relances pour une étape.
8. **Vérification finale, obligatoire** avant de dire que c'est fini :
   - appelle `bilan` : il liste chaque étape confiée, son modèle, son état et les fichiers écrits ;
   - reprends la demande d'origine point par point et contrôle que chacun est satisfait ;
   - contrôle chaque fichier écrit par un modèle moins fort : tests ou commande qui le prouve s'il y en a, sinon
     recherche des noms cités et lecture de quelques passages ; corrige ce qui est faux ;
   - aucune étape ne doit rester « bloquée » sans avoir été reprise.
9. **Termine** par un bilan court : étapes confiées et à quel modèle, étapes reprises par toi, ce qui a été vérifié
   et comment, et ce qui n'a PAS pu être vérifié. Ne dis jamais « vérifié » pour ce que tu n'as pas contrôlé.

## Si ces outils sont là, ils remplacent tes propres lectures

Mesuré : sans cette règle, ils restent inutilisés et tu refais leur travail au prix fort.

- `resumer_fichier`, `resumer_dossier` : à l'étape 1, à la place de lire un gros fichier ou de confier son résumé.
  Un fichier déjà résumé ne coûte plus rien.
- `controler` avec « recaler » à true : aux étapes 6 et 8, sur chaque document écrit par un modèle moins fort
  (déjà fait pour ce qui sort de `confier_lot` : ne le refais pas), AVANT toute recherche ou lecture de ta part. Il ne consomme aucun token, corrige lui-même les numéros de ligne
  faux et rend la liste exacte de ce qui reste : noms absents, lignes qu'il n'a pas su retrouver.
- `retoucher` : pour corriger ce que `controler` a signalé, au lieu de réécrire le passage toi-même. Une seule
  retouche par document ; s'il reste des erreurs après elle, corrige-les à la main.

## À ne pas faire

- Confier une étape dont tu ne pourrais pas vérifier le résultat.
- Confier un secret (clé, mot de passe) ou une décision.
- Découper un travail de deux minutes : fais-le directement.
"""


def host() -> str:
    raw = (os.environ.get("OLLAMA_HOST") or "127.0.0.1:11434").strip().rstrip("/")
    raw = raw if "://" in raw else f"http://{raw}"
    return raw.replace("://0.0.0.0", "://127.0.0.1")  # adresse d'écoute, pas d'appel


ROUTES = ("local", "eco", "rapide", "equilibre")  # qui choisit le modèle d'une étape, et comment
GATEWAY_MODE = {"eco": "economy", "rapide": "speed", "equilibre": "balanced"}
ROUTE_HELP = {
    "local": "modèles de l'Ollama de ce PC, gratuits",
    "eco": "par Lezard IA, mode éco : le moins cher capable (local d'abord, cloud seulement s'il le faut)",
    "rapide": "par Lezard IA, mode rapide : le plus rapide capable (souvent un modèle cloud, qui peut être payant)",
    "equilibre": "par Lezard IA, mode équilibré : compromis entre le prix et la vitesse",
}
ROUTE: dict[str, str | None] = {"mode": None}  # choix de CETTE session ; None : le défaut vaut


def mode_file() -> Path:
    return Path(os.environ.get("LEZARD_IA_MODE") or Path.home() / ".lezard-ia" / "mode.json")


def saved_mode() -> dict:
    """Ce que valent les nouvelles sessions : {"actif": bool, "routage": "local" | "eco" | …}."""
    try:
        data = json.loads(mode_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def link_file() -> Path:
    return Path(os.environ.get("LEZARD_IA_LIEN") or Path.home() / ".lezard-ia" / "orchestre.json")


def link() -> tuple[str, str] | None:
    """(adresse, clé) de la Lezard IA de ce PC : variables LEZARD_IA_URL et LEZARD_IA_KEY, sinon le fichier écrit
    par sa marketplace à l'installation de l'interrupteur (clé dédiée, sans droits d'administration)."""
    url, key = (os.environ.get("LEZARD_IA_URL") or "").strip(), (os.environ.get("LEZARD_IA_KEY") or "").strip()
    if not (url and key):
        try:
            data = json.loads(link_file().read_text(encoding="utf-8"))
            url, key = str(data.get("url") or "").strip(), str(data.get("key") or "").strip()
        except (OSError, ValueError, AttributeError):
            return None
    return (url.rstrip("/").removesuffix("/v1"), key) if url and key else None


def route() -> str:
    """Routage des étapes : celui de la session (/lezard eco…), sinon ORCHESTRE_MODE, sinon le défaut gardé,
    sinon « local »."""
    wanted = ROUTE["mode"] or (os.environ.get("ORCHESTRE_MODE") or "").strip().lower() or saved_mode().get("routage")
    return wanted if wanted in ROUTES else "local"


LOCAL_ONLY = {"on": False}


def lezard() -> tuple[str, str] | None:
    """(adresse, clé) de Lezard IA si les étapes passent par elle : routage éco, rapide ou équilibré, ou bien les
    deux variables d'environnement données à la main (comportement d'origine). En « local » : Ollama, direct."""
    if LOCAL_ONLY[
        "on"
    ]:  # outil à trois modes : sa part « locale » se fait sur l'Ollama de ce PC, quel que soit le routage
        return None
    by_hand = (os.environ.get("LEZARD_IA_URL") or "").strip() and (os.environ.get("LEZARD_IA_KEY") or "").strip()
    return link() if by_hand or route() != "local" else None


class Slow(RuntimeError):
    """Le modèle n'a pas fini dans le délai : la connexion est fermée, ce qui arrête son calcul."""


def step_timeout() -> float:
    """Délai d'une étape (ORCHESTRE_DELAI, secondes) : 25 minutes, moins que les 30 après lesquelles Claude Code
    abandonne un outil sans rien dire au serveur, qui continuerait à occuper la carte graphique."""
    try:
        return max(30.0, float(os.environ.get("ORCHESTRE_DELAI") or 1500))
    except ValueError:
        return 1500.0


def call(url: str, body: dict | None = None, timeout: float = 0, key: str = "") -> dict:
    timeout = timeout or step_timeout()
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})}
    req = urllib.request.Request(url, data=data, headers=headers)  # noqa: S310 (adresse réglée par l'utilisateur)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"refus du serveur de modèles ({e.code}) : {detail}") from None
    except (urllib.error.URLError, OSError) as e:
        if isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError):
            raise Slow(
                f"pas de réponse en {int(timeout / 60)} min : trop lent pour cette étape. Prends un modèle plus "
                "léger ou déjà chargé, ou une plage de lignes plus courte."
            ) from None
        raise RuntimeError(f"le serveur de modèles ne répond pas à {url} ({e}). Est-il lancé ?") from None


def models() -> list[dict]:
    """Modèles qui écrivent du texte. Ollama : du plus léger au plus lourd, avec ceux déjà chargés. Lezard IA :
    dans l'ordre où elle les liste."""
    via = lezard()
    if via:
        data = call(f"{via[0]}/v1/models", timeout=20, key=via[1]).get("data") or []
        found = []
        for m in data:
            name, meta = m.get("id") or "", m.get("metadata") or {}
            if not _writes_text(name) or meta.get("chat") is False:
                continue
            local = meta.get("hosting") == "local"
            price = (meta.get("input_per_mtok_usd") or 0) + (meta.get("output_per_mtok_usd") or 0)
            kind = "local, gratuit" if local else "cloud, gratuit" if meta.get("free") or not price else "cloud, payant"
            found.append(
                {"name": name, "size": 0, "params": "", "loaded": False, "kind": kind}
                | {"window": int(meta.get("context_window") or 0)}
            )
        return found
    return ollama_models()


def ollama_models() -> list[dict]:
    """Modèles de l'Ollama de ce PC qui écrivent du texte, du plus léger au plus lourd."""
    tags = call(f"{host()}/api/tags", timeout=10).get("models") or []
    try:
        loaded = {m.get("name") for m in call(f"{host()}/api/ps", timeout=10).get("models") or []}
    except RuntimeError:
        loaded = set()
    found = [
        {
            "name": m["name"],
            "size": m.get("size") or 0,
            "params": (m.get("details") or {}).get("parameter_size") or "?",
            "loaded": m["name"] in loaded,
        }
        for m in tags
        if _writes_text(m.get("name") or "")
    ]
    return sorted(found, key=lambda m: m["size"])


def _writes_text(name: str) -> bool:
    return bool(name) and not any(k in name.lower() for k in EMBEDDINGS)


def default_model(found: list[dict]) -> str | None:
    """Le modèle voulu par l'utilisateur ; sinon « auto » par Lezard IA ; sinon le plus léger déjà chargé (aucun
    rechargement, la carte graphique n'a pas deux modèles à tenir), sinon le plus léger tout court."""
    wanted = (os.environ.get("ORCHESTRE_MODELE") or "").strip()
    if wanted:
        return wanted
    if lezard():
        return "auto"
    ready = [m for m in found if m["loaded"]]
    # rien de chargé : un modèle minuscule invente trop, le plus léger d'au moins 2 Go s'il y en a un
    solid = [m for m in found if m["size"] >= SOLID_BYTES]
    return (ready or solid or found)[0]["name"] if found else None


def inside(raw: str, roots: list[Path], why: str) -> Path:
    """Chemin absolu de `raw`, situé dans un dossier autorisé ; ValueError sinon."""
    if not roots:
        raise ValueError(f"{why} non autorisée : donnez un dossier en argument du serveur MCP.")
    path = Path(raw).expanduser()
    path = (path if path.is_absolute() else roots[0] / path).resolve()
    if not any(path.is_relative_to(r) for r in roots):
        raise ValueError(f"{raw} est hors des dossiers autorisés ({', '.join(map(str, roots))}).")
    return path


def read_file(raw: str, roots: list[Path]) -> tuple[str, str]:
    path = inside(raw, roots, "lecture de fichiers")
    if not path.is_file():
        raise ValueError(f"{raw} : fichier introuvable.")
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > MAX_FILE_CHARS:
        text = text[:MAX_FILE_CHARS] + f"\n[… fichier coupé : {len(text) - MAX_FILE_CHARS} caractères non lus]"
    return path.name, text


def write_like(path: Path, text: str) -> None:
    """Écrit `text` en gardant les fins de ligne du fichier déjà là : un fichier en LF ne devient pas CRLF sous
    Windows (tout le fichier apparaîtrait modifié dans Git)."""
    newline = None
    if path.is_file():
        with path.open("rb") as f:
            newline = "\r\n" if b"\r\n" in f.read(65536) else "\n"
    with path.open("w", encoding="utf-8", newline=newline) as f:
        f.write(text)


def unfenced(text: str) -> str:
    """Le contenu d'une réponse faite d'un seul bloc de code (```…```), sans ses clôtures ; sinon la réponse."""
    m = re.fullmatch(r"\s*```[^\n]*\n(.*?)\n?```\s*", text, re.DOTALL)
    return m.group(1) if m else text


CHARS_PER_TOKEN = 3.4  # mesuré sur du code Python commenté en français : 3,6 ; un peu moins, par prudence
_CONTEXT: dict[str, int] = {}


def context_of(model: str) -> int:
    """Fenêtre maximale du modèle d'après Ollama (0 : inconnue). qwen2.5-coder : 32 768, quelle que soit la
    fenêtre demandée."""
    if model not in _CONTEXT:
        try:
            info = call(f"{host()}/api/show", {"model": model}, timeout=20).get("model_info") or {}
        except RuntimeError:
            return 0
        sizes = [v for k, v in info.items() if k.endswith(".context_length") and isinstance(v, int)]
        _CONTEXT[model] = max(sizes, default=0)
    return _CONTEXT[model]


MAX_AUTO_WINDOW = 65_536  # sans réglage, la fenêtre ne grandit pas au-delà : la mémoire du PC a ses limites


def window_of(model: str, need: int = 0) -> int:
    """Fenêtre demandée à Ollama pour une demande de `need` tokens, sans dépasser ce que le modèle sait lire.
    ORCHESTRE_FENETRE réglé : cette valeur, toujours. Sinon 16 384 (rapide, tient sur la carte graphique), doublée
    tant que la demande n'y tient pas, jusqu'à 65 536."""
    real = context_of(model)
    try:
        fixed = int(os.environ.get("ORCHESTRE_FENETRE") or 0)
    except ValueError:
        fixed = 0
    if fixed > 0:
        return min(fixed, real) if real else fixed
    top = min(real, MAX_AUTO_WINDOW) if real else MAX_AUTO_WINDOW
    window = DEFAULT_WINDOW
    while window < need and window < top:
        window *= 2
    return min(window, top)


LAST: dict = {"usd": 0.0, "where": "local"}  # coût et hébergement du dernier appel, pour le bilan


def generate(model: str, messages: list[dict], max_tokens: int | None) -> tuple[str, int, int, str]:
    """(réponse, tokens lus, tokens écrits, alerte) par Lezard IA ou par Ollama."""
    via = lezard()
    if via:
        body = {"model": model, "messages": messages, **({"max_tokens": max_tokens} if max_tokens else {})}
        if model == "auto" and route() in GATEWAY_MODE:
            body["mode"] = GATEWAY_MODE[route()]
        r = call(f"{via[0]}/v1/chat/completions", body, key=via[1])
        choice, usage = (r.get("choices") or [{}])[0], r.get("usage") or {}
        cut = "réponse coupée (max_tokens atteint)." if choice.get("finish_reason") == "length" else ""
        meta = r.get("x_gateway") or {}
        used, usd = meta.get("model") or model, float(meta.get("cost_usd") or 0)
        where = "local" if meta.get("hosting") == "local" else "cloud"
        LAST.update(usd=usd, where=where, model=used)
        note = f"modèle : {used} ({where}" + (f", {usd:.4f} $)" if usd else ", gratuit)")
        answer = (choice.get("message") or {}).get("content") or ""
        return answer, usage.get("prompt_tokens") or 0, usage.get("completion_tokens") or 0, cut or note
    LAST.update(usd=0.0, where="local", model=model)
    guess = int(sum(len(m.get("content") or "") for m in messages) / CHARS_PER_TOKEN)
    window = window_of(model, guess + (max_tokens or 512))
    too_long = (
        f"demande trop longue pour {model} : environ {guess} tokens, sa fenêtre en lit {window}. Donne une plage "
        "de lignes (« fichier.py:1-800 », plusieurs étapes), ou prends un modèle à plus grande fenêtre (outil "
        "modeles)."
    )
    if guess + (max_tokens or 512) > window:  # Ollama couperait en silence : le modèle ne verrait que la fin
        return "", 0, 0, too_long
    options = {"num_ctx": window, **({"num_predict": max_tokens} if max_tokens else {})}
    # « think » à false : un modèle qui réfléchit (deepseek-r1, Qwen 3.8) passerait des minutes à raisonner sur une
    # étape simple ; un Ollama trop ancien pour ce réglage le refuse, la demande repart alors sans lui
    body = {"model": model, "messages": messages, "stream": False, "options": options}
    try:
        r = call(f"{host()}/api/chat", {**body, "think": False})
    except Slow:
        raise
    except RuntimeError as e:
        if "think" not in str(e).lower():
            raise
        r = call(f"{host()}/api/chat", body)
    seen, written = r.get("prompt_eval_count") or 0, r.get("eval_count") or 0
    alert = ""
    if seen >= window - 16 or (
        guess > window * 0.5 and seen < guess * 0.5
    ):  # coupé quand même : bien moins de tokens lus que prévu
        alert = too_long
    elif r.get("done_reason") == "length":
        alert = "réponse coupée (longueur maximale atteinte)."
    return (r.get("message") or {}).get("content") or "", seen, written, alert


STEPS: list[dict] = []  # étapes confiées depuis le lancement du serveur (une session de l'IA qui orchestre)


def _record(model: str, prompt: str, state: str, target: Path | None = None, seconds: float = 0) -> None:
    STEPS.append(
        {"model": model, "task": " ".join(prompt.split())[:80], "state": state, "file": target}
        | {"seconds": seconds, "usd": LAST["usd"] if state == "faite" else 0.0, "where": LAST["where"]}
    )


def report() -> str:
    """Ce qui a été confié, pour la vérification finale de l'IA qui orchestre."""
    if not STEPS:
        return "Aucune étape confiée dans cette session : rien à vérifier de ce côté."
    lines = []
    for i, step in enumerate(STEPS, 1):
        took = f" · {step['seconds']:.0f} s" if (step.get("seconds") or 0) >= 1 else ""
        paid = f" · {step['usd']:.4f} $ (cloud)" if step.get("usd") else ""
        line = f"{i}. [{step['state']}] {step['model']}{took}{paid} — {step['task']}"
        if step["file"] is not None:
            f = step["file"]
            line += f"\n   fichier : {f} " + (
                f"({f.stat().st_size} octets)" if f.is_file() else "(ABSENT : supprimé ou déplacé)"
            )
        lines.append(line)
    stuck = sum(1 for x in STEPS if x["state"] == "bloquée")
    files = sum(1 for x in STEPS if x["file"] is not None)
    return (
        f"{len(STEPS)} étape(s) confiée(s), {stuck} bloquée(s), {files} fichier(s) écrit(s) par un modèle moins fort, "
        f"{sum(x.get('seconds') or 0 for x in STEPS) / 60:.1f} min de calcul, "
        f"{sum(x.get('usd') or 0 for x in STEPS):.4f} $ de modèles cloud :\n"
        + "\n".join(lines)
        + "\n\nVérification finale, à faire maintenant : reprends la demande d'origine point par point ; contrôle "
        "chaque fichier ci-dessus (tests ou commande qui le prouve, sinon recherche des noms cités et lecture de "
        "quelques passages) ; assure-toi que chaque étape bloquée a été reprise ; puis dis à l'utilisateur ce qui "
        "est vérifié, comment, et ce qui ne l'est pas."
    )


def delegate(args: dict, roots: list[Path]) -> str:
    prompt = str(args.get("consigne") or "").strip()
    if not prompt:
        raise ValueError("« consigne » manquante : dites au modèle quoi faire.")
    found = models()
    model = str(args.get("modele") or "").strip() or default_model(found)
    if not model:
        raise ValueError("aucun modèle local dans Ollama : installez-en un (marketplace de Lezard IA, ou ollama pull).")
    names = {m["name"] for m in found} | {m["name"].removesuffix(":latest") for m in found}
    if model not in names and model not in ("auto", (os.environ.get("ORCHESTRE_MODELE") or "").strip()):
        raise ValueError(f"modèle « {model} » absent. Disponibles : {', '.join(m['name'] for m in found)}.")
    target = inside(str(args["ecrire_dans"]), roots, "écriture de fichiers") if args.get("ecrire_dans") else None
    parts, texts = [], []
    plain = bool(args.get("sans_numeros"))
    for raw in args.get("fichiers") or []:
        path, first, last = line_range(str(raw))
        name, text = read_file(path, roots)
        texts.append(text)  # le fichier entier : le recalage des lignes cherche partout
        if first:
            every = text.splitlines()
            if first > len(every):
                raise ValueError(f"{raw} : le fichier n'a que {len(every)} lignes.")
            text = "\n".join(every[first - 1 : last])
            name = f"{name} (lignes {first} à {min(last, len(every))} sur {len(every)})"
        shown = text if plain else numbered(text, first or 1)
        parts.append(f"<fichier nom={json.dumps(name, ensure_ascii=False)}>\n{shown}\n</fichier>")
    system = WORKER_RULES + (f" {NUMBERED_RULE}" if texts and not plain else "")
    system += f"\n\nRôle : {args['role']}" if args.get("role") else ""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join([*parts, prompt])}]
    limit = args["max_tokens"] if isinstance(args.get("max_tokens"), int) and args["max_tokens"] > 0 else None
    advice = "À toi : précise la consigne ou donne ce qui manque et relance, prends un modèle plus fort, ou fais-la."
    started = time.monotonic()
    try:
        answer, seen, written, alert = generate(model, messages, limit)
    except Slow as e:
        _record(model, prompt, "bloquée", seconds=time.monotonic() - started)
        return f"ÉTAPE BLOQUÉE : {e}\n{advice}\n[{model}]"
    took = time.monotonic() - started
    answer = answer.strip()
    if model == "auto" and LAST.get("model") not in (None, "auto"):  # le bilan nomme le modèle que Lezard IA a pris
        model = f"auto → {LAST['model']}"
    cost = f"[{model} · {seen} tokens lus, {written} écrits · {took:.0f} s]"
    if BLOCKED.match(answer) or not answer:
        reason = BLOCKED.sub("", answer, count=1).strip() or alert or "réponse vide."
        _record(model, prompt, "bloquée", seconds=took)
        return f"ÉTAPE BLOQUÉE : {reason}\n{advice}\n{cost}"
    if alert and not alert.startswith("modèle :"):
        _record(model, prompt, "bloquée", seconds=took)
        return f"ÉTAPE BLOQUÉE : {alert}\n{advice}\n{cost}" + ("" if target else f"\n\nRéponse partielle :\n{answer}")
    cost = cost if not alert else f"{cost[:-1]} · {alert}]"
    if texts and not plain:  # un numéro de ligne faux à côté d'un nom défini une seule fois : recalé sans modèle
        answer, moved = realign(answer, texts)
        cost = cost if not moved else f"{cost[:-1]} · {moved} numéro(s) de ligne recalé(s) sans modèle]"
    _record(model, prompt, "faite", target, took)
    if target is None:
        return f"{answer}\n\n{cost}"
    content = unfenced(answer)
    target.parent.mkdir(parents=True, exist_ok=True)
    write_like(target, content + ("" if content.endswith("\n") else "\n"))
    lines = content.splitlines()
    head = "\n".join(lines[:5])
    return f"Écrit dans {target} : {len(lines)} lignes, {len(content)} caractères. Début :\n{head}\n\n{cost}"


def _fit(window: int) -> int:
    """Lignes de code numérotées (55 caractères) qui tiennent dans une fenêtre, en gardant de quoi répondre."""
    return max(int((window - 1500) * CHARS_PER_TOKEN / 55 / 100) * 100, 100)


def _lines_fit(model: str) -> str:
    """« environ 2 000 lignes de code » : ce qui tient dans la fenêtre du modèle, en gardant de quoi répondre."""
    window = window_of(model, 10**9)  # la plus grande fenêtre possible pour ce modèle
    lines = int((window - 1500) * CHARS_PER_TOKEN / 55 / 100) * 100  # 55 caractères par ligne numérotée
    return f"environ {max(lines, 100)} lignes de code à la fois ({window} tokens)"


def listing() -> str:
    found = models()
    if not found:
        return "Aucun modèle disponible."
    chosen = default_model(found)
    if lezard():
        rows = []
        for m in found:
            fit = f", lit environ {_fit(m['window'])} lignes de code" if m.get("window") else ""
            rows.append(f"- {m['name']} : {m.get('kind') or 'modèle de Lezard IA'}{fit}")
        how = ROUTE_HELP[route()] if route() in GATEWAY_MODE else "par Lezard IA (« auto » : le moins cher capable)"
        return (
            f"Étapes confiées {how}.\n"
            "Nomme un modèle précis seulement pour une raison (étape bloquée, code délicat) ; un modèle « cloud, "
            "payant » coûte de l'argent à l'utilisateur.\n"
            "- auto : Lezard IA choisit le modèle de chaque étape (à préférer)\n"
            + "\n".join(rows)
            + f"\nPar défaut : {chosen}"
        )
    lines = [
        f"- {m['name']} : {m['size'] / 1e9:.1f} Go, {m['params']}, lit {_lines_fit(m['name'])}"
        + (", fait pour le code" if any(k in m["name"].lower() for k in CODE_NAMES) else "")
        + (" — déjà chargé" if m["loaded"] else "")
        for m in found
    ]
    return (
        "Modèles locaux, gratuits, du plus léger (rapide, simple) au plus lourd (lent, plus capable) :\n"
        + "\n".join(lines)
        + f"\nPar défaut : {chosen}"
    )


TOOLS = [
    {
        "name": "modeles",
        "description": "Liste les modèles moins chers à qui confier une étape (modèles locaux de ce PC, gratuits, ou "
        "modèles de Lezard IA), du plus léger au plus lourd, et celui pris par défaut. À appeler une fois.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "bilan",
        "description": "À appeler à la FIN, avant de dire que le travail est terminé : liste chaque étape confiée "
        "(modèle, état, fichier écrit) et rappelle la vérification finale à faire.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "confier_etape",
        "description": "Confie UNE étape simple à un modèle moins cher que toi : résumer ou lire de longs fichiers, "
        "extraire, trier, reformuler, traduire, documenter, écrire du code répétitif d'après un modèle clair. "
        "Économise tes tokens : passe les fichiers par « fichiers » (le serveur les lit lui-même) et fais écrire "
        "les longues réponses par « ecrire_dans » (tu ne reçois qu'un reçu). Le modèle ne voit rien de ta "
        "conversation : la consigne doit être complète. S'il ne sait pas faire, la réponse commence par « ÉTAPE "
        "BLOQUÉE » avec la raison : aide-le, change de modèle, ou fais l'étape toi-même. Vérifie ce qui compte.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "consigne": {
                    "type": "string",
                    "description": "Objectif, format de sortie exact, contraintes : tout le contexte utile.",
                },
                "modele": {
                    "type": "string",
                    "description": "Nom exact d'un modèle de l'outil modeles ; sinon celui par défaut.",
                },
                "fichiers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Fichiers texte à lui faire lire (chemins dans le projet). Pour un fichier trop "
                    "long pour la fenêtre du modèle, une plage de lignes : « src/a.py:1-800 ».",
                },
                "ecrire_dans": {
                    "type": "string",
                    "description": "Fichier du projet où écrire sa réponse (remplacé s'il existe) au lieu de te la rendre.",
                },
                "role": {"type": "string", "description": "Rôle ou style à lui donner, facultatif."},
                "sans_numeros": {
                    "type": "boolean",
                    "description": "Les fichiers lui arrivent avec un numéro devant chaque ligne (il peut ainsi "
                    "citer la bonne ligne). true pour les donner tels quels, par exemple s'il doit recopier "
                    "tout un fichier.",
                },
                "max_tokens": {"type": "integer", "description": "Longueur maximale de sa réponse, facultatif."},
            },
            "required": ["consigne"],
        },
    },
]


# ---------- mémoire des résumés (serveur « resumes ») ----------
CACHE_VERSION = "2"  # à changer si la forme des résumés change : les anciens ne sont plus repris
MAX_GROUP = 40  # fichiers résumés en un appel, au plus
FOLDER_BUDGET = 1200  # secondes : au-delà, le reste du dossier attend l'appel suivant
STORED = re.compile(r"[0-9a-f]{32}\.md")  # nos résumés, et rien d'autre dans le dossier de mémoire
SUMMARY_ASK = (
    "Résume ce fichier pour une IA qui doit travailler dessus sans le lire : son rôle en deux lignes, puis ses "
    "éléments principaux (fonctions, classes, constantes, sections) au format « `nom` (ligne N) : ce qu'il fait », "
    "puis comment on l'utilise ou le lance. Les lignes sont numérotées. N'écris que ce qui est dans le fichier."
)


def cache_dir() -> Path:
    return Path(os.environ.get("ORCHESTRE_MEMOIRE") or Path.home() / ".lezard-ia" / "resumes")


def numbered(text: str, first: int = 1) -> str:
    return "\n".join(f"{i}\t{line}" for i, line in enumerate(text.splitlines(), first))


def line_range(raw: str) -> tuple[str, int, int]:
    """« src/a.py:120-480 » → (« src/a.py », 120, 480) ; sans plage : (chemin, 0, 0)."""
    m = re.fullmatch(r"(.+?):(\d+)-(\d+)", raw.strip())
    if not m or int(m.group(2)) < 1 or int(m.group(3)) < int(m.group(2)):
        return raw, 0, 0
    return m.group(1), int(m.group(2)), int(m.group(3))


def pick_model(wanted: str) -> str:
    found = models()
    model = wanted.strip() or default_model(found)
    if not model:
        raise ValueError("aucun modèle local dans Ollama : installez-en un (marketplace de Lezard IA, ou ollama pull).")
    names = {m["name"] for m in found} | {m["name"].removesuffix(":latest") for m in found}
    if model not in names and model not in ("auto", (os.environ.get("ORCHESTRE_MODELE") or "").strip()):
        raise ValueError(f"modèle « {model} » absent. Disponibles : {', '.join(m['name'] for m in found)}.")
    return model


def worker(model: str, content: str) -> tuple[str, str]:
    """(réponse, coût) d'un modèle moins cher ; RuntimeError s'il bloque, se tait ou déborde."""
    messages = [{"role": "system", "content": WORKER_RULES}, {"role": "user", "content": content}]
    answer, seen, written, alert = generate(model, messages, None)
    answer = answer.strip()
    if BLOCKED.match(answer) or not answer:
        raise RuntimeError(f"{model} bloque : {BLOCKED.sub('', answer, count=1).strip() or alert or 'réponse vide.'}")
    if alert and not alert.startswith("modèle :"):
        raise RuntimeError(f"{model} : {alert}")
    return answer, f"{model}, {seen} tokens lus, {written} écrits"


def chunks(lines: list[str], model: str, reserved: int) -> list[tuple[int, int]]:
    """Plages de lignes (première, dernière) qui tiennent chacune dans la fenêtre du modèle ; une seule si tout le
    fichier tient. Par Lezard IA, une seule : elle connaît ses modèles et découpe elle-même."""
    if not lines or lezard():
        return [(1, max(len(lines), 1))]
    budget = (window_of(model, 10**9) - 2500) * CHARS_PER_TOKEN - reserved - len(WORKER_RULES)
    budget = max(budget, 4000)
    out, first, size = [], 1, 0
    for i, line in enumerate(lines, 1):
        cost = len(line) + 8  # le numéro, la tabulation, le saut de ligne
        if size + cost > budget and i > first:
            out.append((first, i - 1))
            first, size = i, 0
        size += cost
    out.append((first, len(lines)))
    return out


def summarize(path: Path, question: str, wanted: str) -> tuple[str, bool, str]:
    """(résumé, repris de la mémoire, détail). La mémoire est rangée par empreinte du contenu et de la question :
    un fichier inchangé n'est jamais relu, un fichier modifié est résumé de nouveau."""
    data = path.read_bytes()
    ask = question.strip() or SUMMARY_ASK
    key = hashlib.sha256(b"\0".join((CACHE_VERSION.encode(), ask.encode(), data))).hexdigest()[:32]
    stored = cache_dir() / f"{key}.md"
    if stored.is_file():
        return stored.read_text(encoding="utf-8"), True, "repris de la mémoire, 0 token"
    text = data.decode("utf-8", errors="replace")
    if len(text) > MAX_FILE_CHARS:
        text = text[:MAX_FILE_CHARS] + "\n[… fichier coupé ici]"
    model = pick_model(wanted)
    every = text.splitlines()
    pieces, costs = [], []
    for first, last in chunks(every, model, len(ask)):
        whole = first == 1 and last == len(every)
        label = path.name if whole else f"{path.name} (lignes {first} à {last} sur {len(every)})"
        extra = "" if whole else " Ceci est un extrait du fichier : décris seulement ce que tu y vois."
        body = numbered("\n".join(every[first - 1 : last]), first)
        name = json.dumps(label, ensure_ascii=False)
        part, cost = worker(model, f"<fichier nom={name}>\n{body}\n</fichier>\n\n{ask}{extra}")
        pieces.append(part if whole else f"### Lignes {first} à {last}\n{part}")
        costs.append(cost)
    answer = "\n\n".join(pieces)
    answer, _ = realign(answer, [text])  # numéros de ligne faux recalés sans modèle, avant de garder en mémoire
    cost = costs[0] if len(costs) == 1 else f"{model}, en {len(costs)} morceaux (fichier plus long que sa fenêtre)"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_text(answer, encoding="utf-8")
    return answer, False, f"calculé par {cost}, gardé en mémoire"


def summarize_file(args: dict, roots: list[Path]) -> str:
    raw = str(args.get("fichier") or "").strip()
    if not raw:
        raise ValueError("« fichier » manquant.")
    path = inside(raw, roots, "lecture de fichiers")
    if not path.is_file():
        raise ValueError(f"{raw} : fichier introuvable.")
    text, _, detail = summarize(path, str(args.get("question") or ""), str(args.get("modele") or ""))
    return f"{text}\n\n[{path.name} · {detail}]"


def summarize_folder(args: dict, roots: list[Path]) -> str:
    pattern = str(args.get("motif") or "").strip()
    if not pattern:
        raise ValueError("« motif » manquant (exemple : src/**/*.py).")
    if not roots:
        raise ValueError("lecture de fichiers non autorisée : donnez un dossier en argument du serveur MCP.")
    if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
        raise ValueError("« motif » : un chemin relatif au projet, sans « .. ».")
    files = sorted(f for f in roots[0].glob(pattern) if f.is_file())
    if not files:
        raise ValueError(f"aucun fichier ne correspond à {pattern}.")
    if len(files) > MAX_GROUP:
        raise ValueError(f"{len(files)} fichiers pour {pattern} : {MAX_GROUP} au plus par appel, resserrez le motif.")
    parts, reused, failed, late = [], 0, 0, 0
    started = time.monotonic()
    for f in files:
        rel = f.relative_to(roots[0]).as_posix()
        if time.monotonic() - started > FOLDER_BUDGET:  # rendre la main avant que le client n'abandonne l'appel
            late += 1
            parts.append(f"## {rel}\nPAS ENCORE RÉSUMÉ : relance le même appel, ce qui est fait reste en mémoire.")
            continue
        try:
            text, cached, detail = summarize(f, str(args.get("question") or ""), str(args.get("modele") or ""))
            reused += cached
            parts.append(f"## {rel}\n{text}\n[{detail}]")
        except (RuntimeError, OSError) as e:
            failed += 1
            parts.append(f"## {rel}\nRÉSUMÉ IMPOSSIBLE : {e}")
    done = len(files) - failed - reused - late
    foot = f"{len(files)} fichier(s) : {reused} repris de la mémoire, {done} résumé(s) maintenant, {failed} en échec."
    if late:
        foot += f" {late} pas encore résumé(s), faute de temps : relance le même appel."
    return "\n\n".join(parts) + f"\n\n{foot}"


def forget(args: dict, roots: list[Path]) -> str:
    folder = cache_dir()
    # seulement nos fichiers : ORCHESTRE_MEMOIRE peut désigner un dossier où l'utilisateur garde d'autres .md
    stored = [f for f in folder.glob("*.md") if STORED.fullmatch(f.name)] if folder.is_dir() else []
    for f in stored:
        f.unlink()
    return f"{len(stored)} résumé(s) oublié(s)."


TOOLS_RESUMES = [
    {
        "name": "resumer_fichier",
        "description": "Rend le résumé d'un fichier du projet SANS que tu le lises : rôle, éléments principaux avec "
        "leur numéro de ligne, utilisation. Écrit par un modèle local gratuit, puis gardé en mémoire : tant que le "
        "fichier ne change pas, le résumé revient tout de suite et ne coûte rien, même dans une autre session. À "
        "préférer à la lecture d'un gros fichier ; lis ensuite seulement les lignes dont tu as besoin.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "fichier": {"type": "string", "description": "Chemin du fichier dans le projet."},
                "question": {
                    "type": "string",
                    "description": "Ce que tu veux en savoir, à la place du résumé général (gardé en mémoire à part).",
                },
                "modele": {"type": "string", "description": "Modèle local à utiliser ; sinon le plus léger."},
            },
            "required": ["fichier"],
        },
    },
    {
        "name": "resumer_dossier",
        "description": "Les résumés de plusieurs fichiers en UN appel (40 au plus), par un motif comme src/**/*.py : "
        "repris de la mémoire quand le fichier n'a pas changé, calculés par un modèle local sinon. Pour découvrir "
        "un projet sans le lire.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "motif": {"type": "string", "description": "Motif relatif au projet, par exemple src/**/*.py."},
                "question": {"type": "string", "description": "Question posée sur chaque fichier, facultatif."},
                "modele": {"type": "string", "description": "Modèle local à utiliser ; sinon le plus léger."},
            },
            "required": ["motif"],
        },
    },
    {
        "name": "oublier_resumes",
        "description": "Efface tous les résumés gardés en mémoire (ils seront recalculés à la demande).",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# ---------- atelier de brouillons (serveur « brouillons ») ----------
CITED = re.compile(r"`([^`\n]{2,80})`(?:\s*\(\s*(?:lignes?\s+)?(\d+)\s*[-–)])?")
NAME = re.compile(r"[\w./\\-]+")


def _cited_in(name: str, text: str, near: bool = False) -> bool:
    """« Runs.start » n'est écrit nulle part tel quel : la méthode `start` est dans la classe `Runs`. Un nom à points
    existe si chacun de ses morceaux existe ; près d'une ligne citée, seul le dernier morceau y est."""
    if name in text:
        return True
    parts = [x for x in name.split(".") if x]
    if len(parts) < 2:
        return False
    return parts[-1] in text if near else all(x in text for x in parts)


# une définition : Python, JavaScript et TypeScript, Go, Rust, Java et C#, puis une affectation « nom = »
DEFINES = (
    r"^\s*(?:(?:export|default|public|private|protected|static|abstract|final|pub|async)\s+)*"
    r"(?:def|class|function\*?|func|fn|const|let|var|type|interface|struct|enum|trait|impl|module)\s+{0}\b"
    r"|^\s*(?:func\s+\([^)]*\)\s*){0}\s*\("
    r"|^\s*{0}\s*(?::[^=\n]+)?=(?!=)"
)


def defined_at(name: str, lines: list[list[str]]) -> list[int]:
    """Lignes où le nom est défini (def, class, function, affectation…), toutes sources confondues."""
    pattern = re.compile(DEFINES.format(re.escape(name.rsplit(".", 1)[-1])))
    return [i for ls in lines for i, line in enumerate(ls, 1) if pattern.search(line)]


def _on_line(name: str, n: int, lines: list[list[str]]) -> bool:
    leaf = name.rsplit(".", 1)[-1]
    return any(0 < n <= len(ls) and leaf in ls[n - 1] for ls in lines)


def line_ok(name: str, n: int, lines: list[list[str]]) -> bool:
    """La ligne citée est-elle la bonne ? Nom défini une seule fois : sa ligne exacte (une IA qui lit « ligne 2199 »
    ira à cette ligne). Défini plusieurs fois, ou seulement utilisé : écrit à deux lignes près de la ligne citée."""
    hits = defined_at(name, lines)
    if len(hits) == 1:  # ou le nom écrit sur la ligne citée elle-même (son import dans un autre fichier)
        return n == hits[0] or _on_line(name, n, lines)
    return any(_cited_in(name, "\n".join(ls[max(0, n - 3) : n + 2]), near=True) for ls in lines)


def realign(draft: str, texts: list[str]) -> tuple[str, int]:
    """Sans modèle : un numéro de ligne cité qui n'est pas celle où le nom est défini (def, class, affectation…) est
    remplacé par cette ligne, si elle est unique dans les sources. Rend (texte, numéros recalés).
    Un nom absent ou défini plusieurs fois n'est pas touché : `controler` le signalera."""
    lines = [t.splitlines() for t in texts]
    moved = 0

    def fix(m: re.Match) -> str:
        nonlocal moved
        whole = m.group(0)
        core = NAME.match(m.group(1).strip())
        if not m.group(2) or not core or not whole.endswith(")"):  # pas de ligne, ou une plage « 47-52 »
            return whole
        name, n = core.group().rstrip("."), int(m.group(2))
        hits = defined_at(name, lines)
        if len(hits) != 1 or hits[0] == n or _on_line(name, n, lines):
            return whole
        moved += 1
        return whole[: m.start(2) - m.start()] + str(hits[0]) + whole[m.end(2) - m.start() :]

    return CITED.sub(fix, draft), moved


def check_draft(args: dict, roots: list[Path]) -> str:
    """Sans aucun modèle : chaque nom écrit entre accents graves dans le brouillon existe-t-il dans les sources, et
    à la ligne citée (à deux lignes près) ?"""
    raw = str(args.get("brouillon") or "").strip()
    sources = [str(x) for x in args.get("sources") or []]
    if not raw or not sources:
        raise ValueError("« brouillon » et « sources » sont requis.")
    draft = read_file(raw, roots)[1]
    texts = [inside(x, roots, "lecture de fichiers").read_text(encoding="utf-8", errors="replace") for x in sources]
    moved = 0
    if args.get("recaler"):
        draft, moved = realign(draft, texts)
        if moved:
            write_like(inside(raw, roots, "écriture de fichiers"), draft)
    lines = [t.splitlines() for t in texts]
    known = {Path(x).name for x in sources} | {Path(x).as_posix() for x in sources}  # les sources elles-mêmes
    names, missing, wrong_line, checked_lines = set(), [], [], 0
    for m in CITED.finditer(draft):
        core = NAME.match(m.group(1).strip())
        if not core or len(core.group()) < 2:
            continue
        name = core.group().rstrip(".")
        if name not in names:
            names.add(name)
            if name not in known and not any(_cited_in(name, t) for t in texts):
                missing.append(name)
                continue
        if m.group(2) and name not in missing:
            checked_lines += 1
            n = int(m.group(2))
            if not line_ok(name, n, lines):
                at = defined_at(name, lines)
                wrong_line.append(f"{name} (ligne {n}" + (f", défini ligne {at[0]})" if len(at) == 1 else ")"))
    head = f"{len(names)} nom(s) cité(s), {checked_lines} ligne(s) citée(s) contrôlée(s)."
    if moved:
        head += f" {moved} numéro(s) de ligne recalé(s) dans le fichier, sans modèle."
    if not missing and not wrong_line:
        return f"{head} Tout existe dans les sources. Ce contrôle ne dit rien de la justesse des descriptions."
    out = [head]
    if missing:
        out.append(f"ABSENTS des sources ({len(missing)}) : " + ", ".join(missing[:40]))
    if wrong_line:
        out.append(f"PAS à la ligne citée ({len(wrong_line)}) : " + ", ".join(wrong_line[:40]))
    out.append("À corriger : outil retoucher, avec ces noms dans « corrections ».")
    return "\n".join(out)


def revise(args: dict, roots: list[Path]) -> str:
    raw = str(args.get("fichier") or "").strip()
    fixes = str(args.get("corrections") or "").strip()
    if not raw or not fixes:
        raise ValueError("« fichier » et « corrections » sont requis.")
    target = inside(raw, roots, "écriture de fichiers")
    if not target.is_file():
        raise ValueError(f"{raw} : fichier introuvable.")
    parts = []
    for src in args.get("sources") or []:
        name, text = read_file(str(src), roots)
        parts.append(f"<source nom={json.dumps(name, ensure_ascii=False)}>\n{numbered(text)}\n</source>")
    draft = target.read_text(encoding="utf-8", errors="replace")
    parts.append(f"<document>\n{draft}\n</document>")
    parts.append(
        f"Corrections demandées :\n{fixes}\n\nRends le document complet, corrigé, et rien d'autre. Ne change que "
        "ce que les corrections demandent ; garde le reste mot pour mot."
    )
    model = pick_model(str(args.get("modele") or ""))
    try:
        answer, cost = worker(model, "\n\n".join(parts))
    except RuntimeError as e:
        _record(model, f"retouche de {target.name}", "bloquée")
        return f"ÉTAPE BLOQUÉE : {e}\nLe fichier n'a pas été modifié. À toi : précise les corrections, ou fais-les."
    content = unfenced(answer)
    before, after = len(draft.splitlines()), len(content.splitlines())
    if before >= 10 and after < before * 0.6:  # un petit modèle rend parfois un résumé au lieu du document
        _record(model, f"retouche de {target.name}", "bloquée")
        return (
            f"ÉTAPE BLOQUÉE : {model} a rendu {after} lignes au lieu de {before}, le document serait tronqué.\n"
            "Le fichier n'a pas été modifié. À toi : fais ces corrections, ou prends un modèle plus fort."
        )
    write_like(target, content + ("" if content.endswith("\n") else "\n"))
    _record(model, f"retouche de {target.name}", "faite", target)
    return f"{target.name} retouché : {before} lignes avant, {after} après.\n[{cost}]"


TOOLS_BROUILLONS = [
    {
        "name": "controler",
        "description": "Contrôle un document écrit par un modèle moins fort, SANS modèle et sans que tu le lises : "
        "chaque nom écrit entre accents graves (fonction, fichier, option) existe-t-il dans les fichiers sources, "
        "et à la ligne citée ? Rend seulement les écarts. Ne juge pas le sens des phrases.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "brouillon": {"type": "string", "description": "Le document à contrôler (chemin dans le projet)."},
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Les fichiers dont le document parle.",
                },
                "recaler": {
                    "type": "boolean",
                    "description": "true : corrige d'abord dans le document, sans modèle, chaque numéro de ligne "
                    "faux dont le nom est défini une seule fois dans les sources. À mettre presque toujours.",
                },
            },
            "required": ["brouillon", "sources"],
        },
    },
    {
        "name": "retoucher",
        "description": "Fait corriger un document par un modèle local gratuit au lieu de le réécrire toi-même (ce "
        "que tu écris coûte bien plus cher que ce que tu lis) : donne le fichier et tes corrections en quelques "
        "lignes. Le fichier est remplacé ; seul un reçu te revient.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "fichier": {"type": "string", "description": "Le document à corriger (chemin dans le projet)."},
                "corrections": {"type": "string", "description": "Ce qu'il faut changer, précisément et brièvement."},
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Fichiers à lui faire relire pour corriger juste, facultatif.",
                },
                "modele": {"type": "string", "description": "Modèle local à utiliser ; sinon le plus léger."},
            },
            "required": ["fichier", "corrections"],
        },
    },
]


# ---------- assistants Claude moins chers (fichiers d'agents de Claude Code) ----------
AGENT_REPORT = (
    "Tu exécutes UNE étape précise confiée par l'agent principal. Fais exactement ce qui est demandé, rien de plus. "
    "Ne relis pas tout le projet : seulement les fichiers nommés et ce qu'il faut pour l'étape. Termine par un "
    "compte rendu de cinq lignes au plus : fichiers modifiés, ce que tu as vérifié et comment, ce qui reste. Si tu "
    "ne peux pas réussir l'étape, n'improvise pas : réponds « BLOQUE: » suivi de la raison précise."
)
AGENTS = {
    "etape-moyenne": (
        "sonnet",
        "Use for a well-specified sub-task that needs real coding ability but not the strongest model: implement a "
        "function from a clear spec, write tests for existing code, refactor one file, fix a bug whose cause is "
        "known. Give it a complete, self-contained brief (files, expected result, how to verify).",
    ),
    "etape-simple": (
        "haiku",
        "Use for a simple, mechanical sub-task: rename across files, apply the same small edit in many places, "
        "format or reorder, collect information with search tools, write a short summary of given files. Give it a "
        "complete, self-contained brief.",
    ),
}


def agents_folder() -> Path:
    return Path.home() / ".claude" / "agents"


def install_agents() -> int:
    folder = agents_folder()
    folder.mkdir(parents=True, exist_ok=True)
    for name, (model, description) in AGENTS.items():
        text = f"---\nname: {name}\ndescription: {description}\nmodel: {model}\n---\n\n{AGENT_REPORT}\n"
        (folder / f"{name}.md").write_text(text, encoding="utf-8")
        print(f"Assistant écrit : {folder / (name + '.md')} (modèle {model})")
    return 0


def remove_agents() -> int:
    for name in AGENTS:
        f = agents_folder() / f"{name}.md"
        if f.exists():
            f.unlink()
            print(f"Assistant retiré : {f}")
    print("Terminé.")
    return 0


# ---------- Claude Code allégé (variable d'environnement de l'utilisateur, Windows) ----------
ENV_ALLOWED = ("ENABLE_CLAUDEAI_MCP_SERVERS",)  # connecteurs du compte claude.ai : coupés avec « false »


def user_env(name: str, value: str | None) -> int:
    """Écrit (ou retire, value=None) une variable d'environnement de l'utilisateur ; les programmes ouverts ensuite
    la voient. Seules les variables connues sont acceptées."""
    if name not in ENV_ALLOWED:
        print(f"variable refusée : {name}")
        return 2
    if sys.platform != "win32":
        print(f"Hors de Windows : ajoutez ou retirez « export {name}=… » dans le fichier de votre shell.")
        return 0
    import ctypes
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        if value is None:
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                print("Déjà retirée.")
                return 0
        else:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    result = ctypes.c_ulong()  # prévenir Windows : les programmes ouverts ensuite voient le changement
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result))
    print(f"{name} retirée." if value is None else f"{name}={value} enregistrée pour votre compte Windows.")
    print("Fermez puis rouvrez VS Code et vos terminaux pour que Claude Code en tienne compte.")
    return 0


def _no_roots(function):
    return lambda args, roots: function()


# ---------- lot d'étapes : plusieurs étapes, leur contrôle et l'assemblage en UN appel ----------
MAX_LOT = 12  # étapes par lot, au plus
LOT_BUDGET = 1200  # secondes : au-delà, les étapes restantes attendent l'appel suivant


def _assemble(raw: str, parts: list[str], written: set[Path], roots: list[Path]) -> str:
    """Met bout à bout les parties, dans l'ordre, dans un seul fichier ; les parties écrites par ce lot sont ensuite
    supprimées (elles n'étaient que des morceaux), les autres restent."""
    target = inside(raw, roots, "écriture de fichiers")
    paths = [inside(p, roots, "lecture de fichiers") for p in parts]
    absent = [p.name for p in paths if not p.is_file()]
    if absent:
        return f"NON ASSEMBLÉ : partie(s) absente(s) : {', '.join(absent)}."
    text = "\n\n".join(p.read_text(encoding="utf-8", errors="replace").strip("\n") for p in paths) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    write_like(target, text)
    for p in paths:
        if p in written and p != target:
            p.unlink(missing_ok=True)
    return f"Assemblé dans {target} : {len(paths)} partie(s), {len(text.splitlines())} lignes."


def delegate_batch(args: dict, roots: list[Path]) -> str:
    """Chaque appel de l'IA qui orchestre relit toute sa conversation : huit appels pour quatre fichiers coûtent bien
    plus que le travail lui-même. Ici, un seul appel confie les étapes, contrôle ce qu'elles ont écrit et assemble."""
    steps = args.get("etapes") or []
    if not isinstance(steps, list) or any(not isinstance(s, dict) for s in steps):
        raise ValueError("« etapes » : une liste d'étapes (mêmes champs que confier_etape).")
    if len(steps) > MAX_LOT:
        raise ValueError(f"{len(steps)} étapes : {MAX_LOT} au plus par lot, fais-en deux.")
    whole = str(args.get("assembler_dans") or "").strip()
    parts = [str(p) for p in args.get("parties") or []] or [
        str(s["ecrire_dans"]) for s in steps if s.get("ecrire_dans")
    ]
    if not steps and not (whole and parts):
        raise ValueError("« etapes » manquantes (ou « assembler_dans » avec « parties » pour assembler seulement).")
    common = str(args.get("consigne_commune") or "").strip()
    started, out, written, undone = time.monotonic(), [], set(), 0
    for i, step in enumerate(steps, 1):
        if time.monotonic() - started > LOT_BUDGET:
            undone += len(steps) - i + 1
            out.append(f"{i} à {len(steps)}. PAS FAITES : temps du lot dépassé. Relance un lot avec ces étapes.")
            break
        merged = {k: v for k, v in step.items() if k != "controler"}
        merged["consigne"] = "\n\n".join(x for x in (common, str(step.get("consigne") or "").strip()) if x)
        if args.get("modele") and not merged.get("modele"):
            merged["modele"] = args["modele"]
        try:
            result = delegate(merged, roots)
        except (ValueError, RuntimeError, OSError) as e:
            undone += 1
            out.append(f"{i}. ERREUR : {e}")
            continue
        done = not result.startswith("ÉTAPE BLOQUÉE")
        undone += 0 if done else 1
        target, sources = step.get("ecrire_dans"), [line_range(str(f))[0] for f in step.get("fichiers") or []]
        if done and target:
            written.add(inside(str(target), roots, "écriture de fichiers"))
            result = result.split(" Début :\n", 1)[0] + " " + result.rsplit("\n\n", 1)[-1]  # reçu sans l'extrait
            if sources and step.get("controler", True) and not step.get("sans_numeros"):
                check = check_draft({"brouillon": str(target), "sources": sources, "recaler": True}, roots)
                result += "\n   Contrôle : " + check.replace("\n", "\n   ")
        out.append(f"{i}. {result}")
    if whole:
        out.append(
            "NON ASSEMBLÉ : une étape au moins n'est pas faite. Reprends-la, puis appelle confier_lot avec "
            "« assembler_dans » et « parties » seulement."
            if undone
            else _assemble(whole, parts, written, roots)
        )
    return "\n".join(out)


TOOLS.append(
    {
        "name": "confier_lot",
        "description": "Confie PLUSIEURS étapes en UN seul appel, à préférer à plusieurs confier_etape dès qu'il y a "
        "deux étapes indépendantes ou plus (un fichier chacune, par exemple) : chaque appel que tu fais te fait "
        "relire toute la conversation, c'est ce qui coûte le plus. Les étapes s'exécutent l'une après l'autre ; ce "
        "que chacune écrit est contrôlé aussitôt sans modèle (noms cités présents dans ses fichiers, numéros de "
        "ligne recalés), puis tout peut être assemblé dans un seul fichier. Tu ne reçois qu'un reçu par étape, "
        "avec les écarts à corriger.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "etapes": {
                    "type": "array",
                    "description": f"Les étapes, {MAX_LOT} au plus.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "consigne": {"type": "string", "description": "Ce qui est propre à cette étape."},
                            "fichiers": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Fichiers à lui faire lire (plage possible : « a.py:1-800 »).",
                            },
                            "ecrire_dans": {"type": "string", "description": "Fichier où écrire sa réponse."},
                            "modele": {"type": "string"},
                            "role": {"type": "string"},
                            "sans_numeros": {"type": "boolean"},
                            "max_tokens": {"type": "integer"},
                            "controler": {
                                "type": "boolean",
                                "description": "false pour ne pas contrôler ce que l'étape écrit (du code, par exemple).",
                            },
                        },
                    },
                },
                "consigne_commune": {
                    "type": "string",
                    "description": "Ce qui vaut pour toutes les étapes (objectif, format exact, contraintes) : écrit "
                    "une seule fois, ajouté devant la consigne de chacune.",
                },
                "modele": {"type": "string", "description": "Modèle de toutes les étapes qui n'en nomment pas."},
                "assembler_dans": {
                    "type": "string",
                    "description": "Fichier où mettre bout à bout, dans l'ordre, ce que les étapes ont écrit (leurs "
                    "morceaux sont ensuite supprimés). Rien n'est assemblé si une étape est bloquée.",
                },
                "parties": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Fichiers à assembler, dans l'ordre, si ce ne sont pas ceux des étapes (pour "
                    "assembler après avoir repris une étape bloquée : sans « etapes »).",
                },
            },
        },
    }
)

SERVERS = {
    "orchestre": (
        lambda: TOOLS,
        {
            "modeles": _no_roots(listing),
            "bilan": _no_roots(report),
            "confier_etape": delegate,
            "confier_lot": delegate_batch,
        },
    ),
    "resumes": (
        lambda: TOOLS_RESUMES,
        {"resumer_fichier": summarize_file, "resumer_dossier": summarize_folder, "oublier_resumes": forget},
    ),
    "brouillons": (lambda: TOOLS_BROUILLONS, {"controler": check_draft, "retoucher": revise}),
}


# ---------- interrupteur (serveur « lezard ») : tous les outils d'économie, montrés ou cachés par /lezard ----------
OUTBOX: list[dict] = []  # messages à envoyer au client après la réponse en cours (liste d'outils changée)
ACTIVE: dict[str, bool | None] = {"on": None}  # état de CETTE session ; None : pas encore choisi, le défaut vaut


def default_on() -> bool:
    """Ce que valent les nouvelles sessions (« /lezard toujours » ou « jamais ») : éteint tant que rien n'est choisi."""
    return bool(saved_mode().get("actif"))


def is_on() -> bool:
    return default_on() if ACTIVE["on"] is None else bool(ACTIVE["on"])


SWITCH_TOOL = {
    "name": "lezard_ia",
    "description": "Interrupteur des outils d'économie de tokens de Lezard IA pour cette session (commande /lezard). "
    "« activer » : les outils apparaissent (résumés de fichiers sans les lire, étapes confiées à des modèles locaux "
    "gratuits, contrôle et retouche de brouillons, bilan). « desactiver » : Claude seul, comme d'habitude. "
    "« etat » : dit ce qui est actif. N'appelle « activer » ou « desactiver » que si l'utilisateur le demande.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["activer", "desactiver", "etat"]},
            "routage": {
                "type": "string",
                "enum": list(ROUTES),
                "description": "Qui fait les étapes confiées. local : l'Ollama de ce PC, gratuit. eco : Lezard IA "
                "choisit le moins cher capable (local d'abord, cloud s'il le faut). rapide : le plus rapide "
                "(souvent du cloud, parfois payant). equilibre : entre les deux. À ne changer que si "
                "l'utilisateur le demande.",
            },
            "par_defaut": {
                "type": "boolean",
                "description": "Garder aussi ce choix (actif ou éteint, routage) pour les prochaines sessions.",
            },
        },
        "required": ["action"],
    },
}


def switch(args: dict, roots: list[Path]) -> str:
    action = str(args.get("action") or "etat")
    if action not in ("activer", "desactiver", "etat"):
        raise ValueError("« action » : activer, desactiver ou etat.")
    keep, wanted_route = dict(saved_mode()), args.get("routage")
    if wanted_route is not None:
        if wanted_route not in ROUTES:
            raise ValueError(f"« routage » : {', '.join(ROUTES)}.")
        if wanted_route != "local" and link() is None:
            raise ValueError(
                "Lezard IA n'est pas reliée à cet interrupteur : installez « Interrupteur Lezard IA » depuis la "
                "marketplace de Lezard IA (elle crée la clé), puis recommencez. En attendant : routage local."
            )
        ROUTE["mode"] = wanted_route
        keep["routage"] = wanted_route
    if action != "etat":
        before, wanted = is_on(), action == "activer"
        ACTIVE["on"] = wanted
        keep["actif"] = wanted
        if before != wanted:
            OUTBOX.append({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
    if args.get("par_defaut") is True and (action != "etat" or wanted_route is not None):
        mode_file().parent.mkdir(parents=True, exist_ok=True)
        mode_file().write_text(json.dumps(keep), encoding="utf-8")
    later = ("actifs" if default_on() else "éteints") + f", routage {saved_mode().get('routage') or 'local'}"
    if is_on():
        names = ", ".join(t["name"] for t in [*ALL_TOOLS, *mode_specs()])
        return (
            f"Lezard IA : ACTIF dans cette session. Étapes confiées : {ROUTE_HELP[route()]}. Outils : {names}. "
            "Sers-t'en dès qu'il y a de gros fichiers à lire ou du texte répétitif à produire ; fais directement "
            "les petites tâches. "
            f"Nouvelles sessions : outils {later}."
        )
    return (
        f"Lezard IA : ÉTEINT dans cette session (Claude seul, comme d'habitude). Nouvelles sessions : outils {later}."
    )


ALL_TOOLS = [*TOOLS, *TOOLS_RESUMES, *TOOLS_BROUILLONS]
# outils à trois modes (gateway/mcp_modes.py) : images, documents, web, contrôle du PC
# pc_* : appelables seulement avec ORCHESTRE_PC=1 (voir mcp_modes.pc_offered), pas proposés sinon
MODE_TOOLS = ("lire_image", "lire_document", "chercher_web", "pc_tache", "pc_voir", "pc_agir")
_MODES: list = []  # le module, chargé à la première demande ([None] : absent)


def modes():
    """gateway.mcp_modes, ou None si ce script a été copié seul (les autres outils marchent sans lui)."""
    if not _MODES:
        here = Path(__file__).resolve().parent
        # le dossier du script sort des chemins de Python (ses modules en masqueraient d'autres), son parent y entre
        sys.path[:] = [x for x in sys.path if not x or Path(x).resolve() != here]
        if str(here.parent) not in sys.path:
            sys.path.insert(0, str(here.parent))
        try:
            from gateway import mcp_modes

            _MODES.append(mcp_modes)
        except ImportError:
            _MODES.append(None)
    return _MODES[0]


def mode_specs() -> list[dict]:
    found = modes()
    return found.specs(sys.modules[__name__]) if found else []


def _mode_tool(name: str):
    def run(args: dict, roots: list[Path]):
        found = modes()
        if found is None:
            raise ValueError(f"{name} : outil indisponible ici (il vient avec Lezard IA installée).")
        return found.HANDLERS[name](sys.modules[__name__], args, roots)

    return run


def _when_on(function):
    def guarded(args: dict, roots: list[Path]) -> str:
        if not is_on():
            raise ValueError("Lezard IA est éteint dans cette session : l'utilisateur l'active avec /lezard on.")
        return function(args, roots)

    return guarded


SERVERS["lezard"] = (
    lambda: [SWITCH_TOOL, *ALL_TOOLS, *mode_specs()] if is_on() else [SWITCH_TOOL],
    {
        "lezard_ia": switch,
        **{
            name: _when_on(f)
            for server in ("orchestre", "resumes", "brouillons")
            for name, f in SERVERS[server][1].items()
        },
        **{name: _when_on(_mode_tool(name)) for name in MODE_TOOLS},
    },
)

SKILL_LEZARD = """---
name: lezard
description: Use when the user types /lezard, or asks to turn Lezard IA's token-saving tools on or off for this session ("/lezard on", "/lezard off", "active Lezard IA", "coupe Lezard IA", "Claude normal", "travaille sans Lezard IA", "/lezard toujours", "/lezard jamais"). Requires the MCP server "lezard" (tool lezard_ia).
---

# Interrupteur Lezard IA

Un intermédiaire entre toi et les modèles locaux de ce PC. Éteint : tu travailles comme d'habitude. Actif : tu
dépenses moins de tokens en confiant ce qui est simple.

| L'utilisateur tape | Tu appelles `lezard_ia` avec |
|---|---|
| `/lezard on`, « active Lezard IA » | action « activer » |
| `/lezard off`, « Claude normal » | action « desactiver » |
| `/lezard`, `/lezard etat` | action « etat » |
| `/lezard toujours` | action « activer », par_defaut true |
| `/lezard jamais` | action « desactiver », par_defaut true |
| `/lezard local` | action « activer », routage « local » : étapes confiées à l'Ollama de ce PC, gratuit |
| `/lezard eco` | action « activer », routage « eco » : Lezard IA prend le moins cher capable, local d'abord |
| `/lezard rapide` | action « activer », routage « rapide » : le plus rapide, souvent du cloud |
| `/lezard equilibre` | action « activer », routage « equilibre » |

« toujours » après un routage (`/lezard eco toujours`) : ajoute par_defaut true. Si l'outil répond que Lezard IA
n'est pas reliée, dis-le à l'utilisateur tel quel et reste en routage local.

Réponds ensuite en UNE ligne : actif ou éteint, et ce que valent les prochaines sessions.

## Quand il est actif, pour tout le reste de la session

- Gros fichier ou dossier à connaître : `resumer_fichier` ou `resumer_dossier` au lieu de lire ; lis ensuite
  seulement les lignes utiles.
- Travail en plusieurs étapes avec beaucoup à lire ou à écrire : suis la méthode « orchestre » (`confier_lot` pour
  plusieurs étapes en un seul appel, `confier_etape` pour une seule, puis `bilan` et vérification finale).
- Document produit par un modèle moins fort : `controler` avec « recaler » à true (il corrige lui-même les numéros
  de ligne faux), puis `retoucher` pour ce qui reste, plutôt que de le réécrire.
- Fichier trop long pour un modèle local (`modeles` dit combien de lignes chacun lit) : confie-le par plages,
  « fichier.py:1-800 ».
- Petite tâche : fais-la directement, ces outils n'y font rien gagner.

## Les trois modes, pour les images, les documents et le web

`lire_image`, `lire_document` et `chercher_web` suivent le routage choisi par l'utilisateur :

| Routage | Qui fait quoi | Ce qui sort du PC |
|---|---|---|
| local | le modèle local fait tout, tu relaies sa réponse | rien : ni image, ni document |
| eco | le modèle local décrit ou résume, tu décides | sa description, en texte |
| rapide | tu reçois l'image ou le texte entier, tu fais tout | la matière brute |

- Utilise ces outils à la place des tiens (lecture d'image, recherche web, lecture d'un long document) tant que
  le routage n'est pas « rapide » : c'est le choix de l'utilisateur, ne le contourne pas.
- Le contrôle du PC (souris, clavier) n'est pas disponible ici : il existe dans le chat de Lezard IA.
- En local, ne redemande pas l'image ou le document par un autre moyen : ils ne doivent pas sortir du PC.

## Quand il est éteint

N'utilise aucun de ces outils et n'en parle pas : travaille normalement.
"""
SKILLS = {"orchestre": lambda: SKILL, "lezard": lambda: SKILL_LEZARD}


def handle(msg: dict, roots: list[Path], server: str = "orchestre") -> dict | None:
    """Réponse JSON-RPC à un message, None pour une notification."""
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None
    if method == "initialize":
        result = {
            "protocolVersion": (msg.get("params") or {}).get("protocolVersion") or PROTOCOL,
            "capabilities": {"tools": {"listChanged": server == "lezard"}},
            "serverInfo": {"name": server, "version": "1.1.0"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": SERVERS[server][0]()}
    elif method == "tools/call":
        params = msg.get("params") or {}
        try:
            name, args = params.get("name"), params.get("arguments") or {}
            function = SERVERS[server][1].get(name)
            if function is None:
                raise ValueError(f"outil inconnu : {name}")
            text = function(args, roots)
            # un outil à trois modes peut rendre une image (mode rapide) : déjà une liste de contenus
            content = text if isinstance(text, list) else [{"type": "text", "text": text}]
            result = {"content": content, "isError": False}
        except (ValueError, RuntimeError, OSError) as e:
            result = {"content": [{"type": "text", "text": f"Erreur : {e}"}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"méthode inconnue : {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def skill_folder(name: str = "orchestre") -> Path:
    return Path.home() / ".claude" / "skills" / name


def install_skill(name: str = "orchestre") -> int:
    """Une méthode pour Claude Code : « orchestre » (/orchestre) ou « lezard » (/lezard on, off)."""
    if name not in SKILLS:
        print(f"méthode inconnue : {name}")
        return 2
    folder = skill_folder(name)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(SKILLS[name](), encoding="utf-8")
    print(f"Méthode écrite : {folder / 'SKILL.md'}")
    return 0


def remove_skill(name: str = "orchestre") -> int:
    if name not in SKILLS:
        print(f"méthode inconnue : {name}")
        return 2
    if name == "lezard" and link_file().is_file():  # la clé dédiée écrite par la marketplace ne sert plus
        link_file().unlink()
    folder = skill_folder(name)
    if folder.exists():
        shutil.rmtree(folder)
        print(f"Dossier supprimé : {folder}")
    else:
        print("Déjà retiré.")
    return 0


def refresh_skills() -> int:
    """Les méthodes déjà installées dans Claude Code suivent la version de Lezard IA : après une mise à jour, le
    serveur est neuf mais le fichier SKILL.md écrit à l'installation ne l'était pas. Seules celles qui existent
    sont réécrites ; rien n'est installé ici. Rend le nombre de méthodes mises à jour."""
    done = 0
    switch_installed = (skill_folder("lezard") / "SKILL.md").is_file()
    for name, text in SKILLS.items():
        f = skill_folder(name) / "SKILL.md"
        try:
            if name == "orchestre" and switch_installed and not f.is_file():
                # l'interrupteur suit cette méthode ; désinstaller la fiche « Chef d'orchestre » l'avait retirée
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(text(), encoding="utf-8")
                done += 1
            elif f.is_file() and f.read_text(encoding="utf-8") != text():
                f.write_text(text(), encoding="utf-8")
                done += 1
        except OSError:
            pass  # dossier protégé : l'ancienne méthode reste, le serveur démarre quand même
    return done


def serve(argv: list[str], server: str = "orchestre") -> int:
    # « . » : le dossier du projet. Claude Code lance le serveur depuis ce dossier et le nomme aussi dans
    # CLAUDE_PROJECT_DIR ; cette variable l'emporte si le serveur a été lancé d'ailleurs.
    project = (os.environ.get("CLAUDE_PROJECT_DIR") or "").strip()
    roots = [Path(project if a == "." and project else a).expanduser().resolve() for a in argv]
    refresh_skills()
    stdin = open(sys.stdin.fileno(), encoding="utf-8", errors="replace", closefd=False)  # noqa: SIM115
    stdout = open(sys.stdout.fileno(), "w", encoding="utf-8", newline="\n", closefd=False)  # noqa: SIM115
    for line in stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        reply = handle(msg, roots, server) if isinstance(msg, dict) else None
        for out in ([reply] if reply is not None else []) + OUTBOX:
            stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            stdout.flush()
        OUTBOX.clear()
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--skill"]:
        sys.exit(install_skill(*sys.argv[2:3]))
    if sys.argv[1:2] == ["--remove-skill"]:
        sys.exit(remove_skill(*sys.argv[2:3]))
    if sys.argv[1:2] == ["--agents"]:
        sys.exit(install_agents())
    if sys.argv[1:2] == ["--remove-agents"]:
        sys.exit(remove_agents())
    if sys.argv[1:2] == ["--env"] and len(sys.argv) == 4:
        sys.exit(user_env(sys.argv[2], sys.argv[3]))
    if sys.argv[1:2] == ["--env-remove"] and len(sys.argv) == 3:
        sys.exit(user_env(sys.argv[2], None))
    chosen, rest = "orchestre", sys.argv[1:]
    if rest[:1] == ["--serveur"]:
        chosen, rest = (rest[1] if len(rest) > 1 else ""), rest[2:]
    if chosen not in SERVERS:
        print(f"serveur inconnu : {chosen} (connus : {', '.join(SERVERS)})", file=sys.stderr)
        sys.exit(2)
    sys.exit(serve(rest, chosen))
