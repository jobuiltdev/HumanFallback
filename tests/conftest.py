from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from humanfallback import cli
from humanfallback.adapters import AdapterError, ErrorCode, MockGibworkAdapter
from humanfallback.classifier import default_classifier
from humanfallback.contracts import build_contract
from humanfallback.models import Reward, TaskContract
from humanfallback.store import ContractStore

PHYSICAL_REQUEST = "Go to the hardware store on Main St and take a photo of the paint aisle."


@pytest.fixture(autouse=True)
def _never_touch_real_gibwork(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may spawn the real Gibwork CLI. Tests that need the gibwork
    adapter install their own fake factory on top of this guard."""
    monkeypatch.setenv("HF_GIBWORK_BIN", str(Path("/nonexistent/gibwork-must-not-run")))
    monkeypatch.delenv("HF_ADAPTER", raising=False)

    def refuse(writes: bool):  # noqa: ANN202
        raise AdapterError(ErrorCode.CONFIG_ERROR, "real gibwork adapter is disabled in tests")

    monkeypatch.setitem(cli.ADAPTER_FACTORIES, "gibwork", refuse)


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def store(tmp_path: Path) -> ContractStore:
    with ContractStore(tmp_path / "test.db") as s:
        yield s


class FakeClock:
    """Manually advanced clock for testing time-sensitive adapter behaviour."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: int) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def adapter(clock: FakeClock) -> MockGibworkAdapter:
    return MockGibworkAdapter(clock=clock)


@pytest.fixture
def contract() -> TaskContract:
    classification = default_classifier().classify(PHYSICAL_REQUEST)
    return build_contract(
        PHYSICAL_REQUEST,
        classification,
        Reward(amount="5.00"),
        tags=["errand", "photo"],
    )
