"""AI layer ("AIP"): ontology-augmented generation.

The LLM never sees raw data dumps. We first resolve the question against the
ontology (relevant objects, their live state, statistics, forecasts, open
cases) and hand only that grounded context to the model. Without an API key a
deterministic local briefing is returned instead.
"""
import json
import re
import time

import aiohttp

from . import analytics as an
from . import insights as ins
from .config import cfg
from .ingest import to_float

SYSTEM = (
    "You are the analyst of a home 'digital twin'. Answer ONLY from the provided ontology context "
    "(objects, live states, statistics, forecasts, open cases). If the context does not contain the "
    "answer, say so. Cite object ids in backticks, e.g. `sensor.wohnzimmer_temperatur`. Be concise, "
    "concrete and practical. Never claim you executed an action; actions need human approval in the UI."
)


def _stats(db, eid: str):
    since = int(time.time() // 3600) - 24
    r = db.one("SELECT MIN(mn) mn, MAX(mx) mx, AVG(mean) av FROM hourly WHERE entity_id=? AND hour>=?", (eid, since))
    if not r or r["av"] is None:
        return None
    return {"min_24h": round(r["mn"], 2), "max_24h": round(r["mx"], 2), "avg_24h": round(r["av"], 2)}


def build_context(db, ingest, question: str) -> dict:
    ents = ins.entities(db)
    now = time.time()
    open_cases = db.query(
        "SELECT id, severity, kind, title, detail, area, entity_id FROM insights "
        "WHERE status IN ('open','ack') ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, id DESC LIMIT 15")
    people = {e: ingest.live[e]["state"] for e in ents if e.startswith("person.") and e in ingest.live}
    areas = db.query("SELECT name FROM objects WHERE type='Area' ORDER BY name")
    tokens = [t for t in re.findall(r"\w+", question.lower()) if len(t) > 2]
    scored = []
    for eid, m in ents.items():
        hay = f"{eid} {m['name']} {m.get('area') or ''} {m.get('device_class') or ''}".lower()
        sc = sum(1 for t in tokens if t in hay)
        if sc:
            scored.append((sc, eid, m))
    scored.sort(key=lambda x: -x[0])
    objs = []
    for _, eid, m in scored[:10]:
        live = ingest.live.get(eid, {})
        o = {"id": eid, "name": m["name"], "area": m.get("area"), "type": m["otype"], "state": live.get("state"),
             "unit": m.get("unit"), "since_min": round((now - live["last_changed"]) / 60) if live else None}
        if to_float(live.get("state")) is not None:
            o["stats"] = _stats(db, eid)
            fc = an.forecast(db, eid, 6)
            if fc["points"]:
                o["forecast_next_6h"] = [round(p["yhat"], 2) for p in fc["points"]]
        objs.append(o)
    return {"now": time.strftime("%Y-%m-%d %H:%M %Z"), "areas": [a["name"] for a in areas], "people": people,
            "open_cases": open_cases, "relevant_objects": objs, "tracked_entities": len(ents)}


def local_briefing(ctx: dict) -> str:
    lines = [f"Briefing ({ctx['now']}) - {ctx['tracked_entities']} tracked entities."]
    if ctx["people"]:
        lines.append("People: " + ", ".join(f"{k.split('.')[1]}={v}" for k, v in ctx["people"].items()))
    cases = ctx["open_cases"]
    lines.append(f"{len(cases)} open case(s)." if cases else "No open cases.")
    for c in cases[:8]:
        lines.append(f"- [{c['severity']}] {c['title']}: {c['detail']}")
    for o in ctx["relevant_objects"][:5]:
        lines.append(f"- {o['id']}: {o['state']}{o.get('unit') or ''}")
    lines.append("(No AI provider configured - this is the deterministic local briefing.)")
    return "\n".join(lines)


async def ask(db, ingest, question: str) -> dict:
    ctx = build_context(db, ingest, question)
    provider = cfg.ai_provider
    prompt = f"Question: {question}\n\nOntology context (JSON):\n{json.dumps(ctx, ensure_ascii=False)[:14000]}"
    try:
        if provider == "anthropic" and cfg.anthropic_key and cfg.anthropic_model:
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": cfg.anthropic_key, "anthropic-version": "2023-06-01",
                             "content-type": "application/json"},
                    json={"model": cfg.anthropic_model, "max_tokens": 1024, "system": SYSTEM,
                          "messages": [{"role": "user", "content": prompt}]},
                    timeout=aiohttp.ClientTimeout(total=90)) as r:
                    data = await r.json()
                    if r.status != 200:
                        raise RuntimeError(data.get("error", {}).get("message", f"HTTP {r.status}"))
                    text = "".join(b.get("text", "") for b in data.get("content", []))
            return {"answer": text, "provider": f"anthropic:{cfg.anthropic_model}", "context_objects": len(ctx["relevant_objects"])}
        if provider == "gemini" and cfg.gemini_key:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{cfg.gemini_model}:generateContent"
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    url, headers={"x-goog-api-key": cfg.gemini_key, "content-type": "application/json"},
                    json={"systemInstruction": {"parts": [{"text": SYSTEM}]},
                          "contents": [{"role": "user", "parts": [{"text": prompt}]}]},
                    timeout=aiohttp.ClientTimeout(total=90)) as r:
                    data = await r.json()
                    if r.status != 200:
                        raise RuntimeError(data.get("error", {}).get("message", f"HTTP {r.status}"))
                    text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
            return {"answer": text, "provider": f"gemini:{cfg.gemini_model}", "context_objects": len(ctx["relevant_objects"])}
    except Exception as e:  # noqa: BLE001
        return {"answer": local_briefing(ctx) + f"\n\n(AI call failed: {e})", "provider": "local-fallback",
                "context_objects": len(ctx["relevant_objects"])}
    return {"answer": local_briefing(ctx), "provider": "local", "context_objects": len(ctx["relevant_objects"])}
