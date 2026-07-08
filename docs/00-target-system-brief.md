# CBDB Online Main Server — Target System Brief

Source: local checkout of `cbdb-project/cbdb-online-main-server` (develop branch) at
`C:\Users\sudos\OneDrive\document\GitHub\cbdb-online-main-server`, read directly on
2026-07-08. This is the reference brief for everything in `01-implementation-plan.md`.
Re-verify against the live repo before relying on this for anything security-critical —
it is a snapshot, not a live contract.

## 1. Tech stack

- Laravel 12, PHP ^8.2, `laravel/sanctum:^4.0` (`composer.json`).
- DB: MariaDB in production, SQLite for tests.
- Frontend (irrelevant to us except as a map of what the UI submits): Vue3/legacy +
  React19/Inertia2.

## 2. Authentication — use Sanctum Bearer tokens

- Two auth modes exist: session-cookie (web SPA) and **Sanctum Personal Access Token**
  (`Authorization: Bearer {token}`) — documented in
  `docs/API_AUTHENTICATION.md`.
- **There is no non-interactive "login → token" endpoint.** A human must log into the
  web UI once, go to `/profile`, and create a Personal Access Token there (shown once).
  This token is what our agent will use. It must be supplied by the user via `.env`,
  never generated or requested by the agent itself.
- Bearer-token requests hit `App\Http\Middleware\OptionalAuthentication`, which forces
  the `sanctum` guard and returns 401 on an invalid/expired token. No CSRF token is
  needed for Bearer-token calls to the v2 mutation endpoints (`VerifyCsrfToken`
  explicitly exempts `api/v2/create|delete|get|mutate`).
- The **legacy** `/basicinformation/...` resource routes are session-cookie + CSRF only
  (`$this->middleware('auth')`, default guard `web`) — a Bearer token does **not**
  authenticate against them. **Our agent must not use these routes.**
- Global `api` middleware group throttles at 600 req/min; no explicit throttle was found
  on `/api/v2/*` (registered under the `web` group) — treat this as "unspecified," not
  "unlimited," and self-throttle regardless.

## 3. API surface to use — `/api/v2/*` Mutation API only

All writes go through one JSON envelope, dispatched by resource name
(`App\Services\Mutations\MutationHandlerRegistry`):

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v2/create` | create a row |
| POST | `/api/v2/mutate` | update a row |
| POST | `/api/v2/delete` | delete a row |
| GET/POST | `/api/v2/get` | read a row back (for verification / idempotency checks) |
| POST | `/api/v2/relationship/opposite-edges` | mirror-relationship helper (kinship/assoc) |

Request shape:

```json
{
  "resource": "basicinformation | altnames | addresses | kinship | offices | assoc | entries | events | possession | socialinst | sources | statuses | texts",
  "mode": "direct",
  "operation": "create | update | delete",
  "person_id": 12345,
  "target": { "pk": { "...": "composite or single key columns" } },
  "changes": { "field": "value" },
  "meta": { "comment": "optional free text" }
}
```

- `mode` must be `"direct"` for our agent — `"proposal"` mode queues a row for human
  review instead of writing immediately, and is not what we want for authorized direct
  entry (and `BiogMainCreateHandler` returns `501` for proposal mode on person-create
  anyway).
- `authorizeDirect()` requires the authenticated user to be `isActive()` and
  `canWriteDirectly()` (not a crowdsourcing-tier account). The token owner's account
  must have this permission or every direct-mode call will fail with 403.
- Required call order: create the person (`resource: basicinformation`) **before**
  any sub-resource row that references `person_id`. Every sub-resource create must set
  `target.pk.c_personid` == `person_id`.
- `c_personid` is **client-assigned, not server-generated**. `BiogMainCreateHandler`
  validates it is nonzero, not already taken, and within `max(existing c_personid) +
  10000`. Our agent must pick IDs deliberately (or read `GET /api/v2/persons` /
  `/api/v2/get` first) rather than guessing.
- Duplicate composite PKs return `409 target.pk conflict`. Kinship/association writes
  can also trigger mirror-relationship conflicts (`409`/`422`) requiring
  `meta.force` or manual resolution — never blind-retry these.
- Field whitelists differ per resource (`app/Services/Mutations/<Resource>*Handler.php`).
  `c_created_by/date`, `c_modified_by/date` are server-set and rejected if sent by the
  client.

## 4. Audit logging — confirmed, but not universal

**Good news: the `/api/v2/*` mutation endpoints we're required to use are the
best-audited write path in the system.** For every `direct`-mode create/update/delete
through `AbstractPersonSubresourceCreateHandler` / `BiogMainCreateHandler` and their
update/delete counterparts:

1. The row write, an `operations` table insert, and an `audit_log` table insert all
   happen inside **one DB transaction** — for sub-resources via
   `app/Services/Mutations/AbstractPersonSubresourceCreateHandler.php:163-207`, and for
   `BIOG_MAIN` (person) writes via `app/Repositories/BiogMainRepository.php:353-380`
   (`BiogMainCreateHandler` delegates to this repository method), both calling into
   `app/Services/AuditLogService.php`.
2. `audit_log` records: `table_name`, `operation` (INSERT/UPDATE/DELETE), `actor_type`/
   `actor_id` (from `Auth::user()` — populated by our Bearer token), `operation_id`
   (ULID linking the `operations` row and any mirrored writes), `row_pk`/`row_pk_text`,
   and full `old_data`/`new_data` JSON snapshots. Append-only by design.
3. `c_created_by`/`c_modified_by` on the business row itself are stamped from
   `Auth::user()->name` by `ToolsRepository::timestamp()` — this **throws if
   unauthenticated**, so every successful write is attributable by construction.
4. `operations` table is also written and is what backs the public
   `GET /api/v2/operations` feed.

**Known gaps:** `docs/AUDIT_LOG_PROPOSAL.md`'s "Known Issues" section explicitly names
`BasicInformationController::Duplicate_Collateral_Info()` and unspecified "少量非
`/basicinformation` 模組" controller-centric paths as not fully converged onto
`audit_log`. The deprecated `routes/api.php` `v1` GET-based add/update/delete and
`Api\OperationsController@storeProcess` are **not** named in that document — their
audit-completeness is unconfirmed either way, not confirmed-gapped. We exclude them
from this agent's allowed endpoints anyway (see below) because they're legacy,
undocumented in `API.md`, and semantically wrong (GET-verb writes), independent of
whether they happen to log correctly today.

**Implication for this agent: only ever call `/api/v2/create`, `/api/v2/mutate`,
`/api/v2/delete` with `mode: "direct"`. Never call the legacy `/basicinformation/*`
web routes, the `v1` GET-based CRUD, or `Api\OperationsController` directly** — those
are the paths flagged as potentially not fully audited. Staying on `/api/v2/*` is what
makes every write from this agent traceable back to the token's user, with full
before/after payloads, without us needing to build any of our own server-side logging.

## 5. Data model (high-level)

- `BIOG_MAIN` — person table, PK `c_personid` (int, client-assigned per §3).
- `BIOG_ADDR_DATA` — composite PK `(c_personid, c_addr_id, c_addr_type, c_sequence)`.
- `KIN_DATA` — composite PK `(c_kin_code, c_kin_id, c_personid)`.
- Composite PK field order per table is centralized in
  `app/Support/CompositePrimaryKey.php` — treat as source of truth for what
  `target.pk` must contain per resource.
- `operations` and `audit_log` are the two audit-adjacent tables described in §4.

## 6. Operational constraints for a well-behaved client

- Besides the write endpoints, our agent may also call the two read-only, public
  `GET /api/v2/persons` and `GET /api/v2/operations` list endpoints (`API.md`) — e.g.
  to discover `max(c_personid)` before assigning a new person ID (§3).
  **Confirmed live (Milestone 7):** `GET /api/v2/persons`' pagination metadata is
  nested under a top-level `"pagination"` key (`total`/`per_page`/`current_page`/
  `last_page`/`from`/`to`) — **not** `"meta"`. Rows are server-ordered ascending by
  `c_personid` (`PersonListController::index()`'s `orderBy(...,'asc')`), so the
  highest existing ID is always on the *last* page — fetch page 1 to learn
  `last_page`, then fetch that page directly, rather than scanning every page.
- Self-throttle bulk writes even though no explicit limit was found on `/api/v2/*`
  (global `api` group elsewhere throttles 600/min) — respect `429` with backoff.
- Use `GET /api/v2/get` to check for existing rows before create, for idempotency.
  **Confirmed live (Milestone 7):** this endpoint requires the *same* envelope
  shape as the write endpoints — `resource`, `person_id`, **and** a nested
  `target.pk` object — sent as a JSON body (works on GET too; Laravel reads
  `$request->json()->all()` first, same as for POST). Flat query params alone are
  rejected with `422 缺少 target.pk`. A nonexistent row 404s (not a 200 with a
  null/empty `result`).
  **Also confirmed live:** the resource-alias list `MutationReadService` accepts
  for GET is not always identical to the create/update/delete alias lists in
  `docs/04-field-whitelists.md` — e.g. it accepts `"socialinstitution"` (no
  underscore) instead of `"socialinst"`, and additionally accepts `"source"`
  (singular) for `sources`. Always send the canonical resource key for GET calls
  rather than reusing a write-side alias.
- Handle `409`/`422` (conflict / mirror-relationship issues) as terminal for that
  record, surfaced to a human — do not auto-retry with different data.
- No CSRF dance needed for `/api/v2/*` Bearer-token calls.

## 7. Local dev setup (for pointing the agent at a non-production instance)

- `composer install && npm install`, copy `.env`, `php artisan migrate`,
  `npm run build`, `php artisan serve`. `APP_URL` defaults to `http://localhost`.
- Run `php artisan cbdb:rebuild-person-change-index` once after a fresh migrate.
- Sanctum stateful domains already include localhost variants — irrelevant to us since
  we deliberately use Bearer tokens, not cookies.

## Explicit unknowns (do not assume)

- No fully-audited server-side path exists for issuing tokens non-interactively — token
  bootstrap is manual (human logs in once, creates a token at `/profile`).
- Whether `/api/v2/*` has a dedicated rate limit is unconfirmed either way.
- Full per-resource field whitelists (`AltnameCreateHandler`, `PostingCreateHandler`,
  etc.) were not exhaustively read for every one of the ~13 sub-resources; read the
  specific handler file before wiring a new resource's field mapping into the client.

## Confirmed live (Milestone 7, 2026-07-08)

Everything below was previously a documented assumption/unknown and has now been
verified end-to-end against the user's local `cbdb-online-main-server` instance
(create → get → delete, for both `basicinformation` and an `addresses` sub-resource,
using the standing local test account):

- `/api/v2/create`'s `target.pk`-in-both-`target.pk`-and-`changes` design (§3's
  design note in `mutation_api.py`) is correct — a real create with the composite
  PK fields present in both places succeeds.
- The full create → read-back → delete (soft-delete for `basicinformation`, hard
  delete for sub-resources) cycle works exactly as documented in §3-4, including
  `c_created_by` being stamped from the token's associated user
  (`"CBDB Inputter Agent (local test)"`) and an `operation_id` being returned.
- `GET /api/v2/persons` and `GET /api/v2/get`'s exact required shapes (see §6) —
  the single biggest gap this brief had before Milestone 7.
