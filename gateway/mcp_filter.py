"""Relais MCP (stdio) entre Claude Code et le serveur d'une extension, pour une IA locale :
python mcp_filter.py [--only a,b] [--drop x,y] [--any-word t1,t2] -- <commande du serveur…>

Pour les extensions de Claude Code lancées depuis le chat (chat_tools.mcp_args). Le relais lance le vrai serveur et
lui passe chaque message, sauf :
- « --only » : seuls ces outils sont montrés (réponse à « tools/list ») et appelables. REA présente 207 outils, plus
  que la fenêtre d'une IA locale ;
- « --drop » : ces arguments sont retirés des appels. REA sous Windows : « document » ne doit pas être le chemin du
  fichier, qu'une petite IA y met toujours ; sans lui, REA prend le programme ouvert ;
- « --any-word » : une recherche de ces outils qui ne trouve rien est refaite mot par mot (« query »). agentmemory sans
  son serveur ne garde que les souvenirs qui ont TOUS les mots, et une petite IA pose la question entière.
Lancé comme un script : aucune dépendance au paquet gateway.
"""

import json
import re
import subprocess
import sys
import threading
import time

OPTIONS = ("--only", "--drop", "--any-word")


def parse(argv: list[str]) -> tuple[dict, list[str]] | None:
    opts: dict = {}
    while argv and argv[0] in OPTIONS and len(argv) > 1:
        opts[argv[0]] = {name for name in argv[1].split(",") if name}
        argv = argv[2:]
    if len(argv) < 2 or argv[0] != "--":
        return None
    return opts, argv[1:]


def empty(result) -> bool:
    """Réponse d'une recherche sans résultat (texte JSON avec « results » vide ou « matched_count » à 0)."""
    try:
        data = json.loads(result["content"][0]["text"])
    except (KeyError, IndexError, TypeError, ValueError):
        return False
    return isinstance(data, dict) and (data.get("results") == [] or data.get("matched_count") == 0)


def words(query: str) -> list[str]:
    seen = []
    for w in re.findall(r"\w{4,}", query.lower()):
        if w not in seen:
            seen.append(w)
    return seen


def main(argv: list[str]) -> int:
    parsed = parse(argv)
    if parsed is None:
        print("usage : mcp_filter.py [--only a,b] [--drop x,y] [--any-word t] -- commande [arguments]", file=sys.stderr)
        return 2
    opts, command = parsed
    only, drop, any_word = opts.get("--only"), opts.get("--drop", set()), opts.get("--any-word", set())
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    listing: set = set()  # identifiants des demandes « tools/list » en cours
    retry: dict = {}  # identifiant d'une recherche → (demande, mots à essayer ensuite)
    lock = threading.Lock()
    out = sys.stdout.buffer

    def send(data: dict) -> None:
        with lock:
            out.write(json.dumps(data, ensure_ascii=False).encode("utf-8") + b"\n")
            out.flush()

    def to_server(data: dict) -> None:
        child.stdin.write(json.dumps(data).encode("utf-8") + b"\n")
        child.stdin.flush()

    def from_server() -> None:
        for line in child.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # le serveur n'écrit que du JSON sur sa sortie : rien d'autre à relayer
            if not isinstance(msg, dict):
                continue
            ident = msg.get("id")
            if ident in listing and isinstance(msg.get("result"), dict) and only is not None:
                listing.discard(ident)
                tools = msg["result"].get("tools") or []
                msg["result"]["tools"] = [t for t in tools if t.get("name") in only]
            if ident in retry:
                request, rest = retry[ident]
                if rest and empty(msg.get("result")):  # rien trouvé : le mot suivant, même identifiant
                    retry[ident] = (request, rest[1:])
                    request["params"]["arguments"]["query"] = rest[0]
                    to_server(request)
                    continue
                del retry[ident]
            send(msg)
        sys.stdout.close()

    reader = threading.Thread(target=from_server, daemon=True)
    reader.start()
    for line in sys.stdin.buffer:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if msg.get("method") == "tools/list" and "id" in msg:
            listing.add(msg["id"])
        elif msg.get("method") == "tools/call":
            if only is not None and params.get("name") not in only:
                if "id" in msg:
                    send(
                        {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "Outil non proposé."}}
                    )
                continue
            arguments = params.get("arguments")
            if isinstance(arguments, dict) and drop:
                params["arguments"] = arguments = {k: v for k, v in arguments.items() if k not in drop}
            query = arguments.get("query") if isinstance(arguments, dict) else None
            if params.get("name") in any_word and isinstance(query, str) and "id" in msg:
                rest = [w for w in words(query) if w != query.lower()]
                if len(rest) > 1:
                    retry[msg["id"]] = (json.loads(json.dumps(msg)), rest)
        to_server(msg)
    for _ in range(300):  # fin de l'entrée : les recherches refaites mot par mot vont d'abord à leur terme
        if not retry:
            break
        time.sleep(0.1)
    child.stdin.close()
    reader.join(timeout=5)
    child.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
