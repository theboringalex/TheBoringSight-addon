"""Connector contract. A connector turns an external system into
ontology objects/links (via Ingest.replace_ontology) and time series
(via Ingest.record_state / record_history_batch).

To add a new data source (UniFi, Proxmox, MQTT, n8n webhook, ...):
  1. subclass Connector
  2. implement run() (long-running, must handle its own reconnects)
  3. optionally implement call_service() if it supports governed actions
  4. register it in app/main.py
"""
from abc import ABC, abstractmethod


class Connector(ABC):
    name: str = "base"
    status: dict

    @abstractmethod
    async def run(self) -> None:
        """Long-running task. Must never return unless cancelled."""

    async def call_service(self, domain: str, service: str, data: dict) -> dict:
        raise NotImplementedError(f"{self.name} does not support actions")
