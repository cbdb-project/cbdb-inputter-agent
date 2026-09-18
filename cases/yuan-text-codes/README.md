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
| `2026-08-18-yuan-text-codes` | 1 (`text-codes` / create) | **not submitted** — reported to the user as a finding |

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
