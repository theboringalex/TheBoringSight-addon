"""TheBoringSight - a Palantir-style operational layer for Home Assistant."""
import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import actions, ai
from . import analytics as an
from . import insights as ins
from .config import cfg
from .connectors.homeassistant import HomeAssistantConnector
from .db import DB
from .ingest import Ingest, to_float

logging.basicConfig(level=getattr(logging, cfg.log_level, logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("sight")

STATIC = os.path.join(os.path.dirname(__file__), "static")


class State:
    db: DB
    ingest: Ingest
    connectors: dict
    run_event: asyncio.Event


S = State()


async def analysis_loop():
    await asyncio.sleep(20)
    while True:
        try:
            await asyncio.to_thread(ins.run_analysis, S.db, S.ingest)
        except Exception:  # noqa: BLE001
            log.exception("analysis loop")
        try:
            await asyncio.wait_for(S.run_event.wait(), timeout=cfg.analysis_interval)
        except asyncio.TimeoutError:
            pass
        S.run_event.clear()


async def maintenance_loop():
    while True:
        await asyncio.sleep(3600)
        try:
            await asyncio.to_thread(S.ingest.purge)
        except Exception:  # noqa: BLE001
            log.exception("purge")


@asynccontextmanager
async def lifespan(app: FastAPI):
    S.db = DB(cfg.db_path)
    S.ingest = Ingest(S.db)
    S.run_event = asyncio.Event()
    S.connectors = {"homeassistant": HomeAssistantConnector(S.ingest, S.db)}  # register more connectors here
    tasks = [asyncio.create_task(c.run()) for c in S.connectors.values()]
    tasks += [asyncio.create_task(analysis_loop()), asyncio.create_task(maintenance_loop())]
    log.info("TheBoringSight started (db=%s, actions_enabled=%s, ai=%s)", cfg.db_path, cfg.actions_enabled, cfg.ai_provider)
    yield
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="TheBoringSight", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


async def auth(request: Request, authorization: str = Header(default=""), x_api_key: str = Header(default="")):
    if not cfg.api_key:
        return
    token = x_api_key or authorization.removeprefix("Bearer ").strip() or request.query_params.get("key", "")
    if token != cfg.api_key:
        raise HTTPException(401, "invalid or missing API key")


api = [Depends(auth)]


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/api/health")
async def health():
    return {"ok": True}


# ---------------------------------------------------------------- helpers
def _obj(r: dict) -> dict:
    return {"id": r["id"], "type": r["type"], "name": r["name"], "props": json.loads(r.get("props") or "{}")}


def _live(eid: str) -> dict:
    live = S.ingest.live.get(eid)
    if not live:
        return {}
    a = live["attrs"]
    return {"state": live["state"], "last_changed": live["last_changed"],
            "unit": a.get("unit_of_measurement"), "attrs": {k: a[k] for k in list(a)[:25]}}


def _ents():
    return ins.entities(S.db)


# ------------------------------------------------------------------ status
@app.get("/api/status", dependencies=api)
async def status():
    d = S.db.one("SELECT COUNT(*) c, MIN(ts) t0 FROM states")
    h = S.db.one("SELECT COUNT(*) c FROM hourly")
    last = S.db.meta_get("last_analysis")
    return {
        "connectors": {n: c.status for n, c in S.connectors.items()},
        "data": {"state_rows": d["c"], "hourly_rows": h["c"], "oldest": d["t0"], "live_entities": len(S.ingest.live)},
        "last_analysis": json.loads(last) if last else None,
        "actions_enabled": cfg.actions_enabled, "allowed_services": cfg.allowed_services,
        "ai_provider": cfg.ai_provider if (cfg.anthropic_key or cfg.gemini_key) else "none",
        "now": time.time(),
    }


@app.get("/api/overview", dependencies=api)
async def overview():
    types = S.db.query("SELECT type, COUNT(*) c FROM objects GROUP BY type")
    cases = S.db.query("SELECT severity, area, COUNT(*) c FROM insights WHERE status IN ('open','ack') GROUP BY severity, area")
    sev = {"critical": 0, "warning": 0, "info": 0}
    per_area: dict = {}
    for c in cases:
        sev[c["severity"]] = sev.get(c["severity"], 0) + c["c"]
        if c["area"]:
            per_area.setdefault(c["area"], {"critical": 0, "warning": 0, "info": 0})[c["severity"]] += c["c"]
    areas = []
    for a in S.db.query("SELECT id, name FROM objects WHERE type='Area' ORDER BY name"):
        p = per_area.get(a["name"], {"critical": 0, "warning": 0, "info": 0})
        score = max(0, 100 - 30 * p["critical"] - 12 * p["warning"] - 3 * p["info"])
        n = S.db.one("SELECT COUNT(*) c FROM links WHERE dst=? AND rel='located_in'", (a["id"],))["c"]
        areas.append({"id": a["id"], "name": a["name"], "score": score, "cases": p, "objects": n})
    ents = _ents()
    people = [{"id": e, "name": m["name"], "state": S.ingest.live[e]["state"],
               "since": S.ingest.live[e]["last_changed"]} for e, m in ents.items()
              if m["domain"] == "person" and e in S.ingest.live]
    return {"types": {t["type"]: t["c"] for t in types}, "cases": sev, "areas": areas, "people": people}


# ---------------------------------------------------------------- ontology
@app.get("/api/ontology/objects", dependencies=api)
async def objects(type: str = "", q: str = "", area: str = "", limit: int = Query(200, le=1000)):
    sql, p = "SELECT id, type, name, props FROM objects WHERE 1=1", []
    if type:
        sql += " AND type=?"
        p.append(type)
    if q:
        sql += " AND (LOWER(name) LIKE ? OR LOWER(id) LIKE ?)"
        p += [f"%{q.lower()}%"] * 2
    if area:
        sql += " AND (id IN (SELECT src FROM links WHERE dst=? AND rel IN ('located_in','part_of')) OR id=?)"
        p += [area, area]
    sql += " ORDER BY type, name LIMIT ?"
    p.append(limit)
    out = []
    for r in S.db.query(sql, tuple(p)):
        o = _obj(r)
        if o["id"].startswith("entity:"):
            lv = _live(o["id"][7:])
            o["live"] = {"state": lv.get("state"), "unit": lv.get("unit")} if lv else None
        out.append(o)
    return out


@app.get("/api/ontology/object/{oid:path}", dependencies=api)
async def object_detail(oid: str):
    r = S.db.one("SELECT id, type, name, props FROM objects WHERE id=?", (oid,))
    if not r:
        raise HTTPException(404, "object not found")
    o = _obj(r)
    if oid.startswith("entity:"):
        o["live"] = _live(oid[7:])
    nb = lambda rows, key: [{"rel": x["rel"], "id": x[key], "name": (S.db.one("SELECT name, type FROM objects WHERE id=?", (x[key],)) or {}).get("name"),
                             "type": (S.db.one("SELECT type FROM objects WHERE id=?", (x[key],)) or {}).get("type")} for x in rows]
    o["out"] = nb(S.db.query("SELECT rel, dst FROM links WHERE src=? LIMIT 60", (oid,)), "dst")
    inc = S.db.query("SELECT rel, src FROM links WHERE dst=? LIMIT 60", (oid,))
    o["in"] = nb(inc, "src")
    o["cases"] = S.db.query("SELECT id, severity, title, status FROM insights WHERE entity_id=? AND status IN ('open','ack') ORDER BY id DESC",
                            (oid[7:],)) if oid.startswith("entity:") else []
    return o


# --------------------------------------------------------------- analytics
def _require_entity(eid: str) -> dict:
    m = _ents().get(eid)
    if not m:
        raise HTTPException(404, "unknown entity")
    return m


@app.get("/api/series/{eid}", dependencies=api)
async def series(eid: str, hours: int = Query(72, le=24 * 60)):
    _require_entity(eid)
    since = int(time.time() // 3600) - hours
    rows = S.db.query("SELECT hour, mean, mn, mx FROM hourly WHERE entity_id=? AND hour>=? ORDER BY hour", (eid, since))
    return [{"ts": r["hour"] * 3600, "mean": r["mean"], "min": r["mn"], "max": r["mx"]} for r in rows]


@app.get("/api/forecast/{eid}", dependencies=api)
async def forecast(eid: str, horizon: int = Query(24, ge=1, le=72)):
    _require_entity(eid)
    return await asyncio.to_thread(an.forecast, S.db, eid, horizon)


@app.get("/api/presence/{eid}", dependencies=api)
async def presence(eid: str):
    _require_entity(eid)
    res = await asyncio.to_thread(an.presence_profile, S.db, eid)
    if res is None:
        raise HTTPException(404, "no history")
    return res


@app.get("/api/correlations/{eid}", dependencies=api)
async def correlations(eid: str, deseason: bool = True):
    _require_entity(eid)
    ents = _ents()
    cands = [e for e, m in ents.items() if m["domain"] == "sensor" and to_float((S.ingest.live.get(e) or {}).get("state")) is not None
             and m.get("state_class") not in ("total", "total_increasing") and m.get("device_class") != "battery"]
    res = await asyncio.to_thread(an.correlations, S.db, eid, cands[:400], 14, 8, deseason)
    for r in res:
        r["name"] = ents[r["entity_id"]]["name"]
    return res


@app.get("/api/energy/{eid}", dependencies=api)
async def energy(eid: str):
    _require_entity(eid)
    daily = await asyncio.to_thread(an.energy_daily, S.db, eid, 21)
    proj = await asyncio.to_thread(an.energy_projection, S.db, eid)
    return {"daily": daily, "projection": proj}


@app.get("/api/battery/{eid}", dependencies=api)
async def battery(eid: str):
    _require_entity(eid)
    return await asyncio.to_thread(an.battery_forecast, S.db, eid)


# ---------------------------------------------------------------- insights
@app.get("/api/insights", dependencies=api)
async def list_insights(status: str = "open", limit: int = Query(100, le=500)):
    where = {"open": "status IN ('open','ack')", "all": "1=1", "resolved": "status='resolved'",
             "dismissed": "status='dismissed'", "actioned": "status='actioned'"}.get(status, "status IN ('open','ack')")
    rows = S.db.query(f"SELECT * FROM insights WHERE {where} ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, updated DESC LIMIT ?", (limit,))
    for r in rows:
        r["evidence"] = json.loads(r["evidence"] or "{}")
        r["proposal"] = json.loads(r["proposal"] or "{}")
    return rows


def _set_status(iid: int, status: str):
    if not S.db.execute("UPDATE insights SET status=?, updated=? WHERE id=?", (status, time.time(), iid)):
        raise HTTPException(404, "insight not found")
    actions.audit(S.db, "ui", status, f"insight:{iid}", {}, {"ok": True})
    return {"ok": True}


@app.post("/api/insights/{iid}/ack", dependencies=api)
async def ack(iid: int):
    return _set_status(iid, "ack")


@app.post("/api/insights/{iid}/dismiss", dependencies=api)
async def dismiss(iid: int):
    return _set_status(iid, "dismissed")


@app.post("/api/insights/{iid}/approve", dependencies=api)
async def approve(iid: int):
    return await actions.run_proposal(S.db, S.connectors["homeassistant"], iid)


@app.post("/api/analysis/run", dependencies=api)
async def run_now():
    res = await asyncio.to_thread(ins.run_analysis, S.db, S.ingest)
    return res


# ---------------------------------------------------------------- AIP / misc
class Question(BaseModel):
    question: str


@app.post("/api/ask", dependencies=api)
async def ask(q: Question):
    return await ai.ask(S.db, S.ingest, q.question[:1000])


@app.get("/api/audit", dependencies=api)
async def audit_log(limit: int = Query(100, le=500)):
    rows = S.db.query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
    return rows
