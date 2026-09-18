# 陳俊卿 / 陳文龍 kinship note

Record a genealogical reconstruction against the kinship pair between
`c_personid` **10884** (陳俊卿) and **15213** (陳文龍): the two are collateral, not
lineal, and a reader's reconstruction is a fifth reading alongside the four already
in the row's `c_notes`.

**Source of the request:** GitHub issue `cbdb-project/cbdb_sqlite#23`, comment
`4742439661`.

**Resource:** `kinship` / `update`, on `c_kin_code=243` (10884→15213) and its mirror
`c_kin_code=62` (15213→10884).

## Batches

| Batch id | Proposals | Outcome |
|---|---|---|
| `2026-07-17-chen-junqing-chen-wenlong-kinship-note` | 2 | 2 success (local instance) |
| `2026-07-17-chen-junqing-chen-wenlong-kinship-note-attempt2` | 2 | 1 success, 1 failed |
| `2026-07-17-prod-kinship-note-append` | 1 | 1 success — production |

The third batch is the one that landed in production, and it is a single
one-direction append rather than two proposals. That is the whole lesson of this case.

## What this case taught the repo

`kinship` and `associations` are **mirrored pairs**: writing
`c_notes`/`c_source`/`c_pages` on one direction makes the server overwrite the same
fields on the reverse row, in the same transaction, and return `200 ok:true` for
both. Two proposals in one batch writing deliberately different text to the two
directions silently corrupt each other — whichever runs last wins.

That is why the production batch has one proposal, not two, and why AGENTS.md now
carries the section **"Reverse-pair mirror sync (`kinship`, `associations` — check
before every write)"**. The append also had to preserve the existing text
byte-for-byte, including a U+00A0 where a space looked like a space.

Written up in `docs/02-review-log.md`, *"Real-world finding — kinship/associations
c_notes mirror-sync (2026-07-17)"*.

## Code

None. All three batches were hand-written `proposal.yaml` files run through
`python -m cbdb_agent validate --staging` and `submit --staging`.
