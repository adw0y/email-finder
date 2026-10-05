"""Local ledger of how many free credits each service has used.

Stored in usage.json next to the app. Monthly meters reset on the 1st;
lifetime meters (Serper's one-time 2,500) never reset.
"""
from __future__ import annotations

import json
import threading
from datetime import date
from pathlib import Path


def this_month() -> str:
    return date.today().strftime("%Y-%m")


class UsageStore:
    def __init__(self, path: str | Path = "usage.json"):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.data = {"months": {}, "lifetime": {}, "exhausted": {}}
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text()))
            except (json.JSONDecodeError, OSError):
                pass

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self.data, indent=2))
        except OSError:
            pass

    def used(self, meter: str, period: str) -> float:
        if period == "lifetime":
            return float(self.data["lifetime"].get(meter, 0))
        return float(self.data["months"].get(this_month(), {}).get(meter, 0))

    def add(self, meter: str, cost: float, period: str) -> None:
        with self._lock:
            if period == "lifetime":
                self.data["lifetime"][meter] = self.used(meter, period) + cost
            else:
                month = self.data["months"].setdefault(this_month(), {})
                month[meter] = month.get(meter, 0) + cost
            self._save()

    # A service that told us it's out of credits stays off until next month
    # (or until the user resets it from the sidebar).
    def mark_exhausted(self, service: str) -> None:
        with self._lock:
            self.data["exhausted"][service] = this_month()
            self._save()

    def is_exhausted(self, service: str) -> bool:
        return self.data["exhausted"].get(service) == this_month()

    def reset(self, service: str) -> None:
        with self._lock:
            self.data["exhausted"].pop(service, None)
            self._save()
