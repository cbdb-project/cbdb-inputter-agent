# 知某州事 — a Tang office title

Record the Tang office 知某州事 ("Administrator of Prefectural Civil Affairs") from
《唐會要》卷六八《刺史上》, with two aliases: 攝某州事 and 知州事.

**Source of the request:** user, 2026-09-04.

## Batches

| Batch id | Proposals | Outcome |
|---|---|---|
| `2026-09-04-tang-zhi-mou-zhou-shi` | 1 (`office` / update) | 1 success — **production**, read back and verified |

## Two decisions that shaped it

### The glyph

**Traditional 攝, not the requested simplified 摄.** `OFFICE_CODES` is traditional
throughout, and the server's `char_variant_map` is a curated variant list, not a
簡→繁 converter — 摄 would land verbatim and be invisible to every
traditional-character search. 知州事 is kept as a second alias because it is the form
actually attested, and would otherwise become unsearchable.

### Which row

Not a create. `OFFICE_CODES` row **12304 知州事** is the same office written without
the 某 placeholder, and its own `c_notes` asks for exactly this addition. So the batch
edits 12304 in place — plan B of `docs/10-office-aggregate-design.md` §5.1 — rather
than minting a near-duplicate code beside it.

That choice matters because `office` is an entity aggregate whose `update` is a
**full-row overwrite**: an omitted field is written as `NULL` over whatever was there.
The client refuses a partial payload for it (`full_overwrite_update` in `models.py`),
which is what makes an in-place edit of a global row a reviewable act rather than a
silent truncation.

## What this case taught the repo

- `office` needed three new `ResourceSpec` features before it could be modelled at
  all — full-overwrite update, aggregate shape, and a live duplicate guard
  (`preflight.assert_office_create_is_not_a_duplicate`, which must never be replaced
  by a snapshot lookup because the snapshot is a weekly build).
- `/api/v2/get` answers `501` for `office`, so the read-back went through the public
  lookup `GET /api/select/search/office`. This is why `fetch_current_values()` reports
  "couldn't fetch" for an aggregate proposal instead of failing.

Written up in `docs/02-review-log.md`, *"`office` entity aggregate modelled
(2026-09-04)"* — including §*The write itself — sent to production, verified* and
§*Review passes — 4 SERIOUS + ~20 MINOR, and the guard was wrong three times*.

Design: `docs/10-office-aggregate-design.md`.

## Code

None — a hand-written single-proposal `proposal.yaml`.
