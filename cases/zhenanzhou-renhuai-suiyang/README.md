# 真安州 / 綏陽 / 仁懷 under 遵義軍民府

Enter three Ming administrative units — 真安州 and the two counties it governed,
綏陽 and 仁懷 — into CBDB as place names, each with a coordinate and a hierarchy
edge, for the period 1601–1643.

**Source of the request:** Ning Hao, 2026-09-18. A one-line proposal citing
《中國行政區劃通史·明代卷》「（六）遵義軍民府」, with CHGIS coordinate extracts for the
three units. Neither the passage nor the extracts are in this repo — they are
unpublished source material, and the batch's `source_excerpt` summarises what they
say rather than reproducing them.

No `case.py`: three places and three edges are hand-written YAML, per AGENTS.md
("Most cases have no code at all").

## Batches

| Batch id | Proposals | Outcome |
|---|---|---|
| `2026-09-21-zhenanzhou-renhuai-suiyang` | 6 (3 `addr-codes`, 3 `addr-belongs-data`) | **complete** — 6/6 in production |

## What landed

Submitted to production 2026-09-21 07:01 UTC, 6/6 in one run, no interruption.

| Row | `c_addr_id` | Parent | `operations` |
|---|---|---|---|
| 真安州 `Zhen'an Zhou`, 1601–1643, Zhou | **702773** | 702684 遵義 | 366683, edge 366686 |
| 綏陽 `Suiyang`, 1601–1643, Xian | **702774** | 702773 真安州 | 366684, edge 366687 |
| 仁懷 `Renhuai`, 1601–1643, Xian | **702775** | 702773 真安州 | 366685, edge 366688 |

Verified independently afterwards, not inferred from the `200 ok:true` (AGENTS.md
rule 11). All three `ADDR_CODES` rows read back live through
`GET /api/select/search/addr` with the expected name, period, category and
coordinates. The three `ADDR_BELONGS_DATA` edges have no read surface at all, so
their confirmation is two-sided: the create responses' `result.pk`/`result.row`, and
the parent that `/api/select/search/addr` embeds for each child row — which renders
from `ADDR_BELONGS_DATA` and reads 702774 → 702773, 702775 → 702773, 702773 → 702684.
`GET /api/v2/operations` shows all six rows under the token's user.

Empty fields landed as NULL, confirmed in the server's own echo of each row:
`c_alt_names` on all three places, `CHGIS_PT_ID` on 真安州, `c_pages` on all three
edges. No empty strings anywhere.

**Still owed on the server:** `php artisan cbdb:regenerate-addresses-table`.
`ADDRESSES` is a derived cache, so until it is rebuilt these three places stay
invisible to posting autofill and dynasty homonym disambiguation.

## What it writes

Three `ADDR_CODES` rows, all 1601–1643, and the three `ADDR_BELONGS_DATA` edges that
place them: 真安州 (`c_admin_cat_code` 215, Zhou) directly under the existing Ming
遵義 row, and 綏陽 / 仁懷 (both `c_admin_cat_code` 21, Xian) under 真安州. Both
categories already exist, so — unlike the salt import — this batch creates no
`ADMIN_CAT_CODES` row, and touches only the two tables it has to.

All six proposals are global reference data (AGENTS.md rule 12). The three edge keys
are write-once: `ADDR_BELONGS_DATA`'s update whitelist is `c_source`/`c_pages`/`c_notes`
only, so a wrong parent or a wrong year in the four-column key is permanent.

## Duplicate check

Run live against `/api/select/search/addr` at 2026-09-21T05:19:57Z, matching
`c_name_chn` exactly and counting only rows whose period overlaps 1601–1643: no
existing row for 真安州, 綏陽, 仁懷, and none under the 縣-suffixed spellings either.
The parent, `ADDR_CODES` 702684 遵義 1378–1643, was confirmed live in the same pass.
The weekly snapshot agreed, but per AGENTS.md it is not allowed to answer this
question — a row added since its build date is invisible in it.

## Decisions

Recorded in full in the batch's `batch_notes`. In brief:

1. **真安州's seat — one row, at the 1601 point.** Decided by the user 2026-09-21
   (conflict `zhenanzhou-which-seat`, resolved as 107.68781). CHGIS gives three
   successive seat points and two fall inside 1601–1643, while `ADDR_CODES` holds one
   coordinate per row. The row takes the 1601 point: 《通史》 records exactly one Ming
   seat for 真安州 and that is the one matching it. The alternative shape — two rows
   split at 1619/1620, as `docs/11` §5.1 did for the salt 分司 that moved — was not
   taken; it would have doubled the write-once edge count from 3 to 6.
2. **縣 bare, 州 suffixed.** The request writes 綏陽縣 / 仁懷縣; CBDB stores counties
   without the 縣 (15010 rows vs 909; 1086 vs 112 within the Ming window) and 州 with
   it (3734 vs 22), so the counties go in as 綏陽 / 仁懷, spelled to match their own
   Qing successors. Romanization follows the same table, hence `Zhen'an Zhou`.
3. **`CHGIS_PT_ID` set on the counties, null on 真安州** — a single 1601–1643 row for
   真安州 is not any one of the three CHGIS points, it spans two.
4. **Coordinate precision** taken from the full-precision value CBDB already stores
   for the identical CHGIS point, where one exists; it agrees with the supplied
   extract at 5 decimal places and makes each Ming row geometrically identical to its
   Qing successor.
5. **Empty fields are NULL, not `""`.** `c_alt_names` on all three places,
   `CHGIS_PT_ID` on 真安州 and `c_pages` on all three edges carry `null`. That is also
   what would be written whatever we sent: the target system converts `""` to NULL on
   every request (`docs/07` §1.5), and the two integer columns here hold no `''` at
   all in the live table.

## The finding, and how it closed

《中國行政區劃通史·明代卷》 had **no `TEXT_CODES` row** when the batch was drafted, though
the 唐代, 宋代 and 遼金 volumes did (40304, 68950, 68967 — and 40304 is the most-used
source on `ADDR_BELONGS_DATA`). Creating it is a code-table write, so under rule 12 it
was reported rather than done, and the three edges were drafted with `c_source: 0`, the
documented unknown sentinel.

The user created the row on 2026-09-21 as `c_textid` **72219**, verified live via
`GET /api/v2/texts/72219` — 中國行政區劃通史(明代卷), `c_text_type_id` 0201, the same type
as the other volumes. All three edges now carry it. The review page shows the id
unlabelled, because label resolution reads the weekly snapshot and that build predates
the row by five weeks; the title was confirmed against the live endpoint instead.

Two pre-existing oddities in the parent chain are noted in `batch_notes` and left
alone, since changing either would itself be a rule-12 write: CBDB has no
遵義軍民府 row (702684 is plain 遵義 spanning 1378–1643, romanized `Zhunyi`), and its
own edge puts it under 貴州布政司 for the whole Ming, where the source has 遵義 under
四川布政司 from 洪武二十七年.

## Review

`review/zhenanzhou-renhuai-suiyang/` — which surface to open. The batch loads into
`review/batch.html` from
`data/staging/2026-09-21-zhenanzhou-renhuai-suiyang/review.json` (gitignored).
