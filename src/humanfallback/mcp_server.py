"""HumanFallback as an MCP server, for agents that need a human.

Exposes classification, contract creation and inspection, delegation
*preparation*, backend refresh/reconcile, and submission inspection.

It deliberately exposes no way to move money. There is no submit, refund,
or manual-resolve tool. A prepared quote is informational: the person who
approves a spend does so in a terminal (`hf delegate <id> --confirm`),
which re-prepares and shows a fresh quote. The adapter is pinned when the
server starts and cannot be changed per call.

Every tool returns structured content. Failures come back as `isError`
results carrying `{"error": {"code", "message", "details"}}`, the same
envelope the Gibwork MCP server uses.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any, get_type_hints

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field, TypeAdapter

from humanfallback import __version__
from humanfallback.adapters import AdapterError
from humanfallback.models import (
    ClassificationResult,
    ContractStatus,
    PaymentQuote,
    RemoteSubmission,
    TaskCategory,
    TaskContract,
    WalletStatus,
)
from humanfallback.service import ReconcileOutcome, Service, ServiceCode, ServiceError, error_envelope

SERVER_NAME = "humanfallback"
PREFIX = "humanfallback_"

INSTRUCTIONS = """HumanFallback turns work an agent cannot do itself into Task Contracts for people, delegated as Gibwork bounties.

Typical flow: humanfallback_classify -> humanfallback_contract_create -> humanfallback_delegate_prepare -> ask a person to fund it in a terminal -> humanfallback_contract_refresh / humanfallback_submission_list.

This server cannot move money. humanfallback_delegate_prepare only returns a quote; the confirmation id it shows is ephemeral and cannot be submitted through MCP or reused anywhere. To fund a bounty a person must run `hf delegate <contract_id> --confirm` in a terminal, which prepares a fresh quote and asks them to approve it. Never tell a person to reuse a confirmation id.

If a contract is `submit_uncertain`, a previous submit had an unknown outcome. Call humanfallback_contract_reconcile; do not attempt anything else on it until it is resolved."""

CONFIRMATION_NOTE = (
    "Ephemeral and informational only. This confirmation expires with this call and "
    "cannot be submitted through MCP. Do not pass it to any other tool or command. "
    "Funding requires a person to run the terminal command in human_action_required, "
    "which prepares a fresh quote for them to approve."
)


# -- views ----------------------------------------------------------------------


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(BaseModel):
    error: ErrorBody


class ClassificationView(BaseModel):
    classification: ClassificationResult
    recommended_action: str


class ContractView(BaseModel):
    contract: TaskContract
    locked: bool
    lock_reason: str | None = None
    allowed_actions: list[str]
    human_action_required: str | None = None
    backend: WalletStatus | None = None


class ContractSummary(BaseModel):
    id: str
    title: str
    status: ContractStatus
    category: TaskCategory
    reward_amount: str
    reward_symbol: str
    created_at: datetime
    locked: bool


class ContractListView(BaseModel):
    contracts: list[ContractSummary]
    total: int


class EphemeralConfirmation(BaseModel):
    id: str
    ephemeral: bool = True
    usable_for_submit: bool = False
    expires_at: datetime
    note: str = CONFIRMATION_NOTE


class PrepareView(BaseModel):
    contract_id: str
    status: str = "prepared"
    funds_moved: bool = False
    backend: WalletStatus
    quote: PaymentQuote
    task_id: str
    intent_id: str
    last_valid_block_height: int | None = None
    confirmation: EphemeralConfirmation
    human_action_required: str
    contract_status: ContractStatus


class ReconcileView(BaseModel):
    outcome: ReconcileOutcome
    message: str
    contract: ContractView
    backend: WalletStatus


class SubmissionListView(BaseModel):
    contract_id: str
    task_id: str
    submissions: list[RemoteSubmission]
    total: int
    backend: WalletStatus


class SubmissionView(BaseModel):
    contract_id: str
    submission: RemoteSubmission
    backend: WalletStatus


class StatusView(BaseModel):
    version: str
    adapter: str
    adapter_pinned: bool = True
    data_dir: str
    counts: dict[str, int]
    money_moving_tools_exposed: bool = False


# -- view builders --------------------------------------------------------------


def _human_delegate_command(contract: TaskContract, adapter: str) -> str:
    return f"hf delegate {contract.id} --adapter {adapter} --confirm"


def contract_view(contract: TaskContract, adapter: str, backend: WalletStatus | None = None) -> ContractView:
    status = contract.status
    locked = status is ContractStatus.SUBMIT_UNCERTAIN
    lock_reason = None
    human_action = None
    if locked:
        attempt = contract.uncertain_attempt
        op = attempt.operation if attempt else "submit"
        lock_reason = f"a {op} was dispatched with an unknown outcome; reconcile before any further submit"
        actions = ["contract_reconcile"]
        human_action = (
            f"Run `hf contract reconcile {contract.id} --adapter {adapter}`; if a person has verified "
            f"the operation never happened, run `hf contract resolve {contract.id} --outcome ...`"
        )
    elif status is ContractStatus.READY:
        actions = ["delegate_prepare"]
    elif status is ContractStatus.DRAFT:
        actions = []
        human_action = (
            "Contract is a draft (classified agent-capable). A person can promote it with the CLI "
            "or the agent should do the work itself."
        )
    elif status in (ContractStatus.DELEGATED, ContractStatus.SUBMITTED):
        actions = ["contract_refresh", "submission_list", "submission_get"]
    elif status in (ContractStatus.APPROVED, ContractStatus.REJECTED):
        actions = ["contract_refresh", "submission_list"]
    else:
        actions = []
    if status is ContractStatus.READY:
        human_action = f"To fund this bounty a person must run: {_human_delegate_command(contract, adapter)}"
    return ContractView(
        contract=contract,
        locked=locked,
        lock_reason=lock_reason,
        allowed_actions=[PREFIX + a for a in actions],
        human_action_required=human_action,
        backend=backend,
    )


def contract_summary(contract: TaskContract) -> ContractSummary:
    return ContractSummary(
        id=contract.id,
        title=contract.title,
        status=contract.status,
        category=contract.classification.category,
        reward_amount=contract.reward.amount,
        reward_symbol=contract.reward.symbol,
        created_at=contract.created_at,
        locked=contract.status is ContractStatus.SUBMIT_UNCERTAIN,
    )


# -- tool registration ----------------------------------------------------------


def _ok(model: BaseModel) -> CallToolResult:
    body = model.model_dump(mode="json")
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(body, indent=2))],
        structured_content=body,
    )


def _err(exc: ServiceError | AdapterError) -> CallToolResult:
    body = error_envelope(exc)
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(body, indent=2))],
        structured_content=body,
        is_error=True,
    )


def _register(
    server: MCPServer,
    *,
    name: str,
    output: type[BaseModel],
    read_only: bool,
    fn: Callable[..., BaseModel],
) -> None:
    """Register `fn` as a tool with a structured success/error contract."""

    def wrapper(**kwargs: Any) -> CallToolResult:
        try:
            return _ok(fn(**kwargs))
        except (ServiceError, AdapterError) as exc:
            return _err(exc)

    # Annotations are strings under `from __future__ import annotations`; the SDK
    # needs real types to build the argument model, so resolve them here.
    hints = get_type_hints(fn)
    params = [
        p.replace(annotation=hints.get(p.name, p.annotation))
        for p in inspect.signature(fn).parameters.values()
    ]
    wrapper.__signature__ = inspect.Signature(params, return_annotation=CallToolResult)  # type: ignore[attr-defined]
    wrapper.__annotations__ = {**{k: v for k, v in hints.items() if k != "return"}, "return": CallToolResult}
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    server.add_tool(
        wrapper,
        name=name,
        description=inspect.getdoc(fn),
        structured_output=False,
        annotations=ToolAnnotations(
            read_only_hint=read_only,
            destructive_hint=False,
            idempotent_hint=read_only,
            open_world_hint=False,
        ),
    )
    server._tool_manager.get_tool(name).output_schema = _output_schema(output)  # type: ignore[union-attr]


def _output_schema(output: type[BaseModel]) -> dict[str, Any]:
    """Success view or error envelope. MCP requires an object schema at the top level."""
    schema = TypeAdapter(output | ErrorEnvelope).json_schema()
    return {"type": "object", **schema}


def build_server(service: Service) -> MCPServer:
    server = MCPServer(name=SERVER_NAME, version=__version__, instructions=INSTRUCTIONS)
    adapter = service.adapter_name

    def classify(text: str) -> ClassificationView:
        """Decide whether a request needs a human, which category of human work it is, and why."""
        result = service.classify(text)
        if result.human_required:
            action = "Create a Task Contract with humanfallback_contract_create."
        else:
            action = "An agent can do this itself; do not delegate unless a person insists (force=true)."
        return ClassificationView(classification=result, recommended_action=action)

    def contract_create(
        text: str,
        reward: str = "1.00",
        min_submission_amount: str | None = None,
        title: str | None = None,
        tags: list[str] | None = None,
        deadline: datetime | None = None,
        force: bool = False,
    ) -> ContractView:
        """Classify a request and build a Task Contract with acceptance criteria and evidence requirements.

        `reward` and `min_submission_amount` are USDC amounts with exactly two decimals
        (minimum funding 1.00). Up to three `tags`. Saves the contract locally; moves no money.
        """
        contract = service.create_contract(
            text,
            reward=reward,
            min_submission_amount=min_submission_amount,
            title=title,
            tags=tags,
            deadline=deadline,
            force=force,
        )
        return contract_view(contract, adapter)

    def contract_get(contract_id: str) -> ContractView:
        """Fetch one Task Contract with its status, lock state, and the actions currently allowed."""
        return contract_view(service.get_contract(contract_id), adapter)

    def contract_list(
        status: ContractStatus | None = None,
        category: TaskCategory | None = None,
        limit: int = 25,
    ) -> ContractListView:
        """List saved Task Contracts, newest first, optionally filtered by status or category."""
        contracts = service.list_contracts(status=status, category=category, limit=max(1, min(limit, 200)))
        return ContractListView(contracts=[contract_summary(c) for c in contracts], total=len(contracts))

    def contract_refresh(contract_id: str) -> ContractView:
        """Read the delegated bounty's current state from the backend and record it on the contract."""
        contract, backend = service.refresh(contract_id)
        return contract_view(contract, adapter, backend)

    def contract_reconcile(contract_id: str) -> ReconcileView:
        """Resolve a submit_uncertain contract by checking the backend for the dispatched operation's result.

        Read-only. Clears the lock only when the backend confirms the outcome.
        """
        result = service.reconcile(contract_id)
        return ReconcileView(
            outcome=result.outcome,
            message=result.message,
            contract=contract_view(result.contract, adapter, result.backend),
            backend=result.backend,
        )

    def delegate_prepare(contract_id: str) -> PrepareView:
        """Get the exact funding quote for a ready contract. Moves no money.

        The returned confirmation is ephemeral and cannot be submitted through MCP.
        A person funds the bounty by running the command in `human_action_required`,
        which prepares a fresh quote and asks them to approve it.
        """
        with service.open_delegation(contract_id) as session:
            prepared = session.prepare()
            contract = session.contract
        assert hasattr(prepared, "payment_quote")
        return PrepareView(
            contract_id=contract.id,
            backend=session.backend,
            quote=prepared.payment_quote,
            task_id=prepared.task_id,
            intent_id=prepared.intent_id,
            last_valid_block_height=prepared.last_valid_block_height,
            confirmation=EphemeralConfirmation(id=prepared.confirmation_id, expires_at=prepared.expires_at),
            human_action_required=(
                f"A person must run in a terminal: {_human_delegate_command(contract, adapter)}"
            ),
            contract_status=contract.status,
        )

    def submission_list(contract_id: str, status: str | None = None) -> SubmissionListView:
        """List submissions on a delegated contract's bounty. `status` filters pending, approved, or rejected."""
        items, backend, task_id = service.list_submissions(contract_id, status=status)
        return SubmissionListView(
            contract_id=contract_id, task_id=task_id, submissions=items, total=len(items), backend=backend
        )

    def submission_get(contract_id: str, submission_id: str) -> SubmissionView:
        """Fetch one submission in full: content, submitter, media, status, comments."""
        item, backend = service.get_submission(contract_id, submission_id)
        if item is None:
            raise ServiceError(ServiceCode.NOT_FOUND, f"submission {submission_id} not found")
        return SubmissionView(contract_id=contract_id, submission=item, backend=backend)

    def wallet() -> WalletStatus:
        """Show the pinned backend's profile, environment, and public wallet address. Read-only."""
        return service.backend_info()

    def status() -> StatusView:
        """Show server version, pinned adapter, data directory, and contract counts. Never contacts the backend."""
        stats = service.stats()
        return StatusView(
            version=stats.version, adapter=stats.adapter, data_dir=stats.data_dir, counts=stats.counts
        )

    _register(server, name=PREFIX + "classify", output=ClassificationView, read_only=True, fn=classify)
    _register(server, name=PREFIX + "contract_create", output=ContractView, read_only=False, fn=contract_create)
    _register(server, name=PREFIX + "contract_get", output=ContractView, read_only=True, fn=contract_get)
    _register(server, name=PREFIX + "contract_list", output=ContractListView, read_only=True, fn=contract_list)
    _register(server, name=PREFIX + "contract_refresh", output=ContractView, read_only=False, fn=contract_refresh)
    _register(server, name=PREFIX + "contract_reconcile", output=ReconcileView, read_only=False, fn=contract_reconcile)
    _register(server, name=PREFIX + "delegate_prepare", output=PrepareView, read_only=False, fn=delegate_prepare)
    _register(server, name=PREFIX + "submission_list", output=SubmissionListView, read_only=True, fn=submission_list)
    _register(server, name=PREFIX + "submission_get", output=SubmissionView, read_only=True, fn=submission_get)
    _register(server, name=PREFIX + "wallet", output=WalletStatus, read_only=True, fn=wallet)
    _register(server, name=PREFIX + "status", output=StatusView, read_only=True, fn=status)
    return server


def serve(service: Service, *, log: Callable[[str], None] | None = None) -> None:
    """Resolve the backend, announce it on stderr, then serve over stdio."""
    say = log or (lambda _m: None)
    say(f"humanfallback mcp {__version__}: adapter={service.adapter_name} (pinned for this process)")
    if service.adapter_name != "mock":
        backend = service.backend_info()  # raises AdapterError if the CLI/profile is unusable
        say(
            f"backend: {backend.adapter}  profile={backend.profile}  environment={backend.environment}  "
            f"wallet={backend.wallet_address}"
        )
        say("note: this backend moves real funds; no submit tools are exposed over MCP.")
    build_server(service).run("stdio")


def claude_code_snippet(adapter: str, project_dir: str | None = None) -> str:
    from pathlib import Path

    root = project_dir or str(Path(__file__).resolve().parents[2])
    name = "humanfallback" if adapter == "mock" else f"humanfallback-{adapter}"
    env_flag = "" if adapter == "mock" else f" -e HF_ADAPTER={adapter}"
    mcp_json = {
        "mcpServers": {
            name: {
                "command": "uv",
                "args": ["--directory", root, "run", "hf", "mcp", "serve"],
                "env": {} if adapter == "mock" else {"HF_ADAPTER": adapter},
            }
        }
    }
    lines = [
        f"# Register with Claude Code (adapter: {adapter})",
        f'claude mcp add {name}{env_flag} -- uv --directory "{root}" run hf mcp serve',
        "",
        "# Or add to .mcp.json in your project:",
        json.dumps(mcp_json, indent=2),
    ]
    if adapter != "mock":
        lines += [
            "",
            "# The gibwork adapter needs the Gibwork CLI with a configured profile:",
            "#   npm install --global @gibwork/cli",
            "#   gibwork config set keypair-path /path/to/id.json   (never via HumanFallback)",
            "#   gibwork wallet doctor",
        ]
    return "\n".join(lines)
