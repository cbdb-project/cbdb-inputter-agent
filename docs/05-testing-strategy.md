# Testing Strategy

Status: draft, pending review. Fills in the "unit tests with a mocked HTTP layer"
mention in `01-implementation-plan.md` §10 (Milestone 2) with concrete tooling and
fixture conventions.

## 1. Two tiers of tests

1. **Unit tests (default, run always)** — no real network calls. Mock the HTTP layer
   with the [`responses`](https://github.com/getsentry/responses) library, which
   intercepts `requests` at the transport level. Cover `http_client.py`,
   `mutation_api.py`, `person_id.py`, `staging.py`, `audit_log.py` this way.
2. **Local integration tests (opt-in, not run by default)** — hit the real local
   `cbdb-online-main-server` instance (now running at `http://localhost:8000` with
   the dedicated `cbdb-inputter-agent@local.test` test account, per
   `docs/02-review-log.md`'s Milestone-2-prep notes) with `CBDB_DRY_RUN=false` and
   `CBDB_CONFIRM_PROD` pinned to that local URL. Marked with `@pytest.mark.integration`
   and skipped by default (`pytest -m "not integration"` is the default `pytest.ini`
   config); run explicitly with `pytest -m integration` only when the local server is
   confirmed up. These are the only tests allowed to perform a real mutating call, and
   only against the pinned local test account — never against production.

## 2. Mocking conventions (unit tier)

- Use `responses.RequestsMock()` (or the `@responses.activate` decorator) to register
  expected requests: method, URL, expected JSON body (via `match=[responses.matchers
  .json_params_matcher(...)]`), and the mocked response body/status.
- Never monkeypatch `requests` globally or reach into `http_client.py` internals to
  skip the HTTP layer — mock at the `responses` transport level so the real
  `requests.Session` code path (headers, retries, timeouts) is still exercised.
- One fixture file per representative server response shape, under `tests/fixtures/`:
  - `create_success.json` — 200 `{"ok": true, "resource": ..., "result": {...}}`
  - `create_success_server_assigned_pk.json` — 200 response for `possessions`/
    `postings` where the server-assigned surrogate ID
    (`c_possession_record_id`/`c_posting_id`, see `docs/04-field-whitelists.md`) only
    appears in `result.pk` — distinct fixture because the client must read this ID
    back rather than knowing it upfront.
  - `conflict_409.json` — plain duplicate PK conflict (`target.pk conflict`)
  - `mirror_conflict_409.json` — `MirrorConflictException` (existing reciprocal
    kinship/association row diverges; requires `meta.force`)
  - `mirror_suspected_409.json` — `MirrorSuspectedException` (ambiguous candidate
    reciprocal rows)
  - `mirror_integrity_422.json` — `MirrorIntegrityException` (no authoritative
    reverse code available, fail-closed)
  - `unprocessable_422.json` — validation error, unknown/invalid field
  - `unauthenticated_401.json` — bad/expired token
  - `forbidden_403.json` — `canWriteDirectly()` false (e.g. crowdsourcing account)
  - `rate_limited_429.json`
  - `server_error_500.json`
- Fixtures are literal server response bodies (see `docs/00-target-system-brief.md`
  §3–4 for the documented envelope shape) — copy real shapes here rather than
  inventing plausible-looking ones, and update them if a live call ever reveals the
  real shape differs (flag this in `docs/02-review-log.md` if it happens).

## 3. What each module's tests must cover

- **`http_client.py`**: one test per status-code branch in
  `01-implementation-plan.md` §5 (401/403/409/422/429/5xx), a dry-run test asserting
  no `responses`-mocked call is actually made for a mutating verb, and a rate-limiter
  test using `freezegun` to control elapsed time deterministically instead of
  real `time.sleep`.
- **`mutation_api.py`**: one test per resource wrapper asserting the JSON envelope
  sent matches brief §3's shape exactly (resource/mode/operation/person_id/target/
  changes), and a whitelist-rejection test per resource using `models.py`'s field
  list from `docs/04-field-whitelists.md`. Additionally, per `docs/04-field-
  whitelists.md`'s per-resource quirks:
  - **Mirror-relationship resources (`kinship`, `associations`)**: a test per
    exception type — `mirror_conflict_409.json` response must surface as a
    catchable, non-retried error distinct from a plain PK conflict (never silently
    retried with `meta.force: true` auto-set); `mirror_suspected_409.json` and
    `mirror_integrity_422.json` likewise must not be conflated with each other or
    with `conflict_409.json` in `http_client.py`'s error mapping.
  - **Server-assigned-PK resources (`possessions`, `postings`/`offices`)**: a test
    that `mutation_api.py` never sends a client-chosen
    `c_possession_record_id`/`c_posting_id` on create, and a test that after a
    mocked `create_success_server_assigned_pk.json` response, the returned ID is
    correctly threaded into a subsequent same-batch call that needs it in
    `target_pk` (this is the scenario `staging.py`'s validation rule 6 in
    `docs/03-extraction-review-workflow.md` §2.5 exists to guard).
  - **`social_institutions` update alias gap**: a regression test asserting
    `update_social_institution()` never sends `resource: "socialinst"` (per
    `docs/04-field-whitelists.md` §12's documented server-side gap).
  - **`basicinformation` update immutability + soft delete**: a test that
    `update_person()` rejects (client-side, before even sending) an attempt to
    change `c_name_chn`/`c_name`/`c_name_proper`/`c_name_rm` — those are blocked on
    update though allowed on create (`docs/04-field-whitelists.md` §1) — and a test
    that `delete_person()`'s local audit log entry correctly records the operation as
    a soft-delete `UPDATE` (matching what the server actually does), not a `DELETE`,
    so our own audit trail doesn't mislead a human reading it later.
  - **`events` address-only pseudo-field path**: a test that a `changes` payload
    containing *only* `c_addr_id`/`c_addr_cleared` (no scalar `EVENTS_DATA` field) is
    still accepted and sent — this is a real, separate server code path
    (`docs/04-field-whitelists.md` §6), not an edge case to reject as "empty changes."
  - **`sources` nullable/re-keyable PK**: a test that `c_pages` (optional at the PK
    level, canonicalized to `''` not `null`) round-trips correctly through create,
    update (where `c_textid`/`c_pages` are re-keyable but `c_personid` is immutable —
    an update attempting to change `c_personid` must be rejected client-side), and
    delete (`docs/04-field-whitelists.md` §13).
- **`person_id.py`**: boundary tests for the `<= max(existing) + 10000` and
  not-already-taken rules (brief §3), using a mocked `GET /api/v2/persons` response.
- **`staging.py`**: a YAML fixture with a deliberately unresolved conflict must fail
  validation; a fixture with a dangling sibling-`id` reference must fail; a clean
  fixture must pass and produce the exact ordered call sequence (person before
  sub-resources) that `cli.py submit --staging` would send.
- **`audit_log.py`**: assert the JSONL line format (§4 fields) is written for both a
  successful and a failed call, and that dry-run calls are logged with the dry-run
  flag set and no real request attempted.

## 4. Test data hygiene

- No real person data, real tokens, or content from actual source texts in
  `tests/fixtures/` — use clearly fictional placeholder names/IDs (the brief's
  `c_personid` numbering scheme leaves room for an obviously-fake range, e.g.
  `9999901`+, reserved by convention for test fixtures only).
- `pytest.ini` / `pyproject.toml` test config lives at the repo root once Milestone 2
  lands; `-m "not integration"` is the default so `pytest` alone never touches a real
  server, local or otherwise.
