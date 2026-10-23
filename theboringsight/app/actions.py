"""Governed actions ("Action Types" in Palantir terms).

An insight carries a *proposal*. Nothing is written back to Home Assistant until
a human approves it, the service is on the allow-list AND ACTIONS_ENABLED=true.
Every attempt (including dry runs and denials) lands in the audit table.
"""
import json
import time

from .config import cfg


def audit(db, actor: str, action: str, target: str, payload, result) -> None:
    db.execute("INSERT INTO audit(ts,actor,action,target,payload,result) VALUES(?,?,?,?,?,?)",
               (time.time(), actor, action, target, json.dumps(payload, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False)))


async def run_proposal(db, connector, insight_id: int, actor: str = "ui") -> dict:
    row = db.one("SELECT * FROM insights WHERE id=?", (insight_id,))
    if not row:
        return {"ok": False, "error": "insight not found"}
    proposal = json.loads(row["proposal"] or "{}")
    if not proposal:
        return {"ok": False, "error": "insight has no proposed action"}
    svc = f"{proposal['domain']}.{proposal['service']}"
    target = f"insight:{insight_id}"
    if svc not in cfg.allowed_services:
        res = {"ok": False, "error": f"service {svc} is not in ALLOWED_SERVICES"}
        audit(db, actor, "denied", target, proposal, res)
        return res
    if not cfg.actions_enabled:
        res = {"ok": True, "dry_run": True, "would_call": svc, "data": proposal.get("data"),
               "message": "ACTIONS_ENABLED=false - nothing was sent to Home Assistant."}
        audit(db, actor, "dry_run", target, proposal, res)
        return res
    try:
        r = await connector.call_service(proposal["domain"], proposal["service"], proposal.get("data") or {})
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "error": str(e)}
        audit(db, actor, "failed", target, proposal, res)
        return res
    ok = 200 <= r["status"] < 300
    res = {"ok": ok, "service": svc, "http_status": r["status"]}
    audit(db, actor, "executed" if ok else "failed", target, proposal, res)
    if ok:
        db.execute("UPDATE insights SET status='actioned', updated=? WHERE id=?", (time.time(), insight_id))
    return res
