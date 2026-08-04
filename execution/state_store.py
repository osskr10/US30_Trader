"""
State store para dedup y (más adelante) el mapeo alert_id ↔ dealId + estado de BE.

Dos implementaciones con la misma interfaz:
  - InMemoryStateStore: para tests.
  - JsonStateStore: persistente en disco (JSON), para el bot real.

Interfaz mínima que usa el executor:
  is_seen(alert_id) -> bool
  mark(alert_id, record: dict) -> None
  get(alert_id) -> dict | None
  all_records() -> dict[alert_id, record]
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Dict, Optional


class InMemoryStateStore:
    def __init__(self):
        self._d: Dict[str, dict] = {}
        self._lock = threading.RLock()

    def is_seen(self, alert_id: str) -> bool:
        with self._lock:
            return alert_id in self._d

    def mark(self, alert_id: str, record: dict) -> None:
        with self._lock:
            self._d[alert_id] = dict(record)

    def get(self, alert_id: str) -> Optional[dict]:
        with self._lock:
            rec = self._d.get(alert_id)
            return dict(rec) if rec is not None else None

    def all_records(self) -> Dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._d.items()}


class JsonStateStore:
    """Persistente. Carga al construir; reescribe el archivo completo en cada mark
    (los volúmenes son chicos: 1 registro por setup)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._d: Dict[str, dict] = {}
        if self.path.exists():
            try:
                self._d = json.loads(self.path.read_text(encoding="utf-8")) or {}
            except (json.JSONDecodeError, OSError):
                self._d = {}

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._d, indent=2, sort_keys=True, default=str),
                       encoding="utf-8")
        tmp.replace(self.path)   # escritura atómica

    def is_seen(self, alert_id: str) -> bool:
        with self._lock:
            return alert_id in self._d

    def mark(self, alert_id: str, record: dict) -> None:
        with self._lock:
            self._d[alert_id] = dict(record)
            self._flush()

    def get(self, alert_id: str) -> Optional[dict]:
        with self._lock:
            rec = self._d.get(alert_id)
            return dict(rec) if rec is not None else None

    def all_records(self) -> Dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._d.items()}
