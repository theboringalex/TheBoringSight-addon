"""Home Assistant connector.

- WebSocket API: registries (areas/devices/entities), full state snapshot,
  live state_changed events.
- REST API: history backfill, service calls (governed actions).
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from urllib.parse import quote

import aiohttp

from ..config import cfg
from ..db import DB
from ..ingest import Ingest
from .base import Connector

log = logging.getLogger("sight.ha")


def parse_ts(s) -> float:
    if not s:
        return time.time()
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def classify(domain: str) -> str:
    return {
        "person": "Person",
        "sensor": "Sensor",
        "binary_sensor": "BinarySensor",
        "device_tracker": "Tracker",
        "weather": "Weather",
    }.get(domain, "Actuator")


class HomeAssistantConnector(Connector):
    name = "homeassistant"

    def __init__(self, ingest: Ingest, db: DB) -> None:
        self.ingest = ingest
        self.db = db
        self.status = {
            "connected": False, "since": None, "events": 0, "last_event": None,
            "error": None, "backfill": "idle", "entities": 0,
        }
        self._dirty = False
        self._dirty_at = 0.0
        self._built_at = 0.0
        self._bf_done = False
        self._last_meta_write = 0.0

    # ------------------------------------------------------------------ http
    @property
    def base(self) -> str:
        return cfg.ha_url.rstrip("/")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {cfg.ha_token}"}

    def _ssl(self):
        return None if cfg.ha_verify_ssl else False

    async def call_service(self, domain: str, service: str, data: dict) -> dict:
        url = f"{self.base}/api/services/{domain}/{service}"
        async with aiohttp.ClientSession() as http:
            async with http.post(url, json=data, headers=self._headers(), ssl=self._ssl(),
                                 timeout=aiohttp.ClientTimeout(total=20)) as r:
                body = await r.text()
                return {"status": r.status, "body": body[:500]}

    # ------------------------------------------------------------------- run
    async def run(self) -> None:
        if not cfg.ha_token:
            self.status["error"] = "HA_TOKEN is not set"
            log.error("HA_TOKEN is not set - connector idle")
        backoff = 5
        while True:
            try:
                if cfg.ha_token:
                    await self._session()
                    backoff = 5
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.status["error"] = str(e)
                log.warning("HA session ended: %s", e)
            self.status["connected"] = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _session(self) -> None:
        url = cfg.ha_ws_url or (self.base.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/api/websocket")
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(url, heartbeat=30, max_msg_size=0, ssl=self._ssl()) as ws:
                hello = await ws.receive_json()
                if hello.get("type") != "auth_required":
                    raise RuntimeError(f"unexpected handshake: {hello}")
                await ws.send_json({"type": "auth", "access_token": cfg.ha_token})
                res = await ws.receive_json()
                if res.get("type") != "auth_ok":
                    raise RuntimeError(f"HA auth failed: {res.get('message')}")

                mid = 0

                async def cmd(payload: dict):
                    nonlocal mid
                    mid += 1
                    await ws.send_json({**payload, "id": mid})
                    while True:
                        msg = await ws.receive_json()
                        if msg.get("id") == mid and msg.get("type") == "result":
                            if not msg.get("success"):
                                raise RuntimeError(f"{payload['type']} failed: {msg.get('error')}")
                            return msg.get("result")

                states = await cmd({"type": "get_states"})
                areas = await cmd({"type": "config/area_registry/list"})
                devices = await cmd({"type": "config/device_registry/list"})
                entities = await cmd({"type": "config/entity_registry/list"})

                self._build(states, areas, devices, entities)
                await cmd({"type": "subscribe_events", "event_type": "state_changed"})
                for et in ("entity_registry_updated", "device_registry_updated", "area_registry_updated"):
                    await cmd({"type": "subscribe_events", "event_type": et})

                self._dirty = False
                self.status.update(connected=True, since=time.time(), error=None)
                log.info("connected to Home Assistant, %d tracked entities", len(self.ingest.tracked))
                bf = asyncio.create_task(self._backfill())
                try:
                    while True:
                        try:
                            msg = await ws.receive(timeout=30)
                        except asyncio.TimeoutError:
                            msg = None
                        now = time.time()
                        if self._bf_done and now - self._last_meta_write > 60:
                            self.db.meta_set("ha_history_until", now)
                            self._last_meta_write = now
                        if self._dirty and now - self._dirty_at > 10 and now - self._built_at > 300:
                            log.info("registry changed - re-syncing ontology")
                            return
                        if msg is None:
                            continue
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            if data.get("type") != "event":
                                continue
                            ev = data["event"]
                            if ev.get("event_type") == "state_changed":
                                self._on_state(ev["data"])
                            else:
                                self._dirty = True
                                self._dirty_at = now
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING,
                                          aiohttp.WSMsgType.ERROR):
                            raise RuntimeError("websocket closed")
                finally:
                    bf.cancel()
                    if self._bf_done:
                        self.db.meta_set("ha_history_until", time.time())

    def _on_state(self, data: dict) -> None:
        eid = data.get("entity_id")
        ns = data.get("new_state")
        if not eid:
            return
        if not ns:
            self.ingest.remove_live(eid)
            return
        self.status["events"] += 1
        self.status["last_event"] = time.time()
        self.ingest.record_state(eid, ns.get("state"), ns.get("attributes") or {}, parse_ts(ns.get("last_changed")))

    # -------------------------------------------------------------- ontology
    def _build(self, states: list, areas: list, devices: list, entities: list) -> None:
        area_by_id = {a["area_id"]: a for a in areas}
        dev_by_id = {d["id"]: d for d in devices}
        reg_by_eid = {e["entity_id"]: e for e in entities}

        objects, links = [{"id": "home:main", "type": "Home", "name": "Home", "props": {}}], []
        used_areas, used_devices = set(), set()
        tracked = set()
        ent_objects = []

        for s in states:
            eid = s["entity_id"]
            if not self.ingest.passes_filter(eid):
                continue
            reg = reg_by_eid.get(eid, {})
            if reg.get("disabled_by"):
                continue
            attrs = s.get("attributes") or {}
            domain = eid.split(".", 1)[0]
            dev = dev_by_id.get(reg.get("device_id")) if reg.get("device_id") else None
            area_id = reg.get("area_id") or (dev or {}).get("area_id")
            area_name = (area_by_id.get(area_id) or {}).get("name") if area_id else None
            tracked.add(eid)
            if dev:
                used_devices.add(dev["id"])
            if area_id:
                used_areas.add(area_id)
            props = {
                "domain": domain,
                "device_class": attrs.get("device_class"),
                "unit": attrs.get("unit_of_measurement"),
                "state_class": attrs.get("state_class"),
                "area_id": area_id, "area": area_name,
                "device_id": dev["id"] if dev else None,
                "platform": reg.get("platform"),
            }
            oid = f"entity:{eid}"
            ent_objects.append({"id": oid, "type": classify(domain),
                                "name": attrs.get("friendly_name") or eid, "props": props})
            if dev:
                links.append((oid, "belongs_to", f"device:{dev['id']}"))
            if area_id:
                links.append((oid, "located_in", f"area:{area_id}"))
            src = attrs.get("source")
            if domain == "person" and src:
                links.append((oid, "tracked_by", f"entity:{src}"))

        for aid in used_areas:
            a = area_by_id.get(aid)
            if not a:
                continue
            objects.append({"id": f"area:{aid}", "type": "Area", "name": a["name"],
                            "props": {"floor_id": a.get("floor_id")}})
            links.append((f"area:{aid}", "part_of", "home:main"))
        for did in used_devices:
            d = dev_by_id[did]
            objects.append({"id": f"device:{did}", "type": "Device",
                            "name": d.get("name_by_user") or d.get("name") or did,
                            "props": {"manufacturer": d.get("manufacturer"), "model": d.get("model"),
                                      "sw_version": d.get("sw_version"), "area_id": d.get("area_id")}})
            if d.get("area_id") in used_areas:
                links.append((f"device:{did}", "located_in", f"area:{d['area_id']}"))
        objects.extend(ent_objects)

        valid = {o["id"] for o in objects}
        links = [l for l in links if l[0] in valid and l[2] in valid]
        self.ingest.tracked = tracked
        self.ingest.replace_ontology(self.name, objects, links)
        ts_now = time.time()
        for s in states:
            self.ingest.record_state(s["entity_id"], s.get("state"), s.get("attributes") or {},
                                     parse_ts(s.get("last_changed")) if s["entity_id"] in tracked else ts_now)
        self.status["entities"] = len(tracked)
        self._built_at = time.time()

    # -------------------------------------------------------------- backfill
    async def _backfill(self) -> None:
        days = cfg.backfill_days
        if days <= 0:
            self._bf_done = True
            self.status["backfill"] = "disabled"
            return
        try:
            now = time.time()
            last = self.db.meta_get("ha_history_until")
            start = max(float(last), now - days * 86400) if last else now - days * 86400
            if now - start < 600:
                self._bf_done = True
                self.status["backfill"] = "done"
                return
            ids = sorted(self.ingest.tracked)
            total_steps = max(1, len(ids))
            done, rows_total = 0, 0
            self.status["backfill"] = "running 0%"
            async with aiohttp.ClientSession() as http:
                t = start
                windows = []
                while t < now:
                    windows.append((t, min(t + 3 * 86400, now)))
                    t += 3 * 86400
                steps = len(windows) * total_steps
                for (w0, w1) in windows:
                    for i in range(0, len(ids), 25):
                        batch = ids[i:i + 25]
                        rows = await self._fetch_history(http, batch, w0, w1)
                        if rows:
                            rows_total += await asyncio.to_thread(self.ingest.record_history_batch, rows)
                        done += len(batch)
                        self.status["backfill"] = f"running {int(100 * done / steps)}%"
                        await asyncio.sleep(0)
            self.db.meta_set("ha_history_until", time.time())
            self._bf_done = True
            self.status["backfill"] = f"done ({rows_total} rows)"
            log.info("history backfill finished: %d rows", rows_total)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.status["backfill"] = f"error: {e}"
            log.warning("backfill failed: %s", e)

    async def _fetch_history(self, http, ids: list, t0: float, t1: float) -> list:
        url = f"{self.base}/api/history/period/{quote(iso(t0))}"
        params = {"filter_entity_id": ",".join(ids), "end_time": iso(t1), "minimal_response": "",
                  "no_attributes": "", "significant_changes_only": "0"}
        async with http.get(url, params=params, headers=self._headers(), ssl=self._ssl(),
                            timeout=aiohttp.ClientTimeout(total=180)) as r:
            if r.status != 200:
                raise RuntimeError(f"history HTTP {r.status}")
            data = await r.json()
        rows = []
        for series in data:
            if not series:
                continue
            eid = series[0].get("entity_id")
            if not eid:
                continue
            for p in series:
                rows.append((eid, p.get("state"), parse_ts(p.get("last_changed"))))
        return rows
