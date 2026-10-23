# TheBoringSight

Ontology, forecasts, predictive maintenance and governed actions for your Home Assistant.
Open it from the **Sight** entry in the sidebar.

## Access to Home Assistant

* **Default (leave `ha_token` empty):** the add-on uses the Supervisor proxy with its own token. Nothing to configure.
* **Own token:** set `ha_url` (e.g. `http://192.168.1.5:8123`) and `ha_token` (long-lived access token from your profile).

## Options

| Option | Meaning |
|---|---|
| `backfill_days` | History pulled from the recorder on first start |
| `anomaly_z` | Robust z-score threshold for "unusual reading" |
| `actions_enabled` | `false` = dry run. Proposals are shown and audited, nothing is sent to Home Assistant |
| `allowed_services` | Allow-list of services a case may call |
| `api_key` | Only needed if you expose the optional direct port |
| `ai_provider` | `none`, `anthropic` or `gemini`, plus the matching key |

## Notes

* Forecasts and anomaly baselines need a few days of history. The recorder backfill provides it on first start.
* Data is stored in the add-on's persistent `/data` volume (SQLite).
