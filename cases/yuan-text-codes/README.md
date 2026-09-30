# A book title CBDB does not have

While coding one of the people in the [yuan-18-persons](../yuan-18-persons/) case,
the collected works cited in his source turned out to have no `TEXT_CODES` row. This
case is the proposal to add one, and the record of why it was not added.

The title, the person and the source text stay in the batch directory —
`data/staging/2026-08-18-yuan-text-codes/`, gitignored. The batch was never
submitted, so none of it is public through CBDB either, and nothing about this case
needs them to be readable here.

## Batches

| Batch id | Proposals | Outcome |
|---|---|---|
| `2026-08-18-yuan-text-codes` | 1 (`text-codes` / create) | **not submitted, and now never will be** — superseded |

## Outcome — the user created the title themselves

On 2026-09-30 the user created the title by hand, outside this client, as
**`c_textid` 72220** (read back through `GET /api/v2/texts/72220`: 元, source
《全元文》, created by the user that day). The person row that needed it —
王寔's 著述 in [yuan-18-persons](../yuan-18-persons/) — was pointed at 72220 and
is in production. This batch is therefore superseded: sending it now would create a
second, permanent row for the same book.

That is rule 12 working as written. The agent reported the gap with its evidence;
the person holding the decision made it, in the way they chose.

One loose end, and it is not something this client can tie off: the new row's
`c_notes` holds an auto-generated bracketed stamp, and the user does not want it
kept. A `TEXT_CODES` `update` accepts only `c_title` (`API.md` §13.3), so clearing
it has to be done in the web interface or by someone with database access.

## What was checked before concluding the title was missing

Six searches: the full title, two shortenings, the simplified form, and two pinyin
spellings. Two similar titles do exist in `TEXT_CODES` and both are different works.
The searches and their results are written into the batch's `source_excerpt`, and
the proposal's own `source_quote` carries the attestation — which is where both have
to be: a create against a code table has to be able to show it is not a duplicate.

## What this case taught the repo

`text-codes` is **global reference data**: one row is visible to every CBDB user and
can be referenced by any number of person records. The bare code-table resource this
batch uses **has no delete path at all** (`403` / `501`, `API.md` §13.3) and is only
partly correctable afterwards — its `update` accepts `c_title` and never
`c_title_chn`.

(The `text-entity` *aggregate*, which spans `TEXT_CODES` + `TEXT_INSTANCE_DATA`, does
have create/update/delete and can change `title_alt_chn` — see `docs/07` §2.4. It is
not modelled in this client, so it was not an option here, and a row created through
`text-codes` is the irreversible one.)

So a missing book title is something you **report, with the evidence, and let the
user decide** — not something you add to get a batch moving. That is AGENTS.md
**rule 12**, and this case is why it is worded as a judgement rule rather than a
mechanism. The batch sits here, complete and unsent, as the shape that judgement
takes: the work is done, the finding is documented, the decision is the user's.

Written up in `docs/02-review-log.md`, *"text-codes support + AGENTS.md rule 12 gate
(2026-08-18)"*.

## Code

None.
