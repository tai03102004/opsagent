"""Runtime configuration. Everything is overridable via environment variables."""

import os
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("OPSAGENT_DATA_DIR", ROOT / "data"))
OUTBOX_DIR = Path(os.getenv("OPSAGENT_OUTBOX_DIR", ROOT / "outbox"))
SCENARIOS_FILE = ROOT / "evals" / "scenarios.yaml"

# A fixed reference clock keeps time-based rules ("shipped > 7 days ago") deterministic.
DEFAULT_NOW = "2026-10-06T12:00:00+00:00"

DEFAULT_MODEL = "claude-opus-5-5"


def reference_now() -> datetime:
    return datetime.fromisoformat(os.getenv("OPSAGENT_NOW", DEFAULT_NOW))


def model_name() -> str:
    return os.getenv("OPSAGENT_MODEL", DEFAULT_MODEL)


def llm_mode() -> str:
    """'auto' (Claude if credentials exist, else rules), 'claude' (no fallback) or 'off'."""
    return os.getenv("OPSAGENT_LLM", "auto").lower()


def has_llm_credentials() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))
