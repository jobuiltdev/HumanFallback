"""Adapter boundary between contracts and a bounty backend.

Bounty creation is a two-step prepare/submit flow: prepare returns a quote
and a one-time confirmation id, submit consumes that id exactly once.
"""

from __future__ import annotations

from typing import Protocol

from humanfallback.models import PrepareResult, RemoteTask, SubmitResult, TaskContract


class AdapterError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class GibworkAdapter(Protocol):
    name: str
    environment: str
    wallet_address: str

    def prepare_task(self, contract: TaskContract) -> PrepareResult: ...

    def submit_task(self, confirmation_id: str) -> SubmitResult: ...

    def list_tasks(self) -> list[RemoteTask]: ...

    def get_task(self, task_id: str) -> RemoteTask | None: ...
