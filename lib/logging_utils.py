"""JSONL event log: every signal check, decision, and order event, one JSON
object per line, one file per UTC day (data/logs/YYYY-MM-DD.jsonl). This is
the audit trail for reviewing dry-run cycles and, later, live trading.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


class EventLogger:
    def __init__(self, data_dir: str | Path = "data", now: Callable[[], datetime] | None = None):
        self.logs_dir = Path(data_dir) / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._now = now or (lambda: datetime.now(timezone.utc))

    def log(self, event: str, **fields: Any) -> dict:
        ts = self._now()
        record = {"timestamp": ts.isoformat(), "event": event, **fields}
        path = self.logs_dir / f"{ts.strftime('%Y-%m-%d')}.jsonl"
        with path.open("a") as f:
            f.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        return record
