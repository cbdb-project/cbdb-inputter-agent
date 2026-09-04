# Per-Resource Field Whitelists

Status: implemented (`src/cbdb_agent/models.py`, Milestone 3 — see
`docs/02-review-log.md`; all 13 resources cross-checked field-by-field against
the target repo during review). Read directly from
`cbdb-online-main-server`'s `app/Services/Mutations/*Handler.php` and
`app/Support/CompositePrimaryKey.php` on 2026-07-08 (see brief's caveat: this is a
snapshot, re-verify against the live repo before trusting it for anything
security-critical). This is the source of truth `models.py` (Milestone 3) should
encode — one dataclass/whitelist per resource, matching this document exactly.

⚠️ **Re-verified 2026-09-04 against upstream `b2df35f5`, and three of these lists were
wrong** — which supersedes the "all 13 resources cross-checked field-by-field against the
target repo" claim in the paragraph above. That cross-check happened and was faithful; it
just could not catch this, because **the handler source it checked against was itself
wrong**. `basicinformation`, `altnames` and `texts` between them named **11 fields that
are not columns in the database at all**, and `basicinformation` was missing six that
are. Those names sat in upstream's own whitelists — which is where this document was
transcribed from — until `8a3c9f04` (2026-08, the `altnames`/`texts` six) and `b1f4bf44`
(2026-09-04, the `basicinformation` five) removed them.

While upstream shared the phantom, sending one was **not** a silent drop: a field inside
the server's whitelist that is not a column survives every filter and fails at the
`INSERT`, so the caller got a **`500` that echoed the SQL plus the host and database
name**. After upstream's cleanup the same field is silently dropped on
`basicinformation` (`200 ok:true`, value missing) and rejected with
`422 disallowed_fields` on `altnames`/`texts`. Each affected section carries a ⚠️ note;
the full account is in `docs/07-api-md-digest.md` §3.1. Lesson worth keeping: check a
field name against a real **column list** (e.g. `pragma table_info` on the weekly SQLite
snapshot), not only against the handler that claims to accept it.

General mechanics shared by every "person subresource" handler (everything except
`basicinformation`, `possessions`, `postings`/`offices`, and `sources`, which have
their own quirks noted below):
- Create/update reject any `changes` key not in that handler's `allowedFields()` →
  server-side `422 disallowed_fields`. Our client-side whitelist in `models.py` should
  mirror this exactly so we fail fast with a clearer message (per
  `01-implementation-plan.md` §6).
- Delete only needs `target.pk` matching the resource's key columns — no body
  whitelist.
- `target.pk` is validated against `CompositePrimaryKey::SCHEMAS[TABLE]` (exact
  required key set).

## Quick-reference table

| Resource | Accepted `resource` aliases | PK fields (count) | Extra create-only fields vs. update | PK server-assigned? |
|---|---|---|---|---|
| basicinformation | `basicinformation`, `biogmain`, `biog_main` | `c_personid` (1) | `c_name*` fields immutable on update (see §1) | No — client-supplied `c_personid` |
| altnames | `altnames`, `altname`, `altname_data` | 3 | `c_personid` | No |
| addresses | `addresses`, `address`, `biog_addr_data` | 4 | `c_personid` | No |
| entries | `entries`, `entry`, `entry_data` | 10 | `c_personid` | No |
| statuses | `statuses`, `status`, `status_data` | 3 | `c_personid` | No |
| events | `events`, `event`, `events_data` | 3 | `c_personid` | No (`c_addr_id` via side table) |
| associations | `associations`, `association`, `assoc_data` | 9 | `c_personid` | No |
| kinship | `kinship`, `kin`, `kin_data` | 3 | `c_personid` | No |
| possessions | `possessions`, `possession`, `possession_data` | 1 | none (same list) | **Yes** — `c_possession_record_id` |
| texts | `texts`, `text`, `biog_text_data`, `text_data` | 3 | `c_personid` | No |
| postings / offices | `postings`, `posting`, `posted_to_office_data` (⚠️ server also accepts `offices`, but our client deliberately doesn't — see §11) | 2 | none (same list, incl. `c_office_id`) | **Yes** — `c_posting_id` |
| social_institutions | create/delete: + `socialinst`; **update: missing `socialinst`, see §12** | 4 | `c_personid` | No |
| sources | create/update: `sources` only; **delete also accepts `source`, `biog_source_data`** | 3 (`c_pages` optional) | none (identical create/update list) | No, but `c_textid`/`c_pages` re-keyable |
| office ⚠️ | **`office` only** — never `offices` (postings wins server-side, and it would poison the approval gate); see §15 | 1 | none (identical create/update list) | **Yes** on create — `c_office_id`; known value on update |

**⚠️ Bug/gap in the target system to design around (§12):** the `social_institutions`
update handler's alias list is `['social_institutions', 'social_institution',
'biog_inst_data']` — it does **not** accept `socialinst`, unlike its create/delete
counterparts. `mutation_api.py`'s `update_social_institution()` wrapper must send
`resource: "social_institutions"` (or `"biog_inst_data"`), never `"socialinst"`, or
the update call will 404/mismatch. Worth flagging upstream to the CBDB team, but our
client should route around it either way.

**⚠️ Two resources have a server-assigned PK component**, unlike the general rule
that IDs are client-assigned (`AGENTS.md` rule 6 is specifically about `c_personid`,
which is still always client-assigned for `basicinformation` — this is a *different*
ID on a *different* table):
- `possessions`: `c_possession_record_id` is server-assigned (max+1) — never send it
  in `target.pk` on create.
- `postings`/`offices`: `c_posting_id` is server-assigned (max+1) — client supplies
  only `c_office_id` in `changes`; `target.pk` is only fully known after the server
  responds.

`person_id.py` / `mutation_api.py` must special-case these two: don't run the
generic "allocate then validate ID" logic from brief §3 for them, and read the
server's response to learn the assigned ID before referencing that row again in the
same batch.

## 1. basicinformation (`BIOG_MAIN`)

- PK: `c_personid`.
- **Create** whitelist (`BiogMainCreateHandler.php` `ALLOWED_FIELDS`): `c_personid`,
  `c_name_chn`, `c_name`, `c_name_proper`, `c_name_rm`, `c_surname_chn`,
  `c_mingzi_chn`, `c_surname`, `c_mingzi`, `c_surname_proper`, `c_mingzi_proper`,
  `c_surname_rm`, `c_mingzi_rm`, `c_female`, `c_index_year`, `c_index_year_type_code`,
  `c_index_year_source_id`, `c_index_addr_id`, `c_index_addr_type_code`, `c_dy`,
  `c_by_intercalary`, `c_birthyear`, `c_by_nh_code`, `c_by_nh_year`, `c_by_range`,
  `c_by_month`, `c_by_day`, `c_by_day_gz`, `c_dy_intercalary`, `c_deathyear`,
  `c_dy_nh_code`, `c_dy_nh_year`, `c_dy_range`, `c_dy_month`, `c_dy_day`,
  `c_dy_day_gz`, `c_death_age`,
  `c_death_age_range`, `c_fl_earliest_year`, `c_fl_ey_nh_code`, `c_fl_ey_nh_year`,
  `c_fl_ey_notes`, `c_fl_latest_year`, `c_fl_ly_nh_code`, `c_fl_ly_nh_year`,
  `c_fl_ly_notes`, `c_ethnicity_code`, `c_household_status_code`, `c_tribe`,
  `c_choronym_code`, `c_notes` (51 fields). Blocked always:
  `c_created_by`/`c_created_date`/`c_modified_by`/`c_modified_date`.
  - ⚠️ **Corrected 2026-09-04.** This list previously named `c_by_yymm`,
    `c_by_yymm_day`, `c_dy_yymm`, `c_dy_yymm_day` and `c_self_bio`. **None of those is
    a `BIOG_MAIN` column** — the real month/day columns are `c_by_month`, `c_by_day`,
    `c_dy_month`, `c_dy_day`, and `c_self_bio` was dropped from `BIOG_MAIN` in
    migration `2026_03_13` (a column of that name survives only on
    `BIOG_SOURCE_DATA`, see §13). They were wrong in *upstream's own* whitelist too
    until `b1f4bf44` removed them, and this list was transcribed from the version that
    still had them. Sending one produced a **`500` echoing the SQL, host and database
    name**, not a `422` — the field passed validation, passed the filter, and failed at
    the `INSERT`. The six real
    birth/death columns above (`c_birthyear`, `c_deathyear`, `c_by_month`, `c_by_day`,
    `c_dy_month`, `c_dy_day`) were missing as a direct consequence — the phantom names
    had taken the month/day slots — so recording a birth or death date used to need a
    create followed by an update. See `docs/07-api-md-digest.md` §3.1.
- **Update** whitelist: **the same set minus the five create-only fields**
  (`c_personid`, `c_name_chn`, `c_name`, `c_name_proper`, `c_name_rm`), which become
  **immutable on update** (blocked, not just excluded) — you cannot rename a person via
  `/api/v2/mutate`; all other `BIOG_MAIN` columns are otherwise mutable. Upstream now
  enforces this symmetry mechanically
  (`tests/Feature/MutationCreateUpdateParityTest.php`): anything `mutate` accepts,
  `create` accepts too, so a field on only one side of our two lists is a
  transcription bug rather than a real asymmetry.
- **Delete** is a **soft delete**: sets `c_name_chn = "<待删除>"` and issues an
  `UPDATE`, not a real `DELETE` — the row persists. Writes an `Operation::TYPE_DELETE`
  and an `audit_log` `UPDATE` entry (not `DELETE`).
- `mode: "proposal"` returns `501` for create and delete (person-level create/delete
  is direct-only); update *does* support proposal mode.
- `c_personid` create validation: nonzero, not already taken, and
  `personId - max(existing c_personid) <= 10000`.
- `c_ethnicity_code` → `ETHNICITY_TRIBE_CODES`; `c_choronym_code` → `CHORONYM_CODES`;
  `c_dy` → `DYNASTIES`.

## 2. altnames (`ALTNAME_DATA`)

- PK: `c_personid`, `c_alt_name_chn`, `c_alt_name_type_code` (3-key; a legacy 4-key
  form including `c_sequence` is auto-stripped).
- **Create**: `c_personid`, `c_alt_name_chn`, `c_alt_name_type_code`, `c_alt_name`,
  `c_source`, `c_pages`, `c_notes`, `c_sequence` (8 fields).
- **Update**: identical minus `c_personid` (7 fields).
  - ⚠️ **Corrected 2026-09-04.** Both lists previously also named
    `c_alt_name_pinyin`, `c_alt_name_pinyin2`, `c_alt_name_pinyin3` and
    `c_alt_name_role`. `ALTNAME_DATA` has 12 columns and **none of them is any of
    those four**; they were in upstream's own whitelist by mistake until `8a3c9f04`.
    While the name sat in *both* whitelists, sending one reached the `INSERT` and
    produced a **`500`** echoing the SQL (see the header note); now that upstream has
    removed it, the altname handler's `array_diff` catches it and returns
    `422 disallowed_fields`. Either way it is loud — this is not a silent-drop path.
    See `docs/07-api-md-digest.md` §3.1.
- `c_alt_name_type_code`/`c_source` sentinel-normalized (`-999`/null/`''` → `'0'`).
- Update checks the new `(c_personid, c_alt_name_chn, c_alt_name_type_code)` tuple for
  collision → `409`.

## 3. addresses (`BIOG_ADDR_DATA`)

- PK: `c_personid`, `c_addr_id`, `c_addr_type`, `c_sequence`.
- **Create**: `c_personid`, `c_addr_id`, `c_addr_type`, `c_sequence`, `c_firstyear`,
  `c_lastyear`, `c_notes`, `c_source`, `c_pages`, `c_natal`, `c_fy_nh_code`,
  `c_fy_nh_year`, `c_fy_range`, `c_fy_intercalary`, `c_fy_month`, `c_fy_day`,
  `c_fy_day_gz`, `c_ly_nh_code`, `c_ly_nh_year`, `c_ly_range`, `c_ly_intercalary`,
  `c_ly_month`, `c_ly_day`, `c_ly_day_gz`.
- **Update**: identical minus `c_personid`.
- `c_addr_id`/`c_source` normalized (null/`''`/`-999` → `'0'`).
- Update checks the new `(c_addr_id, c_addr_type, c_sequence)` tuple for collision →
  `409`.

## 4. entries (`ENTRY_DATA`)

- PK (10-key!): `c_personid`, `c_entry_code`, `c_sequence`, `c_kin_code`,
  `c_assoc_code`, `c_kin_id`, `c_year`, `c_assoc_id`, `c_inst_code`,
  `c_inst_name_code`.
- **Create**: all 10 PK fields + `c_entry_addr_id`, `c_source`, `c_pages`, `c_notes`,
  `c_entry_nh_id` (renamed from legacy `c_nianhao_id`), `c_entry_nh_year`,
  `c_entry_range`, `c_exam_rank`, `c_attempt_count`, `c_exam_field`,
  `c_parental_status_code`, `c_age`, `c_posting_notes`.
- **Update**: identical minus `c_personid` (the other 9 PK fields remain mutable in
  the update body since re-keying is allowed here).
- `c_entry_code`/`c_entry_addr_id`/`c_kin_code`/`c_assoc_code`/`c_inst_code`/`c_source`
  sentinel-normalized. `c_entry_nh_id` → `NIAN_HAO`.

## 5. statuses (`STATUS_DATA`)

- PK: `c_personid`, `c_sequence`, `c_status_code`.
- **Create**: `c_personid`, `c_sequence`, `c_status_code`, `c_source`, `c_pages`,
  `c_notes`, `c_supplement`, `c_firstyear`, `c_fy_nh_code`, `c_fy_nh_year`,
  `c_fy_range`, `c_lastyear`, `c_ly_nh_code`, `c_ly_nh_year`, `c_ly_range`.
- **Update**: identical minus `c_personid`.
- `c_status_code`/`c_source` sentinel-normalized.

## 6. events (`EVENTS_DATA`)

- PK: `c_personid`, `c_sequence`, `c_event_code`.
- **Create**: `c_personid`, `c_event_code`, `c_sequence`, `c_source`, `c_pages`,
  `c_notes`, `c_year`, `c_month`, `c_day`, `c_day_ganzhi`, `c_nh_code`, `c_nh_year`,
  `c_yr_range`, `c_intercalary`, `c_role`, `c_event`. **`c_addr_id` is deliberately
  excluded** from this scalar whitelist.
- **Update**: identical minus `c_personid`; `c_addr_id` excluded here too.
- `c_addr_id` (array of address IDs) and `c_addr_cleared` (flag) are pseudo-fields:
  stripped from `changes` before the whitelist check and written instead to the
  `EVENTS_ADDR` side table via `EventStatusRepository::syncEventAddresses`. An
  "address-only" update (only these two pseudo-fields, no scalar `EVENTS_DATA`
  change) takes a separate direct/proposal code path.

## 7. associations (`ASSOC_DATA`) — mirror-relationship resource

- PK (9-key): `c_personid`, `c_assoc_code`, `c_assoc_id`, `c_kin_code`, `c_kin_id`,
  `c_assoc_kin_code`, `c_assoc_kin_id`, `c_text_title`, `c_assoc_first_year`.
- **Create**: 9 PK fields + `c_assoc_last_year`, `c_assoc_fy_nh_code`,
  `c_assoc_fy_nh_year`, `c_assoc_fy_range`, `c_assoc_fy_intercalary`,
  `c_assoc_fy_month`, `c_assoc_fy_day`, `c_assoc_fy_day_gz`, `c_assoc_ly_nh_code`,
  `c_assoc_ly_nh_year`, `c_assoc_ly_range`, `c_assoc_ly_intercalary`,
  `c_assoc_ly_month`, `c_assoc_ly_day`, `c_assoc_ly_day_gz`, `c_source`, `c_pages`,
  `c_notes`, `c_sequence`, `c_assoc_count`, `c_topic_code`, `c_occasion_code`,
  `c_tertiary_personid`, `c_tertiary_type_notes`, `c_assoc_claimer_id`, `c_addr_id`,
  `c_inst_code`, `c_inst_name_code`. **`c_supplement` is explicitly not allowed**
  (`ASSOC_DATA` has no such column).
- **Update**: identical minus `c_personid`.
- **Pseudo-fields** (accepted in `changes`, stripped before validation, used to
  build/refresh the reciprocal row): `c_assocship_pair`, `c_kinship_pair`,
  `c_assoc_kinship_pair`.
- **Mirror handling**: `c_assoc_code` → `ASSOC_CODES` (reverse via `c_assoc_pair`/
  `c_assoc_pair2`); `c_kin_code`/`c_assoc_kin_code` → `KINSHIP_CODES` (reverse via
  `c_kin_pair1`). Create inserts the reciprocal row in the same transaction. Can
  throw `MirrorConflictException` (`409`, existing reverse row diverges — needs
  `meta.force: true`), `MirrorSuspectedException` (`409`, ambiguous candidates), or
  `MirrorIntegrityException` (`422`, no authoritative reverse code, fail-closed).
  Sending only the `*_pair` pseudo-fields (no other change) triggers a
  mirror-repair-only path. `c_text_title` sentinel `'[n/a]'`,
  `c_assoc_first_year` sentinel `'-9999'` — both required, non-empty PK fields.
- Delete also deletes the mirror row (`BiogMainRepository::syncAssocMirrorOnDelete`).

## 8. kinship (`KIN_DATA`) — mirror-relationship resource

- PK: `c_personid`, `c_kin_id`, `c_kin_code`.
- **Create**: `c_personid`, `c_kin_id`, `c_kin_code`, `c_source`, `c_pages`,
  `c_notes`, `c_autogen_notes`. **`c_supplement` explicitly not allowed** (confirmed
  no such column on `KIN_DATA`).
- **Update**: identical minus `c_personid`.
- **Pseudo-field** `c_kinship_pair`: optional override of the reverse relationship
  code, stripped before whitelist validation. If omitted, resolved authoritatively
  via `KINSHIP_CODES.c_kin_pair1`; if provided, must be a legal reverse candidate for
  the forward `c_kin_code` or `422`.
- Same `Mirror*` exception family as associations (`409`/`422`); same "pair-only"
  update shortcut. Update preserves the existing mirror code when the client omits an
  override and the forward code is unchanged.
- Delete also deletes the mirror row (`BiogMainRepository::syncKinMirrorOnDelete`,
  supports `meta.force` for multi-candidate deletion).

## 9. possessions (`POSSESSION_DATA`) — server-assigned surrogate PK

- PK: `c_possession_record_id` (single field, **server-assigned**, max+1 — never
  send it on create; the class doesn't even use the generic person-subresource base
  classes, since it has no `c_personid` in its PK).
- **Create/update share the same field list** (a legacy phantom-field cleanup removed
  `c_supplement`/`c_measure_value`/`c_firstyear`/`c_lastyear`, which don't exist on
  this table): `c_sequence`, `c_possession_act_code`, `c_possession_desc`,
  `c_possession_desc_chn`, `c_quantity`, `c_measure_code`, `c_possession_yr`,
  `c_possession_nh_code`, `c_possession_nh_yr`, `c_possession_yr_range`, `c_source`,
  `c_pages`, `c_notes`. Plus pseudo-field `c_addr_id` (array) → `POSSESSION_ADDR`
  side table.
- Create requires `person_id != 0` ("unknown person cannot own possessions").
- `c_source`/`c_measure_code`/`c_possession_act_code` normalized to `'0'` even when
  the key is entirely missing (create semantics: unfilled ⇒ sentinel `0`).
- Delete requires only `target.pk.c_possession_record_id`; server validates the row's
  `c_personid` matches the given `person_id`.

## 10. texts (`BIOG_TEXT_DATA`)

- Resource aliases: `texts`, `text`, `biog_text_data`, `text_data` (4 aliases; two of
  them — `text_data` and `biog_text_data` — map to the same table).
- PK: `c_personid`, `c_textid`, `c_role_id`.
- **Create**: `c_personid`, `c_textid`, `c_role_id`, `c_source`, `c_pages`,
  `c_notes` (6 fields).
- **Update**: identical minus `c_personid` (5 fields).
  - ⚠️ **Corrected 2026-09-04.** Both lists previously also named `c_supplement` and
    `c_text_year`. `BIOG_TEXT_DATA` has **neither** column — `c_supplement` is real on
    `STATUS_DATA` (§5) and `c_text_year` is real on `TEXT_CODES` (§14), which is what
    made them look plausible. Same loud failure mode as the `altnames` case: this
    handler pair validates its whitelist, so sending one was a
    `422 disallowed_fields`, **not** a silent drop — `texts` is not on `API.md`
    §4.6's silent-drop list. See `docs/07-api-md-digest.md` §3.1.
- Four real `BIOG_TEXT_DATA` columns are outside the whitelist and **the server does not
  accept them either**: `c_year`, `c_nh_code`, `c_nh_year`, `c_range_code`. Not a gap on
  our side; noted because `c_year` is the real analogue of the removed `c_text_year`.
- `c_textid`/`c_source` normalized (null/`''` → `'0'`).

## 11. postings / offices (`POSTED_TO_OFFICE_DATA`) — server-assigned surrogate PK

- Resource aliases (server-side, per `PostingMutationHandler`/`*CreateHandler`/
  `*DeleteHandler`): `postings`, `posting`, `offices`, `posted_to_office_data`.
  **⚠️ Our client's `models.py` deliberately does NOT include `"offices"`** in its
  own alias sets, despite the server still accepting it as of 2026-07-17. A new,
  unrelated "office entity" resource (`OFFICE_CODES`/`OFFICE_CODE_TYPE_REL`
  reference-data CRUD, added mid-2026 for managing the office code dictionary
  itself, distinct from a *person's appointment record* in `POSTED_TO_OFFICE_DATA`)
  also claims `"offices"` via its own handler's `supports()`. Server-side
  resolution is first-match-wins by registration order in
  `MutationHandlerRegistry`, and today that order still favors the postings
  handlers — but that's an accident of registration order, not a contract. Always
  use `"postings"` (or `"posting"`/`"posted_to_office_data"`) explicitly; never
  `"offices"`.
- PK: `c_office_id` (client-supplied, required, references `OFFICE_CODES`),
  `c_posting_id` (**server-assigned**, max+1).
- **Create/update share the same field list** (unlike most resources, `c_office_id`
  IS included and mutable in the update body — re-keying is allowed): `c_office_id`,
  `c_sequence`, `c_source`, `c_pages`, `c_notes`, `c_firstyear`, `c_fy_nh_code`,
  `c_fy_nh_year`, `c_fy_range`, `c_fy_intercalary`, `c_fy_month`, `c_fy_day`,
  `c_fy_day_gz`, `c_lastyear`, `c_ly_nh_code`, `c_ly_nh_year`, `c_ly_range`,
  `c_ly_intercalary`, `c_ly_month`, `c_ly_day`, `c_ly_day_gz`, `c_appt_code`,
  `c_assume_office_code`, `c_dy`, `c_inst_code`, `c_inst_name_code`,
  `c_office_category_id`. Plus pseudo-field `c_addr` (array) → `POSTED_TO_ADDR_DATA`.
- `c_office_id` required and non-empty on create (`422` if missing).
- Changing `c_office_id` during update triggers address migration; can surface as a
  `409`. `c_appt_code` is `NOT NULL` (null/`''`/`-999` → `0`, references
  `APPOINTMENT_CODES`). `c_source` normalized fully to `'0'`.

## 12. social_institutions (`BIOG_INST_DATA`)

- Resource aliases — **create/delete**: `social_institutions`, `social_institution`,
  `socialinst`, `biog_inst_data`. **Update: `social_institutions`,
  `social_institution`, `biog_inst_data` only — `socialinst` is missing.** See the
  ⚠️ callout above the per-resource sections; `mutation_api.py`'s update wrapper must
  not send `"socialinst"`.
- PK: `c_personid`, `c_inst_code`, `c_inst_name_code`, `c_bi_role_code`.
- **Create**: `c_personid`, `c_inst_code`, `c_inst_name_code`, `c_bi_role_code`,
  `c_source`, `c_pages`, `c_notes`, `c_bi_begin_year`, `c_bi_by_nh_code`,
  `c_bi_by_nh_year`, `c_bi_by_range`, `c_bi_end_year`, `c_bi_ey_nh_code`,
  `c_bi_ey_nh_year`, `c_bi_ey_range`.
- **Update**: identical minus `c_personid`.
- `c_bi_role_code`/`c_source` sentinel-normalized. `c_inst_code`/`c_inst_name_code`
  reference social-institution type/name code tables.
- **Confirmed live (Milestone 7): `GET /api/v2/get`'s alias list is separately
  defined and different again** — `app/Services/Mutations/MutationReadService.php`
  accepts `social_institutions`, `socialinstitution` (no underscore — not the same
  string as the write-side `social_institution`), `social_institution`,
  `biog_inst_data` for reads. It does not accept `socialinst` either. Always pass
  the canonical `social_institutions` key for GET; don't reuse a write-side alias.

## 13. sources (`BIOG_SOURCE_DATA`) — single unified handler, no separate create class

- Resource string: **asymmetric by operation** — `create`/`update` accept `sources`
  only, but `delete` additionally accepts `source` and `biog_source_data`
  (`API.md` §4.5/§9.13; an earlier version of this document said "no aliases" full
  stop, which was true only of the write side). `models.py` deliberately keeps
  `delete_aliases={"sources"}` — a safe subset. Unlike every other resource,
  **create and update share one handler** (`SourceMutationHandler`, which handles
  both operations by delegating to `BiogSourceRepository`).
- PK: `c_personid`, `c_textid`, `c_pages` (`c_pages` is optional/nullable at the PK
  level).
- **Field whitelist is `BiogSourceRepository::MUTABLE_COLUMNS`, not a handler
  constant**: `c_notes`, `c_main_source`, `c_self_bio`. Same list for create and
  update — no divergence, unlike every other resource in this document.
- `c_textid`/`c_pages` are **re-keyable on update**; `c_personid` is explicitly
  immutable on update (`422 changes.c_personid: immutable` if changed).
- `c_textid` must reference an existing row (validated against `TEXT_DATA`/
  `TEXT_CODES`) — `422 c_textid: invalid` otherwise.
- **Confirmed live (Milestone 7):** `GET /api/v2/get`'s `MutationReadService`
  definition for this resource additionally accepts `source` (singular) and
  `biog_source_data` as aliases, beyond the write-side's `sources`-only. Always
  pass the canonical `sources` key for GET.
- Create: `409` if the PK already exists, or if a pending create-proposal exists for
  the same PK. Update: re-keying checked for collision → `409 target.pk: duplicate`.
- Delete: `c_pages` is an optional key field, canonicalized to `''` (not null) to
  match create/update's canonical empty-`c_pages` representation.
- `c_main_source`/`c_self_bio` are boolean flags.

## 14. text_codes (`TEXT_CODES`) — a code table, not person data ⚠️

**Read `AGENTS.md` rule 12 before using this.** This is the only code-table write this
client models, and it exists only because a citation for a book CBDB doesn't yet know
cannot be recorded any other way. It is global reference data: referenced by potentially
tens of thousands of person rows, and **the server has no delete path for it** — `403`
for `mode=direct`, `501` for `mode=proposal` (`API.md` §13.3). A wrong row is permanent.
Afterwards only `c_title` (the romanization) is editable; `c_title_chn` is frozen.

- Resource strings registered for create: **`text-codes`** (what the client actually
  sends) and **`text_codes`**. The server accepts a third, `textcodes`, which
  `models.py` deliberately omits. `text_codes` is registered not because we send it but
  because `MutationApi.create()` falls back to `spec.key` when no `resource_string` is
  given, so a key that is not one of its own aliases makes the generic API unusable for
  the resource — a `tests/test_models.py` invariant now guards that for every resource.
- **`create` only.** `update` is not modelled (the server allows just `c_title`, via the
  different alias `text_codes`); `delete` is disabled server-side.
- PK: `c_textid`, **server-assigned** (`max+1`) when `target.pk` is `{}`. The `target`
  key itself must still be present — a fully omitted `target` is a controller-level 422.
  `API.md` §13.2 also allows supplying an explicit `c_textid` (in `target.pk` *or* in
  `changes`), but **`models.py` deliberately blocks both paths**: `c_textid` is in
  `server_assigned_pk_fields` and is not in `create_fields`, so the only reachable
  behaviour is "let the server pick". Choosing an id by hand risks a `409` on a live
  collision and gains nothing.
- **`c_title_chn` is required by `models.py`, though not by the server.** The server
  accepts `changes: {}` on a create (`API.md` §4.3) and would mint a blank row at
  `max+1` — which then cannot be deleted, and cannot even be given a title afterwards,
  since `update` reaches only `c_title`. `ResourceSpec.required_create_fields` enforces
  it in both `staging.find_issues()` and `mutation_api`.
- **Create** whitelist (`API.md` §13.2): `c_title_chn`, `c_title`, `c_title_trans`,
  `c_text_type_id`, `c_text_year`, `c_text_nh_code`, `c_text_nh_year`,
  `c_text_range_code`, `c_bibl_cat_code`, `c_extant`, `c_text_country`, `c_text_dy`,
  `c_source`, `c_pages`, `c_url_api`, `c_url_api_coda`, `c_url_homepage`, `c_notes`,
  `c_title_alt_chn`.
- `mode` must be `direct`; `proposal` returns **501**.
- `person_id` is still required in the envelope (convention: `0` for a global table).
  Note the asymmetry in `API.md` §13.1 vs §13.2 — code-table *updates* always record
  `c_personid = 0` in `operations` regardless of what you send, but *creates* record what
  you sent.
- `requires_explicit_approval = True`: `staging.find_issues()` errors until a named human
  is in `approved_by`, and `batch_runner` forwards that into `meta.comment`.
- **Before proposing one, search for the title by pinyin as well as by Chinese
  characters.** Variant characters are the norm in CBDB's titles — 《俟庵集》 is stored as
  《俟菴集》 (菴 U+83F4), and a Chinese-title search for 俟庵 returns zero hits while the
  pinyin `Sian ji` finds it immediately. Searching one way only is how you create a
  duplicate of a book that already exists, which cannot then be deleted.

## 15. office (`OFFICE_CODES` + `OFFICE_CODE_TYPE_REL`) — an entity aggregate ⚠️

Not a code table and not person data: an **entity aggregate** (`API.md` §13.4) spanning
two tables, written only through the aggregate resource. Full design, the eight traps,
and the worked batch: **`docs/10-office-aggregate-design.md`**. What `models.py` encodes:

- **Resource string: `office`, and only `office`.** The server also accepts `offices` and
  `office-load`; both are deliberately unregistered, for two independent reasons.
  Server-side, `offices` is matched by the **postings** handler first
  (`PostingCreateHandler::supports()`), so a payload saying `offices` writes a person's
  appointment record instead of an office code. Client-side,
  `models.approval_gated_aliases()` is built from the gated specs' alias sets and
  `http_client._check_approval()` matches it against the raw `resource` string — so
  registering `offices` here would make **every routine postings write** demand an
  `approved_by`. `tests/test_models.py::test_gating_office_did_not_gate_the_postings_aliases`
  is the regression guard. See also §11.
- **PK: `c_office_id`** — server-assigned (`max+1`) on create, a known pre-existing value
  on update. Registered in `server_assigned_pk_fields`, so staging requires it to be
  *absent* on create and *present* on update ("never invented").
- **Fields are the aggregate's semantic short names, not column names**:
  `name`, `name_alt`, `translation`, `translation_alt`, `pinyin`, `pinyin_alt`,
  `dynasty_code`, `type_ids`, `source_id`, `pages`, `notes`. The server also accepts the
  `OFFICE_CODES` column names (`c_office_chn`, `c_dy`, `c_source`, …); we register only
  the semantic set so there is one spelling per field. `dynasty_label` is **not**
  registered although the server accepts it: it resolves through `VariantLabelMap` and, on
  a normalized-key collision, keeps the **smallest** `c_dy`. Send `dynasty_code`.
- **Required on create *and* update**: `name`, `type_ids`, `source_id`, `dynasty_code` —
  the aggregate shares one validator (`ResolvesOfficeAggregateInput`) between the two
  operations, unlike every person resource where an update may carry a single field.
  Encoded as both `required_create_fields` and `required_update_fields`.
- **`update` is a FULL-ROW OVERWRITE, not §7's PATCH.** Any *optional* field absent from
  `changes` is written as `NULL` — with one carve-out worth knowing: `pinyin` and
  `pinyin_alt` are **derived from the corresponding Chinese** when omitted
  (`OfficeImportService::officeColumns()` calls `buildPinyin()`), not nulled. The client
  demands all eleven fields regardless, so this never changes what we send; it changes
  what you should expect if you ever read a row written by another client. `full_overwrite_update=True` makes `validate_changes()`
  demand every field in `update_fields` — a value or an explicit `null` — so "I forgot
  `notes`" becomes a validation error instead of silent data loss, and clearing a field
  has to be said out loud where a reviewer sees it.
- **`type_ids` is shape-checked** (`list_fields`): a non-empty list of non-empty strings.
  These are **varchar node ids and leading zeros are significant** (`"06"`, not `6`); a
  bare scalar is the plausible mistake and the server would not reliably reject it.
- **`delete` is not modelled.** Supported server-side and guarded by
  `409 c_office_id: referenced_by_postings`, but nothing here needs to remove an office
  code and a narrower surface is the point.
- **`requires_explicit_approval = True`** (rule 12). ⚠️ But do **not** reuse
  `text_codes`' rationale: an office row **is** deletable while unreferenced, so a
  mistake is recoverable — until something references it. The refusal messages in
  `staging.py` and `http_client.py` still assert the code-table wording ("no delete
  path", "no way to undo it"), which is wrong for this resource; see
  `docs/10-office-aggregate-design.md` §4.
- **The server has NO duplicate-name guard on create** — `allocateNextId()` then
  `insert()`, no name lookup — so a re-run mints a second permanent row with the same
  name. `preflight.assert_office_create_is_not_a_duplicate()` is the client-side guard,
  run by `batch_runner` before any office create. It must be **live**: `AGENTS.md`'s
  snapshot rule names "does this row already exist" as precisely what the weekly build
  may never decide. It blocks on a same-dynasty exact-name match only — the same office
  name legitimately recurs across dynasties (`知州` exists separately for Tang, Yuan,
  Ming and Qing).
- **`/api/v2/get` cannot read this resource** (`MutationReadService` covers 13 person
  resources plus `nianhao`), so rule 11's read-back goes through
  `GET /api/select/search/office` and `batch_runner.fetch_current_values()` reports
  "couldn't fetch" for an aggregate proposal — expected, not a bug.

## Source citations

- `app/Support/CompositePrimaryKey.php` (`SCHEMAS` const) — authoritative PK schema
  per table.
- `app/Services/Mutations/MutationHandlerRegistry.php` — full handler roster.
- Each resource's `allowedFields()` in its `*CreateHandler.php`/`*MutationHandler.php`
  (or, for `sources`, `BiogSourceRepository::MUTABLE_COLUMNS`) is the authoritative
  create/update whitelist. Delete handlers never define their own whitelist — PK
  match is sufficient.
