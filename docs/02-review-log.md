# Review Log

Running record of the review-agent + `codex exec` review pass required for each
milestone before moving on (see `01-implementation-plan.md` §11).

Each entry: milestone, date, review-agent findings + resolution, codex findings +
resolution, sign-off.

---

## Milestone 1 — Scaffolding

### Review-agent pass 1
Findings: prod-write gate was a bypassable hostname denylist; brief falsely cited
AUDIT_LOG_PROPOSAL.md for a v1-routes gap it never mentions; plan's inline `.env`
template missing `CBDB_CONFIRM_PROD`; wrong file citation for `BiogMainCreateHandler`'s
audit transaction; four empty dirs missing `.gitkeep`.
Resolution: all fixed. Verified by a second review-agent pass (4/5 immediately
correct; one follow-up — AGENTS.md rule 1 still had the false citation — caught and
fixed).

### codex exec pass 1
Findings:
1. Boolean `CBDB_CONFIRM_PROD` was sticky across a later `CBDB_API_BASE_URL` change
   (switch to prod would inherit an earlier non-prod confirmation).
2. `person_id.py`'s planned use of `GET /api/v2/persons` contradicted AGENTS.md's
   endpoint allowlist, which didn't mention it.
3. `.gitignore` too narrow (`.env.*`, `*.env`, `.env.bak`, non-JSONL log exports not
   covered).
4. `.env.sample`'s comment said "production-looking host" while other docs said "any
   host" — contradictory.

Resolution:
1. Redesigned `CBDB_CONFIRM_PROD` from boolean to URL-pinned (must equal the exact
   current `CBDB_API_BASE_URL`) — a base-URL change now automatically re-locks the
   gate. Applied in `.env.sample`, `AGENTS.md`, `docs/01-implementation-plan.md`.
2. Added `/api/v2/persons` and `/api/v2/operations` (public, read-only) to the
   allowed-endpoints list in `AGENTS.md`, `docs/00-target-system-brief.md` §6, and
   `docs/01-implementation-plan.md` §9.
3. Broadened `.gitignore`: `.env.*` (with `!.env.sample` exception), `*.env`, `*.bak`,
   `logs/*` (was `logs/*.jsonl`) with `!logs/.gitkeep`.
4. Rewrote `.env.sample`'s `CBDB_CONFIRM_PROD` comment to match the URL-pinned,
   any-host design consistently across all three files.

### Review-agent pass 2 (after adding docs/03-extraction-review-workflow.md)
Findings: 3 stale "milestone N" references left over from renumbering
(01-implementation-plan.md §12, and 03-extraction-review-workflow.md's own two
"Milestone 3.5" mentions); repo-layout tree in §2 missing the new doc; §3's inline
`.gitignore` snippet not updated after the earlier broadening fix; new `data/staging/`
directory not covered by `.gitignore` the way `data/inbox`/`data/processed` are.
Resolution: all fixed and re-verified by a follow-up Explore-agent check (5/5 pass).

### codex exec pass 2 (final)
Findings:
1. `.env.sample`'s `CBDB_CONFIRM_PROD` comment said the gate applies "regardless of
   dry-run," while `AGENTS.md` and the plan correctly scoped it to "whenever
   `CBDB_DRY_RUN=false`" — wording drift on the exact safety rule.
2. `docs/03-extraction-review-workflow.md` justified choosing YAML partly by
   "supports comments" while planning to implement with `PyYAML`, which does not
   round-trip `#` comments — an internally inconsistent claim.

Resolution:
1. Reworded `.env.sample`'s `CBDB_CONFIRM_PROD` comment to explicitly say "whenever
   CBDB_DRY_RUN=false", matching AGENTS.md/plan exactly.
2. Reworded §2.2 to justify YAML by readability/block-scalars instead of comment
   preservation, and made explicit that the design doesn't depend on comment
   round-tripping (every "why" is a structured field, not a bare `#` comment) — so
   plain `PyYAML` remains sufficient and the claim is now accurate.

Sign-off: codex reported "milestone numbering is aligned, the `.gitignore` snippet
matches the real file, the `/api/v2/persons`/`/api/v2/operations` allowlist matches
across brief/plan/AGENTS, and the extraction-review workflow otherwise fits the rest
of the repo design" with only the 2 findings above, both now fixed. **Milestone 1
(scaffolding + docs) is closed.**

## Milestone 2 prep — local test env + remaining design docs

Set up ahead of Milestone 2 (core HTTP client) at the user's request:

- Created a permanent local test account (`cbdb-inputter-agent@local.test`, ID 722,
  `regular` role, active — satisfies `canWriteDirectly()`) on the user's local
  `cbdb-online-main-server` instance via `php artisan cbdb:manage-user`. **Will not
  be deleted** — standing account for all future local testing.
- Generated a Sanctum personal access token for it via `php artisan tinker` and wrote
  it directly into `.env` via a PHP script that never printed the token value to
  stdout/chat (the Claude Code permission classifier correctly flagged and blocked an
  earlier attempt that would have echoed it — the write-directly-to-file approach was
  used instead). `.env`'s `CBDB_API_BASE_URL` set to `http://localhost:8080` (port
  identified via `netstat`, pending the user's confirmation — see open task).
- Wrote the remaining design docs the user asked for: `docs/04-field-whitelists.md`
  (per-resource field whitelists, read from all ~13 mutation handler files in the
  target repo), `docs/05-testing-strategy.md`, `skills/cbdb-data-entry/SKILL.md`,
  `requirements.txt`, `requirements-dev.txt`; extended `docs/03`'s staging design
  with a concrete pydantic schema (§2.5).
- Resolved `01-implementation-plan.md` §12's three open questions (account/token,
  local instance, structured-input format — the last resolved as "design now at the
  generic-internal-schema level, build a source-specific adapter later if/when a
  real structured source appears").

### Review-agent pass (this batch)
Findings: `staging.py`'s pydantic schema had no field to identify *which* existing
row an update/delete targets on multi-field-PK resources; `SKILL.md` cited an
unplanned `validate --input` CLI subcommand; `docs/05`'s test plan didn't call out
`docs/04`'s mirror-relationship exception types or server-assigned-PK read-back flow.
Resolution: added `target_pk` to the `Proposal` schema with rules for when it's
required; removed the invented subcommand reference from `SKILL.md`; expanded
`docs/05` with explicit mirror-exception and surrogate-PK-readback test requirements.
All 3 confirmed fixed by a follow-up Explore-agent check.

### codex exec pass (this batch)
Findings: field-whitelist validation didn't account for documented pseudo-fields
(`c_addr_id`, `c_kinship_pair`, etc.) that the server itself strips before its own
whitelist check — would have wrongly rejected valid proposals; `AGENTS.md` still said
the local instance was at `:8000` (stale, contradicting the `:8080` set up this
session); validation rule 6's wording said a surrogate PK is "never present in
`target_pk`" while the very next sentence required it there for update/delete — an
internal contradiction; `01-implementation-plan.md` still used the brief's shorthand
`assoc` resource name instead of `docs/04`'s canonical `associations` alias, and
`docs/05` was missing coverage for `basicinformation` soft-delete/immutable-name
behavior, `events`' address-only pseudo-field path, and `sources`' re-keyable PK.

Resolution: added an explicit pseudo-field allowance to validation rule 3; fixed
`AGENTS.md` to state `:8080` and to tell readers to check `.env` rather than assume a
port; reworded rule 6 to state the create-vs-update/delete distinction without
contradiction; fixed the resource-name list in `01-implementation-plan.md` §6 and
added a clarifying note to its inline `.env.sample` template disambiguating the
generic Laravel default from this repo's actual local target; added the 3 missing
coverage items to `docs/05`. A follow-up codex pass confirmed 3 of 4 fully fixed and
flagged one remaining wording-consistency nit (the `.env.sample` template comment vs.
`AGENTS.md`'s tone), which was then also fixed and verified.

Sign-off: **Milestone-2-prep docs are closed.**

### Correction (2026-07-08, post-commit)
User confirmed the local instance's actual port is `:8000`, not the `:8080` guessed
from `netstat` output alone (both ports happened to be listening; `:8080` was a
different, unrelated local service). Fixed in `.env` (`CBDB_API_BASE_URL`),
`AGENTS.md`, `docs/01-implementation-plan.md` §12 and its inline `.env.sample`
template comment, and `docs/05-testing-strategy.md` §1. No code existed yet to be
affected. Lesson: don't treat a `netstat`-identified port as confirmed without an
explicit user check — flagged as exactly that kind of open item last time, and it
did turn out to be wrong.

## Milestone 2 — Core client (config.py, audit_log.py, http_client.py, person_id.py)

First real Python code in the repo, plus a full `responses`/`freezegun`-based unit
test suite (45 tests) per `docs/05-testing-strategy.md`. Packaged with
`pyproject.toml` (src layout, `pip install -e .`).

### Review-agent pass
Findings: (1) `mutating: bool` on `HttpClient.post()`/`get()` was trusted blindly
with no cross-check against the actual endpoint — a future Milestone-3 wrapper bug
could silently skip both the dry-run and `CBDB_CONFIRM_PROD` gates; (2) two
live-write-gate tests lacked `@responses.activate`, so a regression moving the gate
check after the network call could make them silently attempt a real request instead
of failing; (3) dead `DryRunBlocked` exception class; (4) `RateLimiter`'s actual
algorithm (fixed minimum-interval) silently diverged from docs/01's "token-bucket"
wording; (5) a `requests.RequestException` was re-raised with zero retries, unlike
5xx responses which retry — undocumented asymmetry; (6) `freezegun` was an unused
dev dependency; (7) `config.py`'s `load_dotenv(override=False)` precedence was
undocumented.

Resolution: added `MutatingFlagMismatch`/`_check_mutating_flag()` as a fail-closed
guard on known mutating/read-only paths; added `@responses.activate` + zero-calls
assertions to the two gate tests; removed `DryRunBlocked`; corrected docs/01 §5's
wording to describe the real algorithm; added a `NetworkError` class with the same
retry/backoff as 5xx, with test coverage; added a `freezegun`-based timestamp test to
`test_audit_log.py`; added an explanatory comment to `config.py`. All 7 confirmed
fixed by a follow-up Explore-agent pass; full suite (45 tests) still green.

### codex exec pass
Findings: (1) `config.py`'s `load_dotenv(override=False)` — flagged again, this time
as a real bug rather than just an undocumented footgun, since a stale exported env
var could keep sending live writes to an old host even after `.env` was edited back
to something safer; (2) `http_client.py` always logged `request_payload=json_body`,
but `get()` never sets `json_body` (real input lives in `params`) — GET calls
(including `person_id.py`'s existence/discovery lookups) were being audit-logged
with no payload; (3) most status-code tests didn't assert an `audit_log.record()`
entry was actually written, so a regression skipping logging on those paths could
leave the suite green. Core safety logic (dry-run + `CBDB_CONFIRM_PROD` gate always
run before any mutating call; 409/422 never retried) was independently verified
clean by codex tracing the code by hand.

Resolution: flipped to `load_dotenv(override=True)` so `.env` is authoritative, with
an expanded comment explaining why (opposite of typical dotenv advice, deliberately);
computed a `logged_payload` that falls back to `params` when `json_body` is `None`,
used consistently across all three `audit_log.record()` call sites; added
`read_audit_records()` assertions to the GET, 401, 409, 429-exhausted, and
network-error-exhausted tests. A follow-up codex pass confirmed all 3 fixed with no
new issues introduced; full suite (45 tests) still green.

Sign-off: **Milestone 2 is closed.**

## Milestone 3 — Mutation wrappers (models.py, mutation_api.py)

Encodes all 13 resources from `docs/04-field-whitelists.md` as data
(`RESOURCE_SPECS`), plus generic `MutationApi.create/update/delete/get()` and named
convenience wrappers for `basicinformation`/`addresses`/`kinship`
(docs/01-implementation-plan.md milestone 3 scope). 76 tests total (31 new).

### Review-agent pass
Findings: (1) `postings`' `pseudo_fields` wrongly included `c_addr_cleared` (that
belongs to `events`, not `postings`) — a real whitelist transcription error that
would let an invalid field through client-side validation; (2)
`validate_target_pk_for_create()` only rejected server-assigned PK fields, never
checked required-field completeness or unknown fields, unlike the update/delete
version; (3) `update_immutable_fields` was dead code — the generic whitelist check
always fired first with a less specific message; (4) `MutationApi.get()` lacked a
`resource_string` override for symmetry with the write methods; (5) the `sources`
resource's field list looked ambiguous against docs/04's prose — verified directly
against `BiogSourceRepository.php` in the target repo and confirmed correct (the
`c_personid`-in-changes handling is intentionally stricter on our side than the
server's tolerant-if-equal behavior, which is safe).

Resolution: fixed `postings.pseudo_fields` to `{"c_addr"}`; added completeness/
unknown-field checks to `validate_target_pk_for_create()`; reordered
`validate_changes()` so the immutable-field check runs first with a clear message;
added `resource_string` param to `get()`; added regression tests for all of the
above (including a dedicated postings-pseudo-field test and an events-pseudo-field
test to prevent the two being confused again). All 4 confirmed fixed by a follow-up
Explore-agent pass; full suite green.

### codex exec pass
Cross-checked all 13 `RESOURCE_SPECS` entries against `docs/04-field-whitelists.md`
exhaustively — no further transcription errors found (the postings fix from the
review-agent pass held up). One new finding: `create()` merged `target_pk` into
`changes` via `dict.setdefault()`, meaning a caller passing conflicting values for
the same PK field in `target_pk` vs. `changes` (e.g. `target_pk={"c_office_id": 1}`,
`changes={"c_office_id": 2}`) would silently send `changes`' value with no error —
an internally inconsistent envelope reaching the server, undetected client-side.

Resolution: `create()` now raises `FieldWhitelistError` if a PK field appears in
both `target_pk` and `changes` with different values, before ever building the
envelope. Added a regression test. Full suite green (76 tests).

Sign-off: **Milestone 3 is closed.**

## Milestone 4 — Extraction staging (staging.py)

Implements the pydantic schema and 7 validation rules from
`docs/03-extraction-review-workflow.md` §2.5: `Proposal`/`Conflict`/
`ConflictOption`/`StagingBatch` models, YAML load/save, `find_issues()`/
`validate_for_submit()`, `submittable_proposals()`, `topological_submission_order()`,
and `resolve_target_pk()`. Added `find_spec_by_alias()` to `models.py` (staging
proposals carry a human/agent-written alias string, not necessarily the canonical
`RESOURCE_SPECS` key). 104 tests total (28 new).

### Review-agent pass
Findings: (1) a `continue` after a resource-alias lookup failure skipped the
unresolved-conflict check for that same proposal, hiding an unrelated real problem;
(2) no cycle detection for `person_id` references (a mutual or self-reference
would pass `find_issues()`/`validate_for_submit()` cleanly and only fail later,
confusingly, in `topological_submission_order()`); (3) `find_issues()` and
`topological_submission_order()` disagreed on whether a numeric-looking string
`person_id` matching a sibling `id` counts as a dependency; (4) the documented
`resolution: "defer"` value had no implementation; (5) `StagingError` didn't carry
the structured `Issue` list, unlike other error classes in the codebase; (6-7) two
minor doc/type consistency notes (YAML block-style not preserved on save, `changes`
typed looser than docs/03's literal schema to accommodate pseudo-fields).

Resolution: moved the conflict check to run unconditionally before the alias
lookup; added `_find_person_reference_cycles()` (DFS-based) plus an explicit
self-reference check, both called from `find_issues()`; factored out a shared
`_sibling_dependency()` helper used by both the cycle check and
`topological_submission_order()` so they agree; added `submittable_proposals()`
implementing "defer"; added `StagingError.issues`; added explanatory comments for
the two minor items. All fixes verified by a follow-up Explore-agent pass; full
suite green (103 tests at that point).

### codex exec pass
Finding: `submittable_proposals()` only excluded proposals *directly* resolved as
"defer" — a proposal depending (via sibling reference) on a deferred proposal would
still be included, meaning `validate_for_submit()` accepts the batch but
`topological_submission_order()` (which now defaults to `submittable_proposals()`)
would raise a confusing "dependency cycle or unresolved sibling reference" error at
submission time for a batch that had already been declared safe.

Resolution: `submittable_proposals()` now does a fixpoint transitive closure —
excludes the directly-deferred proposals, then repeatedly excludes anything
depending on an already-excluded proposal until nothing new is found. Added a
regression test for a person-create deferred while a sub-resource still references
it. A follow-up codex pass independently traced 3+-level chains, mid-chain deferrals,
and multiple independent deferred chains and found no further bug. Full suite green
(104 tests).

Sign-off: **Milestone 4 is closed.**

## Milestone 5 — CLI + batch submission (batch_runner.py, cli.py)

Implements `python -m cbdb_agent validate/submit --staging|--input`. Added
`batch_runner.py` (submission engine: `allocate_person_id`, per-proposal execution,
failure isolation) and `staging.load_input_batch()` so both `--staging` (YAML,
Milestone 4) and `--input` (already-structured JSON) converge on one `StagingBatch`
representation and one execution engine, rather than duplicating submission logic.
123 tests total (19 new + `tests/conftest.py`).

### Review-agent pass
Findings: (1) `run_batch()` only caught `CbdbApiError`, so a `FieldWhitelistError`
from `mutation_api.create()` (e.g. a `target_pk`/`changes` value mismatch on a
shared PK field — a case `find_issues()` doesn't check) would crash the *entire*
batch instead of being isolated to one proposal; (2) two independent `person_id:
"NEW"` proposals in the same batch could be allocated the *same* `c_personid`,
since nothing tracked IDs already claimed earlier in the same run; (3) `cli.py`
would silently overwrite a previous attempt's `results.json`/archived source file
if the same `batch_id` was submitted twice; (4) `find_issues()` never validated
that `person_id: "NEW"` is only meaningful on a `basicinformation` create — a
malformed proposal would pass validation and get a misleading
`skipped_dependency_failed` status at runtime instead of a clear upfront error;
(5) `cli.py` returned exit code `1` for every failure type, making "nothing was
attempted" indistinguishable from "some records failed"; (6) a latent risk that a
future test omitting `--env` could silently load the repo's real root `.env`.

Resolution: broadened the catch to `(CbdbApiError, FieldWhitelistError)`; added
`already_claimed` tracking to `allocate_person_id()`, passed as
`set(person_id_map.values())`; `_archive_batch()` now creates a numbered
`-attempt2`/`-attempt3` directory instead of overwriting; added a hard
`find_issues()` error for `"NEW"` used outside a `basicinformation` create;
introduced distinct exit codes (`EXIT_LOAD_ERROR=2`, `EXIT_VALIDATION_ERROR=3`,
`EXIT_CONFIG_ERROR=4`, `EXIT_SUBMISSION_FAILURES=1`); added an autouse
`tests/conftest.py` fixture that raises loudly if `load_dotenv` is ever called
without an explicit path during tests. All 6 confirmed fixed by a follow-up
Explore-agent pass; full suite green (121 tests at that point).

### codex exec pass
Findings: (1) **High** — dry-run was not actually network-free: `allocate_person_id()`
always made real `GET /api/v2/persons`/`GET /api/v2/get` calls to discover a real
ID, even though nothing was ever going to be created — a "preview only" dry run
would still hit the configured host (including production, if pointed there) purely
for ID-discovery reads. (2) **Medium** — `_archive_batch()`'s character-level
sanitizer left a `batch_id` of `".."` unchanged, which could resolve via normal
filesystem dot-segment handling to escape `data/processed/`.

Resolution: added a public `HttpClient.dry_run` property; `allocate_person_id()`
now checks it first and returns an obviously-fake negative placeholder ID with
zero network calls when true; `_archive_batch()` now falls back to a literal
`"_batch"` directory name if the sanitized `batch_id` is empty or consists only of
dots. Added regression tests for both. A follow-up codex pass confirmed both fixed
with no new issues (one accepted-by-design note: archiving is relative to cwd).
Full suite green (123 tests).

Sign-off: **Milestone 5 is closed.**

## Milestone 6 — Finalize skill wiring (SKILL.md)

Rewrote `skills/cbdb-data-entry/SKILL.md` to describe the CLI surface actually
implemented in Milestones 2-5 (it previously described a not-yet-built design):
real exit codes, dry-run/archiving/`-attemptN` behavior, `"defer"` semantics, and
the input JSON shape.

### Review-agent pass
Findings: (1) a real bug surfaced while fact-checking the docs — `staging.
load_input_batch()` used raw dict indexing (`record["resource"]`, etc.), so a
structured-input record missing a required field raised an uncaught `KeyError`
instead of the clean `StagingError` (→ `EXIT_LOAD_ERROR`) SKILL.md claimed;
(2) SKILL.md claimed a human-supplied `c_personid` goes through `person_id.py`'s
validation, but that module is only ever invoked via `batch_runner.
allocate_person_id()` for `"NEW"` proposals — a human-supplied ID is passed
through as-is; (3) SKILL.md attributed the "never call an external LLM API"
constraint to `AGENTS.md`, but it actually comes from `docs/03-extraction-review-
workflow.md` §2.4 (`AGENTS.md`'s 8 rules don't mention LLMs at all).

Resolution: `load_input_batch()` now checks for missing `resource`/`operation`/
`person_id` and raises a clean `StagingError` naming the record and the missing
fields; added a regression test. Corrected SKILL.md's `c_personid` bullet to
accurately describe the two different code paths; split the LLM-API constraint
into its own correctly-attributed section. All 3 confirmed fixed by a follow-up
Explore-agent pass; full suite green (124 tests).

### codex exec pass
Independently cross-checked every remaining factual claim in SKILL.md (exit
codes, dry-run/archive behavior, defer semantics, input JSON shape, person_id
handling, both attribution fixes) against the current code — reported clean, no
further findings. Full suite green (124 tests).

Sign-off: **Milestone 6 is closed.**

## Milestone 7 — End-to-end dry-run + local live validation

Ran the client for real against the user's local `cbdb-online-main-server`
instance (`http://localhost:8000`, standing test account) — dry-run first, then a
real create → read → delete cycle for both `basicinformation` and an `addresses`
sub-resource. This is the milestone that exists specifically to catch wrong
assumptions unit tests (mocked HTTP) can't catch, and it did: **two real bugs
were found live**, beyond the usual review-agent/codex loop.

### Live findings (found by actually calling the real server, not by review)
1. `GET /api/v2/persons` pagination metadata is nested under `"pagination"`, not
   `"meta"` as originally assumed — `get_max_person_id()` was silently unable to
   ever find `last_page` and would have looped until hitting the old
   `max_pages` cap and raising `PersonIdError` on every real call.
2. `GET /api/v2/get` requires the *same* envelope shape as the write endpoints —
   `resource`, `person_id`, **and** a nested `target.pk` — sent as a JSON body
   (works on GET; Laravel reads the JSON body first). The old flat
   `params={"resource": ..., **target_pk}` design (missing `person_id` entirely)
   404'd/422'd on every real call. A nonexistent row 404s, not a 200 with null.
3. (Bonus, read directly from `MutationReadService.php` while fixing #2):
   `GET /api/v2/get`'s resource-alias list is a *separate* definition from the
   write-side alias lists in `docs/04-field-whitelists.md` — e.g. it accepts
   `"socialinstitution"` (no underscore) instead of `"socialinst"` for
   `social_institutions`, and additionally accepts `"source"` (singular) for
   `sources`.

Fixed: rewrote `person_id.py`'s pagination/response parsing entirely; added a
`json_body` parameter to `HttpClient.get()` and a `NotFoundError` class mapped to
404; rewrote `MutationApi.get()` and `is_person_id_taken()` to send the full
envelope. Confirmed the fix live: a real `create_person()` + `create_address()` +
`get()` + `delete_address()` + `delete_person()` (soft-delete) cycle all
succeeded end-to-end, with `c_created_by` correctly attributed to the token's
user and an `operation_id` returned. `.env` was reverted to safe dry-run defaults
immediately after. Corrected `docs/00`, `docs/04`, and `docs/05`'s testing-ID
convention (a hardcoded "obviously fake" ID range turned out to be impossible
given the real `max(existing)+10000` ceiling) with these live-confirmed facts.

### Review-agent pass (on the fix)
Findings: (1) `get_max_person_id()`'s new "jump to last page" logic wasn't safe
against concurrent writes shifting the page count between the two requests —
could silently undershoot; (2) `mutation_api.py`'s module docstring was left
stale, still describing the target_pk/changes design as unconfirmed; (3)
`tests/test_batch_runner.py`'s mocks still simulated the old (wrong) 200/null
"not taken" shape instead of the confirmed-live 404; (4) no test asserted 404
maps to `NotFoundError` specifically; (5) `HttpClient.get()` accepted both
`params` and `json_body` with no guard, risking a silent audit-log gap.

Resolution: added a stability-check retry loop to `get_max_person_id()`; updated
the stale docstring; updated all `test_batch_runner.py` mocks to 404; added
`test_404_raises_not_found_error_specifically_no_retry` and tightened
`is_person_id_taken()` to catch `NotFoundError` specifically; added a `ValueError`
guard against `get()` receiving both `params` and `json_body`. All 5 confirmed
fixed by a follow-up Explore-agent pass.

### codex exec pass
Finding: the stability-check loop still returned immediately after fetching the
candidate last page without a post-fetch recheck — a concurrent insert between
the final page-1 read and the final last-page fetch could still return a stale
max, contradicting the docstring's own stated design.

Resolution: rewrote to a true "verify-after-fetch" pattern — fetch the candidate
last page, THEN re-fetch page 1 to confirm `last_page` didn't change during the
fetch, retrying against the fresh reading if it did. A follow-up codex pass
confirmed the specific reported race is closed, but correctly noted one
irreducible residual race remains (a new max landing on the same, not-yet-full
last page between fetch and recheck) — documented in the function's docstring as
an accepted, harmless limitation: `allocate_person_id()` always re-validates its
final candidate via `is_person_id_taken()` before use, so a stale-by-a-little max
can only waste an ID, never cause a real collision. Also added a defensive
`max_attempts >= 1` guard. Full suite green (129 tests).

Sign-off: **Milestone 7 is closed.**

## Final — README/docs update before publishing

Updated README.md (early-development notice, real CLI usage, status summary) and
`docs/01-implementation-plan.md` (all 7 milestones marked done, repo-layout block
and §7's CLI description brought in line with what was actually built) ahead of
publishing the repo.

### codex exec pass
Findings: `docs/03`/`04`/`05` still had stale `Status: draft, pending review`
headers, contradicting the "all milestones complete" framing; `01`'s repo-layout
block was out of sync (referenced a nonexistent `skills/.../scripts/` directory,
omitted `batch_runner.py` and several test files); §7's CLI description described
a "per-record slice" archiving behavior that isn't what was actually implemented
(the whole source file + a `results.json` gets archived, not per-record slices).

Resolution: updated the three stale status headers to `Status: implemented`;
corrected the repo-layout block to match the real file tree; rewrote §7 to
describe the actual `batch_runner.run_batch()`/`cli.py` behavior (shared
`StagingBatch` representation for both input paths, per-proposal failure
isolation, `-attemptN` archiving). One codex finding (README's "early-stage"
framing reading as inconsistent with "all milestones complete") was intentionally
not applied — the user explicitly asked for that notice to stay, since the
implemented code is still expected to change before wider use. Full suite green
(129 tests).

Sign-off: **Ready to publish.**

## Maintenance — target-repo sync check (2026-07-17)

~40 commits had landed in `cbdb-online-main-server` since this brief/whitelist docs
were last synced (2026-07-08). Full diff review of everything this client depends
on found: no breaking change to any of the 13 resources' field whitelists, PKs,
alias lists, or the mutation/read envelope shapes; a new additive
`POST /api/v2/batch_mutate` endpoint (not adopted, documented as a future option);
new unrelated resources outside our scope; and a `basicinformation`/`altnames`
character-variant-substitution behavior change plus an optional `notices` response
key (no code change needed — already tolerated) that doesn't affect this client
since we only use `mode: "direct"`.

One real finding: a **new "office entity" resource** (managing the `OFFICE_CODES`
reference table) was added whose handler claims the string `"offices"` — the same
alias our existing `postings` resource (`POSTED_TO_OFFICE_DATA`, a person's
appointment record — a completely different table) also accepted. Server-side
resolution is first-match-wins by registration order, and today's order still
favors postings, but that's incidental, not a contract.

### Fix
Removed `"offices"` from `models.py`'s `postings` alias sets (kept `"postings"`/
`"posting"`/`"posted_to_office_data"`); added a regression test
(`test_postings_rejects_offices_alias`); documented the collision and the other
sync-check findings in `docs/00-target-system-brief.md` and
`docs/04-field-whitelists.md`.

### Review-agent pass
Finding: the quick-reference table in `docs/04-field-whitelists.md` still listed
`"offices"` as an accepted alias with no caveat, contradicting the newly-added §11
warning that the client deliberately excludes it.

Resolution: updated the table row to note the server-vs-client distinction; also
added a pointer from `docs/00`'s illustrative JSON example (which still shows
`"offices"` as one of several server-valid resource strings) to the new sync-check
section, so a reader doesn't copy that example into using the ambiguous alias.

### codex exec pass
Independently re-verified the alias-collision claim and registration-order claim
directly against the target repo source (`MutationHandlerRegistry.php`,
`Office*Handler.php`, `Posting*Handler.php`) — confirmed accurate. Confirmed the
three docs (table, §11, JSON example) are now mutually consistent. Reported clean.
Full suite green (130 tests).

## Bug fix — missing `__main__.py`

Discovered when the user actually ran the documented `python -m cbdb_agent
validate --staging ...` command for real: every doc (README, `01-implementation-
plan.md`, `03-extraction-review-workflow.md`) documents this as the CLI entry
point, but the package never had a `__main__.py`, so it failed with "No module
named cbdb_agent.__main__; 'cbdb_agent' is a package and cannot be directly
executed". `cli.py`'s own `if __name__ == "__main__":` guard only fires for
`python -m cbdb_agent.cli`, not `python -m cbdb_agent` — a distinct, missing
file. Fixed by adding `src/cbdb_agent/__main__.py`, delegating to `cli.main()`.
Added `tests/test_main_entry_point.py`, a subprocess-based regression test
(`sys.executable -m cbdb_agent validate --input ...`) — the only kind of test
that actually exercises `-m`'s module-resolution behavior; every other CLI test
in this suite calls `cli.main()` in-process and would not have caught this.
Verified the new test fails without the fix (temporarily removed `__main__.py`,
confirmed the exact original error reproduces) and passes with it restored.

### Review-agent pass
No issues found. Confirmed no double-execution risk (importing `cli` as
`cbdb_agent.cli` never triggers `cli.py`'s own `__main__` guard), confirmed the
test's assertions (`returncode == 0` + expected stdout, not just absence of the
error string) rule out a false pass from an unrelated failure, confirmed
`pyproject.toml`'s `packages.find` correctly includes the new file, and found no
stale doc/comment anywhere claiming the command doesn't work.

### codex exec pass
Independently re-checked the same points (delegation correctness, no
double-execution, test false-pass risk, packaging). No must-fix issues. One
nice-to-have noted (the test doesn't separately assert delegation-vs-
reimplementation, given the actual code is a one-line delegation) — not acted
on. Full suite green (131 tests).

## Milestone 8 (design-only) — staging batch preview

Written after a real review friction point during actual use: reviewing a real
data-correction batch (a `KIN_DATA` `c_notes` update for a disputed kinship
relation between two historical CBDB persons) by reading raw YAML meant manually
cross-referencing nested `conflicts[].options[]` with no at-a-glance status, and
manually checking the current server value before trusting an "append to c_notes"
proposal — exactly the kind of check a tool should do automatically. Added
`docs/06-staging-preview-design.md` (design only, no code): a generated read-only
Markdown summary (status line, per-proposal conflict highlighting) plus an
optional best-effort live old→new diff for `update`/`delete` proposals, refreshed
by `validate --staging`. Added as an unchecked Milestone 8 to `docs/01-
implementation-plan.md` §10, and documented the review friction's root cause
(local instance is a full production-data mirror; `MutationApi.get()` doesn't
auto-merge `person_id` into `target_pk`) as an explicit note in `AGENTS.md`.

### Review-agent pass
Findings: (1) the design's Tier 2 live-diff section didn't account for
`MutationApi.get()` needing `target_pk` to include `c_personid` (which a staging
`Proposal.target_pk` deliberately excludes) — a literal implementation would 422
on the very first multi-field-PK resource, including the doc's own kinship
example; (2) a fabricated citation to `docs/00-target-system-brief.md` §6 for the
"GET ignores dry-run" claim — that section never mentions dry-run at all; (3)
`docs/01-implementation-plan.md`'s milestone list only went up to 7, with no
mention of this new Milestone 8 doc.

Resolution: added an explicit implementation note pointing at the existing
`staging.resolve_target_pk()` helper (already used for submission) as the correct
way to merge `person_id` before calling `get()`; corrected the citation to
`docs/01-implementation-plan.md` §3's actual "GET calls still go through" text;
added Milestone 8 as an unchecked entry to `docs/01`'s list. All 3 confirmed fixed.

### codex exec pass
Independently re-verified all 3 fixes and did an additional consistency pass
against `staging.py`/`http_client.py`/`mutation_api.py`/`batch_runner.py` and
docs 00/01/03 — reported clean, with one minor editorial nit (the plan's header
still said "all 7 milestones implemented" despite §10 already showing Milestone 8
as design-only) which was also fixed. Full suite green (130 tests, docs-only
change).

### Implementation — Increment 1: Tier 1 offline preview renderer

Added `render_preview_markdown()` + `ProposalCurrentState` to `staging.py`: a
pure, network-free Markdown summary of a staging batch (status line, per-proposal
conflict highlighting with ⚠️/✅, options/agent-suggestion display), exactly as
Tier 1 of `docs/06-staging-preview-design.md` §2 specifies. 22 new tests.

#### Review-agent pass
Findings: (1) issues whose `proposal_id` didn't match any real proposal (or was
`None`) were silently dropped from the rendered body, only counted in the status
line with no explanation anywhere; (2) a missing/`None` current value rendered as
the literal text `None` instead of the design's `_(empty)_`; (3) `source_quote`/
`conflict.description`/`agent_reasoning` were interpolated raw with no newline
handling, unlike `source_excerpt`/`batch_notes`, so a multi-line value would break
the bullet structure; (4) conflict option values/`agent_suggestion` containing a
literal backtick could break the inline code span; (5) several test coverage gaps
(zero proposals, multi-proposal issue attribution, empty `changes`, multi-line
`source_quote`, empty options list, unattributed-issues section); (6) a minor
label-spacing inconsistency.

Resolution: added an `## Unattributed issues` fallback section; added
`_preview_value()` (renders `None` as `_(empty)_`) and `_preview_inline()`
(collapses newlines, neutralizes backticks), applied consistently; added all 7
missing tests; fixed the spacing inconsistency. All 6 confirmed fixed by a
follow-up Explore-agent pass; full suite green (152 tests at that point).

#### codex exec pass
Finding: the resolved-conflict status line (`` resolved as `{conflict.resolution}` ``)
still interpolated `resolution` raw, unlike option values/`agent_suggestion` which
already went through `_preview_inline()` — same backtick/newline risk, just missed
on this one line. Minor: `ProposalCurrentState`'s docstring claimed "never both
set" for `row`/`error` but nothing enforced it.

Resolution: routed the resolution status line through `_preview_inline()` too;
added a `model_validator(mode="after")` enforcing exactly one of `row`/`error` is
set on `ProposalCurrentState`. Added regression tests for both. A follow-up codex
pass confirmed both fixed — reported clean. Full suite green (152 tests).

### Implementation — Increment 2: Tier 2 best-effort live diff

Added `fetch_current_values(batch, api)` to `batch_runner.py`: for every
`update`/`delete` proposal with a concrete, resolvable `person_id`, attempts one
`GET /api/v2/get` (merging `c_personid` into `target_pk` via the existing
`resolve_target_pk()`, reusing the mechanism the Increment-1/design review had
already flagged as necessary) to fetch the row's current server-side values for
`render_preview_markdown()` to diff against, per Tier 2 of
`docs/06-staging-preview-design.md` §2. `create` proposals are skipped entirely
(nothing to diff). Never raises — every failure (unresolved `person_id`, 404,
network error, unknown resource alias, malformed response) degrades to a
`ProposalCurrentState(error=...)`. 8 new tests initially.

#### Review-agent pass
Findings: implementation and test coverage were correct and matched the design
doc; one nice-to-have gap — no test covered an unknown/invalid resource alias
reaching `find_spec_by_alias()` (already safely caught by the broad
`except Exception`, just unproven by a test).

Resolution: added
`test_fetch_current_values_unknown_resource_alias_becomes_error_not_exception`.
Full suite green (162 tests at that point, before the codex pass's fix below).

#### codex exec pass
Finding (must-fix): the row-shape check ran *after* the broad `try/except`, so a
malformed successful response with a non-dict `result.row` (e.g. a list or
string) would reach `ProposalCurrentState(row=row)` and raise a Pydantic
`ValidationError` — violating the function's own "never raises" contract, since
`ProposalCurrentState.row` is typed `dict[str, Any] | None`.

Resolution: changed the check from `if row is None:` to
`if not isinstance(row, dict):` so a non-dict row also degrades to
`ProposalCurrentState(error="row not found in response")`. Added
`test_fetch_current_values_non_dict_row_becomes_error_not_exception` as a
regression test. A follow-up codex pass confirmed the fix closes the gap, found
no other similar gaps, and confirmed the new test genuinely exercises the fixed
path. Full suite green (162 tests).

### Implementation — Increment 3: CLI integration

Wired both tiers into `cli.py` per `docs/06-staging-preview-design.md` §3:
`validate --staging <path>` now also writes/refreshes `preview.md` next to the
staging YAML on every run via a new `_write_preview()` helper. Tier 2's live
diff is attempted only if `load_config()` succeeds; any `ConfigError` falls
back to a Tier-1-only (offline) preview. A separate, narrower `except OSError`
guards the actual file write so a disk error only prints a warning rather than
affecting `validate`'s exit code. `--env` was added to the `validate`
subcommand's parser (previously `submit`-only) so Tier 2 can point at a
non-default `.env`. `validate --input` deliberately skips preview generation —
there's no "next to the file" location for a JSON input batch the way there is
for a staging YAML. 4 new tests, plus one pre-existing test updated to pass an
explicit `--env` now that `validate --staging` touches `load_config()`.

#### Review-agent pass
Findings: none must-fix. One nice-to-have — the original `except Exception`
guarding Tier 2 was broader than necessary, since `fetch_current_values()`
already never raises internally; only `load_config()`'s `ConfigError` can
realistically reach that except.

Resolution: narrowed `except Exception` to `except ConfigError`, matching
`cmd_submit`'s existing error-handling convention. This required updating one
pre-existing Increment-1 test (`test_validate_staging_unresolved_conflict_
still_returns_zero`) to pass an explicit `--env`, since it predated
`_write_preview()` and previously never touched `load_config()` at all — without
the narrowing, this went unnoticed because the old broad `except Exception` had
been silently swallowing the test-only ambient-dotenv-lookup guard's
`AssertionError` (see `conftest.py`'s `_forbid_ambient_dotenv_lookup`). Full
suite green (166 tests).

#### codex exec pass
Findings: none must-fix. Confirmed the `ConfigError` narrowing is safe (checked
`AuditLog.__init__`/`HttpClient.__init__`/`MutationApi.__init__` — none raise
anything else), confirmed the preview path/OSError guard/`--input` skip all
match the design. One nice-to-have (env var leakage risk in the "without env"
test) was already covered by `conftest.py`'s existing autouse
`_clean_cbdb_env` fixture, so no change needed. Full suite green (166 tests).

Milestone 8 (staging batch preview) is now fully implemented: Tier 1, Tier 2,
and CLI integration are all done and reviewed. Tier 3 (optional Artifact
rendering) is explicitly out of scope for the Python package per the design
doc's §2 — it's documented as agent-level `SKILL.md` behavior, not code.

### Follow-up — SKILL.md was never actually updated for Tier 3, and had a stale claim

Caught when the user directly asked "did `/goal` actually finish `docs/06`?"
after the goal-completion signal had already fired. Checking honestly (not
just re-asserting "done") turned up two real gaps the increments above missed:
`SKILL.md` still said "`validate` never touches the network and never requires
`.env`" — true before Increment 2/3, no longer accurate now that `validate
--staging` can optionally do a network call for Tier 2 — and Tier 3 (session-
only Artifact rendering) was never actually written into `SKILL.md` at all,
despite the design doc explicitly requiring it to be documented there (§2:
"documented as agent behavior in `SKILL.md`"). Neither gap broke any test,
since both are pure documentation.

Fixed: scoped the "never touches the network" claim to `find_issues()`
specifically; added a bullet describing `preview.md` generation, Tier 2's
config-dependent behavior, and the whole-batch-vs-per-proposal fallback
distinction; added Tier 3 guidance under section B's validate step (session-
only, `preview.md` must stand alone without a Claude Code session, never a
second source of truth).

#### Review-agent pass
Findings: none must-fix on the Tier 3 addition itself (accurately scoped,
consistent with docs/06 §4's constraints). One nice-to-have: the fallback
wording conflated "no `--env`" with "a per-proposal network failure" as both
causing a full offline fallback, overstating the blast radius of a single bad
`GET` — only a `ConfigError` (broken/missing `.env`) drops Tier 2 for the whole
batch; a per-proposal failure only affects that one proposal's row.

Resolution: reworded to distinguish whole-batch fallback (config fails to
load at all) from per-proposal fallback (one bad `GET`). Full suite green
(166 tests, docs-only change).

#### codex exec pass
Finding (must-fix): the reworded text still said "no `--env` ... drops the
live diff for the whole batch" — wrong, since omitting `--env` just triggers
python-dotenv's standard `.env` lookup and can still succeed; the real trigger
for whole-batch fallback is `load_config()` raising `ConfigError`, independent
of whether `--env` was passed.

Resolution: reworded again to "config that fails to load at all (missing/
broken `.env`, whether or not `--env` was passed) drops the live diff for the
whole batch." A follow-up codex pass confirmed this is now accurate against
`cmd_validate()`/`_write_preview()`/`load_config()`'s actual behavior, and that
the per-proposal distinction holds regardless of how `--env` was supplied.
Full suite green (166 tests).
