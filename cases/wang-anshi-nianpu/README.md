# 王安石, from 《王荊文公年譜》

Enter what a 年譜 of 王安石 (1762) says about him and about twenty people around him:
his postings, residences, honorific names, writings and entry, the kin and
associations it records, and three people CBDB does not have.

**Source of the request:** supplied by Hongsu Wang, 2026-10-06, as a workbook in the
人物資料標準化 template (`王荊文公年譜_結構化_標準化_20261006_153404.xlsx`, kept outside
this repo). The template is a standing format: one sheet per CBDB table, every row
already coded and compared with CBDB, marked `new` / `update` / `exist`. It was
consolidated from an earlier 汇总本 and checked against a collated copy of the 年譜.

## Batches

| Batch id | Proposals | Outcome |
|---|---|---|
| `2026-10-06-wang-anshi-nianpu-titles` | 2 (`text-codes`) | submitted — **2/2 in production**: 王荊文公年譜 72223, 三經新義 72224, read back |
| `2026-10-06-wang-anshi-nianpu` | 78 | reviewed 2026-10-07 (74 to send, 4 held out); submitted — 1 landed, then a 500 at proposal 2 |
| `2026-10-07-wang-anshi-nianpu-resume` | 77 | submitted — 37 landed, then a refused connection at `post-052` |
| `2026-10-07-wang-anshi-nianpu-resume-2` | 40 | submitted — **36/36 landed**; the main batch is complete, **74/74** |
| `2026-10-07-wang-anshi-nianpu-inst-alias` | 1 | submitted — **1/1 landed**: alias 報寧寺 on institution 945 (operation 371832); later removed by the user, see below |
| `2026-10-07-wang-anshi-nianpu-inst-redo` | 2 | submitted — **2/2**: 王安石's BIOG_INST_DATA row to 945 deleted (371873); institution **4012** 報寧寺 created, name code 2640 (371875) |
| `2026-10-07-wang-anshi-nianpu-inst-baoning` | 2 | submitted — **2/2**: 4012 filled (start 1084, address row sourced) (371877); 王安石 → 4012 as 捐贈者 (371879) |

What the 76 proposals of the main batch are (the two titles went first):

| Resource | create | update |
|---|---|---|
| `basicinformation` | 3 | 2 |
| `sources` | 5 | |
| `postings` | 25 | 6 |
| `associations` | 21 | 3 |
| `addresses` | 3 | |
| `altnames` | 3 | |
| `kinship` | 2 | |
| `entries` | | 1 |
| `texts` | 1 | |
| `social_institutions` | 1 | |

## State — complete

All 74 proposals the review kept are in production, over three runs; the 4 the review
held out (`post-004`, `post-046`, `post-050`, `post-051`) were not sent, and two of
them became updates of existing postings instead (28196's end year 1074 → 1076;
105221's notes). The three new people are **黃某 705348**, **王氏(王安石女) 705349** and
**沈某 705350**. Every landed row's returned `result.row` was compared with what was
sent — 74 rows, no difference — and the mirrors were read back through
`/cbdbapi/person`: the new people's reverse kinship and association rows exist, and
the three association year updates reached their reverse rows.

Both interruptions were reconciled before resuming (`reconciliation.json` in each
processed directory): the first was an upstream defect — a `sources` create with no
`c_pages` key 500s (`BiogSourceRepository::buildCreatePayload` reads an undefined
array key); the client now sends `c_pages: null`, which lands as the documented
empty page. The second was the known refused-connection drop on sustained writes
(AGENTS.md rule 2). Neither wrote anything.

The workbook's temporary ids were written back on 2026-10-07: TMP-001/002/003 became
705348/705349/705350 in every data sheet, with their 人物ID狀態 set to 已對應 (the
修正記錄 sheet, a history of the consolidation, was left as it was).

**報寧寺 is institution 4012, not 945 (2026-10-07).** The temple was first anchored to
945 半山寺 and given the alias 報寧寺 there, through a write path the user had opened
upstream for it (cbdb-online-main-server #1335; batch `…-inst-alias`). The user then
judged 945 the wrong anchor: its only address row is 7537 荊溪, with notes describing
another temple. And the 年譜 itself names the temple only as 報寧 (「有旨賜名報寧」):
「半山」 came from the workbook coder's note and 945's own notes (27842《中国の寺院》),
not from the source. So, decided with the user:

* the alias on 945 was removed (by the user, directly — no operation row, see docs/02),
  and 王安石's BIOG_INST_DATA row to 945 deleted; 945 is otherwise as before;
* a new institution **4012 報寧寺** (name code 2640), type 2, dynasty 宋, start 1084
  (元豐七年), source 72223 — no 半山寺 alias, since the source does not give it;
* its address **上元 12829**, the row citing 27842 p. 194 with a note that the place
  comes from Temple ID 138321 「上元縣東北」 — the source says only that 王安石 lived at
  鍾山 and gave his house as a temple;
* 王安石 → 4012 as 捐贈者 (role 6), the deleted row's content unchanged.

All read back. Both new names carry the server-derived pinyin `bao ning si`
(lowercase), unlike CBDB's `Banshan Si` style; left as is. 945's own problems (the
荊溪 address row, `c_inst_floruit_dy` 20) are untouched.

## Decisions taken before staging (the user, 2026-10-06)

* **The source book is created.** 《王荊文公年譜》 was not in `TEXT_CODES` (live
  search, four spellings). AGENTS.md rule 12: the user approved it, and it was
  created on its own first — **72223** — so the main batch cites it directly.
* **《三經新義》 is created too** — **72224** — for the one 著述 row that names it.
  CBDB has the three works separately (e.g. 3994 周官新義).
* **Existing CBDB values are kept.** An `update` fills only fields CBDB has empty.
  Every field it writes was read live during `validate`'s preview and is currently
  empty, 0 or −1. Where the workbook differs from an existing row (mostly
  appointment type: the workbook's 改/除/授 against CBDB's 正授), the difference is
  listed in the batch notes and nothing is changed.

## What the batch does not carry

Read off the generator's findings (`batch_notes` in the staging file):

* 13 association rows lose their 文類 code: `ASSOC_DATA.c_litgenre_code` is not
  in the API's associations whitelist.
* The year on two 著述 rows: `BIOG_TEXT_DATA`'s year columns are not accepted by
  the API either. For 字說 (8417) that year was the whole of the update.
* 官名-002 (簽書淮南節度判官廳公事): the workbook identifies it with posting 79765
  and lists nothing to add, so nothing is proposed.
* 13 postings that differ from CBDB (9 `exist`, 4 also `update`), kept as CBDB has
  them: appointment type in 11, years in 官名-025 and 官名-033, a month in
  官名-040.

## Code

`python -m cbdb_agent from-workbook` (`src/cbdb_agent/person_workbook.py`) reads the
template. It was written for this case and is not specific to it. This case's facts
are in `workbook.yaml`: the batch id, the two titles to create, and the existing row
each `update` targets (keys from the SQLite snapshot, confirmed live by the preview).

    python -m cbdb_agent from-workbook --xlsx <workbook> --case cases/wang-anshi-nianpu/workbook.yaml
    python -m cbdb_agent validate --staging data/staging/2026-10-06-wang-anshi-nianpu/proposal.yaml

The two titles were sent as their own batch, through `submit`, with the
production gate opened for that one run and closed again; both rows were read back
through `GET /api/v2/texts`. `workbook.yaml` now names 72223 and 72224, so a
regeneration cites them instead of creating them a second time.

Tool changes that came with it. `{"ref": ...}` now also covers `c_source`,
`texts`/`sources`.`c_textid` (→ a `text-codes` create) and `kinship.c_kin_id` /
`associations.c_assoc_id` (→ a new person's allocated `c_personid`). `TEXT_CODES`
creates also get a live duplicate check at submit time, as `ADDR_CODES` and `office`
already had. And `python -m cbdb_agent check-mirrors` reads the reverse row of
every kinship/association update the way the server locates it, and records whether
writing it is safe; all three association updates here came out safe. See
`docs/02-review-log.md`, 2026-10-06.
