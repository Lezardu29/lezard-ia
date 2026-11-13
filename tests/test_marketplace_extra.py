"""Supplément signé de la marketplace (marketplace_extra.py) : des fiches ajoutées sans nouvelle version, et rien
d'autre. Ce qui n'est pas signé par la clé des mises à jour, ce qui recule, et toute commande libre sont refusés."""

import copy
import json

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from gateway import detection, installers, marketplace, release
from gateway import marketplace_extra as extra
from gateway.license import public_key_b64
from gateway.store import DocumentStore

KEY = Ed25519PrivateKey.generate()
KEYS = {"u1": public_key_b64(KEY)}
LICENSE = {"name": "MIT", "copyright": "Quelqu'un", "url": "https://github.com/a/b"}
SHA = "ab" * 32


def content(**over) -> dict:
    base = {
        "serial": 2,
        "mcp": [
            {
                "id": "meteo",
                "name": "Météo",
                "description": "La météo d'une ville.",
                "package": "@exemple/server-meteo",
                "version": "1.4.2",
                "size": "moins de 1 Mo",
                "license": LICENSE,
                "example": "/mcp quel temps fait-il à Brest ?",
            }
        ],
        "apps": [
            {
                "id": "nouvelle-appli",
                "name": "Nouvelle appli",
                "description": "Une application de chat.",
                "url": "https://github.com/a/appli",
                "license": LICENSE,
                "commands": [{"use": "Ctrl+K", "what": "Ouvrir la recherche.", "where": "app"}],
                "recipe": {
                    "steps": [
                        {
                            "type": "setup",
                            "url": "https://github.com/a/appli/releases/download/v1.2.0/Appli-1.2.0-setup.exe",
                            "sha256": SHA,
                            "size": 1000,
                            "silent": "/S",
                        }
                    ],
                    "size": "1 Mo",
                    "after": "Ouvrez l'application.",
                    "windows_name": "Appli",
                    "uninstall_name": "Appli",
                },
            }
        ],
        "tools": [
            {
                "id": "nouvel-outil",
                "name": "Nouvel outil",
                "description": "Un outil en ligne de commande.",
                "url": "https://github.com/a/outil",
                "license": LICENSE,
                "commands": [{"use": "outil --aide", "what": "Affiche l'aide.", "where": "terminal"}],
                "recipe": {
                    "steps": [{"type": "npm", "package": "outil-cli", "version": "3.1.0"}],
                    "size": "2 Mo",
                    "after": "Dans un terminal : outil --aide.",
                },
            }
        ],
        "removed": [{"key": "tool:repomix", "reason": "paquet compromis le 2027-01-02."}],
    }
    return {**base, **over}


@pytest.fixture(autouse=True)
def catalogue_du_programme(monkeypatch):
    monkeypatch.setattr(extra, "PUBLIC_KEYS", KEYS)
    extra.apply(None)
    yield
    extra.apply(None)


def test_les_fiches_signees_entrent_dans_le_catalogue_avec_des_etapes_figees():
    before = (
        len(marketplace.MCP_SERVERS),
        len(marketplace.APPS),
        len(marketplace.AGENT_TOOLS),
        len(installers.RECIPES),
    )
    state = extra.apply(extra.verify(extra.sign(KEY, "u1", content())))
    assert state["serial"] == 2 and sorted(state["added"]) == ["app:nouvelle-appli", "mcp:meteo", "tool:nouvel-outil"]
    assert marketplace.mcp_server("meteo")["version"] == "1.4.2"
    assert marketplace.add_mcp("meteo", "npx", write=False)["args"] == ["-y", "@exemple/server-meteo@1.4.2"]
    assert installers.RECIPES["tool:nouvel-outil"]["steps"] == [("npm", "install", "--global", "outil-cli@3.1.0")]
    assert installers.uninstall_steps("tool:nouvel-outil") == [("npm", "uninstall", "--global", "outil-cli")]
    setup = installers.RECIPES["app:nouvelle-appli"]
    assert setup["steps"][0][:2] == (installers.PYTHON, installers.SETUP) and setup["steps"][0][-1] == "/S"
    assert setup["windows"] is True and detection.WINDOWS_NAMES["app:nouvelle-appli"] == "appli"
    assert installers.uninstall_steps("app:nouvelle-appli")[0][-2:] == ("--uninstall", "Appli")
    # la fiche retirée reste affichée avec la raison, se désinstalle encore, ne s'installe plus
    retired = next(t for t in marketplace.AGENT_TOOLS if t["id"] == "repomix")
    assert retired["retired"] is True and "compromis" in retired["warning"]
    assert installers.uninstall_steps("tool:repomix")
    with pytest.raises(marketplace.MarketplaceError) as e:
        installers.plan("tool:repomix")
    assert e.value.status == 410
    # appliquer deux fois ne double rien ; sans supplément, le catalogue du programme revient tel quel
    extra.apply(extra.parse(content()))
    assert len(marketplace.AGENT_TOOLS) == before[2] + 1
    extra.apply(None)
    assert (
        len(marketplace.MCP_SERVERS),
        len(marketplace.APPS),
        len(marketplace.AGENT_TOOLS),
        len(installers.RECIPES),
    ) == before
    assert not marketplace.RETIRED and "retired" not in next(t for t in marketplace.AGENT_TOOLS if t["id"] == "repomix")


def test_une_fiche_du_meme_identifiant_remplace_celle_du_programme():
    old = marketplace.mcp_server("memoire")
    new = {**content()["mcp"][0], "id": "memoire", "package": old["package"], "version": "9.9.9"}
    state = extra.apply(extra.parse(content(mcp=[new], apps=[], tools=[], removed=[])))
    assert state["replaced"] == ["mcp:memoire"] and marketplace.mcp_server("memoire")["version"] == "9.9.9"
    assert len(marketplace.MCP_SERVERS) == len(extra._base()["mcp"])


def test_signature_cle_et_prefixe():
    token = extra.sign(KEY, "u1", content())
    assert extra.verify(token)["serial"] == 2
    body, sig = token[len(extra.PREFIX) :].split(".")
    forged = extra._b64(extra._unb64(body).replace(b'"serial":2', b'"serial":9'))
    for bad in (
        f"{extra.PREFIX}{forged}.{sig}",
        token[:-4] + "AAAA",
        "LZC1.x.y",
        "",
        None,
        token.replace("LZC1.", "LZU1."),
    ):
        with pytest.raises(extra.CatalogError):
            extra.verify(bad)
    other = Ed25519PrivateKey.generate()
    with pytest.raises(extra.CatalogError, match="invalide"):
        extra.verify(extra.sign(other, "u1", content()))
    with pytest.raises(extra.CatalogError, match="inconnue"):
        extra.verify(extra.sign(KEY, "u9", content()))
    # une annonce de version, signée par la même clé, ne passe pas pour un catalogue, ni l'inverse
    announce = release.sign(KEY, "u1", {"version": "9.9.9", "url": "https://x/y.exe", "sha256": SHA, "size": 5})
    with pytest.raises(extra.CatalogError):
        extra.verify(announce)
    with pytest.raises(extra.CatalogError):
        extra.verify(extra.PREFIX + announce[len(release.PREFIX) :])
    with pytest.raises(release.ReleaseError):
        release.verify(release.PREFIX + token[len(extra.PREFIX) :], KEYS)


def _with(path: str, value) -> dict:
    """Le supplément d'exemple avec un champ changé : « tools.0.recipe.steps.0.package »."""
    data = copy.deepcopy(content())
    node, keys = data, path.split(".")
    for k in keys[:-1]:
        node = node[int(k)] if k.isdigit() else node[k]
    last = int(keys[-1]) if keys[-1].isdigit() else keys[-1]
    if value is ...:
        del node[last]
    else:
        node[last] = value
    return data


@pytest.mark.parametrize(
    "path, value",
    [
        ("tools.0.recipe.steps.0", {"type": "run", "argv": ["powershell", "-c", "calc"]}),  # pas de commande libre
        ("tools.0.recipe.steps.0", ["cmd", "/c", "calc"]),
        ("tools.0.recipe.steps.0.package", "outil-cli & calc"),
        ("tools.0.recipe.steps.0.package", "--registry=https://mal.example"),
        ("tools.0.recipe.steps.0.package", "https://mal.example/x.tgz"),
        ("tools.0.recipe.steps.0.version", "latest"),  # version figée
        ("tools.0.recipe.steps.0.version", "1.0.0 || calc"),
        ("tools.0.recipe.steps.0.script", "x"),  # champ inconnu
        ("tools.0.recipe.steps", []),
        ("tools.0.recipe.requires", ["powershell"]),
        ("tools.0.recipe.after", ...),
        ("tools.0.url", "javascript:alert(1)"),
        ("tools.0.url", "http://github.com/a/b"),
        ("tools.0.url", "https://user:pass@github.com/a"),
        ("tools.0.name", "x\x1b[2J"),
        ("tools.0.description", "x" * 601),
        ("tools.0.id", "../etc"),
        ("tools.0.commands", []),
        ("tools.0.commands.0.where", "ailleurs"),
        ("tools.0.license.commercial", "peut-être"),
        ("tools.0.onclick", "x"),
        ("apps.0.recipe.steps.0.url", "https://mal.example/releases/download/v1/a.exe"),  # GitHub seulement
        ("apps.0.recipe.steps.0.url", "https://github.com/a/b/releases/download/v1/../../x.exe"),
        ("apps.0.recipe.steps.0.url", "https://github.com/a/b/releases/download/v1/a.ps1"),
        ("apps.0.recipe.steps.0.url", "https://github.com/a/b/releases/download/../a.exe"),
        ("apps.0.recipe.steps.0.url", "https://github.com/a/../releases/download/v1/a.exe"),
        ("apps.0.recipe.steps.0.sha256", "abc"),  # empreinte figée
        ("apps.0.recipe.steps.0.size", "1000"),
        ("apps.0.recipe.steps.0.silent", "/S & calc"),
        ("apps.0.recipe.windows_name", ...),
        ("apps.0.recipe.uninstall_name", ...),
        ("apps.0.recipe.windows", False),
        ("mcp.0.package", "server meteo"),
        ("mcp.0.version", "^1.4.2"),
        ("mcp.0.args", ["--ok", "x & calc"]),
        ("mcp.0.args", ["%COMSPEC%"]),
        ("mcp.0.env", {"NODE_OPTIONS": "--require=x"}),
        ("mcp.0.env", {"PATH": "x"}),
        ("mcp.0.env", {"MODE": "a b"}),
        ("mcp.0.secret", {"env": "NPM_TOKEN", "label": "Clé", "url": "https://x.example"}),
        ("mcp.0.secret", {"env": "CLE_API", "label": "Clé", "url": "https://x.example"}),  # sans « service »
        ("mcp.0.folder_env", "DOSSIER"),  # sans needs_folder
        ("mcp.0.secret", "CLE_API"),  # mauvais type : refusé proprement, jamais une erreur brute
        ("tools.0.recipe.requires", [["node"]]),
        ("tools.0.license", ["MIT"]),
        ("apps.0.recipe.uninstall_name", "Appli & calc"),
        ("apps.0.recipe.windows_name", r"..\Appli"),
        ("removed.0.key", "component:ollama"),
        ("removed.0.key", "tool:"),
        ("removed.0.reason", ...),
        ("serial", 0),
        ("serial", True),
        ("serial", "3"),
        ("script", "x"),
        ("tools.1", copy.deepcopy(content()["tools"][0])),  # identifiant en double
    ],
)
def test_ce_que_le_supplement_ne_peut_pas_contenir(path, value):
    data = (
        _with(path, value) if not path.startswith("tools.1") else {**content(), "tools": [content()["tools"][0], value]}
    )
    with pytest.raises(extra.CatalogError):
        extra.parse(data)
    with pytest.raises(extra.CatalogError):  # et l'outil de signature refuse de le signer
        extra.sign(KEY, "u1", data)


def test_chaque_type_d_etape_donne_une_etape_que_lezard_ia_sait_defaire_et_reconnaitre():
    steps = [
        {"type": "npm", "package": "@scope/outil", "version": "1.2.3"},
        {"type": "uv", "package": "outil[all]", "version": "0.4.1", "python": "3.12"},
        {"type": "winget", "id": "Editeur.Outil"},
        {"type": "vscode", "extension": "editeur.outil"},
        {"type": "claude-plugin", "source": "editeur/depot", "plugin": "outil@depot"},
        {"type": "setup", "url": "https://github.com/a/b/releases/download/v1.0/Outil-win.zip", "sha256": SHA,
         "size": 10, "folder": "Outil"},
    ]  # fmt: skip
    tool = {**content()["tools"][0], "recipe": {"steps": steps, "size": "x", "after": "y", "windows_name": "Outil"}}
    extra.apply(extra.parse(content(tools=[tool])))
    recipe = installers.RECIPES["tool:nouvel-outil"]
    assert [a[0] for a in recipe["steps"]] == ["npm", "uv", "winget", "code", "claude", "claude", installers.PYTHON]
    assert recipe["steps"][1] == ("uv", "tool", "install", "--force", "--python", "3.12", "outil[all]==0.4.1")
    undo = installers.uninstall_steps("tool:nouvel-outil")
    assert undo is not None and ("uv", "tool", "uninstall", "outil") in undo
    assert ("winget", "uninstall", "--id", "Editeur.Outil", "--exact", "--silent", "--disable-interactivity") in undo
    assert all(isinstance(a, str) for argv in recipe["steps"] for a in argv)

    class Facts:  # un PC où rien n'est installé : chaque étape est reconnue (False), aucune n'est « inconnue » (None)
        vscode = plugins = set()
        npm = uv = windows_app = local_program_folder = staticmethod(lambda name: False)

    assert [detection._step("tool:nouvel-outil", argv, Facts()) for argv in recipe["steps"]] == [False] * 4 + [
        True,
        False,
        False,
    ]
    assert installers.display("tool:nouvel-outil")  # affichable dans la page


class Site:
    """Le fichier publié : ce que rend le dépôt des téléchargements."""

    def __init__(self):
        self.status, self.body, self.calls = 200, b"", 0

    def publish(self, data: dict, key=KEY):
        self.body = json.dumps({"catalog": extra.sign(key, "u1", data)}).encode()

    def __call__(self, request):
        self.calls += 1
        return httpx.Response(self.status, content=self.body)


def test_lecture_en_ligne_jamais_de_retour_en_arriere_et_le_dernier_recu_revient_au_lancement():
    site, store = Site(), DocumentStore(":memory:")
    updater = extra.CatalogUpdater(store, transport=httpx.MockTransport(site))
    site.publish(content())
    assert updater.refresh()["serial"] == 2 and marketplace.mcp_server("meteo")
    # un ancien fichier, bien signé, rejoué : ignoré
    site.publish(content(serial=1, mcp=[], apps=[], tools=[], removed=[]))
    assert "plus ancien" in updater.refresh()["error"] and marketplace.mcp_server("meteo")
    # fichier contrefait, illisible, trop gros, serveur en panne : le catalogue reçu est gardé
    site.publish(content(serial=5), key=Ed25519PrivateKey.generate())
    assert "invalide" in updater.refresh()["error"]
    for body, status in (
        (b"<html>", 200),
        (b"{}", 200),
        (b"[" * 200_000, 200),
        (b"x" * (extra.MAX_BYTES + 10), 200),
        (b"", 404),
        (b"", 500),
    ):
        site.body, site.status = body, status
        assert updater.refresh().get("error") and extra.status()["serial"] == 2
    # au lancement suivant, sans réseau : le dernier supplément reçu est revérifié puis appliqué
    extra.apply(None)
    offline = extra.CatalogUpdater(store, transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    assert offline.load()["serial"] == 2 and marketplace.mcp_server("meteo")
    # réglage modifié sur le disque : ignoré, catalogue du programme
    saved = store.get_setting(extra.SETTING)
    store.set_setting(extra.SETTING, {**saved, "token": saved["token"][:-6] + "AAAAAA"})
    assert offline.load()["serial"] == 0
    with pytest.raises(marketplace.MarketplaceError):
        marketplace.mcp_server("meteo")
    # une publication plus récente passe
    site.status = 200
    site.publish(content(serial=3, removed=[]))
    assert updater.refresh()["serial"] == 3 and not marketplace.RETIRED


def test_outil_de_signature_refuse_une_autre_cle_et_un_serial_qui_recule(tmp_path, capsys):
    from cryptography.hazmat.primitives import serialization

    key = tmp_path / "cle.key"
    key.write_bytes(
        KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    source, out = tmp_path / "supplement.json", tmp_path / "catalogue.json"
    source.write_text(json.dumps(content()), encoding="utf-8")
    assert extra.main(["check", str(source)]) == 0
    assert extra.main(["sign", "--key", str(key), "--in", str(source), "--out", str(out)]) == 0
    assert extra.main(["show", str(out)]) == 0 and "serial 2" in capsys.readouterr().out
    assert extra.main(["sign", "--key", str(key), "--in", str(source), "--out", str(out)]) == 2  # même serial
    assert "doit dépasser 2" in capsys.readouterr().out
    other = tmp_path / "autre.key"
    other.write_bytes(Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))  # fmt: skip
    source.write_text(json.dumps(content(serial=3)), encoding="utf-8")
    assert extra.main(["sign", "--key", str(other), "--in", str(source), "--out", str(tmp_path / "x.json")]) == 2
    source.write_text(json.dumps(_with("tools.0.recipe.steps.0.version", "latest")), encoding="utf-8")
    assert extra.main(["check", str(source)]) == 2


def test_le_fichier_source_du_depot_est_valide_et_la_cle_est_celle_des_mises_a_jour():
    assert extra.SOURCE.is_file() and extra.parse(json.loads(extra.SOURCE.read_text(encoding="utf-8")))["serial"] >= 1
    assert release.PUBLIC_KEYS == {"u1": "sx2FdJpBK64Dg9JVSUiqxfNKKXb4ESgrAajjeWrLgqg="}
    assert extra.URL.startswith("https://raw.githubusercontent.com/Lezardu29/lezard-ia-telecharger/")


def test_dans_l_application_fiches_visibles_installation_refusee_si_retiree_relecture_reservee_au_proprietaire(
    tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient

    from gateway.main import create_app
    from gateway.plans import PlanResolver

    monkeypatch.setenv("MCP_CONFIG", str(tmp_path / "mcp.json"))
    store = DocumentStore(":memory:")
    store.set_setting(extra.SETTING, {"token": extra.sign(KEY, "u1", content()), "serial": 2})
    app = create_app(
        None,
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))),
        plans=PlanResolver({"compte": "free"}, "free", master_key="master"),
        store=store,
    )
    master, account = {"Authorization": "Bearer master"}, {"Authorization": "Bearer compte"}
    with TestClient(app) as c:
        public = c.get("/v1/marketplace").json()  # visiteurs : les fiches, sans rien de ce PC
        assert "meteo" in [s["id"] for s in public["mcp"]] and "nouvel-outil" in [
            t["id"] for t in public["agent_tools"]
        ]
        seen = c.get("/v1/admin/marketplace", headers=master).json()
        assert seen["extra"]["serial"] == 2 and seen["extra"]["removed"] == {
            "tool:repomix": "paquet compromis le 2027-01-02."
        }
        tool = next(t for t in seen["agent_tools"] if t["id"] == "nouvel-outil")
        assert tool["one_click"] and tool["steps"] == ["npm install --global outil-cli@3.1.0"] and tool["uninstall"]
        assert next(t for t in seen["agent_tools"] if t["id"] == "repomix")["retired"] is True
        assert c.post("/v1/admin/marketplace/tools/repomix/install", headers=master).status_code == 410
        assert c.post("/v1/admin/marketplace/refresh", headers=account).status_code == 403
        assert c.post("/v1/admin/marketplace/refresh").status_code in (401, 403)
        newer = extra.sign(KEY, "u1", content(serial=4, tools=[], removed=[]))
        monkeypatch.setattr(extra.CatalogUpdater, "_fetch", lambda self: newer)
        r = c.post("/v1/admin/marketplace/refresh", headers=master).json()
        assert r["serial"] == 4 and r["last"]["serial"] == 4 and not r["removed"]
        ids = [t["id"] for t in c.get("/v1/admin/marketplace", headers=master).json()["agent_tools"]]
        assert "nouvel-outil" not in ids and "repomix" in ids


def test_un_serveur_mcp_retire_n_est_plus_lance_meme_deja_declare(tmp_path, monkeypatch):
    import asyncio

    from gateway import mcp

    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps({"servers": {"memoire": {"command": "introuvable-xyz", "args": []}}}), encoding="utf-8"
    )
    monkeypatch.setenv("MCP_CONFIG", str(config))
    started = []

    async def connect(name, cfg):
        started.append(name)
        return 3

    monkeypatch.setattr(mcp, "_connect", connect)
    extra.apply(extra.parse(content(removed=[{"key": "mcp:memoire", "reason": "paquet compromis."}])))
    asyncio.run(mcp.load())
    assert started == [] and "Retiré du catalogue" in mcp.STATUS["memoire"]["error"]
    extra.apply(None)
    asyncio.run(mcp.load())
    assert started == ["memoire"]
    mcp.STATUS.clear()
