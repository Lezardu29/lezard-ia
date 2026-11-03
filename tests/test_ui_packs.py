"""Interfaces personnalisées (ui_packs.py) : .zip importé dans le panneau Apparence, jamais de script ; modèle et
guide des connecteurs (static/guide-interface.md) fournis."""

import io
import re
import zipfile

import pytest

from gateway import preferences, ui_packs
from gateway.core import WEB_DIR
from gateway.ui_packs import PackError
from tests.test_conversations import ACCOUNT, make

MANIFEST = b'{"name": "Test", "version": "1.0", "interface": "classic"}'
CSS = b":root { --accent: #2d6a4f !important; } body { background: url(assets/fond.svg); }"
GUIDE = (WEB_DIR / "static" / "guide-interface.md").read_text(encoding="utf-8")


def zipped(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def good(**extra):
    return zipped({"manifest.json": MANIFEST, "style.css": CSS, "assets/fond.svg": b"<svg/>", **extra})


# ---------- vérification du .zip ----------
def test_a_valid_pack_is_read_with_its_manifest():
    manifest, files = ui_packs.read_pack(good())
    assert manifest == {"name": "Test", "version": "1.0", "interface": "classic"}
    assert set(files) == {"manifest.json", "style.css", "assets/fond.svg"}


def test_a_zipped_folder_loses_its_root():
    data = zipped({"theme/manifest.json": MANIFEST, "theme/style.css": CSS, "__MACOSX/theme/._style.css": b"x"})
    assert set(ui_packs.read_pack(data)[1]) == {"manifest.json", "style.css"}


@pytest.mark.parametrize(
    ("extra", "words"),
    [
        ({"app.js": b"alert(1)"}, "jamais de script"),
        ({"page.html": b"<script>"}, "jamais de script"),
        ({"autre.css": b"@import 'https://evil.example/x.css';"}, "adresse externe"),
        ({"autre.css": b"a { background: url(//evil.example/x.png) }"}, "adresse externe"),
        ({"autre.css": b"a { background: url( 'https://x.example/p.png') }"}, "adresse externe"),
        ({"../style.css": CSS}, "refusé"),
        ({"assets/mon fond.png": b"x"}, "refusé"),
    ],
)
def test_scripts_pages_external_urls_and_bad_paths_are_refused(extra, words):
    with pytest.raises(PackError) as e:
        ui_packs.read_pack(good(**extra))
    assert words in e.value.message


def test_manifest_and_style_are_required_and_checked():
    for files, words in (
        ({"style.css": CSS}, "manifest.json manquant"),
        ({"manifest.json": MANIFEST}, "style.css manquant"),
        ({"manifest.json": b"{}", "style.css": CSS}, "« name »"),
        ({"manifest.json": b'{"name": "A", "interface": "autre"}', "style.css": CSS}, "interface"),
        ({"manifest.json": b"pas du json", "style.css": CSS}, "illisible"),
    ):
        with pytest.raises(PackError) as e:
            ui_packs.read_pack(zipped(files))
        assert words in e.value.message
    with pytest.raises(PackError):
        ui_packs.read_pack(b"pas un zip")


def test_size_limits_hold_before_decompressing(monkeypatch):
    monkeypatch.setattr(ui_packs, "MAX_TOTAL", 100)
    with pytest.raises(PackError) as e:
        ui_packs.read_pack(good(**{"assets/gros.png": b"0" * 200}))  # compressé petit, décompressé trop gros
    assert e.value.status == 413
    monkeypatch.setattr(ui_packs, "MAX_FILES", 2)
    with pytest.raises(PackError):
        ui_packs.read_pack(good())


# ---------- API ----------
def upload(c, data, headers=ACCOUNT):
    return c.post("/v1/ui-packs", headers=headers, files={"file": ("pack.zip", data, "application/zip")})


def test_upload_list_serve_and_delete():
    c, _ = make()
    with c:
        r = upload(c, good())
        assert r.status_code == 201
        pack = r.json()
        assert re.match(r"^pk_[0-9a-f]{24}$", pack["id"]) and pack["name"] == "Test" and pack["files"] == 3
        assert [p["id"] for p in c.get("/v1/ui-packs", headers=ACCOUNT).json()["data"]] == [pack["id"]]
        css = c.get(f"/ui-packs/{pack['id']}/style.css")  # sans clé : une feuille ne peut pas en envoyer
        assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
        assert (
            "sandbox" in css.headers["content-security-policy"] and css.headers["x-content-type-options"] == "nosniff"
        )
        svg = c.get(f"/ui-packs/{pack['id']}/assets/fond.svg")
        assert svg.headers["content-type"] == "image/svg+xml" and "sandbox" in svg.headers["content-security-policy"]
        assert c.get(f"/ui-packs/{pack['id']}/inconnu.css").status_code == 404
        assert c.get(f"/ui-packs/{pack['id']}/manifest.js").status_code == 404
        assert c.get("/ui-packs/pk_pasbon/style.css").status_code == 404
        assert c.delete(f"/v1/ui-packs/{pack['id']}", headers=ACCOUNT).json()["deleted"] is True
        assert c.get(f"/ui-packs/{pack['id']}/style.css").status_code == 404


def test_refusals_reach_the_panel_and_packs_are_limited():
    c, _ = make()
    with c:
        r = upload(c, good(**{"app.js": b"x"}))
        assert r.status_code == 400 and "script" in r.json()["error"]["message"]
        assert upload(c, good(), headers={}).status_code == 403  # sans clé : rien n'est enregistré
        for _ in range(ui_packs.MAX_PACKS):
            assert upload(c, good()).status_code == 201
        assert upload(c, good()).status_code == 409


def test_packs_belong_to_their_account():
    c, _ = make()
    with c:
        pack = upload(c, good()).json()
        master = {"Authorization": "Bearer gw-master-0123456789abcdefghijklmnop"}
        assert c.get("/v1/ui-packs", headers=master).json()["data"] == []
        assert c.delete(f"/v1/ui-packs/{pack['id']}", headers=master).status_code == 404


def test_the_template_downloads_imports_and_carries_the_guide():
    c, _ = make()
    with c:
        r = c.get("/ui-packs/modele.zip")
        assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
        names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
        assert {"manifest.json", "style.css", "assets/motif.svg", "GUIDE.md"} <= set(names)
        assert upload(c, r.content).status_code == 201  # le modèle s'importe tel quel
        assert c.get("/static/guide-interface.md").status_code == 200


# ---------- thème ----------
def test_the_theme_carries_the_chosen_pack():
    theme = preferences.validate_theme({"mode": "auto", "preset": "claude", "pack": "pk_" + "a" * 24})
    assert theme["pack"] == "pk_" + "a" * 24
    with pytest.raises(preferences.PreferenceError):
        preferences.validate_theme({"mode": "auto", "preset": "claude", "pack": "../x"})


def test_theme_js_loads_the_pack_last_and_offers_a_way_out():
    theme = (WEB_DIR / "static" / "theme.js").read_text(encoding="utf-8")
    panel = (WEB_DIR / "static" / "theme-panel.js").read_text(encoding="utf-8")
    assert '"/ui-packs/" + pack + "/style.css"' in theme and "appendChild(link)" in theme
    assert 'askedPack === "aucun"' in theme and "?pack=aucun" in GUIDE
    assert "pack: cur.pack" in theme and "pack: cur.pack" in panel  # gardé quand on change d'interface ou de couleur
    assert 'href: "/ui-packs/modele.zip"' in panel and 'href: "/static/guide-interface.md"' in panel
    assert "innerHTML" not in panel.split("// ---------- interfaces personnalisées")[1].split("function open")[0]


# ---------- guide : chaque connecteur cité existe ----------
def test_every_id_in_the_guide_exists_in_the_pages():
    pages = "".join((WEB_DIR / p).read_text(encoding="utf-8") for p in ("app.html", "share.html"))
    ids = set(re.findall(r"`#([a-z][a-z0-9-]*)", GUIDE)) | set(re.findall(r", `#([a-z][a-z0-9-]*)`", GUIDE))
    ids |= set(re.findall(r"`#([a-z][a-z0-9-]*)`", GUIDE))
    assert len(ids) > 60
    missing = sorted(i for i in ids if f'id="{i}"' not in pages)
    assert not missing, f"connecteurs du guide absents des pages : {missing}"


def test_every_css_variable_in_the_guide_is_used():
    sheets = "".join(p.read_text(encoding="utf-8") for p in (WEB_DIR / "static").glob("*.css"))
    sheets += (WEB_DIR / "static" / "theme.js").read_text(encoding="utf-8")
    for var in set(re.findall(r"`(--[a-z0-9-]+)`", GUIDE)):
        assert var in sheets, var
