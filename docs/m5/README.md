# M5: live validation on Gibwork stage

Goal: prove the M1-M4 pipeline end to end against real Gibwork
infrastructure with real USDC, real contributors, and the deterministic
review layer, then fix whatever the live data exposed.

Result: completed 2026-09-15/16. One 1.00 USDC bounty, five real
submissions, two review defects found and fixed, escrow refunded, remote
task confirmed closed.

## What ran

Environment: Gibwork CLI 0.2.4 (MCP runtime 0.3.1), `stage`, mainnet USDC.

1. `bounty.md` + `spec.json` -> `hf contract create --spec` produced a
   contract with two evidence requirements (screenshot, text with
   `min_words: 60`) and seven criteria: three manual, one
   `evidence_present`, three `pattern`, one of them optional.
2. `hf delegate --adapter gibwork` (dry run) showed the quote:
   funding 1.00, platform fee 0.00 (0%), total debit 1.00.
3. `hf delegate --adapter gibwork --confirm`, run by a person in a
   terminal: fresh prepare, interactive approval, one submit. The
   contract moved to `delegated` with the task id recorded.
4. Retrieval through the current API: `hf contract refresh` now uses
   `gibwork_task_get`, whose payload carries per-status submission counts
   instead of a total; both are mapped.
5. Contributors from the hackathon channel submitted five entries over
   about a day. Each paid Gibwork's participation fee themselves.
6. `hf review all` and `hf review rank` scored them; two defects were
   fixed (below) and the reviews re-run read-only.
7. `hf refund --adapter gibwork --confirm`: prepare, quote, approval, one
   submit. Contract `closed`; `hf contract refresh` reports the remote
   task `CLOSED`, `open=False`, five submissions.

Nothing was approved, rejected, or paid through HumanFallback at any
point; reviews stayed advisory.

## The five submissions, as reviewed

| shape of the submission | score | recommendation | why |
|---|---|---|---|
| ver line, hf version, classify output, feedback, screenshot pasted inline | 100 | strong | every required item found; two manual criteria left to a person |
| pseudo-code with a wrong version string and a pytest line, no image | 57 | incomplete | screenshot missing; version and classify patterns fail; optional pytest line counted |
| ver line and placeholders for the outputs, no image | 53 | incomplete | screenshot missing; version and classify patterns fail |
| one sentence saying it worked | 32 | incomplete | screenshot missing; text under 60 words; no outputs |
| tested on macOS, no outputs, no image | 32 | incomplete | as above |

`hf review rank` named the first one `strongest` (unique, `strong`) and
noted that all five still need a person's judgment. The ordering matched
what a human reviewer concluded independently. No submission was flagged
for irrelevance or duplicate evidence; none carried Gibwork media.

## Defects found on live data

**Optional criteria were penalised.** An optional `pattern` criterion
(`uv run pytest` line) that a submission simply did not include was
scored as failed, listed under missing requirements, and shrank the
factual denominator. Fixed: such criteria are `not_applicable`, left out
of numerator and denominator, named in the score reason and summary, and
can never make a submission incomplete. Supplied optional inputs are
judged as before.

**Inline images were invisible.** A screenshot pasted into the Gibwork
editor arrives as an `<img>` element inside the submission content with
an empty media list. HTML was stripped before extraction, so the
screenshot requirement read as missing and the strongest submission
scored 67/incomplete. Fixed: `<img src>` values are extracted as image
evidence with their source recorded, deduplicated against media, and
satisfy screenshot and photo requirements. Prose links and text that
merely says "screenshot attached" still do not.

Both fixes carry regression tests built from sanitised copies of the
live HTML structures.

## Other observations

- `minSubmissionAmount` is the minimum payout per approved submission,
  not a cap on entrants; a 1.00 bounty with a 1.00 floor received five.
- The bounty text embeds the example `hf classify` command, so the
  classifier filed the contract itself under `physical_action`. Harmless
  with `--spec`, but the derived category is wrong for that text.
- With optional criteria no longer deducting, a submission that satisfies
  everything required scores 100; the 70-89 `acceptable` band is only
  reachable through partial credit on evidence. Left as is.
- Every money-moving step printed the resolved profile, environment, and
  wallet first, and the quote before asking for approval. Prepared
  confirmations expired within five minutes as documented; the confirm
  path prepares fresh each time.

## Feedback from contributors

Concise, anonymised:

- Prerequisites (Git, Python, uv) should be stated before the steps, or
  linked.
- Show an example of the expected command output so a first-time user can
  confirm things work.
- The clone URL should be copy-pasteable in the bounty text.
- The setup itself was reported as quick and clear by everyone who
  completed it, including one contributor on macOS.

## Files

- `bounty.md`: the bounty text as published.
- `spec.json`: the evidence and criteria passed to `hf contract create --spec`.

Raw terminal output and JSON captures from the run are kept out of the
repository.
