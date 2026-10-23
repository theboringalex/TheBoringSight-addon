#!/bin/sh
set -e
cd /srv
# Translate /data/options.json (add-on configuration) into environment variables.
eval "$(python3 /srv/tools/addon_env.py)"
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --log-level info
