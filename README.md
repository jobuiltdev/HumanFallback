# HumanFallback

An agent-to-human delegation layer built on [Gibwork](https://gib.work).

Agents run into work they cannot do themselves: something has to be
picked up in person, a document needs a real signature, an account owner
has to click through a second factor, a judgement call needs human taste.
HumanFallback detects those requests, turns them into a structured Task
Contract with acceptance criteria and evidence requirements, delegates
them to people as Gibwork bounties, and scores what comes back against
the contract. A person still approves every payment and every submission.

Built for the Gibwork Developer Hackathon. Python 3.13, `uv`, no
services other than Gibwork itself.

## How it works

```
agent or developer request
  -> classifier decides whether a person is needed
  -> Task Contract: reward, acceptance criteria, evidence requirements
  -> Gibwork adapter prepares the bounty and shows the exact quote
  -> a person approves the quote in a terminal; the bounty is funded once
  -> contributors submit work on Gibwork
  -> evidence extraction and deterministic checks against the contract
  -> submissions ranked
  -> advisory scorecards with score, flags, and what is still missing
  -> a person approves, rejects, or pays in Gibwork
```

Two front ends share one service: the `hf` command line and an MCP
server for agents. The MCP server can classify, build contracts, quote,
inspect, and review. It has no tool that submits, refunds, approves,
rejects, or pays.

## What this adds on top of the Gibwork CLI and MCP

The Gibwork CLI and MCP server already create tasks, list submissions,
and pay contributors, and HumanFallback uses them for all of that. What
it adds is the layer an agent needs to hand work over safely and get a
usable answer back:

- classification of a request as human-required or agent-capable, with
  reasons
- a Task Contract that states what must be true and what proves it, before
  any money moves
- evidence requirements with checkable constraints (`min_words`,
  `url_pattern`, `domain`, `extension`)
- deterministic review of each submission: evidence found or missing,
  factual criteria pass or fail, every score point attributed to a reason
- evidence provenance: which URL, image, media item, or transaction
  satisfied which requirement, and whether it arrived as an inline image
  or a media attachment
- ranking and comparison across submissions, including ties and shared
  evidence
- an explicit boundary between what was verified and what a person still
  has to judge
- one prepare, quote, confirm, submit shape for every financial action,
  with an uncertain-submit lock and reconciliation when a result is lost

## Quick start

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/). The default
backend is a mock that behaves like Gibwork stage, so this runs with no
account and no funds.

```sh
uv sync
uv run pytest

uv run hf classify "Go to the hardware store and take a photo of the shelf"
uv run hf contract create "Go to the hardware store and take a photo of the shelf" \
    --reward 5.00 --tag errand --tag photo
uv run hf contract list
uv run hf delegate <contract_id>             # quote only
uv run hf delegate <contract_id> --confirm   # asks, then funds the mock bounty
uv run hf status
```

`docs/example.md` walks through the same flow including submissions,
reviews, and ranking.

## Commands

| Command | What it does |
|---|---|
| `hf classify "<text>"` | Human-required or not, category, confidence, reasons |
| `hf contract create "<text>" [--reward] [--title] [--tag] [--deadline] [--spec file.json] [--force]` | Build and save a Task Contract |
| `hf contract show / list / refresh / reconcile / resolve` | Inspect, sync with the backend, clear an uncertain submit |
| `hf delegate <id> [--adapter gibwork] [--confirm [--yes]]` | Quote the bounty; fund it only with `--confirm` |
| `hf refund <id> [--adapter gibwork] [--confirm [--yes]]` | Refund an open bounty, same shape |
| `hf submissions <id>`, `hf submission <id> <sub_id>` | Retrieve submissions |
| `hf review submission / all / rank <id>` | Advisory scorecards and ranking |
| `hf wallet`, `hf status` | Resolved backend identity; local counts |
| `hf mcp serve`, `hf mcp snippet` | Run the MCP server; print client registration |

Every command accepts `--json`. Local state lives in `~/.humanfallback/`
(override with `HF_HOME`).

`--spec` takes a JSON file with `evidence_requirements` and
`acceptance_criteria` that replace the category template, so `pattern`
and `exact_match` checks and evidence constraints can be stated up front.
`docs/m5/spec.json` is a complete example.

## Using the real Gibwork backend

Install the Gibwork CLI (`npm install --global @gibwork/cli`, 0.2.4 or
later) and configure a profile with `gibwork config set keypair-path`.
HumanFallback starts `gibwork mcp serve` as a child process and the CLI
signs inside that process. HumanFallback never reads, stores, or forwards
key material; `GIBWORK_PRIVATE_KEY` is stripped from the child
environment on purpose.

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
uv run hf wallet --adapter gibwork                       # resolve the wallet, no API call
uv run hf delegate <id> --adapter gibwork                # prepare only, prints the quote
uv run hf delegate <id> --adapter gibwork --confirm      # fresh quote, y/N, one submit
uv run hf contract refresh <id> --adapter gibwork
uv run hf submissions <id> --adapter gibwork
uv run hf review rank <id> --adapter gibwork
uv run hf refund <id> --adapter gibwork --confirm
```

Constraints observed on stage and enforced locally before any call:
funding between 1.00 and 100000.00 USDC, mint
`EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`, confirmations valid for
about five minutes.

## Financial safety

- Inspection commands start the Gibwork server `--read-only`; write tools
  are not registered in that process.
- `delegate` and `refund` are prepare, quote, approval, submit. Prepare
  moves nothing. Submit happens at most once per confirmation and never
  without `--confirm` plus an interactive yes or an explicit `--yes`.
  `--yes` alone is a usage error.
- The confirm path prepares a fresh confirmation in its own session, so
  the quote on screen is the one being approved.
- If a submit is dispatched and the outcome is unknown, the contract
  enters `submit_uncertain` and every further submit is refused until
  `hf contract reconcile` finds the result on Gibwork, or a person checks
  and runs `hf contract resolve`. Exit status 22 marks this case.
- Every prepare/submit cycle is recorded on the contract with its
  identifiers, quote, and outcome.
- Backend errors map to stable codes: `AMOUNT_OUT_OF_RANGE`,
  `UNSUPPORTED_MINT`, `MISSING_TOKEN_ACCOUNT`, `INSUFFICIENT_FUNDS`,
  `CONFIRMATION_EXPIRED`, `CONFIRMATION_INVALID`, `CREDENTIAL_ERROR`,
  `CONFIG_ERROR`, `NETWORK_ERROR`, `AMBIGUOUS_SUBMIT`, `API_ERROR`.
- Nothing in HumanFallback approves, rejects, or pays a submission.
  Reviews are advisory and say so in every result.

## MCP server for agents

`hf mcp serve` runs HumanFallback over stdio. The backend is pinned when
the server starts; tools take no backend argument. `hf mcp snippet`
prints a registration command for a local MCP client; the equivalent
`.mcp.json` entry is:

```json
{
  "mcpServers": {
    "humanfallback": {
      "command": "uv",
      "args": ["--directory", "/path/to/humanfallback", "run", "hf", "mcp", "serve"],
      "env": {"HF_ADAPTER": "mock"}
    }
  }
}
```

Set `HF_ADAPTER` to `gibwork` for the real backend. At startup the server
prints the resolved profile, environment, and wallet to stderr and refuses
to start if the profile is unusable.

| Tool | Input | Backend |
|---|---|---|
| `humanfallback_classify` | `text` | none |
| `humanfallback_contract_create` | `text`, `reward`, `min_submission_amount?`, `title?`, `tags?`, `deadline?`, `force?` | none |
| `humanfallback_contract_get` / `_list` | `contract_id`; filters | none |
| `humanfallback_contract_refresh` / `_reconcile` | `contract_id` | read |
| `humanfallback_delegate_prepare` | `contract_id` | prepare, no funds move |
| `humanfallback_submission_list` / `_get` | `contract_id`, `status?` / `submission_id` | read |
| `humanfallback_review_submission` / `_all` / `_rank` | `contract_id`, `submission_id?` / `status?` | read |
| `humanfallback_wallet`, `humanfallback_status` | none | read / none |

Results are structured content; failures are `isError` results with the
same `{"error": {"code", "message", "details"}}` envelope the Gibwork MCP
server uses. A contract view carries `locked`, `allowed_actions`, and
`human_action_required`.

`humanfallback_delegate_prepare` returns the exact quote and a
confirmation marked `ephemeral: true, usable_for_submit: false`. It cannot
be submitted through MCP or anywhere else. To fund the bounty a person
runs `hf delegate <id> --adapter gibwork --confirm` in a terminal, which
prepares its own fresh quote and asks.

## Reviewing and ranking submissions

```sh
uv run hf review submission <contract_id> <submission_id>
uv run hf review all <contract_id> [--status pending]
uv run hf review rank <contract_id>
```

A review keeps three things apart: what was observed, what was inferred,
and what follows from the two.

Factual checks. For each evidence requirement: `found`,
`found_unverified_type` (present, but a bare media id cannot prove it is
a screenshot rather than a photo), `found_constraint_failed`, or
`missing`. Images pasted into the Gibwork editor arrive as `<img>`
elements in the content rather than as media; both count. For each
acceptance criterion: `evidence_present`, `exact_match`, and `pattern`
pass or fail mechanically; `manual` criteria are `needs_human`, or
`blocked` when their evidence is missing. An optional criterion whose
input was not supplied is `not_applicable`: not missing, not counted, and
never a reason to call a submission incomplete.

Flags. Factual: `EMPTY_SUBMISSION`, `NEAR_EMPTY`, `FILLER_ONLY`,
`MISSING_REQUIRED_EVIDENCE`, `CONSTRAINT_FAILED`, `DUPLICATE_EVIDENCE`,
`UNSUPPORTED_EVIDENCE_TYPE`. Inferred, for a person to confirm:
`UNVERIFIED_EVIDENCE_TYPE`, `IRRELEVANT_CONTENT`, `LOW_RELEVANCE`,
`SUBJECTIVE_ONLY`. Inferred flags never change the score; they change
the recommendation.

Recommendation, by fixed rules: `reject_candidate`, `incomplete`,
`needs_human_review`, `acceptable`, `strong`. `strong` means the
verifiable parts are complete; `human_judgment_required` says whether
manual criteria remain, and they usually do.

Score, 100 points, every one attributed to a named component with a
reason in `score_breakdown`:

| component | max | rule |
|---|---|---|
| required evidence | 50 | split equally; found = full, unverified type = half, constraint failed = half, missing = 0 |
| factual criteria | 30 | split equally among criteria that were supplied; pass = full |
| deliverables | 20 | content present 10, content substantive 10 |
| judgment criteria | 0 | never scored |

If a contract has no factual criteria the 30 points move to evidence; if
it has no required evidence the 50 move to criteria. With neither, those
80 points are reported as `unverifiable` and the recommendation is
`needs_human_review` no matter how complete the text looks.

`hf review rank` orders by score, then missing items, then flags, then
submission time. It names a strongest submission only when the top result
is unique and `strong` or `acceptable`, and notes ties, evidence shared
across submissions, and how many still need judgment.

## Live validation

Milestone 5 ran the whole pipeline against Gibwork stage with real USDC.

- One real stage bounty was created through `hf delegate --confirm`.
- Five real submissions from hackathon contributors were retrieved and
  reviewed. Final ranking scores: 100, 57, 53, 32, 32. The 100 was the
  one submission that included every required item; the others were
  correctly reported as incomplete with the specific items missing.
- Two integration defects were found on the live data and fixed:
  optional criteria were penalised when absent, and images pasted inline
  into Gibwork content were not recognised as image evidence.
- The remaining escrow was refunded through `hf refund --confirm` and the
  remote task was confirmed `CLOSED`, `open=false`.
- Raw validation logs are intentionally kept out of the repository.
  `docs/m5/README.md` has the write-up.

## Demo

A two to four minute walkthrough, mock backend unless noted:

1. `hf classify` on an agent-capable request and on a physical one; show
   the reasons.
2. `hf contract create` for the physical one; point at the criteria and
   the evidence requirement.
3. `hf delegate` dry run: the quote. Then `--confirm` with the y/N prompt.
4. Against stage, read-only: `hf contract refresh` and `hf submissions`
   on the closed M5 bounty.
5. `hf review all` on two contrasting submissions, then `hf review rank`.
6. The MCP tool list, to show what is and is not there.

## Project layout

```
src/humanfallback/
  cli.py          typer application; presents results, collects approval
  mcp_server.py   MCP server for agents; no money-moving tools
  service.py      shared rules: states, uncertain-submit lock, attempts, one-shot submit
  backends.py     adapter construction, pinned per process
  config.py       data directory and backend selection
  models/         pydantic schemas: contract, criteria, evidence, quotes, reviews
  classifier/     Classifier protocol and the rule-based implementation
  contracts/      build_contract, category templates, ContractSpec
  adapters/       GibworkAdapter protocol, mock adapter, MCP adapter, mapping, errors
  review/         extract, text, rules, compare
  store/          ContractStore (SQLite)
tests/            392 tests; fakes for the Gibwork MCP server and captured stage payloads
docs/
  architecture.md components, money boundaries, review pipeline
  example.md      one request end to end
  m5/             live validation write-up, bounty text, contract spec
```

Contracts move through `draft -> ready -> delegated -> submitted ->
approved | rejected -> closed`, with `submit_uncertain` as a side state
that only reconciliation or manual resolution can leave.

## Status and limits

The classifier is rule-based; a learned one can drop in behind the same
protocol. Reviews check structure, presence, and patterns, not the
content of an image. Approval and rejection are done in Gibwork, not
here. The mock adapter is the default; nothing reaches Gibwork unless
`HF_ADAPTER=gibwork` or `--adapter gibwork` is given.
