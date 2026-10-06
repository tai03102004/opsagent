"""Shared fallback logic for LLM-backed components."""

from __future__ import annotations

import logging
from typing import Callable, Optional, TypeVar

log = logging.getLogger(__name__)
T = TypeVar("T")

# Errors that will not fix themselves by retrying (bad key, no permission, unknown model).
PERMANENT_ERRORS = {"AuthenticationError", "PermissionDeniedError", "NotFoundError"}


class CircuitFallback:
    """Call primary; on failure use fallback. A permanent error opens the circuit for the process lifetime."""

    def __init__(self, name: str):
        self.name = name
        self.open_reason: Optional[str] = None

    def call(self, primary: Callable[[], T], fallback: Callable[[str], T]) -> T:
        if self.open_reason:
            return fallback(f"circuit open: {self.open_reason}")
        try:
            return primary()
        except Exception as exc:  # noqa: BLE001 - any LLM failure must degrade, not crash
            reason = f"{type(exc).__name__}: {str(exc)[:200]}"
            if type(exc).__name__ in PERMANENT_ERRORS:
                self.open_reason = reason
                log.warning("%s: permanent error, disabling LLM for this process: %s", self.name, reason)
            else:
                log.warning("%s fallback: %s", self.name, reason)
            return fallback(reason)
