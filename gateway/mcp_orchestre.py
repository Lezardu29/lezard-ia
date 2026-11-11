"""Chef d'orchestre : python mcp_orchestre.py [dossier autorisé…]   (serveur MCP, entrée et sortie standard)
                      python mcp_orchestre.py --skill | --remove-skill   (méthode pour Claude Code)

Une IA forte (Claude Fable ou Opus dans Claude Code, ou tout modèle qui parle MCP) garde le rôle difficile — découper
le travail, écrire la consigne de chaque étape, choisir le modèle de chaque étape, vérifier — et confie les étapes
simples à des modèles moins chers : ceux de l'Ollama de ce PC (gratuits), ou ceux de Lezard IA. Une étape que son
modèle ne sait pas faire revient « BLOQUÉE » avec la raison : l'IA forte précise la consigne, change de modèle ou
la fait elle-même. But : dépenser le moins possible de tokens du modèle fort.

Outils :
- `modeles` : les modèles disponibles, du plus léger au plus lourd, celui déjà chargé, celui pris par défaut.
- `confier_etape` : une consigne complète, des fichiers à lire (le serveur les lit lui-même : leur contenu ne passe
  pas par l'IA qui appelle), le modèle voulu, et un fichier où écrire la réponse (elle ne repasse pas non plus par
  l'IA qui appelle : seul un reçu de quelques lignes lui revient).

Lancé comme un script (aucune dépendance, pas même le paquet gateway) ; un message JSON par ligne. Fichiers : lus et
écrits seulement dans les dossiers donnés en arguments (« . » : le dossier du projet ouvert) ; sans argument, aucun.
Réglages (variables d'environnement) : OLLAMA_HOST ; ORCHESTRE_MODELE (modèle par défaut) ; ORCHESTRE_FENETRE (tokens
de contexte des modèles d'Ollama, 16 384 par défaut) ; LEZARD_IA_URL et LEZARD_IA_KEY (les étapes passent alors par
Lezard IA : tous ses modèles, « auto » compris, et son suivi des coûts).
"""

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
description: Use when the user asks to orchestrate a task, to delegate work to cheaper or local models, or to spend fewer tokens on a multi-step task ("/orchestre", "orchestre ça", "délègue aux petits modèles", "économise les tokens"). Requires the MCP server "orchestre" (tools modeles, confier_etape).
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
8. **Termine** par un bilan de trois lignes : étapes confiées et à quel modèle, étapes reprises par toi, ce qui a
   été vérifié.

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
        return f"ÉTAPE BLOQUÉE : {reason}\n{advice}\n{cost}"
    if alert and not alert.startswith("modèle :"):
        return f"ÉTAPE BLOQUÉE : {alert}\n{advice}\n{cost}" + ("" if target else f"\n\nRéponse partielle :\n{answer}")
    cost = cost if not alert else f"{cost[:-1]} · {alert}]"
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


def handle(msg: dict, roots: list[Path]) -> dict | None:
    """Réponse JSON-RPC à un message, None pour une notification."""
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None
    if method == "initialize":
        result = {
            "protocolVersion": (msg.get("params") or {}).get("protocolVersion") or PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "orchestre", "version": "1.0.0"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = msg.get("params") or {}
        try:
            name, args = params.get("name"), params.get("arguments") or {}
            if name == "modeles":
                text = listing()
            elif name == "confier_etape":
                text = delegate(args, roots)
            else:
                raise ValueError(f"outil inconnu : {name}")
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        except (ValueError, RuntimeError, OSError) as e:
            result = {"content": [{"type": "text", "text": f"Erreur : {e}"}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"méthode inconnue : {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def skill_folder() -> Path:
    return Path.home() / ".claude" / "skills" / "orchestre"


def install_skill() -> int:
    """La méthode, pour Claude Code : il la suit quand on lui demande d'orchestrer (ou /orchestre)."""
    folder = skill_folder()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(SKILL, encoding="utf-8")
    print(f"Méthode écrite : {folder / 'SKILL.md'}")
    return 0


def remove_skill() -> int:
    folder = skill_folder()
    if folder.exists():
        shutil.rmtree(folder)
        print(f"Dossier supprimé : {folder}")
    else:
        print("Déjà retiré.")
    return 0


def serve(argv: list[str]) -> int:
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
        reply = handle(msg, roots) if isinstance(msg, dict) else None
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--skill"]:
        sys.exit(install_skill())
    if sys.argv[1:2] == ["--remove-skill"]:
        sys.exit(remove_skill())
    sys.exit(serve(sys.argv[1:]))
