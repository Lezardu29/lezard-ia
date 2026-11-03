"""Téléchargements figés de la marketplace : chacun répond-il encore, empreinte SHA-256 enregistrée comprise ?

Test réseau, lent : lancé seulement avec LEZARD_TESTS_RESEAU=1 (et ignoré sans réseau). Même contrôle à la main :
python -m gateway.pins --liens (--complet pour recalculer l'empreinte des installateurs téléchargés).
"""

import asyncio
import os
import socket

import httpx
import pytest

from gateway import pins


def _online() -> bool:
    try:
        socket.create_connection(("registry.npmjs.org", 443), timeout=5).close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(
    os.getenv("LEZARD_TESTS_RESEAU") != "1", reason="test réseau : LEZARD_TESTS_RESEAU=1 pour le lancer"
)
def test_every_pinned_download_still_answers_and_matches():
    if not _online():
        pytest.skip("pas de réseau")

    async def go():
        async with httpx.AsyncClient(headers={"User-Agent": "LezardIA-tests"}, follow_redirects=True) as client:
            return await pins.check_downloads(client)

    rows = asyncio.run(go())
    dead = [
        f"{r['kind']} {r['name']} : {r['problem']}" for r in rows if r["problem"] and "non vérifié" not in r["problem"]
    ]
    assert not dead, "Téléchargements en défaut :\n" + "\n".join(dead)
