# TheBoringSight - Home Assistant add-on repository

[![Add repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Ftheboringalex%2FTheBoringSight-addon)

A Palantir-style operational layer for your smart home: an ontology of areas, devices, sensors and people,
transparent analytics (anomalies, forecasts, battery and availability maintenance, presence prediction,
link analysis), cases with governed and audited actions, and an optional AI layer (Anthropic API or Gemini).

## Install

1. Home Assistant → Settings → Add-ons → Add-on Store → ⋮ → Repositories
2. Add `https://github.com/theboringalex/TheBoringSight-addon`
3. Install **TheBoringSight**, start it, open **Sight** in the sidebar.

By default the add-on talks to Home Assistant through the Supervisor, so no token is required.
See [`theboringsight/DOCS.md`](theboringsight/DOCS.md) for all options.

> Independent project, not affiliated with Palantir.
