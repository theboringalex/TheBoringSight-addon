"""Small SQLite wrapper (one shared connection, guarded by a re-entrant lock)."""
import os
import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);

-- Ontology: objects + links
CREATE TABLE IF NOT EXISTS objects(
  id TEXT PRIMARY KEY, type TEXT NOT NULL, name TEXT, source TEXT, props TEXT
);
CREATE INDEX IF NOT EXISTS ix_obj_type ON objects(type);
CREATE TABLE IF NOT EXISTS links(
  src TEXT, rel TEXT, dst TEXT, source TEXT, PRIMARY KEY(src, rel, dst)
);
CREATE INDEX IF NOT EXISTS ix_links_dst ON links(dst);

-- Time series
CREATE TABLE IF NOT EXISTS states(
  entity_id TEXT, ts REAL, value REAL, state TEXT, PRIMARY KEY(entity_id, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS hourly(
  entity_id TEXT, hour INTEGER, n INTEGER, mean REAL, mn REAL, mx REAL,
  last REAL, last_ts REAL, PRIMARY KEY(entity_id, hour)
) WITHOUT ROWID;

-- Case management
CREATE TABLE IF NOT EXISTS insights(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT, kind TEXT, severity TEXT, entity_id TEXT, area TEXT,
  title TEXT, detail TEXT, evidence TEXT, proposal TEXT,
  status TEXT DEFAULT 'open',
  created REAL, updated REAL, resolved_at REAL
);
CREATE INDEX IF NOT EXISTS ix_ins_key ON insights(key, status);

-- Audit trail for every governed action
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL, actor TEXT, action TEXT, target TEXT, payload TEXT, result TEXT
);
"""


class DB:
    def __init__(self, path: str) -> None:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)

    def execute(self, sql: str, params=()):
        with self.lock:
            cur = self.conn.execute(sql, params)
            return cur.rowcount

    def executemany(self, sql: str, rows):
        with self.lock:
            self.conn.executemany(sql, rows)

    def query(self, sql: str, params=()) -> list:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params=()):
        with self.lock:
            r = self.conn.execute(sql, params).fetchone()
            return dict(r) if r else None

    @contextmanager
    def tx(self):
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                yield self
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def meta_get(self, key: str, default=None):
        r = self.one("SELECT v FROM meta WHERE k=?", (key,))
        return r["v"] if r else default

    def meta_set(self, key: str, value) -> None:
        self.execute(
            "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, str(value)),
        )
