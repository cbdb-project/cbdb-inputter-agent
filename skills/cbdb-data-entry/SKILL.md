---
name: cbdb-data-entry
description: >
  Submit biographical records into the CBDB online data-entry system
  (cbdb-online-main-server) via its authorized /api/v2/* Mutation API — either from
  already-structured records, or by first reading unstructured source material (e.g.
  a classical-Chinese biography) and drafting a human-reviewable extraction proposal.
  Use when the user asks to "录入" / "提交" / "导入" data into CBDB, or gives source
  text and asks what should go into CBDB from it.
---

# CBDB Data Entry

This skill wraps the `cbdb_agent` Python package (`src/cbdb_agent/`) in this repo,
via its `python -m cbdb_agent` CLI (`src/cbdb_agent/cli.py`). Read `AGENTS.md`
(hard rules) and `docs/00-target-system-brief.md` (target-system facts) before
using it if you haven't already — this file assumes both are known.

Setup check: `pip install -e .` (or `-r requirements-dev.txt` for running tests
too) must have been run once in this environment, and `.env` must exist (copied
from `.env.sample`) with a real `CBDB_API_TOKEN`.

## CLI reference

```
python -m cbdb_agent validate --staging <path> | --input <path>  [--env <path>]
python -m cbdb_agent submit   --staging <path> | --input <path>  [--dry-run] [--env <path>]
```

- `validate` checks the file's structure/whitelists/conflicts
  (`staging.find_issues()`) and prints every issue found — this part never
  requires `.env` and never fails without it. Exit codes: `0` clean (unresolved
  conflicts alone still exit `0` — they're expected mid-review, per
  `docs/03-extraction-review-workflow.md` §2.5), `2` couldn't load/parse the
  file, `3` structural error found.
- `validate --staging` additionally writes/refreshes `preview.md` next to the
  staging YAML on every run (`docs/06-staging-preview-design.md` §3) — a
  generated, read-only Markdown summary (status line, per-proposal diffs,
  conflict highlighting). If a working config is available — via `--env`, or
  the standard `.env` lookup if `--env` is omitted — `preview.md` also includes
  a best-effort live old-vs-new diff for `update`/`delete` proposals
  (`batch_runner.fetch_current_values()`). Two different degrees of fallback,
  both silent and never affecting `validate`'s own exit code: config that
  fails to load at all (missing/broken `.env`, whether or not `--env` was
  passed) drops the live diff for the *whole* batch (offline-only); a
  per-proposal failure (a 404, an unreachable network) only shows `could not fetch
  (<reason>)` for *that* proposal — the rest of the batch still gets its live
  diff. `validate --input` does not write a preview (no staging YAML to put it
  next to).
- `submit` first re-validates (hard gate — any structural error or unresolved
  conflict blocks it, exit `3`), then loads `.env` (exit `4` on a config error),
  then actually runs the batch through `MutationApi`/`HttpClient`. Exit `1` if any
  proposal failed or was skipped; `0` only if every proposal succeeded.
- `--dry-run` forces dry-run on for this run even if `.env` says otherwise — it
  cannot force dry-run off (`AGENTS.md` rule 4). A dry-run `submit` never sends a
  mutating request and never archives the source file (nothing was actually
  attempted, so it's left in place for another iteration).
- On a real (non-dry-run) `submit`, the source file is moved to
  `data/processed/<batch_id>/` alongside a `results.json` with the per-proposal
  outcome — re-submitting the same `batch_id` gets its own `-attempt2`/`-attempt3`
  directory rather than overwriting the previous attempt's results.
- `--env <path>` points at a specific `.env` file — useful for testing against the
  local `cbdb-online-main-server` instance (see `AGENTS.md`'s Local dev section)
  without touching the default one.

## Two entry paths

### A. Already-structured input (JSON records ready to submit)

Input file shape: a JSON array of records, each with `id`, `resource`,
`operation` (`create`/`update`/`delete`), `person_id` (a real `c_personid`,
`"NEW"` for a person to be created in this batch, or another record's `id` string
for a sub-resource of a person also being created in this batch), optional
`target_pk`, and `changes` — see `staging.load_input_batch()`'s docstring and
`docs/04-field-whitelists.md` for what belongs in `changes` per resource.

1. Confirm `.env` is configured: `CBDB_API_BASE_URL`, `CBDB_API_TOKEN` set,
   `CBDB_DRY_RUN` state known. **Never proceed with a non-dry-run call without
   telling the user which host it's about to hit and getting explicit confirmation**,
   even if `CBDB_CONFIRM_PROD` is already set — a human should always know before a
   live write happens, gate or no gate.
2. Run `python -m cbdb_agent validate --input <path>` first; fix anything it
   reports before proceeding.
3. Run `python -m cbdb_agent submit --input <path>`.
4. Report the per-record summary verbatim to the user (successes, conflicts,
   failures, skipped-dependency records) — do not paraphrase away a failure.
5. Never construct a raw HTTP call yourself, bypassing `mutation_api.py`'s validation
   or `http_client.py`'s local audit logging — always go through the CLI/library
   (`AGENTS.md` rule 2).

### B. Unstructured source material (e.g. a 文言文 biography) — extraction workflow

Full design: `docs/03-extraction-review-workflow.md`. Summary of what this skill
does when invoked this way:

1. **Read the source text** the user provides (pasted or a file path). Cross-reference
   `docs/00-target-system-brief.md` §3 (resources/fields) and
   `docs/04-field-whitelists.md` (per-resource allowed fields) to figure out which
   facts map to which resource/operation.
2. **Draft a staging file** at `data/staging/<batch-id>/proposal.yaml` following the
   schema in `docs/03-extraction-review-workflow.md` §2.2: one proposal entry per
   row to be created/updated, each with `source_quote`, `confidence`, and (if
   ambiguous or conflicting with existing data) a `conflicts` block with `options`,
   `agent_suggestion`, `agent_reasoning`, and `resolution: null`.
3. **Tell the user** (in chat, briefly): how many proposals, how many flagged
   conflicts, and where the file is. Do not ask the user to approve rows one at a
   time — the file is for bulk review.
4. **Support both review modes, freely mixed**:
   - The user edits the YAML file directly and says "改完了" / "done editing" —
     re-read the file.
   - The user discusses a specific conflict in chat (referencing its local `id`,
     e.g. "c3 应该是 820") — update that conflict's `resolution` in the file
     yourself and confirm back what you changed.
5. **Validate before ever offering to submit**: `python -m cbdb_agent validate
   --staging <path>`. Every `conflicts[].resolution` must be non-null, every field
   must pass that resource's whitelist, every sibling-`id` person reference must
   resolve to a `basicinformation` create. Report remaining issues plainly and
   loop back to step 4 — do not submit a batch with unresolved items even if the
   user seems to be in a hurry. A conflict resolved as `"defer"` is treated as
   resolved (validation passes) but that proposal — and anything depending on it —
   is silently excluded from submission; tell the user which rows were deferred
   when you report the submit summary.
   - This step also refreshes `preview.md` next to the staging file (see the CLI
     reference above) — point the user at it, or read it back to them yourself,
     as the primary way to review a batch instead of raw YAML.
   - **For a batch of more than a few proposals, point the user at the review page
     instead of `preview.md`**: `validate --staging` also writes a `review.json`
     next to the YAML, and `tools/review/index.html` (open it directly, no server)
     loads that file. It gives a dense per-person table, filters, and — the reason
     it exists — **bulk resolution of repeated conflicts**: identical questions
     asked across many rows (an index-year convention, a reign-year check) collapse
     into one click. The user then exports `decisions.json` and you run
     `python -m cbdb_agent apply-review --staging <yaml> --decisions <json>`, which
     prints every change it made. Re-run `validate --staging` afterwards. See
     `docs/08-review-interface-design.md`. Never edit the YAML yourself to mirror
     what the page did — `apply-review` is the write path.
   - If the user wants a nicer visual than the plain Markdown file, or asks to
     "see"/可视化 the batch, render `preview.md` as a Claude Code Artifact
     (`docs/06-staging-preview-design.md` Tier 3) — conflict cards, side-by-side
     option comparisons, etc. This is session-only interactive behavior, not a
     CLI/package feature: `preview.md` itself must always stand alone (openable
     in any plain editor, no Claude Code session required), so never make the
     Artifact rendering something a user depends on to review a batch at all —
     it's a nicer view of the same file, not a second source of truth.
6. **Only submit on explicit confirmation** ("提交" / "submit" / equivalent):
   `python -m cbdb_agent submit --staging <path>`. Same dry-run/production-gate and
   audit-logging guarantees as path A apply — this workflow does not create a
   separate, less-audited write path. A `"NEW"` person's real `c_personid` is only
   allocated at this point (`batch_runner.allocate_person_id`), not while drafting.
7. On completion, tell the user where the processed batch (staging file + server
   responses) landed in `data/processed/<batch_id>/`.

## Hard constraints from AGENTS.md (do not relax them for this skill)

- For **writes**: only `/api/v2/create|mutate|delete` with `mode: "direct"`, plus
  `/api/v2/get` — never legacy `/basicinformation/*` routes or other undocumented
  endpoints. For **reads**, additionally allowed: `/api/v2/persons`,
  `/api/v2/operations`, and the public code-lookup endpoints (`/api/v2/texts`,
  `/api/select/*`, `/api/code/addr`, `/api/name`) used to turn a book title, place
  name or office name into a code. Call those with `get(..., public=True)` so a stale
  token can't turn them into failed-auth attempts, and treat whatever they return as a
  **suggested** code that still goes through staging review — never straight into a
  `changes` payload. See AGENTS.md rule 1 and `docs/07-api-md-digest.md` §2.1.
- **Never create a code-table row or an entity aggregate (`text-codes`, `office`,
  `social-institution`, `merged-person`) to unblock a batch.** If a book title or
  office code the source needs doesn't exist, that is a *finding to report to the
  user*, with the evidence — they decide. These writes are global, referenced by
  potentially tens of thousands of person rows (AGENTS.md rule 12). The code tables
  additionally have **no delete path at all**, and a `TEXT_CODES` row's Chinese title
  can never be edited afterwards; the `office`/`social-institution` aggregates *are*
  deletable while still unreferenced.
  - Before reporting a code as missing, **search by pinyin as well as by Chinese
    characters**. Variant characters are normal in CBDB: 《俟庵集》 is stored as
    《俟菴集》, and searching 俟庵 returns zero hits while `Sian ji` finds it. Reporting
    a false "missing" is how a duplicate gets created for a row that can never be
    deleted.
  - If the user *does* approve one, two are modelled and go through the normal staging
    pipeline: `text-codes` (create only) and `office` (create + update; see
    `docs/04-field-whitelists.md` §15). Either way the proposal needs an explicit
    `approved_by: <the human's name>` or validation fails with a structural error.
  - **An `office` update rewrites the WHOLE row** (`API.md` §13.4) — an omitted field is
    written as `NULL`, not left alone. Read the current row first
    (`GET /api/select/search/office?q=<id>`, since `/api/v2/get` cannot read this
    resource) and carry every value across, or write an explicit `null` to say you mean
    to clear it. The client refuses a partial payload rather than letting it through.
  - Use the resource string **`office`**, never `offices` — the plural is matched by the
    postings handler first, so it would write a person's appointment record instead.
- **Never write a provisional marker into a submitted field.** No "Need to be checked",
  "to be verified", "tentative", "temporarily added" in `c_notes` or anywhere else that
  lands in CBDB. Submitting is what asserts the data is right; such a note describes the
  editor's workflow, not the record, and it stays public long after the checking is done.
  Uncertainty belongs in the staging file — `confidence`, a `conflict`, `batch_notes` —
  which is not submitted. **And when existing data already carries such a marker and your
  task is the check it asks for, delete it** rather than preserving it: that is the one
  case where the usual "keep other people's prose byte-for-byte" instinct is wrong.
  (Real case: office 12304's note read "Temporarily added to office codes. Need to be
  checked."; the edict being added was that check, the agent preserved the sentence
  anyway, and the user had to remove it by hand.)
    **You must never fill that field in yourself**; it records that a named human
    made the call, and `batch_runner` forwards it into `meta.comment` so the
    sign-off is visible in the server's own `operations` row too.
  - `c_textid` is server-assigned, so a `BIOG_TEXT_DATA` (`texts`) row that cites a
    newly-created book **cannot be in the same batch** — the staging schema can
    carry a sibling reference for `person_id` only, not for a `c_textid`. Submit the
    `text-codes` create first, read the assigned id out of the response, then put
    the literal id in the follow-up batch.
- `c_personid` is client-assigned — for a `"NEW"` proposal, only
  `batch_runner.allocate_person_id()` may pick the real value (it's the only code
  path that calls `person_id.py`'s validation/existence checks); for a
  human-supplied `c_personid`, the CLI currently passes it through as-is with no
  extra validation beyond what the server itself enforces on the actual request —
  don't invent or hand-adjust an ID yourself either way.
- Person-before-sub-resource ordering, always — the CLI already enforces this via
  `staging.topological_submission_order()`, so don't reorder proposals yourself in
  a way that fights it.
- Never auto-retry a `409`/`422` — surface it, don't guess a fix and resend.
- **For `kinship`/`associations`: `GET` both directions of the pair before writing
  `c_notes`/`c_source`/`c_pages` (or the assoc year fields)** — the server mirrors
  these fields between a pair's two rows on every direct update, so two proposals
  in the same batch writing *different* content to each direction will silently
  corrupt each other (whichever mutate call runs last wins on both sides). If the
  two directions' existing content already differs, stop and get a human decision
  before writing either — don't let a batch resolve it by write order. See
  AGENTS.md's "Reverse-pair mirror sync" section for the full mechanism and the
  byte-for-byte-preservation gotcha when appending to existing content.

## Additional constraint from docs/03-extraction-review-workflow.md §2.4

- This skill must never itself call an external LLM API for extraction — the
  "reading" in path B is done by the agent session invoking this skill, using its own
  language understanding, not a separate model call from `src/cbdb_agent/`.
