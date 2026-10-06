import os
from pathlib import Path

import pytest

# Tests must never hit the network: force the rule/template implementations.
os.environ["OPSAGENT_LLM"] = "off"

from opsagent.config import DATA_DIR, reference_now  # noqa: E402
from opsagent.store import Store  # noqa: E402


@pytest.fixture
def store() -> Store:
    return Store.load(DATA_DIR, reference_now())


@pytest.fixture
def outbox_dir(tmp_path: Path) -> Path:
    return tmp_path / "outbox"
