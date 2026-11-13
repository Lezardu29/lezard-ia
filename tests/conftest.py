import os

import pytest

os.environ.setdefault("GATEWAY_DB", ":memory:")  # pas de fichier SQLite pendant les tests
os.environ.setdefault("GATEWAY_ENCRYPTION_KEY", "cle-de-test-uniquement")  # pas de fichier de clé
os.environ.setdefault("FX_AUTO_REFRESH", "false")  # aucun appel réseau à la BCE
os.environ.setdefault("PRICING_AUTO_REFRESH", "false")  # ni à la grille tarifaire LiteLLM
# ni aux API natives des serveurs locaux (/api/show…) : les faux serveurs des tests ne connaissent que l'API OpenAI.
# Testé à part dans test_local_context.py.
os.environ.setdefault("LOCAL_CONTEXT_PROBE", "false")
# fenêtre du serveur Ollama du PC (variable utilisateur de Windows) : jamais dans les tests, qui la règlent eux-mêmes
os.environ.pop("OLLAMA_CONTEXT_LENGTH", None)
# TestClient se présente comme « testclient » (pas une IP) : accès sans clé permis partout dans les tests,
# sauf ceux qui vérifient la restriction (12.2) et redéfinissent la variable
os.environ.setdefault("GATEWAY_ANONYMOUS_FROM", "*")
# transcription locale intégrée (faster-whisper) : pas de modèle chargé pendant les tests (testée à part, simulée)
os.environ.setdefault("LOCAL_WHISPER", "false")
os.environ.setdefault("HARDWARE_PROBE", "false")  # ni de relevé du matériel en tâche de fond (nvidia-smi…)
os.environ.setdefault("GATEWAY_IDLE_UNLOAD", "off")  # ni de déchargement automatique (testé à part, unload_idle)
os.environ.setdefault("GATEWAY_KEEP_ALIVE", "ollama")  # ni de rappel keep_alive après chaque requête (test_idle_unload)
os.environ.setdefault("LOCAL_TTS", "false")  # ni de lecture vocale locale (Kokoro) : testée à part, simulée
# guide des applications du contrôle du PC : jamais celui du vrai PC (data/pc_apps.json) ; tests : fichier à part
os.environ.setdefault("PC_APP_GUIDES", os.path.join(os.path.dirname(__file__), "aucun-guide-pc_apps.json"))
# serveurs MCP : jamais ceux du vrai PC (data/mcp.json), que chaque application de test lancerait pour de bon et
# qu'une autre, sur une autre boucle asyncio, tenterait ensuite d'arrêter ; tests : fichier à part (MCP_CONFIG)
os.environ.setdefault("MCP_CONFIG", os.path.join(os.path.dirname(__file__), "aucun-serveur-mcp.json"))


@pytest.fixture(autouse=True)
def _pro_edition(monkeypatch):
    """Offre Pro (29.1) dans tous les tests : toutes les fonctions. L'offre Gratuite et la licence signée sont
    testées dans test_edition.py."""
    from gateway import edition

    monkeypatch.setattr(edition, "_override", "pro")


@pytest.fixture(autouse=True)
def _nothing_detected(request, monkeypatch):
    """Ce qui est installé sur le vrai PC ne compte pas : seul ce que la page a noté. Détection : ses propres tests."""
    if not request.node.name.startswith("test_detection"):
        from gateway import detection

        monkeypatch.setattr(detection, "scan", lambda: {})


@pytest.fixture(autouse=True)
def _installs_ignore_this_pc(request, monkeypatch):
    """Installations : rien ne dépend de l'état du vrai PC (déclarations de Claude Code, programmes lancés, dossiers
    npm). Ces vérifications ont leurs propres tests (test_install_cleanup.py)."""
    if request.node.fspath.basename == "test_install_cleanup.py":
        return
    from gateway import installers

    monkeypatch.setattr(installers, "already_undone", lambda argv: False)
    monkeypatch.setattr(installers, "stop_running", lambda argv: [])
    monkeypatch.setattr(installers, "replaced_first", lambda argv: None)
    monkeypatch.setattr(installers, "tidy_after", lambda argv: None)
