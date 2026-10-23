"""Central configuration. Everything comes from environment variables
(set directly in docker-compose.yml, no .env file)."""
import os


def _get(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _bool(name: str, default: bool) -> bool:
    return _get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(_get(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_get(name, str(default)))
    except ValueError:
        return default


def _list(name: str, default: str) -> list:
    return [x.strip() for x in _get(name, default).split(",") if x.strip()]


class Settings:
    def __init__(self) -> None:
        self.tz = _get("TZ", "Europe/Berlin")
        self.log_level = _get("LOG_LEVEL", "INFO").upper()
        self.db_path = _get("DB_PATH", "/data/sight.db")
        self.api_key = _get("API_KEY", "")  # optional; protects /api/*

        # --- Home Assistant connector
        self.ha_url = _get("HA_URL", "http://192.168.1.5:8123")
        self.ha_token = _get("HA_TOKEN", "")
        self.ha_ws_url = _get("HA_WS_URL", "")  # optional override (Supervisor proxy)
        self.ha_verify_ssl = _bool("HA_VERIFY_SSL", True)
        self.track_domains = _list(
            "TRACK_DOMAINS",
            "sensor,binary_sensor,climate,light,switch,lock,cover,person,device_tracker,weather,fan,water_heater",
        )
        self.exclude_regex = _get(
            "EXCLUDE_ENTITY_REGEX",
            r"(_linkquality$|_rssi$|_signal_strength$|_uptime$|_last_seen$|_ssid$|_ip_address$|_firmware|_update$|^sensor\.sun_|^sensor\.date|^sensor\.time)",
        )
        self.backfill_days = _int("BACKFILL_DAYS", 28)
        self.retention_days = _int("RETENTION_DAYS", 90)

        # --- Analysis
        self.analysis_interval = _int("ANALYSIS_INTERVAL_SEC", 300)
        self.anomaly_z = _float("ANOMALY_Z", 4.0)
        self.open_door_min = _int("OPEN_DOOR_MIN", 30)
        self.open_window_min = _int("OPEN_WINDOW_MIN", 240)
        self.open_garage_min = _int("OPEN_GARAGE_MIN", 20)
        self.unavailable_min = _int("UNAVAILABLE_MIN", 30)
        self.battery_warn = _float("BATTERY_WARN", 20)
        self.battery_crit = _float("BATTERY_CRIT", 10)
        self.empty_home_min = _int("EMPTY_HOME_MIN", 30)

        # --- Actions (governed write-back to Home Assistant)
        self.actions_enabled = _bool("ACTIONS_ENABLED", False)
        self.allowed_services = _list(
            "ALLOWED_SERVICES",
            "persistent_notification.create,climate.turn_off,light.turn_off",
        )

        # --- AI layer (optional)
        self.ai_provider = _get("AI_PROVIDER", "none").lower()  # none | anthropic | gemini
        self.anthropic_key = _get("ANTHROPIC_API_KEY", "")
        self.anthropic_model = _get("ANTHROPIC_MODEL", "")
        self.gemini_key = _get("GEMINI_API_KEY", "")
        self.gemini_model = _get("GEMINI_MODEL", "gemini-2.5-flash")


cfg = Settings()
