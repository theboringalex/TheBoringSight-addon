"""Translate the Home Assistant add-on options into the environment variables
TheBoringSight reads. Prints `export KEY='value'` lines for `eval` in run.sh.

Access to Home Assistant:
  * ha_token set  -> use ha_url + ha_token as given (long-lived access token)
  * ha_token empty -> use the Supervisor proxy with the add-on's own token
"""
import json
import os
import shlex

OPTIONS = os.environ.get("OPTIONS_FILE", "/data/options.json")
o = {}
try:
    with open(OPTIONS) as f:
        o = json.load(f)
except FileNotFoundError:
    pass


def b(v):
    return "true" if v else "false"


env = {
    "DB_PATH": "/data/sight.db",
    "TZ": o.get("timezone") or "Europe/Berlin",
    "LOG_LEVEL": o.get("log_level", "INFO"),
    "BACKFILL_DAYS": o.get("backfill_days", 28),
    "RETENTION_DAYS": o.get("retention_days", 90),
    "ANALYSIS_INTERVAL_SEC": o.get("analysis_interval_sec", 300),
    "ANOMALY_Z": o.get("anomaly_z", 4.0),
    "BATTERY_WARN": o.get("battery_warn", 20),
    "BATTERY_CRIT": o.get("battery_crit", 10),
    "OPEN_DOOR_MIN": o.get("open_door_min", 30),
    "OPEN_WINDOW_MIN": o.get("open_window_min", 240),
    "ACTIONS_ENABLED": b(o.get("actions_enabled", False)),
    "ALLOWED_SERVICES": o.get("allowed_services", "persistent_notification.create"),
    "API_KEY": o.get("api_key") or "",
    "AI_PROVIDER": o.get("ai_provider", "none"),
    "ANTHROPIC_API_KEY": o.get("anthropic_api_key") or "",
    "ANTHROPIC_MODEL": o.get("anthropic_model") or "",
    "GEMINI_API_KEY": o.get("gemini_api_key") or "",
    "GEMINI_MODEL": o.get("gemini_model", "gemini-2.5-flash"),
}

token = (o.get("ha_token") or "").strip()
url = (o.get("ha_url") or "").strip()
if token:
    env["HA_TOKEN"] = token
    env["HA_URL"] = url or "http://homeassistant:8123"
else:
    env["HA_TOKEN"] = os.environ.get("SUPERVISOR_TOKEN", "")
    env["HA_URL"] = "http://supervisor/core"
    env["HA_WS_URL"] = "ws://supervisor/core/websocket"

for k, v in env.items():
    print(f"export {k}={shlex.quote(str(v))}")
