"""Sauvegarde et restauration locales de la base (tâche 18.2).

Sauvegarde : copie À CHAUD par l'API de sauvegarde de SQLite (instantané cohérent, journal WAL inclus, sans
bloquer les requêtes), vérifiée (quick_check), écrite sous un nom temporaire puis renommée. Une par jour au plus
(GATEWAY_BACKUP=off pour couper), dans $GATEWAY_BACKUP_DIR (défaut : dossier backups/ à côté de la base).
Rotation des sauvegardes AUTOMATIQUES : la plus récente de chacun des 7 derniers jours ayant une sauvegarde, et
de chacune des 4 dernières semaines. Les autres (manuelles, « avant-restauration ») : les 10 plus récentes, jamais
supprimées par la rotation quotidienne — sinon une sauvegarde faite le même jour effacerait la copie d'avant une
restauration, seul moyen de revenir en arrière. Une sauvegarde sur le même disque que la base ne protège pas d'une panne de ce disque : l'admin le signale.

La clé de chiffrement n'est JAMAIS copiée avec la base (9.2) : elle s'exporte à part, protégée par une phrase de
passe (crypto.wrap_key). Sans elle, une base restaurée garde ses clés virtuelles mais pas les clés des fournisseurs.

Restauration DIFFÉRÉE : la sauvegarde choisie est vérifiée puis préparée (<base>.restore-pending), et appliquée au
prochain démarrage, avant l'ouverture de la base — jamais à chaud : limites, clés et fournisseurs sont gardés en
mémoire depuis la base, et la remplacer sous eux les rendrait incohérents. L'état d'avant est d'abord sauvegardé
(étiquette « avant-restauration »), puis remplacé.

Ligne de commande (sur le PC) :
    python -m gateway.backup create | list
    python -m gateway.backup restore <nom ou fichier>       appliquée au prochain démarrage de la gateway
    python -m gateway.backup export-key <fichier.json>      clé de chiffrement, phrase de passe demandée
    python -m gateway.backup import-key <fichier.json> [--force]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as dt
import getpass
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

from .crypto import Cipher, CryptoError, derive_key, key_file_path, unwrap_key

log = logging.getLogger("gateway")

NAME = re.compile(r"^gateway-(\d{8}-\d{6})(?:-([a-z][a-z-]{0,29}))?\.db$")
KEEP_DAILY, KEEP_WEEKLY, KEEP_OTHER = 7, 4, 10
INTERVAL = 24 * 3600  # une sauvegarde automatique par jour au plus
CHECK_EVERY = 3600  # vérification de l'échéance (un PC éteint la nuit rattrape au réveil)
REQUIRED_TABLES = {"api_keys", "settings", "provider_credentials"}


class BackupError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def backup_dir(db_path: str) -> Path:
    env = os.getenv("GATEWAY_BACKUP_DIR")
    return Path(env) if env else Path(os.path.abspath(db_path)).parent / "backups"


def auto_enabled() -> bool:
    return os.getenv("GATEWAY_BACKUP", "on").strip().lower() not in ("off", "0", "false", "no")


def pending_path(db_path: str) -> Path:
    return Path(db_path + ".restore-pending")


def _stamp(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y%m%d-%H%M%S")


def _snapshot(src: str | Path, dest: Path) -> None:
    """Instantané cohérent de `src` dans `dest` (fichier autonome, sans -wal), vérifié avant d'être visible."""
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.unlink(missing_ok=True)
    source = sqlite3.connect(str(src), timeout=30)
    try:
        target = sqlite3.connect(str(tmp))
        try:
            source.backup(target)
            target.execute("PRAGMA journal_mode = DELETE")
            check = target.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            target.close()
    finally:
        source.close()
    if check != "ok":
        tmp.unlink(missing_ok=True)
        raise BackupError(500, f"Sauvegarde corrompue à la vérification : {check}")
    os.replace(tmp, dest)


def _info(path: Path) -> dict:
    stamp, label = NAME.match(path.name).groups()
    return {
        "name": path.name,
        "label": label or "",
        "created_at": dt.datetime.strptime(stamp, "%Y%m%d-%H%M%S").timestamp(),
        "size_bytes": path.stat().st_size,
    }


def inspect_file(path: Path, cipher: Cipher | None = None) -> dict:
    """Une sauvegarde est-elle restaurable ? Intégrité, tables, et clé de chiffrement de ses secrets."""
    if not path.is_file():
        return {"ok": False, "problem": f"Fichier introuvable : {path}"}
    try:
        # lecture seule stricte : rien n'est créé ni modifié, même sur un fichier qui n'est pas une base
        con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            integrity = con.execute("PRAGMA quick_check").fetchone()[0]
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = sorted(REQUIRED_TABLES - tables)
            keys = con.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0] if not missing else 0
            creds = (
                con.execute("SELECT provider, key_fingerprint FROM provider_credentials").fetchall()
                if not missing
                else []
            )
        finally:
            con.close()
    except sqlite3.DatabaseError as e:
        return {"ok": False, "problem": f"Fichier illisible : ce n'est pas une base de la gateway ({e})."}
    problem = (
        f"Base endommagée : {integrity}"
        if integrity != "ok"
        else f"Tables manquantes : {', '.join(missing)}"
        if missing
        else None
    )
    other = sorted(p for p, fp in creds if cipher and fp != cipher.fingerprint)
    return {
        "ok": problem is None,
        "problem": problem,
        "keys": keys,
        "credentials": len(creds),
        # clés API de fournisseurs chiffrées avec une AUTRE clé : illisibles sans import-key
        "credentials_other_key": other,
    }


class BackupManager:
    def __init__(self, store, directory: str | Path | None = None, clock=time.time):
        self.db: str = store.path
        self.available = self.db != ":memory:"
        self.dir = Path(directory) if directory else backup_dir(self.db) if self.available else None
        self.clock = clock
        self._lock = threading.Lock()

    def _require(self) -> None:
        if not self.available:
            raise BackupError(409, "Base en mémoire : aucune sauvegarde possible.")

    def _path(self, name: str) -> Path:
        self._require()
        if not NAME.match(name or ""):  # jamais de chemin fourni par le client (../, C:\…)
            raise BackupError(400, "Nom de sauvegarde invalide.")
        path = self.dir / name
        if not path.is_file():
            raise BackupError(404, f"Sauvegarde « {name} » introuvable.")
        return path

    # ----- sauvegarde -----
    def create(self, label: str = "manuelle") -> dict:
        self._require()
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            dest = self.dir / f"gateway-{_stamp(self.clock())}-{label}.db"
            if dest.exists():
                raise BackupError(409, "Une sauvegarde vient d'être faite à l'instant.")
            _snapshot(self.db, dest)
            self.prune()
            log.info("Sauvegarde de la base : %s", dest)
            return _info(dest)

    def list(self) -> list[dict]:
        if not self.available or not self.dir.is_dir():
            return []
        found = [_info(p) for p in self.dir.iterdir() if p.is_file() and NAME.match(p.name)]
        return sorted(found, key=lambda b: (b["created_at"], b["name"]), reverse=True)

    def prune(self) -> list[str]:
        """Automatiques : la plus récente de chacun des KEEP_DAILY derniers jours et des KEEP_WEEKLY dernières
        semaines. Manuelles et « avant-restauration » : les KEEP_OTHER plus récentes."""
        days, weeks = [], []
        backups = self.list()
        keep = {b["name"] for b in [b for b in backups if b["label"] != "auto"][:KEEP_OTHER]}
        for b in (b for b in backups if b["label"] == "auto"):
            when = dt.datetime.fromtimestamp(b["created_at"])
            day, week = when.date(), when.isocalendar()[:2]
            if day not in days and len(days) < KEEP_DAILY:
                days.append(day)
                keep.add(b["name"])
            if week not in weeks and len(weeks) < KEEP_WEEKLY:
                weeks.append(week)
                keep.add(b["name"])
        removed = [b["name"] for b in backups if b["name"] not in keep]
        for name in removed:
            (self.dir / name).unlink(missing_ok=True)
        return removed

    def due(self) -> bool:
        latest = self.list()
        return not latest or self.clock() - latest[0]["created_at"] >= INTERVAL

    def same_disk(self) -> bool | None:
        """Sauvegardes sur le disque de la base : inutiles si ce disque lâche."""
        if not self.available:
            return None
        try:
            probe = self.dir if self.dir.exists() else self.dir.parent
            return os.stat(probe).st_dev == os.stat(os.path.dirname(os.path.abspath(self.db))).st_dev
        except OSError:
            return None

    async def run(self) -> None:
        while True:
            try:
                if self.due():
                    await asyncio.to_thread(self.create, "auto")
            except Exception:  # disque plein, dossier retiré… : on réessaiera
                log.exception("Sauvegarde automatique impossible")
            await asyncio.sleep(CHECK_EVERY)

    # ----- restauration (différée au prochain démarrage) -----
    def inspect(self, name: str, cipher: Cipher | None = None) -> dict:
        return {**_info(self._path(name)), **inspect_file(self._path(name), cipher)}

    def stage_restore(self, name: str, cipher: Cipher | None = None, source: Path | None = None) -> dict:
        path = source or self._path(name)
        report = inspect_file(path, cipher)
        if not report["ok"]:
            raise BackupError(422, f"Restauration refusée : {report['problem']}")
        pending = pending_path(self.db)
        with self._lock:
            tmp = pending.with_name(pending.name + ".tmp")
            shutil.copyfile(path, tmp)
            os.replace(tmp, pending)
            Path(str(pending) + ".json").write_text(
                json.dumps({"source": name, "staged_at": self.clock()}), encoding="utf-8"
            )
        log.warning("Restauration de « %s » préparée : appliquée au prochain démarrage de la gateway.", name)
        return {**report, "pending": self.pending(), "restart_required": True}

    def pending(self) -> dict | None:
        if not self.available or not pending_path(self.db).is_file():
            return None
        try:
            return json.loads(Path(str(pending_path(self.db)) + ".json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"source": None, "staged_at": None}

    def cancel_restore(self) -> bool:
        if not self.available:
            return False
        with self._lock:
            pending = pending_path(self.db)
            found = pending.is_file()
            pending.unlink(missing_ok=True)
            Path(str(pending) + ".json").unlink(missing_ok=True)
        return found


def apply_pending_restore(db_path: str, clock=time.time) -> dict | None:
    """Au démarrage, AVANT d'ouvrir la base : remplace la base par la sauvegarde préparée, après avoir sauvegardé
    l'état actuel. Rien à faire sans restauration en attente."""
    pending = pending_path(db_path)
    if not pending.is_file():
        return None
    meta_path = Path(str(pending) + ".json")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    safety = None
    if os.path.exists(db_path):
        directory = backup_dir(db_path)
        directory.mkdir(parents=True, exist_ok=True)
        safety = directory / f"gateway-{_stamp(clock())}-avant-restauration.db"
        try:
            _snapshot(db_path, safety)
        except (sqlite3.DatabaseError, BackupError):  # base actuelle illisible : copie brute, par précaution
            shutil.copyfile(db_path, safety)
    for suffix in ("", "-wal", "-shm"):
        Path(db_path + suffix).unlink(missing_ok=True)
    os.replace(pending, db_path)
    meta_path.unlink(missing_ok=True)
    result = {"source": meta.get("source"), "safety_backup": safety.name if safety else None, "at": clock()}
    log.warning(
        "Sauvegarde « %s » restaurée. État précédent conservé dans « %s ».", result["source"], result["safety_backup"]
    )
    return result


# ---------- ligne de commande ----------
def _passphrase(confirm: bool) -> str:
    first = getpass.getpass("Phrase de passe : ")
    if confirm and getpass.getpass("Confirmez la phrase de passe : ") != first:
        raise SystemExit("Les deux phrases de passe diffèrent.")
    return first


def _import_key(path: str, force: bool) -> str:
    key = unwrap_key(json.loads(Path(path).read_text(encoding="utf-8")), _passphrase(confirm=False))
    encoded = base64.b64encode(key).decode()
    fingerprint = Cipher(key).fingerprint
    if os.getenv("GATEWAY_ENCRYPTION_KEY"):
        return (
            "GATEWAY_ENCRYPTION_KEY est défini : il a priorité sur le fichier de clé. Remplacez sa valeur "
            f"dans .env par :\n{encoded}\n(empreinte {fingerprint})"
        )
    target = Path(key_file_path())
    if target.exists():
        if Cipher(derive_key(target.read_text(encoding="ascii"))).fingerprint == fingerprint:
            return f"Clé déjà en place ({target}, empreinte {fingerprint})."
        if not force:
            raise SystemExit(
                f"{target} contient une AUTRE clé : les secrets chiffrés avec elle deviendraient "
                "illisibles. Relancez avec --force pour la remplacer (elle est mise de côté)."
            )
        target.rename(target.with_name(f"{target.name}.old-{_stamp(time.time())}"))
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(encoded)
    return f"Clé importée dans {target} (empreinte {fingerprint}). Redémarrez la gateway."


def main(argv: list[str] | None = None) -> None:
    from .store import DocumentStore, default_db_path

    parser = argparse.ArgumentParser(prog="python -m gateway.backup", description=(__doc__ or "").split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("create", help="sauvegarde maintenant")
    sub.add_parser("list", help="liste les sauvegardes")
    rst = sub.add_parser("restore", help="prépare une restauration, appliquée au prochain démarrage")
    rst.add_argument("backup", help="nom d'une sauvegarde de la liste, ou chemin d'un fichier .db")
    rst.add_argument("--cancel", action="store_true", help="annule la restauration en attente")
    exp = sub.add_parser("export-key", help="exporte la clé de chiffrement (phrase de passe)")
    exp.add_argument("file")
    imp = sub.add_parser("import-key", help="installe une clé exportée")
    imp.add_argument("file")
    imp.add_argument("--force", action="store_true", help="remplace une autre clé déjà présente")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "export-key":
            data = Cipher().export_key(_passphrase(confirm=True))
            Path(args.file).write_text(json.dumps(data, indent=2), encoding="utf-8")
            print(
                f"Clé exportée dans {args.file} (empreinte {data['fingerprint']}). Rangez ce fichier À PART de "
                "la base (clé USB, gestionnaire de mots de passe) et retenez la phrase de passe."
            )
            return
        if args.cmd == "import-key":
            print(_import_key(args.file, args.force))
            return
        store = DocumentStore(default_db_path())
        try:
            manager = BackupManager(store)
            if args.cmd == "create":
                print(f"Sauvegarde créée : {manager.dir / manager.create()['name']}")
            elif args.cmd == "list":
                for b in manager.list() or []:
                    when = dt.datetime.fromtimestamp(b["created_at"]).strftime("%d/%m/%Y %H:%M")
                    print(f"{b['name']}  {when}  {b['size_bytes'] / 1e6:.1f} Mo")
                print(f"Dossier : {manager.dir}")
            elif args.cmd == "restore" and args.cancel:
                print("Restauration annulée." if manager.cancel_restore() else "Aucune restauration en attente.")
            else:
                source = Path(args.backup)
                if NAME.match(args.backup) and not source.is_file():
                    report = manager.stage_restore(args.backup, Cipher())
                else:  # fichier venu d'ailleurs (disque externe, NAS…)
                    report = manager.stage_restore(source.name, Cipher(), source=source)
                if report["credentials_other_key"]:
                    print(
                        "Attention : clés API chiffrées avec une autre clé de chiffrement "
                        f"({', '.join(report['credentials_other_key'])}) : importez-la (import-key)."
                    )
                print(
                    f"Restauration préparée ({report['keys']} clé(s) virtuelle(s)) : redémarrez la gateway "
                    "pour l'appliquer. L'état actuel sera sauvegardé avant d'être remplacé."
                )
        finally:
            store.close()
    except (BackupError, CryptoError) as e:
        sys.exit(getattr(e, "message", str(e)))


if __name__ == "__main__":
    main()
