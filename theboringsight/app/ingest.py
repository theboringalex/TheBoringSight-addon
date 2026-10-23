"""Ingest layer: the single place where connectors hand data to the platform.

Connectors (Home Assistant today, others later) call:
  - replace_ontology(source, objects, links)   -> ontology (objects + links)
  - record_state(...) / record_history_batch() -> time series + live cache
"""
import json
import math
import re
import time
from typing import Optional

from .config import cfg
from .db import DB

MIN_STORE_INTERVAL = 10.0  # seconds; throttles bursty sensors


def to_float(s) -> Optional[float]:
    try:
        v = float(s)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


class Ingest:
    def __init__(self, db: DB) -> None:
        self.db = db
        self.live: dict = {}          # entity_id -> {state, attrs, last_changed, seen}
        self.tracked: set = set()     # entity ids that get time-series storage
        self._last_stored: dict = {}  # entity_id -> (ts, state)
        self._exclude = re.compile(cfg.exclude_regex) if cfg.exclude_regex else None
        self.last_event_ts: float = 0.0

    # ------------------------------------------------------------------ filter
    def passes_filter(self, entity_id: str) -> bool:
        domain = entity_id.split(".", 1)[0]
        if domain not in cfg.track_domains:
            return False
        if self._exclude and self._exclude.search(entity_id):
            return False
        return True

    # ---------------------------------------------------------------- ontology
    def replace_ontology(self, source: str, objects: list, links: list) -> None:
        with self.db.tx():
            self.db.execute("DELETE FROM objects WHERE source=?", (source,))
            self.db.execute("DELETE FROM links WHERE source=?", (source,))
            self.db.executemany(
                "INSERT OR REPLACE INTO objects(id,type,name,source,props) VALUES(?,?,?,?,?)",
                [(o["id"], o["type"], o["name"], source, json.dumps(o.get("props", {}), ensure_ascii=False)) for o in objects],
            )
            self.db.executemany(
                "INSERT OR IGNORE INTO links(src,rel,dst,source) VALUES(?,?,?,?)",
                [(l[0], l[1], l[2], source) for l in links],
            )

    # ------------------------------------------------------------ time series
    def record_state(self, entity_id: str, state: str, attrs: dict, ts: float) -> None:
        now = time.time()
        self.last_event_ts = now
        self.live[entity_id] = {
            "state": state, "attrs": attrs or {}, "last_changed": ts, "seen": now,
        }
        if entity_id not in self.tracked:
            return
        prev = self._last_stored.get(entity_id)
        if prev and prev[1] == state:
            return  # attribute-only change
        if prev and (ts - prev[0]) < MIN_STORE_INTERVAL and ts >= prev[0]:
            return
        self._store(entity_id, state, ts)

    def remove_live(self, entity_id: str) -> None:
        self.live.pop(entity_id, None)

    def record_history_batch(self, rows: list) -> int:
        """rows: [(entity_id, state, ts)] in chronological order per entity."""
        n = 0
        with self.db.tx():
            for eid, state, ts in rows:
                if self._store(eid, state, ts):
                    n += 1
        return n

    def _store(self, entity_id: str, state: str, ts: float) -> bool:
        value = to_float(state)
        stored_state = None if value is not None else state
        inserted = self.db.execute(
            "INSERT OR IGNORE INTO states(entity_id,ts,value,state) VALUES(?,?,?,?)",
            (entity_id, ts, value, stored_state),
        )
        prev = self._last_stored.get(entity_id)
        if prev is None or ts >= prev[0]:
            self._last_stored[entity_id] = (ts, state)
        if inserted and value is not None:
            hour = int(ts // 3600)
            self.db.execute(
                """INSERT INTO hourly(entity_id,hour,n,mean,mn,mx,last,last_ts)
                   VALUES(?,?,1,?,?,?,?,?)
                   ON CONFLICT(entity_id,hour) DO UPDATE SET
                     mean=(mean*n+excluded.mean)/(n+1), n=n+1,
                     mn=min(mn,excluded.mn), mx=max(mx,excluded.mx),
                     last=CASE WHEN excluded.last_ts>=last_ts THEN excluded.last ELSE last END,
                     last_ts=max(last_ts,excluded.last_ts)""",
                (entity_id, hour, value, value, value, value, ts),
            )
        return bool(inserted)

    # --------------------------------------------------------------- retention
    def purge(self) -> None:
        cutoff = time.time() - cfg.retention_days * 86400
        self.db.execute("DELETE FROM states WHERE ts<?", (cutoff,))
        self.db.execute("DELETE FROM hourly WHERE hour<?", (int(cutoff // 3600) - 24 * 300,))
        self.db.execute("DELETE FROM audit WHERE ts<?", (time.time() - 365 * 86400,))
