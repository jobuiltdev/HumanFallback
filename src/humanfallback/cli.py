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

Money moves only on `--confirm`, and only after the quote is printed and a
person approves it. `--yes` skips the interactive prompt but is refused
unless `--confirm` is also present.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Literal

import typer
from pydantic import BaseModel, ValidationError

from humanfallback import __version__, config
from humanfallback.adapters import (
    AdapterError,
    AmbiguousSubmit,
    GibworkAdapter,
    GibworkMcpAdapter,
    MockGibworkAdapter,
)
from humanfallback.classifier import default_classifier
from humanfallback.contracts import AgentCapableRequest, build_contract
from humanfallback.models import (
    DELEGATABLE,
    REFUNDABLE,
    AttemptOutcome,
    ContractStatus,
    DelegationAttempt,
    DelegationRecord,
    PrepareResult,
    RefundRecord,
    RemoteSnapshot,
    Reward,
    TaskCategory,
    TaskContract,
)
from humanfallback.store import ContractStore

EXIT_USAGE = 2
EXIT_AMBIGUOUS = 22

app = typer.Typer(
    help="Detect human-required tasks and turn them into Task Contracts.",
    no_args_is_help=True,
)
contract_app = typer.Typer(help="Create and inspect Task Contracts.", no_args_is_help=True)
app.add_typer(contract_app, name="contract")

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


# -- adapter factories ----------------------------------------------------------

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


def _adapter(name: str | None, *, writes: bool) -> GibworkAdapter:
    chosen = (name or config.adapter_name()).lower()
    factory = ADAPTER_FACTORIES.get(chosen)
    if factory is None:
        _fail(f"unknown adapter {chosen!r}; choose one of {', '.join(ADAPTER_FACTORIES)}")
    try:
        return factory(writes)
    except AdapterError as exc:
        _fail(str(exc))
    raise AssertionError  # unreachable


def _announce(backend: GibworkAdapter) -> None:
    """Resolve and print the backend identity before anything else happens."""
    try:
        status = backend.wallet_status()
    except AdapterError as exc:
        _fail(f"could not resolve backend: {exc}")
    _note(
        f"backend: {status.adapter}  profile={status.profile}  "
        f"environment={status.environment}  wallet={status.wallet_address}  "
        f"writes={'enabled' if status.writes_enabled else 'disabled'}"
    )
    if status.adapter != "mock":
        _note("note: this backend moves real funds; stage uses mainnet USDC.")


# -- output helpers -------------------------------------------------------------


def _emit(model: BaseModel) -> None:
    typer.echo(model.model_dump_json(indent=2))


def _note(message: str) -> None:
    typer.secho(message, err=True, fg=typer.colors.YELLOW)


def _fail(message: str, code: int = 1) -> None:
    typer.secho(f"error: {message}", err=True, fg=typer.colors.RED)
    raise typer.Exit(code)


def _open_store() -> ContractStore:
    return ContractStore(config.db_path())


def _load_contract(store: ContractStore, contract_id: str) -> TaskContract:
    contract = store.get(contract_id)
    if contract is None:
        _fail(f"no contract with id {contract_id}")
    return contract  # type: ignore[return-value]


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


def _refuse_if_uncertain(contract: TaskContract) -> None:
    if contract.status is ContractStatus.SUBMIT_UNCERTAIN:
        _fail(
            "a previous submit has an unknown outcome; run "
            f"`hf contract reconcile {contract.id}` before any further submit"
        )


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
    result = default_classifier().classify(text)
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
    classification = default_classifier().classify(text)
    try:
        reward_model = Reward(amount=reward, min_submission_amount=min_submission)
        contract = build_contract(
            text,
            classification,
            reward_model,
            title=title,
            tags=tag,
            deadline=deadline,
            allow_agent_capable=force,
        )
    except AgentCapableRequest:
        _fail(
            f"classified as agent-capable (confidence {classification.confidence}); "
            "use --force to build a contract anyway"
        )
    except ValidationError as exc:
        _fail(str(exc))

    with _open_store() as store:
        store.save(contract)
    if as_json:
        _emit(contract)
    else:
        _print_contract(contract)


@contract_app.command("show")
def contract_show(contract_id: str, as_json: JsonFlag = False) -> None:
    """Show one contract."""
    with _open_store() as store:
        contract = _load_contract(store, contract_id)
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
    with _open_store() as store:
        contracts = store.list(status=status, category=category)
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
    with _open_store() as store, _adapter(adapter, writes=False) as backend:
        contract = _load_contract(store, contract_id)
        if contract.delegation is None:
            _fail("contract has not been delegated; nothing to refresh")
        _announce(backend)
        try:
            task = backend.get_task(contract.delegation.task_id)
        except AdapterError as exc:
            _fail(str(exc))
        if task is None:
            _fail(f"task {contract.delegation.task_id} not found on {backend.name}")
        now = datetime.now(UTC)
        contract.remote = RemoteSnapshot(
            status=task.status,
            is_open=task.is_open,
            total_submissions=task.total_submissions,
            synced_at=now,
        )
        if contract.status is ContractStatus.DELEGATED and task.total_submissions:
            contract.transition_to(ContractStatus.SUBMITTED)
        contract.updated_at = now
        store.save(contract)
    if as_json:
        _emit(contract)
    else:
        _print_contract(contract)


@contract_app.command("reconcile")
def contract_reconcile(
    contract_id: str, adapter: AdapterOpt = None, as_json: JsonFlag = False
) -> None:
    """Resolve an uncertain submit by checking the backend for its result."""
    with _open_store() as store, _adapter(adapter, writes=False) as backend:
        contract = _load_contract(store, contract_id)
        attempt = contract.uncertain_attempt
        if contract.status is not ContractStatus.SUBMIT_UNCERTAIN or attempt is None:
            _fail("contract has no uncertain submit to reconcile")
        if attempt.adapter != backend.name:
            _fail(
                f"attempt was made with adapter {attempt.adapter!r}; "
                f"reconcile with the same adapter, not {backend.name!r}"
            )
        _announce(backend)
        try:
            task = backend.get_task(attempt.task_id)
        except AdapterError as exc:
            _fail(str(exc))

        now = datetime.now(UTC)
        if attempt.operation == "task_create":
            if task is None:
                typer.echo(
                    f"task {attempt.task_id} was not found on {backend.name}. The bounty may "
                    "still settle; check again later. If you have verified it never funded, run "
                    f"`hf contract resolve {contract.id} --outcome not-created`."
                )
                raise typer.Exit(1)
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
            store.save(contract)
            typer.echo(
                f"reconciled: task {attempt.task_id} exists on {backend.name}; contract is delegated"
            )
        else:
            if task is None or task.is_open:
                state = "still open" if task is not None else "not found"
                typer.echo(
                    f"task {attempt.task_id} is {state} on {backend.name}. The refund may still "
                    "settle; check again later. If you have verified it never dispatched, run "
                    f"`hf contract resolve {contract.id} --outcome not-refunded`."
                )
                raise typer.Exit(1)
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
            store.save(contract)
            typer.echo(
                f"reconciled: task {attempt.task_id} is closed on {backend.name}; contract is closed"
            )
    if as_json:
        _emit(contract)


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
    with _open_store() as store:
        contract = _load_contract(store, contract_id)
        attempt = contract.uncertain_attempt
        if contract.status is not ContractStatus.SUBMIT_UNCERTAIN or attempt is None:
            _fail("contract has no uncertain submit to resolve")
        expected = "task_create" if outcome == "not-created" else "task_refund"
        if attempt.operation != expected:
            _fail(f"uncertain attempt is a {attempt.operation}; --outcome {outcome} does not apply")
        attempt.outcome = AttemptOutcome.RECONCILED_NOT_FOUND
        attempt.resolved_at = datetime.now(UTC)
        contract.transition_to(attempt.origin_status)
        store.save(contract)
    typer.echo(f"resolved: contract returned to {contract.status.value}")
    if as_json:
        _emit(contract)


def _run_money_operation(
    *,
    contract: TaskContract,
    store: ContractStore,
    backend: GibworkAdapter,
    attempt: DelegationAttempt,
    prompt: str,
    confirm: bool,
    yes: bool,
    submit: Callable[[str], object],
) -> object:
    """Shared approve -> submit-once -> record path for delegate and refund."""
    contract.attempts.append(attempt)
    store.save(contract)

    if not _approved(prompt, confirm=confirm, yes=yes):
        typer.echo("aborted: nothing submitted")
        raise typer.Exit(1)

    try:
        return submit(attempt.confirmation_id)
    except AmbiguousSubmit as exc:
        attempt.outcome = AttemptOutcome.AMBIGUOUS
        attempt.error = str(exc)
        contract.transition_to(ContractStatus.SUBMIT_UNCERTAIN)
        store.save(contract)
        _fail(
            f"{exc} — contract is now {ContractStatus.SUBMIT_UNCERTAIN.value}; "
            f"run `hf contract reconcile {contract.id}`. Do not resubmit.",
            EXIT_AMBIGUOUS,
        )
    except AdapterError as exc:
        attempt.outcome = AttemptOutcome.FAILED
        attempt.error = str(exc)
        store.save(contract)
        _fail(str(exc))
    raise AssertionError  # unreachable


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
    with _open_store() as store, _adapter(adapter, writes=True) as backend:
        contract = _load_contract(store, contract_id)
        _refuse_if_uncertain(contract)
        if contract.status not in DELEGATABLE:
            _fail(f"contract is {contract.status.value}; only ready contracts can be delegated")
        _announce(backend)
        if not confirm:
            _note("dry run: prepare only; nothing will be submitted")

        try:
            prepared = backend.prepare_task(contract)
        except AdapterError as exc:
            _fail(str(exc))

        if not confirm:
            if as_json:
                _emit(prepared)
            else:
                _print_quote(prepared, backend)
                typer.echo("dry run: nothing submitted. Re-run with --confirm to fund.")
            return

        _print_quote(prepared, backend, err=as_json)
        q = prepared.payment_quote
        attempt = DelegationAttempt(
            operation="task_create",
            adapter=backend.name,
            environment=backend.environment,
            wallet_address=prepared.wallet_address,
            origin_status=contract.status,
            task_id=prepared.task_id,
            confirmation_id=prepared.confirmation_id,
            intent_id=prepared.intent_id,
            quote=q,
            prepared_at=prepared.created_at,
        )
        submitted = _run_money_operation(
            contract=contract,
            store=store,
            backend=backend,
            attempt=attempt,
            prompt=(
                f"Fund {q.total_debit} {q.token.symbol} from wallet {prepared.wallet_address} "
                f"on {backend.name}/{backend.environment}?"
            ),
            confirm=confirm,
            yes=yes,
            submit=backend.submit_task,
        )
        attempt.outcome = AttemptOutcome.SUBMITTED
        attempt.resolved_at = submitted.submitted_at  # type: ignore[attr-defined]
        contract.delegation = DelegationRecord(
            adapter=backend.name,
            task_id=submitted.task_id,  # type: ignore[attr-defined]
            intent_id=submitted.intent_id,  # type: ignore[attr-defined]
            confirmation_id=submitted.confirmation_id,  # type: ignore[attr-defined]
            signature=submitted.signature,  # type: ignore[attr-defined]
            quote=q,
            delegated_at=submitted.submitted_at,  # type: ignore[attr-defined]
        )
        contract.transition_to(ContractStatus.DELEGATED)
        store.save(contract)

    if as_json:
        _emit(contract)
    else:
        typer.echo(f"delegated {contract.id} via {backend.name}: task_id={contract.delegation.task_id}")
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
    with _open_store() as store, _adapter(adapter, writes=True) as backend:
        contract = _load_contract(store, contract_id)
        _refuse_if_uncertain(contract)
        if contract.status not in REFUNDABLE or contract.delegation is None:
            _fail(f"contract is {contract.status.value}; only delegated bounties can be refunded")
        if contract.delegation.adapter != backend.name:
            _fail(
                f"bounty was created with adapter {contract.delegation.adapter!r}; "
                f"refund with the same adapter, not {backend.name!r}"
            )
        _announce(backend)
        if not confirm:
            _note("dry run: prepare only; nothing will be submitted")

        try:
            prepared = backend.prepare_refund(contract.delegation.task_id)
        except AdapterError as exc:
            _fail(str(exc))

        wallet_address = prepared.wallet_address or backend.wallet_address
        amount = prepared.refund_amount or "?"
        symbol = prepared.token.symbol if prepared.token else contract.reward.symbol
        err = as_json
        typer.echo(f"adapter:           {backend.name} ({backend.environment})", err=err)
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

        attempt = DelegationAttempt(
            operation="task_refund",
            adapter=backend.name,
            environment=backend.environment,
            wallet_address=wallet_address,
            origin_status=contract.status,
            task_id=prepared.task_id,
            confirmation_id=prepared.confirmation_id,
            intent_id=prepared.intent_id,
            quote=contract.delegation.quote,
            prepared_at=prepared.created_at or datetime.now(UTC),
        )
        submitted = _run_money_operation(
            contract=contract,
            store=store,
            backend=backend,
            attempt=attempt,
            prompt=(
                f"Refund task {prepared.task_id} ({amount} {symbol}) to wallet {wallet_address} "
                f"on {backend.name}/{backend.environment}?"
            ),
            confirm=confirm,
            yes=yes,
            submit=backend.submit_refund,
        )
        attempt.outcome = AttemptOutcome.SUBMITTED
        attempt.resolved_at = submitted.submitted_at  # type: ignore[attr-defined]
        contract.refund = RefundRecord(
            adapter=backend.name,
            task_id=submitted.task_id,  # type: ignore[attr-defined]
            confirmation_id=submitted.confirmation_id,  # type: ignore[attr-defined]
            signature=submitted.signature,  # type: ignore[attr-defined]
            quote=prepared.raw,
            refunded_at=submitted.submitted_at,  # type: ignore[attr-defined]
        )
        contract.transition_to(ContractStatus.CLOSED)
        store.save(contract)

    if as_json:
        _emit(contract)
    else:
        typer.echo(f"refunded {contract.id} via {backend.name}: task_id={contract.refund.task_id}")


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
    with _open_store() as store, _adapter(adapter, writes=False) as backend:
        contract = _load_contract(store, contract_id)
        if contract.delegation is None:
            _fail("contract has not been delegated")
        _announce(backend)
        try:
            items = backend.list_submissions(contract.delegation.task_id, status=status)
        except AdapterError as exc:
            _fail(str(exc))
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
    with _open_store() as store, _adapter(adapter, writes=False) as backend:
        contract = _load_contract(store, contract_id)
        if contract.delegation is None:
            _fail("contract has not been delegated")
        _announce(backend)
        try:
            item = backend.get_submission(contract.delegation.task_id, submission_id)
        except AdapterError as exc:
            _fail(str(exc))
        if item is None:
            _fail(f"submission {submission_id} not found")
    if as_json:
        _emit(item)
        return
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


@app.command()
def wallet(adapter: AdapterOpt = None, as_json: JsonFlag = False) -> None:
    """Show the backend's resolved profile, environment, and public wallet address."""
    with _adapter(adapter, writes=False) as backend:
        try:
            status = backend.wallet_status()
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
    typer.echo(f"version:   {__version__}")
    typer.echo(f"data dir:  {config.data_dir()}")
    with _open_store() as store:
        counts = store.count_by_status()
    total = sum(counts.values())
    typer.echo(f"contracts: {total}")
    for st in ContractStatus:
        if st in counts:
            typer.echo(f"  {st.value:<17} {counts[st]}")
    name = config.adapter_name()
    typer.echo(f"adapter:   {name} (from {config.ENV_ADAPTER}; default mock)")
    if name == "mock":
        backend = MockGibworkAdapter(state_path=config.mock_state_path())
        typer.echo(f"  wallet   {backend.wallet_address}")
        typer.echo(f"  balance  {backend.balance} USDC")
        typer.echo(f"  tasks    {len(backend.list_tasks())}")
    else:
        typer.echo(f"  profile      {config.gibwork_profile() or '(gibwork default)'}")
        typer.echo(f"  environment  {config.gibwork_environment() or '(from profile)'}")
        typer.echo("  run `hf wallet` to resolve the wallet without moving funds")


if __name__ == "__main__":
    app()
