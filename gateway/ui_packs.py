"""Interfaces personnalisées : un .zip importé dans le panneau Apparence habille Lezard IA (styles, images, polices).

Contenu d'un pack (voir le guide des connecteurs, static/guide-interface.md, et le modèle /ui-packs/modele.zip) :
- manifest.json : {"name", "version", "description", "author", "interface": "claude" | "classic"} ; name requis ;
- style.css : la feuille appliquée, chargée APRÈS toutes celles de Lezard IA, sur toutes les pages ;
- d'autres feuilles (@import relatif), des images et des polices, référencées par des adresses relatives.

Sécurité : jamais de script ni de page (.js, .html… refusés) : un script importé tournerait dans la page et lirait
la clé de connexion. Aucune adresse externe dans les feuilles (la CSP des pages les bloquerait de toute façon, sans
le dire). Les fichiers sont servis avec une CSP « sandbox » : une image SVG ouverte directement n'exécute rien.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import PurePosixPath

MAX_ZIP = 5 * 1024 * 1024  # .zip importé
MAX_TOTAL = 10 * 1024 * 1024  # contenu décompressé (bombe zip : vérifié avant de lire)
MAX_FILE = 4 * 1024 * 1024
MAX_FILES = 100
MAX_PACKS = 5  # par compte
INTERFACES = ("claude", "classic")
TYPES = {
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".json": "application/json",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}
# fichiers servis : ni script, ni requête vers l'extérieur, même ouverts directement dans un onglet
FILE_CSP = "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; font-src 'self'; sandbox"
_SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$")
_PACK_ID = re.compile(r"^pk_[0-9a-f]{24}$")
# url(http…), url(//…), @import "https://…" : adresses externes ; javascript: / expression() : vieux pièges
_EXTERNAL = re.compile(
    r"url\(\s*['\"]?\s*(https?:|//|javascript:)|@import\s+(url\()?\s*['\"]?\s*(https?:|//)|expression\s*\(", re.I
)
IGNORED = ("__MACOSX/", ".DS_Store", "Thumbs.db")


class PackError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def valid_id(pack_id: str) -> bool:
    return isinstance(pack_id, str) and bool(_PACK_ID.match(pack_id))


def content_type(path: str) -> str:
    return TYPES[PurePosixPath(path).suffix.lower()]


def _clean_path(name: str) -> str:
    parts = name.replace("\\", "/").split("/")
    if not all(_SEGMENT.match(p) for p in parts):
        raise PackError(
            400, f"Nom de fichier refusé dans le .zip : « {name[:80]} » (lettres, chiffres, - _ . seulement)."
        )
    return "/".join(parts)


def _manifest(raw: bytes) -> dict:
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise PackError(400, f"manifest.json illisible : {e}.") from None
    if not isinstance(data, dict):
        raise PackError(400, "manifest.json : un objet JSON est attendu.")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 60:
        raise PackError(400, "manifest.json : « name » (1 à 60 caractères) est requis.")
    out = {"name": name.strip()}
    for key in ("version", "description", "author"):
        value = data.get(key)
        if value is not None:
            if not isinstance(value, str) or len(value) > 200:
                raise PackError(400, f"manifest.json : « {key} » doit être un texte de 200 caractères au plus.")
            out[key] = value.strip()
    ui = data.get("interface")
    if ui is not None:
        if ui not in INTERFACES:
            raise PackError(400, "manifest.json : « interface » vaut « claude » ou « classic ».")
        out["interface"] = ui
    return out


def read_pack(data: bytes) -> tuple[dict, dict[str, bytes]]:
    """Vérifie un .zip ; renvoie (manifeste, {chemin: contenu})."""
    if len(data) > MAX_ZIP:
        raise PackError(413, f"Pack trop lourd : {MAX_ZIP // (1024 * 1024)} Mo au plus.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise PackError(400, "Ce fichier n'est pas un .zip valide.") from None
    infos = [
        i
        for i in archive.infolist()
        if not i.is_dir()
        and not any(i.filename.replace("\\", "/").startswith(x) or i.filename.endswith(x) for x in IGNORED)
    ]
    if len(infos) > MAX_FILES:
        raise PackError(413, f"{MAX_FILES} fichiers au plus dans un pack.")
    if sum(i.file_size for i in infos) > MAX_TOTAL or any(i.file_size > MAX_FILE for i in infos):
        raise PackError(413, f"Pack trop volumineux une fois décompressé ({MAX_TOTAL // (1024 * 1024)} Mo au plus).")
    names = [_clean_path(i.filename) for i in infos]
    # zip d'un dossier (« mon-theme/manifest.json ») : le dossier racine est retiré
    roots = {n.split("/")[0] for n in names}
    if "manifest.json" not in names and len(roots) == 1 and all("/" in n for n in names):
        names = [n.split("/", 1)[1] for n in names]
    files: dict[str, bytes] = {}
    for info, path in zip(infos, names):
        suffix = PurePosixPath(path).suffix.lower()
        if suffix not in TYPES:
            raise PackError(
                400,
                f"Fichier refusé : « {path} ». Un pack ne contient que des feuilles de style, des images et des "
                "polices (jamais de script ni de page : ils pourraient lire votre clé).",
            )
        with archive.open(info) as f:
            content = f.read(MAX_FILE + 1)
        if len(content) > MAX_FILE:
            raise PackError(413, f"« {path} » est trop lourd.")
        files[path] = content
    if "manifest.json" not in files:
        raise PackError(400, "manifest.json manquant à la racine du .zip (voir le modèle).")
    if "style.css" not in files:
        raise PackError(400, "style.css manquant à la racine du .zip (voir le modèle).")
    for path, content in files.items():
        if not path.endswith(".css"):
            continue
        try:
            css = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise PackError(400, f"« {path} » doit être en UTF-8.") from None
        if _EXTERNAL.search(css):
            raise PackError(
                400,
                f"« {path} » fait appel à une adresse externe (url(https://…), @import https://…). Mettez les images "
                "et les polices dans le .zip et utilisez des adresses relatives, par exemple url(assets/fond.png).",
            )
    return _manifest(files["manifest.json"]), files


def template_zip(folder, guide: bytes) -> bytes:
    """Pack d'exemple (gateway/web/ui-pack-modele) avec le guide des connecteurs, à modifier puis réimporter."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(folder.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(folder).as_posix())
        z.writestr("GUIDE.md", guide)
    return buf.getvalue()
