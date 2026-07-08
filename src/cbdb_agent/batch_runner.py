"""Submission engine: takes a validated StagingBatch and actually submits it via
MutationApi, one proposal at a time, in topological order.

Both cli.py entry points (`submit --staging` and `submit --input`) go through this
module - `--input`'s already-structured JSON records are converted into a
StagingBatch first (see load_input_batch below) precisely so both paths share this
one execution engine instead of duplicating submission logic.

Per-record failure isolation (docs/01-implementation-plan.md section 7): a runtime
failure (409/422/etc.) on one proposal stops processing THAT proposal only; the
batch continues with the next one. This is distinct from validate_for_submit()'s
pre-flight structural checks, which are a hard gate for the WHOLE batch before any
submission starts - by the time run_batch() is called, the batch has already been
declared structurally safe. What run_batch() isolates is failures the server
reports at submission time (a conflict, a permission error), not a client-side
whitelist mistake, which should have been caught earlier.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .http_client import CbdbApiError
from .models import FieldWhitelistError, find_spec_by_alias
from .mutation_api import MutationApi
from .person_id import PersonIdError, get_max_person_id, is_person_id_taken, validate_new_person_id
from .staging import Proposal, StagingBatch, resolve_target_pk, topological_submission_order


@dataclass
class ProposalResult:
    proposal_id: str
    status: Literal["success", "failed", "skipped_dependency_failed"]
    response: dict[str, Any] | None = None
    error: str | None = None
    resolved_person_id: int | None = None
    resolved_target_pk: dict[str, Any] | None = None


def allocate_person_id(
    api: MutationApi, *, already_claimed: set[int] = frozenset(), max_attempts: int = 10_000
) -> int:
    """Pick an unused, valid c_personid for a new person (AGENTS.md rule 6).

    Starts at max(existing)+1 and probes forward past any already-taken ID
    (shouldn't normally be needed - c_personid should be contiguous - but a gap
    from a prior deletion or out-of-band insert is possible) up to max_attempts,
    which is comfortably inside the server's max(existing)+10000 ceiling.

    `already_claimed` must include every c_personid this same run_batch() call has
    already allocated to an earlier "NEW" proposal. Without it, two independent
    "NEW" persons in the same batch could be allocated the SAME id: in particular
    under dry-run, a "create" never actually persists anything server-side, so a
    second `is_person_id_taken()` check against the real server would still see
    the id as free even though this run already handed it to an earlier proposal.

    Under dry-run, this never touches the network at all: `GET /api/v2/persons`/
    `GET /api/v2/get` are read-only and would normally still go through even in
    dry-run (http_client.py never short-circuits GET), but a dry-run's whole
    point is to preview a batch without touching the target system - real ID
    discovery isn't needed since nothing is actually going to be created. Returns
    an obviously-fake negative placeholder instead (never a valid c_personid, so
    it can't be mistaken for a real one if a dry-run result is inspected later).
    """
    if api.client.dry_run:
        candidate = -1
        while candidate in already_claimed:
            candidate -= 1
        return candidate

    max_existing = get_max_person_id(api.client)
    candidate = max_existing + 1
    for _ in range(max_attempts):
        validate_new_person_id(candidate, max_existing)
        if candidate not in already_claimed and not is_person_id_taken(api.client, candidate):
            return candidate
        candidate += 1
    raise PersonIdError(
        f"Could not find a free c_personid after {max_attempts} attempts starting "
        f"from {max_existing + 1}"
    )


def _resolve_person_id(
    proposal: Proposal, person_id_map: dict[str, int]
) -> int | None:
    """Return the resolved integer person_id for `proposal`, or None if it depends
    on a sibling that hasn't been (successfully) resolved yet."""
    pid = proposal.person_id
    if isinstance(pid, int):
        return pid
    if isinstance(pid, str):
        if pid == "NEW":
            return person_id_map.get(proposal.id)
        if pid.lstrip("-").isdigit():
            return int(pid)
        return person_id_map.get(pid)
    return None


def run_batch(batch: StagingBatch, api: MutationApi) -> list[ProposalResult]:
    """Submit every submittable proposal in `batch`, in dependency order.

    Callers MUST call staging.validate_for_submit(batch) first - this function
    does not re-validate structure, whitelists, or conflict resolution; it only
    handles the runtime concerns (person_id allocation, per-proposal failure
    isolation, skipping proposals whose dependency failed).
    """
    order = topological_submission_order(batch)
    person_id_map: dict[str, int] = {}
    results: list[ProposalResult] = []

    for proposal in order:
        resolved_pid = _resolve_person_id(proposal, person_id_map)

        if resolved_pid is None:
            # Either this proposal's own person_id is "NEW" and needs allocating
            # (handled just below, only for basicinformation creates), or it
            # depends on a sibling that failed/was skipped earlier in this run.
            spec = find_spec_by_alias(proposal.resource)
            is_new_person_create = (
                proposal.person_id == "NEW"
                and spec.key == "basicinformation"
                and proposal.operation == "create"
            )
            if is_new_person_create:
                try:
                    resolved_pid = allocate_person_id(api, already_claimed=set(person_id_map.values()))
                    person_id_map[proposal.id] = resolved_pid
                except (CbdbApiError, PersonIdError) as exc:
                    results.append(
                        ProposalResult(proposal_id=proposal.id, status="failed", error=str(exc))
                    )
                    continue
            else:
                results.append(
                    ProposalResult(
                        proposal_id=proposal.id,
                        status="skipped_dependency_failed",
                        error="a sibling proposal this one depends on did not succeed",
                    )
                )
                continue

        spec = find_spec_by_alias(proposal.resource)
        full_target_pk = resolve_target_pk(proposal, resolved_person_id=resolved_pid, spec_key=spec.key)

        try:
            if proposal.operation == "create":
                response = api.create(
                    spec.key,
                    person_id=resolved_pid,
                    target_pk=full_target_pk,
                    changes=proposal.changes,
                    resource_string=proposal.resource,
                )
            elif proposal.operation == "update":
                response = api.update(
                    spec.key,
                    person_id=resolved_pid,
                    target_pk=full_target_pk,
                    changes=proposal.changes,
                    resource_string=proposal.resource,
                )
            else:  # delete
                response = api.delete(
                    spec.key,
                    person_id=resolved_pid,
                    target_pk=full_target_pk,
                    resource_string=proposal.resource,
                )
        except (CbdbApiError, FieldWhitelistError) as exc:
            # Per-record isolation (AGENTS.md rule 5): never retry with modified
            # data, never let one proposal's failure raise out of the batch loop.
            # FieldWhitelistError is included because mutation_api.create() can
            # still raise it here even after validate_for_submit() passed - e.g.
            # a target_pk/changes value mismatch on a shared PK field, which
            # find_issues() checks for presence/whitelist membership but not
            # value agreement between the two.
            results.append(
                ProposalResult(
                    proposal_id=proposal.id,
                    status="failed",
                    error=str(exc),
                    resolved_person_id=resolved_pid,
                    resolved_target_pk=full_target_pk,
                )
            )
            if spec.key == "basicinformation" and proposal.operation == "create":
                person_id_map.pop(proposal.id, None)  # never record a failed create
            continue

        if spec.key == "basicinformation" and proposal.operation == "create":
            person_id_map[proposal.id] = resolved_pid

        results.append(
            ProposalResult(
                proposal_id=proposal.id,
                status="success",
                response=response,
                resolved_person_id=resolved_pid,
                resolved_target_pk=full_target_pk,
            )
        )

    return results
