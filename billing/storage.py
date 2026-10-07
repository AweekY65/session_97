"""Local-only storage backends: in-memory or a JSON file on disk.

No database, no network, no external service of any kind.
"""
from __future__ import annotations

import json
import os
from typing import Dict, Optional, Set, Tuple

from .models import UsageEvent


class InMemoryStore:
    """Holds all state in process memory."""

    def __init__(self):
        self.events: Dict[str, UsageEvent] = {}
        # event_id -> period_key the event is billed in (late events are
        # reassigned to the first open period after their original one)
        self.assignments: Dict[str, str] = {}
        # (customer_id, period_key) that have been closed
        self.closed: Set[Tuple[str, str]] = set()
        # (customer_id, period_key) -> invoice snapshot dict, frozen at close time
        self.snapshots: Dict[Tuple[str, str], dict] = {}

    def flush(self) -> None:
        pass


class JsonFileStore(InMemoryStore):
    """Persists the full state to a single local JSON file after each mutation."""

    def __init__(self, path: str):
        super().__init__()
        self.path = path
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.events = {k: UsageEvent.from_dict(v) for k, v in data.get("events", {}).items()}
            self.assignments = dict(data.get("assignments", {}))
            self.closed = {tuple(x) for x in data.get("closed", [])}
            self.snapshots = {tuple(k.split("|", 1)): v for k, v in data.get("snapshots", {}).items()}

    def flush(self) -> None:
        data = {
            "events": {k: v.to_dict() for k, v in self.events.items()},
            "assignments": self.assignments,
            "closed": [list(x) for x in sorted(self.closed)],
            "snapshots": {f"{k[0]}|{k[1]}": v for k, v in self.snapshots.items()},
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
