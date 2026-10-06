"""Append-only JSONL event log standing in for external systems (Zendesk, Slack, Stripe, email).

State = seed data + replay of these events, so the CLI and API see the same world.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STREAMS = ("tickets", "slack", "emails", "refunds", "subscriptions", "approvals", "audit")


class Outbox:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, stream: str) -> Path:
        if stream not in STREAMS:
            raise ValueError(f"unknown stream {stream}")
        return self.dir / f"{stream}.jsonl"

    def append(self, stream: str, record: dict[str, Any]) -> dict[str, Any]:
        record = {"logged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **record}
        with self._lock, self._path(stream).open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return record

    def read(self, stream: str) -> list[dict[str, Any]]:
        path = self._path(stream)
        if not path.exists():
            return []
        with self._lock:
            return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def next_id(self, stream: str, prefix: str) -> str:
        return f"{prefix}-{len(self.read(stream)) + 1:04d}"

    def clear(self) -> None:
        for stream in STREAMS:
            self._path(stream).unlink(missing_ok=True)
