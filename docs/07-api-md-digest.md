# `API.md` Digest — the target system's own API contract

**This document is a *derived* summary, not a source of truth.** The source of truth
is the target system's own `API.md`:

| | |
|---|---|
| Canonical URL | <https://github.com/cbdb-project/cbdb-online-main-server/blob/develop/API.md> |
| Local path | `${CBDB_ONLINE_MAIN_SERVER_REPO_DIR}/API.md` (see `.env.sample`; `Config.online_main_server_repo_dir`) |
| **Synced against** | `origin/develop` commit `fd747aba` (2026-08-18 00:39 +0800), `API.md` blob `948585d1` |
| Upstream length at sync | 2667 lines (v2 chapters 1–14, plus a "舊版 API 文檔" v1 appendix) |

The user has stated `API.md` **will keep being updated**. Treat every claim below as
carrying that sync stamp: if the stamp is old, re-verify before relying on it. This
file exists so an agent can answer "what does the API actually allow" without reading
2667 lines every session — not so it can skip reading upstream when the answer matters.

**Precedence between this repo's three reference docs**, when they disagree:
upstream `API.md` > this digest > `docs/04-field-whitelists.md` / `models.py` >
`docs/00-target-system-brief.md`. `docs/04` is *the client's own contract* (what
`models.py` encodes and enforces); this digest is *the server's* contract. If they
diverge, the digest is right about the server and `docs/04` + `models.py` are the
things that need fixing — never the other way round. `docs/00` is a source-reading
snapshot that predates the published spec.

## 0. Re-sync procedure (run this when `API.md` may have changed)

```bash
cd "$CBDB_ONLINE_MAIN_SERVER_REPO_DIR"
git fetch origin develop
git diff HEAD origin/develop -- API.md        # or: git diff <last-synced-sha> origin/develop -- API.md
```

Then: read the diff, update **this file's** sync stamp and any affected section, update
`AGENTS.md` if a *hard rule* changed, update `docs/00-target-system-brief.md` /
`docs/04-field-whitelists.md` if a field/endpoint fact changed, and note the sync in
`docs/02-review-log.md`. Never edit the target repo — it is read-only to us
(`.env.sample`'s wording on `CBDB_ONLINE_MAIN_SERVER_REPO_DIR`).

If no local checkout is configured, fetch the raw file instead:
`https://raw.githubusercontent.com/cbdb-project/cbdb-online-main-server/develop/API.md`.

## 1. Constraints that bind *this* client (the operative part)

These are the parts of `API.md` that our code and our agents must actually obey.
`AGENTS.md` restates the non-negotiable ones as hard rules; this section is the
"why", with chapter references into upstream.

### 1.1 Write rate: ≤ 1 request/second, **serialized** (`API.md` §1.3)

> 寫入請求（`create`／`mutate`／`delete`／`batch_mutate`）：每秒不超過 1 次，且務必序列化 …
> 這是**整支客戶端的總速率**：不是每個端點各 1 次、不是每個資源各 1 次、不是每個執行緒各 1 次。

- "Serialized" means *wait for the previous response before sending the next* — not
  "fire on a 1-second metronome". A `batch_mutate` can run for seconds; a metronome
  would stack requests server-side.
- Our `RateLimiter` (`http_client.py`) is a fixed-interval limiter, so
  `CBDB_MAX_REQUESTS_PER_MINUTE=60` ⇒ exactly the 1 req/s spacing. Serialization is a
  *separate* guarantee and is implemented explicitly by `RateLimiter.slot()`, the
  context manager `_request` wraps the actual send in: it holds a lock for the whole
  request (so a concurrent caller can't send mid-flight) and stamps the clock on
  **completion**, not on start — so the interval runs from the previous *response*,
  which is what upstream asks for. A merely "sleep 1s then send" limiter would let a
  3-second request be followed immediately by the next one.
  **Consequence: 60 is a ceiling, not a default to tune upward.** Raising
  `CBDB_MAX_REQUESTS_PER_MINUTE` above 60 violates the upstream write contract as long
  as one limiter is shared by reads and writes. If read throughput ever becomes the
  bottleneck, the fix is a *separate* read limiter, not a bigger shared number.
- The `web` group (all `/api/v2` write endpoints + `get`) has **no application-layer
  throttle on the write rate**, so exceeding 1 req/s will not produce a `429` — nothing
  tells you you are over. That is *not* the same as "these endpoints never 429": the
  failed-auth gate in §1.4 below is scoped to **all endpoints** and blocks before
  authentication, and a reverse proxy or WAF can return `429` at any time. Keep handling
  `429` with exponential backoff regardless.
- `GET /api/v2/get` is exempt from the write rate (read-only, and not in the `api`
  group either).

### 1.2 Bulk writes: bigger batches, not faster requests (`API.md` §1.3, §10)

`POST /api/v2/batch_mutate` takes up to **500** rows, hard cap. Recommended per-batch
sizes are cost-driven, and are explicitly *conservative starting values, not benchmark
results*:

| Case | Suggested rows/batch |
|---|---|
| `mode=proposal` (any resource) | ≤ 150 |
| `mode=direct`, ordinary sub-resource (altnames, addresses, texts, events…) | 50–150 |
| `mode=direct`, kinship / associations (mirror row + its audit_log) | 20–50 |
| `mode=direct`, entity aggregates (office / social-institution) | 20–50 |

Two traps:
- **Non-atomic `batch_mutate` returns HTTP 200 even when every row failed** — success
  is in the body (`ok`, `summary`), not the status code.
- A request can be killed by a deployment execution-time limit (30 s PHP
  `max_execution_time` observed in production 2026-08 — an observation, not a
  guarantee). In non-atomic mode the already-processed rows **stay committed** but you
  get no `results`. Do **not** blind-resend the batch — reconcile with `/api/v2/get`
  or `GET /api/v2/operations` first.

**Status in this repo:** `batch_mutate` is *not* implemented (`mutation_api.py` sends
one row per request). That is compliant and safe, just slower — at 1 req/s a batch
costs about one second per row, unremarkable at the batch sizes seen so far (single-digit
proposals). Adopting `batch_mutate` would be a deliberate milestone, with the
200-status-code trap and the partial-commit reconciliation path handled explicitly —
not a drop-in speedup.

### 1.3 Headers: `Accept` required; never send `Origin`/`Referer` (`API.md` §1.1, §1.4)

- `Accept: application/json` is **required, not advisory**. Without it, middleware-level
  failures (401/403/419/429) come back as an HTML error page, and unauthenticated
  requests get a `302` to `/login` instead of a JSON `401`.
  ✅ `http_client.py::_headers()` already sends it unconditionally.
- If `Origin` or `Referer` matches `SANCTUM_STATEFUL_DOMAINS`, the optional-auth
  middleware treats the request as first-party and **ignores the Bearer token
  entirely** → `401`. Server-side callers must simply not set these headers.
  ✅ `requests` doesn't set them for us; the rule is "never add them", not a code change.

### 1.4 Never retry authentication failures (`API.md` §1.3, upstream `fd747aba`)

New as of 2026-08-18: a dedicated gate counts **failed Bearer-authenticated attempts
per source IP, 60/minute**, and blocks *before* authentication runs — so an expired
token turns into `429 Too Many Attempts.` rather than `401`, with a longer backoff.

- Counted: requests carrying `Authorization: Bearer …` that fail (401, or a redirect to
  the login page when `Accept` isn't JSON). Not counted: credential-less requests, or
  successful authentications.
- Counted **per source IP, not per account** — a broken client behind an institutional
  NAT will block *other people's* Bearer clients from the same egress IP.
- ✅ `http_client.py` raises `AuthenticationError` on 401 without retrying, **and**
  `batch_runner.py` treats 401/403 as a whole-batch abort (`_ABORTING_ERRORS`) rather
  than isolating it per record — otherwise one dead token would produce one failed-auth
  attempt *per proposal*, spending N slots of a 60/minute budget shared with everyone
  else on the same egress IP. Keep both: this is a shared-blast-radius issue, not just a
  wasted request.
- ✅ `HttpClient.get(..., public=True)` sends no credentials at all, so the public lookup
  endpoints (§2.1) can't be turned into failed-auth attempts by a stale token.

### 1.5 Empty string ≡ null; "unknown" is an explicit sentinel (`API.md` §1.4, §4.4, §9)

Global middleware runs `TrimStrings` + `ConvertEmptyStringsToNull` over the JSON body,
so `""` and `null` are the same request to the server. To say "unknown" in a code /
foreign-key / PK column you must send the sentinel **explicitly**:

| Kind | Sentinel | Example |
|---|---|---|
| numeric code / id | `0` | all 10 `ENTRY_DATA` PK columns; `ASSOC_DATA`'s `c_kin_code`/`c_kin_id`/`c_assoc_kin_code`/`c_assoc_kin_id` |
| year | `-9999` | `ASSOC_DATA.c_assoc_first_year` |
| text (source title) | `[n/a]` | `ASSOC_DATA.c_text_title` |

PK completeness is checked **before** normalization, so omitting a PK column or sending
`""` is a `422`, never a silent "unknown". Some resources also accept `-999` and
normalize it to `"0"`, but that's per-resource — send the sentinel above and don't rely
on it.

**On `create`, sentinel columns default to `"0"` even when the key is absent entirely**
(§9 preamble) — omitting a field is not the same as leaving it null.

### 1.6 `mode` defaults to `direct` — always send it explicitly (`API.md` §4.2)

Omitting `mode` writes to the live database immediately for any account with direct-write
permission. Our client always sends `"mode": "direct"` explicitly (per `AGENTS.md` rule 1),
which is the right habit; never let it become an omitted default.

Role matrix: admins / experts / ordinary users get both modes; **crowdsourcing accounts
get `proposal` only** (`direct` → `403`). Our token's account is direct-capable.

### 1.7 `ok: true` does not mean your field was written (`API.md` §4.6 warning)

Unknown/blacklisted fields are **silently dropped** (not `422`) on these paths:

- `basicinformation` `create` **and** `update`
- `postings` `create`, `possessions` `create`
- `sources` `create` / `update`

So a typo'd column name on a person-create returns `200 ok:true` with the field simply
missing from the database. One exception worth knowing while debugging: if `changes`
contains **nothing but** dropped fields, so the filter leaves an empty set,
`basicinformation` does return `422 changes: no_supported_fields` (§9.1) — the silent
drop only hides a *partially* wrong payload, which is the more dangerous case anyway. **Verify writes by reading back `result.row` or calling
`/api/v2/get`** — don't trust `ok: true` alone. This is exactly what our client-side
whitelist in `models.py` is for: it catches the typo before the request is sent, on the
paths where the server would not.

Related: `possessions` and `postings` `create` ignore `target.pk` **entirely** (the
surrogate id is always server-assigned), so a client-chosen id there is silently
discarded rather than rejected.

### 1.8 Proposal-mode PK squatting (`API.md` §4.2) — relevant even to a direct-write client

A `pending` **or `rejected`** create-proposal squats its PK and makes any later `create`
on that PK `409 pending_proposal_exists` — regardless of who filed it, and there is **no
API to withdraw or amend a proposal** — `POST /api/v2/proposals/{operation}/resubmit`
is the only endpoint that would, and it needs CSRF + a session, so Bearer clients get
`419`. (`POST /api/v2/relationship/opposite-edges` is CSRF-gated too, but it has
nothing to do with proposals: §12.6 defines it as a *read-only* probe of what the
mirror row currently looks like.) If we ever hit that `409`, the only fix is a human working
inside the web UI. Do not treat it as retryable.

## 2. Surface beyond the 13 resources this client models

None of this is wired into `models.py`, and most of it we deliberately don't use — but
it is what the API can do, written down so a future request ("add this book title",
"record a person merge") doesn't start from zero. `docs/00-target-system-brief.md`
already *names* `merged-person` and the code-table CRUD as out-of-scope additions; what
follows is the detail behind those names — plus the lookup endpoints in §2.1, which were
not previously recorded anywhere and are the genuinely new capability here.

### 2.1 Read-only lookup endpoints — how to resolve names → codes (`API.md` §14.1, §14.4)

This is what makes source-text coding (place names, offices, book titles…) possible
without a database dump. **All public, no auth**, in the `api` group (600 req/min).

| Endpoint | Shape | Use |
|---|---|---|
| `GET /api/v2/texts?ids=1,2,3` | v2 envelope: `ok`/`data`/`meta` (`meta.missing_ids`!) | `c_textid` → book title. **Batch**, order follows `ids`, misses listed in `meta.missing_ids` |
| `GET /api/v2/texts/{textId}` | `ok`/`data`; `404` if absent | single text |
| `GET /api/select/{table}` | **bare array of rows** | whole small code table: `dynasty`, `nianhao`, `ganzhi`, `ethnicity`, `choronym`, `household`, `appttype`, `assumeoffice`, `officecate`, `parentstatus`, `measure`, `possact`, `birole`, `topic`, `occasion`, `role`, `range`, `altcode`, `biogaddr` |
| `GET /api/select/search/{table}?q=…` | **Laravel paginator** (`{current_page, data:[…], total,…}`) | keyword search: `addr`, `assoccode`, `assocpair`, `biog`, `entry`, `event`, `kincode`, `kinpair`, `office`, `officetype`, `pinyin`, `socialinst`, `socialinstaddr`, `socialinstcode`, `status`, `text`, `textauthor`, `textperson` |
| `GET /api/code/addr` | Laravel paginator | address code lookup |
| `GET /api/name` (`GET` or `POST`) | Laravel paginator | find persons by name/conditions |

Response-shape exceptions to watch for: `search/kinpair` and `search/assocpair` return
**bare arrays** like the whole-table endpoints (not paginators), and `search/pinyin`
returns **plain text**.

**Client support (implemented):** `HttpClient.get(..., public=True)` sends *no*
`Authorization` header — required here, since a stale token would fail these public
reads *and* spend the shared per-IP failed-auth budget from §1.4. All four path
prefixes are in `_KNOWN_READ_ONLY_PATHS`, so `_check_mutating_flag` fails closed on
them. Because `get()`'s contract is a dict, a non-object body comes back wrapped as
`{"raw": <body>}` — so a lookup caller must handle **three** shapes: a paginator object
(rows under `"data"`), `{"raw": [...]}` (bare array), and `{"raw": "..."}` (plain text).
Bulk lookup responses are also summarized before they reach `logs/*.jsonl`
(`PUBLIC_RESPONSE_LOG_MAX_ROWS`), since that log is append-only (`AGENTS.md` rule 8) and
a whole code table would bloat it permanently; `/api/v2/*` traffic is still logged in
full.

⚠️ Upstream's own caveat: these serve the site's UI, **their response format is not
guaranteed stable**, and they are not v2 endpoints. Treat them as a *lookup aid for a
human-reviewed coding pass*, never as something a submission silently depends on: a
code that a lookup produced still has to be reviewed by a human before it reaches a
`changes` payload. Prefer `GET /api/v2/texts` (a real v2 endpoint) over
`/app/codes/text-title/{textId}` for id→title.

### 2.2 `TEXT_CODES` can be created through the API (`API.md` §13.2)

A missing book title (`c_textid`) is **not** a hard blocker anymore:

- `POST /api/v2/create` with `resource: "text-codes"`, `mode: "direct"` only
  (`proposal` → `501`), `person_id` required (convention: `0` for global code tables,
  but note §13.1's asymmetry — code-table *updates* always record `c_personid = 0` in
  `operations` regardless of what you send, while *creates* record what you sent).
- `target` **must be present** — send `"target": {"pk": {}}` to let the server assign
  `c_textid = max+1`. Omitting `target` entirely is a controller-level `422`.
- Writable: `c_title_chn`, `c_title`, `c_title_trans`, `c_text_type_id`, `c_text_year`,
  `c_text_nh_code`, `c_text_nh_year`, `c_text_range_code`, `c_bibl_cat_code`, `c_extant`,
  `c_text_country`, `c_text_dy`, `c_source`, `c_pages`, `c_url_api`, `c_url_api_coda`,
  `c_url_homepage`, `c_notes`, `c_title_alt_chn`.
- Alias asymmetry: **create** takes `text-codes`/`text_codes`/`textcodes`; **update**
  takes `text_codes` only (and only `c_title`). Safest: create with `text-codes`,
  update with `text_codes`.
- **Deletion of any code table is disabled.** The refusal is table × *mode*, not table
  alone (§13.3): the two writable tables refuse with `403` **when `mode=direct`**; every
  other table, *and* any `mode=proposal` delete including on those two, gets `501`.
  Either way there is no delete path.

That last point is why creating a code row is a **higher-stakes** write than creating a
person row, not a lower one. Precisely: a wrong row is **un-deletable**, and only
*partly* correctable — §13.1 lets `text_codes` `update` exactly one column, `c_title`
(the romanized title). The Chinese title `c_title_chn`, and every other field the create
accepted, are frozen the moment the row exists, while staying globally visible and
referenceable by any person record. See `AGENTS.md`'s hard rule on this.

Other code tables (`nianhao`, `office_codes`, `dynasties`, `choronym_codes`,
`ethnicity_tribe_codes`, `ganzhi_codes`, `addr_codes`, …) support **`update` only**, and
only for a couple of pinyin/name columns each (§13.1) — `create`/`delete` → `501`.

### 2.3 Entity aggregates: `office`, `social-institution` (`API.md` §13.4)

New office codes and new social institutions must go through the aggregate resources
(`office`, `social-institution`), not by writing `OFFICE_CODES` directly. Two things
make these dangerous to touch casually:

- **The aggregate `update` is full-overwrite, not PATCH** — any optional field you omit
  is written as `null` (e.g. dropping `name_alt`/`pages` wipes them). Read the current
  row and resend it in full.
- `offices` (plural) as a resource string hits the **postings sub-resource** handler
  first, not the office entity. Use `office` (singular) for the entity. This is a
  sharper version of the warning already in `docs/04-field-whitelists.md` §11.

Referential guards: deleting an office still referenced by postings → `409
c_office_id: referenced_by_postings` with a `reference_count`; renaming a referenced
institution → `409 name: rename_blocked_while_referenced`.

### 2.4 `merged-person` resource (`API.md` §9.14)

`MERGED_PERSON_DATA`, PK `c_personid` + `c_merged_from_personid`; `create`/`delete` only
(no `update`), and `/api/v2/get` does **not** support it — write-only from our side.
Not currently in `docs/04-field-whitelists.md`'s 13-resource roster, and not wired into
`models.py`. Only relevant if we're ever asked to record a person merge.

### 2.5 `GET /api/user` (`API.md` §14.2)

Bearer-authenticated; returns the token's account. The `name` is exactly what gets
stamped into `c_created_by` / `c_modified_by`, and the `id` is what
`GET /api/v2/operations?editor=` wants. Useful as a pre-flight "which account am I about
to write as" check before a production run — but read the two flags correctly:

- **`is_admin` is a 4-valued role code, not a boolean**: `0`=ordinary, `1`=expert,
  `2`=**crowdsourcing**, `3`=system admin. Direct-write capability is `is_admin != 2`.
  Reading it as a boolean inverts the answer for `0`, the most common value.
- **`is_active` is useless as a check**: the endpoint returns `403` outright for an
  inactive account, so in any `200` response it is always `1`.

## 3. Corrections / confirmations for our existing docs

Checked `API.md` §9 field-by-field against `docs/04-field-whitelists.md` and
`models.py`. The whitelists, composite PKs, sentinel columns, and server-assigned
surrogate PKs all match across the 13 resources we encode. **One real contradiction**,
plus a set of additions and sharpened points:

**One contradiction found and fixed in this same pass — `sources` aliases.**
`docs/04-field-whitelists.md` used to state twice that `sources` has *no* aliases.
`API.md` §4.5/§9.13 say the asymmetry runs **both ways**: `create`/`update` accept only
`sources`, but `delete` *also* accepts `source` and `biog_source_data` — so `docs/04`
recorded only the restrictive half. It has been corrected (quick-reference table and
§13). No behavioural impact either way: `models.py`'s `delete_aliases={"sources"}` is a
safe subset of what the server accepts.

The rest are additions, not conflicts:

| Topic | `docs/04` said | `API.md` adds |
|---|---|---|
| `possessions` create | never send the surrogate id | `target.pk` is ignored wholesale — a sent id is discarded silently, not rejected |
| `basicinformation` update | `c_name*` immutable | plus: `c_mingzi_chn`/`c_mingzi` can't be *emptied* if currently non-empty; `c_index_year` ∈ [-3000, 3000]; `c_death_age` ∈ [0, 200]; 13 FK columns write `null` (not `"0"`) on empty |
| `basicinformation` names | — | `c_name_chn` is **derived** from `c_surname_chn` + `c_mingzi_chn`; edit the parts, not the whole |
| `altnames` | 3-key PK, legacy 4-key stripped | `c_alt_name_chn` gets **variant-character substitution**, and since it's a PK column, `result.pk` (not what you sent) is the row that exists |
| `events` addr-only update | separate code path | that path writes **neither `operations` nor `audit_log`** — the one known audit gap in `/api/v2/*` |
| `kinship` vs `associations` mirror back-fill | same mirror family | **Not symmetric, and §9.8 alone is misleading — read §12.2/§12.4.** `associations.update` back-fills a missing mirror row whenever any `*_pair` field is sent. `kinship.update` does *not* back-fill in general — **except** on the "pair-only" repair path (`changes` holds `c_kinship_pair` and **no** `KIN_DATA` column), which *does* back-fill. See the expansion below. |
| assoc `*_pair` pseudo-fields | stripped before validation | **`associations`' three pair fields are not validated at all** — a bogus code is silently written into the mirror row. `kinship`'s `c_kinship_pair` *is* validated (422, `message` only, no `errors`). |
| `sources` | `c_pages` optional in PK | writable side treats empty `c_pages` as `""`; **`/api/v2/get` does not** — reading such a row back needs different handling |

None of these require a `models.py` change; several rows are agent-behavior warnings,
and the `basicinformation` range checks are worth adding to client-side validation if we
ever touch those fields.

**Expanding the mirror back-fill row, because it is the one with a live blast radius.**
`API.md` §9.8's one-liner ("與 `associations` 不同，`kinship` 的 `update` **不會**補建缺失的
鏡像列") is true only of an *ordinary* update. §12.2's table and §12.4 both carve out an
exception that §9.8 never mentions:

> `kinship` update ｜ 一般情況**不補建**，只同步已存在的那一列；例外是「只送
> `c_kinship_pair`、沒有任何 `KIN_DATA` 欄變更」的修復路徑，那條會補建

So a `kinship` update whose `changes` is *only* `c_kinship_pair` is not a narrow edit of
one row — it is a mirror-repair request that will **insert** a row under the other
person if none exists. This repo has already written `kinship` against production
(`data/processed/2026-07-17-*`), so treat any pair-only kinship write as a two-person
change and `GET` both directions first, exactly as `AGENTS.md`'s reverse-pair section
already requires for content fields.

Two more details from §12.2/§12.4 worth carrying: a forward code with **no** authoritative
reverse in the code table still gets a mirror row — with the reverse code written as
sentinel `0` (未詳) and **no divergence detection at all**, so the other person silently
acquires an "unknown relationship" row; and `meta.force` on a multi-candidate drift
converges only the **first** candidate, leaving the rest for a human.

## 4. What we deliberately still don't use

- `POST /api/v2/batch_mutate` — see §1.2.
- `mode: "proposal"` — our account is direct-capable and `AGENTS.md` rule 1 pins
  `direct`. Proposal mode would also *lose* the mirror-conflict detection (§4.7:
  "提案階段不做互逆鏡像偵測").
- `/api/v2/proposals/{operation}/resubmit`, `/api/v2/relationship/opposite-edges` —
  CSRF-gated, unusable from a Bearer client (`419`).
- `/api/operations/*` (the old crowdsourcing channel, §14.3) — no whitelist, no PK
  validation, **no `audit_log`**, unreliable status codes. Exactly the kind of path
  `AGENTS.md` rule 1 exists to keep us off.
- `POST /api/v1/user/login` (§14.2) — dead route, always `404`, but leaves a session
  cookie behind on success. Never call it.
- `/api/ai/*` (§14.5), `/api/mcp` (§14.6) — session-gated / read-only-different-protocol;
  not part of the write path.
