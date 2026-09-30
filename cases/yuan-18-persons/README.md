# 18 Yuan persons

Enter eighteen Yuan-dynasty people from a supplied text: the full person record for
each, with the sub-resources the source supports.

**Source of the request:** supplied by Hongsu Wang, 2026-08-18, as text pasted into
the session (the same eighteen lines are the `bio` column of a spreadsheet kept
outside this repo). The text itself is `source.txt` in the batch directory; the code
lookups behind every numeric value are `coding-worksheet.md` beside it.

## Batches

| Batch id | Proposals | Outcome |
|---|---|---|
| `2026-08-18-yuan-18-persons` | 78 | reviewed 2026-09-30; submitted — 56 landed, then stopped on a connection reset at proposal 57 |
| `2026-09-30-yuan-18-persons-resume` | 22 (20 to send, 2 held out) | submitted — **20/20 landed** |
| `2026-09-30-yuan-18-persons-zhang-haogu` | 3 | submitted — **3/3 landed**: 張好古 entered as the existing 106999 |

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

## State — complete

**All eighteen people are now in CBDB**: 76 of 78 proposals of the first batch
landed, verified, plus the three rows for 張好古 below. The other two (a `statuses` row
for 陳元弼 and one for 朱象先) were resolved `defer` in review and deliberately not
sent; they are still in the resume batch, still deferred.

Sixteen people were created, `c_personid` 705002–705017, in source order: 丁元善
705002, 李元善 705003, 朱元善 705004, 饒元凱 705005, 賀元忠 705006, 陳元弼 705007,
八元凱 705008, 楊文舉 705009, 陳奎 705010, 王寔 705011, 李廷傑 705012, 黃文仲
705013, 周芳 705014, 朱象先 705015, 劉浩然 705016, 裴憲 705017.

Of the other two:

* **蕭㪺 is the existing 35442**, corrected rather than created: given name set to
  㪺 (U+3ABA), 字 惟斗 / 維斗 retyped from 未詳 to 字, a stray alias `勤齋\` (with a
  trailing backslash) deleted, a 《全元文》 citation added, and a note added to his
  著述 row. 690674 is a duplicate of him; this batch did not touch it.
* **張好古 is the existing 106999**, by the user's decision on 2026-09-30
  (`coding-worksheet.md` §A2, option a; 106998 is a military officer and was ruled
  out). A third batch added the 《全元文》 citation, 建安 as a second 籍貫 — coded to
  the Jin-period 建安 under 蓋州 (2873), the one that is 今屬遼寧 — and his
  講經師 post in `c_notes`, since it has no office code and the 宮 no institution
  code (the same treatment as 朱象先's). 106999's 安陽 籍貫 is unchanged and stays
  the index address.

Read back after submission (AGENTS.md rule 11): every returned `result.row` was
compared with what was sent — one difference, on 朱象先's `c_notes`, where the
server normalised 説 (U+8AAC) to 說 (U+8AAA). The deletion and 35442's corrections
were confirmed through `GET /cbdbapi/person`, and the 著述 note through
`GET /api/v2/get`.

The book title the batch needed, 《聽雪先生集》, was created by hand on 2026-09-30 as
`c_textid` 72220 and referenced from there; see [yuan-text-codes](../yuan-text-codes/).

## What the review found in the tool

Three defects, all fixed on 2026-09-30 and logged in `docs/02-review-log.md`:

1. **A resolution never reached the payload.** `apply-review` recorded which option
   the reviewer chose and `submit` sent `changes`/`target_pk`, which nothing had
   updated: four postings would have gone without an office code, 丁元善's without
   its address, and 王寔's 著述 row with `c_textid` 0.
2. **`defer` meant "hold this proposal out", while the options said "leave this
   field empty".** Five index-year conflicts resolved that way would have dropped
   five people and every row they owned.
3. **`resume` could not resume a person batch** — it refused because of the two
   deferred rows, and it did not rewrite `person_id: p13` into the `c_personid` the
   first run had allocated.

The first two were caught before anything was sent and patched in this batch by
hand; the third was hit and fixed mid-submission. One error of the agent's own is
recorded in the batch file: the 2026-08 draft carried the wrong character for
蕭𣂏's name (U+230CF instead of U+2308F), a transcription slip that the reviewer's
choice of 㪺 kept out of the data.

## Code

None — the batch is a hand-written `proposal.yaml`. The lookups that produced its
codes went through the public lookup endpoints (AGENTS.md rule 1) via
`http_client.py`, and the worksheet records each answer with the query that found
it. (Until 2026-09-30 this line named a `python -m cbdb_agent lookup` command; the
CLI has no such subcommand.)
