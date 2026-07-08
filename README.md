# cbdb-inputter-agent

> **⚠️ Early-stage project, under active development.** The core client
> (Milestones 1–7 — auth, mutation API, extraction/staging workflow, CLI,
> live-validated against a real instance) is implemented and tested, but the CLI
> surface, staging-file schema, and skill wiring should all still be considered
> subject to change. Read [`AGENTS.md`](AGENTS.md) before touching production
> data with this.

An authorized API client for [`cbdb-online-main-server`](https://github.com/cbdb-project/cbdb-online-main-server)
(the CBDB online data-entry system, live at https://input.cbdb.fas.harvard.edu) that
submits biographical records via its `/api/v2/*` Mutation API, in place of manually
clicking through the web UI — while keeping every write attributable and auditable.

Start here:
- [`docs/00-target-system-brief.md`](docs/00-target-system-brief.md) — what we know
  about the target system's auth, API, and audit logging (includes facts confirmed
  live against a real instance during Milestone 7).
- [`docs/01-implementation-plan.md`](docs/01-implementation-plan.md) — this repo's
  architecture and milestone-by-milestone build plan.
- [`docs/03-extraction-review-workflow.md`](docs/03-extraction-review-workflow.md) —
  how unstructured source material (e.g. a classical-Chinese biography) becomes a
  human-reviewable, bulk-editable staging file before anything is submitted.
- [`docs/04-field-whitelists.md`](docs/04-field-whitelists.md) — per-resource allowed
  fields and composite primary keys for all 13 supported resources.
- [`docs/02-review-log.md`](docs/02-review-log.md) — the review-agent + `codex`
  findings and fixes for every milestone, including the two real bugs Milestone 7's
  live validation caught that no amount of mocked testing would have found.
- [`AGENTS.md`](AGENTS.md) — hard rules for any agent (or human) working in this repo.

## Setup

1. `cp .env.sample .env`
2. Log into the target CBDB instance's web UI once, go to `/profile`, create a
   Personal Access Token, and paste it into `.env` as `CBDB_API_TOKEN`.
3. Leave `CBDB_DRY_RUN=true` until you've validated your input data against a local
   or test instance.
4. `pip install -e .` for the runtime package, or `pip install -r requirements-dev.txt`
   to also run the test suite (`pytest`).

## Usage

```
python -m cbdb_agent validate --staging <path> | --input <path>
python -m cbdb_agent submit   --staging <path> | --input <path>  [--dry-run] [--env <path>]
```

See [`skills/cbdb-data-entry/SKILL.md`](skills/cbdb-data-entry/SKILL.md) for the full
CLI reference (exit codes, archiving behavior, the `"defer"` conflict-resolution
semantics) and both entry paths — already-structured JSON records, or an
unstructured-source-text extraction workflow that drafts a bulk-editable YAML
staging file for human review before anything is submitted.

## Status

All 7 planned milestones are implemented, tested (129 unit tests, no real network
calls), and — for the core write path — validated live against a real local
`cbdb-online-main-server` instance. See `docs/01-implementation-plan.md` for the
milestone list and `docs/02-review-log.md` for what each one's review passes found.
