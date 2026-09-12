"""Runtime configuration: where local state lives."""

from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "HF_HOME"
DEFAULT_DIRNAME = ".humanfallback"
DB_FILENAME = "humanfallback.db"
MOCK_STATE_FILENAME = "mock_gibwork.json"


def data_dir() -> Path:
    """Directory holding the contract database and mock adapter state.

    Overridable with the HF_HOME environment variable. Created on first use.
    """
    raw = os.environ.get(ENV_HOME)
    path = Path(raw).expanduser() if raw else Path.home() / DEFAULT_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / DB_FILENAME


def mock_state_path() -> Path:
    return data_dir() / MOCK_STATE_FILENAME
