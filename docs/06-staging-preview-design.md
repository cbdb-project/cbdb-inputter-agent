# Staging Batch Preview — Design

Status: implemented and reviewed (Tier 1, Tier 2, and CLI integration). This is
Milestone 8 — see `01-implementation-plan.md` §10 and `docs/02-review-log.md`'s
Milestone 8 entry for the implementation/review history. Written after a real
review friction point during actual use (2026-07-17,
the 陳俊卿/陳文龍 kinship-note batch): reviewing a staging batch by opening its raw
YAML meant manually cross-referencing nested `conflicts[].options[]`, and manually
having to remember to check the *current* server value of a field before trusting
a proposed `changes` value — which is exactly the kind of thing a generated preview
should do automatically instead of relying on a human (or the agent) to remember.

## 1. Problem

`docs/03-extraction-review-workflow.md`'s review loop is "open the YAML, or discuss
in chat." That's fine for *editing* (YAML is the right format for that — see §2.2's
rationale), but it's not a good *reading* experience:
- Conflicts are buried in nested structure (`proposals[].conflicts[].options[]`)
  with no at-a-glance "how many are still unresolved" signal.
- Nothing shows what a proposed `changes` value would actually change *from* — the
  reviewer has to separately look up the current record to know if `changes.c_notes`
  is appending to something or (accidentally) blanking it out. This exact gap caused
  the `prior_note_p2` conflict in the worked example above to require manual
  reasoning through an unrelated GitHub issue thread instead of being caught
  automatically by the tooling.
- There's no single "is this batch ready to submit" status line.

## 2. Design: two tiers, YAML stays the only editable source of truth

**Nothing in this design lets you edit a batch by editing the preview.** The preview
is always generated, never hand-edited — regenerate it, don't patch it. Editing
still happens in the YAML (directly, or via chat asking the agent to update a
specific `resolution`), exactly as `03-extraction-review-workflow.md` already
specifies. This design only adds a better *read* path, not a second *write* path.

### Tier 1 — offline Markdown preview (core deliverable, no new dependencies)

A new function, e.g. `staging.render_preview_markdown(batch: StagingBatch, issues:
list[Issue]) -> str`, reusing `find_issues()`'s already-computed issue list (no
duplicate validation logic). Pure string formatting, stdlib only.

Content, roughly:

```markdown
# Staging batch: 2026-07-17-chen-junqing-chen-wenlong-kinship-note

**Status:** 2 proposals, 3 unresolved conflict(s) — NOT ready to submit

> GitHub issue cbdb-project/cbdb_sqlite#23, comment 4742439661 (reggiechan74): ...

## 1. `kin_junqing_to_wenlong` — kinship / update (confidence: medium)
- person_id: 10884 · target_pk: c_kin_id=15213, c_kin_code=243
- **c_notes**
  - current:  _(empty)_
  - proposed: 據 reggiechan74（...）陳俊卿與陳文龍親屬關係又作＿＿＿
- source_quote: "reggiechan74 (comment 4742439661): 譜系圖顯示…"
- ⚠️ **term_p1** (c_notes) — UNRESOLVED
  - 附註結尾需採用一個具體的親屬用語…
  - options: `五世從孫` (《八閩通志》用語…) · `旁系子孫` (中性描述性詞語…)
  - agent suggests: `五世從孫` — 優先採用項目維護者自己已經引用過的史料用語…
  - resolution: _(not set — edit `resolution:` in the YAML, or tell the agent which
    option to use, e.g. "resolve term_p1 as 五世從孫")_

## 2. `kin_wenlong_to_junqing` — kinship / update (confidence: medium)
...

## Batch notes
> 僅針對 KIN_DATA 現有的兩條記錄...
```

Properties this gets right for "elegant, efficient, fast":
- One status line at the very top — no counting nested YAML by eye.
- Conflicts are `⚠️`-flagged inline with the field they affect, options laid out
  side by side with their rationale, agent suggestion visually distinct from the
  raw options list.
- Every conflict/proposal keeps its local `id`, so a chat instruction like "resolve
  term_p1 as 五世從孫" still works exactly as designed in `03-extraction-review-
  workflow.md` §2.3.

### Tier 2 — best-effort live diff (the part that would have caught `prior_note_p2`)

For every `update`/`delete` proposal (never `create` — nothing exists yet to diff
against), attempt one `GET /api/v2/get` per proposal via the already-configured
`MutationApi`/`HttpClient` (this is a read, so it's unaffected by `CBDB_DRY_RUN` —
`get()` always passes `mutating=False`, and `http_client.py`'s dry-run short-circuit
only gates `mutating=True` calls; see `docs/01-implementation-plan.md` §3, "GET calls
still go through") to fetch the row's current values, and show `current: ...` vs
`proposed: ...` per changed field instead of just the proposed value alone.

**Implementation note for whoever builds this:** `MutationApi.get()` requires the
*full* `target.pk` including `c_personid`, and does not auto-merge `person_id` into
it (see `AGENTS.md`'s "Local dev / testing" section — this bit AGENTS itself during
manual testing). A staging `Proposal.target_pk` deliberately *excludes* `c_personid`
(§2 above; `staging.py`'s module docstring). Don't pass `proposal.target_pk` to
`api.get()` directly — reuse the existing `staging.resolve_target_pk()` helper
(already used for submission) to merge the resolved `person_id` back in first, the
same way `batch_runner.run_batch()` already does before calling `create`/`update`/
`delete`. Skipping this would 422 ("缺少必要的複合主鍵參數") on every multi-field-PK
resource, including `kinship` in this doc's own worked example.

This is explicitly **best-effort, not a hard requirement**:
- If `.env` isn't configured, the token is invalid, the row 404s unexpectedly, or
  the network is unreachable, catch it and render `current: ⚠️ could not fetch
  (<reason>)` for that one proposal — never fail the whole preview render over one
  unreachable row.
- Never fetches for `create` proposals, and never treats a fetch failure as a
  structural error (`find_issues()`'s error/conflict semantics are untouched by
  this — the live diff is purely presentational).

This tier is what would have surfaced `prior_note_p2` (the "did Hongsu already put
something in this `c_notes`?" question) automatically as a rendered diff instead of
requiring the agent to manually reason through the GitHub issue thread and ask the
user.

### Tier 3 (optional, session-only, not a CLI feature) — styled Artifact rendering

The Markdown from Tier 1/2 can optionally be rendered as a nicer-looking styled page
via Claude Code's Artifact tool *when working interactively in a session* — colored
conflict badges, side-by-side option comparison cards, etc. This is documented as
agent behavior in `SKILL.md` ("if the user wants a nicer visual than the plain
Markdown file, or asks to 'see' the batch, render `preview.md` as an Artifact"), not
as a feature of the Python package itself — the CLI has no route to publish to
claude.ai, so this can never be a hard dependency of `cli.py`/`staging.py`. Tier 1
must always work standalone (CI, a plain terminal, no Claude Code session).

## 3. CLI integration

`python -m cbdb_agent validate --staging <path>` — in addition to its existing
terminal report — writes/refreshes `preview.md` next to the staging file (e.g.
`data/staging/<batch_id>/preview.md`). Tying it to `validate` (rather than a
separate `preview` subcommand) means:
- The preview can never go stale relative to the last validation pass — no new
  command to remember, and no risk of reading a preview that reflects an older,
  already-edited version of the YAML.
- `validate` is already cheap and already run before every edit-review cycle per
  `03-extraction-review-workflow.md` §2.3 step 4 — no new step added to the human
  workflow, just a richer artifact produced by the step that's already there.

A standalone `preview` subcommand is not needed for the core workflow, but could be
added later purely as a convenience if `validate`'s stdout report turns out to be
wanted separately from the file (e.g. scripting `validate` without touching disk).
Not designing that now — YAGNI until it's actually asked for.

## 4. Explicitly out of scope

- **Editing via the preview.** The preview is read-only, generated, and safe to
  delete/regenerate at any time. All edits go through the YAML (hand-edited or via
  chat), per `03-extraction-review-workflow.md`.
- **A TUI or interactive terminal app.** Bigger engineering lift, a new dependency,
  and doesn't fit "the agent and the human are both just reading/writing a file."
- **Making Tier 2's live diff a hard requirement.** It must degrade gracefully to
  Tier 1's offline view; a broken network must never block a review from happening
  at all.

## 5. Open question for the user

Markdown (git-diffable, no dependencies, opens in any editor) vs. a self-contained
HTML file generated directly by the CLI (not via Artifact — just a plain `.html`
written to disk, styled with inline CSS) as the Tier-1 format. Markdown is the
default assumption above since it's simpler and the Tier-3 Artifact path already
covers "I want to see something visually nicer" for session use — but if the
`.html`-on-disk route is preferred (e.g. to open directly in a browser without
Claude Code), that's a small change to this design, not a different architecture.
