# AGENTS.md — cbdb-inputter-agent

This repo is an authorized API client that submits biographical records into
`cbdb-online-main-server` (the CBDB online data-entry system) on behalf of an
authenticated CBDB user, in place of manually clicking through the web UI. Any agent
(Claude Code or otherwise) working in this repo must follow the rules below.

Background reading, in order: `docs/00-target-system-brief.md` (facts about the target
system — auth, API, audit logging), `docs/01-implementation-plan.md` (this repo's
architecture and milestones), `docs/03-extraction-review-workflow.md` (source-text →
staging-file → human-review pipeline), `docs/04-field-whitelists.md` (per-resource
allowed fields), `docs/05-testing-strategy.md` (mocking/fixture conventions).

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
   PK — `possessions` (`c_possession_record_id`) and `postings`/`offices`
   (`c_posting_id`). Never try to allocate or predict these client-side; read them
   back from the server's create response. See `docs/04-field-whitelists.md`.
7. **Person before sub-resources.** Never submit a sub-resource
   (`altnames`/`addresses`/`kinship`/etc.) referencing a `person_id` that hasn't been
   created yet in this run or confirmed to already exist via `GET /api/v2/get`.
8. **Local audit log is append-only.** Never delete or rewrite a `logs/*.jsonl` file.
   It exists specifically so a human can reconstruct what this agent attempted, even if
   the target server's own log has a gap or the request never arrived.

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
