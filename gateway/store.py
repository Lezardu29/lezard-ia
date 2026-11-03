"""Stockage SQLite des documents et de leurs chunks (tâche 3.1).

Chaque document appartient à une identité (clé client ou IP) : un client ne voit
que ses propres documents. Chemin : $GATEWAY_DB (défaut data/gateway.db).
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
import uuid

from .migrations import migrate
from .documents import Chunk

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, filename TEXT NOT NULL, kind TEXT NOT NULL,
    language TEXT, size_bytes INTEGER NOT NULL, n_chunks INTEGER NOT NULL, tokens INTEGER NOT NULL,
    chunk_size INTEGER NOT NULL, chunk_overlap INTEGER NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_owner ON documents(owner);
CREATE TABLE IF NOT EXISTS chunks (
    doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE, idx INTEGER NOT NULL,
    text TEXT NOT NULL, tokens INTEGER NOT NULL, meta TEXT NOT NULL, PRIMARY KEY (doc_id, idx)
);
CREATE TABLE IF NOT EXISTS chunk_vectors (
    doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE, idx INTEGER NOT NULL,
    vec BLOB NOT NULL, PRIMARY KEY (doc_id, idx)           -- float32 normalisés (17.1)
);
CREATE TABLE IF NOT EXISTS prompts (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, description TEXT,
    content TEXT NOT NULL, variables TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS prompts_owner ON prompts(owner);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, description TEXT,
    system_prompt TEXT, prompt_id TEXT REFERENCES prompts(id), prompt_vars TEXT NOT NULL DEFAULT '{}',
    model TEXT, mode TEXT, effort TEXT, documents_budget INTEGER, max_context_tokens INTEGER,
    max_output_tokens INTEGER, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS projects_owner ON projects(owner);
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, description TEXT,
    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL, plan TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE, key_prefix TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active', created_at REAL NOT NULL, updated_at REAL NOT NULL,
    last_used_at REAL
);
CREATE INDEX IF NOT EXISTS api_keys_owner ON api_keys(owner);
CREATE TABLE IF NOT EXISTS key_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, key_id TEXT NOT NULL, owner TEXT NOT NULL,
    at REAL NOT NULL, action TEXT NOT NULL, reason TEXT
);
CREATE INDEX IF NOT EXISTS key_events_key ON key_events(key_id);
CREATE TABLE IF NOT EXISTS provider_credentials (
    provider TEXT PRIMARY KEY, secret TEXT NOT NULL, hint TEXT NOT NULL,
    key_fingerprint TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS billing_accounts (
    owner TEXT PRIMARY KEY, customer_id TEXT UNIQUE, subscription_id TEXT, status TEXT, plan TEXT,
    current_period_end REAL, cancel_at_period_end INTEGER, items TEXT, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS custom_providers (
    name TEXT PRIMARY KEY, kind TEXT NOT NULL, base_url TEXT NOT NULL, hosting TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_rules (
    provider TEXT PRIMARY KEY, rules TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_spend (
    provider TEXT NOT NULL, period TEXT NOT NULL, amount_usd REAL NOT NULL DEFAULT 0, PRIMARY KEY (provider, period)
);
CREATE TABLE IF NOT EXISTS bridges (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL UNIQUE, token_hash TEXT NOT NULL UNIQUE,
    token_prefix TEXT NOT NULL, created_at REAL NOT NULL, last_seen_at REAL, models TEXT
);
CREATE INDEX IF NOT EXISTS bridges_owner ON bridges(owner);
CREATE TABLE IF NOT EXISTS stripe_events (
    id TEXT PRIMARY KEY, type TEXT NOT NULL, received_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS alert_rules (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, key_id TEXT, period TEXT NOT NULL,
    amount REAL NOT NULL, currency TEXT NOT NULL, thresholds TEXT NOT NULL, webhook_url TEXT,
    webhook_secret TEXT, enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS alert_rules_owner ON alert_rules(owner);
CREATE TABLE IF NOT EXISTS alert_fired (
    rule_id TEXT NOT NULL, period_id TEXT NOT NULL, threshold INTEGER NOT NULL, at REAL NOT NULL,
    PRIMARY KEY (rule_id, period_id, threshold)
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, ts REAL NOT NULL, level TEXT NOT NULL,
    title TEXT NOT NULL, message TEXT NOT NULL, data TEXT, read INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS notifications_owner ON notifications(owner, ts);
CREATE TABLE IF NOT EXISTS model_prices (
    ref TEXT PRIMARY KEY, fields TEXT NOT NULL, source TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS request_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, owner TEXT NOT NULL, key_id TEXT,
    project_id TEXT, plan TEXT, endpoint TEXT, stream INTEGER, status TEXT NOT NULL, error_code INTEGER,
    provider TEXT, model TEXT, hosting TEXT, mode TEXT, effort TEXT, task TEXT, complexity TEXT,
    prompt_tokens INTEGER, completion_tokens INTEGER, cached_tokens INTEGER, cache_write_tokens INTEGER,
    total_tokens INTEGER, cost_usd REAL, billed_usd REAL, billed_eur REAL, fx_rate REAL, saved_usd REAL,
    simulated_usd REAL, compression_usd REAL, latency_ms INTEGER, ttft_ms INTEGER, cache_hit INTEGER,
    usage_estimated INTEGER, fallback_from TEXT, agents INTEGER
);
CREATE INDEX IF NOT EXISTS metrics_owner_ts ON request_metrics(owner, ts);
CREATE INDEX IF NOT EXISTS metrics_key_ts ON request_metrics(key_id, ts);
CREATE TABLE IF NOT EXISTS preferences (
    owner TEXT NOT NULL, scope TEXT NOT NULL, name TEXT NOT NULL, value TEXT NOT NULL,
    updated_at REAL NOT NULL, PRIMARY KEY (owner, scope, name)
);
CREATE TABLE IF NOT EXISTS key_spend (
    key_id TEXT NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE, period TEXT NOT NULL,
    amount_usd REAL NOT NULL DEFAULT 0, PRIMARY KEY (key_id, period)
);
CREATE TABLE IF NOT EXISTS project_documents (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    position INTEGER NOT NULL, PRIMARY KEY (project_id, doc_id)
);
CREATE TABLE IF NOT EXISTS counters (
    scope TEXT NOT NULL, bucket REAL NOT NULL, amount REAL NOT NULL, expires REAL NOT NULL,
    PRIMARY KEY (scope, bucket)
);
CREATE INDEX IF NOT EXISTS counters_expires ON counters(expires);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, scope TEXT NOT NULL,
    title TEXT NOT NULL, body TEXT NOT NULL,                  -- chiffrés (AES-GCM, 19.1)
    private INTEGER NOT NULL DEFAULT 0, message_count INTEGER NOT NULL, revision INTEGER NOT NULL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS conversations_scope ON conversations(owner, scope, updated_at);
CREATE TABLE IF NOT EXISTS ui_packs (                     -- interfaces personnalisées (.zip du panneau Apparence)
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, manifest TEXT NOT NULL, size_bytes INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ui_packs_owner ON ui_packs(owner);
CREATE TABLE IF NOT EXISTS ui_pack_files (
    pack_id TEXT NOT NULL REFERENCES ui_packs(id) ON DELETE CASCADE, path TEXT NOT NULL,
    data BLOB NOT NULL, PRIMARY KEY (pack_id, path)
);
CREATE TABLE IF NOT EXISTS conversation_shares (
    id TEXT PRIMARY KEY,                                      -- empreinte SHA-256 du jeton du lien
    owner TEXT NOT NULL, scope TEXT NOT NULL, conv_id TEXT NOT NULL,
    token TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,   -- chiffrés : copie figée au partage
    created_at REAL NOT NULL, UNIQUE (owner, scope, conv_id)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, actor_kind TEXT NOT NULL, actor TEXT NOT NULL,
    owner TEXT, ip TEXT, action TEXT NOT NULL, target TEXT, status INTEGER NOT NULL, detail TEXT   -- 20.2
);
CREATE INDEX IF NOT EXISTS audit_at ON audit_log(at);
CREATE TABLE IF NOT EXISTS audit_captures (
    audit_id INTEGER PRIMARY KEY REFERENCES audit_log(id) ON DELETE CASCADE,  -- contrôle du PC (21.4)
    at REAL NOT NULL, image TEXT NOT NULL                     -- capture d'écran chiffrée (AES-GCM)
);
CREATE INDEX IF NOT EXISTS audit_captures_at ON audit_captures(at);
CREATE INDEX IF NOT EXISTS audit_ip ON audit_log(ip, status);
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY, identity TEXT NOT NULL UNIQUE,       -- comptes nommés (20.3)
    name TEXT NOT NULL, plan TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, token_prefix TEXT NOT NULL,
    status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, last_used_at REAL
);
CREATE TABLE IF NOT EXISTS admin_sessions (
    id TEXT PRIMARY KEY,                                      -- SHA-256 du jeton du cookie (20.1)
    master TEXT NOT NULL, ip TEXT, user_agent TEXT, via TEXT NOT NULL,
    created_at REAL NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,   -- mémoire du contrôle du PC
    kind TEXT NOT NULL, text TEXT NOT NULL, source TEXT NOT NULL, hits INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE (owner, kind, text)
);
"""
DOC_COLS = (
    "id",
    "filename",
    "kind",
    "language",
    "size_bytes",
    "n_chunks",
    "tokens",
    "chunk_size",
    "chunk_overlap",
    "created_at",
    "embed_model",
    "embed_dims",
)
PROMPT_COLS = ("id", "name", "description", "content", "variables", "created_at", "updated_at")
PROJECT_COLS = (
    "id",
    "name",
    "description",
    "system_prompt",
    "prompt_id",
    "prompt_vars",
    "model",
    "mode",
    "effort",
    "documents_budget",
    "max_context_tokens",
    "max_output_tokens",
    "compression",
    "created_at",
    "updated_at",
)
# modifications des tables existantes : migrations numérotées (gateway/migrations.py, 18.3)
KEY_COLS = (
    "id",
    "name",
    "description",
    "project_id",
    "plan",
    "key_prefix",
    "metadata",
    "permissions",
    "limits",
    "expires_at",
    "status",
    "status_reason",
    "suspended_until",
    "revoked_at",
    "created_at",
    "updated_at",
    "last_used_at",
)
ADMIN_SESSION_COLS = ("id", "master", "ip", "user_agent", "via", "created_at", "expires_at")
ACCOUNT_COLS = (
    "id",
    "identity",
    "name",
    "plan",
    "token_hash",
    "token_prefix",
    "status",
    "created_at",
    "updated_at",
    "last_used_at",
)
JSON_COLS = {"variables", "prompt_vars", "metadata", "permissions", "limits"}
JSON_DEFAULTS = {"prompt_vars": {}, "metadata": {}, "permissions": {}, "limits": {}}


def default_db_path() -> str:
    return os.getenv("GATEWAY_DB", os.path.join("data", "gateway.db"))


class DocumentStore:
    def __init__(self, path: str | None = None):
        path = path or default_db_path()
        self.path = path  # fichier de la base (sauvegardes 18.2), ou ":memory:"
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self._db.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            # 10.2 : journal WAL + synchronous=NORMAL : une validation n'attend plus l'écriture physique du disque
            # (fsync seulement aux points de contrôle) ; durable aux plantages de la gateway, au pire la toute
            # dernière transaction est perdue sur une coupure de courant. Lectures non bloquées par les écritures.
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA synchronous = NORMAL")
        self._db.execute("PRAGMA busy_timeout = 5000")
        self._db.executescript(SCHEMA)
        self._db.commit()
        migrate(self._db)  # étapes numérotées manquantes (18.3), tout ou rien
        self._lock = threading.Lock()
        # 10.2 : seconde connexion pour les lectures analytiques lourdes (tableau de bord, exports, alertes),
        # exécutées hors de la boucle d'événements : en WAL elles lisent pendant que l'autre connexion écrit,
        # sans prendre le verrou du chemin des requêtes. Base en mémoire : une seule connexion possible.
        if path != ":memory:":
            self._read = sqlite3.connect(path, check_same_thread=False, timeout=5)
            self._read.execute("PRAGMA query_only = ON")
            self._read.execute("PRAGMA busy_timeout = 5000")
            self._read_lock = threading.Lock()
        else:
            self._read, self._read_lock = self._db, self._lock

    def close(self) -> None:
        """Fermeture à l'arrêt : attend l'écriture ou la lecture en cours (verrous), et la fermeture de la dernière
        connexion reporte le journal WAL dans la base (gateway.db autonome, sans -wal à côté)."""
        with self._read_lock:
            if self._read is not self._db:
                self._read.close()
        with self._lock:
            self._db.close()

    def add(
        self,
        owner: str,
        filename: str,
        kind: str,
        language: str | None,
        size_bytes: int,
        chunks: list[Chunk],
        chunk_size: int,
        chunk_overlap: int,
    ) -> dict:
        doc_id = f"doc_{uuid.uuid4().hex[:24]}"
        tokens = sum(c.tokens for c in chunks)
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO documents (id, owner, filename, kind, language, size_bytes, n_chunks, tokens, "
                "chunk_size, chunk_overlap, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    doc_id,
                    owner,
                    filename,
                    kind,
                    language,
                    size_bytes,
                    len(chunks),
                    tokens,
                    chunk_size,
                    chunk_overlap,
                    time.time(),
                ),
            )
            self._db.executemany(
                "INSERT INTO chunks VALUES (?,?,?,?,?)",
                [(doc_id, c.index, c.text, c.tokens, json.dumps(c.meta, ensure_ascii=False)) for c in chunks],
            )
        return self.get(owner, doc_id)

    def list(self, owner: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT {', '.join(DOC_COLS)} FROM documents WHERE owner=? ORDER BY created_at DESC", (owner,)
            ).fetchall()
        return [dict(zip(DOC_COLS, r)) for r in rows]

    def get(self, owner: str, doc_id: str) -> dict | None:
        with self._lock:
            r = self._db.execute(
                f"SELECT {', '.join(DOC_COLS)} FROM documents WHERE owner=? AND id=?", (owner, doc_id)
            ).fetchone()
        return dict(zip(DOC_COLS, r)) if r else None

    def chunks(self, owner: str, doc_id: str) -> list[dict] | None:
        doc = self.get(owner, doc_id)
        if not doc:
            return None
        with self._lock:
            rows = self._db.execute(
                "SELECT idx, text, tokens, meta FROM chunks WHERE doc_id=? ORDER BY idx", (doc_id,)
            ).fetchall()
        return [
            {"index": i, "text": t, "tokens": n, "meta": json.loads(m), "doc_id": doc_id, "filename": doc["filename"]}
            for i, t, n, m in rows
        ]

    def delete(self, owner: str, doc_id: str) -> bool:
        with self._lock, self._db:
            cur = self._db.execute("DELETE FROM documents WHERE owner=? AND id=?", (owner, doc_id))
        return cur.rowcount > 0

    # ---------- vecteurs des passages (17.1) ----------
    def set_vectors(self, owner: str, doc_id: str, model: str, dims: int, rows: list[tuple[int, bytes]]) -> bool:
        with self._lock, self._db:
            if not self._db.execute("SELECT 1 FROM documents WHERE owner=? AND id=?", (owner, doc_id)).fetchone():
                return False  # supprimé pendant le calcul
            self._db.execute("DELETE FROM chunk_vectors WHERE doc_id=?", (doc_id,))
            self._db.executemany("INSERT INTO chunk_vectors VALUES (?,?,?)", [(doc_id, i, v) for i, v in rows])
            self._db.execute("UPDATE documents SET embed_model=?, embed_dims=? WHERE id=?", (model, dims, doc_id))
        return True

    def vectors(self, doc_ids: list[str], model: str) -> dict[tuple[str, int], bytes]:
        """Vecteurs des documents vectorisés par `model` (les autres sont ignorés : jamais deux modèles mêlés)."""
        if not doc_ids:
            return {}
        marks = ",".join("?" * len(doc_ids))
        with self._lock:
            rows = self._db.execute(
                f"SELECT v.doc_id, v.idx, v.vec FROM chunk_vectors v JOIN documents d ON d.id = v.doc_id "
                f"WHERE v.doc_id IN ({marks}) AND d.embed_model = ?",
                (*doc_ids, model),
            ).fetchall()
        return {(d, i): v for d, i, v in rows}

    def documents_to_vectorize(self, model: str) -> list[tuple[str, str]]:
        with self._lock:
            return [
                tuple(r)
                for r in self._db.execute(
                    "SELECT owner, id FROM documents WHERE embed_model IS NULL OR embed_model <> ? ORDER BY created_at",
                    (model,),
                ).fetchall()
            ]

    def document_in_project(self, doc_id: str) -> bool:
        with self._lock:
            return (
                self._db.execute("SELECT 1 FROM project_documents WHERE doc_id=? LIMIT 1", (doc_id,)).fetchone()
                is not None
            )

    # ---------- prompts & projets (3.2) ----------
    @staticmethod
    def _row(cols, r) -> dict:
        d = dict(zip(cols, r))
        for k in JSON_COLS & d.keys():
            d[k] = json.loads(d[k]) if d[k] else JSON_DEFAULTS.get(k, [])
        return d

    def _insert(self, table: str, cols: tuple, prefix: str, owner: str, data: dict, hidden: dict | None = None) -> str:
        """`hidden` : colonnes stockées mais jamais renvoyées (ex. empreinte de clé)."""
        new_id = f"{prefix}_{uuid.uuid4().hex[:24]}"
        now = time.time()
        row = {**{c: None for c in cols}, **data, "id": new_id, "created_at": now, "updated_at": now}
        for k in JSON_COLS & row.keys():
            row[k] = json.dumps(row[k] if row[k] is not None else JSON_DEFAULTS.get(k, []), ensure_ascii=False)
        hidden = hidden or {}
        names = ("owner",) + cols + tuple(hidden)
        with self._lock, self._db:
            self._db.execute(
                f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                (owner,) + tuple(row[c] for c in cols) + tuple(hidden.values()),
            )
        return new_id

    def _update(self, table: str, owner: str, obj_id: str, data: dict) -> bool:
        data = {k: (json.dumps(v, ensure_ascii=False) if k in JSON_COLS else v) for k, v in data.items()}
        data["updated_at"] = time.time()
        sets = ", ".join(f"{k}=?" for k in data)
        with self._lock, self._db:
            cur = self._db.execute(f"UPDATE {table} SET {sets} WHERE owner=? AND id=?", (*data.values(), owner, obj_id))
        return cur.rowcount > 0

    def _select(self, table: str, cols: tuple, owner: str, obj_id: str | None = None):
        q = f"SELECT {', '.join(cols)} FROM {table} WHERE owner=?"
        args: tuple = (owner,)
        if obj_id:
            q, args = q + " AND id=?", (owner, obj_id)
        with self._lock:
            rows = self._db.execute(q + " ORDER BY created_at DESC", args).fetchall()
        return [self._row(cols, r) for r in rows]

    def add_prompt(self, owner: str, data: dict) -> dict:
        return self.get_prompt(owner, self._insert("prompts", PROMPT_COLS, "prm", owner, data))

    def list_prompts(self, owner: str) -> list[dict]:
        return self._select("prompts", PROMPT_COLS, owner)

    def get_prompt(self, owner: str, prompt_id: str) -> dict | None:
        r = self._select("prompts", PROMPT_COLS, owner, prompt_id)
        return r[0] if r else None

    def update_prompt(self, owner: str, prompt_id: str, data: dict) -> dict | None:
        return self.get_prompt(owner, prompt_id) if self._update("prompts", owner, prompt_id, data) else None

    def prompt_users(self, owner: str, prompt_id: str) -> list[str]:
        with self._lock:
            return [
                r[0]
                for r in self._db.execute("SELECT id FROM projects WHERE owner=? AND prompt_id=?", (owner, prompt_id))
            ]

    def delete_prompt(self, owner: str, prompt_id: str) -> bool:
        with self._lock, self._db:
            cur = self._db.execute("DELETE FROM prompts WHERE owner=? AND id=?", (owner, prompt_id))
        return cur.rowcount > 0

    def _project_docs(self, project_id: str) -> list[str]:
        with self._lock:
            return [
                r[0]
                for r in self._db.execute(
                    "SELECT doc_id FROM project_documents WHERE project_id=? ORDER BY position", (project_id,)
                )
            ]

    def set_project_documents(self, project_id: str, doc_ids: list[str]) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM project_documents WHERE project_id=?", (project_id,))
            self._db.executemany(
                "INSERT INTO project_documents VALUES (?,?,?)", [(project_id, d, i) for i, d in enumerate(doc_ids)]
            )

    def add_project(self, owner: str, data: dict) -> dict:
        docs = data.pop("documents", [])
        pid = self._insert("projects", PROJECT_COLS, "proj", owner, data)
        self.set_project_documents(pid, docs)
        return self.get_project(owner, pid)

    def list_projects(self, owner: str) -> list[dict]:
        return [{**p, "documents": self._project_docs(p["id"])} for p in self._select("projects", PROJECT_COLS, owner)]

    def get_project(self, owner: str, project_id: str) -> dict | None:
        r = self._select("projects", PROJECT_COLS, owner, project_id)
        return {**r[0], "documents": self._project_docs(project_id)} if r else None

    def update_project(self, owner: str, project_id: str, data: dict) -> dict | None:
        docs = data.pop("documents", None)
        if not self._update("projects", owner, project_id, data):
            return None
        if docs is not None:
            self.set_project_documents(project_id, docs)
        return self.get_project(owner, project_id)

    # ---------- clés virtuelles (4.1) ----------
    def add_key(self, owner: str, data: dict, key_hash: str) -> dict:
        kid = self._insert(
            "api_keys", KEY_COLS, "key", owner, {"status": "active", **data}, hidden={"key_hash": key_hash}
        )
        return self.get_key(owner, kid)

    def list_keys(self, owner: str) -> list[dict]:
        return self._select("api_keys", KEY_COLS, owner)

    def count_keys(self) -> int:
        """Clés virtuelles de toute l'installation (suspendues en version gratuite, 29.5)."""
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0]

    def get_key(self, owner: str, key_id: str) -> dict | None:
        r = self._select("api_keys", KEY_COLS, owner, key_id)
        return r[0] if r else None

    def update_key(self, owner: str, key_id: str, data: dict, key_hash: str | None = None) -> dict | None:
        if key_hash:
            data = {**data, "key_hash": key_hash}
        return self.get_key(owner, key_id) if self._update("api_keys", owner, key_id, data) else None

    def delete_key(self, owner: str, key_id: str) -> bool:
        with self._lock, self._db:
            cur = self._db.execute("DELETE FROM api_keys WHERE owner=? AND id=?", (owner, key_id))
        return cur.rowcount > 0

    # ---------- réglages de l'instance (6.1) ----------
    def get_setting(self, name: str) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT value FROM settings WHERE name=?", (name,)).fetchone()
        return json.loads(r[0]) if r else None

    def instance_id(self) -> str:
        """Identifiant de cette installation, créé une fois et partagé par les workers (même base) : sert à
        reconnaître la gateway elle-même parmi les serveurs détectés."""
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO settings VALUES ('instance_id',?,?)",
                (json.dumps({"id": secrets.token_hex(12)}), time.time()),
            )
            r = self._db.execute("SELECT value FROM settings WHERE name='instance_id'").fetchone()
        return json.loads(r[0])["id"]

    def set_setting(self, name: str, value: dict) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO settings VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at",
                (name, json.dumps(value), time.time()),
            )

    def replace_setting(self, name: str, expected: dict, value: dict) -> bool:
        """Remplace un réglage seulement s'il vaut encore `expected` (lu juste avant) : un code TOTP ou de secours
        n'est consommé qu'une fois, même par deux workers en même temps (20.1)."""
        with self._lock, self._db:
            cur = self._db.execute(
                "UPDATE settings SET value=?, updated_at=? WHERE name=? AND value=?",
                (json.dumps(value), time.time(), name, json.dumps(expected)),
            )
        return cur.rowcount == 1

    # ---------- journal d'audit (20.2) ----------
    def add_audit(self, e: dict) -> int:
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO audit_log (at, actor_kind, actor, owner, ip, action, target, status, detail) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    e["at"],
                    e["actor_kind"],
                    e["actor"],
                    e.get("owner"),
                    e.get("ip"),
                    e["action"],
                    e.get("target"),
                    e["status"],
                    json.dumps(e["detail"], ensure_ascii=False) if e.get("detail") else None,
                ),
            )
        return cur.lastrowid

    # ---------- captures d'écran du contrôle du PC, rattachées au journal d'audit (21.4) ----------
    def add_capture(self, audit_id: int, at: float, token: str) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO audit_captures VALUES (?,?,?)", (audit_id, at, token))

    def get_capture(self, audit_id: int) -> str | None:
        with self._lock:
            r = self._db.execute("SELECT image FROM audit_captures WHERE audit_id=?", (audit_id,)).fetchone()
        return r[0] if r else None

    def purge_captures(self, before: float) -> int:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM audit_captures WHERE at < ?", (before,)).rowcount

    def audit_ip_known(self, ip: str) -> bool:
        """Une action réussie a-t-elle déjà été enregistrée depuis cette adresse ?"""
        with self._lock:
            return (
                self._db.execute("SELECT 1 FROM audit_log WHERE ip=? AND status < 400 LIMIT 1", (ip,)).fetchone()
                is not None
            )

    def query_audit(
        self,
        *,
        action: str | None = None,
        actor_kind: str | None = None,
        ip: str | None = None,
        result: str | None = None,
        q: str | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int | None = 100,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        where, args = ["1=1"], []
        if action:  # « key » : toute la catégorie ; « key.create » : exacte
            where.append("(action = ? OR action LIKE ?)")
            args += [action, action.rstrip(".") + ".%"]
        if actor_kind:
            where.append("actor_kind = ?")
            args.append(actor_kind)
        if ip:
            where.append("ip = ?")
            args.append(ip)
        if result == "ok":
            where.append("status < 400")
        elif result == "fail":
            where.append("status >= 400")
        if q:
            where.append("(actor LIKE ? OR target LIKE ? OR ip LIKE ?)")
            args += [f"%{q}%"] * 3
        if since is not None:
            where.append("at >= ?")
            args.append(since)
        if until is not None:
            where.append("at < ?")
            args.append(until)
        cond = " AND ".join(where)
        cols = ("id", "at", "actor_kind", "actor", "owner", "ip", "action", "target", "status", "detail")
        page = "" if limit is None else f" LIMIT {int(limit)} OFFSET {int(offset)}"
        capture = "EXISTS (SELECT 1 FROM audit_captures c WHERE c.audit_id = audit_log.id)"
        with self._lock:
            total = self._db.execute(f"SELECT COUNT(*) FROM audit_log WHERE {cond}", args).fetchone()[0]
            rows = self._db.execute(
                f"SELECT {', '.join(cols)}, {capture} FROM audit_log WHERE {cond} ORDER BY at DESC, id DESC{page}",
                args,
            ).fetchall()
        out = []
        for r in rows:
            d = dict(zip(cols, r))
            d["capture"] = bool(r[-1])  # capture d'écran chiffrée consultable (21.4)
            d["detail"] = json.loads(d["detail"]) if d["detail"] else None
            out.append(d)
        return out, total

    def purge_audit(self, before: float) -> int:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM audit_log WHERE at < ?", (before,)).rowcount

    # ---------- sessions d'administration (20.1) ----------
    # ---------- comptes nommés (20.3) ----------
    def _account(self, where: str, value) -> dict | None:
        with self._lock:
            r = self._db.execute(f"SELECT {', '.join(ACCOUNT_COLS)} FROM accounts WHERE {where}=?", (value,)).fetchone()
        return dict(zip(ACCOUNT_COLS, r)) if r else None

    def add_account(self, account: dict) -> None:
        with self._lock, self._db:
            self._db.execute(
                f"INSERT INTO accounts ({', '.join(ACCOUNT_COLS)}) VALUES ({', '.join('?' * len(ACCOUNT_COLS))})",
                tuple(account.get(k) for k in ACCOUNT_COLS),
            )

    def get_account(self, account_id: str) -> dict | None:
        return self._account("id", account_id)

    def account_by_hash(self, token_hash: str) -> dict | None:
        return self._account("token_hash", token_hash)

    def account_by_identity(self, identity: str) -> dict | None:
        return self._account("identity", identity)

    def list_accounts(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(f"SELECT {', '.join(ACCOUNT_COLS)} FROM accounts ORDER BY created_at").fetchall()
        return [dict(zip(ACCOUNT_COLS, r)) for r in rows]

    def update_account(self, account_id: str, fields: dict) -> bool:
        allowed = {k: v for k, v in fields.items() if k in ACCOUNT_COLS and k not in ("id", "identity")}
        if not allowed:
            return False
        with self._lock, self._db:
            cur = self._db.execute(
                f"UPDATE accounts SET {', '.join(f'{k}=?' for k in allowed)} WHERE id=?",
                (*allowed.values(), account_id),
            )
        return cur.rowcount > 0

    def delete_account(self, account_id: str) -> bool:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM accounts WHERE id=?", (account_id,)).rowcount > 0

    def add_admin_session(self, session: dict) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM admin_sessions WHERE expires_at <= ?", (session["created_at"],))
            self._db.execute(
                "INSERT INTO admin_sessions VALUES (?,?,?,?,?,?,?)", tuple(session[k] for k in ADMIN_SESSION_COLS)
            )

    def get_admin_session(self, session_id: str) -> dict | None:
        with self._lock:
            r = self._db.execute(
                f"SELECT {', '.join(ADMIN_SESSION_COLS)} FROM admin_sessions WHERE id=?", (session_id,)
            ).fetchone()
        return dict(zip(ADMIN_SESSION_COLS, r)) if r else None

    def list_admin_sessions(self, now: float) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT {', '.join(ADMIN_SESSION_COLS)} FROM admin_sessions "
                "WHERE expires_at > ? ORDER BY created_at DESC",
                (now,),
            ).fetchall()
        return [dict(zip(ADMIN_SESSION_COLS, r)) for r in rows]

    def delete_admin_sessions(self, session_id: str | None = None) -> int:
        """Une session (`session_id`) ou toutes."""
        with self._lock, self._db:
            if session_id is None:
                return self._db.execute("DELETE FROM admin_sessions").rowcount
            return self._db.execute("DELETE FROM admin_sessions WHERE id=?", (session_id,)).rowcount

    # ---------- métriques par requête (6.1) ----------
    def insert_metrics(self, rows: list[dict]) -> None:
        from .metrics import COLUMNS

        with self._lock, self._db:
            self._db.executemany(
                f"INSERT INTO request_metrics ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
                [tuple(int(r.get(c)) if isinstance(r.get(c), bool) else r.get(c) for c in COLUMNS) for r in rows],
            )

    def query_metrics(
        self,
        owner: str | None,
        *,
        since: float | None = None,
        until: float | None = None,
        key_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """Lignes les plus récentes d'abord. owner=None : toute l'instance (admin)."""
        where, args = [], []
        for cond, val in (
            ("owner=?", owner),
            ("ts>=?", since),
            ("ts<?", until),
            ("key_id=?", key_id),
            ("status=?", status),
        ):
            if val is not None:
                where.append(cond)
                args.append(val)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._read_lock:
            total = self._read.execute(f"SELECT COUNT(*) FROM request_metrics {clause}", args).fetchone()[0]
            cur = self._read.execute(
                f"SELECT * FROM request_metrics {clause} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                (*args, limit, offset),
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for r in rows:
            for b in ("stream", "cache_hit", "usage_estimated"):
                r[b] = bool(r[b]) if r[b] is not None else None
        return rows, total

    # ---------- ponts vers les modèles locaux (7.4) ----------
    _BRIDGE_COLS = ("id", "owner", "name", "token_prefix", "created_at", "last_seen_at", "models")

    def _bridge_row(self, row) -> dict | None:
        if not row:
            return None
        d = dict(zip(self._BRIDGE_COLS, row))
        d["models"] = json.loads(d["models"]) if d["models"] else []
        return d

    def create_bridge(self, owner: str, name: str, token_hash: str, token_prefix: str) -> dict:
        bid = f"br_{uuid.uuid4().hex[:16]}"
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO bridges (id, owner, name, token_hash, token_prefix, created_at) VALUES (?,?,?,?,?,?)",
                (bid, owner, name, token_hash, token_prefix, time.time()),
            )
        return self.get_bridge(owner, bid)

    def get_bridge(self, owner: str, bridge_id: str) -> dict | None:
        with self._lock:
            return self._bridge_row(
                self._db.execute(
                    f"SELECT {', '.join(self._BRIDGE_COLS)} FROM bridges WHERE owner=? AND id=?", (owner, bridge_id)
                ).fetchone()
            )

    def bridge_by_token(self, token_hash: str) -> dict | None:
        with self._lock:
            return self._bridge_row(
                self._db.execute(
                    f"SELECT {', '.join(self._BRIDGE_COLS)} FROM bridges WHERE token_hash=?", (token_hash,)
                ).fetchone()
            )

    def list_bridges(self, owner: str | None = None) -> list[dict]:
        sql = f"SELECT {', '.join(self._BRIDGE_COLS)} FROM bridges"
        with self._lock:
            rows = (
                self._db.execute(sql + " WHERE owner=? ORDER BY created_at", (owner,))
                if owner
                else self._db.execute(sql + " ORDER BY created_at")
            ).fetchall()
        return [self._bridge_row(r) for r in rows]

    def bridge_name_taken(self, name: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM bridges WHERE name=?", (name,)).fetchone() is not None

    def touch_bridge(self, bridge_id: str, models: list[str] | None = None) -> None:
        with self._lock, self._db:
            if models is None:
                self._db.execute("UPDATE bridges SET last_seen_at=? WHERE id=?", (time.time(), bridge_id))
            else:
                self._db.execute(
                    "UPDATE bridges SET last_seen_at=?, models=? WHERE id=?",
                    (time.time(), json.dumps(models), bridge_id),
                )

    def delete_bridge(self, owner: str, bridge_id: str) -> bool:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM bridges WHERE owner=? AND id=?", (owner, bridge_id)).rowcount > 0

    # ---------- exports (6.5) ----------
    def iter_metrics(
        self,
        owner: str | None,
        *,
        since: float | None = None,
        until: float | None = None,
        key_id: str | None = None,
        chunk: int = 2000,
    ):
        """Lignes de métriques en ordre chronologique, par lots (pagination par clé, pas d'OFFSET)."""
        last_id = 0
        while True:
            where, args = ["id>?"], [last_id]
            for cond, val in (("owner=?", owner), ("ts>=?", since), ("ts<?", until), ("key_id=?", key_id)):
                if val is not None:
                    where.append(cond)
                    args.append(val)
            with self._read_lock:
                cur = self._read.execute(
                    f"SELECT * FROM request_metrics WHERE {' AND '.join(where)} ORDER BY id LIMIT ?", (*args, chunk)
                )
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            if not rows:
                return
            yield from rows
            last_id = rows[-1]["id"]

    def key_names(self, owner: str | None) -> dict[str, str]:
        q, args = (
            ("SELECT id, name FROM api_keys", ())
            if owner is None
            else ("SELECT id, name FROM api_keys WHERE owner=?", (owner,))
        )
        with self._lock:
            return dict(self._db.execute(q, args).fetchall())

    # ---------- alertes budgétaires (6.5) ----------
    def spend_between(self, owner: str, since: float, until: float, key_id: str | None = None) -> tuple[float, float]:
        """(dépense facturée en $, en €) sur [since, until[ — l'euro au taux de chaque requête."""
        q = "SELECT TOTAL(billed_usd), TOTAL(billed_eur) FROM request_metrics WHERE owner=? AND ts>=? AND ts<?"
        args: tuple = (owner, since, until)
        if key_id:
            q, args = q + " AND key_id=?", (*args, key_id)
        with self._read_lock:
            return tuple(self._read.execute(q, args).fetchone())

    def add_alert_rule(self, owner: str, data: dict) -> dict:
        rid = f"alr_{uuid.uuid4().hex[:20]}"
        now = time.time()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO alert_rules VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rid,
                    owner,
                    data["name"],
                    data.get("key_id"),
                    data["period"],
                    data["amount"],
                    data["currency"],
                    json.dumps(data["thresholds"]),
                    data.get("webhook_url"),
                    data.get("webhook_secret"),
                    int(data.get("enabled", True)),
                    now,
                    now,
                ),
            )
        return self.get_alert_rule(owner, rid)

    def _alert_rows(self, where: str, args: tuple) -> list[dict]:
        cols = (
            "id",
            "owner",
            "name",
            "key_id",
            "period",
            "amount",
            "currency",
            "thresholds",
            "webhook_url",
            "webhook_secret",
            "enabled",
            "created_at",
            "updated_at",
        )
        with self._lock:
            rows = self._db.execute(
                f"SELECT {', '.join(cols)} FROM alert_rules WHERE {where} ORDER BY created_at", args
            ).fetchall()
        out = []
        for r in rows:
            d = dict(zip(cols, r))
            d["thresholds"], d["enabled"] = json.loads(d["thresholds"]), bool(d["enabled"])
            out.append(d)
        return out

    def list_alert_rules(self, owner: str | None = None, enabled_only: bool = False) -> list[dict]:
        where, args = ["1=1"], []
        if owner is not None:
            where.append("owner=?")
            args.append(owner)
        if enabled_only:
            where.append("enabled=1")
        return self._alert_rows(" AND ".join(where), tuple(args))

    def get_alert_rule(self, owner: str, rule_id: str) -> dict | None:
        r = self._alert_rows("owner=? AND id=?", (owner, rule_id))
        return r[0] if r else None

    def update_alert_rule(self, owner: str, rule_id: str, data: dict) -> dict | None:
        data = {k: (json.dumps(v) if k == "thresholds" else int(v) if k == "enabled" else v) for k, v in data.items()}
        data["updated_at"] = time.time()
        with self._lock, self._db:
            n = self._db.execute(
                f"UPDATE alert_rules SET {', '.join(f'{k}=?' for k in data)} WHERE owner=? AND id=?",
                (*data.values(), owner, rule_id),
            ).rowcount
        return self.get_alert_rule(owner, rule_id) if n else None

    def delete_alert_rule(self, owner: str, rule_id: str) -> bool:
        with self._lock, self._db:
            n = self._db.execute("DELETE FROM alert_rules WHERE owner=? AND id=?", (owner, rule_id)).rowcount
            self._db.execute("DELETE FROM alert_fired WHERE rule_id=?", (rule_id,))
        return n > 0

    def mark_fired(self, rule_id: str, period_id: str, threshold: int) -> bool:
        """True si ce palier n'avait pas encore été déclenché sur cette période (dédoublonnage)."""
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO alert_fired VALUES (?,?,?,?)", (rule_id, period_id, threshold, time.time())
            )
        return cur.rowcount > 0

    def add_notification(self, owner: str, level: str, title: str, message: str, data: dict | None = None) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO notifications (owner, ts, level, title, message, data) VALUES (?,?,?,?,?,?)",
                (owner, time.time(), level, title, message, json.dumps(data or {})),
            )

    def list_notifications(self, owner: str, limit: int = 50) -> tuple[list[dict], int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, ts, level, title, message, data, read FROM notifications "
                "WHERE owner=? ORDER BY ts DESC, id DESC LIMIT ?",
                (owner, limit),
            ).fetchall()
            unread = self._db.execute(
                "SELECT COUNT(*) FROM notifications WHERE owner=? AND read=0", (owner,)
            ).fetchone()[0]
        return [
            {"id": i, "ts": t, "level": lv, "title": ti, "message": m, "data": json.loads(d or "{}"), "read": bool(r)}
            for i, t, lv, ti, m, d, r in rows
        ], unread

    def mark_notifications_read(self, owner: str, ids: list[int] | None = None) -> int:
        with self._lock, self._db:
            if ids:
                q = f"UPDATE notifications SET read=1 WHERE owner=? AND id IN ({','.join('?' * len(ids))})"
                return self._db.execute(q, (owner, *ids)).rowcount
            return self._db.execute("UPDATE notifications SET read=1 WHERE owner=? AND read=0", (owner,)).rowcount

    def key_budgets(self, key_ids: set[str]) -> list[tuple[str, str, str, dict]]:
        """(owner, key_id, nom, limits) des clés ayant un budget (4.3), pour les alertes automatiques."""
        if not key_ids:
            return []
        with self._lock:
            rows = self._db.execute(
                f"SELECT owner, id, name, limits FROM api_keys WHERE id IN ({','.join('?' * len(key_ids))})",
                tuple(key_ids),
            ).fetchall()
        return [(o, i, n, json.loads(l or "{}")) for o, i, n, l in rows if json.loads(l or "{}").get("budget")]

    # ---------- grille tarifaire (6.2) ----------
    def list_prices(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT ref, fields, source, updated_at FROM model_prices ORDER BY ref").fetchall()
        return [{"ref": r, "fields": json.loads(f), "source": s, "updated_at": u} for r, f, s, u in rows]

    def set_price(self, ref: str, fields: dict, source: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO model_prices VALUES (?,?,?,?) ON CONFLICT(ref) DO UPDATE SET "
                "fields=excluded.fields, source=excluded.source, updated_at=excluded.updated_at",
                (ref, json.dumps(fields), source, time.time()),
            )

    def delete_price(self, ref: str) -> bool:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM model_prices WHERE ref=?", (ref,)).rowcount > 0

    def metrics_summary(
        self, owner: str | None, *, since: float | None = None, until: float | None = None, key_id: str | None = None
    ) -> dict:
        """Indicateurs clés (6.3) en une agrégation : volume, dépense facturée (€ au taux de chaque
        requête), coût réel, tokens, latences, erreurs."""
        where, args = [], []
        for cond, val in (("owner=?", owner), ("ts>=?", since), ("ts<?", until), ("key_id=?", key_id)):
            if val is not None:
                where.append(cond)
                args.append(val)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        sql = (
            "SELECT COUNT(*), TOTAL(status='error'), TOTAL(billed_eur), TOTAL(cost_usd*fx_rate), "
            "TOTAL(prompt_tokens), TOTAL(completion_tokens), TOTAL(total_tokens), "
            "AVG(CASE WHEN status='ok' THEN latency_ms END), AVG(ttft_ms), TOTAL(cache_hit), "
            "TOTAL(hosting='local'), MIN(ts), MAX(ts) "
            f"FROM request_metrics {clause}"
        )
        with self._read_lock:
            r = self._read.execute(sql, args).fetchone()
        n = r[0] or 0
        return {
            "requests": n,
            "errors": int(r[1]),
            "error_rate": round(r[1] / n, 4) if n else 0.0,
            "spend_eur": round(r[2], 6),
            "provider_cost_eur": round(r[3], 6),
            "tokens": {"prompt": int(r[4]), "completion": int(r[5]), "total": int(r[6])},
            "avg_latency_ms": round(r[7]) if r[7] is not None else None,
            "avg_ttft_ms": round(r[8]) if r[8] is not None else None,
            "cache_hits": int(r[9]),
            "local_requests": int(r[10]),
            "first_ts": r[11],
            "last_ts": r[12],
        }

    def chart_data(
        self,
        owner: str | None,
        *,
        since: float | None,
        until: float,
        key_id: str | None,
        tz_offset_min: int,
        bucket: int,
        base_in: float,
        base_out: float,
    ) -> dict:
        """Données des graphiques (6.4). Les seaux temporels et la carte de chaleur sont calculés
        en heure LOCALE du lecteur (tz_offset_min = Date.getTimezoneOffset() du navigateur)."""
        where, args = ["status='ok'"], []
        for cond, val in (("owner=?", owner), ("ts>=?", since), ("ts<?", until), ("key_id=?", key_id)):
            if val is not None:
                where.append(cond)
                args.append(val)
        clause = " AND ".join(where)
        off = tz_offset_min * 60  # local = UTC - off
        with self._read_lock:
            series = self._read.execute(
                f"SELECT CAST((ts - ?) / ? AS INTEGER) AS b, TOTAL(cost_usd*fx_rate), "
                f"TOTAL((prompt_tokens*? + completion_tokens*?)*fx_rate), COUNT(*) "
                f"FROM request_metrics WHERE {clause} GROUP BY b ORDER BY b",
                (off, bucket, base_in, base_out, *args),
            ).fetchall()
            split = self._read.execute(
                f"SELECT CASE WHEN hosting='local' THEN 'local' WHEN hosting='free' THEN 'free' ELSE provider END "
                f"AS p, COUNT(*), "
                f"TOTAL(total_tokens), TOTAL(cost_usd*fx_rate) FROM request_metrics WHERE {clause} GROUP BY p",
                args,
            ).fetchall()
            scatter = self._read.execute(
                f"SELECT latency_ms, cost_usd*fx_rate, provider, hosting, model, ts FROM request_metrics "
                f"WHERE {clause} AND latency_ms IS NOT NULL ORDER BY ts DESC LIMIT 400",
                args,
            ).fetchall()
            heat = self._read.execute(
                f"SELECT (CAST(strftime('%w', ts - ?, 'unixepoch') AS INTEGER) + 6) % 7, "
                f"CAST(strftime('%H', ts - ?, 'unixepoch') AS INTEGER), COUNT(*) "
                f"FROM request_metrics WHERE {clause} GROUP BY 1, 2",
                (off, off, *args),
            ).fetchall()
        return {"series": series, "split": split, "scatter": scatter, "heat": heat}

    def cloud_models_used(self, since: float) -> list[dict]:
        """Modèles cloud ayant répondu depuis `since` (toutes identités) : nombre de requêtes, tokens, dernière
        utilisation. Sert à repérer ceux dont le tarif est inconnu (10.7)."""
        with self._read_lock:
            cur = self._read.execute(
                "SELECT provider, model, COUNT(*) AS requests, TOTAL(total_tokens) AS tokens, MAX(ts) AS last_used "
                "FROM request_metrics WHERE ts>=? AND status='ok' AND hosting='cloud' AND COALESCE(cache_hit, 0)=0 "
                "AND provider IS NOT NULL AND model IS NOT NULL GROUP BY provider, model",
                (since,),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def savings_groups(
        self,
        owner: str | None,
        *,
        since: float | None = None,
        until: float | None = None,
        key_id: str | None = None,
        by_model: bool = False,
    ) -> list[dict]:
        """Agrégats pour le calcul des économies : par catégorie (local, free, cloud, response_cache)
        ou par modèle. Les montants « _fx » sont pondérés par le taux de change de chaque requête."""
        where, args = ["status='ok'"], []
        for cond, val in (("owner=?", owner), ("ts>=?", since), ("ts<?", until), ("key_id=?", key_id)):
            if val is not None:
                where.append(cond)
                args.append(val)
        category = (
            "CASE WHEN cache_hit=1 THEN 'response_cache' WHEN hosting='local' THEN 'local' "
            "WHEN hosting='free' THEN 'free' ELSE 'cloud' END"
        )  # free : offres gratuites (13.8)
        group = "provider, model" if by_model else category
        select = "provider, model" if by_model else f"{category} AS category"
        sql = (
            f"SELECT {select}, COUNT(*) AS n, "
            "TOTAL(prompt_tokens) AS prompt, TOTAL(completion_tokens) AS completion, "
            "TOTAL(cost_usd) AS cost, TOTAL(saved_usd) AS saved, "
            "TOTAL(prompt_tokens*fx_rate) AS prompt_fx, TOTAL(completion_tokens*fx_rate) AS completion_fx, "
            "TOTAL(cost_usd*fx_rate) AS cost_fx, TOTAL(saved_usd*fx_rate) AS saved_fx "
            f"FROM request_metrics WHERE {' AND '.join(where)} GROUP BY {group}"
        )
        with self._read_lock:
            cur = self._read.execute(sql, args)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ---------- préférences (5.3) ----------
    def get_pref(self, owner: str, scope: str, name: str) -> tuple[dict, float] | None:
        with self._lock:
            r = self._db.execute(
                "SELECT value, updated_at FROM preferences WHERE owner=? AND scope=? AND name=?", (owner, scope, name)
            ).fetchone()
        return (json.loads(r[0]), r[1]) if r else None

    def set_pref(self, owner: str, scope: str, name: str, value: dict) -> float:
        now = time.time()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO preferences VALUES (?,?,?,?,?) ON CONFLICT(owner, scope, name) "
                "DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (owner, scope, name, json.dumps(value, ensure_ascii=False), now),
            )
        return now

    def delete_pref(self, owner: str, scope: str, name: str) -> bool:
        with self._lock, self._db:
            cur = self._db.execute("DELETE FROM preferences WHERE owner=? AND scope=? AND name=?", (owner, scope, name))
        return cur.rowcount > 0

    # ---------- mémoire du contrôle du PC : préférences de l'utilisateur et leçons tirées des erreurs ----------
    MEMORY_COLS = ("id", "kind", "text", "source", "hits", "created_at", "updated_at")

    def add_memory(self, owner: str, kind: str, text: str, source: str, keep: int) -> dict:
        """Ajout, ou souvenir déjà connu revu (hits + 1) ; au-delà de `keep` souvenirs, les plus anciens
        s'effacent, ceux dictés par l'utilisateur en dernier."""
        now = time.time()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO agent_memory (owner, kind, text, source, created_at, updated_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(owner, kind, text) DO UPDATE SET hits=hits+1, updated_at=excluded.updated_at, "
                "source=CASE WHEN excluded.source='user' THEN 'user' ELSE source END",
                (owner, kind, text, source, now, now),
            )
            self._db.execute(
                "DELETE FROM agent_memory WHERE id IN (SELECT id FROM agent_memory WHERE owner=? "
                # id DESC : à horodatage égal (horloge de Windows à ~15 ms), le plus récemment écrit reste
                "ORDER BY source='user' DESC, updated_at DESC, id DESC LIMIT -1 OFFSET ?)",
                (owner, keep),
            )
            r = self._db.execute(
                f"SELECT {', '.join(self.MEMORY_COLS)} FROM agent_memory WHERE owner=? AND kind=? AND text=?",
                (owner, kind, text),
            ).fetchone()
        return dict(zip(self.MEMORY_COLS, r)) if r else {}  # aussitôt effacé : `keep` souvenirs plus prioritaires

    def list_memory(self, owner: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT {', '.join(self.MEMORY_COLS)} FROM agent_memory WHERE owner=? ORDER BY updated_at DESC",
                (owner,),
            ).fetchall()
        return [dict(zip(self.MEMORY_COLS, r)) for r in rows]

    def delete_memory(self, owner: str, memory_id: int | None = None) -> int:
        """Un souvenir, ou tous (memory_id None)."""
        with self._lock, self._db:
            if memory_id is None:
                cur = self._db.execute("DELETE FROM agent_memory WHERE owner=?", (owner,))
            else:
                cur = self._db.execute("DELETE FROM agent_memory WHERE owner=? AND id=?", (owner, memory_id))
        return cur.rowcount

    # ---------- conversations (19.1) : titre et messages chiffrés par gateway.conversations ----------
    CONV_COLS = (
        "id",
        "title",
        "body",
        "private",
        "message_count",
        "revision",
        "created_at",
        "updated_at",
        "project_id",
        "starred",
    )

    def add_conversation(self, owner: str, scope: str, row: dict) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO conversations (id, owner, scope, title, body, private, message_count, revision, "
                "created_at, updated_at, project_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["id"],
                    owner,
                    scope,
                    row["title"],
                    row["body"],
                    int(row["private"]),
                    row["message_count"],
                    row["revision"],
                    row["created_at"],
                    row["updated_at"],
                    row.get("project_id"),
                ),
            )

    def get_conversation(self, owner: str, scope: str, conv_id: str) -> dict | None:
        with self._lock:
            r = self._db.execute(
                f"SELECT {', '.join(self.CONV_COLS)} FROM conversations WHERE owner=? AND scope=? AND id=?",
                (owner, scope, conv_id),
            ).fetchone()
        return dict(zip(self.CONV_COLS, r)) if r else None

    def list_conversations(self, owner: str, scope: str) -> list[dict]:
        """Sans les messages (body) : la liste latérale n'a besoin que des titres."""
        cols = [c for c in self.CONV_COLS if c != "body"]
        with self._lock:
            rows = self._db.execute(
                f"SELECT {', '.join(cols)} FROM conversations WHERE owner=? AND scope=? ORDER BY updated_at DESC, id",
                (owner, scope),
            ).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def count_conversations(self, owner: str, scope: str) -> int:
        with self._lock:
            return self._db.execute(
                "SELECT COUNT(*) FROM conversations WHERE owner=? AND scope=?", (owner, scope)
            ).fetchone()[0]

    def update_conversation(
        self,
        owner: str,
        scope: str,
        conv_id: str,
        fields: dict,
        expected_revision: int | None = None,
        bump: bool = True,
    ) -> bool:
        """Écrit `fields` (et revision + 1 si `bump`) ; avec `expected_revision`, seulement si personne n'a écrit
        entre-temps (autre appareil) : sinon False."""
        sets = ", ".join(f"{k}=?" for k in fields) + (", revision=revision+1" if bump else "")
        sql = f"UPDATE conversations SET {sets} WHERE owner=? AND scope=? AND id=?"
        args = [int(v) if isinstance(v, bool) else v for v in fields.values()] + [owner, scope, conv_id]
        if expected_revision is not None:
            sql += " AND revision=?"
            args.append(expected_revision)
        with self._lock, self._db:
            return self._db.execute(sql, args).rowcount > 0

    def delete_conversation(self, owner: str, scope: str, conv_id: str) -> bool:
        with self._lock, self._db:
            self._db.execute(  # son lien de partage cesse de fonctionner
                "DELETE FROM conversation_shares WHERE owner=? AND scope=? AND conv_id=?", (owner, scope, conv_id)
            )
            return (
                self._db.execute(
                    "DELETE FROM conversations WHERE owner=? AND scope=? AND id=?", (owner, scope, conv_id)
                ).rowcount
                > 0
            )

    def delete_conversations(self, owner: str, scope: str) -> int:
        with self._lock, self._db:
            self._db.execute("DELETE FROM conversation_shares WHERE owner=? AND scope=?", (owner, scope))
            return self._db.execute("DELETE FROM conversations WHERE owner=? AND scope=?", (owner, scope)).rowcount

    def purge_conversations(self, before: float) -> int:
        """Conservation limitée (GATEWAY_CONVERSATION_DAYS) : sans activité depuis `before`."""
        with self._lock, self._db:
            n = self._db.execute("DELETE FROM conversations WHERE updated_at < ?", (before,)).rowcount
            self._db.execute(  # liens des conversations effacées
                "DELETE FROM conversation_shares WHERE NOT EXISTS (SELECT 1 FROM conversations c WHERE "
                "c.owner=conversation_shares.owner AND c.scope=conversation_shares.scope AND c.id=conv_id)"
            )
            return n

    # ---------- interfaces personnalisées (ui_packs.py) ----------
    def add_ui_pack(self, owner: str, pack_id: str, manifest: dict, files: dict[str, bytes], created_at: float) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO ui_packs (id, owner, manifest, size_bytes, created_at) VALUES (?,?,?,?,?)",
                (pack_id, owner, json.dumps(manifest, ensure_ascii=False), sum(map(len, files.values())), created_at),
            )
            self._db.executemany(
                "INSERT INTO ui_pack_files (pack_id, path, data) VALUES (?,?,?)",
                [(pack_id, path, data) for path, data in files.items()],
            )

    def list_ui_packs(self, owner: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT p.id, p.manifest, p.size_bytes, p.created_at, COUNT(f.path) FROM ui_packs p "
                "LEFT JOIN ui_pack_files f ON f.pack_id = p.id WHERE p.owner=? GROUP BY p.id ORDER BY p.created_at",
                (owner,),
            ).fetchall()
        return [{"id": r[0], **json.loads(r[1]), "size_bytes": r[2], "created_at": r[3], "files": r[4]} for r in rows]

    def ui_pack_file(self, pack_id: str, path: str) -> bytes | None:
        with self._lock:
            r = self._db.execute(
                "SELECT data FROM ui_pack_files WHERE pack_id=? AND path=?", (pack_id, path)
            ).fetchone()
        return r[0] if r else None

    def delete_ui_pack(self, owner: str, pack_id: str) -> bool:
        with self._lock, self._db:
            if not self._db.execute("SELECT 1 FROM ui_packs WHERE owner=? AND id=?", (owner, pack_id)).fetchone():
                return False
            self._db.execute("DELETE FROM ui_pack_files WHERE pack_id=?", (pack_id,))
            self._db.execute("DELETE FROM ui_packs WHERE id=?", (pack_id,))
            return True

    # ---------- partage d'une conversation par lien, en lecture seule ----------
    SHARE_COLS = ("id", "owner", "scope", "conv_id", "token", "title", "body", "created_at")

    def set_share(self, row: dict) -> None:
        """Une copie par conversation : un nouveau partage remplace l'ancien (et son lien)."""
        with self._lock, self._db:
            self._db.execute(
                "DELETE FROM conversation_shares WHERE owner=? AND scope=? AND conv_id=?",
                (row["owner"], row["scope"], row["conv_id"]),
            )
            self._db.execute(
                f"INSERT INTO conversation_shares ({', '.join(self.SHARE_COLS)}) VALUES (?,?,?,?,?,?,?,?)",
                [row[c] for c in self.SHARE_COLS],
            )

    def get_share(self, owner: str, scope: str, conv_id: str) -> dict | None:
        with self._lock:
            r = self._db.execute(
                f"SELECT {', '.join(self.SHARE_COLS)} FROM conversation_shares WHERE owner=? AND scope=? AND conv_id=?",
                (owner, scope, conv_id),
            ).fetchone()
        return dict(zip(self.SHARE_COLS, r)) if r else None

    def find_share(self, share_id: str) -> dict | None:
        with self._lock:
            r = self._db.execute(
                f"SELECT {', '.join(self.SHARE_COLS)} FROM conversation_shares WHERE id=?", (share_id,)
            ).fetchone()
        return dict(zip(self.SHARE_COLS, r)) if r else None

    def delete_share(self, owner: str, scope: str, conv_id: str) -> bool:
        with self._lock, self._db:
            return (
                self._db.execute(
                    "DELETE FROM conversation_shares WHERE owner=? AND scope=? AND conv_id=?", (owner, scope, conv_id)
                ).rowcount
                > 0
            )

    # ---------- statut & audit des clés (4.4) ----------
    def set_key_status(
        self,
        owner: str,
        key_id: str,
        status: str,
        reason: str | None = None,
        suspended_until: float | None = None,
        actor: str = "owner",
    ) -> dict | None:
        now = time.time()
        data = {"status": status, "status_reason": reason, "suspended_until": suspended_until}
        if status == "revoked":
            data["revoked_at"] = now
        key = self.update_key(owner, key_id, data)
        if key:
            self.log_key_event(
                owner, key_id, status if status != "active" else "resumed", f"{reason or ''}".strip() or None, actor
            )
        return key

    def log_key_event(
        self, owner: str, key_id: str, action: str, reason: str | None = None, actor: str = "owner"
    ) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO key_events (key_id, owner, at, action, reason) VALUES (?,?,?,?,?)",
                (key_id, owner, time.time(), action, f"{reason} [{actor}]" if reason else f"[{actor}]"),
            )

    def key_events(self, owner: str, key_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT at, action, reason FROM key_events WHERE owner=? AND key_id=? ORDER BY id", (owner, key_id)
            ).fetchall()
        return [{"at": a, "action": b, "reason": c} for a, b, c in rows]

    # ---------- secrets des providers, chiffrés (4.4) ----------
    def set_credential(self, provider: str, secret: str, hint: str, fingerprint: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO provider_credentials VALUES (?,?,?,?,?) ON CONFLICT(provider) "
                "DO UPDATE SET secret=excluded.secret, hint=excluded.hint, "
                "key_fingerprint=excluded.key_fingerprint, updated_at=excluded.updated_at",
                (provider, secret, hint, fingerprint, time.time()),
            )

    def get_credential(self, provider: str) -> str | None:
        with self._lock:
            r = self._db.execute("SELECT secret FROM provider_credentials WHERE provider=?", (provider,)).fetchone()
        return r[0] if r else None

    def list_credentials(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT provider, hint, key_fingerprint, updated_at FROM provider_credentials ORDER BY provider"
            ).fetchall()
        return [{"provider": p, "hint": h, "encryption_key": f, "updated_at": u} for p, h, f, u in rows]

    # ---------- fournisseurs ajoutés depuis la page Modèles ----------
    def add_custom_provider(self, name: str, kind: str, base_url: str, hosting: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO custom_providers VALUES (?,?,?,?,?)", (name, kind, base_url, hosting, time.time())
            )

    def list_custom_providers(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT name, kind, base_url, hosting, created_at FROM custom_providers ORDER BY created_at"
            ).fetchall()
        return [dict(zip(("name", "kind", "base_url", "hosting", "created_at"), r)) for r in rows]

    def delete_custom_provider(self, name: str) -> bool:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM custom_providers WHERE name=?", (name,)).rowcount > 0

    # ---------- restrictions des IA en entrée (fournisseurs) ----------
    def set_provider_rules(self, provider: str, rules: dict) -> None:
        with self._lock, self._db:
            if rules:
                self._db.execute(
                    "INSERT INTO provider_rules VALUES (?,?,?) ON CONFLICT(provider) DO UPDATE "
                    "SET rules=excluded.rules, updated_at=excluded.updated_at",
                    (provider, json.dumps(rules), time.time()),
                )
            else:
                self._db.execute("DELETE FROM provider_rules WHERE provider=?", (provider,))

    def list_provider_rules(self) -> dict[str, dict]:
        with self._lock:
            rows = self._db.execute("SELECT provider, rules FROM provider_rules").fetchall()
        return {p: json.loads(r) for p, r in rows}

    def provider_spend(self, provider: str, period: str) -> float:
        with self._lock:
            r = self._db.execute(
                "SELECT amount_usd FROM provider_spend WHERE provider=? AND period=?", (provider, period)
            ).fetchone()
        return r[0] if r else 0.0

    def add_provider_spend(self, provider: str, period: str, amount_usd: float) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO provider_spend VALUES (?,?,?) ON CONFLICT(provider, period) "
                "DO UPDATE SET amount_usd = amount_usd + excluded.amount_usd",
                (provider, period, amount_usd),
            )

    def delete_credential(self, provider: str) -> bool:
        with self._lock, self._db:
            cur = self._db.execute("DELETE FROM provider_credentials WHERE provider=?", (provider,))
        return cur.rowcount > 0

    def reencrypt_credentials(self, old, new) -> int:
        """Rotation de la clé de chiffrement : tout est rechiffré dans une seule transaction."""
        with self._lock, self._db:
            rows = self._db.execute("SELECT provider, secret FROM provider_credentials").fetchall()
            for provider, secret in rows:
                plain = old.decrypt(secret, provider)
                self._db.execute(
                    "UPDATE provider_credentials SET secret=?, key_fingerprint=? WHERE provider=?",
                    (new.encrypt(plain, provider), new.fingerprint, provider),
                )
        return len(rows)

    def key_spend(self, key_id: str, period: str) -> float:
        with self._lock:
            r = self._db.execute(
                "SELECT amount_usd FROM key_spend WHERE key_id=? AND period=?", (key_id, period)
            ).fetchone()
        return r[0] if r else 0.0

    def add_key_spend(self, key_id: str, period: str, amount_usd: float) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO key_spend VALUES (?,?,?) ON CONFLICT(key_id, period) "
                "DO UPDATE SET amount_usd = amount_usd + excluded.amount_usd",
                (key_id, period, amount_usd),
            )

    # -- compteurs glissants partagés (10.3, gateway/counters.py) --
    def counter_add(self, scope: str, bucket: float, amount: float, expires: float) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO counters VALUES (?,?,?,?) ON CONFLICT(scope, bucket) "
                "DO UPDATE SET amount = amount + excluded.amount",
                (scope, bucket, amount, expires),
            )

    def counter_sub(self, scope: str, bucket: float, amount: float) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE counters SET amount = amount - ? WHERE scope=? AND bucket=?", (amount, scope, bucket)
            )

    def counter_buckets(self, scope: str, after: float) -> list[tuple[float, float]]:
        with self._lock:
            return self._db.execute(
                "SELECT bucket, amount FROM counters WHERE scope=? AND bucket>? ORDER BY bucket", (scope, after)
            ).fetchall()

    def counter_scopes(self, prefix: str, now: float) -> list[str]:
        like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self._lock:
            return [
                r[0]
                for r in self._db.execute(
                    "SELECT DISTINCT scope FROM counters WHERE scope LIKE ? ESCAPE '\\' AND expires>? ORDER BY scope",
                    (like, now),
                )
            ]

    def counter_reset(self, scope: str) -> int:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM counters WHERE scope=?", (scope,)).rowcount

    def counter_purge(self, now: float) -> int:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM counters WHERE expires<=?", (now,)).rowcount

    LAST_USED_RESOLUTION = 60  # last_used_at : au plus une écriture par minute et par clé (10.2)

    def key_by_hash(self, key_hash: str) -> tuple[str, dict] | None:
        """(propriétaire, clé) pour une empreinte de secret ; met à jour last_used_at (à la minute près)."""
        with self._lock:
            r = self._db.execute(
                f"SELECT owner, {', '.join(KEY_COLS)} FROM api_keys WHERE key_hash=?", (key_hash,)
            ).fetchone()
        if not r:
            return None
        owner, key = r[0], self._row(KEY_COLS, r[1:])
        now = time.time()
        if not key["last_used_at"] or now - key["last_used_at"] >= self.LAST_USED_RESOLUTION:
            with self._lock, self._db:
                self._db.execute("UPDATE api_keys SET last_used_at=? WHERE key_hash=?", (now, key_hash))
            key["last_used_at"] = now
        return owner, key

    def delete_project(self, owner: str, project_id: str) -> bool:
        with self._lock, self._db:
            cur = self._db.execute("DELETE FROM projects WHERE owner=? AND id=?", (owner, project_id))
            if cur.rowcount:  # ses conversations restent, détachées
                self._db.execute(
                    "UPDATE conversations SET project_id=NULL WHERE owner=? AND project_id=?", (owner, project_id)
                )
        return cur.rowcount > 0
