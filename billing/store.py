"""Local-only persistence: in-memory or JSON files on disk.

No databases, no network, no external services. The file store keeps:
- events.jsonl        : append-only log of accepted usage events
- state.json          : processed event ids, closed periods, generated invoices
"""
import json
import os
from dataclasses import asdict
from decimal import Decimal
from typing import Dict, List, Optional


class _Enc(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return str(o)
        return super().default(o)


class MemoryStore:
    def __init__(self):
        self.events: List[dict] = []          # accepted events (incl. adjustments)
        self.seen_ids: set = set()            # idempotency keys
        self.closed: Dict[str, dict] = {}     # period_id -> invoice snapshot
        self.invoices: Dict[str, dict] = {}   # (customer|period) -> invoice

    # --- events ---
    def has_event(self, event_id: str) -> bool:
        return event_id in self.seen_ids

    def append_event(self, record: dict) -> None:
        self.seen_ids.add(record["event_id"])
        self.events.append(record)

    def all_events(self) -> List[dict]:
        return list(self.events)

    # --- periods / invoices ---
    def is_closed(self, period_id: str) -> bool:
        return period_id in self.closed

    def close_period(self, period_id: str, snapshot: dict) -> None:
        self.closed[period_id] = snapshot

    def save_invoice(self, customer_id: str, period_id: str, invoice: dict) -> None:
        self.invoices[f"{customer_id}|{period_id}"] = invoice

    def get_invoice(self, customer_id: str, period_id: str) -> Optional[dict]:
        return self.invoices.get(f"{customer_id}|{period_id}")


class FileStore(MemoryStore):
    """JSON-file-backed store rooted at a local directory."""

    def __init__(self, root: str):
        super().__init__()
        self.root = root
        os.makedirs(root, exist_ok=True)
        self._events_path = os.path.join(root, "events.jsonl")
        self._state_path = os.path.join(root, "state.json")
        self._load()

    def _load(self):
        if os.path.exists(self._events_path):
            with open(self._events_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        rec = json.loads(line)
                        self.events.append(rec)
                        self.seen_ids.add(rec["event_id"])
        if os.path.exists(self._state_path):
            with open(self._state_path, encoding="utf-8") as fh:
                state = json.load(fh)
            self.closed = state.get("closed", {})
            self.invoices = state.get("invoices", {})

    def _flush_state(self):
        tmp = self._state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"closed": self.closed, "invoices": self.invoices}, fh,
                      cls=_Enc, indent=2, sort_keys=True)
        os.replace(tmp, self._state_path)

    def append_event(self, record: dict) -> None:
        super().append_event(record)
        with open(self._events_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, cls=_Enc, sort_keys=True) + "\n")

    def close_period(self, period_id: str, snapshot: dict) -> None:
        super().close_period(period_id, snapshot)
        self._flush_state()

    def save_invoice(self, customer_id: str, period_id: str, invoice: dict) -> None:
        super().save_invoice(customer_id, period_id, invoice)
        self._flush_state()
