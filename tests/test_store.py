from __future__ import annotations

from pathlib import Path

from humanfallback.classifier import default_classifier
from humanfallback.contracts import build_contract
from humanfallback.models import ContractStatus, Reward, TaskCategory, TaskContract
from humanfallback.store import ContractStore


def _make(request: str) -> TaskContract:
    return build_contract(request, default_classifier().classify(request), Reward(amount="1.00"))


def test_save_and_get_round_trip(store: ContractStore, contract: TaskContract) -> None:
    store.save(contract)
    assert store.get(contract.id) == contract


def test_get_missing_returns_none(store: ContractStore) -> None:
    assert store.get("nope") is None


def test_save_is_upsert(store: ContractStore, contract: TaskContract) -> None:
    store.save(contract)
    contract.transition_to(ContractStatus.DELEGATED)
    store.save(contract)
    loaded = store.get(contract.id)
    assert loaded is not None
    assert loaded.status is ContractStatus.DELEGATED
    assert len(store.list()) == 1


def test_list_newest_first(store: ContractStore) -> None:
    a = _make("Pick up the parcel.")
    b = _make("Sign the NDA.")
    store.save(a)
    b.created_at = a.created_at.replace(year=a.created_at.year + 1)
    store.save(b)
    assert [c.id for c in store.list()] == [b.id, a.id]


def test_list_filters(store: ContractStore) -> None:
    physical = _make("Pick up the parcel.")
    legal = _make("Sign the NDA.")
    legal.transition_to(ContractStatus.DELEGATED)
    store.save(physical)
    store.save(legal)

    assert [c.id for c in store.list(status=ContractStatus.READY)] == [physical.id]
    assert [c.id for c in store.list(category=TaskCategory.LEGAL_SIGNATURE)] == [legal.id]
    assert store.list(status=ContractStatus.READY, category=TaskCategory.LEGAL_SIGNATURE) == []


def test_delete(store: ContractStore, contract: TaskContract) -> None:
    store.save(contract)
    assert store.delete(contract.id) is True
    assert store.delete(contract.id) is False
    assert store.get(contract.id) is None


def test_count_by_status(store: ContractStore) -> None:
    a = _make("Pick up the parcel.")
    b = _make("Sign the NDA.")
    b.transition_to(ContractStatus.DELEGATED)
    store.save(a)
    store.save(b)
    assert store.count_by_status() == {ContractStatus.READY: 1, ContractStatus.DELEGATED: 1}


def test_creates_parent_directories(tmp_path: Path, contract: TaskContract) -> None:
    path = tmp_path / "nested" / "dir" / "hf.db"
    with ContractStore(path) as s:
        s.save(contract)
    assert path.exists()
    with ContractStore(path) as s:
        assert s.get(contract.id) is not None
