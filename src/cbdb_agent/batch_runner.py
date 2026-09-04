"""Submission engine: takes a validated StagingBatch and actually submits it via
MutationApi, one proposal at a time, in topological order.

Both cli.py entry points (`submit --staging` and `submit --input`) go through this
module - `--input`'s already-structured JSON records are converted into a
StagingBatch first (see load_input_batch below) precisely so both paths share this
one execution engine instead of duplicating submission logic.

One deliberate exception to per-record isolation: an authentication or
authorization failure is a WHOLE-BATCH condition, not a per-record one - see
_ABORTING_ERRORS below.

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

from .http_client import AuthenticationError, AuthorizationError, CbdbApiError
from .models import FieldWhitelistError, find_spec_by_alias
from .mutation_api import MutationApi
from .person_id import PersonIdError, get_max_person_id, is_person_id_taken, validate_new_person_id
from .staging import (
    Proposal,
    ProposalCurrentState,
    StagingBatch,
    resolve_target_pk,
    topological_submission_order,
)


@dataclass
class ProposalResult:
    proposal_id: str
    status: Literal[
        "success", "failed", "skipped_dependency_failed", "skipped_auth_aborted"
    ]
    response: dict[str, Any] | None = None
    error: str | None = None
    resolved_person_id: int | None = None
    resolved_target_pk: dict[str, Any] | None = None


# Errors that mean "the credentials are broken", not "this record is broken".
# Retrying the next 17 proposals with the same dead token cannot succeed, and each
# attempt spends one slot of the per-source-IP failed-auth budget (60/minute,
# counted per IP and shared with every other Bearer client behind the same egress
# IP - API.md 1.3, AGENTS.md rule 10). So the correct behaviour is to stop the
# batch immediately and report the remainder as skipped, rather than to isolate
# the failure per record the way a 409/422 is isolated.
#   - AuthenticationError (401): token invalid/expired/revoked.
#   - AuthorizationError (403): account not active, or lacks canWriteDirectly().
# Both are properties of the account, identical for every proposal in the batch.
_ABORTING_ERRORS = (AuthenticationError, AuthorizationError)


def _skip_rest_of_batch(
    order: list[Proposal], stopped_at_index: int, exc: Exception, failed_id: str
) -> list[ProposalResult]:
    """Mark every proposal after `stopped_at_index` as skipped by an auth abort.

    Indexed by POSITION, deliberately - `order.index(proposal)` would be wrong
    here: Proposal is a pydantic model with structural equality, so two proposals
    that happen to carry identical field values (entirely possible for, say, two
    identical altname rows on different people before person_id resolution) would
    make index() return the FIRST match and silently mis-slice the remainder.
    """
    return [
        ProposalResult(
            proposal_id=later.id,
            status="skipped_auth_aborted",
            error=(
                "batch aborted: authentication/authorization failed on proposal "
                f"{failed_id!r} ({exc}). Fix the token or the account's permissions "
                "and re-run; nothing after that proposal was attempted."
            ),
        )
        for later in order[stopped_at_index + 1 :]
    ]


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

    for index, proposal in enumerate(order):
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
                except _ABORTING_ERRORS as exc:
                    # allocate_person_id() makes AUTHENTICATED reads
                    # (GET /api/v2/persons, GET /api/v2/get - see person_id.py), so
                    # a dead token fails here too, before any write is attempted. A
                    # batch of N "NEW" persons would otherwise produce N failed-auth
                    # attempts against the shared per-IP budget without ever sending
                    # a single mutating request. Same abort as the write stage.
                    results.append(
                        ProposalResult(proposal_id=proposal.id, status="failed", error=str(exc))
                    )
                    results.extend(_skip_rest_of_batch(order, index, exc, proposal.id))
                    return results
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
        # Carry an approval-gated proposal's signer into meta.comment, so the
        # sign-off lands in the SERVER's own `operations` row and is not only
        # recorded in this repo's staging file (AGENTS.md rule 12; the comment is
        # what `direct` mode writes to that row's `__note`, per API.md 4.3).
        approved_by = (proposal.approved_by or "").strip() or None
        comment = None
        if approved_by:
            comment = (
                f"approved_by: {approved_by} "
                f"(batch {batch.batch_id}, proposal {proposal.id})"
            )

        # NOTE the office duplicate pre-flight is NOT called here. It lives inside
        # MutationApi.create(), at the layer that actually sends the request, so a
        # direct library call cannot walk past it. It raises PreflightError, which is a
        # CbdbApiError and so lands in the per-proposal isolation below. Nothing extra
        # is recorded on success: the check is a GET through http_client, so its request
        # and response are already in the append-only audit log (AGENTS.md rule 8).
        try:
            if proposal.operation == "create":
                response = api.create(
                    spec.key,
                    person_id=resolved_pid,
                    target_pk=full_target_pk,
                    changes=proposal.changes,
                    resource_string=proposal.resource,
                    comment=comment,
                    approved_by=approved_by,
                )
            elif proposal.operation == "update":
                response = api.update(
                    spec.key,
                    person_id=resolved_pid,
                    target_pk=full_target_pk,
                    changes=proposal.changes,
                    resource_string=proposal.resource,
                    comment=comment,
                    approved_by=approved_by,
                )
            else:  # delete
                response = api.delete(
                    spec.key,
                    person_id=resolved_pid,
                    target_pk=full_target_pk,
                    resource_string=proposal.resource,
                    comment=comment,
                    approved_by=approved_by,
                )
        except _ABORTING_ERRORS as exc:
            # NOT per-record isolation: the credentials are broken, so every
            # remaining proposal would fail identically while burning the shared
            # per-IP failed-auth budget (see _ABORTING_ERRORS). Record this one as
            # failed, mark the rest as skipped, and stop.
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
            results.extend(_skip_rest_of_batch(order, index, exc, proposal.id))
            return results
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


def fetch_current_values(batch: StagingBatch, api: MutationApi) -> dict[str, ProposalCurrentState]:
    """Best-effort live old-vs-new diff support (docs/06-staging-preview-design.md
    Tier 2): for every `update`/`delete` proposal whose `person_id` is already a
    concrete, resolvable value (not `"NEW"` or a sibling reference to a create
    that hasn't happened yet in this batch), attempt one `GET /api/v2/get` to
    fetch the row's current values, for staging.render_preview_markdown() to
    diff against the proposed `changes`.

    Never raises - every failure mode (network, 404, an unresolved person_id, an
    unknown resource alias) is caught and reported as a
    `ProposalCurrentState(error=...)` for that one proposal, so a preview can
    always render something rather than fail outright over one broken lookup.
    An auth failure is the one case handled batch-wide instead of per-proposal:
    it stops further probing and marks every remaining update/delete proposal with
    the same reason, so one dead token costs one failed request rather than one
    per proposal (see _ABORTING_ERRORS). The preview still renders either way.
    This is presentational-only support for a nicer review experience - it must
    never be treated as a stand-in for `validate_for_submit()`'s hard structural
    checks, and callers must not skip that just because this ran successfully.
    """
    results: dict[str, ProposalCurrentState] = {}
    for proposal in batch.proposals:
        if proposal.operation not in ("update", "delete"):
            continue  # create: nothing exists yet to diff against

        # An empty person_id_map here is deliberate: we're previewing, not
        # submitting, so no "NEW" proposal has actually been allocated a real id
        # yet. This correctly treats "NEW" and any still-pending sibling
        # reference as unresolved for diffing purposes, even though the same
        # helper (with a populated map) is used differently by run_batch().
        resolved_pid = _resolve_person_id(proposal, {})
        if resolved_pid is None:
            results[proposal.id] = ProposalCurrentState(
                error="person_id not yet resolved in this batch (depends on a pending create)"
            )
            continue

        try:
            spec = find_spec_by_alias(proposal.resource)
            full_target_pk = resolve_target_pk(
                proposal, resolved_person_id=resolved_pid, spec_key=spec.key
            )
            body = api.get(spec.key, person_id=resolved_pid, target_pk=full_target_pk)
        except _ABORTING_ERRORS as exc:
            # Broken credentials are a batch-wide condition: stop probing rather
            # than 401 once per update/delete proposal and spend N slots of the
            # shared per-IP failed-auth budget (see _ABORTING_ERRORS). The preview
            # still renders - every proposal from here on just shows this reason
            # instead of a live diff.
            reason = f"live diff unavailable: {exc} (stopped after first auth failure)"
            for later in batch.proposals:
                if later.operation in ("update", "delete") and later.id not in results:
                    results[later.id] = ProposalCurrentState(error=reason)
            return results
        except Exception as exc:  # noqa: BLE001 - intentionally broad, see docstring:
            # this is best-effort presentational support, and it must degrade to
            # "couldn't fetch" for ANY failure mode rather than ever propagate and
            # abort the whole preview over one bad lookup.
            results[proposal.id] = ProposalCurrentState(error=str(exc))
            continue

        row = None
        if isinstance(body, dict):
            result = body.get("result")
            if isinstance(result, dict):
                row = result.get("row")
        if not isinstance(row, dict):
            # Covers both "missing" (row is None) and a malformed response where
            # the server returned something unexpected in its place (e.g. a list
            # or string) - either way this is "couldn't get a usable row", never
            # a crash, per this function's never-raises contract.
            results[proposal.id] = ProposalCurrentState(error="row not found in response")
        else:
            results[proposal.id] = ProposalCurrentState(row=row)

    return results
