"""Adapter boundary between contracts and a bounty backend.

Money-moving operations are two-step: prepare returns a quote and a
one-time confirmation id; submit consumes that id exactly once. A submit
is never retried by the adapter. If its outcome is unknown the adapter
raises AmbiguousSubmit and the caller must reconcile.
"""

from __future__ import annotations

from typing import Protocol

from humanfallback.models import (
    PrepareResult,
    RefundPrepareResult,
    RemoteSubmission,
    RemoteTask,
    SubmitResult,
    TaskContract,
    WalletStatus,
)

from .errors import AdapterError, AmbiguousSubmit, ErrorCode

__all__ = ["AdapterError", "AmbiguousSubmit", "ErrorCode", "GibworkAdapter"]


class GibworkAdapter(Protocol):
    name: str
    environment: str
    wallet_address: str

    # -- inspection ---------------------------------------------------------

    def wallet_status(self) -> WalletStatus: ...

    def list_tasks(self) -> list[RemoteTask]: ...

    def get_task(self, task_id: str) -> RemoteTask | None: ...

    def list_submissions(
        self, task_id: str, *, status: str | None = None
    ) -> list[RemoteSubmission]: ...

    def get_submission(self, task_id: str, submission_id: str) -> RemoteSubmission | None: ...

    # -- bounty creation ----------------------------------------------------

    def prepare_task(self, contract: TaskContract) -> PrepareResult: ...

    def submit_task(self, confirmation_id: str) -> SubmitResult: ...

    # -- refund -------------------------------------------------------------

    def prepare_refund(self, task_id: str) -> RefundPrepareResult: ...

    def submit_refund(self, confirmation_id: str) -> SubmitResult: ...

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None: ...
