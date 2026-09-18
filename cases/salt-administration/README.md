# Ming/Qing salt administration

Enter the six Ming and Qing 都轉運鹽使司 and their 分司 into CBDB as **place names**,
with each unit's coordinate copied from its 治所 and its parent recorded as a
hierarchy edge.

**Source of the request:** Ning Hao's compilation,
`明清六個鹽運使司情況（完整版）.xlsx`. The spreadsheet is not in this repo — it is
unpublished source material, and `case.py` names the sheets it reads rather than
carrying the data.

**Design:** `docs/11-salt-administration-design.md`.

## Batches

| Batch id | Proposals | Outcome |
|---|---|---|
| `2026-09-10-salt-administration` | 49 (`office` / create) | **dropped**, never submitted |
| `2026-09-18-salt-addresses` | 118 | 11 success, 1 failed, 106 not attempted |

### Why the first batch was dropped

Track A entered each unit as an `OFFICE_CODES` row. On 2026-09-11 that was abandoned:
a separate import had already covered the salt-administration **post titles**, and
Ning Hao's list names the **institutions**, which belong in the place-name tables —
which was his proposal to begin with. `emit_offices.py` still exists and its `main()`
refuses to run, so the reasoning is readable and the batch is not re-creatable by
accident.

### Where the second batch stands

2 `ADMIN_CAT_CODES` + 57 `ADDR_CODES` + 59 `ADDR_BELONGS_DATA`, submitted to
production on 2026-09-18. It stopped at proposal 12 on
`SSLError(SSLEOFError(8, '[SSL: UNEXPECTED_EOF_WHILE_READING]'))` — an **indeterminate
write**, which per AGENTS.md rule 11 is not retried and stops the whole batch.

Reconciled two ways afterwards (`GET /api/v2/operations`, and
`GET /api/select/search/addr` for the one uncertain row). What landed:

- `ADMIN_CAT_CODES` **226** 都轉運鹽使司 and **227** 分司
- `ADDR_CODES` **702716–702724** — nine 明代 兩淮/兩浙 rows
- no `ADDR_BELONGS_DATA` edges at all
- the 12th proposal (`長蘆都轉運鹽使司`) did **not** land: no `operations` row, and an
  exact-name search returns zero

So **11 of 118 rows are in production** and 107 are not. The production write gate is
re-locked (`CBDB_DRY_RUN=true`, `CBDB_CONFIRM_PROD` empty).

### What is still owed

1. A resume path for the remaining 107, driven by
   `data/processed/2026-09-18-salt-addresses/results.json`'s `proposal_id → pk`
   mapping. `emit_addresses.main` refuses to emit if `live_state.find_existing_addresses`
   reports *any* name already present — correct as a default, but it is also what
   blocks a re-run, so resuming cannot go through that check unchanged.
2. After the rest land: `php artisan cbdb:regenerate-addresses-table` on the server,
   or the new places stay invisible to posting autofill and dynasty homonym
   disambiguation.

## Code

`case.py` — this job's content: the source note and id, the two dynasty windows, the
seat name variants and disambiguation boxes, the settled seat decisions, the blocked
units, the successions, the naming/translation/romanization conventions, and the
reader for this particular spreadsheet.

The method it plugs into is `src/cbdb_agent/places_and_offices/`, which knows nothing
about salt. Run it from the repo root:

```
python -m cbdb_agent.places_and_offices.build_dataset \
    --case salt-administration --xlsx <path to the xlsx>
python -m cbdb_agent.places_and_offices.emit_addresses \
    --case salt-administration --batch-id <batch id>
```

The first writes `data/build/salt-administration/` (gitignored); review it at
`review/dataset.html?case=salt-administration`. The second writes
`data/staging/<batch-id>/proposal.yaml`.
