"""Runtime configuration: where local state lives and which backend to use.

Nothing here touches wallet credentials. The Gibwork CLI resolves its own
keypair from its profile configuration; HumanFallback only names the
profile and environment to use.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "HF_HOME"
ENV_ADAPTER = "HF_ADAPTER"
ENV_GIBWORK_PROFILE = "HF_GIBWORK_PROFILE"
ENV_GIBWORK_ENVIRONMENT = "HF_GIBWORK_ENVIRONMENT"
ENV_GIBWORK_BIN = "HF_GIBWORK_BIN"
ENV_GIBWORK_TIMEOUT = "HF_GIBWORK_TIMEOUT_S"

DEFAULT_DIRNAME = ".humanfallback"
DB_FILENAME = "humanfallback.db"
MOCK_STATE_FILENAME = "mock_gibwork.json"

DEFAULT_ADAPTER = "mock"
ADAPTERS = ("mock", "gibwork")
DEFAULT_GIBWORK_TIMEOUT_S = 60.0


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


def adapter_name() -> str:
    """Backend selected by HF_ADAPTER. Defaults to the mock; real Gibwork
    operations only happen when 'gibwork' is chosen explicitly."""
    return os.environ.get(ENV_ADAPTER, DEFAULT_ADAPTER).strip().lower() or DEFAULT_ADAPTER


def gibwork_profile() -> str | None:
    """Gibwork CLI profile name, or None to let the CLI use its own default."""
    return os.environ.get(ENV_GIBWORK_PROFILE) or None


def gibwork_environment() -> str | None:
    """Explicit Gibwork environment override ('stage' or 'production'), or None."""
    return os.environ.get(ENV_GIBWORK_ENVIRONMENT) or None


def gibwork_bin() -> str | None:
    """Explicit path to the gibwork executable or its bin.js, or None for PATH lookup."""
    return os.environ.get(ENV_GIBWORK_BIN) or None


def gibwork_timeout_s() -> float:
    raw = os.environ.get(ENV_GIBWORK_TIMEOUT)
    try:
        return float(raw) if raw else DEFAULT_GIBWORK_TIMEOUT_S
    except ValueError:
        return DEFAULT_GIBWORK_TIMEOUT_S
