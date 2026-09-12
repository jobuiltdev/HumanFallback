"""Operations shared by every front end (CLI, MCP server).

One implementation of each rule lives here: which statuses may be
delegated or refunded, how the SUBMIT_UNCERTAIN lock is applied and
cleared, how attempts are recorded, and how a money-moving submit is
dispatched exactly once. Front ends only present results and collect
human approval.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import ValidationError

from humanfallback import __version__, config
from humanfallback.adapters import AdapterError, AmbiguousSubmit, GibworkAdapter
from humanfallback.backends import AdapterFactory
from humanfallback.classifier import default_classifier
from humanfallback.contracts import AgentCapableRequest, build_contract
from humanfallback.models import (
    DELEGATABLE,
    REFUNDABLE,
    AttemptOutcome,
    ClassificationResult,
    ContractStatus,
    DelegationAttempt,
    DelegationRecord,
    MoneyOperation,
    PrepareResult,
    RefundPrepareResult,
    RefundRecord,
    RemoteSnapshot,
    RemoteSubmission,
    Reward,
    SubmissionComparison,
    SubmissionReview,
    SubmitResult,
    TaskCategory,
    TaskContract,
    WalletStatus,
)
from humanfallback.review import compare_reviews, review_submission
from humanfallback.store import ContractStore

StoreFactory = Callable[[], ContractStore]


class ServiceCode(StrEnum):
    NOT_FOUND = "NOT_FOUND"
    VALIDATION = "VALIDATION"
    AGENT_CAPABLE = "AGENT_CAPABLE"
    INVALID_STATE = "INVALID_STATE"
    CONTRACT_LOCKED = "CONTRACT_LOCKED"
    NOT_DELEGATED = "NOT_DELEGATED"
    ADAPTER_MISMATCH = "ADAPTER_MISMATCH"
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    NOT_RECONCILABLE = "NOT_RECONCILABLE"


class ServiceError(Exception):
    def __init__(self, code: ServiceCode, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def error_envelope(exc: ServiceError | AdapterError) -> dict[str, Any]:
    """The same shape the Gibwork MCP server uses, so agents handle both alike."""
    return {
        "error": {
            "code": str(exc.code),
            "message": exc.message,
            "details": dict(exc.details),
        }
    }


ReconcileOutcome = Literal["confirmed", "not_found"]


@dataclass
class ReconcileResult:
    outcome: ReconcileOutcome
    message: str
    contract: TaskContract
    backend: WalletStatus


@dataclass
class ContractStats:
    version: str
    adapter: str
    data_dir: str
    counts: dict[str, int]


def _now() -> datetime:
    return datetime.now(UTC)


class MoneySession:
    """An open adapter session for one money-moving operation.

    Lifecycle: open (backend resolved, contract validated) -> prepare (quote,
    attempt recorded, nothing moved) -> optionally submit (exactly once) ->
    close. Closing without submitting is the dry run; the prepared
    confirmation simply expires inside the backend.
    """

    def __init__(
        self,
        *,
        service: Service,
        operation: MoneyOperation,
        contract: TaskContract,
        adapter: GibworkAdapter,
        backend: WalletStatus,
    ) -> None:
        self.service = service
        self.operation = operation
        self.contract = contract
        self.adapter = adapter
        self.backend = backend
        self.prepared: PrepareResult | RefundPrepareResult | None = None
        self.attempt: DelegationAttempt | None = None
        self.result: SubmitResult | None = None
        self._closed = False

    # -- lifecycle --

    def __enter__(self) -> MoneySession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.adapter.close()

    # -- steps --

    def prepare(self) -> PrepareResult | RefundPrepareResult:
        """Obtain the exact quote. Records a PREPARED attempt. Moves nothing."""
        if self.prepared is not None:
            return self.prepared
        contract = self.contract
        if self.operation == "task_create":
            prepared = self.adapter.prepare_task(contract)
            attempt = DelegationAttempt(
                operation="task_create",
                adapter=self.adapter.name,
                environment=self.backend.environment,
                wallet_address=prepared.wallet_address,
                origin_status=contract.status,
                task_id=prepared.task_id,
                confirmation_id=prepared.confirmation_id,
                intent_id=prepared.intent_id,
                quote=prepared.payment_quote,
                prepared_at=prepared.created_at,
            )
        else:
            assert contract.delegation is not None
            prepared = self.adapter.prepare_refund(contract.delegation.task_id)
            attempt = DelegationAttempt(
                operation="task_refund",
                adapter=self.adapter.name,
                environment=self.backend.environment,
                wallet_address=prepared.wallet_address or self.backend.wallet_address,
                origin_status=contract.status,
                task_id=prepared.task_id,
                confirmation_id=prepared.confirmation_id,
                intent_id=prepared.intent_id,
                quote=contract.delegation.quote,
                prepared_at=prepared.created_at or _now(),
            )
        contract.attempts.append(attempt)
        self.service.save(contract)
        self.prepared = prepared
        self.attempt = attempt
        return prepared

    def submit(self) -> SubmitResult:
        """Dispatch the prepared operation exactly once and record the outcome.

        Callers must have obtained explicit human approval before calling.
        On an ambiguous outcome the contract is locked (SUBMIT_UNCERTAIN)
        before the exception propagates.
        """
        if self.prepared is None or self.attempt is None:
            raise ServiceError(ServiceCode.INVALID_STATE, "prepare before submit")
        if self.result is not None:
            raise ServiceError(ServiceCode.INVALID_STATE, "this session already submitted")
        contract, attempt = self.contract, self.attempt
        try:
            if self.operation == "task_create":
                result = self.adapter.submit_task(self.prepared.confirmation_id)
            else:
                result = self.adapter.submit_refund(self.prepared.confirmation_id)
        except AmbiguousSubmit as exc:
            attempt.outcome = AttemptOutcome.AMBIGUOUS
            attempt.error = str(exc)
            contract.transition_to(ContractStatus.SUBMIT_UNCERTAIN)
            self.service.save(contract)
            raise
        except AdapterError as exc:
            attempt.outcome = AttemptOutcome.FAILED
            attempt.error = str(exc)
            self.service.save(contract)
            raise

        attempt.outcome = AttemptOutcome.SUBMITTED
        attempt.resolved_at = result.submitted_at
        if self.operation == "task_create":
            assert isinstance(self.prepared, PrepareResult)
            contract.delegation = DelegationRecord(
                adapter=self.adapter.name,
                task_id=result.task_id,
                intent_id=result.intent_id,
                confirmation_id=result.confirmation_id,
                signature=result.signature,
                quote=self.prepared.payment_quote,
                delegated_at=result.submitted_at,
            )
            contract.transition_to(ContractStatus.DELEGATED)
        else:
            assert isinstance(self.prepared, RefundPrepareResult)
            contract.refund = RefundRecord(
                adapter=self.adapter.name,
                task_id=result.task_id,
                confirmation_id=result.confirmation_id,
                signature=result.signature,
                quote=self.prepared.raw,
                refunded_at=result.submitted_at,
            )
            contract.transition_to(ContractStatus.CLOSED)
        self.service.save(contract)
        self.result = result
        return result


class Service:
    def __init__(
        self,
        *,
        store_factory: StoreFactory,
        adapter_factory: AdapterFactory,
        adapter_name: str,
    ) -> None:
        self._store_factory = store_factory
        self._adapter_factory = adapter_factory
        self.adapter_name = adapter_name

    # -- persistence helpers --

    def save(self, contract: TaskContract) -> None:
        with self._store_factory() as store:
            store.save(contract)

    def get_contract(self, contract_id: str) -> TaskContract:
        with self._store_factory() as store:
            contract = store.get(contract_id)
        if contract is None:
            raise ServiceError(ServiceCode.NOT_FOUND, f"no contract with id {contract_id}")
        return contract

    def list_contracts(
        self,
        *,
        status: ContractStatus | None = None,
        category: TaskCategory | None = None,
        limit: int | None = None,
    ) -> list[TaskContract]:
        with self._store_factory() as store:
            contracts = store.list(status=status, category=category)
        return contracts[:limit] if limit else contracts

    def stats(self) -> ContractStats:
        with self._store_factory() as store:
            counts = store.count_by_status()
        return ContractStats(
            version=__version__,
            adapter=self.adapter_name,
            data_dir=str(config.data_dir()),
            counts={status.value: n for status, n in counts.items()},
        )

    # -- local operations --

    def classify(self, text: str) -> ClassificationResult:
        return default_classifier().classify(text)

    def create_contract(
        self,
        text: str,
        *,
        reward: str = "1.00",
        min_submission_amount: str | None = None,
        title: str | None = None,
        tags: list[str] | None = None,
        deadline: datetime | None = None,
        force: bool = False,
    ) -> TaskContract:
        classification = self.classify(text)
        try:
            reward_model = Reward(amount=reward, min_submission_amount=min_submission_amount)
            contract = build_contract(
                text,
                classification,
                reward_model,
                title=title,
                tags=tags,
                deadline=deadline,
                allow_agent_capable=force,
            )
        except AgentCapableRequest:
            raise ServiceError(
                ServiceCode.AGENT_CAPABLE,
                f"classified as agent-capable (confidence {classification.confidence}); "
                "pass force=True to build a contract anyway",
                details={"classification": classification.model_dump(mode="json")},
            ) from None
        except ValidationError as exc:
            raise ServiceError(ServiceCode.VALIDATION, _validation_message(exc)) from None
        self.save(contract)
        return contract

    def resolve_uncertain(self, contract_id: str, outcome: str) -> TaskContract:
        """Clear a SUBMIT_UNCERTAIN lock on a human's verified assertion."""
        if outcome not in ("not-created", "not-refunded"):
            raise ServiceError(ServiceCode.VALIDATION, "outcome must be not-created or not-refunded")
        contract = self.get_contract(contract_id)
        attempt = contract.uncertain_attempt
        if contract.status is not ContractStatus.SUBMIT_UNCERTAIN or attempt is None:
            raise ServiceError(ServiceCode.NOT_RECONCILABLE, "contract has no uncertain submit to resolve")
        expected = "task_create" if outcome == "not-created" else "task_refund"
        if attempt.operation != expected:
            raise ServiceError(
                ServiceCode.VALIDATION,
                f"uncertain attempt is a {attempt.operation}; outcome {outcome} does not apply",
            )
        attempt.outcome = AttemptOutcome.RECONCILED_NOT_FOUND
        attempt.resolved_at = _now()
        contract.transition_to(attempt.origin_status)
        self.save(contract)
        return contract

    # -- backend reads --

    def _open_adapter(self, *, writes: bool) -> tuple[GibworkAdapter, WalletStatus]:
        adapter = self._adapter_factory(writes)
        try:
            backend = adapter.wallet_status()
        except AdapterError:
            adapter.close()
            raise
        return adapter, backend

    def backend_info(self) -> WalletStatus:
        adapter, backend = self._open_adapter(writes=False)
        adapter.close()
        return backend

    def refresh(self, contract_id: str) -> tuple[TaskContract, WalletStatus]:
        contract = self.get_contract(contract_id)
        if contract.delegation is None:
            raise ServiceError(ServiceCode.NOT_DELEGATED, "contract has not been delegated; nothing to refresh")
        adapter, backend = self._open_adapter(writes=False)
        try:
            task = adapter.get_task(contract.delegation.task_id)
        finally:
            adapter.close()
        if task is None:
            raise ServiceError(
                ServiceCode.TASK_NOT_FOUND,
                f"task {contract.delegation.task_id} not found on {adapter.name}",
            )
        now = _now()
        contract.remote = RemoteSnapshot(
            status=task.status,
            is_open=task.is_open,
            total_submissions=task.total_submissions,
            synced_at=now,
        )
        if contract.status is ContractStatus.DELEGATED and task.total_submissions:
            contract.transition_to(ContractStatus.SUBMITTED)
        contract.updated_at = now
        self.save(contract)
        return contract, backend

    def reconcile(self, contract_id: str) -> ReconcileResult:
        contract = self.get_contract(contract_id)
        attempt = contract.uncertain_attempt
        if contract.status is not ContractStatus.SUBMIT_UNCERTAIN or attempt is None:
            raise ServiceError(ServiceCode.NOT_RECONCILABLE, "contract has no uncertain submit to reconcile")
        if attempt.adapter != self.adapter_name:
            raise ServiceError(
                ServiceCode.ADAPTER_MISMATCH,
                f"attempt was made with adapter {attempt.adapter!r}; "
                f"reconcile with the same adapter, not {self.adapter_name!r}",
            )
        adapter, backend = self._open_adapter(writes=False)
        try:
            task = adapter.get_task(attempt.task_id)
        finally:
            adapter.close()

        now = _now()
        if attempt.operation == "task_create":
            if task is None:
                return ReconcileResult(
                    "not_found",
                    f"task {attempt.task_id} was not found on {adapter.name}. The bounty may still "
                    "settle; check again later. If a person has verified it never funded, run "
                    f"`hf contract resolve {contract.id} --outcome not-created`.",
                    contract,
                    backend,
                )
            attempt.outcome = AttemptOutcome.RECONCILED_CONFIRMED
            attempt.resolved_at = now
            contract.delegation = DelegationRecord(
                adapter=attempt.adapter,
                task_id=attempt.task_id,
                intent_id=attempt.intent_id,
                confirmation_id=attempt.confirmation_id,
                signature="reconciled",
                quote=attempt.quote,  # type: ignore[arg-type]
                delegated_at=now,
            )
            contract.transition_to(ContractStatus.DELEGATED)
            self.save(contract)
            return ReconcileResult(
                "confirmed",
                f"task {attempt.task_id} exists on {adapter.name}; contract is delegated",
                contract,
                backend,
            )

        if task is None or task.is_open:
            state = "still open" if task is not None else "not found"
            return ReconcileResult(
                "not_found",
                f"task {attempt.task_id} is {state} on {adapter.name}. The refund may still "
                "settle; check again later. If a person has verified it never dispatched, run "
                f"`hf contract resolve {contract.id} --outcome not-refunded`.",
                contract,
                backend,
            )
        attempt.outcome = AttemptOutcome.RECONCILED_CONFIRMED
        attempt.resolved_at = now
        contract.refund = RefundRecord(
            adapter=attempt.adapter,
            task_id=attempt.task_id,
            confirmation_id=attempt.confirmation_id,
            signature="reconciled",
            quote=attempt.quote.model_dump(mode="json") if attempt.quote else {},
            refunded_at=now,
        )
        contract.transition_to(ContractStatus.CLOSED)
        self.save(contract)
        return ReconcileResult(
            "confirmed",
            f"task {attempt.task_id} is closed on {adapter.name}; contract is closed",
            contract,
            backend,
        )

    def list_submissions(
        self, contract_id: str, *, status: str | None = None
    ) -> tuple[list[RemoteSubmission], WalletStatus, str]:
        contract = self.get_contract(contract_id)
        if contract.delegation is None:
            raise ServiceError(ServiceCode.NOT_DELEGATED, "contract has not been delegated")
        adapter, backend = self._open_adapter(writes=False)
        try:
            items = adapter.list_submissions(contract.delegation.task_id, status=status)
        finally:
            adapter.close()
        return items, backend, contract.delegation.task_id

    def get_submission(
        self, contract_id: str, submission_id: str
    ) -> tuple[RemoteSubmission | None, WalletStatus]:
        contract = self.get_contract(contract_id)
        if contract.delegation is None:
            raise ServiceError(ServiceCode.NOT_DELEGATED, "contract has not been delegated")
        adapter, backend = self._open_adapter(writes=False)
        try:
            item = adapter.get_submission(contract.delegation.task_id, submission_id)
        finally:
            adapter.close()
        return item, backend

    # -- advisory review (read-only; never approves, rejects, or pays) --

    def _delegated_contract(self, contract_id: str) -> TaskContract:
        contract = self.get_contract(contract_id)
        if contract.delegation is None:
            raise ServiceError(ServiceCode.NOT_DELEGATED, "contract has not been delegated")
        return contract

    def review_submission(self, contract_id: str, submission_id: str) -> tuple[SubmissionReview, WalletStatus]:
        contract = self._delegated_contract(contract_id)
        assert contract.delegation is not None
        adapter, backend = self._open_adapter(writes=False)
        try:
            item = adapter.get_submission(contract.delegation.task_id, submission_id)
        finally:
            adapter.close()
        if item is None:
            raise ServiceError(ServiceCode.NOT_FOUND, f"submission {submission_id} not found")
        return review_submission(contract, item), backend

    def review_all(
        self, contract_id: str, *, status: str | None = None
    ) -> tuple[list[SubmissionReview], list[RemoteSubmission], WalletStatus]:
        contract = self._delegated_contract(contract_id)
        assert contract.delegation is not None
        adapter, backend = self._open_adapter(writes=False)
        try:
            items = adapter.list_submissions(contract.delegation.task_id, status=status)
        finally:
            adapter.close()
        reviews = [review_submission(contract, item) for item in items]
        return reviews, items, backend

    def compare_submissions(
        self, contract_id: str, *, status: str | None = None
    ) -> tuple[SubmissionComparison, WalletStatus]:
        reviews, items, backend = self.review_all(contract_id, status=status)
        return compare_reviews(contract_id, reviews, items), backend

    # -- money-moving operations --

    def open_delegation(self, contract_id: str) -> MoneySession:
        """Validate and open a session for funding a bounty. Nothing is prepared yet."""
        contract = self.get_contract(contract_id)
        self._refuse_if_locked(contract)
        if contract.status not in DELEGATABLE:
            raise ServiceError(
                ServiceCode.INVALID_STATE,
                f"contract is {contract.status.value}; only ready contracts can be delegated",
            )
        adapter, backend = self._open_adapter(writes=True)
        return MoneySession(
            service=self, operation="task_create", contract=contract, adapter=adapter, backend=backend
        )

    def open_refund(self, contract_id: str) -> MoneySession:
        contract = self.get_contract(contract_id)
        self._refuse_if_locked(contract)
        if contract.status not in REFUNDABLE or contract.delegation is None:
            raise ServiceError(
                ServiceCode.INVALID_STATE,
                f"contract is {contract.status.value}; only delegated bounties can be refunded",
            )
        if contract.delegation.adapter != self.adapter_name:
            raise ServiceError(
                ServiceCode.ADAPTER_MISMATCH,
                f"bounty was created with adapter {contract.delegation.adapter!r}; "
                f"refund with the same adapter, not {self.adapter_name!r}",
            )
        adapter, backend = self._open_adapter(writes=True)
        return MoneySession(
            service=self, operation="task_refund", contract=contract, adapter=adapter, backend=backend
        )

    @staticmethod
    def _refuse_if_locked(contract: TaskContract) -> None:
        if contract.status is ContractStatus.SUBMIT_UNCERTAIN:
            attempt = contract.uncertain_attempt
            raise ServiceError(
                ServiceCode.CONTRACT_LOCKED,
                "a previous submit has an unknown outcome; reconcile "
                f"(`hf contract reconcile {contract.id}`) before any further submit",
                details={
                    "operation": attempt.operation if attempt else None,
                    "task_id": attempt.task_id if attempt else None,
                    "confirmation_id": attempt.confirmation_id if attempt else None,
                },
            )


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "input"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "; ".join(parts)


def build_service(adapter_name: str) -> Service:
    """Service bound to one backend name and the configured data directory."""
    from humanfallback.backends import adapter_factory_for, resolve_adapter_name

    resolved = resolve_adapter_name(adapter_name)
    return Service(
        store_factory=lambda: ContractStore(config.db_path()),
        adapter_factory=adapter_factory_for(resolved),
        adapter_name=resolved,
    )
