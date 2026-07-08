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
