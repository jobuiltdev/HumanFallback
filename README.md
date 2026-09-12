# HumanFallback

An agent-to-human delegation layer built around [Gibwork](https://gib.work).

Autonomous agents hit tasks they cannot complete on their own: something has
to be picked up in person, a document needs a real signature, an account
owner has to click through a second factor, or a judgement call needs human
taste. HumanFallback detects those tasks, converts them into structured
**Task Contracts** with explicit acceptance criteria and evidence
requirements, and delegates them to people through Gibwork bounties.

## Status: Milestone 1

M1 delivers the local foundation. It runs entirely offline and never
touches a wallet or the live Gibwork service.

| Piece | Where |
|---|---|
| Task Contract schema | `src/humanfallback/models/` |
| Human-required task classifier (rule-based) | `src/humanfallback/classifier/` |
| Contract builder with acceptance criteria and evidence templates | `src/humanfallback/contracts/` |
| Adapter protocol and a faithful mock Gibwork adapter | `src/humanfallback/adapters/` |
| SQLite persistence | `src/humanfallback/store/` |
| `hf` command-line tool | `src/humanfallback/cli.py` |

Not in M1: the real Gibwork adapter, submission evaluation, a learned
classifier.

## Setup

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pytest
```

## Usage

```sh
# Is this something a human has to do?
uv run hf classify "Go to the hardware store and take a photo of the shelf"

# Turn it into a contract and save it
uv run hf contract create "Go to the hardware store and take a photo of the shelf" \
    --reward 5.00 --tag errand --tag photo

uv run hf contract list
uv run hf contract show <id>

# Get a funding quote from the mock adapter (dry run)
uv run hf delegate <id>

# Fund it against the mock adapter
uv run hf delegate <id> --confirm

uv run hf status
```

Every command accepts `--json` for machine-readable output.

Local state lives in `~/.humanfallback/` (override with `HF_HOME`).

## How it fits together

```
request text
  -> classifier      human_required? category, confidence, reasons
  -> builder         TaskContract with acceptance criteria + evidence requirements
  -> store           SQLite
  -> adapter         prepare (quote + confirmation id) -> submit (one-shot)
```

### Task Contract

A `TaskContract` carries the original request, its classification, a reward
in a Solana stablecoin (two-decimal string amounts, matching Gibwork), an
optional deadline, up to three discovery tags, and two linked lists:

- **Acceptance criteria** say what must be true for a submission to pass.
  Each has a `check_type` (`manual`, `evidence_present`, `exact_match`,
  `pattern`) and points at the evidence that proves it.
- **Evidence requirements** say what artifact the worker must supply:
  a URL, screenshot, photo, file, text, or transaction, with optional
  constraints the evaluator can act on later.

Contracts move through `draft -> ready -> delegated -> submitted ->
approved | rejected -> closed`. M1 exercises the first three states.

### Classifier

`RuleBasedClassifier` scores a request against a table of weighted regex
rules grouped into categories: physical action, identity verification,
subjective judgement, account access, legal signature, offline data
collection, human interaction. Agent-capable verbs (write, summarise,
refactor, ...) count against. A request is human-required when its
strongest category clears the threshold and beats the counter-score. The
`Classifier` protocol lets a different implementation drop in later.

### Mock adapter

`MockGibworkAdapter` reproduces the constraints observed on the Gibwork
staging API so the real adapter is a swap rather than a rewrite: funding
between 1.00 and 100000.00, a required token account, a prepare/submit
split with a five-minute confirmation window that can be consumed once, and
a zero platform fee. Its state is persisted to `mock_gibwork.json` in the
data directory so CLI invocations share a wallet balance and task list.

## Layout

```
src/humanfallback/
  cli.py              typer application
  config.py           data directory resolution
  models/             pydantic schemas
  classifier/         Classifier protocol + rule-based implementation
  contracts/          build_contract and per-category templates
  adapters/           GibworkAdapter protocol + MockGibworkAdapter
  store/              ContractStore (SQLite)
tests/
```
