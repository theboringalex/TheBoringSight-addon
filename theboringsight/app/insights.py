"""Detectors + case management.

Detectors turn raw analytics into *insights* (cases). Each insight has a stable
key, so it is updated in place while the condition persists and is resolved
automatically when the condition disappears. Humans can acknowledge or dismiss.
"""
import json
import logging
import time

import numpy as np

from . import analytics as an
from .config import cfg
from .ingest import to_float

log = logging.getLogger("sight.insights")

SEV_ORDER = {"critical": 0, "warning": 1, "info": 2}


def entities(db) -> dict:
    out = {}
    for r in db.query("SELECT id, type, name, props FROM objects WHERE id LIKE 'entity:%'"):
        p = json.loads(r["props"] or "{}")
        out[r["id"][7:]] = {**p, "otype": r["type"], "name": r["name"]}
    return out


def _notify(title: str, message: str, key: str) -> dict:
    return {"domain": "persistent_notification", "service": "create", "label": "Notify in Home Assistant",
            "data": {"title": f"[Sight] {title}", "message": message,
                     "notification_id": "sight_" + "".join(c if c.isalnum() else "_" for c in key)[:60]}}


def _ins(key, kind, severity, eid, area, title, detail, evidence=None, proposal=None):
    return {"key": key, "kind": kind, "severity": severity, "entity_id": eid, "area": area,
            "title": title, "detail": detail, "evidence": evidence or {},
            "proposal": proposal or _notify(title, detail, key)}


def _num(ingest, eid):
    live = ingest.live.get(eid)
    return to_float(live["state"]) if live else None


# ------------------------------------------------------------------ detectors
def detect_anomalies(db, ingest, ents):
    out = []
    for eid, m in ents.items():
        dc = m.get("device_class")
        if m.get("domain") != "sensor" or dc not in an.ANOMALY_KINDS:
            continue
        if m.get("state_class") in ("total", "total_increasing"):
            continue
        v = _num(ingest, eid)
        if v is None:
            continue
        r = an.anomaly_check(db, eid, v, dc, cfg.anomaly_z)
        if not r:
            continue
        unit = m.get("unit") or ""
        sev = "critical" if abs(r["z"]) >= 2 * cfg.anomaly_z else "warning"
        detail = (f"{m['name']} reads {v:g}{unit}, typical for this hour is {r['baseline']:g}{unit} "
                  f"(deviation {r['z']:+.1f}σ, {r['samples']} comparable days).")
        out.append(_ins(f"anomaly:{eid}", "anomaly", sev, eid, m.get("area"),
                        f"Unusual reading: {m['name']}", detail, r))
    return out


def detect_batteries(db, ingest, ents):
    out = []
    for eid, m in ents.items():
        if m.get("device_class") != "battery":
            continue
        live = ingest.live.get(eid)
        if not live:
            continue
        if m["domain"] == "binary_sensor":
            if live["state"] == "on":
                out.append(_ins(f"battery:{eid}", "battery", "warning", eid, m.get("area"),
                                f"Battery low: {m['name']}", f"{m['name']} reports a low battery."))
            continue
        v = to_float(live["state"])
        if v is None:
            continue
        fc = an.battery_forecast(db, eid)
        ev = {"level": v, **(fc or {})}
        if v <= cfg.battery_crit:
            out.append(_ins(f"battery:{eid}", "battery", "critical", eid, m.get("area"),
                            f"Battery critical: {m['name']} ({v:g}%)",
                            f"{m['name']} is at {v:g}%. Replace soon.", ev))
        elif v <= cfg.battery_warn:
            extra = f" At the current trend it is empty in ~{fc['days_to_empty']:g} days." if fc and "days_to_empty" in fc else ""
            out.append(_ins(f"battery:{eid}", "battery", "warning", eid, m.get("area"),
                            f"Battery low: {m['name']} ({v:g}%)", f"{m['name']} is at {v:g}%.{extra}", ev))
        elif fc and fc.get("days_to_15") is not None and fc["days_to_15"] <= 14:
            out.append(_ins(f"battery:{eid}", "battery", "info", eid, m.get("area"),
                            f"Battery depleting: {m['name']}",
                            f"{m['name']} is at {v:g}% and falls {abs(fc['slope_per_day']):.2f}%/day - "
                            f"expected to reach 15% in ~{fc['days_to_15']:g} days.", ev))
    return out


def _group_key(m, eid):
    return m.get("device_id") or eid


def detect_availability(db, ingest, ents):
    out, now = [], time.time()
    names = {r["id"]: r["name"] for r in db.query("SELECT id, name FROM objects WHERE type='Device'")}
    down: dict = {}
    for eid, live in ingest.live.items():
        m = ents.get(eid)
        if not m or m["domain"] in ("person",) or live["state"] != "unavailable":
            continue
        if now - live["last_changed"] < cfg.unavailable_min * 60:
            continue
        down.setdefault(_group_key(m, eid), []).append((eid, m, now - live["last_changed"]))
    for g, items in down.items():
        eid, m, age = max(items, key=lambda x: x[2])
        label = names.get(f"device:{g}") or m["name"]
        out.append(_ins(f"unavailable:{g}", "availability", "warning" if age > 6 * 3600 else "info", eid, m.get("area"),
                        f"Unavailable: {label}",
                        f"{len(items)} entit{'y' if len(items) == 1 else 'ies'} of {label} unavailable for {age / 3600:.1f} h.",
                        {"entities": [i[0] for i in items][:20], "hours": round(age / 3600, 2)}))
    since = now - 7 * 86400
    rows = db.query("SELECT entity_id, COUNT(*) c FROM states WHERE ts>=? AND state='unavailable' "
                    "GROUP BY entity_id HAVING c>=6", (since,))
    flap: dict = {}
    for r in rows:
        m = ents.get(r["entity_id"])
        if m:
            g = _group_key(m, r["entity_id"])
            flap[g] = max(flap.get(g, (0, None, None)), (r["c"], r["entity_id"], m), key=lambda x: x[0])
    for g, (c, eid, m) in flap.items():
        label = names.get(f"device:{g}") or m["name"]
        out.append(_ins(f"flapping:{g}", "availability", "warning" if c >= 12 else "info", eid, m.get("area"),
                        f"Unstable connection: {label}",
                        f"{label} dropped offline {c} times in the last 7 days.", {"drops_7d": c}))
    return out


def detect_open_and_rules(db, ingest, ents):
    out, now = [], time.time()
    open_windows = {}
    for eid, live in ingest.live.items():
        m = ents.get(eid)
        if not m or m["domain"] != "binary_sensor" or live["state"] != "on":
            continue
        dc = m.get("device_class")
        mins = (now - live["last_changed"]) / 60
        limit = {"door": cfg.open_door_min, "window": cfg.open_window_min,
                 "garage_door": cfg.open_garage_min}.get(dc)
        if dc == "window":
            open_windows.setdefault(m.get("area_id"), []).append((eid, m, mins))
        if limit is not None and mins >= limit:
            what = {"door": "Door", "window": "Window", "garage_door": "Garage door"}[dc]
            out.append(_ins(f"open:{eid}", "open", "info", eid, m.get("area"),
                            f"{what} open for {mins:.0f} min: {m['name']}",
                            f"{m['name']} has been open for {mins:.0f} minutes.", {"minutes": round(mins)}))
    # Rule: heating while a window is open in the same room
    for eid, live in ingest.live.items():
        m = ents.get(eid)
        if not m or m["domain"] != "climate" or live["state"] in ("off", "unavailable", "unknown"):
            continue
        if (live["attrs"] or {}).get("hvac_action") == "idle":
            continue
        for w_eid, w, mins in open_windows.get(m.get("area_id"), []):
            if mins >= 10:
                detail = (f"{m['name']} is active while {w['name']} has been open for {mins:.0f} min "
                          f"in {m.get('area')}. Heating energy is being wasted.")
                out.append(_ins(f"rule:heat_window:{eid}", "rule", "warning", eid, m.get("area"),
                                f"Heating with open window: {m.get('area')}", detail,
                                {"window": w_eid, "minutes": round(mins)},
                                {"domain": "climate", "service": "turn_off", "label": f"Turn off {m['name']}",
                                 "data": {"entity_id": eid}}))
                break
    # Rule: everybody away but lights are on
    persons = [(e, l) for e, l in ingest.live.items() if e.startswith("person.") and e in ents]
    if persons and all(l["state"] != "home" for _, l in persons):
        away = min(now - l["last_changed"] for _, l in persons)
        if away >= cfg.empty_home_min * 60:
            lights = [e for e, l in ingest.live.items() if e.startswith("light.") and l["state"] == "on" and e in ents]
            if lights:
                out.append(_ins("rule:empty_home_lights", "rule", "info", lights[0], None,
                                f"Nobody home, {len(lights)} light(s) on",
                                f"Everyone has been away for {away / 60:.0f} min but {len(lights)} light(s) are still on.",
                                {"lights": lights[:20]},
                                {"domain": "light", "service": "turn_off", "label": "Turn off these lights",
                                 "data": {"entity_id": lights[:50]}}))
    return out


def detect_energy(db, ingest, ents):
    out = []
    for eid, m in ents.items():
        if m.get("device_class") != "energy" or m.get("state_class") not in ("total", "total_increasing"):
            continue
        daily = [d for d in an.energy_daily(db, eid, 21) if d["complete"]]
        if len(daily) < 8:
            continue
        y, hist = daily[-1], np.array([d["kwh"] for d in daily[-15:-1]])
        med = float(np.median(hist))
        mad = 1.4826 * float(np.median(np.abs(hist - med)))
        if y["kwh"] > med + 3.5 * max(mad, 0.1 * med) and y["kwh"] > 1.25 * med:
            out.append(_ins(f"energy:{eid}:{y['date']}", "energy", "info", eid, m.get("area"),
                            f"High consumption on {y['date']}: {m['name']}",
                            f"{y['kwh']:.1f} kWh vs. typical {med:.1f} kWh/day.",
                            {"date": y["date"], "kwh": y["kwh"], "typical": round(med, 2)}))
    return out


DETECTORS = [detect_anomalies, detect_batteries, detect_availability, detect_open_and_rules, detect_energy]


# ----------------------------------------------------------------- case sync
def sync_insights(db, detections: list) -> dict:
    now = time.time()
    seen = set()
    created = 0
    for d in detections:
        seen.add(d["key"])
        row = db.one("SELECT id FROM insights WHERE key=? AND status!='resolved' ORDER BY id DESC LIMIT 1", (d["key"],))
        ev, pr = json.dumps(d["evidence"], ensure_ascii=False), json.dumps(d["proposal"], ensure_ascii=False)
        if row:
            db.execute("UPDATE insights SET severity=?, title=?, detail=?, evidence=?, proposal=?, updated=? WHERE id=?",
                       (d["severity"], d["title"], d["detail"], ev, pr, now, row["id"]))
        else:
            db.execute("INSERT INTO insights(key,kind,severity,entity_id,area,title,detail,evidence,proposal,"
                       "status,created,updated) VALUES(?,?,?,?,?,?,?,?,?,'open',?,?)",
                       (d["key"], d["kind"], d["severity"], d["entity_id"], d["area"], d["title"], d["detail"],
                        ev, pr, now, now))
            created += 1
    resolved = 0
    for r in db.query("SELECT id, key FROM insights WHERE status IN ('open','ack','dismissed','actioned')"):
        if r["key"] not in seen:
            db.execute("UPDATE insights SET status='resolved', resolved_at=? WHERE id=?", (now, r["id"]))
            resolved += 1
    return {"detected": len(detections), "created": created, "resolved": resolved}


def run_analysis(db, ingest) -> dict:
    t0 = time.time()
    ents = entities(db)
    detections = []
    for det in DETECTORS:
        try:
            detections.extend(det(db, ingest, ents))
        except Exception:  # noqa: BLE001
            log.exception("detector %s failed", det.__name__)
            return {"error": f"{det.__name__} failed", "detected": 0}
    res = sync_insights(db, detections)
    res["seconds"] = round(time.time() - t0, 2)
    db.meta_set("last_analysis", json.dumps({"ts": time.time(), **res}))
    log.info("analysis: %s", res)
    return res
