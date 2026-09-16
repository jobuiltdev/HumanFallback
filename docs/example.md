# End-to-end example

One request, from classification to a ranked set of submissions. The
output below comes from the mock backend, which reproduces the constraints
observed on Gibwork stage (funding range, token account, five-minute
confirmations, zero platform fee). Identifiers are shortened placeholders;
against the real backend they are UUIDs and a Solana wallet address.

## 1. Is this something a person has to do?

```
$ hf classify "Go to the hardware store and take a photo of the shelf"
human_required: True
category:       physical_action
confidence:     0.95
reasons:
  - requires physical presence (matched 'Go to the')
  - requires taking a photo of something real (matched 'take a photo')
```

A request like "refactor the auth module and write tests" is classified
agent-capable and `hf contract create` refuses it unless `--force` is
given.

## 2. Build the Task Contract

```
$ hf contract create "Go to the hardware store on Main Street and take a photo of the paint shelf showing today's prices" \
    --title "Photograph the paint shelf at the Main Street hardware store" \
    --reward 5.00 --tag errand --tag photo
id:        c-paint-shelf
title:     Photograph the paint shelf at the Main Street hardware store
status:    ready
category:  physical_action  (human_required=True, confidence=0.95)
reward:    5.00 USDC (min per submission 5.00)
tags:      errand, photo
deadline:  -
acceptance criteria:
  ac-1  [manual]  Submission fulfils the request as described  <- ev-1
  ac-2  [manual]  Action was performed at the specified place  <- ev-1
  ac-3  [evidence_present]  Photo evidence is present and unambiguous  <- ev-1
evidence requirements:
  ev-1  [photo]  Photo showing the completed action with visible location or time context
```

The `physical_action` template asks for one photo and links every
criterion to it. `ac-3` can be checked mechanically; `ac-1` and `ac-2`
are for a person. A `--spec` file can replace the template with explicit
criteria, including `pattern` checks and `min_words` constraints.

## 3. Quote the bounty (nothing moves)

```
$ hf delegate c-paint-shelf --adapter gibwork
backend: gibwork  profile=default  environment=stage  wallet=<wallet>  writes=enabled
note: this backend moves real funds; stage uses mainnet USDC.
dry run: prepare only; nothing will be submitted
adapter:           gibwork (stage)
wallet:            <wallet>
confirmation_id:   <confirmation>
intent_id:         <intent>
task_id:           t-paint-shelf
token:             USDC EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v
funding_amount:    5.00
platform_fee:      0.00 (0.0%)
total_debit:       5.00
expires_at:        2026-09-16T11:26:32+00:00
dry run: nothing submitted. Re-run with --confirm to fund.
```

## 4. Fund it, with a person saying yes

```
$ hf delegate c-paint-shelf --adapter gibwork --confirm
...same banner and a fresh quote...
Fund 5.00 USDC from wallet <wallet> on gibwork/stage? [y/N]: y
delegated c-paint-shelf via gibwork: task_id=t-paint-shelf
debited 5.00 USDC
```

The quote shown is the one being approved: the confirm path prepares a
fresh confirmation in its own session rather than reusing one from an
earlier call. Over MCP, `humanfallback_delegate_prepare` returns the same
quote but a confirmation marked `usable_for_submit: false`; there is no
submit tool.

## 5. Retrieve the submissions

```
$ hf submissions c-paint-shelf --adapter gibwork
s-a  pending    worker_a   <p>Done. Photo of the paint shelf at the Main Street store, taken this afternoon
s-b  pending    worker_b   <p>done</p>
s-c  pending    worker_c   <p>Went to the hardware store and took the photo of the shelf as asked, attached
```

`s-a` attached an image file, `s-b` attached nothing, and `s-c` attached
a media item whose id carries no file type.

## 6. Review

An incomplete one:

```
$ hf review submission c-paint-shelf s-b --adapter gibwork
submission:      s-b  (by worker_b)
score:           10/100
recommendation:  reject_candidate
human judgment:  required
evidence:
  [x] ev-1  photo        required  missing  no photo evidence found
acceptance criteria:
  [!] ac-1  judgment  blocked      Submission fulfils the request as described  (cannot be judged: evidence ev-1 missing)
  [!] ac-2  judgment  blocked      Action was performed at the specified place  (cannot be judged: evidence ev-1 missing)
  [x] ac-3  factual   fail         Photo evidence is present and unambiguous  (evidence ev-1 missing)
deliverables:
  [+] content_present              1 words, 0 evidence items
  [x] content_substantive          needs >= 10 words or at least one evidence item
  [x] required_evidence_supplied   0 of 1 required items found
  [+] constraints_satisfied        all evidence constraints met
missing:
  - evidence ev-1: Photo showing the completed action with visible location or time context
  - criterion ac-1: Submission fulfils the request as described
  - criterion ac-2: Action was performed at the specified place
  - criterion ac-3: Photo evidence is present and unambiguous
  - deliverable content_substantive: needs >= 10 words or at least one evidence item
  - deliverable required_evidence_supplied: 0 of 1 required items found
flags (factual):
  [critical] EMPTY_SUBMISSION: fewer than 3 words and no links or media
  [warning] FILLER_ONLY: body is only a filler phrase ('done') with no evidence
  [critical] MISSING_REQUIRED_EVIDENCE: 1 of 1 required evidence items missing
score breakdown:
  required_evidence      0/50  50/1 points per required item; ev-1=0%
  factual_criteria       0/30  0 of 1 factual criteria pass (30/1 each)
  deliverables          10/20  content_present 10/10, content_substantive 0/10
  judgment_criteria      0/0   2 criteria need a person's judgment and are never scored
summary: Score 10/100, reject_candidate: critical issue: EMPTY_SUBMISSION, MISSING_REQUIRED_EVIDENCE. ...
```

A strong one:

```
$ hf review submission c-paint-shelf s-a --adapter gibwork
submission:      s-a  (by worker_a)
score:           100/100
recommendation:  strong
human judgment:  required
evidence:
  [+] ev-1  photo        required  found  image evidence https://cdn.example/uploads/shelf-2026-09-16.jpg
acceptance criteria:
  [?] ac-1  judgment  needs_human  Submission fulfils the request as described  (requires a person's judgment)
  [?] ac-2  judgment  needs_human  Action was performed at the specified place  (requires a person's judgment)
  [+] ac-3  factual   pass         Photo evidence is present and unambiguous  (evidence ev-1 present)
deliverables:
  [+] content_present              26 words, 1 evidence items
  [+] content_substantive          needs >= 10 words or at least one evidence item
  [+] required_evidence_supplied   1 of 1 required items found
  [+] constraints_satisfied        all evidence constraints met
score breakdown:
  required_evidence     50/50  50/1 points per required item; ev-1=100%
  factual_criteria      30/30  1 of 1 factual criteria pass (30/1 each)
  deliverables          20/20  content_present 10/10, content_substantive 10/10
  judgment_criteria      0/0   2 criteria need a person's judgment and are never scored
summary: Score 100/100, strong: all verifiable checks pass (score 100). Required evidence 1/1 found.
         Factual criteria 1/1 pass. 2 criteria still need your judgment; the score does not cover them.
         Advisory only; approve or reject in Gibwork yourself.
```

`strong` means the verifiable parts are complete. Whether the photo shows
the right shelf at the right store is still `ac-1` and `ac-2`, and those
stay with the reviewer.

The third submission scores 75 and is `needs_human_review`: the media
item is counted at half credit because nothing proves it is a photo, and
the `UNVERIFIED_EVIDENCE_TYPE` flag says so.

## 7. Rank

```
$ hf review rank c-paint-shelf --adapter gibwork
contract: c-paint-shelf
rank  score  recommendation      judgment  missing  flags  submission
1     100    strong              yes       0        0      s-a
2     75     needs_human_review  yes       0        1      s-c
3     10     reject_candidate    yes       6        3      s-b
strongest: s-a
notes:
  - s-a ranks first on verifiable checks (score 100)
  - 3 of 3 submissions need a person's judgment on at least one criterion
human action: Review the ranked scorecards, then approve or reject submissions in Gibwork yourself. HumanFallback does not approve, reject, or pay.
```

## 8. Decide

The person opens the top submission in Gibwork, checks the two manual
criteria against the photo, and approves or rejects there. If the bounty
is no longer needed, `hf refund c-paint-shelf --adapter gibwork --confirm`
follows the same prepare, quote, approve, submit shape.

Every command above takes `--json` for machine-readable output, and the
same operations are available to agents as `humanfallback_*` MCP tools.
