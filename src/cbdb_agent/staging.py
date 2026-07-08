"""YAML staging-file schema, load/save, and validation.

Implements docs/03-extraction-review-workflow.md section 2.5. A staging batch is a
human-reviewable, bulk-editable proposal for a set of CBDB writes, drafted from
unstructured source material, before anything is actually submitted.

Design note on `target_pk` (see docs/03 section 2.2's worked example, where a
`basicinformation` proposal omits `target_pk` entirely and the design point for
`altnames` shows `target_pk` holding only `c_alt_name_chn`/`c_alt_name_type_code`
"alongside person_id" - NOT including `c_personid`): a Proposal's `target_pk` here
holds the resource's PK fields EXCLUDING `c_personid`, since `person_id` already
carries that value (and may be a placeholder - "NEW" or a sibling proposal id - not
yet resolved to a real integer when the file is drafted). The full `target.pk` sent
to the real API (which does include `c_personid` for resources whose PK has it) is
reconstructed at resolution time by merging in the resolved `person_id`.
"""

from __future__ import annotations

from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from .models import FieldWhitelistError, find_spec_by_alias, get_resource_spec

STAGING_PERSONID_FIELD = "c_personid"


class StagingError(ValueError):
    """Base class for staging-file structural/validation errors.

    `issues` (a list[Issue], set by validate_for_submit()) carries the structured
    findings when available, so a caller (e.g. a future cli.py) can act on them
    programmatically instead of re-parsing the message string.
    """

    issues: list["Issue"] | None = None


class ConflictOption(BaseModel):
    value: str | int | float
    rationale: str


class Conflict(BaseModel):
    id: str
    field: str
    description: str
    options: list[ConflictOption] = Field(default_factory=list)
    agent_suggestion: str | int | float | None = None
    agent_reasoning: str | None = None
    resolution: str | int | float | None = None  # None = unresolved, blocks submit


class Proposal(BaseModel):
    id: str
    resource: str
    operation: Literal["create", "update", "delete"]
    person_id: str | int  # "NEW", a sibling proposal's id, or a real c_personid
    target_pk: dict[str, Any] | None = None  # PK fields EXCLUDING c_personid; see module docstring
    # dict[str, Any], not docs/03 section 2.5's literal dict[str, str|int|float] -
    # deliberately loosened because pseudo-fields need richer types (e.g. events'
    # c_addr_id is a list[int], c_addr_cleared is a bool). This does mean pydantic
    # won't reject a malformed value (e.g. an accidentally-nested dict) under a
    # valid key - models.py's whitelist check still catches an invalid *key*, but
    # not a structurally-wrong *value*.
    changes: dict[str, Any] = Field(default_factory=dict)
    source_quote: str
    confidence: Literal["high", "medium", "low"]
    conflicts: list[Conflict] = Field(default_factory=list)

    @field_validator("target_pk")
    @classmethod
    def _target_pk_excludes_personid(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value and STAGING_PERSONID_FIELD in value:
            raise ValueError(
                f"target_pk must not include {STAGING_PERSONID_FIELD!r} - that "
                "value comes from person_id (which may still be a placeholder); "
                "see staging.py module docstring"
            )
        return value


class StagingBatch(BaseModel):
    batch_id: str
    source_excerpt: str | None = None
    proposals: list[Proposal] = Field(default_factory=list)
    batch_notes: str | None = None


class Issue(BaseModel):
    proposal_id: str | None
    severity: Literal["error", "unresolved_conflict"]
    message: str


def load_staging_file(path: str) -> StagingBatch:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return StagingBatch.model_validate(raw)


def save_staging_file(batch: StagingBatch, path: str) -> None:
    # Known cosmetic limitation: plain yaml.safe_dump() does not reproduce the
    # multi-line `|` block-scalar style shown in docs/03 section 2.2's worked
    # example for long prose fields (source_quote, description, etc.) - it comes
    # back as a folded/quoted scalar instead. Round-trip data fidelity is still
    # exact (verified in tests/test_staging.py); this only affects how pleasant
    # the regenerated file is to read by hand. Worth a custom YAML representer if
    # this becomes a real friction point during Milestone 6/7 usage.
    data = batch.model_dump(exclude_none=False)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def find_issues(batch: StagingBatch) -> list[Issue]:
    """Collect every structural/whitelist/conflict issue in the batch.

    Does not raise - callers decide what to do with the result. `validate_for_submit`
    below is what turns "error"-severity issues (and, at submit time, unresolved
    conflicts) into a hard failure.
    """
    issues: list[Issue] = []
    seen_ids: set[str] = set()
    by_id = {p.id: p for p in batch.proposals}

    for p in batch.proposals:
        if p.id in seen_ids:
            issues.append(
                Issue(proposal_id=p.id, severity="error", message=f"duplicate proposal id {p.id!r}")
            )
        seen_ids.add(p.id)

        # Every check below this point except the resource/PK/whitelist checks
        # (which need `spec`) must still run even if the resource alias itself is
        # invalid - a report-everything pass must not let one bad field hide a
        # separately-real unresolved conflict on the same proposal.
        for conflict in p.conflicts:
            if conflict.resolution is None:
                issues.append(
                    Issue(
                        proposal_id=p.id,
                        severity="unresolved_conflict",
                        message=f"conflict {conflict.id!r} ({conflict.field}) is unresolved",
                    )
                )

        # person_id: a real integer c_personid, "NEW", or a string referencing a
        # sibling proposal's id. Any string other than "NEW" that isn't parseable
        # as an integer is necessarily an attempted sibling reference (there is no
        # other valid meaning for a non-numeric, non-"NEW" string here), so treat
        # it as such and report a dangling reference if it doesn't resolve -
        # rather than reporting the vaguer "not NEW/int/sibling" message that would
        # obscure which of the three the author actually intended.
        if p.person_id == p.id:
            issues.append(
                Issue(
                    proposal_id=p.id,
                    severity="error",
                    message="person_id refers to its own proposal id (self-reference)",
                )
            )
        elif isinstance(p.person_id, str) and p.person_id != "NEW":
            is_numeric_string = p.person_id.lstrip("-").isdigit()
            if not is_numeric_string:
                sibling = by_id.get(p.person_id)
                if sibling is None:
                    issues.append(
                        Issue(
                            proposal_id=p.id,
                            severity="error",
                            message=f"person_id references unknown sibling id {p.person_id!r}",
                        )
                    )
                elif not (
                    sibling.resource in ("basicinformation", "biogmain", "biog_main")
                    and sibling.operation == "create"
                ):
                    issues.append(
                        Issue(
                            proposal_id=p.id,
                            severity="error",
                            message=(
                                f"person_id references sibling {p.person_id!r}, but "
                                "that proposal is not a basicinformation create"
                            ),
                        )
                    )

        try:
            spec = find_spec_by_alias(p.resource)
        except FieldWhitelistError as exc:
            issues.append(Issue(proposal_id=p.id, severity="error", message=str(exc)))
            continue  # remaining checks below all require `spec`

        try:
            spec.resolve_alias(p.resource, p.operation)
        except FieldWhitelistError as exc:
            issues.append(Issue(proposal_id=p.id, severity="error", message=str(exc)))

        # target_pk: structural completeness against pk_fields minus c_personid.
        non_personid_pk = tuple(f for f in spec.pk_fields if f != STAGING_PERSONID_FIELD)
        supplied = set((p.target_pk or {}).keys())
        if p.operation in ("update", "delete"):
            required = (
                set(non_personid_pk)
                - spec.server_assigned_pk_fields
                - spec.optional_pk_fields
            )
            missing = required - supplied
            if missing:
                issues.append(
                    Issue(
                        proposal_id=p.id,
                        severity="error",
                        message=f"target_pk missing required field(s) {sorted(missing)} for {p.operation}",
                    )
                )
            server_assigned_present = supplied & spec.server_assigned_pk_fields
            if not (server_assigned_present == (spec.server_assigned_pk_fields & set(non_personid_pk))):
                missing_sa = (spec.server_assigned_pk_fields & set(non_personid_pk)) - supplied
                if missing_sa:
                    issues.append(
                        Issue(
                            proposal_id=p.id,
                            severity="error",
                            message=(
                                f"target_pk missing server-assigned field(s) "
                                f"{sorted(missing_sa)} required for {p.operation} - "
                                "must come from an earlier create's response or a "
                                "pre-existing known value, never invented"
                            ),
                        )
                    )
        elif p.operation == "create":
            bad = supplied & spec.server_assigned_pk_fields
            if bad:
                issues.append(
                    Issue(
                        proposal_id=p.id,
                        severity="error",
                        message=(
                            f"target_pk must not include server-assigned field(s) "
                            f"{sorted(bad)} on create"
                        ),
                    )
                )
        unknown_pk = supplied - set(non_personid_pk)
        if unknown_pk:
            issues.append(
                Issue(
                    proposal_id=p.id,
                    severity="error",
                    message=f"target_pk has field(s) not in this resource's PK: {sorted(unknown_pk)}",
                )
            )

        # Field whitelist (including pseudo-fields) for changes.
        try:
            spec.validate_changes(p.operation if p.operation != "delete" else "update", p.changes)
        except FieldWhitelistError as exc:
            if p.operation != "delete":  # delete has no changes whitelist
                issues.append(Issue(proposal_id=p.id, severity="error", message=str(exc)))
        if p.operation == "delete" and p.changes:
            issues.append(
                Issue(
                    proposal_id=p.id,
                    severity="error",
                    message="delete proposals must not carry changes",
                )
            )

    issues.extend(_find_person_reference_cycles(batch))
    return issues


def _sibling_dependency(proposal: Proposal, by_id: dict[str, Proposal]) -> str | None:
    """Return the sibling proposal id `proposal.person_id` depends on, or None if
    `person_id` is "NEW", a real (numeric) c_personid, or not a recognized sibling.

    Shared by find_issues()'s cycle check and topological_submission_order(), so
    the two agree on what counts as a dependency edge - a numeric-looking string
    is always a literal c_personid, never a sibling reference, in both places.
    """
    if not isinstance(proposal.person_id, str) or proposal.person_id == "NEW":
        return None
    if proposal.person_id.lstrip("-").isdigit():
        return None
    return proposal.person_id if proposal.person_id in by_id else None


def _find_person_reference_cycles(batch: StagingBatch) -> list[Issue]:
    by_id = {p.id: p for p in batch.proposals}
    issues: list[Issue] = []
    state: dict[str, str] = {}  # id -> "visiting" | "done"

    def visit(pid: str, path: list[str]) -> None:
        if state.get(pid) == "done":
            return
        if pid in path:
            cycle = " -> ".join(path[path.index(pid):] + [pid])
            issues.append(
                Issue(
                    proposal_id=pid,
                    severity="error",
                    message=f"person_id reference cycle: {cycle}",
                )
            )
            return
        state[pid] = "visiting"
        dep = _sibling_dependency(by_id[pid], by_id) if pid in by_id else None
        if dep is not None:
            visit(dep, path + [pid])
        state[pid] = "done"

    for p in batch.proposals:
        visit(p.id, [])

    return issues


def validate_for_submit(batch: StagingBatch) -> None:
    """Raise StagingError if the batch is not safe to submit.

    Unlike find_issues() (report-only), this treats BOTH "error"-severity issues
    AND any unresolved conflict as a hard failure, per docs/03 section 2.5.
    """
    issues = find_issues(batch)
    if issues:
        lines = [f"  - [{i.proposal_id}] {i.severity}: {i.message}" for i in issues]
        error = StagingError(
            f"Batch {batch.batch_id!r} is not safe to submit ({len(issues)} issue(s)):\n"
            + "\n".join(lines)
        )
        error.issues = issues
        raise error


def submittable_proposals(batch: StagingBatch) -> list[Proposal]:
    """Proposals to actually submit: excludes any proposal with a conflict
    resolved as "defer" (docs/03 section 2.2: "'defer' (skip this one field/row
    for now, submit the rest of the batch)") - AND, transitively, any proposal
    that (directly or indirectly) depends on a deferred proposal's person_id.

    Without the transitive step, deferring a `basicinformation` create while a
    sub-resource proposal still references it as a sibling would either silently
    try to submit an orphaned sub-resource, or make topological_submission_order()
    raise a confusing "dependency cycle or unresolved sibling reference" error for
    a batch that validate_for_submit() already accepted - a deferred create's
    dependents can never be submitted this round, so cascading the exclusion is
    the only consistent behavior.

    Call validate_for_submit(batch) first - this function doesn't itself check
    for unresolved conflicts or other structural errors.
    """
    by_id = {p.id: p for p in batch.proposals}
    excluded = {p.id for p in batch.proposals if any(c.resolution == "defer" for c in p.conflicts)}

    changed = True
    while changed:
        changed = False
        for p in batch.proposals:
            if p.id in excluded:
                continue
            dep = _sibling_dependency(p, by_id)
            if dep is not None and dep in excluded:
                excluded.add(p.id)
                changed = True

    return [p for p in batch.proposals if p.id not in excluded]


def topological_submission_order(
    batch: StagingBatch, *, proposals: list[Proposal] | None = None
) -> list[Proposal]:
    """Order proposals so a create is never submitted before the sibling person
    create it depends on (AGENTS.md rule 7 / docs/01 milestone 4).

    `proposals` defaults to submittable_proposals(batch) (i.e. excludes any
    "defer"-resolved rows); pass batch.proposals explicitly if you need the full
    unfiltered order for some other purpose.

    Uses the same _sibling_dependency() logic as find_issues()'s cycle check, so
    the two agree on what counts as a dependency edge (a numeric-looking string is
    always a literal c_personid, never a sibling reference, in both places).

    Does not otherwise validate - call find_issues()/validate_for_submit() first.
    Raises StagingError on a dependency cycle (which shouldn't be reachable if
    find_issues() already confirmed every sibling reference points at a
    basicinformation create, but checked defensively here too).
    """
    by_id = {p.id: p for p in batch.proposals}
    resolved: list[Proposal] = []
    resolved_ids: set[str] = set()
    remaining = list(proposals if proposals is not None else submittable_proposals(batch))

    while remaining:
        progressed = False
        next_remaining = []
        for p in remaining:
            depends_on = _sibling_dependency(p, by_id)
            if depends_on is None or depends_on in resolved_ids:
                resolved.append(p)
                resolved_ids.add(p.id)
                progressed = True
            else:
                next_remaining.append(p)
        if not progressed:
            raise StagingError(
                "Dependency cycle or unresolved sibling reference among proposals: "
                f"{[p.id for p in next_remaining]}"
            )
        remaining = next_remaining

    return resolved


def resolve_target_pk(
    proposal: Proposal, *, resolved_person_id: int, spec_key: str | None = None
) -> dict[str, Any]:
    """Build the full target.pk dict (including c_personid where applicable) to
    send to mutation_api.py, given the batch's already-resolved person_id for this
    proposal."""
    # proposal.resource may be any valid alias (e.g. "socialinst"), not
    # necessarily this module's canonical RESOURCE_SPECS key - look it up by
    # alias unless the caller already knows and passed the canonical key.
    spec = get_resource_spec(spec_key) if spec_key else find_spec_by_alias(proposal.resource)
    full = dict(proposal.target_pk or {})
    if STAGING_PERSONID_FIELD in spec.pk_fields:
        full[STAGING_PERSONID_FIELD] = resolved_person_id
    return full
