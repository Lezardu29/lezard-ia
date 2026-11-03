"""Faux binaires de tunnel pour les tests (cloudflared, ngrok, tailscale).

Usage : python fake_tunnel.py <cloudflared|ngrok|tailscale|silent|crash> [args…]
Chaque appel est consigné dans $FAKE_TUNNEL_LOG (une ligne JSON).
"""

import json
import os
import sys
import time

kind, args = sys.argv[1], sys.argv[2:]
if os.getenv("FAKE_TUNNEL_LOG"):
    with open(os.environ["FAKE_TUNNEL_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps([kind] + args) + "\n")
if os.getenv("FAKE_TUNNEL_ENV_LOG"):  # secrets reçus par l'environnement (9.6)
    with open(os.environ["FAKE_TUNNEL_ENV_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps({k: os.getenv(k) for k in ("NGROK_AUTHTOKEN", "TUNNEL_TOKEN")}) + "\n")


def out(line, stream=sys.stdout):
    stream.write(line + "\n")
    stream.flush()


if kind == "cloudflared" and os.getenv("FAKE_NAMED_TUNNEL") and "run" in args:  # tunnel nommé (jeton)
    out("INF Registered tunnel connection connIndex=0 location=cdg01", sys.stderr)
    while True:
        time.sleep(1)
elif kind == "cloudflared":
    out("INF Requesting new quick Tunnel on trycloudflare.com...", sys.stderr)
    out("INF |  https://fluffy-test-tunnel.trycloudflare.com  |", sys.stderr)
    while True:
        time.sleep(1)
elif kind == "ngrok":
    out(json.dumps({"lvl": "info", "msg": "starting web service"}))
    out(json.dumps({"lvl": "info", "msg": "started tunnel", "url": "https://abc.ngrok-free.app"}))
    while True:
        time.sleep(1)
elif kind == "tailscale":
    if args[:2] == ["status", "--json"]:
        out(json.dumps({"Self": {"DNSName": "ma-machine.tail1234.ts.net."}}))
    else:
        out("Available on your tailnet: ok")
elif kind == "flaky":
    # scénario de coupures (12.4) : FAKE_FLAKY_PLAN="die,crash,ok" — une action par lancement successif
    # (die : URL puis arrêt après 0,3 s ; crash : arrêt sans URL ; ok : URL puis tourne)
    counter = os.environ["FAKE_FLAKY_COUNTER"]
    n = int(open(counter).read()) if os.path.exists(counter) else 0
    with open(counter, "w") as f:
        f.write(str(n + 1))
    plan = os.environ.get("FAKE_FLAKY_PLAN", "ok").split(",")
    action = plan[min(n, len(plan) - 1)]
    if action == "crash":
        out("ERR failed to connect to the edge", sys.stderr)
        sys.exit(1)
    url = os.environ.get("FAKE_FLAKY_URL") or f"https://run-{n}.trycloudflare.com"
    out(f"INF |  {url}  |", sys.stderr)
    if action == "die":
        time.sleep(0.3)
        sys.exit(1)
    while True:
        time.sleep(1)
elif kind == "crash":
    out("ERR failed to connect to the edge", sys.stderr)
    sys.exit(1)
elif kind == "silent":
    while True:
        time.sleep(1)
