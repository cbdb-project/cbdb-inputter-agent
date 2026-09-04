# Code Labels and the Weekly SQLite Snapshot — Design

Status: implemented (`src/cbdb_agent/snapshot.py`, `src/cbdb_agent/code_lookup.py`;
`validate --staging` emits the labels into `review.json`). Written 2026-08-19, after
the first real review session showed that a page full of correct numbers is not a
reviewable artifact.

## 1. Problem

`docs/08` gave the reviewer a dense, filterable, bulk-resolvable view of a batch. It
still showed this:

```
c_office_id   63057
c_addr_id     18444
c_source      27144
c_dy          18
```

Nothing there can be checked. `63057` is either the right office or the wrong one and
the page offers no way to tell; a reviewer either trusts the agent's arithmetic or
opens the CBDB site in another tab and types the number in, 200 times. Worse, the
decisions the batch actually asks for are *between* codes — "is 曲阜學正 code `64674`
州學正, `63343` 學正, or `64682` 孔廟學正?" — and a chooser showing three bare integers
is asking the reviewer to decide nothing.

Requested explicitly: every code shows its name; an office additionally shows its
`OFFICE_TYPE_TREE` position and its `c_dy` with the dynasty's Chinese name; an
address shows the **whole** parent chain joined out of `ADDR_BELONGS_DATA` plus the
leaf's own `c_firstyear`/`c_lastyear`; a source shows the book title. All read-only —
these are what a code *means*, not values to edit.

## 2. Where the resolution happens

**In Python, at export time.** Not in the page:

- The page is opened from `file://`. A browser there cannot call the CBDB endpoints
  (cross-origin), and `API.md` §1.1 notes browsers can't send Bearer credentials
  cross-origin at all.
- The page is deliberately network-free (`docs/08` §2) because it is used offline on
  unpublished data.

So `validate --staging` resolves every code and bakes the result into `review.json`
as a flat index, alongside the field→table mapping the page needs to use it:

```json
"code_labels": {
  "office:63057": {
    "label": "縣尉",
    "sub": "xian wei · District Defender",
    "lines": ["朝代 c_dy=18（元）",
              "類型 0 所有門類 › 18 元朝 › 1810 地方官類(府州縣官) › 181003 諸縣門"]
  },
  "addr:18444": {
    "label": "福清州",
    "sub": "Fuqing Zhou · Zhou · 1280~1367",
    "lines": ["隸屬：16776 元朝（隸屬 1280~1367） › 18233 江浙行中書省 › 18434 福州路"]
  }
},
"field_code_tables": {"c_office_id": "office", "c_addr_id": "addr", "c_addr": "addr", …},
"code_table_names":  {"office": "OFFICE_CODES", "addr": "ADDR_CODES", …}
```

One index keyed `"<table>:<value>"`, not a label per field occurrence: the same
address appears in a dozen proposals and there is no reason to resolve or ship it
twelve times.

## 3. Two sources, and why the snapshot is the better one

### 3.1 The weekly SQLite snapshot (preferred)

CBDB publishes a full SQLite build weekly at
<https://huggingface.co/datasets/cbdb/cbdb-sqlite> — `latest.zip`, ~132 MB
compressed, ~557 MB extracted, with a sidecar JSON carrying `generated_at_utc` and a
`sha256` that `snapshot.py` verifies before accepting the file.

It is better than the API here for a specific reason, not a general one: **the two
things the reviewer most needs are hierarchy joins, and the API cannot do a join.**

- An address's parent chain lives in `ADDR_BELONGS_DATA`. The API exposes it only as
  a fragment embedded in `/api/select/search/addr`'s `text` field — exactly one level
  per response — so a 4-deep chain is 4 HTTP requests, and 25 addresses is ~100.
  In SQL it is one table loaded once.
- An office's type position needs `OFFICE_CODE_TYPE_REL` → `OFFICE_TYPE_TREE`. Over
  HTTP that requires two **undocumented** legacy endpoints (`API.md` §14 lists them
  as "still present, not documented here") which ignore their parameters and dump
  44k and 2.7k rows. Depending on those for a core feature is not a good position;
  with the snapshot they are never called.

Location: `data/cbdb-sqlite/` **inside the repo, gitignored**. An earlier draft used
`%LOCALAPPDATA%`, which keeps a synced working tree small but leaves half a gigabyte
somewhere the user never looks, with nothing in the project pointing at it and no
obvious way to clean it up. In-repo-and-ignored is visible and is deleted by deleting
the folder. `CBDB_SQLITE_DIR` overrides it — worth using if the checkout is inside
OneDrive/Dropbox, so the weekly rebuild isn't re-uploaded.

It is opened `mode=ro`. Nothing here has any business writing to a shared local
mirror of CBDB, and read-only means a stray write is an error rather than a silent
divergence from what everyone else's copy says.

### 3.2 The public lookup endpoints (fallback)

Same label shapes, less detail, no download. Used when there is no snapshot and
`CBDB_SQLITE_AUTODOWNLOAD=false`, or when the download failed. The address chain is
walked a level per request; the office type tree uses the two legacy endpoints above
and silently yields nothing if they have disappeared.

Both sources sit behind one `_row(table, id)` / `addr_chain(id)` /
`office_type_chains(id)` seam, so the label-shaping code is written once.

## 4. The line that matters: what a snapshot may not decide

**The snapshot is a weekly build of a database that is written to continuously —
including by this agent.** It answers "what does this code mean". It must never
answer "what is currently true of this record".

Never use it for `max(c_personid)` or ID allocation; never for a "does this row
already exist" check before a create; never for the current-value diff. A row created
since the build is invisible in it, so a duplicate check against it can return "not
there" for something that is — which is precisely how you create the duplicate you
were checking for. Those stay on the live API, and being slower is the price of being
right. This is a hard rule in `AGENTS.md`, not a preference.

`snapshot_is_stale()` (>14 days) exists so the CLI can tell the user how old the build
is rather than quietly trusting it; `validate` prints the age on every run.

## 5. Behaviour in the page

- Every code-bearing field renders a read-only block under the editable value: name,
  romanization/English, then the extra lines (dynasty + type tree for an office, the
  belongs-chain for an address). It follows the **edited** value, so retyping a code
  immediately shows the new code's meaning — which is the point of having it on
  screen while editing.
- `target_pk` values get the same treatment; an `altnames` row keys on
  `c_alt_name_type_code` and an `addresses` row on `c_addr_id`, and those are codes
  like any other.
- Conflict option chips carry the name inline, and the rationale list shows the full
  detail — this is where the reviewer is actually choosing between codes.
- List-valued fields (`postings.c_addr`) get one block per element.
- Nothing is editable. A code's meaning is not a value that could be saved, so
  rendering it in an input would be a lie about what the page can do.

Two failure modes are distinguished, because they call for different actions:

| what the reviewer sees | means |
|---|---|
| `63057 — no match in OFFICE_CODES` | the export *did* resolve codes; this one isn't in that table |
| `63057 — not resolved (export ran without a snapshot or network)` | no lookup ran at all; re-run `validate --staging` |

The first is informative rather than an error in at least one real case: a conflict
may deliberately offer an option from a *different* table — 「鄉貢進士」 is
`ENTRY_CODES` 39 as an entry path but `STATUS_CODES` 136 as a designation — and
naming the table makes that obvious instead of looking like a broken code.

Values that are not codes at all are never looked up: conflict options share a field
with real codes but often hold a decision (`"defer"`, `"new_office_code"`, `"both"`).
Every CBDB code is an integer, so a non-integer option is skipped rather than being
reported as unresolvable.

## 6. Best-effort, always

`validate --staging` must keep working with no network, no `.env` **and** no snapshot
(`docs/06` Tier 1). So every failure in this path degrades to "no label for that
code" and never raises: a missing label makes the page less useful, an exception
would make `validate` unusable offline. The CLI prints which source it used and how
old the snapshot is; the page shows a `code names unavailable` badge when the export
carried none.
