"""Real Gibwork adapter, speaking MCP over stdio to `gibwork mcp serve`.

The Gibwork CLI owns the signing key: it resolves the keypair from its own
profile configuration and signs inside its process. This adapter never
receives, reads, or forwards key material.

Prepared confirmations live inside the server process, so prepare and
submit must happen within one adapter instance. Inspection-only work runs
the server with `--read-only`; write tools are registered only when the
adapter is constructed with `writes=True`.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from humanfallback.models import (
    USDC_MINT,
    PrepareResult,
    RefundPrepareResult,
    RemoteSubmission,
    RemoteTask,
    SubmitResult,
    TaskContract,
    WalletStatus,
)

from . import mapping
from .errors import AdapterError, AmbiguousSubmit, ErrorCode
from .mcp_client import McpProtocolError, McpToolError, McpTransportError, StdioMcpClient

NAME = "gibwork"
MIN_FUNDING = Decimal("1.00")
MAX_FUNDING = Decimal("100000.00")
SUPPORTED_MINTS: frozenset[str] = frozenset({USDC_MINT})

TOOL_WALLET_STATUS = "gibwork_wallet_status"
TOOL_TASK_LIST = "gibwork_task_list"
TOOL_TASK_CREATE_PREPARE = "gibwork_task_create_prepare"
TOOL_TASK_CREATE_SUBMIT = "gibwork_task_create_submit"
TOOL_TASK_REFUND_PREPARE = "gibwork_task_refund_prepare"
TOOL_TASK_REFUND_SUBMIT = "gibwork_task_refund_submit"
TOOL_SUBMISSION_LIST = "gibwork_submission_list"
TOOL_SUBMISSION_GET = "gibwork_submission_get"


class McpSession(Protocol):
    """What the adapter needs from a transport; StdioMcpClient satisfies it."""

    def start(self) -> None: ...

    def close(self) -> None: ...

    def call_tool(self, name: str, arguments: dict[str, Any], *, money: bool = False) -> Any: ...


SessionFactory = Callable[[list[str]], McpSession]


def resolve_gibwork_command(explicit: str | None = None) -> list[str]:
    """Locate the Gibwork CLI entry point.

    Prefers running the CLI's bin.js directly under node so Windows .cmd
    shims are not in the stdio path. Falls back to the `gibwork` executable.
    """
    if explicit:
        path = Path(explicit)
        if path.suffix == ".js":
            node = shutil.which("node")
            if not node:
                raise AdapterError(ErrorCode.CONFIG_ERROR, "node is required to run the Gibwork CLI")
            return [node, str(path)]
        return [str(path)]

    exe = shutil.which("gibwork")
    if not exe:
        raise AdapterError(
            ErrorCode.CONFIG_ERROR,
            "gibwork CLI not found on PATH; install with `npm install --global @gibwork/cli`",
        )
    bin_js = Path(exe).resolve().parent / "node_modules" / "@gibwork" / "cli" / "dist" / "bin.js"
    node = shutil.which("node")
    if bin_js.exists() and node:
        return [node, str(bin_js)]
    return [exe]


def build_server_command(
    base: list[str],
    *,
    profile: str | None,
    environment: str | None,
    writes: bool,
) -> list[str]:
    cmd = list(base)
    if profile:
        cmd += ["--profile", profile]
    if environment:
        cmd += ["--environment", environment]
    cmd += ["mcp", "serve", "--allow-writes" if writes else "--read-only"]
    return cmd


class GibworkMcpAdapter:
    name = NAME

    def __init__(
        self,
        *,
        profile: str | None = None,
        environment: str | None = None,
        writes: bool = False,
        command: list[str] | None = None,
        timeout: float = 60.0,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self.profile = profile
        self.requested_environment = environment
        self.writes = writes
        self.timeout = timeout
        base = command if command is not None else resolve_gibwork_command()
        self.command = build_server_command(
            base, profile=profile, environment=environment, writes=writes
        )
        self._session_factory = session_factory or self._default_session
        self._session: McpSession | None = None
        self._status: WalletStatus | None = None
        # confirmation id -> identifiers recorded at prepare time
        self._prepared: dict[str, dict[str, str | None]] = {}
        self._consumed: set[str] = set()
        # Populated by the first wallet_status() call.
        self.environment = environment or ""
        self.wallet_address = ""

    def _default_session(self, command: list[str]) -> McpSession:
        return StdioMcpClient(command, timeout=self.timeout)

    # -- lifecycle ----------------------------------------------------------

    def connect(self) -> WalletStatus:
        """Start the server and resolve profile/environment/wallet."""
        if self._status is None:
            self._status = self.wallet_status()
        return self._status

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            finally:
                self._session = None

    def __enter__(self) -> GibworkMcpAdapter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- inspection ---------------------------------------------------------

    def wallet_status(self) -> WalletStatus:
        payload = self._call(TOOL_WALLET_STATUS, {})
        status = mapping.map_wallet_status(payload, adapter=self.name)
        self._status = status
        self.environment = status.environment
        self.wallet_address = status.wallet_address
        return status

    def list_tasks(self) -> list[RemoteTask]:
        payload = self._call(TOOL_TASK_LIST, {"pageAll": True})
        return [mapping.map_remote_task(item) for item in mapping.unwrap_list(payload)]

    def get_task(self, task_id: str) -> RemoteTask | None:
        for task in self.list_tasks():
            if task.id == task_id:
                return task
        return None

    def list_submissions(
        self, task_id: str, *, status: str | None = None
    ) -> list[RemoteSubmission]:
        args: dict[str, Any] = {"taskId": task_id, "pageAll": True}
        if status:
            args["status"] = status
        payload = self._call(TOOL_SUBMISSION_LIST, args)
        return [
            mapping.map_submission(item, task_id=task_id) for item in mapping.unwrap_list(payload)
        ]

    def get_submission(self, task_id: str, submission_id: str) -> RemoteSubmission | None:
        try:
            payload = self._call(
                TOOL_SUBMISSION_GET, {"taskId": task_id, "submissionId": submission_id}
            )
        except AdapterError as exc:
            if exc.code is ErrorCode.NOT_FOUND:
                return None
            raise
        if not isinstance(payload, dict) or not payload.get("id"):
            return None
        return mapping.map_submission(payload, task_id=task_id)

    # -- bounty creation ----------------------------------------------------

    def prepare_task(self, contract: TaskContract) -> PrepareResult:
        self._require_writes()
        reward = contract.reward
        if reward.mint_address not in SUPPORTED_MINTS:
            raise AdapterError(
                ErrorCode.UNSUPPORTED_MINT, f"mint {reward.mint_address} is not supported"
            )
        amount = reward.amount_decimal
        if not MIN_FUNDING <= amount <= MAX_FUNDING:
            raise AdapterError(
                ErrorCode.AMOUNT_OUT_OF_RANGE,
                f"payment.amount must be between {MIN_FUNDING} and {MAX_FUNDING} inclusive",
            )
        args: dict[str, Any] = {
            "title": contract.title,
            "content": contract.description,
            "tags": list(contract.tags),
            "payment": {"mintAddress": reward.mint_address, "amount": reward.amount},
            "minSubmissionAmount": reward.min_submission_amount or reward.amount,
            "deadline": contract.deadline.isoformat() if contract.deadline else None,
            "allowOnlyVerifiedSubmissions": False,
            "allowOnlyDiscordGuildSubmissions": False,
        }
        payload = self._call(TOOL_TASK_CREATE_PREPARE, args)
        result = mapping.map_prepare_result(payload)
        self._prepared[result.confirmation_id] = {
            "operation": "task_create",
            "task_id": result.task_id,
            "intent_id": result.intent_id,
        }
        return result

    def submit_task(self, confirmation_id: str) -> SubmitResult:
        return self._submit(TOOL_TASK_CREATE_SUBMIT, confirmation_id, operation="task_create")

    # -- refund -------------------------------------------------------------

    def prepare_refund(self, task_id: str) -> RefundPrepareResult:
        self._require_writes()
        payload = self._call(TOOL_TASK_REFUND_PREPARE, {"taskId": task_id})
        if not isinstance(payload, dict) or not payload.get("confirmationId"):
            raise AdapterError(ErrorCode.PROTOCOL_ERROR, "refund prepare returned no confirmation id")
        result = mapping.map_refund_prepare(payload, task_id=task_id)
        self._prepared[result.confirmation_id] = {
            "operation": "task_refund",
            "task_id": result.task_id,
            "intent_id": result.intent_id,
        }
        return result

    def submit_refund(self, confirmation_id: str) -> SubmitResult:
        return self._submit(TOOL_TASK_REFUND_SUBMIT, confirmation_id, operation="task_refund")

    # -- internals ----------------------------------------------------------

    def _submit(self, tool: str, confirmation_id: str, *, operation: str) -> SubmitResult:
        """Dispatch a money-moving submit exactly once for this confirmation."""
        self._require_writes()
        if confirmation_id in self._consumed:
            raise AdapterError(
                ErrorCode.CONFIRMATION_INVALID,
                "this confirmation was already submitted from this session; never resubmit",
            )
        record = self._prepared.get(confirmation_id)
        if record is None:
            raise AdapterError(
                ErrorCode.CONFIRMATION_INVALID,
                "confirmation was not prepared in this session; prepare again",
            )
        if record["operation"] != operation:
            raise AdapterError(
                ErrorCode.CONFIRMATION_INVALID, "confirmation belongs to a different operation"
            )
        self._consumed.add(confirmation_id)
        try:
            payload = self._call(tool, {"confirmationId": confirmation_id}, money=True)
        except AdapterError as exc:
            if exc.code is ErrorCode.AMBIGUOUS_SUBMIT:
                raise AmbiguousSubmit(
                    exc.message,
                    operation=operation,
                    confirmation_id=confirmation_id,
                    task_id=record["task_id"],
                    intent_id=record["intent_id"],
                    cause=exc.details.get("cause"),
                ) from exc
            raise
        return mapping.map_submit_result(
            payload if isinstance(payload, dict) else {},
            confirmation_id=confirmation_id,
            task_id=record["task_id"],
            intent_id=record["intent_id"],
        )

    def _require_writes(self) -> None:
        if not self.writes:
            raise AdapterError(
                ErrorCode.NOT_SUPPORTED,
                "adapter was opened read-only; write operations are not registered",
            )

    def _session_or_start(self) -> McpSession:
        if self._session is None:
            session = self._session_factory(self.command)
            try:
                session.start()
            except McpTransportError as exc:
                raise AdapterError(ErrorCode.NETWORK_ERROR, str(exc)) from exc
            except McpProtocolError as exc:
                raise AdapterError(ErrorCode.PROTOCOL_ERROR, str(exc)) from exc
            self._session = session
        return self._session

    def _call(self, tool: str, arguments: dict[str, Any], *, money: bool = False) -> Any:
        session = self._session_or_start()
        try:
            return session.call_tool(tool, arguments, money=money)
        except McpToolError as exc:
            raise mapping.translate_error(exc.payload) from exc
        except McpTransportError as exc:
            if money and exc.dispatched:
                raise AdapterError(
                    ErrorCode.AMBIGUOUS_SUBMIT,
                    f"{tool} was dispatched but no response was received; do not retry",
                    details={"cause": str(exc)},
                ) from exc
            raise AdapterError(ErrorCode.NETWORK_ERROR, str(exc)) from exc
        except McpProtocolError as exc:
            if money:
                # The server answered, so the request reached it; treat as unknown outcome.
                raise AdapterError(
                    ErrorCode.AMBIGUOUS_SUBMIT,
                    f"{tool} returned a protocol error after dispatch: {exc}",
                    details={"cause": str(exc)},
                ) from exc
            raise AdapterError(ErrorCode.PROTOCOL_ERROR, str(exc)) from exc
