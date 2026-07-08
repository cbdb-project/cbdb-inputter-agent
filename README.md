# cbdb-inputter-agent

An authorized API client for [`cbdb-online-main-server`](https://github.com/cbdb-project/cbdb-online-main-server)
(the CBDB online data-entry system, live at https://input.cbdb.fas.harvard.edu) that
submits biographical records via its `/api/v2/*` Mutation API, in place of manually
clicking through the web UI — while keeping every write attributable and auditable.

Start here:
- [`docs/00-target-system-brief.md`](docs/00-target-system-brief.md) — what we know
  about the target system's auth, API, and audit logging.
- [`docs/01-implementation-plan.md`](docs/01-implementation-plan.md) — this repo's
  architecture and build plan.
- [`docs/03-extraction-review-workflow.md`](docs/03-extraction-review-workflow.md) —
  how unstructured source material (e.g. a classical-Chinese biography) becomes a
  human-reviewable, bulk-editable staging file before anything is submitted.
- [`AGENTS.md`](AGENTS.md) — hard rules for any agent (or human) working in this repo.

## Setup

1. `cp .env.sample .env`
2. Log into the target CBDB instance's web UI once, go to `/profile`, create a
   Personal Access Token, and paste it into `.env` as `CBDB_API_TOKEN`.
3. Leave `CBDB_DRY_RUN=true` until you've validated your input data against a local
   or test instance.
4. `pip install -r requirements-dev.txt` (once `src/` is implemented — see plan
   milestones; use `requirements.txt` alone for a runtime-only install).

This project is a work in progress; see `docs/01-implementation-plan.md` for current
status and open questions.
