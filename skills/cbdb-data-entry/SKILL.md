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

This skill wraps the `cbdb_agent` Python package (`src/cbdb_agent/`) in this repo.
Read `AGENTS.md` (hard rules) and `docs/00-target-system-brief.md` (target-system
facts) before using it if you haven't already — this file assumes both are known.

## Two entry paths

### A. Already-structured input (CSV/JSON records ready to submit)

1. Confirm `.env` is configured: `CBDB_API_BASE_URL`, `CBDB_API_TOKEN` set,
   `CBDB_DRY_RUN` state known. **Never proceed with a non-dry-run call without
   telling the user which host it's about to hit and getting explicit confirmation**,
   even if `CBDB_CONFIRM_PROD` is already set — a human should always know before a
   live write happens, gate or no gate.
2. Sanity-check the input file's shape yourself before calling anything (does it
   look like the internal per-person + nested sub-resources schema described in
   `docs/01-implementation-plan.md` §7? are required fields present per
   `docs/04-field-whitelists.md`?) — Milestone 5 has not yet defined a dedicated
   `validate --input` subcommand the way the staging path has `validate --staging`
   (`docs/03-extraction-review-workflow.md` §2.5); don't invent one, just read the
   file and flag anything obviously wrong before calling `submit`.
3. Run `python -m cbdb_agent submit --input <path>`.
4. Report the per-record summary verbatim to the user (successes, conflicts,
   failures) — do not paraphrase away a failure or conflict.
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
5. **Validate before ever offering to submit**
   (`python -m cbdb_agent validate --staging <path>`, once Milestone 4 lands): every
   `conflicts[].resolution` must be non-null, every field must pass that resource's
   whitelist, every sibling-`id` person reference must resolve. Report remaining
   issues plainly and loop back to step 4 — do not submit a batch with unresolved
   items even if the user seems to be in a hurry.
6. **Only submit on explicit confirmation** ("提交" / "submit" / equivalent):
   `python -m cbdb_agent submit --staging <path>`. Same dry-run/production-gate and
   audit-logging guarantees as path A apply — this workflow does not create a
   separate, less-audited write path.
7. On completion, tell the user where the processed batch (staging file + server
   responses) landed in `data/processed/`.

## Hard constraints (repeated from AGENTS.md — do not relax them for this skill)

- Only `/api/v2/create|mutate|delete|get` (`mode: "direct"` for writes) and the
  read-only `/api/v2/persons`/`/api/v2/operations` — never legacy `/basicinformation/*`
  routes or other undocumented endpoints.
- `c_personid` is client-assigned — always let `person_id.py` validate/allocate it,
  never invent one inline.
- Person-before-sub-resource ordering, always.
- Never auto-retry a `409`/`422` — surface it, don't guess a fix and resend.
- This skill must never itself call an external LLM API for extraction — the
  "reading" in path B is done by the agent session invoking this skill, using its own
  language understanding, not a separate model call from `src/cbdb_agent/`.
