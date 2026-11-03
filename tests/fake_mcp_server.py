"""Faux serveur MCP (stdio) pour les tests : initialize, tools/list, tools/call ; deux outils."""

import json
import os
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "Répète le texte",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    },
    {
        "name": "secret check",
        "description": "Dit si un secret de la gateway est visible",
        "inputSchema": {"type": "object"},
    },
]


def reply(ident, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    msg = json.loads(line)
    method, ident = msg.get("method"), msg.get("id")
    if method == "initialize":
        reply(ident, {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "faux"}})
    elif method == "tools/list":
        reply(ident, {"tools": TOOLS})
    elif method == "tools/call":
        name, args = msg["params"]["name"], msg["params"]["arguments"]
        if name == "echo":
            reply(ident, {"content": [{"type": "text", "text": "écho : " + args["text"]}]})
        else:
            seen = "GATEWAY_MASTER_KEY" in os.environ or "OPENAI_API_KEY" in os.environ
            extra = os.environ.get("MCP_TEST_EXTRA", "")
            reply(
                ident,
                {"content": [{"type": "text", "text": f"secret visible : {seen} ; extra : {extra}"}], "isError": False},
            )
