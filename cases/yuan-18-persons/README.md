# 18 Yuan persons

Enter eighteen Yuan-dynasty people from a supplied text: the full person record for
each, with the sub-resources the source supports.

**Source of the request:** supplied by Hongsu Wang, 2026-08-18. The text itself is
`source.txt` in the batch directory; the code lookups behind every numeric value are
`coding-worksheet.md` beside it.

## Batches

| Batch id | Proposals | Outcome |
|---|---|---|
| `2026-08-18-yuan-18-persons` | 78 | **not submitted** — 41 unresolved conflicts |

What the 78 proposals cover:

| Resource | create | update | delete |
|---|---|---|---|
| `basicinformation` | 16 | 1 | |
| `sources` | 19 | | |
| `postings` | 12 | | |
| `addresses` | 10 | | |
| `altnames` | 9 | 2 | 1 |
| `entries` | 3 | | |
| `statuses` | 3 | | |
| `texts` | 1 | 1 | |

## State

`preview.md` reports **41 unresolved conflicts**, so `validate --staging` classes the
batch as NOT ready to submit and `submit` refuses it. The conflicts are the point:
each is a place where the source and the existing CBDB row disagree and a human has
to choose. Nothing here has been sent anywhere.

Resuming it means working through `review.json` in `review/batch.html`, writing
the decisions back with `apply-review`, and re-validating.

## Code

None — the batch is a hand-written `proposal.yaml`. The lookups that produced its
codes went through `python -m cbdb_agent lookup` (`code_lookup.py`), and the
worksheet records each answer with the query that found it.
