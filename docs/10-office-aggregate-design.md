# Adding a Tang office code `知某州事` — design

**Status: implemented and submitted.** The `office` spec, `preflight.py` and this
design's §5.4 batch all landed together; the write went to production on 2026-09-04
(`operation_id 360887`) and is recorded in §8 and `docs/02-review-log.md`. The
sections below are kept as written because most of their value is the eight traps in
§2 and the reasoning behind the data decision in §5.1, not the payload — but read
§4's table as *what was done*, not as a proposal.

The request (2026-09-04): record a Tang office `知某州事`, English
`Administrator of Prefectural Civil Affairs`, alias `摄某州事`, attested in
《唐會要》卷六八《刺史上》.

**Decided by the user, 2026-09-04: plan B — edit the existing row `12304 知州事` in
place rather than creating a new office code.** §5.1 records why that is the right call
and what it costs. The rest of this document specifies B.

Two layers, separable:

1. **§1–§4 — the write path.** `office` is an *entity aggregate* (`API.md` §13.4) and
   this client does not model it. `AGENTS.md` rule 12 already names it approval-gated
   and unmodelled, so today a staging file saying `resource: office` is rejected as an
   unknown alias — a safe outcome by absence, not by design. B needs the aggregate's
   **`update`** modelled, which is the harder half (full-overwrite semantics; trap 4).
2. **§5 — the data.** All ten writable `OFFICE_CODES` columns have to be specified,
   because the aggregate `update` overwrites the whole row. Every data decision is now
   settled (§7); the batch is blocked only on `approved_by`.

Cited throughout: upstream `API.md` at `origin/develop` **`b2df35f5`**, read directly
rather than through the digest — which was stamped `fd747aba` when this design was
written and has since been re-synced to the same commit (§8). Plus source reading of
`OfficeAggregateDefinition.php`, `ResolvesOfficeAggregateInput.php`,
`OfficeImportService.php`, `AbstractEntityAggregateHandler.php` and
`Api/MutationController.php`, the production schema in
`database/migrations/2025_01_01_000000_import_cbdb_schema.php`, and the 2026-08-15
weekly SQLite snapshot for reference data.

---

## 1. What an office write actually touches, and how reversible it is

One office = an atomic write to **two** tables (`OfficeImportService`):

| Table | Rows |
|---|---|
| `OFFICE_CODES` | one row. Writable columns: `c_dy`, `c_office_chn`, `c_office_chn_alt`, `c_office_trans`, `c_office_trans_alt`, `c_office_pinyin`, `c_office_pinyin_alt`, `c_source`, `c_pages`, `c_notes` (10) |
| `OFFICE_CODE_TYPE_REL` | **one row per type node.** On `update` this is a *set reconciliation* (`reconcileRowSet`): only the difference is added/removed, and the response reports `types_added`/`types_removed` |

Column types worth knowing before writing (production schema, not the SQLite build):
`c_notes` is **`longtext`**; every other text column, including `c_pages`,
`c_office_chn_alt` and `c_office_pinyin_alt`, is **`varchar(255)`**. The aggregate does
**no length validation at all**, so an overlong value is truncated by MySQL rather than
rejected. That is not theoretical: `950 知某州軍州事`'s `c_office_pinyin_alt` is stored
truncated mid-syllable at exactly 255 characters (`…zhou jiang;j`). Everything §5 sends is
well under, but the check belongs in the pre-flight.

Rule-12 risk profile, and how it differs from `text-codes`:

- `office` **does support `delete`** (§13.4), guarded by `409 c_office_id:
  referenced_by_postings` with a `reference_count`. A *new* office is therefore
  recoverable while nothing references it.
- That guard is **defence in depth, not the last line** — and getting this right took
  two tries. `OfficeImportService::referenceCount()` carries a ⚠ comment saying
  `POSTED_TO_OFFICE_DATA.c_office_id → OFFICE_CODES` is `ON DELETE CASCADE`, so
  bypassing the guard would silently delete other people's posting rows. **That comment
  is stale.** The schema says
  `POSTED_TO_OFFICE_DATA_ibfk_13 … ON DELETE RESTRICT ON UPDATE CASCADE`, and migration
  `2026_07_23_000000_restrict_fks_referencing_small_code_tables.php` (「去級聯 Phase 1」)
  is what flipped it, listing `OFFICE_CODES` explicitly and promising
  「翻轉後任何漏網硬刪一律 fail-closed（1451），零資料損失」. The base schema dump now
  contains no `ON DELETE CASCADE` at all. So a leaked hard delete fails closed with MySQL
  errno 1451 rather than destroying data — better than the comment claims, and worth
  knowing precisely, because "it would cascade" is the kind of belief that makes people
  too frightened to use a legitimate delete and not frightened enough of the real risks.
  General lesson: for schema facts, read the schema; a source comment can go stale.
- **B is not in that category.** `12304` already exists and has a posting. There is no
  "delete and retry" here — the only correction path for a bad update is another
  full-overwrite update. That is exactly why §4 adds a client-side completeness check
  instead of trusting the author to remember all ten columns.

## 2. The server contract for `resource: "office"`, and its eight traps

**Required in `changes`, on `create` *and* `update` alike** (semantic short names; the
`c_*` column names are also accepted): `name`, `type_ids` (array), `source_id`,
`dynasty_code`. **Optional:** `translation`, `name_alt`, `translation_alt`, `pinyin`,
`pinyin_alt`, `pages`, `notes`. `person_id` is required in the envelope; convention for
global reference data is `0` (§13 preamble). `direct` and `proposal` are both supported
here; rule 1 pins us to `direct`.

1. **`offices` (plural) is not this resource — and registering it would break postings.**
   Server-side, `offices` is matched by the *postings* handler first
   (`PostingCreateHandler::supports()`), so a payload saying `offices` writes a person's
   appointment record, not an office code. There is a second, client-side hazard that is
   easy to miss: `models.approval_gated_aliases()` is computed from the gated spec's
   alias sets *and each spec's own `key`*, and consumed by
   `http_client._check_approval()` against the **raw, lower-cased `resource` string**. Putting `offices` (or `office-load`) in the office spec's
   aliases would make *every routine postings write* demand an `approved_by`. Register
   **`office` only**. `docs/04-field-whitelists.md` §11 documents the server-side half of
   this collision; the client-side half is new here.
2. **`target.pk` must exist.** `Api/MutationController` 422s `target.pk: required` unless
   `target.pk` is an array. For B it carries the real id:
   `target: {"pk": {"c_office_id": 12304}}` (`parsePk` accepts `c_office_id` or the
   unprefixed `office_id`; a non-digit or negative value reads as absent → 422, while `0`
   is looked up and 404s).
3. **`create` has no duplicate-name guard.** `create()` allocates `max+1` and inserts;
   two identical names get two ids and nothing in the response hints at it. Not B's
   problem directly, but it is why §5.1's "don't add a fourth Tang code for the same
   office" argument matters, and why §6 keeps the pre-flight search in the procedure.
4. **`update` is full-overwrite, not PATCH.** Every optional field absent from `changes`
   is written as `null`. A payload mentioning only `name` and `translation` silently
   wipes `name_alt`, `pinyin_alt`, `pages` and `notes`. **For B this is the single most
   dangerous property of the operation**, because `12304` already carries a human-authored
   `c_notes` (§5.2). §4's `full_overwrite_update` check exists for exactly this.
5. **`type_ids` is required on `update` too**, and `12304` currently has **zero** type
   rows. So B cannot avoid typing the office — the update *must* name at least one node,
   and `types_added` will report what got attached. This is an unavoidable side effect of
   B, not an optional extra; §5.3 records which nodes were chosen and why.
6. **`/api/v2/get` cannot read an office.** `MutationReadService`'s list is 13 person
   resources plus `nianhao` — 14 entries, none of them an aggregate, so
   `resolve('office')` returns `null`. So rule 11's "read the row back and compare" cannot use
   `/api/v2/get` here, and neither can `batch_runner.fetch_current_values()` — the
   preview for this proposal will show proposed values with no live old-vs-new diff, and
   that is expected, not a bug. The read path is
   `GET /api/select/search/office?q=12304` (public, allowed by rule 1) — see §6.
7. **The text you send is not necessarily the text that lands.** `officeColumns()` runs
   `CharVariantMapService::replaceFor()` over `c_office_chn`, `c_office_chn_alt`,
   `c_notes` and `c_pages` (lenient mode — the full rule set — since none of these are in
   `VariantReplaceScope`'s strict or excluded lists), and `API.md` §4.3 (as of `6de7d34b`)
   adds silent Unicode NFC folding to every text column. Variant replacement *is*
   reported: `AbstractEntityAggregateHandler::envelope()` lifts it into a top-level
   **`notices`** array, and `API.md` now notes `notices` can appear on **failure**
   responses too (409/422). NFC folding is silent. Two consequences: trust `result.row`
   and the read-back over your own payload; and do **not** expect the variant map to do
   simplified→traditional conversion — it is a curated variant list (`峯→峰` class), not a
   簡→繁 converter (§5.2).
8. **`source_id: 0` passes validation.** The check is `ctype_digit((string) $sourceId)`
   + "exists in `TEXT_CODES`", and `TEXT_CODES` has a row `0` = 《未知》/"Weizhi". Legal,
   and sometimes the honest answer — but it must be a *chosen* value in the staging file,
   never a default the agent reaches for to satisfy a required field. Not needed here:
   §5.2 has a real source. (The explicit `(string)` cast is why sending JSON **numbers**
   for `source_id`/`dynasty_code`, as §5.4 does, is safe: PHP's `ctype_digit` on a bare
   `int` would otherwise read values ≤ 255 as a character code. `dynasty_code` is
   compared as `(int)` against `dynastyCodes()`, so it is number-safe too.)

Minor: `dynasty_label` (send `唐`, let the server resolve the code) works, but it goes
through `VariantLabelMap` and on a normalized-key collision keeps the **smallest** `c_dy`.
No reason to take that risk for a value we can state exactly. **Send `dynasty_code`; do
not register `dynasty_label`.**

## 3. Response shape for an office `update`

```json
{ "ok": true, "resource": "office", "mode": "direct", "operation": "update",
  "result": { "pk": {"c_office_id": 12304}, "status": "updated", "operation_id": 12345,
              "types_added": ["06", "06091204", "06091202"], "types_removed": [],
              "row": {"c_office_id": 12304, "c_office_chn": "知某州事",
                      "c_office_pinyin": "zhi mou zhou shi",
                      "type_ids": ["06", "06091204", "06091202"]} },
  "notices": ["…only if a variant character was replaced…"] }
```

`row` is a **partial** echo — four fields. It does **not** confirm `c_office_trans`,
`c_office_chn_alt`, `c_office_trans_alt`, `c_office_pinyin_alt`, `c_source`, `c_pages` or
`c_notes` landed, which for a full-overwrite update is precisely the set you most need to
see. Full verification needs §6's read-back.

Audit trail: `direct` writes one `operations` + `audit_log` row for the `OFFICE_CODES`
update **plus one per type-relation row added or removed**, but the response returns only
the main `operation_id`. B with three type nodes is therefore 4 audit rows, of which we
learn 1 id.

## 4. Changes needed in this repo

Narrow: `office` alias only, semantic field names only, `create` + `update`, no `delete`.

| File | Change |
|---|---|
| `models.py` | `RESOURCE_SPECS["office"]`: `create_aliases={"office"}` (see the revision note below), `update_aliases={"office"}`, `delete_aliases=frozenset()`, `pk_fields=("c_office_id",)`, `server_assigned_pk_fields={"c_office_id"}`, `update_fields={name, name_alt, translation, translation_alt, pinyin, pinyin_alt, dynasty_code, type_ids, source_id, pages, notes}`, `requires_explicit_approval=True` |
| `models.py` | New `ResourceSpec.required_update_fields` — the server requires `name`/`type_ids`/`source_id`/`dynasty_code` on update as well, and nothing client-side says so today |
| `models.py` / `staging.py` / `http_client.py` | The rule-12 refusal messages are hard-coded with the code-table rationale — `staging.py`'s "the server offers NO delete path, so a wrong row is permanent" and `http_client.py`'s "offers no way to undo it". Both become **false** for `office`, which *is* deletable while unreferenced. `models.py`'s own comment on `requires_explicit_approval` already warns "If you gate one of those, don't inherit the undeletable wording" — so the messages need to derive the reason from the spec rather than assert it |
| `code_lookup.py` | **Not free after all** — see the correction below the table. `FIELD_CODE_TABLES` is keyed on `c_*` column names, so the aggregate's semantic inputs (`type_ids`, `source_id`, `dynasty_code`) resolve to nothing, and `office_type_chains()` is keyed by *office* id, not type-node id, so there is no path from `06091204` to 刺史 at all |
| `models.py` | New `ResourceSpec.full_overwrite_update: bool`. When set, `validate_changes("update", …)` requires `changes` to contain **every** key in `update_fields` — a value or an explicit `null`. This is the client-side answer to trap 4: it turns "you forgot `notes`, so it is now empty" into a structural error, and forces an author who really means "clear this" to write `notes: null` where a reviewer can see it |
| `models.py` | New `ResourceSpec.list_fields`, checked in `validate_changes()`: each named field, when present, must be a non-empty list of non-empty strings. `type_ids` is the first field in this client whose *value shape* is load-bearing (`type_ids: "06"` as a bare string would be silently mangled); `postings`' `c_addr` pseudo-field can reuse it later |
| `docs/04-field-whitelists.md` | New §15, mirroring §14's treatment of `text_codes`: alias rule, the `offices` collision (both halves), full-overwrite warning, the varchar(255)/longtext split, and "no duplicate guard on create" |
| `docs/07-api-md-digest.md` | Re-sync (§8) — a prerequisite, since this design cites `API.md` text the digest does not contain |
| `AGENTS.md` | Rule 12: `office` moves from "unmodelled" to "modelled: create + update, no delete"; keep the `office` vs `offices` sentence; add the full-overwrite warning |
| `skills/cbdb-data-entry/SKILL.md` | The "never create a code table to unblock a batch" bullet gains `office` as *modelled if approved*; add that an office `update` rewrites the whole row, so current values must be read first |
| `tests/test_models.py` | Whitelist is exactly §13.4's field list; `office` requires approval; `offices` does **not** resolve to it; `approval_gated_aliases()` still excludes every postings alias (the regression guard for trap 1); `full_overwrite_update` rejects an update missing any writable field and accepts an explicit `null`; `list_fields` rejects a scalar `type_ids`; `delete_aliases` empty |
| `tests/test_staging.py` | An `office` update with no `approved_by` is a structural error; `resolve_target_pk()` yields `{"c_office_id": 12304}` |
| `tests/test_mutation_api.py` | Envelope: `resource: "office"`, `operation: "update"`, `target.pk == {"c_office_id": 12304}`, `person_id: 0`, `mode: "direct"`; refusal without `approved_by` |

**Revision note on `create`.** An earlier draft of this section modelled `update` only,
on the grounds that a create would open a path with no duplicate protection at either
end: the server has none (trap 3), and §6's pre-flight was a *documented procedure for
one batch*, not code. That was scope-narrowing rather than a fix — the moment a second
office write was approved, nothing would make anyone run the procedure. So `create` **is**
modelled, and the condition attached to it was met instead: the duplicate check is now
code (`preflight.py`), it runs inside `MutationApi.create()` — the layer that actually
sends, so a direct library call cannot walk past it — and it fails the proposal. Read its
module docstring before relying on it: it is a real guard with two structural holes, both
imposed by the only endpoint rule 1 allows.

**What genuinely needs no change:** `batch_runner.py`, `staging.py`'s resolution logic
and `cli.py`. `resolve_target_pk()` already produces `{"c_office_id": 12304}` for this
shape (`c_personid` is not in `pk_fields`, so nothing is merged in), `find_issues()`'s
update branch accepts it — `required` reduces to the empty set and the server-assigned
check is satisfied by `c_office_id` being *present*, with the message "must come from an
earlier create's response or a pre-existing known value, never invented", which is
exactly right for `12304` — and `mutation_api.update()` accepts it. The approval gate
fires correctly too: `approval_gated_aliases()` adds each gated spec's `key` as well as
its aliases, so `"office"` is covered even though it is registered only as an update
alias.

**Correction to an earlier claim in this document:** an earlier draft said the review
page would label the office type nodes "for free" via `code_lookup.py`. It will not.
`FIELD_CODE_TABLES` is keyed on `c_*` **column** names, and `resolve_values()` skips any
field not in it, so `type_ids`, `source_id: 3892` and `dynasty_code: 6` get **no**
labels — only `target_pk`'s `c_office_id: 12304` does. And `office_type_chains()` is
keyed by office id, not by type-node id, so nothing in the client can turn `06091204`
into 刺史; since `12304` has no type rows yet, even its own chain renders empty.
`LIST_VALUED_FIELDS` is `{"c_addr"}`, so `type_ids` is not treated as list-valued either.
Options: map the aggregate's semantic names into `FIELD_CODE_TABLES`, add a type-node
lookup, or accept an unlabelled review for this one batch and read the node meanings out
of §5.3. For a single-proposal batch the third is honest and cheapest — but it must be a
decision, not an unnoticed gap.

## 5. The data

### 5.1 Why B, and what it costs

Tang (`c_dy = 6`) already contains three overlapping offices (snapshot 2026-08-15):

| id | name | translation | type nodes | postings |
|---|---|---|---|---|
| **12304** | **知州事** | *(none)* | **none** | **1** |
| 11356 | 知州 | Prefect | `06`, `0609` | 37 |
| 11321 | 知軍州事 | [Not Yet Translated] | `06`, `0609` | 2 |
| 950 *(Song)* | 知某州軍州事 | Administrator of Prefectural Civil and Military Affairs | `15060202` | 17739 |

`12304` is the same office as the request, written without the `某` placeholder — and its
current `c_notes` says so out loud:

> `Title found in Tang epitaphs. Temporarily added to office codes. Need to be checked.`

The 唐會要 passage the user supplied **is** that check. So B is not merely the
duplicate-avoiding option; it completes a row that explicitly asks to be completed.
`12304`'s single posting is 封魯卿 (144856, Tang), whose note reads `融州司馬知州事` — a
real instance, and one the edict describes exactly (a 司馬, i.e. one of the 上佐, standing
in for an absent prefect). CBDB's house style for the generic form is the `某` one: the
Song counterpart is `950 知某州軍州事`, not `知州軍州事`.

What B costs, stated plainly:

- **The office gets typed.** `12304` has no `OFFICE_CODE_TYPE_REL` rows and `type_ids` is
  required on update (trap 5), so it acquires three type nodes as a side effect (§5.3) —
  an improvement on being untyped, but a change nobody asked for.
- **The full row is rewritten** (trap 4), including a human-authored `c_notes`. §5.2
  preserves it byte-for-byte.
- **`知州事` stops being the row's name.** It is the form actually attested in 封魯卿's
  posting note *and* twice in the edict itself (`但令上佐依次知州事`,
  `許差錄事參軍知州事`). Losing it from the row would make the attested string
  unsearchable, so it goes into the alias list — which is exactly what `950` does
  (`知州事` is the first of its 45 aliases). See §5.2.
- **No rollback.** Unlike a bad `create`, a bad update is corrected only by another
  full-overwrite update.

Not a cost: office rename is **not** reference-blocked (only `social-institution` blocks
renames while referenced), and 封魯卿's posting points at `c_office_id`, so it follows the
row automatically.

I had earlier recommended doing B through the web UI's office entity page rather than this
client, on the grounds that the client cannot read the row it is about to overwrite.
**That recommendation no longer holds, and I withdraw it**: the row turns out to have only
ten writable columns, four of them currently `NULL`, and all of them readable live through
`/api/select/search/office?q=12304`. §4's `full_overwrite_update` check plus §6's
pre-flight read make the client path good enough, and it is the better one — it produces
the rule-12 `approved_by` trail, the staging review, and this repo's own audit log, none
of which a UI edit does.

**One residual risk that neither check removes, stated rather than designed away: a
concurrent edit is silently overwritten.** `OfficeImportService::update()` takes a
`lockForUpdate()` on the row, builds the complete replacement from *our* payload, and
writes it. It never compares the row against the values we read in the pre-flight, and
the aggregates have **no baseline/compare-and-swap mechanism at all** — unlike the
kinship/association mirror path, which does have `conflictBaselines()` and a
`MirrorConflictException` 409. So if someone edits `12304` in the web UI between our
pre-flight read and our write, their edit is lost, `ok: true` comes back, and nothing in
the response says so. The lock prevents a torn write, not a lost update. Mitigations, all
of them narrowing the window rather than closing it: run the pre-flight immediately
before `submit` (not at drafting time), keep this a single-proposal batch so nothing
queues behind it, and treat §6's read-back as a *post*-condition — if the row that comes
back is not what we sent, someone else wrote in between and the two versions need a human
to merge. Closing the window properly would need an upstream feature (an expected-values
or revision parameter on the aggregate update); worth raising with the target system's
maintainers if entity-aggregate edits become routine, and not worth blocking one row on.

### 5.2 The row, field by field

Current values on the left, proposed on the right. **All ten writable columns appear** —
that is what makes the full overwrite safe.

| Column (input name) | Current | Proposed |
|---|---|---|
| `c_office_chn` (`name`) | `知州事` | **`知某州事`** |
| `c_office_chn_alt` (`name_alt`) | `NULL` | **`攝某州事;知州事`** |
| `c_office_trans` (`translation`) | `NULL` | **`Administrator of Prefectural Civil Affairs`** |
| `c_office_trans_alt` (`translation_alt`) | `NULL` | `null` (explicit — see below) |
| `c_office_pinyin` (`pinyin`) | `zhi zhou shi` | **`zhi mou zhou shi`** |
| `c_office_pinyin_alt` (`pinyin_alt`) | `NULL` | **`she mou zhou shi;zhi zhou shi`** |
| `c_dy` (`dynasty_code`) | `6` | `6` (unchanged) |
| `c_source` (`source_id`) | `NULL` | **`3892`** |
| `c_pages` (`pages`) | `NULL` | **`卷六十八 刺史上`** |
| `c_notes` (`notes`) | the English provisional note | **existing note verbatim + the 唐會要 passage** |
| `type_ids` | *(none)* | **`["06", "06091204", "06091202"]`** |

⚠️ **This table is a record of what was sent on 2026-09-04, not a description of the
live row today.** `c_notes` has since been edited by someone through another path: the
live value is now 243 characters, the preserved 84-character English sentence is gone,
and the edict carries a `唐會要：「…」` citation wrapper that appears nowhere in the
payload. The other nine columns are unchanged. That edit is an improvement — the English
note said "Need to be checked", and it has now been checked — and it is exactly the
lost-update window §5.1 describes, arriving in practice within hours. Two consequences:
**do not rebuild a full-overwrite payload from this table** (it would revert that edit,
which is why the archived batch is stamped DO NOT RE-SUBMIT), and §6's pre-flight rule
now fires on this row — re-read it live before any further update.

Notes on the non-obvious ones:

- **`攝`, not the requested `摄`.** `OFFICE_CODES` is traditional throughout (`錄事參軍事`,
  `別駕`, `醫學博士`), and the server will not convert it for us: the variant map is a
  curated variant list, not a 簡→繁 converter (trap 7). `摄某州事` would land verbatim and
  be invisible to every traditional-character search. Usefully, the edict attests the verb
  directly — `使司不得差攝` — so `攝某州事` is not a guess.
- **`;` separator, `知州事` second.** `950` and `3310` both store multi-alias lists as
  `;`-separated with no spaces, most-canonical first, with a matching `;`-separated pinyin
  list in `c_office_pinyin_alt`. Ours are 8 and 29 characters — far under the 255 that
  truncated `950`'s (whose 45 aliases have only 31 pinyin parts left as a result).
- **`pinyin` / `pinyin_alt` sent explicitly.** Omitting them makes the server derive
  per-character (`buildPinyin()`), which would give the same `zhi mou zhou shi`
  (cf. `950` = `zhi mou zhou jun zhou shi`) — but the derivation reads a `pinyin` DB table
  we cannot inspect offline, and for `name_alt` it would derive from the whole
  `攝某州事;知州事` string, dropping the `;` and yielding one run-on
  `she mou zhou shi zhi zhou shi`. Explicit is both reviewable and correct.
- **`source_id: 3892`** = 《唐會要:一百卷》/`tang hui yao`. Note it is stored **with the
  卷數 suffix**, which is why an exact-title search for `唐會要` finds nothing — the
  skill's "search by pinyin as well as by characters" rule earned its keep here. Two Tang
  predecessors exist and are *not* what to cite: `25611 會要` (蘇冕) and `26304 續會要`. No
  office row cites `3892` yet; we would be the first.
- **`pages: 卷六十八 刺史上`.** I normalized the user's `卷六八《刺史上》`: every sourced
  office row uses full Chinese numerals with a `卷` prefix and no 《》 (`卷四十五`…`卷四十八`,
  from the 遼史 batch). The 篇名 is worth keeping — 唐會要's 卷 are subdivided by topic — so
  it stays, just unbracketed. Say the word and I will use the exact string instead;
  `c_pages` is free text.
- **`notes`.** The existing 84-character English note is preserved **byte-for-byte** and
  the 237-character edict appended after a blank line. Preserving it is `AGENTS.md`'s own
  rule for content fields, and the two facts are independent: one records that the title
  came from Tang epitaphs, the other that it is attested in a 大曆 12 (777) edict.
  `c_notes` is `longtext`, so 323 characters is not a length concern. The passage is
  NFC-stable — verified with `unicodedata.normalize`, not by eye. Its eight non-CJK
  characters are the four curly quotes (U+2018/2019/201C/201D), the ideographic comma and
  full stop (U+3001/3002) and the fullwidth comma and colon (U+FF0C/FF1A). Worth noting
  the passage is **NFKC-unstable** even though it is NFC-stable: the fullwidth pair would
  fold under NFKC. Upstream applies NFC, so this is safe today and would not be if that
  ever changed. Lenient variant replacement can still rewrite a character, which is what
  §6's read-back checks.
  *Your call if you disagree:* `Need to be checked.` is arguably stale now that you have
  checked it. I left it rather than editing someone else's sentence on my own initiative.
- **`translation_alt: null` explicitly.** Not requested. If you want it, Hucker renders
  `攝` as "Acting" (cf. `2827 攝鴻臚卿` = "Acting Minister of the Court of State Ceremonial
  (Hucker)"), so `Acting Administrator of Prefectural Civil Affairs` is well-founded — and
  the edict's `使司不得差攝` supports the "acting" reading.

### 5.3 The type nodes

The relevant slice of `OFFICE_TYPE_TREE`:

```
06        唐朝                Tang Dynasty                    ← 1678 offices attached
└ 0609    府州郡縣官類        Local Offices                    ← 277
  └ 060912   州郡官門         Local Offices (Prefecture)       ← 2
    ├ 06091202  州官          (not yet translated)   ← 25: the whole 某州* family
    ├ 06091203  郡官          (not yet translated)
    └ 06091204  刺史          (not yet translated)   ← 2: 11304 知刺史, 11588 刺史參佐
```

The edict settles what the office *is* and complicates where it *sits*: when the 刺史 is
absent or the post vacant, the 上佐 (別駕/長史/司馬) 依次知州事; failing them the 錄事參軍;
failing that another 判官. So it is a **刺史-substitute function** (→ `06091204`)
**discharged by 州官** (→ `06091202`).

**Chosen (user, 2026-09-04): `["06", "06091204", "06091202"]` — all three.** The edict
carries both halves at once, so recording both is the more faithful typing: `06091204`
says what the office is (the prefect's authority held in an acting capacity, as
`11304 知刺史` is typed), and `06091202` says who discharges it (the 上佐 and 錄事參軍 —
which is also the 25-office `某州*` family this office sits alongside). Three nodes is
idiomatic: of 4081 typed Tang offices, 1832 carry one node, 2175 carry two, and 74 carry
three or more. The office will therefore appear under 唐朝, under 刺史, and under 州官 in
the browse tree, which is the set of places a reader might look for it.

I had recommended `["06", "06091204"]` alone on the grounds that 知某州事 is not itself a
staff post. That is still true of what the office *is*, but it argues for including
`06091204`, not for excluding `06091202` — and since `OFFICE_CODE_TYPE_REL` is a set
rather than a classification into one bucket, recording both costs nothing.

### 5.4 The staging file, once §4's spec exists

To be written to `data/staging/2026-09-04-tang-zhi-mou-zhou-shi/proposal.yaml`:

```yaml
# GENERATED by the agent. NOTHING HERE HAS BEEN SUBMITTED.
#
# ⚠️ ENTITY-AGGREGATE WRITE, NOT PERSON DATA (AGENTS.md rule 12) - global reference
# data every CBDB user sees. Validation refuses this batch until `approved_by` names
# the human who decided.
#
# ⚠️ THIS UPDATE OVERWRITES THE WHOLE ROW (API.md 13.4: the aggregate update is NOT
# PATCH). Every writable column is listed below on purpose, including
# `translation_alt: null`. Deleting a line from `changes` does not "leave that field
# alone" - it CLEARS it. `c_notes` in particular already contains human-authored text,
# reproduced verbatim below with the new passage appended.
#
# ⚠️ /api/v2/get cannot read an office, so `preview.md` will show no live old-vs-new
# diff for this proposal. That is expected. Run the pre-flight read in section 6 of
# docs/10-office-aggregate-design.md and eyeball it against the values here.

batch_id: 2026-09-04-tang-zhi-mou-zhou-shi
source_excerpt: |
  User request (2026-09-04): record a Tang office 知某州事, English "Administrator of
  Prefectural Civil Affairs", alias 摄某州事, from 《唐會要》卷六八《刺史上》.

  Chose to edit 12304 知州事 in place rather than create a new code (plan B). 12304's
  own note says "Title found in Tang epitaphs. Temporarily added to office codes.
  Need to be checked." - and the 唐會要 edict below is that check. Tang also has
  11356 知州 (37 postings) and 11321 知軍州事 (2); neither is this office. 知某州事
  and 攝某州事 exist as an office name in no dynasty.

  《唐會要》卷六八《刺史上》: 御史臺奏：“謹按大曆十二年五月一日敕：‘刺史有故及缺，使司
  不得差攝，但令上佐依次知州事…’”

proposals:
  - id: off1
    resource: office          # NEVER `offices` - that alias resolves to postings
    operation: update
    person_id: 0              # convention for global reference data (API.md 13)
    target_pk:
      c_office_id: 12304      # pre-existing, verified value - never invented
    changes:
      name: 知某州事
      name_alt: 攝某州事;知州事   # traditional 攝; 知州事 kept - it is the attested form
      translation: Administrator of Prefectural Civil Affairs
      translation_alt: null   # explicit: leave empty (see design section 5.2)
      pinyin: zhi mou zhou shi
      pinyin_alt: she mou zhou shi;zhi zhou shi
      dynasty_code: 6         # 唐 / Tang, per DYNASTIES - unchanged
      type_ids: ["06", "06091204", "06091202"]   # resolved: conflict off1c1
      source_id: 3892         # 《唐會要:一百卷》 - stored WITH the 卷數 suffix
      pages: 卷六十八 刺史上
      notes: |-
        Title found in Tang epitaphs. Temporarily added to office codes. Need to be checked.

        御史臺奏：“謹按大曆十二年五月一日敕：‘刺史有故及缺，使司不得差攝，但令上佐依次知州事。其上佐等，多非其才，亦望委外道使臣，精加銓擇。不勝任者，具以狀聞。’昨者，宣州觀察使於敖所差周墀知池州，若據敕旨，便合奏剖。今勘其由，長史、司馬並在上都守職，有錄事參軍顧復元在任。若不重有條約，所在終難守文。伏請自今已後，刺史未至，上佐闕人，及別有句當處，許差錄事參軍知州事。如錄事參軍又闕，則任別差判官。仍具闕人事由，分析聞奏，並申中書門下御史臺。所冀詔旨必行，繩違有據。”敕旨依奏。
    source_quote: 謹按大曆十二年五月一日敕：刺史有故及缺，使司不得差攝，但令上佐依次知州事
    confidence: high
    approved_by: null         # ⛔ REQUIRED. The agent must never fill this in.
    conflicts:
      - id: off1c1
        field: type_ids
        description: >
          12304 currently has NO OFFICE_CODE_TYPE_REL rows, and type_ids is required
          on update (API.md 13.4), so this update necessarily types the office. The
          edict makes it a 刺史-substitute (06091204) discharged by 州官 (06091202);
          both readings are defensible. See design section 5.3.
        options:
          - value: ["06", "06091204"]
            rationale: >
              刺史 node only, as 11304 知刺史 is typed - the closest analogue, also a
              知 + prefect office. What the office IS.
          - value: ["06", "06091204", "06091202"]
            rationale: >
              Both nodes - what it is AND who holds it. 74 Tang offices carry 3+.
          - value: ["0609", "06091202"]
            rationale: >
              州官 only, matching the 25-office Tang 某州* family (89479-89503).
        agent_suggestion: ["06", "06091204"]
        agent_reasoning: >
          06091202 州官 holds prefectural STAFF posts (別駕, 長史, 司馬, 錄事參軍事...).
          知某州事 is not a staff post - it is the prefect's authority held in an acting
          capacity, which is what 06091204 is for, and 11304 知刺史 is the precedent.
          The staff connection is already captured by the edict quoted in c_notes.
        # RESOLVED by the user 2026-09-04: take both nodes. The edict states both halves
        # (代行刺史 / 由上佐、錄事參軍充任), and OFFICE_CODE_TYPE_REL is a set - recording
        # "who discharges it" costs nothing and does not weaken "what it is".
        resolution: ["06", "06091204", "06091202"]

batch_notes: >
  ONE proposal, one row, ten columns overwritten. Do not edit `changes` by deleting a
  line: an absent optional field is written as null (API.md 13.4).

  After a successful submit, record result.pk / operation_id / types_added here and in
  docs/02-review-log.md, then run the read-back in section 6 of the design doc. Check
  the response's top-level `notices` array: a variant-character replacement means the
  landed text differs from what was sent, and the read-back is the only way to see
  what happened to c_notes (result.row echoes only four fields).
```

## 6. Pre-flight, submit, read-back

`/api/v2/get` cannot serve this resource (trap 6) and `result.row` echoes four of ten
columns (§3), so both ends of the write are on us.

**Pre-flight (live, immediately before submitting).** Public, unauthenticated, through
`http_client.get(..., public=True)`:

```
GET /api/select/search/office?q=12304             # numeric q matches c_office_id
GET /api/select/search/office?q=知某州事&c_dy=6    # must return nothing in Tang
GET /api/select/search/office?q=知州事&c_dy=6      # expect 12304 and nothing else in Tang
GET /api/v2/texts?ids=3892                        # confirm 唐會要 is still c_textid 3892
```

The first is the one that matters: it returns the full `OFFICE_CODES` row, so diff all ten
columns against §5.2's "Current" column. **If any current value differs from what is
recorded there, stop** — someone has edited the row since the 2026-08-15 snapshot, and the
overwrite payload has to be rebuilt from the live values before it is safe.

Note precisely what that does and does not buy. It catches an edit made *before* the
pre-flight; it cannot catch one made *between* the pre-flight and the write, because the
aggregate update carries no baseline (§5.1's residual-risk paragraph). So run these
immediately before `submit`, not the day before.

Three honest limits of that endpoint:

- It matches `c_office_chn` and `c_office_pinyin` **only — never `c_office_chn_alt`**. A
  name hiding in another office's alias list is invisible to it. `攝某州事` appearing in no
  alias field anywhere is a *snapshot* finding (up to a week stale). Acceptable here
  because the alias is not the identity being checked and B creates no new code, but it is
  a residual gap, not a covered case.
- The `c_dy` filter **falls back to unfiltered when it finds nothing**
  (`ApiController::searchOffice()`), so "no Tang match" can silently come back as other
  dynasties' rows. Read `c_dy` on every row; don't trust the filter.
- Shape is a Laravel paginator (rows under `data`) and upstream explicitly does not
  guarantee it (§14.4). Parse defensively.

The snapshot must not be used for the pre-flight — `AGENTS.md`'s snapshot rule names "does
this row already exist / what is currently true of this record" as exactly what it may
never decide. Its role here was finding candidates and conventions, which is what it is
for.

**Submit.** `python -m cbdb_agent validate --staging <path>` until clean, then
`submit --staging` with `CBDB_DRY_RUN=true`, then once end-to-end against the local
instance (`http://localhost:8000`, which mirrors production data and has `12304`), and
only then production — where rule 4 requires `CBDB_CONFIRM_PROD` to equal the exact
current `CBDB_API_BASE_URL`.

**Read-back (rule 11).** `ok: true` does not mean the fields landed. Re-run
`GET /api/select/search/office?q=12304` and compare all ten columns against the staging
file, **`c_notes` byte-for-byte** (lenient variant replacement applies to it; a
replacement should also have appeared in `notices`). The type rows are not visible there:
confirm them from the response's `types_added`, or `GET /api/OFFICE_CODE_TYPE_REL`
(allowed as the no-snapshot fallback, rule 1) if they need independent checking before the
next weekly snapshot.

## 7. Decisions, and what is still blocking

Settled by the user, 2026-09-04:

| Decision | Value |
|---|---|
| Create new vs. edit in place | **B — edit `12304` in place** (§5.1) |
| `translation` | **`Administrator of Prefectural Civil Affairs`** (the requested string plus the head noun, parallel to `950`) |
| `source_id` | **`3892`** = 《唐會要:一百卷》, cited at `卷六十八 刺史上` |
| `notes` | the 大曆 12 (777) 御史臺 memorial, appended after the preserved English note |
| `type_ids` | **`["06", "06091204", "06091202"]`** — all three (§5.3) |

Decided by me rather than asked, all cheaply reversible — say the word and any of them
flips:

- `pages` normalized from `卷六八《刺史上》` to `卷六十八 刺史上`. Weaker evidence than
  first stated: of 5,793 sourced `OFFICE_CODES` rows only 2,314 carry any `c_pages`, and
  those are four distinct 遼史 values (`卷四十五`–`卷四十八`) plus one counterexample,
  office 202979's bare `8947`. So the `卷` + full-Chinese-numeral form is *one batch's*
  habit, not a house style — the "no 《》" half does hold (zero rows contain them).
- ~~The existing English `c_notes` sentence kept verbatim, `Need to be checked.`
  included, rather than editing someone else's sentence on the agent's own initiative.~~
  **Wrong call, corrected by the user the same day.** A "needs checking" marker has no
  place in submitted data: submission is the act that asserts the data is correct, the
  note is a claim about the editor's workflow rather than about the historical record,
  and it stays visible to every CBDB user long after the checking is done. The 唐會要
  edict being added *was* that check, so the sentence should have gone. Keeping other
  people's prose byte-for-byte is still right for substantive content — it was the wrong
  instinct to apply to a self-obsoleting workflow marker.
- `translation_alt` left explicitly `null`. `Acting Administrator of Prefectural Civil
  Affairs` is well-founded if you want it (Hucker renders `攝` as "Acting", and the
  edict's `使司不得差攝` supports the reading).

**Still blocking submission: `approved_by`** — rule 12 requires a named human, and the
agent must never fill it in. `staging.find_issues()` reports the batch as structurally
invalid until it is set, which is the intended behaviour, and `batch_runner` forwards the
name into `meta.comment` so the sign-off also lands on the server's own `operations`
row.

## 8. What the prerequisite re-sync turned up — **done**

`docs/07-api-md-digest.md` was stamped `fd747aba` (2026-08-18); upstream `develop` had
moved to `b2df35f5`, 8 commits and +50/−16 lines of `API.md` later. That re-sync was a
prerequisite for §4 (this design cites `API.md` text the digest did not contain) and has
been completed, along with the defect it exposed. Both are recorded in
`docs/02-review-log.md`.

**The defect: 11 phantom fields and 6 missing ones in `models.py`.** Bigger than the
`API.md` diff suggested, because upstream only spelled out the *update* lists for
`altnames`/`texts` ("create = the same plus `c_personid`"), so reading the diff alone
would have fixed half of it:

| Resource | Phantom — we allowed it, it is not a column | Missing — a real column we forbade |
|---|---|---|
| `basicinformation` (create **and** update) | `c_by_yymm`, `c_by_yymm_day`, `c_dy_yymm`, `c_dy_yymm_day`, `c_self_bio` | `c_birthyear`, `c_deathyear`, `c_by_month`, `c_by_day`, `c_dy_month`, `c_dy_day` |
| `altnames` (create **and** update) | `c_alt_name_pinyin`, `c_alt_name_pinyin2`, `c_alt_name_pinyin3`, `c_alt_name_role` | — |
| `texts` (create **and** update) | `c_supplement`, `c_text_year` | — |

The names were wrong in *upstream's own* whitelists until `8a3c9f04`/`b2df35f5`, and this
repo transcribed them from the older versions of those lists — so "read directly from
the handler source" was not sufficient protection, because the handler source was itself
wrong. The consequence splits by handler base class, not by resource type:
`basicinformation` extends `AbstractMutationHandler` directly and therefore **silently
drops** an unknown field while returning `200 ok:true` (`API.md` §4.6), which is
precisely what `models.py`'s whitelist exists to prevent; `altnames` and `texts` extend
the person-subresource handlers, which validate and return `422 disallowed_fields`, so
there the phantom names would have broken a submission rather than losing data quietly.
The six missing `basicinformation` fields were the costliest part in practice: birth and
death year/month/day could not be sent on a create at all, so recording them required a
create followed by an update.

Fixed in `models.py`, `docs/04-field-whitelists.md` (⚠️ notes on §1, §2, §10 plus the
header) and `docs/07-api-md-digest.md` §3.1, with seven new tests in
`tests/test_models.py`. **Five** of the seven actually fail if the fix is reverted
(checked by rebuilding the pre-fix specs in-process and re-running each one, rather than
assuming). The other two are not regression guards and are not meant to be: one is the
paired anti-over-correction check, the other pins create/update symmetry, which held
before the fix as well. Two things worth keeping from how it
was found:

- **The phantom-ness is per table, not per name.** Three of the 11 names are perfectly
  real columns elsewhere — `c_self_bio` on `BIOG_SOURCE_DATA`, `c_supplement` on
  `STATUS_DATA`, `c_text_year` on `TEXT_CODES` — which is exactly why they looked
  plausible. The first version of the regression guard was a flat by-name blacklist and
  it immediately failed on `sources` and `statuses`, correctly. It is now keyed by
  resource, with a paired test asserting those three stay allowed where they belong, so
  a future cleanup cannot "fix" the drift by deleting three legitimate fields.
- **Verify against a column list, not another whitelist.** The check that settled it was
  `pragma table_info` on the weekly SQLite snapshot, cross-referenced with the handler
  constants in `${CBDB_ONLINE_MAIN_SERVER_REPO_DIR}`. Upstream now has a schema-drift
  guard of its own (`tests/Feature/MutationAllowedFieldsSchemaDriftTest.php`); we have no
  equivalent, because our whitelist is a hand-maintained copy with no database to compare
  against. A test that diffs `models.py` against the handler sources when that checkout
  is configured is the cheap approximation if this recurs — deliberately not added now,
  since an opt-in test that parses PHP is fragile and nobody would run it.

Other facts from the same sync, folded into the digest and relevant here: `notices` now
covers the office and social-institution aggregates and **can appear on failure
responses** (409/422); Unicode NFC folding applies to all text columns, silently;
`text-entity` is a third entity aggregate; `POST /api/v1/user/login` now returns
`410 Gone` and no longer verifies a password; and there are new per-IP throttles on the
unauthenticated form endpoints and on `/api/operations/token`.

**Remaining sequencing:** §4's `office` spec is next, as its own change with its own
review pass.
