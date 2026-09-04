# AGENTS.md — cbdb-inputter-agent

This repo is an authorized API client that submits biographical records into
`cbdb-online-main-server` (the CBDB online data-entry system) on behalf of an
authenticated CBDB user, in place of manually clicking through the web UI. Any agent
(Claude Code or otherwise) working in this repo must follow the rules below.

Background reading, in order: `docs/00-target-system-brief.md` (facts about the target
system — auth, API, audit logging), `docs/07-api-md-digest.md` (**the target system's
own `API.md`, digested — read this before any endpoint/field question**),
`docs/01-implementation-plan.md` (this repo's
architecture and milestones), `docs/03-extraction-review-workflow.md` (source-text →
staging-file → human-review pipeline), `docs/04-field-whitelists.md` (per-resource
allowed fields), `docs/05-testing-strategy.md` (mocking/fixture conventions),
`docs/08-review-interface-design.md` (the offline review page and the
`review.json` → `decisions.json` → `apply-review` round trip).

## The target system's API contract — where it lives, and keeping it in sync

The authoritative API specification is **the target system's own `API.md`**, not
anything in this repo:

- Canonical: <https://github.com/cbdb-project/cbdb-online-main-server/blob/develop/API.md>
- Locally: `${CBDB_ONLINE_MAIN_SERVER_REPO_DIR}/API.md` (path comes from `.env` —
  see `.env.sample`; exposed as `Config.online_main_server_repo_dir`). That checkout
  is **read-only to us** — never modify the target repo.
- Digested for this repo, with a sync stamp and a re-sync procedure:
  **`docs/07-api-md-digest.md`** (last synced against `origin/develop` `b2df35f5`,
  2026-09-04).

`API.md` **is under active, continuing revision** (§1.3's write-throttling contract and
the failed-auth rate cap were both added in the days before that sync). So:

- Before answering any "can the API do X / what fields does Y take" question from
  memory, check the digest's sync stamp; if the work is consequential (a production
  write, a new resource, a field you haven't used before), `git fetch origin develop`
  in that checkout and diff `API.md` against the stamped SHA first.
- When you do re-sync: update `docs/07-api-md-digest.md`'s stamp *and* the affected
  section, propagate anything that changes a hard rule into this file, propagate
  field/endpoint facts into `docs/00-target-system-brief.md` /
  `docs/04-field-whitelists.md`, and log the sync in `docs/02-review-log.md`.
- `docs/07-api-md-digest.md` is a *summary*. Where it and upstream `API.md` disagree,
  upstream wins and the digest is the thing that's wrong — fix it.
- For **reference data** (what a code means, what an address is inside), prefer the
  weekly SQLite snapshot over the API — see the next section, including the hard
  limit on what a snapshot may decide.

## The weekly CBDB SQLite snapshot — what it's for, and what it must never decide

CBDB publishes a full SQLite build of the database every week at
<https://huggingface.co/datasets/cbdb/cbdb-sqlite> (`latest.zip`, ~132 MB compressed,
~557 MB on disk). `src/cbdb_agent/snapshot.py` downloads it on demand, verifies it
against the `sha256` in its sidecar metadata, and opens it **read-only**.

- Location: `data/cbdb-sqlite/` inside this repo, gitignored. Override with
  `CBDB_SQLITE_DIR`; disable the download with `CBDB_SQLITE_AUTODOWNLOAD=false`.
  It lives in the repo rather than a user-cache directory so it is visible and can be
  removed by deleting the folder — but if your checkout is inside a synced folder
  (OneDrive/Dropbox), point `CBDB_SQLITE_DIR` outside it.
- What it buys: reference data and the joins the API cannot do in one call. An
  address's full parent chain through `ADDR_BELONGS_DATA`, an office's
  `OFFICE_TYPE_TREE` ancestry through `OFFICE_CODE_TYPE_REL`, book titles, reign
  periods — one local query instead of one rate-limited HTTP request per level. This
  is what puts a human-readable name next to every code in the review page.

**It is a weekly snapshot of a database that is written to continuously — including
by this agent. So it answers "what does this code mean", never "what is currently
true of this record".** Concretely, it must **never** be used for:

- `max(c_personid)` or any `c_personid` allocation (`person_id.py` stays on the live
  `GET /api/v2/persons`, per rule 6);
- a "does this row already exist" check before a create — a row added since the build
  is invisible in it, so the check can answer "not there" for something that is,
  which is exactly how you create the duplicate you were checking for;
- the current-value diff behind `preview.md` / `review.json` (that stays on
  `GET /api/v2/get`);
- anything else that gates a write.

For those, the live API is the only acceptable source, and its being slower is the
price of being right. `snapshot.snapshot_is_stale()` exists so a caller can tell the
user how old the build is instead of quietly trusting it.

## Hard rules

1. **Only call `/api/v2/create`, `/api/v2/mutate`, `/api/v2/delete`, `/api/v2/get`
   (with `"mode": "direct"` for the first three), plus the read-only, public
   `/api/v2/persons` and `/api/v2/operations` list endpoints (used e.g. to discover
   `max(c_personid)` before assigning a new one — see brief §3, rule 6 below).** Never
   call the legacy `/basicinformation/*` web routes,
   the deprecated `v1` GET-based CRUD in `routes/api.php`, or `Api\OperationsController`
   directly. The target system's own `docs/AUDIT_LOG_PROPOSAL.md` documents that some
   legacy controller-centric paths (e.g. `BasicInformationController::
   Duplicate_Collateral_Info()`) are not fully wired into `audit_log`; the `v1`
   GET-based routes and `Api\OperationsController` aren't named there one way or the
   other, so treat their audit-completeness as unconfirmed, not confirmed-safe. Either
   way, the `/api/v2/*` endpoints are the ones *confirmed* to write `audit_log` +
   `operations` inside one DB transaction — staying on them is what makes every write
   from this agent traceable back to the token's user. See brief §3–4.

   **Read-only lookup endpoints are additionally allowed, for coding source material
   into codes — reads only, never as part of a write payload without human review.**
   `API.md` §14.1/§14.4 documents a set of public, unauthenticated lookup endpoints
   that are how a book title, place name, office name or kinship term gets turned into
   the numeric code a `changes` payload needs. Allowed:
   `GET /api/v2/texts` and `GET /api/v2/texts/{id}` (`c_textid` → title),
   `GET /api/select/{table}` (whole small code table),
   `GET /api/select/search/{table}` (keyword search),
   `GET /api/code/addr`, and `GET /api/name`.
   Two more are allowed **only** as the no-snapshot fallback for office-type
   hierarchy: `GET /api/OFFICE_TYPE_TREE` and `GET /api/OFFICE_CODE_TYPE_REL`.
   `API.md` §14 names these as "still present, not documented here" — they ignore
   their parameters and dump the whole table (2.7k and 44k rows). Treat them as
   liable to vanish: `code_lookup.py` fetches each once and treats a failure as
   "no type tree for this office", never as an error. When the SQLite snapshot is
   available these are not called at all, which is the better path anyway.
   Also allowed, and necessary for a different job — **reading a person's current
   state before proposing changes to them**: `GET /cbdbapi/person?id=<N>&mode=json`
   (`API.md` §14.7). `/api/v2/get` can only fetch one row by its full composite PK, so
   it cannot answer "what does this person already have?" — which is exactly what you
   must know before writing (to avoid duplicating an existing row, and per rule 5's
   preference for human judgement over a blind retry). This endpoint returns the whole
   person in one read: `BasicInfo` plus `PersonSources`, `PersonSourcesAs`,
   `PersonAliases`, `PersonAddresses`, `PersonEntryInfo`, `PersonPostings`,
   `PersonSocialStatus`, `PersonKinshipInfo`, `PersonSocialAssociation`, `PersonTexts`
   (empty collections are stripped, so a missing key means "none", not "unknown").
   **`mode=json` is mandatory** — any other value, including omitting it or misspelling
   it, returns an **HTML page**, not JSON. Prefer it over
   `/app/basicinformation/{id}/summary` and `/app/basicinformation/{id}/tabs/{tabKey}`
   (§14.9), which are page-backing endpoints with no stability guarantee at all.
   Three conditions, all of them binding:
   (a) they must still go through `http_client.py` (rule 2 — they're rate-limited and
   audit-logged like everything else);
   (b) `/api/select/*`, `/api/code/addr` and `/api/name` are **site-UI endpoints whose
   response shape upstream explicitly does not guarantee** (some return a bare array,
   some a Laravel paginator, `search/pinyin` returns plain text) — parse defensively,
   and never let a submission depend on one silently;
   (c) **a code that came out of a lookup is a *suggestion*, not an answer** — it goes
   into a staging proposal with its `source_quote`/`confidence` and gets human review
   before it is ever submitted. Anything else in `/api/*` that isn't named here or in
   the paragraph above stays off-limits, in particular the old crowdsourcing channel
   `/api/operations/*` (no whitelist, no PK validation, **no `audit_log`**) and
   `POST /api/v1/user/login` (**retired as of 2026-08 — it now always returns
   `410 Gone` and no longer verifies a password at all**. Before that it was worse than
   useless: a live, unthrottled password check that always 404'd afterwards and, because
   it runs on the *session* guard, would leave a logged-in session cookie behind first if
   the credentials happened to validate).
2. **Never bypass `http_client.py` for outbound requests.** All HTTP calls to the
   target system — reads included — must go through the shared client so local audit
   logging (`audit_log.py`) and rate limiting apply uniformly. Do not write a "quick"
   inline `requests.post(...)` anywhere else in the codebase.
3. **Never commit `.env` or any real token/credential.** `.env` is gitignored; only
   `.env.sample` (placeholders only) is committed. If you ever see a real-looking
   token in a diff, stop and flag it instead of committing.
4. **Respect the dry-run and production gates.** `CBDB_DRY_RUN=true` is the default and
   must remain the default in `.env.sample`. **`CBDB_CONFIRM_PROD` must equal the exact
   current value of `CBDB_API_BASE_URL`** before any mutating call is sent while
   `CBDB_DRY_RUN=false` — for every target host, not just a hardcoded production
   hostname string. This is deliberately URL-pinned rather than a plain boolean: if
   `CBDB_API_BASE_URL` is later changed (e.g. from a local dev server to production),
   `CBDB_CONFIRM_PROD` no longer matches and the gate re-locks automatically, so
   switching targets always forces a fresh, explicit confirmation of the *new* host —
   a one-time boolean flip would stay "confirmed" across a later silent URL change,
   which is the actual accident this gate exists to prevent. Do not add a way to skip
   this with a single flag or make the match fuzzy/case-insensitive on the URL.
5. **Never auto-retry a `409`/`422` conflict with modified data.** These indicate a
   real data conflict (duplicate PK, mirror-relationship issue) that needs human
   judgment — log it, surface it, move to the next record in a batch.
6. **`c_personid` is client-assigned, not server-generated.** Always validate a
   candidate ID (nonzero, not already taken, within `max(existing)+10000`) via
   `person_id.py` before sending a create — see brief §3. **Exception:** two
   sub-resources have their own, *server*-assigned surrogate ID in their composite
   PK — `possessions` (`c_possession_record_id`) and `postings`
   (`c_posting_id`; the server also answers to the alias `offices` here, but never use
   it — see rule 12(b)). Never try to allocate or predict these client-side; read them
   back from the server's create response. See `docs/04-field-whitelists.md`.
7. **Person before sub-resources.** Never submit a sub-resource
   (`altnames`/`addresses`/`kinship`/etc.) referencing a `person_id` that hasn't been
   created yet in this run or confirmed to already exist via `GET /api/v2/get`.
8. **Local audit log is append-only.** Never delete or rewrite a `logs/*.jsonl` file.
   It exists specifically so a human can reconstruct what this agent attempted, even if
   the target server's own log has a gap or the request never arrived.
9. **`CBDB_MAX_REQUESTS_PER_MINUTE=60` is a ceiling, not a tuning knob.** `API.md` §1.3
   caps *writes* at **1 request/second, serialized, across the whole client** — not per
   endpoint, per resource, per thread, or per machine. 60/min is exactly that limit, and
   `RateLimiter.slot()` is what makes it serialized: it holds a lock across the whole
   request and measures the interval from the previous *response*, not from the previous
   send — "wait for the previous response", not "fire on a metronome". One honest limit
   remains: the limiter is per-`HttpClient` **instance**, so two concurrent
   `python -m cbdb_agent` processes would each hold their own 1 req/s budget — don't run
   them concurrently. And nothing will 429 you for exceeding the *write rate*
   specifically (no application-layer throttle there), so this contract is enforced by
   us or not at all. That is not "these endpoints never 429" — the failed-auth gate in
   rule 10 covers all endpoints, and a proxy/WAF can 429 anytime, so keep handling 429
   with backoff. Never raise this number to speed up reads; add a separate read limiter
   instead.
10. **Never retry a 401, and treat one as a whole-batch stop.** As of `API.md` §1.3
    (upstream `fd747aba`, 2026-08-18), failed *Bearer-authenticated* attempts are counted
    **per source IP at 60/minute** and blocked before authentication even runs, turning a
    stale token into a `429` with a longer backoff. The counter is per-IP, not
    per-account: retrying a dead token behind an institutional NAT blocks other people's
    clients too. So a 401/403 is a property of the *credentials*, never of the record —
    `batch_runner.py` aborts the batch on either (`_ABORTING_ERRORS`) rather than
    isolating it per proposal, and public lookups use `get(..., public=True)` so they
    send no token to spend. Stop and get a new token.
11. **`ok: true` does not mean the field was written.** On `basicinformation`
    create *and* update, `postings` create, `possessions` create, and `sources`
    create/update, the server **silently drops** unknown or blacklisted fields and still
    returns `200 ok:true` (`API.md` §4.6). A typo'd column name on those paths is not an
    error, it's a missing value. `models.py`'s client-side whitelist is what protects
    these paths — it is enforced before every `create`/`update` in `mutation_api.py`, so
    never work around it. **Note the client does not yet verify writes for you**:
    `batch_runner.run_batch()` records a `200` as `status="success"` without inspecting
    `result.row`. After a real (non-dry-run) write that matters, read the row back
    yourself via `/api/v2/get` and compare — don't infer it from the exit code.
12. **Code-table and entity-aggregate writes are a different, higher risk class than
    person data — never do one without explicit, specific user approval.** This covers
    `text-codes` (new `TEXT_CODES` rows), `char-variant-map`, the `office`,
    `social-institution` and `text-entity` entity aggregates, and `merged-person`. What
    they share is
    **blast radius**: they are global reference data, referenced by potentially tens of
    thousands of person rows and visible to every other user, so a mistake is not
    confined to one record. Reversibility differs and it is worth knowing which you are
    touching: the **code tables have no delete path at all** (`403`/`501`, `API.md`
    §13.3), and a `TEXT_CODES` row is only partly correctable afterwards — just
    `c_title`, never `c_title_chn`. The **entity aggregates *do* support `delete`**
    (§13.4), guarded by `409` reference checks, so a mistake there is recoverable if
    nothing has referenced it yet. Either way: a missing book title or office code is
    something you **report to the user as a finding**, with the evidence; you create it
    only if they say to. It is never a gap you close on your own initiative to unblock
    a batch.
    Mechanics and traps: `docs/07-api-md-digest.md` §2.2–2.4.
    **How the gate is enforced.** `ResourceSpec.requires_explicit_approval` marks such a
    resource; `staging.find_issues()` then raises a **structural error** (not a
    "conflict", which is a normal mid-review state) unless that proposal carries an
    explicit `approved_by: <name of the human who decided>`. `batch_runner` forwards it
    into `meta.comment`, so the sign-off lands in the **server's** `operations` row too,
    not only in this repo. **Never fill in `approved_by` yourself** — it exists precisely
    to record that a human, named, made the call.
    Two such resources are modelled today:
    **`text-codes`** (create only; `update` is not modelled since the server only allows
    `c_title`, and `delete` is disabled server-side), and **`office`** (create + update,
    no delete — see `docs/04-field-whitelists.md` §15 and
    `docs/10-office-aggregate-design.md`). Two things about `office` that do not apply to
    the code tables: its `update` is a **full-row overwrite**, so an omitted field is
    written as `NULL` (the client refuses a partial payload — `full_overwrite_update`),
    and **the server has no duplicate-name guard on create**, so
    `preflight.assert_office_create_is_not_a_duplicate()` runs a *live* check before any
    office create and must never be replaced by a snapshot lookup.
    The rest (`char-variant-map`, `social-institution`, `text-entity`, `merged-person`)
    are still unmodelled, so a staging file naming one is rejected as an unknown alias —
    a safe outcome, but by absence rather than by design.
    If you model one, set `requires_explicit_approval=True` on it — and note that
    the refusal messages in `staging.py` and `http_client.py` still assert the
    *code-table* rationale ("no delete path", "no way to undo it"), which is false for
    the three aggregates: those **are** deletable while unreferenced (`API.md` §13.4).
    See `models.py`'s comment on `requires_explicit_approval`.
    **One more trap: near-identical strings mean entirely different resources.**
    `office` is the entity aggregate (needs approval) while `offices` resolves to the
    **postings sub-resource** (routine — and postings wins the server-side dispatch);
    likewise `social-institution` (hyphen, entity, needs approval) vs
    `social_institution` (underscore, the person sub-resource `BIOG_INST_DATA`, routine);
    and `text-entity` (the document aggregate, needs approval) vs `text`/`texts` (the
    person's `BIOG_TEXT_DATA` sub-resource, routine) vs `text-codes` (the bare code-table
    create, needs approval). Read the separator, and prefer the unambiguous spelling.
    A client-side consequence of the same collision: `approval_gated_aliases()` is built
    from the gated specs' alias sets, and `http_client` matches it against the raw
    `resource` string — so registering `offices` as an alias of a gated `office` spec
    would make **every routine postings write** demand an `approved_by`.

## Review workflow for changes in this repo

Per the project's process (see `docs/01-implementation-plan.md` §11): after finishing
a milestone, get a read-the-diff review agent to sign off with no serious issues, then
run `codex exec --dangerously-bypass-approvals-and-sandbox` (via `Write-Output "..." |`
to avoid stdin blocking, with `$env:HTTPS_PROXY`/`$env:HTTP_PROXY` set for proxy access)
as an independent second review, and resolve its findings too, before starting the next
milestone. Log both passes in `docs/02-review-log.md`.

## Git workflow

This repo is public (`github.com/cbdb-project/cbdb-inputter-agent`) with `main`
branch-protected: linear history is required, force-pushes and branch deletion on
`main` are blocked, and merge commits are disabled at the PR level (only squash or
rebase merge). Practical rules that follow from this:

- **Never push directly to `main`.** Branch, open a PR, merge via the GitHub UI
  (squash or rebase — either is fine, just not "create a merge commit").
- **Never `git merge` a feature branch into `main` locally and push the result** —
  that produces a merge commit, which GitHub will reject anyway (linear history is
  enforced), but don't rely on the rejection; branch + PR is the actual workflow.
- If your branch falls behind `main`, `git rebase origin/main` it before opening/
  updating the PR, rather than merging `main` into your branch.
- Delete-branch-on-merge is enabled — a merged PR's branch is cleaned up
  automatically; don't recreate it under the same name for unrelated work later.

## Local dev / testing

Point `CBDB_API_BASE_URL` at a local `cbdb-online-main-server` instance instead of
production whenever testing new code paths. The user's standing local instance is
running at `http://localhost:8000` (confirmed by the user 2026-07-08; this also
matches Laravel's generic `php artisan serve` default, brief §7) with a dedicated,
permanent test account (`cbdb-inputter-agent@local.test`, `canWriteDirectly()`-capable
— never delete it); `.env` is already pointed at it. Still always check `.env`'s
current `CBDB_API_BASE_URL` rather than hardcoding a port anywhere in code — it can
change. Never use a production token for anything other than deliberate,
user-confirmed production writes.

**The local instance is a full mirror of production data**, not a synthetic/empty
test DB — real historical `c_personid`s (e.g. 陳俊卿 10884, 陳文龍 15213) exist there
with their real rows. So when you need to look up or verify a real CBDB record for
a data-correction task: **try the local instance first** (it's already configured,
no token juggling needed) before assuming you need a separate production token or
asking the user for one. Two concrete gotchas that wasted a round-trip once already:
- If the local server seems unreachable, re-check with a plain `requests.get()` /
  `netstat` before concluding it's down and reaching for production — a transient
  connection hiccup looks identical to "not running."
- `MutationApi.get()` (unlike `create()`/`update()`/`delete()`) does **not**
  auto-merge `person_id` into `target_pk` — for a multi-field-PK resource (e.g.
  `kinship`'s PK is `c_personid`+`c_kin_id`+`c_kin_code`), you must include
  `c_personid` in `target_pk` yourself or the server 422s "缺少必要的複合主鍵參數"
  (this is different from the *staging-file* schema's `target_pk`, which
  deliberately excludes `c_personid` — see `staging.py`'s module docstring; that
  exclusion only applies to `Proposal.target_pk`, not to a direct `MutationApi.get()`
  call).

## Reverse-pair mirror sync (`kinship`, `associations` — check before every write)

**Discovered the hard way (2026-07-17):** updating `c_notes`/`c_source`/`c_pages`
(and, for `associations`, `c_assoc_first_year`/`c_assoc_last_year` too) on one
direction of a mirrored-pair resource **automatically overwrites the same fields
on the reverse-direction row**, server-side, in the same transaction. Confirmed in
`KinshipMutationHandler::afterDirectUpdate()` and
`AssociationMutationHandler::afterDirectUpdate()` (both call
`BiogMainRepository::sync*MirrorOnUpdate()` with the just-written row's new
content-field values; both declare a `CONTENT_CONFLICT_FIELDS` constant naming
exactly these fields). For `kinship` specifically: `c_kin_code=243` (10884→15213)
and `c_kin_code=62` (15213→10884) are **not two independently-editable rows** —
they're two views of one relationship, and the server keeps their content fields
in lockstep. Submitting two proposals in the same batch that intentionally write
*different* `c_notes` text to the forward and reverse row (as an earlier version
of this repo's own worked example in `docs/06-staging-preview-design.md` did) will
silently corrupt data: whichever proposal's mutate call runs *last* wins, and its
mirror-sync clobbers whatever the *other* proposal had just written — the server
returns `200 ok:true` for both, so nothing in the response signals the corruption.
The server does have a conflict-detection guard (`conflictBaselines()` /
`MirrorConflictException`, 409) for when the mirror row has genuinely diverged
from what it "should" be — but two same-batch writes racing each other on the
same pair don't trip it, because each write's own baseline is computed *after*
the other write's mirror-sync already ran.

**Before writing to `c_notes`/`c_source`/`c_pages` (or the assoc year fields) on a
`kinship` or `associations` row, always `GET` *both* directions of the pair
first**, then:
- **Same content, or one side blank** → safe to proceed with a single-direction
  `update`; the mirror sync will propagate it to the other side automatically.
  Don't also submit a second proposal for the reverse direction with the same
  intent — it's redundant at best (the server will 422 `no_effective_changes` if
  the mirror-sync already made it a no-op) and duplicated-write risk at worst.
- **Different content on the two sides** → stop and get a human decision on how
  to merge them (concatenate, pick one, rewrite) — never let whichever proposal
  happens to submit last silently overwrite the other's genuinely-different
  content.
- If the field already has content and the task is to *append* rather than
  replace: preserve the existing text **byte-for-byte** in the new value (verify
  with an exact prefix match before submitting — the existing text may contain
  characters that look like plain spaces but aren't, e.g. U+00A0 non-breaking
  space instead of U+0020; a naive "same-looking" retype can silently normalize
  these and count as an unintended edit to content you were only supposed to
  leave alone).

**A `kinship` update whose `changes` contains *only* `c_kinship_pair` is not a
narrow edit — it can create a row under the other person.** `API.md` §9.8 says
flatly that `kinship.update` (unlike `associations.update`) never back-fills a
missing mirror row. That is true only of an *ordinary* update: §12.2's table and
§12.4 both carve out the "pair-only" repair path — `changes` holding
`c_kinship_pair` and **no** `KIN_DATA` column — which **does** back-fill. Reading
§9.8 alone and concluding "kinship never back-fills" is how you'd unknowingly
insert a brand-new `KIN_DATA` row under someone you weren't editing. Treat any
pair-only kinship write as a two-person change: `GET` both directions first, same
as for the content fields above. Two related traps from the same chapter: if the
forward code has no authoritative reverse in the code table, the mirror row is
still created with the reverse code as sentinel `0` (未詳) and **no divergence
detection at all**; and `meta.force` on a multi-candidate drift converges only the
*first* candidate, leaving the rest. See `docs/07-api-md-digest.md` §3.

**Also:** a live *write* against production (`CBDB_DRY_RUN=false`), even from a
correctly-argued ad-hoc verification script using `http_client.py`/`mutation_api.py`
directly rather than a raw HTTP call, may be blocked by Claude Code's own
permission classifier — it doesn't know the script is using the sanctioned
client under the hood. When that happens, don't try to work around it: build a
proper `data/staging/<batch_id>/proposal.yaml` and submit it through
`python -m cbdb_agent submit --staging <path>` instead. This isn't just a
workaround for the block — it's the actually-correct path per this repo's own
design (staged, previewed, reviewed, audited), and an ad-hoc script was cutting
a corner that shouldn't have been cut for a real production write in the first
place.
