"""Migrations numérotées du schéma SQLite (18.3).

`SCHEMA` (store.py) crée les tables manquantes ; il ne sait pas MODIFIER une table existante. Chaque changement de
structure d'une table qui existe déjà devient donc une étape numérotée de STEPS, appliquée une seule fois par base
et dans l'ordre ; la table schema_version garde l'historique (numéro, date, description).

- Chaque étape s'exécute dans une transaction : en cas d'erreur, la base reste à l'étape précédente et la gateway
  refuse de démarrer (une base à moitié migrée serait pire). Restaurer une sauvegarde (18.2) reste possible.
- Une base déjà migrée par une version PLUS RÉCENTE (retour à une version précédente) : avertissement, sans
  rien modifier ; les colonnes inconnues de l'ancienne version sont simplement ignorées.
- Modifier une colonne (type, contrainte), ce que ALTER TABLE de SQLite ne sait pas faire : rebuild_table (table
  recréée, données recopiées, index et contraintes restaurés), dans la même transaction.

Ajouter une étape : l'ajouter À LA FIN de STEPS avec le numéro suivant ; ne jamais modifier une étape publiée.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from collections.abc import Callable

log = logging.getLogger("gateway")


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Step:
    version: int
    description: str
    apply: Callable[[sqlite3.Connection], None]


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def add_columns(specs: list[tuple[str, str, str]]) -> Callable[[sqlite3.Connection], None]:
    """Colonnes ajoutées si absentes (idempotent : les bases antérieures aux migrations numérotées en ont déjà
    une partie, ajoutée par l'ancien mécanisme)."""

    def run(conn: sqlite3.Connection) -> None:
        for table, col, decl in specs:
            if col not in columns(conn, table):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    return run


def rebuild_table(conn: sqlite3.Connection, table: str, create_sql: str, copy: dict[str, str] | None = None) -> None:
    """Recrée `table` selon `create_sql` (« CREATE TABLE {table} (…) ») en recopiant ses lignes. `copy` : colonne
    nouvelle -> expression sur l'ancienne table (défaut : colonnes de même nom). Index de la table recréés."""
    old = columns(conn, table)
    indexes = [
        r[0]
        for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,)
        )
    ]
    tmp = f"_{table}_old"
    conn.execute(f"ALTER TABLE {table} RENAME TO {tmp}")
    conn.execute(create_sql)
    new = columns(conn, table)
    mapping = dict(copy or {})
    for col in new:
        if col not in mapping and col in old:
            mapping[col] = col
    if mapping:
        conn.execute(f"INSERT INTO {table} ({', '.join(mapping)}) SELECT {', '.join(mapping.values())} FROM {tmp}")
    conn.execute(f"DROP TABLE {tmp}")
    for sql in indexes:
        conn.execute(sql)


# ---------- étapes ----------
STEPS: list[Step] = [
    Step(
        1,
        "colonnes ajoutées avant les migrations numérotées (3.4, 4.2-4.4, 7.1, 17.1)",
        add_columns(
            [
                ("projects", "compression", "TEXT"),
                ("documents", "embed_model", "TEXT"),
                ("documents", "embed_dims", "INTEGER"),
                ("api_keys", "permissions", "TEXT NOT NULL DEFAULT '{}'"),
                ("api_keys", "expires_at", "REAL"),
                ("api_keys", "limits", "TEXT NOT NULL DEFAULT '{}'"),
                ("api_keys", "status_reason", "TEXT"),
                ("api_keys", "suspended_until", "REAL"),
                ("api_keys", "revoked_at", "REAL"),
                ("stripe_events", "status", "TEXT NOT NULL DEFAULT 'done'"),
            ]
        ),
    ),
    # Projets du chat : une conversation peut appartenir à un projet (instructions et fichiers communs). Pas de
    # clé étrangère (ALTER TABLE ne sait pas en ajouter) : la suppression d'un projet détache ses conversations.
    Step(2, "conversations rattachées à un projet", add_columns([("conversations", "project_id", "TEXT")])),
    # favoris du chat : épinglées en tête de la barre latérale
    Step(3, "conversations favorites", add_columns([("conversations", "starred", "INTEGER NOT NULL DEFAULT 0")])),
]
LATEST = STEPS[-1].version


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, "
        "description TEXT NOT NULL)"
    )
    return conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]


def migrate(conn: sqlite3.Connection, steps: list[Step] | None = None) -> list[int]:
    """Applique les étapes manquantes ; renvoie leurs numéros."""
    steps = STEPS if steps is None else steps
    numbers = [s.version for s in steps]
    if numbers != sorted(set(numbers)) or (numbers and numbers[0] < 1):
        raise MigrationError("Étapes de migration mal numérotées (croissantes, uniques, à partir de 1).")
    have = current_version(conn)
    conn.commit()
    latest = numbers[-1] if numbers else 0
    if have > latest:
        log.warning(
            "Base de données au schéma %d, plus récent que celui de cette version (%d) : elle a été "
            "migrée par une version plus récente de la gateway. Rien n'est modifié.",
            have,
            latest,
        )
        return []
    done = []
    for step in steps:
        if step.version <= have:
            continue
        try:
            conn.execute("BEGIN IMMEDIATE")  # DDL compris : tout ou rien
            step.apply(conn)
            conn.execute("INSERT INTO schema_version VALUES (?,?,?)", (step.version, time.time(), step.description))
            conn.execute("COMMIT")
        except Exception as e:
            conn.execute("ROLLBACK")
            raise MigrationError(
                f"Migration {step.version} (« {step.description} ») impossible : {e}. La base est "
                f"restée au schéma {have}. Restaurez une sauvegarde ou revenez à la version "
                "précédente."
            ) from e
        have = step.version
        done.append(step.version)
    if done:
        log.info("Base de données migrée au schéma %d (étapes %s).", have, ", ".join(map(str, done)))
    return done
