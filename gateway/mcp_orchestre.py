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
de contexte des modèles d'Ollama, 16 384 par défaut) ; LEZARD_IA_URL et LEZARD_IA_KEY (les étapes passent alors par
Lezard IA : tous ses modèles, « auto » compris, et son suivi des coûts).
"""

import hashlib
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROTOCOL = "2024-11-05"
EMBEDDINGS = ("embed", "bge", "minilm", "e5-", "gte-")  # modèles de vecteurs : ils n'écrivent pas de texte
MAX_FILE_CHARS = 200_000  # par fichier lu ; au-delà, le début seulement (signalé au modèle)
DEFAULT_WINDOW = 16_384
BLOCKED = re.compile(r"^\W{0,3}BLOQU[EÉ]E?\b\W*", re.IGNORECASE)
WORKER_RULES = (
    "Tu exécutes UNE étape d'un travail plus grand, confiée par une IA qui orchestre. Fais exactement ce que la "
    "consigne demande, dans le format demandé, sans introduction ni conclusion. Tu ne vois rien d'autre que ce "
    "message. Si tu ne peux pas réussir l'étape avec ce qui t'est donné (information ou fichier manquant, consigne "
    "ambiguë, tâche au-dessus de tes moyens), n'invente rien : réponds seulement « BLOQUE: » suivi de la raison "
    "précise et de ce qu'il te faudrait. Ne cite jamais un nom (fonction, fichier, option) que tu ne vois pas "
    "écrit tel quel dans ce qui t'est donné."
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
3. **Choisis un modèle par étape** : appelle `modeles` une seule fois, puis prends le plus léger capable de l'étape
   (celui déjà chargé si possible : aucun rechargement). Un modèle « code » pour du code.
4. **Écris une consigne autonome par étape** : le modèle ne voit rien de la conversation. Donne l'objectif, le
   format de sortie exact, les contraintes, un exemple si utile. Pour tout ce qu'il tire d'un fichier, exige le
   nom exact tel qu'il est écrit et son numéro de ligne (« `nom` (ligne N) : … ») : un petit modèle invente des
   noms, et une ligne citée se vérifie d'une seule recherche. Passe les fichiers par `fichiers` (ne recopie pas
   leur contenu) et fais écrire les longues réponses avec `ecrire_dans` (tu ne reçois alors qu'un reçu).
5. **Annonce le plan** à l'utilisateur en un tableau court (étape, qui la fait, pourquoi), puis exécute sans
   attendre, sauf s'il a demandé à valider.
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
- `controler` : aux étapes 6 et 8, sur chaque document écrit par un modèle moins fort, AVANT toute recherche ou
  lecture de ta part. Il ne consomme aucun token et rend la liste exacte des noms absents et des lignes fausses.
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


def lezard() -> tuple[str, str] | None:
    """(adresse, clé) de Lezard IA si les étapes doivent passer par elle."""
    url, key = (os.environ.get("LEZARD_IA_URL") or "").strip(), (os.environ.get("LEZARD_IA_KEY") or "").strip()
    return (url.rstrip("/").removesuffix("/v1"), key) if url and key else None


def call(url: str, body: dict | None = None, timeout: float = 14400, key: str = "") -> dict:
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
        raise RuntimeError(f"le serveur de modèles ne répond pas à {url} ({e}). Est-il lancé ?") from None


def models() -> list[dict]:
    """Modèles qui écrivent du texte. Ollama : du plus léger au plus lourd, avec ceux déjà chargés. Lezard IA :
    dans l'ordre où elle les liste."""
    via = lezard()
    if via:
        data = call(f"{via[0]}/v1/models", timeout=20, key=via[1]).get("data") or []
        names = [m.get("id") or "" for m in data]
        return [{"name": n, "size": 0, "params": "", "loaded": False} for n in names if _writes_text(n)]
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
    return (ready or found)[0]["name"] if found else None


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


def unfenced(text: str) -> str:
    """Le contenu d'une réponse faite d'un seul bloc de code (```…```), sans ses clôtures ; sinon la réponse."""
    m = re.fullmatch(r"\s*```[^\n]*\n(.*?)\n?```\s*", text, re.DOTALL)
    return m.group(1) if m else text


def generate(model: str, messages: list[dict], max_tokens: int | None) -> tuple[str, int, int, str]:
    """(réponse, tokens lus, tokens écrits, alerte) par Lezard IA ou par Ollama."""
    via = lezard()
    if via:
        body = {"model": model, "messages": messages, **({"max_tokens": max_tokens} if max_tokens else {})}
        r = call(f"{via[0]}/v1/chat/completions", body, key=via[1])
        choice, usage = (r.get("choices") or [{}])[0], r.get("usage") or {}
        cut = "réponse coupée (max_tokens atteint)." if choice.get("finish_reason") == "length" else ""
        used = (r.get("x_gateway") or {}).get("model") or model
        answer = (choice.get("message") or {}).get("content") or ""
        return answer, usage.get("prompt_tokens") or 0, usage.get("completion_tokens") or 0, cut or f"modèle : {used}"
    try:
        window = int(os.environ.get("ORCHESTRE_FENETRE") or DEFAULT_WINDOW)
    except ValueError:
        window = DEFAULT_WINDOW
    options = {"num_ctx": window, **({"num_predict": max_tokens} if max_tokens else {})}
    r = call(f"{host()}/api/chat", {"model": model, "messages": messages, "stream": False, "options": options})
    seen, written = r.get("prompt_eval_count") or 0, r.get("eval_count") or 0
    alert = ""
    if seen >= window - 16:  # Ollama coupe en silence ce qui dépasse sa fenêtre
        alert = f"demande plus longue que la fenêtre ({window} tokens) : le début n'a pas été lu."
    elif r.get("done_reason") == "length":
        alert = "réponse coupée (longueur maximale atteinte)."
    return (r.get("message") or {}).get("content") or "", seen, written, alert


STEPS: list[dict] = []  # étapes confiées depuis le lancement du serveur (une session de l'IA qui orchestre)


def _record(model: str, prompt: str, state: str, target: Path | None = None) -> None:
    STEPS.append({"model": model, "task": " ".join(prompt.split())[:80], "state": state, "file": target})


def report() -> str:
    """Ce qui a été confié, pour la vérification finale de l'IA qui orchestre."""
    if not STEPS:
        return "Aucune étape confiée dans cette session : rien à vérifier de ce côté."
    lines = []
    for i, step in enumerate(STEPS, 1):
        line = f"{i}. [{step['state']}] {step['model']} — {step['task']}"
        if step["file"] is not None:
            f = step["file"]
            line += f"\n   fichier : {f} " + (
                f"({f.stat().st_size} octets)" if f.is_file() else "(ABSENT : supprimé ou déplacé)"
            )
        lines.append(line)
    stuck = sum(1 for x in STEPS if x["state"] == "bloquée")
    files = sum(1 for x in STEPS if x["file"] is not None)
    return (
        f"{len(STEPS)} étape(s) confiée(s), {stuck} bloquée(s), {files} fichier(s) écrit(s) par un modèle moins fort :\n"
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
    parts = []
    for raw in args.get("fichiers") or []:
        name, text = read_file(str(raw), roots)
        parts.append(f"<fichier nom={json.dumps(name, ensure_ascii=False)}>\n{text}\n</fichier>")
    system = WORKER_RULES + (f"\n\nRôle : {args['role']}" if args.get("role") else "")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join([*parts, prompt])}]
    limit = args["max_tokens"] if isinstance(args.get("max_tokens"), int) and args["max_tokens"] > 0 else None
    answer, seen, written, alert = generate(model, messages, limit)
    answer = answer.strip()
    cost = f"[{model} · {seen} tokens lus, {written} écrits]"
    advice = "À toi : précise la consigne ou donne ce qui manque et relance, prends un modèle plus fort, ou fais-la."
    if BLOCKED.match(answer) or not answer:
        reason = BLOCKED.sub("", answer, count=1).strip() or "réponse vide."
        _record(model, prompt, "bloquée")
        return f"ÉTAPE BLOQUÉE : {reason}\n{advice}\n{cost}"
    if alert and not alert.startswith("modèle :"):
        _record(model, prompt, "bloquée")
        return f"ÉTAPE BLOQUÉE : {alert}\n{advice}\n{cost}" + ("" if target else f"\n\nRéponse partielle :\n{answer}")
    cost = cost if not alert else f"{cost[:-1]} · {alert}]"
    _record(model, prompt, "faite", target)
    if target is None:
        return f"{answer}\n\n{cost}"
    content = unfenced(answer)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content + ("" if content.endswith("\n") else "\n"), encoding="utf-8")
    lines = content.splitlines()
    head = "\n".join(lines[:5])
    return f"Écrit dans {target} : {len(lines)} lignes, {len(content)} caractères. Début :\n{head}\n\n{cost}"


def listing() -> str:
    found = models()
    if not found:
        return "Aucun modèle disponible."
    chosen = default_model(found)
    if lezard():
        names = "\n".join(f"- {m['name']}" for m in found)
        return f"Modèles de Lezard IA (« auto » : elle choisit le moins cher capable) :\n- auto\n{names}\nPar défaut : {chosen}"
    lines = [
        f"- {m['name']} : {m['size'] / 1e9:.1f} Go, {m['params']}" + (" — déjà chargé" if m["loaded"] else "")
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
                    "description": "Fichiers texte à lui faire lire (chemins dans le projet).",
                },
                "ecrire_dans": {
                    "type": "string",
                    "description": "Fichier du projet où écrire sa réponse (remplacé s'il existe) au lieu de te la rendre.",
                },
                "role": {"type": "string", "description": "Rôle ou style à lui donner, facultatif."},
                "max_tokens": {"type": "integer", "description": "Longueur maximale de sa réponse, facultatif."},
            },
            "required": ["consigne"],
        },
    },
]


# ---------- mémoire des résumés (serveur « resumes ») ----------
CACHE_VERSION = "1"  # à changer si la forme des résumés change : les anciens ne sont plus repris
MAX_GROUP = 40  # fichiers résumés en un appel, au plus
SUMMARY_ASK = (
    "Résume ce fichier pour une IA qui doit travailler dessus sans le lire : son rôle en deux lignes, puis ses "
    "éléments principaux (fonctions, classes, constantes, sections) au format « `nom` (ligne N) : ce qu'il fait », "
    "puis comment on l'utilise ou le lance. Les lignes sont numérotées. N'écris que ce qui est dans le fichier."
)


def cache_dir() -> Path:
    return Path(os.environ.get("ORCHESTRE_MEMOIRE") or Path.home() / ".lezard-ia" / "resumes")


def numbered(text: str) -> str:
    return "\n".join(f"{i}\t{line}" for i, line in enumerate(text.splitlines(), 1))


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
        raise RuntimeError(f"{model} bloque : {BLOCKED.sub('', answer, count=1).strip() or 'réponse vide.'}")
    if alert and not alert.startswith("modèle :"):
        raise RuntimeError(f"{model} : {alert}")
    return answer, f"{model}, {seen} tokens lus, {written} écrits"


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
    name = json.dumps(path.name, ensure_ascii=False)
    answer, cost = worker(pick_model(wanted), f"<fichier nom={name}>\n{numbered(text)}\n</fichier>\n\n{ask}")
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
    parts, reused, failed = [], 0, 0
    for f in files:
        rel = f.relative_to(roots[0]).as_posix()
        try:
            text, cached, detail = summarize(f, str(args.get("question") or ""), str(args.get("modele") or ""))
            reused += cached
            parts.append(f"## {rel}\n{text}\n[{detail}]")
        except (RuntimeError, OSError) as e:
            failed += 1
            parts.append(f"## {rel}\nRÉSUMÉ IMPOSSIBLE : {e}")
    done = len(files) - failed - reused
    foot = f"{len(files)} fichier(s) : {reused} repris de la mémoire, {done} résumé(s) maintenant, {failed} en échec."
    return "\n\n".join(parts) + f"\n\n{foot}"


def forget(args: dict, roots: list[Path]) -> str:
    folder = cache_dir()
    stored = list(folder.glob("*.md")) if folder.is_dir() else []
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


def check_draft(args: dict, roots: list[Path]) -> str:
    """Sans aucun modèle : chaque nom écrit entre accents graves dans le brouillon existe-t-il dans les sources, et
    à la ligne citée (à deux lignes près) ?"""
    raw = str(args.get("brouillon") or "").strip()
    sources = [str(x) for x in args.get("sources") or []]
    if not raw or not sources:
        raise ValueError("« brouillon » et « sources » sont requis.")
    draft = read_file(raw, roots)[1]
    texts = [inside(x, roots, "lecture de fichiers").read_text(encoding="utf-8", errors="replace") for x in sources]
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
            if not any(_cited_in(name, "\n".join(ls[max(0, n - 3) : n + 2]), near=True) for ls in lines):
                wrong_line.append(f"{name} (ligne {n})")
    head = f"{len(names)} nom(s) cité(s), {checked_lines} ligne(s) citée(s) contrôlée(s)."
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
    target.write_text(content + ("" if content.endswith("\n") else "\n"), encoding="utf-8")
    _record(model, f"retouche de {target.name}", "faite", target)
    before, after = len(draft.splitlines()), len(content.splitlines())
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


SERVERS = {
    "orchestre": (
        lambda: TOOLS,
        {"modeles": _no_roots(listing), "bilan": _no_roots(report), "confier_etape": delegate},
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


def mode_file() -> Path:
    return Path(os.environ.get("LEZARD_IA_MODE") or Path.home() / ".lezard-ia" / "mode.json")


def default_on() -> bool:
    """Ce que valent les nouvelles sessions (« /lezard toujours » ou « jamais ») : éteint tant que rien n'est choisi."""
    try:
        return bool(json.loads(mode_file().read_text(encoding="utf-8")).get("actif"))
    except (OSError, ValueError, AttributeError):
        return False


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
            "par_defaut": {
                "type": "boolean",
                "description": "Avec activer ou desactiver : garder aussi ce choix pour les prochaines sessions.",
            },
        },
        "required": ["action"],
    },
}


def switch(args: dict, roots: list[Path]) -> str:
    action = str(args.get("action") or "etat")
    if action not in ("activer", "desactiver", "etat"):
        raise ValueError("« action » : activer, desactiver ou etat.")
    if action != "etat":
        before, wanted = is_on(), action == "activer"
        ACTIVE["on"] = wanted
        if args.get("par_defaut") is True:
            mode_file().parent.mkdir(parents=True, exist_ok=True)
            mode_file().write_text(json.dumps({"actif": wanted}), encoding="utf-8")
        if before != wanted:
            OUTBOX.append({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
    later = "actifs" if default_on() else "éteints"
    if is_on():
        names = ", ".join(t["name"] for t in ALL_TOOLS)
        return (
            f"Lezard IA : ACTIF dans cette session. Outils : {names}. Sers-t'en dès qu'il y a de gros fichiers à "
            "lire ou du texte répétitif à produire ; fais directement les petites tâches. "
            f"Nouvelles sessions : outils {later}."
        )
    return (
        f"Lezard IA : ÉTEINT dans cette session (Claude seul, comme d'habitude). Nouvelles sessions : outils {later}."
    )


ALL_TOOLS = [*TOOLS, *TOOLS_RESUMES, *TOOLS_BROUILLONS]


def _when_on(function):
    def guarded(args: dict, roots: list[Path]) -> str:
        if not is_on():
            raise ValueError("Lezard IA est éteint dans cette session : l'utilisateur l'active avec /lezard on.")
        return function(args, roots)

    return guarded


SERVERS["lezard"] = (
    lambda: [SWITCH_TOOL, *ALL_TOOLS] if is_on() else [SWITCH_TOOL],
    {
        "lezard_ia": switch,
        **{
            name: _when_on(f)
            for server in ("orchestre", "resumes", "brouillons")
            for name, f in SERVERS[server][1].items()
        },
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

Réponds ensuite en UNE ligne : actif ou éteint, et ce que valent les prochaines sessions.

## Quand il est actif, pour tout le reste de la session

- Gros fichier ou dossier à connaître : `resumer_fichier` ou `resumer_dossier` au lieu de lire ; lis ensuite
  seulement les lignes utiles.
- Travail en plusieurs étapes avec beaucoup à lire ou à écrire : suis la méthode « orchestre » (`confier_etape`,
  puis `bilan` et vérification finale).
- Document produit par un modèle moins fort : `controler`, puis `retoucher` plutôt que de le réécrire.
- Petite tâche : fais-la directement, ces outils n'y font rien gagner.

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
            result = {"content": [{"type": "text", "text": text}], "isError": False}
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
    folder = skill_folder(name)
    if folder.exists():
        shutil.rmtree(folder)
        print(f"Dossier supprimé : {folder}")
    else:
        print("Déjà retiré.")
    return 0


def serve(argv: list[str], server: str = "orchestre") -> int:
    roots = [Path(a).expanduser().resolve() for a in argv]
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
