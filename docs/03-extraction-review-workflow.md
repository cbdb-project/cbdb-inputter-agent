# Extraction & Interactive Review Workflow

Status: draft, pending review. Extends `01-implementation-plan.md` with a stage that
sits between "user has source material" and "data is submitted via `/api/v2/*`".

## 1. Problem

The user's real workflow isn't "I already have clean JSON records, submit them." It's:
they have unstructured source material — e.g. a classical-Chinese (文言文) biography —
and want an agent to (a) read it, (b) figure out which facts map to which CBDB
resources/fields, (c) flag anything ambiguous or conflicting for human judgment with a
suggested resolution, and (d) let the human review and bulk-edit the proposal before
anything is sent, then interactively negotiate the ambiguous parts — rather than
approving records one at a time.

Worked example used to pressure-test this design (a real task, not the full scope of
data types this system must eventually handle — treat it as one illustrative case,
not an exhaustive spec):
https://github.com/cbdb-project/cbdb_sqlite/issues/23#issuecomment-4742439661 — given
a piece of source material, (1) decide what should be submitted via which API calls,
(2) flag conflicting information that needs human judgement and propose a resolution.

## 2. Design

Insert an **extraction → staging file → human review/edit → confirm → submit**
pipeline, as milestone 4 in `01-implementation-plan.md` §10 — between milestone 3
(mutation wrappers) and milestone 5 (CLI + batch submission).

```
source text (pasted / file)
        │  (1) EXTRACT — done by the agent's own reading, not a separate script)
        ▼
data/staging/<batch-id>/proposal.yaml   ◄──── human edits directly, in bulk
        │  (2) REVIEW — human edits file + chats with agent about flagged conflicts
        ▼
data/staging/<batch-id>/proposal.yaml   (all conflicts resolved)
        │  (3) VALIDATE — cli.py checks the file against resource whitelists,
        │      person-before-subresource ordering, no unresolved conflicts
        ▼
python -m cbdb_agent submit --staging data/staging/<batch-id>/proposal.yaml
        │  (4) SUBMIT — existing mutation_api.py / http_client.py / audit_log.py path
        ▼
/api/v2/create|mutate  (dry-run by default, per AGENTS.md gates)
```

### 2.1 Why a staging *file*, not one-by-one chat questions

The user explicitly wants **bulk-comfortable editing**, not per-record confirmation
dialogs. A single editable file lets the user:
- scan all proposed rows at once, grouped by person/resource,
- delete/edit many rows in one pass in their own editor,
- diff against a previous version if the agent revises the proposal,
- keep a durable, versioned record of what was proposed vs. what was actually approved.

Chat is still used, but for a different purpose: discussing *specific flagged
conflicts* the agent couldn't resolve on its own, not for approving each row.

### 2.2 Staging file format — YAML, one file per batch

YAML over JSON for human readability and multi-line block scalars (`source_quote`,
`description`, `agent_reasoning` below read naturally as prose, not escaped JSON
strings). Note: this design does **not** rely on preserving hand-written `#` YAML
comments across agent rewrites — `PyYAML` (a plain load/dump library, no round-trip
comment support) is sufficient because every piece of "why" the agent needs to convey
(source quote, confidence, reasoning) is a structured field in the schema itself, not
a bare comment. A human is still free to add their own `#` comments while editing,
but should expect the agent's own rewrites (e.g. after resolving a conflict from chat)
to regenerate the file from the structured fields and not necessarily preserve
comments a human added by hand elsewhere in the file.

```yaml
batch_id: 2026-07-08-liu-zongyuan
source_excerpt: |
  柳宗元，字子厚，河东人。... (short excerpt or pointer to the full source file
  under data/staging/<batch-id>/source.txt, not necessarily inlined in full)

proposals:
  - id: p1                        # local id within this batch, for cross-referencing
    resource: basicinformation
    operation: create
    person_id: NEW                # "NEW" = agent must allocate one at submit time
                                   # (via person_id.py, per AGENTS.md rule 6);
                                   # otherwise an existing c_personid to update
    changes:
      c_name_chn: 柳宗元
      c_female: 0
      c_index_year: 773
      c_dy: <dynasty code — TODO, see conflict c1>
    source_quote: "柳宗元，字子厚，河东人"
    confidence: high
    conflicts: []

  - id: p2
    resource: altnames
    operation: create
    person_id: p1                 # references another proposal's local id, not yet
                                   # a real c_personid — resolved at submit time
    changes:
      c_alt_name_chn: 子厚
      c_alt_name_type_code: <TODO — "字" style code, see conflict c2>
    source_quote: "字子厚"
    confidence: high
    conflicts: []

  - id: p3
    resource: basicinformation
    operation: update
    person_id: 12345               # agent believes this person already exists
    changes:
      c_deathyear: <conflict — see c3>
    source_quote: "元和十四年卒" vs. another source implying a different year
    confidence: low
    conflicts:
      - id: c3
        field: c_deathyear
        description: >
          Source text gives 元和十四年 (819), but the person's existing record in
          CBDB already has c_deathyear=820 with a cited source. Both could be
          reconciled under different calendar conventions.
        options:
          - value: 819
            rationale: "Direct reading of 元和十四年卒 in this source."
          - value: 820
            rationale: "Existing CBDB record's current value; possibly using a
              different year-boundary convention (see index-year rules)."
        agent_suggestion: 819
        agent_reasoning: >
          The source text is explicit and internally consistent with other dated
          events in the same passage; recommend overriding, but flagging for human
          confirmation since this changes an existing record.
        resolution: null            # human must fill this in (or answer in chat and
                                     # have the agent fill it in) before submission

batch_notes: >
  Extracted from docs/03-extraction-review-workflow.md's worked example. Not
  exhaustive of every biographical relationship in the source text — see
  batch_notes for what was deliberately left out and why, if anything.
```

Design points:
- Every proposal carries `source_quote` and `confidence` — nothing is submitted
  without a traceable link back to the source text, independent of the server's or
  our own audit log (this is provenance for the *decision*, not the write itself).
- `conflicts: []` is required (even if empty) on every proposal, so validation has a
  single place to check "any unresolved conflicts anywhere in this batch?"
- `resolution: null` blocks submission. `resolution` can be set to one of the
  `options[].value`s, a free-text override, or `"defer"` (skip this one field/row for
  now, submit the rest of the batch).
- Local `id`s (`p1`, `p2`, `c3`) let the human and the agent refer to specific rows
  precisely in follow-up chat ("resolve c3 as 820") without needing full JSON paths.
- `person_id: NEW` / `person_id: p1` (referencing a sibling proposal) defers real
  `c_personid` allocation to submit time, so the whole batch can be drafted before any
  ID is committed — avoids burning IDs on proposals that get edited out.

### 2.3 Interaction loop

1. User provides source text (pasted in chat, or a file path).
2. The agent (this Claude Code session, using its own reading comprehension —
   *not* a bespoke NLP pipeline) reads it against `docs/00-target-system-brief.md`'s
   resource/field descriptions and drafts `data/staging/<batch-id>/proposal.yaml`,
   plus a short chat summary: "N proposals drafted, M flagged conflicts, see file at
   ... — want to review inline here or open the file?"
3. Human reviews. Two supported paths, freely mixed:
   - **Bulk file edit**: human opens the YAML, edits/deletes rows directly, saves.
   - **Chat negotiation**: human says e.g. "c3 应该是 820，参考的是XX" — the agent
     updates that conflict's `resolution` in the file directly and confirms back.
4. Agent (or `cli.py validate --staging ...`) re-checks the file: any `resolution:
   null` remaining? Any proposal referencing a sibling `id` that was deleted? Any
   field not in that resource's whitelist (front-running the server's own validation,
   per `01-implementation-plan.md` §6)? Reports remaining issues, loops back to step 3.
5. Once clean, human explicitly confirms ("提交" / "submit"). Only then does
   `python -m cbdb_agent submit --staging ...` run, translating each proposal into the
   ordered (person-before-subresource) `/api/v2/*` calls via the existing
   `mutation_api.py`, subject to the same dry-run/production gates as any other
   submission (`AGENTS.md` rule 4).
6. On completion, the staging file is copied to `data/processed/<batch-id>/` alongside
   the server responses (mirroring the plan's existing `data/inbox` → `data/processed`
   pattern in `01-implementation-plan.md` §7), so the original proposal, its
   human-resolved conflicts, and the actual server outcome are all in one place for
   later audit.

### 2.4 What this does NOT do

- It does not call any LLM API itself — the "extraction" is the Claude Code agent
  session's own reading of the text, using the field/resource knowledge already
  documented in `00-target-system-brief.md`. No separate model-calling code is added
  to `src/cbdb_agent/`.
- It does not auto-resolve conflicts. `agent_suggestion`/`agent_reasoning` are always
  advisory; `resolution: null` is a hard submission blocker enforced by validation,
  not just a convention.
- It is not resource-type-specific — the worked example above is one illustrative
  case (a classical-Chinese biography implying `basicinformation` + `altnames` +
  possibly `kinship`/`offices`/`events`), not a fixed template. The staging schema
  (`resource`, `operation`, `person_id`, `changes`, `source_quote`, `confidence`,
  `conflicts`) is generic across all resources listed in the brief §3.

## 3. Repo changes this adds

```
skills/cbdb-data-entry/
  SKILL.md            # extended: extraction/staging workflow, not just direct submit
data/
  staging/            # new: <batch-id>/proposal.yaml, <batch-id>/source.txt
src/cbdb_agent/
  staging.py          # new: load/validate/save the YAML staging schema
                       # (whitelist checks, conflict-resolution checks, id-reference
                       # checks) — reused by both the skill and cli.py
  cli.py               # new subcommands: `validate --staging`, `submit --staging`
                        # (in addition to the existing `submit --input` batch path)
```

`staging.py`'s validation logic is deliberately the *same* per-resource field
whitelist already planned for `models.py` (`01-implementation-plan.md` §6) — not a
separate copy — so a proposal that would fail server-side validation is caught here
too, before ever reaching chat-approved status.

## 4. Milestone update

This is **Milestone 4 — Extraction staging** in `01-implementation-plan.md` §10,
between Milestone 3 (mutation wrappers) and Milestone 5 (CLI + batch submission):

- `staging.py`: YAML schema load/save, whitelist validation reusing `models.py`,
  conflict-resolution completeness check, sibling-id reference resolution.
- `cli.py` additions: `validate --staging <path>`, `submit --staging <path>`.
- `skills/cbdb-data-entry/SKILL.md` extended with the extraction/review loop from
  §2.3 above.
- No new external dependencies beyond a plain YAML library (`PyYAML`) — see §2.2 on
  why comment round-tripping is not required here.

This does not change the review workflow itself (`01-implementation-plan.md` §11
still applies: review agent → fix → codex → fix → log → next milestone).
