# HumanFallback

An agent-to-human delegation layer built around [Gibwork](https://gib.work).

Autonomous agents hit tasks they cannot complete on their own: something has
to be picked up in person, a document needs a real signature, an account
owner has to click through a second factor, or a judgement call needs human
taste. HumanFallback detects those tasks, converts them into structured
**Task Contracts** with explicit acceptance criteria and evidence
requirements, and delegates them to people through Gibwork bounties.

## Status: Milestone 2

M1 delivered the local foundation; M2 adds the real Gibwork integration
behind the same adapter boundary. The default backend is still the mock,
and nothing reaches Gibwork unless you select the `gibwork` adapter.

| Piece | Where |
|---|---|
| Task Contract schema | `src/humanfallback/models/` |
| Human-required task classifier (rule-based) | `src/humanfallback/classifier/` |
| Contract builder with acceptance criteria and evidence templates | `src/humanfallback/contracts/` |
| Adapter protocol, mock adapter, real Gibwork adapter | `src/humanfallback/adapters/` |
| SQLite persistence | `src/humanfallback/store/` |
| `hf` command-line tool | `src/humanfallback/cli.py` |

Not yet: submission approval and rejection, evaluation of returned work,
a learned classifier.

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

# Fund it against the mock adapter (prompts for approval)
uv run hf delegate <id> --confirm

uv run hf status
```

Every command accepts `--json` for machine-readable output.

Local state lives in `~/.humanfallback/` (override with `HF_HOME`).

## Using the real Gibwork backend

Requirements: the Gibwork CLI (`npm install --global @gibwork/cli`) with a
profile whose keypair path is configured through
`gibwork config set keypair-path ...`. HumanFallback never reads, stores,
or forwards key material; it starts `gibwork mcp serve` as a child process
and the CLI signs inside that process. `GIBWORK_PRIVATE_KEY` is stripped
from the child environment on purpose; use a keypair file.

| Variable | Meaning | Default |
|---|---|---|
| `HF_ADAPTER` | `mock` or `gibwork` | `mock` |
| `HF_GIBWORK_PROFILE` | Gibwork CLI profile name | the CLI's own default |
| `HF_GIBWORK_ENVIRONMENT` | `stage` or `production` override | from the profile |
| `HF_GIBWORK_BIN` | path to `gibwork` or its `bin.js` | found on `PATH` |
| `HF_GIBWORK_TIMEOUT_S` | per-call timeout | `60` |

Stage uses real mainnet USDC. Every command that talks to Gibwork prints
the resolved profile, environment, and wallet before doing anything else.

```sh
# Resolve the wallet without touching the API
uv run hf wallet --adapter gibwork

# Quote a bounty (prepare only; nothing is signed)
uv run hf delegate <id> --adapter gibwork

# Fund it: prints the quote, asks for approval, submits once
uv run hf delegate <id> --adapter gibwork --confirm

# Non-interactive approval for scripted runs; refused without --confirm
uv run hf delegate <id> --adapter gibwork --confirm --yes

# Inspect
uv run hf contract refresh <id> --adapter gibwork
uv run hf submissions <id> --adapter gibwork [--status pending]
uv run hf submission <id> <submission-id> --adapter gibwork

# Refund an open bounty (same prepare/approve/submit shape)
uv run hf refund <id> --adapter gibwork
uv run hf refund <id> --adapter gibwork --confirm
```

### How money moves, and how it does not

- Inspection commands start the server with `--read-only`; write tools
  are not registered in that process at all.
- `delegate` and `refund` follow prepare -> quote -> approval -> submit.
  Prepare returns a one-time confirmation id and moves nothing. Submit is
  called at most once per confirmation, and never without `--confirm` plus
  either an interactive yes or an explicit `--yes`.
- `--yes` on its own is a usage error; it only skips the prompt.
- If a submit is dispatched and the outcome is unknown (timeout, crash,
  protocol error), the contract enters `submit_uncertain`. Every further
  submit on that contract is refused until `hf contract reconcile <id>`
  finds the result on Gibwork, or you verify it yourself and run
  `hf contract resolve <id> --outcome not-created|not-refunded`.
- Backend errors map to stable codes: `AMOUNT_OUT_OF_RANGE`,
  `UNSUPPORTED_MINT`, `MISSING_TOKEN_ACCOUNT`, `INSUFFICIENT_FUNDS`,
  `CONFIRMATION_EXPIRED`, `CONFIRMATION_INVALID`, `CREDENTIAL_ERROR`,
  `CONFIG_ERROR`, `NETWORK_ERROR`, `AMBIGUOUS_SUBMIT`, `API_ERROR`.
  Exit status 22 marks an ambiguous submit.
- Every prepare/submit cycle is recorded on the contract as an attempt
  with its identifiers, quote, and outcome.

Constraints observed on the stage API and enforced locally before any
call: funding between 1.00 and 100000.00, USDC mint
`EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`, confirmations valid for
about five minutes.

## How it fits together

```
request text
  -> classifier      human_required? category, confidence, reasons
  -> builder         TaskContract with acceptance criteria + evidence requirements
  -> store           SQLite
  -> adapter         prepare (quote + confirmation id) -> approval -> submit (one-shot)
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
approved | rejected -> closed`, with `submit_uncertain` as a side state
that only reconciliation or manual resolution can leave.

### Classifier

`RuleBasedClassifier` scores a request against a table of weighted regex
rules grouped into categories: physical action, identity verification,
subjective judgement, account access, legal signature, offline data
collection, human interaction. Agent-capable verbs (write, summarise,
refactor, ...) count against. A request is human-required when its
strongest category clears the threshold and beats the counter-score. The
`Classifier` protocol lets a different implementation drop in later.

### Adapters

Both adapters implement the same `GibworkAdapter` protocol: wallet status,
task list/get, submission list/get, task prepare/submit, refund
prepare/submit.

`MockGibworkAdapter` reproduces the constraints observed on the Gibwork
staging API: funding between 1.00 and 100000.00, a required token account,
a prepare/submit split with a five-minute confirmation window bound to one
operation and consumable once, and a zero platform fee. Its state is
persisted to `mock_gibwork.json` in the data directory so CLI invocations
share a wallet balance, task list, and seeded submissions.

`GibworkMcpAdapter` talks to `gibwork mcp serve` over stdio using a small
JSON-RPC client (`adapters/mcp_client.py`), maps tool payloads into the
domain models (`adapters/mapping.py`), and translates errors into the
shared codes. Prepared confirmations live inside the server process, so
prepare and submit happen within one adapter session. The adapter refuses
to submit a confirmation it did not prepare, or one it already submitted,
before the server is even asked.

## Layout

```
src/humanfallback/
  cli.py              typer application
  config.py           data directory and backend selection
  models/             pydantic schemas
  classifier/         Classifier protocol + rule-based implementation
  contracts/          build_contract and per-category templates
  adapters/           GibworkAdapter protocol, errors, MockGibworkAdapter,
                      GibworkMcpAdapter, mcp_client, mapping
  store/              ContractStore (SQLite)
tests/
  fakes/              stdio fake MCP server, in-process fake session,
                      payloads captured from the stage server
```
