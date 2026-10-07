# 12 — The `social-institution` aggregate, and institution aliases

**Status:** modelled 2026-10-07 — update, then create and declared alias removal the
same day. Uses: the alias 報寧寺 on institution 945 半山寺, then its reversal and a new
institution 報寧寺 (`cases/wang-anshi-nianpu/`).

## 1. Why this exists

CBDB keeps a social institution's other names in `SOCIAL_INSTITUTION_ALTNAME_DATA`.
Until 2026-10-07 nothing upstream wrote that table — no handler, no code-table
registry entry, no edit page (see docs/02, 2026-10-07: the 報寧 alias "has no write
path"). The user chose to open one upstream; it shipped as an optional `alt_names`
list on the `social-institution` entity aggregate (cbdb-online-main-server #1335,
merged as `82118559` + `df1495a7` and deployed the same day). `API.md` §13.4 is the
contract; `docs/07-api-md-digest.md` §2.3 the digest.

So the only way in is the aggregate `update`, and that is a full-row overwrite. The
client therefore had to model the aggregate, not just "an alias".

## 2. What is modelled (`models.py`)

`RESOURCE_SPECS["social_institution_aggregate"]`, alias **`social-institution`**
only, **create and update**, no delete.

| | |
|---|---|
| resource string | `social-institution`. Not `social_institution` / `social_institutions` / `socialinst` (those are the PERSON sub-resource `BIOG_INST_DATA`), and not the server's other aggregate spellings (`social-institutions`, `social-institution-load`, `socialinst-load`) — one way to say it |
| PK | `c_inst_code` (server-assigned on create, so staging requires it present on update and never invented) |
| `person_id` | `0` — belongs to no person, like the code tables |
| fields | `name`, `type_code`, `dynasty_code`, `source_id`, `pages`, `notes`, `begin_year`, `by_nianhao_code`, `by_nianhao_year`, `by_year_range`, `floruit_dy`, `first_known_year`, `end_year`, `ey_nianhao_code`, `ey_nianhao_year`, `ey_year_range`, `end_dy`, `last_known_year`, `addresses`, `alt_names` |
| required | `name`, `type_code`, `dynasty_code`, `source_id`, `addresses` (≥ 1 row) |
| full overwrite | every field must be present (value or explicit `null`) **except `alt_names`** — `overwrite_exempt_fields` |
| row lists | `addresses` and `alt_names` are `RowListShape`s: every row carries every key, scalar values only, integer columns are ints, no duplicate rows |
| global reference data | yes (AGENTS.md rule 12) |

**Create** takes exactly the six keys `validateCreate()` reads — `name`, `type_code`,
`dynasty_code`, `addr_id`, `source_id`, `alt_names` — because the server silently
ignores every other key on create (start year, notes, the address row's source…).
Those are filled by an update afterwards, built from a live read of the new row. The
address row create writes is type 1 at `addr_id`, coordinates 0, with the
institution's `source_id`. Before the request, `assert_institution_create_is_not_a_duplicate`
asks live whether an institution of the same name already has that address (same
name elsewhere is a homonym — 保寧寺 is two temples, 56 and 57). The response's
`result.pk` has two keys, `c_inst_code` and `c_inst_name_code`.

The spec key `social_institution_aggregate` is not a server spelling, so
`mutation_api.default_alias()` sends the resource's only alias when a caller names
none.

Delete is not modelled.

## 3. The traps, and what answers each

1. **The update is a full-row overwrite.** Any `SOCIAL_INSTITUTION_CODES` field left
   out is written as `NULL`. → `full_overwrite_update`, and the payload is built from a
   live read (§4), not typed by hand.
2. **The addresses are reconciled, and each matched row rewritten whole.** Key
   `(addr_id, addr_type_code, xcoord, ycoord)`; begin/end year, source, pages and notes
   come from the request. An address missing from the list is deleted. →
   `RowListShape` requires every key on every row; the read refuses rather than return
   a partial list (§4).
3. **`alt_names` deletes whatever it does not list, and the aliases cannot be read.**
   No `/api/v2/get`, no lookup. → absent = untouched (the safe default), and when it
   is present the submit-time guard (§5) refuses any list that would drop an alias.
4. **Inside an alias row, absence is not "unchanged".** An absent `type_code` means
   `0`, so an existing row of type `null` would be deleted and re-added; an absent
   `notes` on a matched row is written `NULL`. `pinyin: null` is the exception that
   keeps the existing reading (or derives one for a new alias). → every key required.
5. **`floruit_dy: null` is not stored as null.** `codeColumns()` writes
   `floruit_dy ?? dynasty_code`, so carrying a current `null` across changes it to the
   dynasty code — 1028 of 4012 institutions (2026-10-03 snapshot) have it NULL. →
   `null_falls_back_to`: a `null` there is refused; send the dynasty code explicitly
   if that change is acceptable, where the reviewer can see it.
5a. **Re-sending the name can rename.** The server resolves a name to the *smallest*
   name code carrying it; if a lower code holds the same literal, the update moves the
   institution, its addresses and its aliases to that code. → `read_institution`
   refuses when the name does not resolve back to the institution's own code.
6. **Variant replacement.** `notes`/`pages` (institution and address rows) and alias
   names go through lenient `char_variant_map` replacement on every write, so resending
   an unchanged row can rewrite it. For 945 this was checked first, read-only, on the
   server (none of the characters is in the 22-row map); in general, read the response
   `notices`.
7. **`social-institution` vs `social_institution`.** One separator: an aggregate over
   four global tables vs one person's institution row. Only the hyphenated spelling is
   registered here, and `tests/test_social_institution_aggregate.py` pins that the
   underscore spellings still resolve to the person spec.

## 4. Reading the current state (`social_institution_aggregate.py`)

`read_institution(client, inst_code)` returns the institution as update `changes`,
from the public lookups AGENTS.md rule 1 allows:

* `GET /api/select/search/socialinstcode?q=<code>` — the raw `SOCIAL_INSTITUTION_CODES` row;
* `GET /api/select/search/socialinst?q=<name code>` — its name;
* `GET /api/select/search/socialinstaddr?q=<code>` — the raw address rows.

Each is a `LIKE %q%` search, paginated at 20 with no `ORDER BY`: every page is read,
results are filtered to the exact code (945 also matches 1945 and 9450), and the
distinct-row count is checked against the paginator's `total`. A short count, a
missing column, no row, or no address is an error, never a shorter answer — the
update built from it would delete what the read missed. A small code is a wide search
(`q=1` is `LIKE %1%`, about 90 pages) and exceeds the 50-page cap: single-digit
institution codes refuse, loudly. Each `alt_names` proposal also walks the operations
log back to the snapshot twice (preview, then submit) at the rate limit — minutes as
the build ages, and per proposal.

`read_alt_names(client, snapshot, inst_code)` composes the aliases the way
`places_and_offices.live_state` composes `ADMIN_CAT_CODES`: the snapshot's rows at its
build date, with every `SOCIAL_INSTITUTION_ALTNAME_DATA` operation since **replayed**
onto them (§7 has the rules). Every alias operation's `resource_data` carries
`c_inst_code`; one that does not, or that does not apply exactly, is a refusal.
**The boundary is a date**: the snapshot's `generated_at_utc` is truncated to its day,
and an operation on that same day may or may not be in the build (both are UTC —
`updated_at` arrives as ISO-8601 `Z` — but the build's time of day is dropped). So an alias
operation for the institution dated on the build day is a refusal too — wait for
the next build. Operations on other institutions that day are irrelevant.
A write made directly in the database leaves no operation, and the replay cannot see
it until the next weekly build — this happened on 2026-10-07 (docs/02).

`batch_runner.fetch_current_values()` uses both for the preview's current-vs-proposed
diff (the aliases only when the proposal sends `alt_names`; an unreadable alias list
fails the whole fetch, so "could not read" never renders as "none").

## 5. The submit-time guard

`assert_alt_names_update_deletes_nothing()` runs in `MutationApi.update()` immediately
before the request, when `alt_names` is sent and not under dry-run. It re-reads the
aliases (§4, local snapshot only, never a download) and refuses if the list would
delete any existing alias with a name. The review sits between staging and submit and
is meant to take time; anything written in that window would otherwise be deleted
silently. An undeclared drop is always a stale list; a deliberate one is declared (§7).

**The window it cannot close.** The guard reads before the request; the server
locks and reads the aliases only when it reconciles. An alias someone else adds in
between would be deleted, and upstream has no precondition (no compare-and-swap on
the alias set) to turn that into a conflict. So the client checks afterwards too:
`assert_alt_names_write_deleted_nothing()` requires the response's
`alt_names_removed` to equal the declared removals (0 unless §7 says otherwise) and otherwise
raises `AliasesDeletedError`, which is `indeterminate`, so `batch_runner` stops the
batch. The deleted rows are in `GET /api/v2/operations` (`SOCIAL_INSTITUTION_ALTNAME_DATA`,
op_type 4, `resource_data` = the row); restoring means re-sending the complete list.
Detection, not prevention: closing the window needs an upstream precondition, worth
adding if alias writes ever become frequent. With declared removals the check is a
count, not a set: if, in that window, someone else deletes the declared alias and adds
another, the server deletes the newcomer and the count still matches. Rare (two
writers on one institution's aliases within seconds) and recoverable from the log, but
not detected. Today the table has had no write at all
before 2026-10-07, and the window is the length of one request.

What neither check can see: a write made directly against the database.

## 6. Building a batch

```python
changes = read_institution(client, 945)          # every field, as it is now
changes["alt_names"] = [{"type_code": 0, "name": "報寧寺", "pinyin": None,
                         "source_id": 72223, "pages": None, "notes": None}]
```

then `validate --staging` (the preview shows every field current = proposed except
`alt_names`), and `submit`. After a real write, `result.row.alt_names` is the only
read-back the server gives — keep it (rule 11).

## 7. Removing an alias (added 2026-10-07)

Removal has to be said. A proposal lists each alias it deletes in
`alt_names_removed` (`[{type_code, name}]`), a **client-only** key: validated like a
row list, only allowed alongside `alt_names`, and stripped before the request. The
pre-write guard then requires the deletions the update would make to equal that list
exactly (nothing undeclared, nothing declared that is not there or is still listed),
and the post-write check requires `result.alt_names_removed` to equal its length.

To know what is there, the alias read now **replays** the operations log onto the
snapshot instead of refusing on any change (§4 was the first version): insert (op 1)
adds the row in `resource_data`, delete (op 4) removes the row in `resource_data`, update
(op 3) replaces `resource_original` by `resource_data`. An operation that does not fit
exactly — a delete of a missing row, an insert of an existing one, any other op type,
one with no `c_inst_code` — is a refusal. Keys compare as (type as text, name with
trailing spaces stripped), the way the server does.
