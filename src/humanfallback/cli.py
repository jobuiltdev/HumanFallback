"""Command-line interface.

    hf classify "<request>"
    hf contract create "<request>" [--reward 1.00] [--title ...] [--tag ...]
    hf contract show <id> | list | refresh <id> | reconcile <id> | resolve <id> --outcome ...
    hf delegate <id> [--adapter mock|gibwork] [--confirm [--yes]]
    hf refund <id>   [--adapter mock|gibwork] [--confirm [--yes]]
    hf submissions <id> [--status pending|approved|rejected]
    hf submission <id> <submission-id>
    hf wallet [--adapter ...]
    hf status
    hf mcp serve [--adapter ...] | hf mcp snippet

Money moves only on `--confirm`, and only after the quote is printed and a
person approves it. `--yes` skips the interactive prompt but is refused
unless `--confirm` is also present. All rules live in `service.py`; this
module only presents results and collects approval.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Annotated

import typer
from pydantic import BaseModel

from humanfallback import __version__, config
from humanfallback.adapters import AdapterError, AmbiguousSubmit, GibworkAdapter, MockGibworkAdapter
from humanfallback.backends import ADAPTER_FACTORIES  # noqa: F401  (patched by tests)
from humanfallback.models import (
    ContractStatus,
    PrepareResult,
    RemoteSubmission,
    TaskCategory,
    TaskContract,
    WalletStatus,
)
from humanfallback.service import MoneySession, Service, ServiceError, build_service

EXIT_USAGE = 2
EXIT_AMBIGUOUS = 22

app = typer.Typer(
    help="Detect human-required tasks and turn them into Task Contracts.",
    no_args_is_help=True,
)
contract_app = typer.Typer(help="Create and inspect Task Contracts.", no_args_is_help=True)
mcp_app = typer.Typer(help="Expose HumanFallback to MCP-capable agents.", no_args_is_help=True)
app.add_typer(contract_app, name="contract")
app.add_typer(mcp_app, name="mcp")

JsonFlag = Annotated[bool, typer.Option("--json", help="Emit JSON instead of text.")]
AdapterOpt = Annotated[
    str | None,
    typer.Option("--adapter", help="Backend: mock or gibwork. Defaults to HF_ADAPTER (mock)."),
]
ConfirmFlag = Annotated[
    bool, typer.Option("--confirm", help="Submit the prepared transaction after approval.")
]
YesFlag = Annotated[
    bool,
    typer.Option(
        "--yes", help="Skip the interactive approval prompt. Only valid together with --confirm."
    ),
]

Failure = (ServiceError, AdapterError)


# -- helpers ----------------------------------------------------------------------


def _service(adapter: str | None) -> Service:
    try:
        return build_service(adapter or config.adapter_name())
    except AdapterError as exc:
        _fail(exc.message)
    raise AssertionError  # unreachable


def _emit(model: BaseModel) -> None:
    typer.echo(model.model_dump_json(indent=2))


def _note(message: str) -> None:
    typer.secho(message, err=True, fg=typer.colors.YELLOW)


def _fail(message: str, code: int = 1) -> None:
    typer.secho(f"error: {message}", err=True, fg=typer.colors.RED)
    raise typer.Exit(code)


def _announce(backend: WalletStatus) -> None:
    """Print the resolved backend identity before anything else happens."""
    _note(
        f"backend: {backend.adapter}  profile={backend.profile}  "
        f"environment={backend.environment}  wallet={backend.wallet_address}  "
        f"writes={'enabled' if backend.writes_enabled else 'disabled'}"
    )
    if backend.adapter != "mock":
        _note("note: this backend moves real funds; stage uses mainnet USDC.")


def _print_contract(contract: TaskContract) -> None:
    c = contract.classification
    typer.echo(f"id:        {contract.id}")
    typer.echo(f"title:     {contract.title}")
    typer.echo(f"status:    {contract.status.value}")
    typer.echo(
        f"category:  {c.category.value}  (human_required={c.human_required}, "
        f"confidence={c.confidence})"
    )
    typer.echo(
        f"reward:    {contract.reward.amount} {contract.reward.symbol} "
        f"(min per submission {contract.reward.min_submission_amount})"
    )
    typer.echo(f"tags:      {', '.join(contract.tags) or '-'}")
    typer.echo(f"deadline:  {contract.deadline.isoformat() if contract.deadline else '-'}")
    typer.echo("acceptance criteria:")
    for ac in contract.acceptance_criteria:
        ev = ", ".join(ac.evidence_ids) or "-"
        typer.echo(f"  {ac.id}  [{ac.check_type.value}]  {ac.statement}  <- {ev}")
    typer.echo("evidence requirements:")
    for ev_req in contract.evidence_requirements:
        typer.echo(f"  {ev_req.id}  [{ev_req.kind.value}]  {ev_req.description}")
    if contract.delegation:
        d = contract.delegation
        typer.echo(
            f"delegated: adapter={d.adapter} task_id={d.task_id} at {d.delegated_at.isoformat()}"
        )
    if contract.remote:
        r = contract.remote
        subs = r.total_submissions if r.total_submissions is not None else "?"
        typer.echo(
            f"remote:    status={r.status} open={r.is_open} submissions={subs} "
            f"synced={r.synced_at.isoformat()}"
        )
    if contract.refund:
        typer.echo(
            f"refunded:  task_id={contract.refund.task_id} at {contract.refund.refunded_at.isoformat()}"
        )
    uncertain = contract.uncertain_attempt
    if uncertain:
        typer.echo(
            f"UNCERTAIN: {uncertain.operation} dispatched at {uncertain.prepared_at.isoformat()} "
            f"task_id={uncertain.task_id} confirmation={uncertain.confirmation_id}; "
            "run `hf contract reconcile` before any further submit"
        )


def _print_quote(prepared: PrepareResult, backend: GibworkAdapter, *, err: bool = False) -> None:
    """Show the exact quote. Goes to stderr under --json so stdout stays parseable."""
    q = prepared.payment_quote
    lines = [
        f"adapter:           {backend.name} ({backend.environment})",
        f"wallet:            {prepared.wallet_address}",
        f"confirmation_id:   {prepared.confirmation_id}",
        f"intent_id:         {prepared.intent_id}",
        f"task_id:           {prepared.task_id}",
        f"token:             {q.token.symbol} {q.token.mint_address}",
        f"funding_amount:    {q.funding_amount}",
        f"platform_fee:      {q.platform_fee.amount} ({q.platform_fee.percent}%)",
        f"total_debit:       {q.total_debit}",
        f"expires_at:        {prepared.expires_at.isoformat()}",
    ]
    if prepared.last_valid_block_height is not None:
        lines.append(f"last_valid_block:  {prepared.last_valid_block_height}")
    for line in lines:
        typer.echo(line, err=err)


def _print_submission(item: RemoteSubmission) -> None:
    typer.echo(f"id:         {item.id}")
    typer.echo(f"task_id:    {item.task_id}")
    typer.echo(f"status:     {item.status}")
    typer.echo(f"submitter:  {item.submitter or '-'}")
    typer.echo(f"created_at: {item.created_at.isoformat() if item.created_at else '-'}")
    typer.echo(f"rating:     {item.rating if item.rating is not None else '-'}")
    typer.echo("media:")
    for m in item.media or ["-"]:
        typer.echo(f"  {m}")
    typer.echo("content:")
    typer.echo(item.content)


# -- approval gate --------------------------------------------------------------


def _stdin_is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _check_flags(confirm: bool, yes: bool) -> None:
    if yes and not confirm:
        _fail("--yes only skips the prompt; it requires --confirm to move funds", EXIT_USAGE)


def _approved(prompt: str, *, confirm: bool, yes: bool) -> bool:
    """True only when a person (or an explicit --confirm --yes) approved the spend."""
    if not confirm:
        return False
    if yes:
        _note("approval: --confirm --yes supplied; skipping interactive prompt")
        return True
    if not _stdin_is_tty():
        _fail(
            "interactive approval required: re-run in a terminal, or pass --confirm --yes "
            "to approve non-interactively"
        )
    return typer.confirm(prompt, default=False)


def _submit_or_fail(session: MoneySession) -> None:
    try:
        session.submit()
    except AmbiguousSubmit as exc:
        _fail(
            f"{exc} — contract is now {ContractStatus.SUBMIT_UNCERTAIN.value}; "
            f"run `hf contract reconcile {session.contract.id}`. Do not resubmit.",
            EXIT_AMBIGUOUS,
        )
    except AdapterError as exc:
        _fail(str(exc))


# -- commands -------------------------------------------------------------------


def _version(value: bool) -> None:
    if value:
        typer.echo(f"humanfallback {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Show version and exit."),
    ] = False,
) -> None:
    """Detect human-required tasks and turn them into Task Contracts."""


@app.command()
def classify(
    text: Annotated[str, typer.Argument(help="The request to classify.")],
    as_json: JsonFlag = False,
) -> None:
    """Decide whether a request needs a human, and why."""
    result = _service(None).classify(text)
    if as_json:
        _emit(result)
        return
    typer.echo(f"human_required: {result.human_required}")
    typer.echo(f"category:       {result.category.value}")
    typer.echo(f"confidence:     {result.confidence}")
    typer.echo("reasons:")
    for reason in result.reasons:
        typer.echo(f"  - {reason}")


@contract_app.command("create")
def contract_create(
    text: Annotated[str, typer.Argument(help="The request to convert.")],
    reward: Annotated[str, typer.Option(help="Total funding, two decimals.")] = "1.00",
    min_submission: Annotated[
        str | None, typer.Option(help="Minimum payout per submission; defaults to reward.")
    ] = None,
    title: Annotated[str | None, typer.Option(help="Override the derived title.")] = None,
    tag: Annotated[list[str] | None, typer.Option(help="Discovery tag (max 3).")] = None,
    deadline: Annotated[
        datetime | None,
        typer.Option(help="ISO-8601 deadline.", formats=["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"]),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Build a contract even if classified agent-capable.")
    ] = False,
    as_json: JsonFlag = False,
) -> None:
    """Classify a request, build a Task Contract, and save it."""
    try:
        contract = _service(None).create_contract(
            text,
            reward=reward,
            min_submission_amount=min_submission,
            title=title,
            tags=tag,
            deadline=deadline,
            force=force,
        )
    except ServiceError as exc:
        if exc.code == "AGENT_CAPABLE":
            _fail(exc.message.replace("pass force=True", "use --force"))
        _fail(exc.message)
    if as_json:
        _emit(contract)
    else:
        _print_contract(contract)


@contract_app.command("show")
def contract_show(contract_id: str, as_json: JsonFlag = False) -> None:
    """Show one contract."""
    try:
        contract = _service(None).get_contract(contract_id)
    except ServiceError as exc:
        _fail(exc.message)
    if as_json:
        _emit(contract)
    else:
        _print_contract(contract)


@contract_app.command("list")
def contract_list(
    status: Annotated[ContractStatus | None, typer.Option(help="Filter by status.")] = None,
    category: Annotated[TaskCategory | None, typer.Option(help="Filter by category.")] = None,
    as_json: JsonFlag = False,
) -> None:
    """List saved contracts, newest first."""
    contracts = _service(None).list_contracts(status=status, category=category)
    if as_json:
        typer.echo(json.dumps([c.model_dump(mode="json") for c in contracts], indent=2))
        return
    if not contracts:
        typer.echo("no contracts")
        return
    for c in contracts:
        typer.echo(
            f"{c.id}  {c.status.value:<16}  {c.classification.category.value:<24}  "
            f"{c.reward.amount} {c.reward.symbol}  {c.title}"
        )


@contract_app.command("refresh")
def contract_refresh(
    contract_id: str, adapter: AdapterOpt = None, as_json: JsonFlag = False
) -> None:
    """Fetch the bounty's current state from the backend and record it."""
    try:
        contract, backend = _service(adapter).refresh(contract_id)
    except Failure as exc:
        _fail(str(exc) if isinstance(exc, AdapterError) else exc.message)
    _announce(backend)
    if as_json:
        _emit(contract)
    else:
        _print_contract(contract)


@contract_app.command("reconcile")
def contract_reconcile(
    contract_id: str, adapter: AdapterOpt = None, as_json: JsonFlag = False
) -> None:
    """Resolve an uncertain submit by checking the backend for its result."""
    try:
        result = _service(adapter).reconcile(contract_id)
    except Failure as exc:
        _fail(str(exc) if isinstance(exc, AdapterError) else exc.message)
    _announce(result.backend)
    if result.outcome == "confirmed":
        typer.echo(f"reconciled: {result.message}")
        if as_json:
            _emit(result.contract)
        return
    typer.echo(result.message)
    raise typer.Exit(1)


@contract_app.command("resolve")
def contract_resolve(
    contract_id: str,
    outcome: Annotated[
        str,
        typer.Option(
            "--outcome",
            help="Your verified conclusion: not-created (bounty never funded) or "
            "not-refunded (refund never dispatched).",
        ),
    ],
    as_json: JsonFlag = False,
) -> None:
    """Manually clear an uncertain submit after verifying its outcome yourself."""
    if outcome not in ("not-created", "not-refunded"):
        _fail("--outcome must be not-created or not-refunded", EXIT_USAGE)
    try:
        contract = _service(None).resolve_uncertain(contract_id, outcome)
    except ServiceError as exc:
        _fail(exc.message)
    typer.echo(f"resolved: contract returned to {contract.status.value}")
    if as_json:
        _emit(contract)


@app.command()
def delegate(
    contract_id: str,
    adapter: AdapterOpt = None,
    confirm: ConfirmFlag = False,
    yes: YesFlag = False,
    as_json: JsonFlag = False,
) -> None:
    """Prepare a bounty quote for a READY contract; fund it only with --confirm."""
    _check_flags(confirm, yes)
    try:
        session = _service(adapter).open_delegation(contract_id)
    except Failure as exc:
        _fail(str(exc) if isinstance(exc, AdapterError) else exc.message)

    with session:
        _announce(session.backend)
        if not confirm:
            _note("dry run: prepare only; nothing will be submitted")
        try:
            prepared = session.prepare()
        except AdapterError as exc:
            _fail(str(exc))
        assert isinstance(prepared, PrepareResult)

        if not confirm:
            if as_json:
                _emit(prepared)
            else:
                _print_quote(prepared, session.adapter)
                typer.echo("dry run: nothing submitted. Re-run with --confirm to fund.")
            return

        _print_quote(prepared, session.adapter, err=as_json)
        q = prepared.payment_quote
        if not _approved(
            f"Fund {q.total_debit} {q.token.symbol} from wallet {prepared.wallet_address} "
            f"on {session.backend.adapter}/{session.backend.environment}?",
            confirm=confirm,
            yes=yes,
        ):
            typer.echo("aborted: nothing submitted")
            raise typer.Exit(1)
        _submit_or_fail(session)

    contract = session.contract
    if as_json:
        _emit(contract)
    else:
        assert contract.delegation is not None
        typer.echo(f"delegated {contract.id} via {session.adapter.name}: task_id={contract.delegation.task_id}")
        typer.echo(f"debited {q.total_debit} {q.token.symbol}")


@app.command()
def refund(
    contract_id: str,
    adapter: AdapterOpt = None,
    confirm: ConfirmFlag = False,
    yes: YesFlag = False,
    as_json: JsonFlag = False,
) -> None:
    """Prepare a refund quote for a delegated bounty; execute it only with --confirm."""
    _check_flags(confirm, yes)
    try:
        session = _service(adapter).open_refund(contract_id)
    except Failure as exc:
        _fail(str(exc) if isinstance(exc, AdapterError) else exc.message)

    with session:
        _announce(session.backend)
        if not confirm:
            _note("dry run: prepare only; nothing will be submitted")
        try:
            prepared = session.prepare()
        except AdapterError as exc:
            _fail(str(exc))
        assert not isinstance(prepared, PrepareResult)

        contract = session.contract
        wallet_address = prepared.wallet_address or session.backend.wallet_address
        amount = prepared.refund_amount or "?"
        symbol = prepared.token.symbol if prepared.token else contract.reward.symbol
        err = as_json
        typer.echo(f"adapter:           {session.adapter.name} ({session.backend.environment})", err=err)
        typer.echo(f"wallet:            {wallet_address}", err=err)
        typer.echo(f"confirmation_id:   {prepared.confirmation_id}", err=err)
        typer.echo(f"task_id:           {prepared.task_id}", err=err)
        typer.echo(f"refund_amount:     {amount} {symbol}", err=err)
        if prepared.expires_at:
            typer.echo(f"expires_at:        {prepared.expires_at.isoformat()}", err=err)
        typer.echo("backend payload:", err=err)
        typer.echo(json.dumps(prepared.raw, indent=2, default=str), err=err)
        if not confirm:
            if as_json:
                _emit(prepared)
            else:
                typer.echo("dry run: nothing submitted. Re-run with --confirm to refund.")
            return

        if not _approved(
            f"Refund task {prepared.task_id} ({amount} {symbol}) to wallet {wallet_address} "
            f"on {session.backend.adapter}/{session.backend.environment}?",
            confirm=confirm,
            yes=yes,
        ):
            typer.echo("aborted: nothing submitted")
            raise typer.Exit(1)
        _submit_or_fail(session)

    contract = session.contract
    if as_json:
        _emit(contract)
    else:
        assert contract.refund is not None
        typer.echo(f"refunded {contract.id} via {session.adapter.name}: task_id={contract.refund.task_id}")


@app.command()
def submissions(
    contract_id: str,
    status: Annotated[
        str | None, typer.Option(help="Filter: pending, approved, or rejected.")
    ] = None,
    adapter: AdapterOpt = None,
    as_json: JsonFlag = False,
) -> None:
    """List submissions on a delegated contract's bounty."""
    try:
        items, backend, _ = _service(adapter).list_submissions(contract_id, status=status)
    except Failure as exc:
        _fail(str(exc) if isinstance(exc, AdapterError) else exc.message)
    _announce(backend)
    if as_json:
        typer.echo(json.dumps([s.model_dump(mode="json") for s in items], indent=2))
        return
    if not items:
        typer.echo("no submissions")
        return
    for s in items:
        preview = s.content.replace("\n", " ")[:80]
        typer.echo(f"{s.id}  {s.status:<9}  {s.submitter or '-':<20}  {preview}")


@app.command()
def submission(
    contract_id: str,
    submission_id: str,
    adapter: AdapterOpt = None,
    as_json: JsonFlag = False,
) -> None:
    """Show one submission in full."""
    try:
        item, backend = _service(adapter).get_submission(contract_id, submission_id)
    except Failure as exc:
        _fail(str(exc) if isinstance(exc, AdapterError) else exc.message)
    _announce(backend)
    if item is None:
        _fail(f"submission {submission_id} not found")
    if as_json:
        _emit(item)
    else:
        _print_submission(item)


@app.command()
def wallet(adapter: AdapterOpt = None, as_json: JsonFlag = False) -> None:
    """Show the backend's resolved profile, environment, and public wallet address."""
    try:
        status = _service(adapter).backend_info()
    except AdapterError as exc:
        _fail(str(exc))
    if as_json:
        _emit(status)
        return
    typer.echo(f"adapter:      {status.adapter}")
    typer.echo(f"profile:      {status.profile}")
    typer.echo(f"environment:  {status.environment}")
    typer.echo(f"wallet:       {status.wallet_address}")
    typer.echo(f"writes:       {'enabled' if status.writes_enabled else 'disabled'}")


@app.command()
def status() -> None:
    """Show local state: data directory, contract counts, configured adapter."""
    stats = _service(None).stats()
    typer.echo(f"version:   {stats.version}")
    typer.echo(f"data dir:  {stats.data_dir}")
    total = sum(stats.counts.values())
    typer.echo(f"contracts: {total}")
    for st in ContractStatus:
        if st.value in stats.counts:
            typer.echo(f"  {st.value:<17} {stats.counts[st.value]}")
    typer.echo(f"adapter:   {stats.adapter} (from {config.ENV_ADAPTER}; default mock)")
    if stats.adapter == "mock":
        backend = MockGibworkAdapter(state_path=config.mock_state_path())
        typer.echo(f"  wallet   {backend.wallet_address}")
        typer.echo(f"  balance  {backend.balance} USDC")
        typer.echo(f"  tasks    {len(backend.list_tasks())}")
    else:
        typer.echo(f"  profile      {config.gibwork_profile() or '(gibwork default)'}")
        typer.echo(f"  environment  {config.gibwork_environment() or '(from profile)'}")
        typer.echo("  run `hf wallet` to resolve the wallet without moving funds")


@mcp_app.command("serve")
def mcp_serve(adapter: AdapterOpt = None) -> None:
    """Run the HumanFallback MCP server over stdio (no submit tools are exposed)."""
    from humanfallback.mcp_server import serve

    svc = _service(adapter)
    try:
        serve(svc, log=_note)
    except AdapterError as exc:
        _fail(str(exc))


@mcp_app.command("snippet")
def mcp_snippet(adapter: AdapterOpt = None) -> None:
    """Print the commands and JSON needed to register this server with Claude Code."""
    from humanfallback.mcp_server import claude_code_snippet

    typer.echo(claude_code_snippet(adapter or config.adapter_name()))


if __name__ == "__main__":
    app()
