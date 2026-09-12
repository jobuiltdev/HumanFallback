# HumanFallback

An agent-to-human delegation layer built around [Gibwork](https://gib.work).

Autonomous agents hit tasks they cannot complete on their own: something has
to be picked up in person, a document needs a real signature, an account
owner has to click through a second factor, or a judgement call needs human
taste. HumanFallback detects those tasks, converts them into structured
**Task Contracts** with explicit acceptance criteria and evidence
requirements, and delegates them to people through Gibwork bounties.

## Status: Milestone 3

M1 delivered the local foundation, M2 the real Gibwork integration, and
M3 exposes HumanFallback itself as an MCP server so agents can use it
directly. The default backend is still the mock, and nothing reaches
Gibwork unless you select the `gibwork` adapter. No path, CLI or MCP,
moves money without a person approving a quote in a terminal.

| Piece | Where |
|---|---|
| Task Contract schema | `src/humanfallback/models/` |
| Human-required task classifier (rule-based) | `src/humanfallback/classifier/` |
| Contract builder with acceptance criteria and evidence templates | `src/humanfallback/contracts/` |
| Adapter protocol, mock adapter, real Gibwork adapter | `src/humanfallback/adapters/` |
| Shared operations layer (rules, locking, one-shot submit) | `src/humanfallback/service.py` |
| SQLite persistence | `src/humanfallback/store/` |
| `hf` command-line tool | `src/humanfallback/cli.py` |
| MCP server for agents | `src/humanfallback/mcp_server.py` |

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

## Using HumanFallback from an agent (MCP)

`hf mcp serve` runs HumanFallback as an MCP server over stdio. Agents can
classify requests, build contracts, get a funding quote, and inspect
bounties and submissions. **They cannot move money.** The server exposes
no submit, refund, or manual-resolve tool, and the adapter is pinned when
the server starts; tools take no backend argument.

### Connect to Claude Code

```sh
# Print the exact commands for this checkout
uv run hf mcp snippet                     # mock backend (default)
uv run hf mcp snippet --adapter gibwork   # real backend, explicitly

# Which amounts to:
claude mcp add humanfallback -- uv --directory /path/to/humanfallback run hf mcp serve
claude mcp add humanfallback-gibwork -e HF_ADAPTER=gibwork -- \
    uv --directory /path/to/humanfallback run hf mcp serve
```

Or in `.mcp.json`:

```json
{
  "mcpServers": {
    "humanfallback": {
      "command": "uv",
      "args": ["--directory", "/path/to/humanfallback", "run", "hf", "mcp", "serve"],
      "env": {}
    }
  }
}
```

The gibwork variant needs the Gibwork CLI installed with a configured
profile (see above). At startup the server resolves and prints the
backend's profile, environment, and wallet to stderr, and refuses to start
if the profile is unusable.

### Tools

All tools return structured content. Failures are `isError` results with
`{"error": {"code", "message", "details"}}`, the same envelope the Gibwork
MCP server uses.

| Tool | Input | Returns | Backend |
|---|---|---|---|
| `humanfallback_classify` | `text` | classification + recommended action | none |
| `humanfallback_contract_create` | `text`, `reward`, `min_submission_amount?`, `title?`, `tags?`, `deadline?`, `force?` | contract view | none |
| `humanfallback_contract_get` | `contract_id` | contract view | none |
| `humanfallback_contract_list` | `status?`, `category?`, `limit?` | summaries | none |
| `humanfallback_contract_refresh` | `contract_id` | contract view with remote snapshot | read |
| `humanfallback_contract_reconcile` | `contract_id` | outcome + contract view | read |
| `humanfallback_delegate_prepare` | `contract_id` | quote + ephemeral confirmation | prepare (no funds move) |
| `humanfallback_submission_list` | `contract_id`, `status?` | submissions | read |
| `humanfallback_submission_get` | `contract_id`, `submission_id` | one submission | read |
| `humanfallback_wallet` | — | profile, environment, wallet | read |
| `humanfallback_status` | — | version, pinned adapter, counts | none |

A contract view carries `locked`, `allowed_actions` (the tools that make
sense next), and `human_action_required` (what a person has to do, if
anything). A `submit_uncertain` contract shows `locked: true` and only
`humanfallback_contract_reconcile` is allowed; `delegate_prepare` is
refused with `CONTRACT_LOCKED` until the backend confirms the outcome or a
person runs `hf contract resolve`.

### The approval boundary

`humanfallback_delegate_prepare` returns the exact quote and records a
`prepared` attempt on the contract. The confirmation it includes is marked
`ephemeral: true, usable_for_submit: false`: it expires with the call and
cannot be submitted through MCP or anywhere else. To fund the bounty a
person runs the command in `human_action_required`:

```sh
hf delegate <contract_id> --adapter gibwork --confirm
```

That command prepares a **fresh** quote in its own session, prints it, and
asks the person to approve it. Nothing an agent obtains over MCP can be
reused to spend.

## How it fits together

```
CLI (hf)            MCP server (hf mcp serve)
    \                  /
     service.py  -- rules, uncertain-submit lock, attempts, one-shot submit
        |
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
  cli.py              typer application (presents results, collects approval)
  mcp_server.py       MCP server for agents (no money-moving tools)
  service.py          shared operations: rules, locking, attempts, one-shot submit
  backends.py         adapter construction, pinned per process
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
