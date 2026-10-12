"""SQLite access with ordered migrations. WAL mode so the API, workers and MCP server share one file."""
from __future__ import annotations

import json
import sqlite3
import threading
from importlib import resources
from pathlib import Path

_local = threading.local()


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def migrate(conn: sqlite3.Connection) -> list[str]:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
    files = sorted(p.name for p in resources.files("shiftws.migrations").iterdir() if p.name.endswith(".sql"))
    applied = []
    for name in files:
        if name in done:
            continue
        sql = resources.files("shiftws.migrations").joinpath(name).read_text()
        conn.execute("BEGIN")
        try:
            for stmt in _split_sql(sql):
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_migrations VALUES (?, datetime('now'))", (name,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        applied.append(name)
    return applied


def _split_sql(sql: str) -> list[str]:
    import re
    body = re.sub(r"--[^\n]*", "", sql)   # migrations never put "--" inside string literals
    return [s.strip() for s in body.split(";") if s.strip() and not s.strip().upper().startswith("PRAGMA")]


class DB:
    """Thin helper around a connection; one per thread."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._conns: dict[int, sqlite3.Connection] = {}
        self._lock = threading.Lock()
        migrate(self.conn)

    @property
    def conn(self) -> sqlite3.Connection:
        tid = threading.get_ident()
        with self._lock:
            c = self._conns.get(tid)
            if c is None:
                c = connect(self.path)
                self._conns[tid] = c
            return c

    def x(self, sql: str, params=()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def one(self, sql: str, params=()) -> dict | None:
        r = self.conn.execute(sql, params).fetchone()
        return dict(r) if r else None

    def all(self, sql: str, params=()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def insert(self, table: str, row: dict) -> None:
        cols = ",".join(row)
        qs = ",".join("?" for _ in row)
        self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", [_enc(v) for v in row.values()])

    def update(self, table: str, key: dict, values: dict) -> None:
        sets = ",".join(f"{k}=?" for k in values)
        where = " AND ".join(f"{k}=?" for k in key)
        self.conn.execute(f"UPDATE {table} SET {sets} WHERE {where}",
                          [_enc(v) for v in values.values()] + list(key.values()))

    def tx(self):
        return _Tx(self.conn)

    def close(self):
        with self._lock:
            for c in self._conns.values():
                c.close()
            self._conns.clear()


def _enc(v):
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, Path):
        return str(v)
    return v


class _Tx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, et, ev, tb):
        self.conn.execute("ROLLBACK" if et else "COMMIT")
        return False
