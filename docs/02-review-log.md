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
