# Per-Resource Field Whitelists

Status: implemented (`src/cbdb_agent/models.py`, Milestone 3 — see
`docs/02-review-log.md`; all 13 resources cross-checked field-by-field against
the target repo during review). Read directly from
`cbdb-online-main-server`'s `app/Services/Mutations/*Handler.php` and
`app/Support/CompositePrimaryKey.php` on 2026-07-08 (see brief's caveat: this is a
snapshot, re-verify against the live repo before trusting it for anything
security-critical). This is the source of truth `models.py` (Milestone 3) should
encode — one dataclass/whitelist per resource, matching this document exactly.

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
| postings / offices | `postings`, `posting`, `offices`, `posted_to_office_data` | 2 | none (same list, incl. `c_office_id`) | **Yes** — `c_posting_id` |
| social_institutions | create/delete: + `socialinst`; **update: missing `socialinst`, see §12** | 4 | `c_personid` | No |
| sources | `sources` (no aliases) | 3 (`c_pages` optional) | none (identical create/update list) | No, but `c_textid`/`c_pages` re-keyable |

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
  `c_by_intercalary`, `c_by_nh_code`, `c_by_nh_year`, `c_by_range`, `c_by_yymm`,
  `c_by_yymm_day`, `c_by_day_gz`, `c_dy_intercalary`, `c_dy_nh_code`, `c_dy_nh_year`,
  `c_dy_range`, `c_dy_yymm`, `c_dy_yymm_day`, `c_dy_day_gz`, `c_death_age`,
  `c_death_age_range`, `c_fl_earliest_year`, `c_fl_ey_nh_code`, `c_fl_ey_nh_year`,
  `c_fl_ey_notes`, `c_fl_latest_year`, `c_fl_ly_nh_code`, `c_fl_ly_nh_year`,
  `c_fl_ly_notes`, `c_ethnicity_code`, `c_household_status_code`, `c_tribe`,
  `c_choronym_code`, `c_notes`, `c_self_bio`. Blocked always:
  `c_created_by`/`c_created_date`/`c_modified_by`/`c_modified_date`.
- **Update** whitelist *differs from create*: `c_personid`, `c_name_chn`, `c_name`,
  `c_name_proper`, `c_name_rm` become **immutable on update** (blocked, not just
  excluded) — you cannot rename a person via `/api/v2/mutate`; all other `BIOG_MAIN`
  columns are otherwise mutable.
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
  `c_source`, `c_pages`, `c_notes`, `c_sequence`, `c_alt_name_pinyin`,
  `c_alt_name_pinyin2`, `c_alt_name_pinyin3`, `c_alt_name_role`.
- **Update**: identical minus `c_personid`.
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
  `c_notes`, `c_supplement`, `c_text_year`.
- **Update**: identical minus `c_personid`.
- `c_textid`/`c_source` normalized (null/`''` → `'0'`).

## 11. postings / offices (`POSTED_TO_OFFICE_DATA`) — server-assigned surrogate PK

- Resource aliases: `postings`, `posting`, `offices`, `posted_to_office_data`.
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

- Resource string: `sources` only — no aliases, and unlike every other resource,
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

## Source citations

- `app/Support/CompositePrimaryKey.php` (`SCHEMAS` const) — authoritative PK schema
  per table.
- `app/Services/Mutations/MutationHandlerRegistry.php` — full handler roster.
- Each resource's `allowedFields()` in its `*CreateHandler.php`/`*MutationHandler.php`
  (or, for `sources`, `BiogSourceRepository::MUTABLE_COLUMNS`) is the authoritative
  create/update whitelist. Delete handlers never define their own whitelist — PK
  match is sufficient.
