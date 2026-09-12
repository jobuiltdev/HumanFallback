"""Command-line interface.

    hf classify "<request>"
    hf contract create "<request>" [--reward 1.00] [--title ...] [--tag ...]
    hf contract show <id>
    hf contract list [--status ...] [--category ...]
    hf delegate <id> [--adapter mock] [--confirm]
    hf status
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated

import typer
from pydantic import BaseModel, ValidationError

from humanfallback import __version__, config
from humanfallback.adapters import AdapterError, GibworkAdapter, MockGibworkAdapter
from humanfallback.classifier import default_classifier
from humanfallback.contracts import AgentCapableRequest, build_contract
from humanfallback.models import (
    ContractStatus,
    DelegationRecord,
    Reward,
    TaskCategory,
    TaskContract,
)
from humanfallback.store import ContractStore

app = typer.Typer(
    help="Detect human-required tasks and turn them into Task Contracts.",
    no_args_is_help=True,
)
contract_app = typer.Typer(help="Create and inspect Task Contracts.", no_args_is_help=True)
app.add_typer(contract_app, name="contract")

JsonFlag = Annotated[bool, typer.Option("--json", help="Emit JSON instead of text.")]


def _emit(model: BaseModel) -> None:
    typer.echo(model.model_dump_json(indent=2))


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


def _adapter(name: str) -> GibworkAdapter:
    if name == "mock":
        return MockGibworkAdapter(state_path=config.mock_state_path())
    _fail(f"unknown adapter {name!r}; only 'mock' is available")
    raise AssertionError  # unreachable


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
        typer.echo(f"delegated: adapter={d.adapter} task_id={d.task_id} at {d.delegated_at.isoformat()}")


# -- commands ---------------------------------------------------------------


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
        datetime | None, typer.Option(help="ISO-8601 deadline.", formats=["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"])
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
            f"{c.id}  {c.status.value:<9}  {c.classification.category.value:<24}  "
            f"{c.reward.amount} {c.reward.symbol}  {c.title}"
        )


@app.command()
def delegate(
    contract_id: str,
    adapter: Annotated[str, typer.Option(help="Adapter to use.")] = "mock",
    confirm: Annotated[
        bool, typer.Option("--confirm", help="Submit the prepared transaction after quoting.")
    ] = False,
    as_json: JsonFlag = False,
) -> None:
    """Prepare a bounty quote for a READY contract; submit only with --confirm."""
    backend = _adapter(adapter)
    with _open_store() as store:
        contract = _load_contract(store, contract_id)
        if contract.status is not ContractStatus.READY:
            _fail(f"contract is {contract.status.value}; only READY contracts can be delegated")

        try:
            prepared = backend.prepare_task(contract)
        except AdapterError as exc:
            _fail(f"{exc.code}: {exc.message}")

        if not confirm:
            if as_json:
                _emit(prepared)
            else:
                q = prepared.payment_quote
                typer.echo(f"adapter:          {backend.name} ({backend.environment})")
                typer.echo(f"confirmation_id:  {prepared.confirmation_id}")
                typer.echo(f"task_id:          {prepared.task_id}")
                typer.echo(f"token:            {q.token.symbol} {q.token.mint_address}")
                typer.echo(f"funding_amount:   {q.funding_amount}")
                typer.echo(f"platform_fee:     {q.platform_fee.amount} ({q.platform_fee.percent}%)")
                typer.echo(f"total_debit:      {q.total_debit}")
                typer.echo(f"expires_at:       {prepared.expires_at.isoformat()}")
                typer.echo("dry run: nothing submitted. Re-run with --confirm to fund.")
            return

        try:
            submitted = backend.submit_task(prepared.confirmation_id)
        except AdapterError as exc:
            _fail(f"{exc.code}: {exc.message}")

        contract.delegation = DelegationRecord(
            adapter=backend.name,
            task_id=submitted.task_id,
            intent_id=submitted.intent_id,
            confirmation_id=submitted.confirmation_id,
            signature=submitted.signature,
            quote=prepared.payment_quote,
            delegated_at=submitted.submitted_at,
        )
        contract.transition_to(ContractStatus.DELEGATED)
        store.save(contract)

    if as_json:
        _emit(contract)
    else:
        typer.echo(f"delegated {contract.id} via {backend.name}: task_id={submitted.task_id}")
        typer.echo(f"debited {prepared.payment_quote.total_debit} {prepared.payment_quote.token.symbol}")


@app.command()
def status() -> None:
    """Show local state: data directory, contract counts, adapter."""
    typer.echo(f"version:   {__version__}")
    typer.echo(f"data dir:  {config.data_dir()}")
    with _open_store() as store:
        counts = store.count_by_status()
    total = sum(counts.values())
    typer.echo(f"contracts: {total}")
    for st in ContractStatus:
        if st in counts:
            typer.echo(f"  {st.value:<10} {counts[st]}")
    backend = MockGibworkAdapter(state_path=config.mock_state_path())
    typer.echo(f"adapter:   {backend.name} ({backend.environment})")
    typer.echo(f"  wallet   {backend.wallet_address}")
    typer.echo(f"  balance  {backend.balance} USDC")
    typer.echo(f"  tasks    {len(backend.list_tasks())}")


if __name__ == "__main__":
    app()
