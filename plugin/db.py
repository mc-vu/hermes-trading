"""SQLite-Zugriff: Pfad, Verbindung, Migrationen.

Die DB liegt unter ``<hermes home>/plugin-data/hermes-trading/data.db``
(``plugins.plugin_storage.plugin_data_dir``). ``HTR_DB_PATH`` oder ``--db``
ueberschreiben den Pfad (Tests, manuelle Laeufe ausserhalb von Hermes).
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

PLUGIN_NAME = "hermes-trading"
DB_FILENAME = "data.db"
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_db_path() -> Path:
    override = os.environ.get("HTR_DB_PATH")
    if override:
        return Path(override)
    try:
        from plugins.plugin_storage import plugin_data_dir
    except ImportError as exc:  # ausserhalb der Hermes-Runtime
        raise RuntimeError(
            "plugins.plugin_storage nicht importierbar - innerhalb von Hermes ausfuehren "
            "oder --db / HTR_DB_PATH setzen."
        ) from exc
    return plugin_data_dir(PLUGIN_NAME) / DB_FILENAME


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path else default_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass  # z. B. Netzwerk-FS: bleibt bei DELETE
    return conn


def available_migrations() -> list[tuple[int, Path]]:
    out = []
    for f in sorted(MIGRATIONS_DIR.iterdir()):
        m = _MIGRATION_RE.match(f.name)
        if m:
            out.append((int(m.group(1)), f))
    return out


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return row[0] or 0


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Wendet alle ausstehenden Migrationen an, jede in einer eigenen Transaktion."""
    applied = []
    have = current_version(conn)
    conn.commit()
    for version, path in available_migrations():
        if version <= have:
            continue
        sql = path.read_text(encoding="utf-8")
        try:
            conn.execute("BEGIN")
            for stmt in _split_sql(sql):
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                (version, path.name, utcnow()),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        applied.append(path.name)
    return applied


def _split_sql(sql: str) -> list[str]:
    """Teilt ein Migrationsskript in Statements (sqlite3.complete_statement)."""
    stmts, buf = [], ""
    for line in sql.splitlines(keepends=True):
        if not buf and line.strip().startswith("--"):
            continue
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                stmts.append(buf.strip())
            buf = ""
    if buf.strip():
        raise ValueError(f"unvollstaendiges SQL-Statement am Ende: {buf[:80]!r}")
    return stmts


def counts(conn: sqlite3.Connection) -> dict:
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
