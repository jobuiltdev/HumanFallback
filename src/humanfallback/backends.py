"""Adapter construction. The backend name is chosen once per process."""

from __future__ import annotations

from collections.abc import Callable

from humanfallback import config
from humanfallback.adapters import GibworkAdapter, GibworkMcpAdapter, MockGibworkAdapter
from humanfallback.adapters.errors import AdapterError, ErrorCode

AdapterFactory = Callable[[bool], GibworkAdapter]


def _mock_factory(writes: bool) -> GibworkAdapter:
    return MockGibworkAdapter(state_path=config.mock_state_path())


def _gibwork_factory(writes: bool) -> GibworkAdapter:
    from humanfallback.adapters.gibwork import resolve_gibwork_command

    return GibworkMcpAdapter(
        profile=config.gibwork_profile(),
        environment=config.gibwork_environment(),
        writes=writes,
        command=resolve_gibwork_command(config.gibwork_bin()),
        timeout=config.gibwork_timeout_s(),
    )


ADAPTER_FACTORIES: dict[str, AdapterFactory] = {
    "mock": _mock_factory,
    "gibwork": _gibwork_factory,
}


def resolve_adapter_name(name: str | None) -> str:
    chosen = (name or config.adapter_name()).lower()
    if chosen not in ADAPTER_FACTORIES:
        raise AdapterError(
            ErrorCode.CONFIG_ERROR,
            f"unknown adapter {chosen!r}; choose one of {', '.join(ADAPTER_FACTORIES)}",
        )
    return chosen


def adapter_factory_for(name: str) -> AdapterFactory:
    """A factory bound to one backend name. Looks the name up at call time so
    tests can swap entries in ADAPTER_FACTORIES."""
    resolved = resolve_adapter_name(name)

    def factory(writes: bool) -> GibworkAdapter:
        return ADAPTER_FACTORIES[resolved](writes)

    return factory
