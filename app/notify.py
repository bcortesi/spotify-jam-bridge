import logging
from typing import Optional

import requests

log = logging.getLogger("jam.notify")


class Notifier:
    def __init__(self, ha_url: Optional[str], ha_token: Optional[str],
                 ha_entity: Optional[str], webhook_url: Optional[str]):
        self.ha_url = ha_url.rstrip("/") if ha_url else None
        self.ha_token = ha_token
        self.ha_entity = ha_entity
        self.webhook_url = webhook_url

    def push(self, state: dict) -> None:
        self._push_ha(state)
        self._push_webhook(state)

    def _push_ha(self, state: dict) -> None:
        if not (self.ha_url and self.ha_token and self.ha_entity):
            return
        value = state.get("qr_payload") or ""
        domain = self.ha_entity.split(".", 1)[0]
        try:
            if domain == "input_text":
                r = requests.post(f"{self.ha_url}/api/services/input_text/set_value",
                                  headers=self._h(), timeout=10,
                                  json={"entity_id": self.ha_entity, "value": value[:255]})
            else:
                # Any other domain: write the state directly via the REST API (creates a sensor).
                r = requests.post(f"{self.ha_url}/api/states/{self.ha_entity}",
                                  headers=self._h(), timeout=10,
                                  json={"state": "on" if state.get("active") else "off",
                                        "attributes": {k: v for k, v in state.items() if k != "raw"}})
            r.raise_for_status()
            log.info("Pushed Jam state to Home Assistant (%s)", self.ha_entity)
        except Exception as e:
            log.warning("Home Assistant push failed: %s", e)

    def _push_webhook(self, state: dict) -> None:
        if not self.webhook_url:
            return
        try:
            requests.post(self.webhook_url, json=state, timeout=10).raise_for_status()
            log.info("Pushed Jam state to webhook")
        except Exception as e:
            log.warning("Webhook push failed: %s", e)

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.ha_token}", "Content-Type": "application/json"}
