# -*- coding: utf-8 -*-
"""Continue a batch that stopped part-way through.

A batch stops mid-flight when a mutating request comes back indeterminate - a
timeout or a 5xx, where the row may or may not exist. `batch_runner` does not
retry it and does not carry on (AGENTS.md rule 11): one uncertain row, named in
`results.json`, is the recoverable outcome. This module is the recovery.

It is deliberately a *generator*, not an editor. What comes out is a new staging
batch, written to `data/staging/<new-batch-id>/proposal.yaml`, that goes through
`validate --staging`, the review page and `submit --staging` like any other. The
alternative - opening the old `proposal.yaml` and deleting the rows that landed -
is a hand-edit of a file whose whole purpose is to be reviewed and audited, and it
loses the record of which ids the first attempt assigned.

Three things it must get right, and each was a way to write permanent wrong data:

* **What landed is dropped, and its primary key is remembered.** A create that
  succeeded must not be sent twice: these tables do not dedupe and cannot be
  deleted.
* **References to what landed become the real ids.** A child proposal carrying
  `{"ref": "addr-ming-兩淮-泰州分司-1368"}` cannot reference a proposal that is no
  longer in the batch. The reference is replaced by the `c_addr_id` the server
  assigned on the first run, read out of `results.json`.
* **An indeterminate row is not guessed.** It is exactly the row nobody knows
  about, so `plan()` refuses until a human has reconciled it against
  `GET /api/v2/operations` and said, in writing, whether it landed.

It works from the *submitted* `proposal.yaml` and its `results.json`, never from
whatever the generator would produce today. Those two files are the record of what
was sent and what happened; a regeneration could differ from both and would resume
with different content under the same proposal ids.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import find_spec_by_alias
from .staging import (
    Proposal,
    StagingBatch,
    is_pk_ref,
    iter_pk_refs,
    load_staging_file,
    pk_ref_target,
)


class ResumeError(RuntimeError):
    """The batch cannot be resumed from what was supplied."""


# The statuses `batch_runner` writes, and what each means here. Every one of
# `ProposalResult`'s Literal values has to appear in exactly one of these, and
# tests/test_resume.py fails if the two ever drift - an unrecognised status that
# fell through to "outstanding" would re-send a create that cannot be deleted.
LANDED = "success"
INDETERMINATE = "failed"          # may or may not have been written - see rule 11
NOT_ATTEMPTED = ("skipped_auth_aborted", "skipped_dependency_failed")
KNOWN_STATUSES = (LANDED, INDETERMINATE) + NOT_ATTEMPTED


@dataclass
class ResumePlan:
    """What a resume would do, before anything is written."""

    landed: dict[str, Any] = field(default_factory=dict)
    """proposal id -> the primary key the server assigned it."""

    outstanding: list[Proposal] = field(default_factory=list)
    """The proposals still to send, references already rewritten."""

    reconciled: dict[str, Any] = field(default_factory=dict)
    """proposal id -> None (absent) or the pk it turned out to have."""

    notes: list[str] = field(default_factory=list)
    """What a reader of the new batch needs to know about where it came from."""


def read_results(path: Path) -> list[dict]:
    """`results.json` as `batch_runner` wrote it: a list, one entry per proposal."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ResumeError(
            f"{path} is not a results list; `batch_runner` writes a JSON array "
            f"with one entry per proposal, and this is {type(data).__name__}")
    for i, entry in enumerate(data):
        if not isinstance(entry, dict) or "proposal_id" not in entry:
            raise ResumeError(f"{path}: entry {i} has no proposal_id")
    return data


def assigned_pk(entry: dict, resource: str) -> Any:
    """The scalar primary key a successful create was given.

    Read off `result.pk`, the same place `batch_runner` reads it from while the
    batch is running, and refused unless the resource has exactly one
    server-assigned key field - anything else and "the id" is not a single value,
    so a child reference could not be resolved to one either.

    **Only called for proposals something still outstanding references.** Most
    resources have no server-assigned key at all - `addr_belongs_data`, whose own
    four-column key IS the row, and the eleven person resources among them - and
    asking for an id they never had would refuse to resume any batch containing
    one. Which, for the batch this module was written for, means the 59 hierarchy
    edges it generates: interrupted after one of those landed, the tool would have
    had nothing to say. Nothing references an edge, so nothing needs its id.
    """
    spec = find_spec_by_alias(resource)
    fields = sorted(spec.server_assigned_pk_fields)
    if len(fields) != 1:
        raise ResumeError(
            f"{entry['proposal_id']}: {resource} has {len(fields)} server-assigned "
            f"key fields ({fields or 'none'}), so there is no single id for a "
            f"reference to resolve to")
    response = entry.get("response")
    result = response.get("result") if isinstance(response, dict) else None
    for container in ((result or {}).get("pk"), (result or {}).get("row")):
        if isinstance(container, dict) and container.get(fields[0]) is not None:
            return container[fields[0]]
    raise ResumeError(
        f"{entry['proposal_id']} is recorded as {LANDED!r} but its response "
        f"carries no {fields[0]}. Without the id it was given, nothing that "
        f"references it can be resumed - reconcile it against "
        f"GET /api/v2/operations by hand.")


def plan(
    batch: StagingBatch,
    results: list[dict],
    *,
    reconciled: dict[str, Any] | None = None,
) -> ResumePlan:
    """Work out what is left to send. Writes nothing.

    `reconciled` answers for each indeterminate proposal: `None` if it did not
    land (so it is re-sent), or the primary key it turned out to have (so it is
    treated as landed). Required for every such proposal, because that is the one
    question this module cannot answer for itself.
    """
    reconciled = dict(reconciled or {})
    seen: set[str] = set()
    for p in batch.proposals:
        if p.id in seen:
            # Everything here is keyed by proposal id, so two rows sharing one id
            # collapse into whichever the dict kept - and if the other landed,
            # the row that never went vanishes without a word. `find_issues`
            # would have caught it, but `resume` does not validate its input.
            raise ResumeError(
                f"{batch.batch_id!r} has more than one proposal with id {p.id!r}. "
                f"Which of them an outcome refers to is not something this can "
                f"decide.")
        seen.add(p.id)
    by_id = {p.id: p for p in batch.proposals}
    status = {}
    for entry in results:
        pid = entry["proposal_id"]
        if pid not in by_id:
            raise ResumeError(
                f"results.json names proposal {pid!r}, which is not in this "
                f"staging file. The two are not from the same batch.")
        if pid in status:
            # Two outcomes for one proposal: either the file has been merged from
            # two runs or it is corrupt. Silently keeping the last would decide
            # "was this already created?" by file order.
            raise ResumeError(
                f"results.json records proposal {pid!r} twice, with status "
                f"{status[pid].get('status')!r} and {entry.get('status')!r}. "
                f"Which one happened is not something this can pick.")
        if entry.get("status") not in KNOWN_STATUSES:
            # Anything unrecognised would fall through to "outstanding" and be
            # sent again. For these tables that is a permanent duplicate, so an
            # unreadable outcome stops the resume instead.
            raise ResumeError(
                f"{pid}: status {entry.get('status')!r} is not one this knows "
                f"({', '.join(KNOWN_STATUSES)}). Whether it was written is "
                f"therefore unknown, and an unknown outcome is not resumable - "
                f"reconcile it by hand.")
        status[pid] = entry

    missing = [p.id for p in batch.proposals if p.id not in status]
    if missing:
        raise ResumeError(
            f"{len(missing)} proposal(s) have no entry in results.json "
            f"({', '.join(missing[:5])}{'…' if len(missing) > 5 else ''}). A "
            f"proposal whose outcome was never recorded cannot be classed as "
            f"outstanding - it might have been sent.")

    plan_ = ResumePlan()

    # --- 1. what landed --------------------------------------------------
    # The id is resolved below, and only where one is needed: see assigned_pk.
    landed_ids = {pid for pid, e in status.items() if e.get("status") == LANDED}

    # --- 2. the indeterminate ones, which a human has to have ruled on ------
    unresolved = [pid for pid, e in status.items()
                  if e.get("status") == INDETERMINATE and pid not in reconciled]
    if unresolved:
        raise ResumeError(
            "these proposals are indeterminate - the request went out and no "
            "answer came back, so the row may or may not exist:\n  "
            + "\n  ".join(sorted(unresolved))
            + "\nReconcile each against GET /api/v2/operations (and, for "
              "ADDR_CODES, GET /api/select/search/addr) and say so explicitly. "
              "Resuming without that either re-creates a row that cannot be "
              "deleted, or drops one that was never written.")

    for pid, outcome in reconciled.items():
        if pid not in status:
            raise ResumeError(
                f"reconciled {pid!r}, which is not in this batch's results")
        if status[pid].get("status") != INDETERMINATE:
            raise ResumeError(
                f"{pid!r} is recorded as {status[pid].get('status')!r}, not "
                f"{INDETERMINATE!r}; there is nothing to reconcile and overriding "
                f"a recorded outcome is not what this is for")
        plan_.reconciled[pid] = outcome
        if outcome is not None:
            landed_ids.add(pid)
            plan_.landed[pid] = outcome

    # --- 3. the ids, where there are any ----------------------------------
    # Read for every landed proposal, because a reader of the resumed batch wants
    # to see them - but only REQUIRED for the ones something still outstanding
    # references. Most resources have no server-assigned key at all, and
    # demanding one would refuse to resume any batch containing one of them.
    outstanding = [p for p in batch.proposals if p.id not in landed_ids]
    needed = {target for p in outstanding for _w, _f, target in iter_pk_refs(p)}
    for pid in sorted(landed_ids):
        if pid in plan_.landed:
            continue                      # a reconciliation already supplied it
        try:
            plan_.landed[pid] = assigned_pk(status[pid], by_id[pid].resource)
        except ResumeError:
            if pid in needed:
                raise
            plan_.landed[pid] = None

    # --- 4. what is left, with references to landed rows resolved -----------
    resolvable = {k: v for k, v in plan_.landed.items() if v is not None}
    for proposal in outstanding:
        plan_.outstanding.append(_rewrite_refs(proposal, resolvable, by_id))

    plan_.notes = _notes(batch, status, plan_)
    return plan_


def _rewrite_refs(proposal: Proposal, landed: dict[str, Any],
                  by_id: dict[str, Proposal]) -> Proposal:
    """A copy of `proposal` with references to landed rows replaced by their ids.

    References to proposals still in the batch are left alone - `batch_runner`
    resolves those at submit time, as it always has.
    """
    for _where, fieldname, target in iter_pk_refs(proposal):
        if target in landed or target in by_id:
            continue
        raise ResumeError(
            f"{proposal.id}.{fieldname} references {target!r}, which is neither "
            f"in this batch nor recorded as landed. A reference that resolves to "
            f"nothing would be sent as a literal dict and land as part of a "
            f"primary key.")

    def walk(value: Any) -> Any:
        if isinstance(value, (list, tuple)):
            return [walk(v) for v in value]
        target = pk_ref_target(value) if is_pk_ref(value) else None
        return landed[target] if target in landed else value

    data = proposal.model_dump()
    for slot in ("target_pk", "changes"):
        if data.get(slot):
            data[slot] = {k: walk(v) for k, v in data[slot].items()}
    return Proposal.model_validate(data)


def _notes(batch: StagingBatch, status: dict[str, dict],
           plan_: ResumePlan) -> list[str]:
    counts: dict[str, int] = {}
    for entry in status.values():
        counts[entry.get("status") or "?"] = counts.get(entry.get("status") or "?", 0) + 1
    lines = [
        f"Resumes batch {batch.batch_id!r}, which stopped part-way through: "
        + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) + ".",
        "",
        f"{len(plan_.landed)} proposal(s) already landed and are NOT repeated here. "
        f"Every reference to one of them has been replaced by the primary key the "
        f"server assigned on that run:",
    ]
    lines += [f"  {pid} -> {pk}" if pk is not None
              else f"  {pid} (nothing references it, so its id was not needed)"
              for pid, pk in sorted(plan_.landed.items())]
    if plan_.reconciled:
        lines += ["", "Reconciled by hand after the interruption:"]
        lines += [f"  {pid}: "
                  + ("did not land, so it is sent again" if outcome is None
                     else f"had in fact landed as {outcome}")
                  for pid, outcome in sorted(plan_.reconciled.items())]
    lines += ["",
              f"{len(plan_.outstanding)} proposal(s) remain. The duplicate checks "
              f"that gate these tables run again before anything is sent."]
    return lines


def resume_batch(
    batch: StagingBatch, results: list[dict], new_batch_id: str,
    *, reconciled: dict[str, Any] | None = None,
) -> tuple[StagingBatch, ResumePlan]:
    """The new staging batch, plus the plan it was built from."""
    plan_ = plan(batch, results, reconciled=reconciled)
    if not plan_.outstanding:
        raise ResumeError(
            f"nothing is outstanding in {batch.batch_id!r} - every proposal "
            f"landed. There is no batch to resume.")
    excerpt = (batch.source_excerpt or "").rstrip()
    resumed = StagingBatch(
        batch_id=new_batch_id,
        source_excerpt="\n".join(
            ([excerpt, "", "-" * 70, ""] if excerpt else []) + plan_.notes),
        proposals=plan_.outstanding,
        # Carried, not rebuilt: whatever the first attempt's author wrote about
        # this batch is still true of the part that has not been sent.
        batch_notes=batch.batch_notes,
    )
    return resumed, plan_


def load_reconciliation(path: Path) -> dict[str, Any]:
    """A hand-written file saying what became of each indeterminate proposal.

        {
          "addr-ming-長蘆-長蘆都轉運鹽使司-1373": {
            "landed": false,
            "evidence": "GET /api/v2/operations shows 11 rows, 365606-365616, none
                         for this name; /api/select/search/addr?q=... returns 0."
          }
        }

    `evidence` is required and is not read by anything - it is there because the
    person writing `landed: false` is asserting something about a table with no
    delete path, and an assertion like that belongs next to what it rests on.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ResumeError(f"{path} must be an object keyed by proposal id")
    out: dict[str, Any] = {}
    for pid, entry in data.items():
        if not isinstance(entry, dict) or "landed" not in entry:
            raise ResumeError(
                f"{path}: {pid!r} must be an object with a `landed` boolean")
        if not isinstance(entry["landed"], bool):
            # `"false"` is a truthy string. Read loosely, a quoted answer in a
            # hand-written file silently means the opposite of what it says, and
            # what it would mean here is "drop a row that was never written".
            raise ResumeError(
                f"{path}: {pid!r} has landed={entry['landed']!r}; it must be the "
                f"JSON literal true or false, not a string or a number")
        if not str(entry.get("evidence") or "").strip():
            raise ResumeError(
                f"{path}: {pid!r} has no `evidence`. Saying an indeterminate row "
                f"did or did not land is a claim about a table that cannot be "
                f"corrected; record what it rests on.")
        if entry["landed"]:
            if entry.get("pk") is None:
                raise ResumeError(
                    f"{path}: {pid!r} is marked as landed but carries no `pk`. "
                    f"Anything referencing it needs the id it was given.")
            if not isinstance(entry["pk"], int) or isinstance(entry["pk"], bool):
                # `{"c_addr_id": 702725}` is the plausible copy-paste from
                # results.json, and it would be substituted into a child's key
                # as a dict.
                raise ResumeError(
                    f"{path}: {pid!r} has pk={entry['pk']!r}; it must be the "
                    f"integer id itself, not the object results.json wraps it in")
            out[pid] = entry["pk"]
        else:
            out[pid] = None
    return out


def load_processed(directory: Path) -> tuple[StagingBatch, list[dict]]:
    """The `proposal.yaml` and `results.json` a submitted batch left behind."""
    directory = Path(directory)
    proposal = directory / "proposal.yaml"
    results = directory / "results.json"
    for path in (proposal, results):
        if not path.exists():
            raise ResumeError(
                f"{path} not found. A resume needs both the batch that was sent "
                f"and what happened to it; `submit` writes them side by side "
                f"under data/processed/<batch-id>/.")
    return load_staging_file(str(proposal)), read_results(results)


__all__ = [
    "INDETERMINATE",
    "LANDED",
    "NOT_ATTEMPTED",
    "ResumeError",
    "ResumePlan",
    "assigned_pk",
    "load_processed",
    "load_reconciliation",
    "plan",
    "read_results",
    "resume_batch",
]
