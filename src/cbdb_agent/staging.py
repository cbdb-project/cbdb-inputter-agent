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

import json
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .models import (
    FieldWhitelistError,
    find_spec_by_alias,
    get_resource_spec,
    is_missing_value,
)

STAGING_PERSONID_FIELD = "c_personid"


class StagingError(ValueError):
    """Base class for staging-file structural/validation errors.

    `issues` (a list[Issue], set by validate_for_submit()) carries the structured
    findings when available, so a caller (e.g. a future cli.py) can act on them
    programmatically instead of re-parsing the message string.
    """

    issues: list["Issue"] | None = None


class ConflictOption(BaseModel):
    # `Any`, not docs/03 section 2.5's literal str|int|float. A conflict can be about
    # ANY field a proposal can set, and Proposal.changes was already loosened to
    # dict[str, Any] for exactly this reason: the address pseudo-fields are LISTS
    # (postings' c_addr, events'/possessions' c_addr_id). A conflict over which
    # addresses a posting should carry - e.g. whether 慶紹所 means [慶元路, 紹興路] -
    # is not expressible with a scalar-only option value, and the file fails to load
    # with a pydantic type error rather than anything an editor can act on.
    value: Any
    rationale: str


class Conflict(BaseModel):
    id: str
    field: str
    description: str
    options: list[ConflictOption] = Field(default_factory=list)
    # Same widening as ConflictOption.value, and for the same reason. `resolution`
    # stays None-by-default and `None` still means "unresolved" - that is what
    # find_issues() checks and what blocks submission; widening the non-None type
    # does not weaken the blocker.
    agent_suggestion: Any = None
    agent_reasoning: str | None = None
    resolution: Any = None  # None = unresolved, blocks submit


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
    # AGENTS.md rule 12 gate. Only consulted for resources whose ResourceSpec sets
    # requires_explicit_approval (today: `text-codes`) - global reference data with
    # no server-side delete path. A named human must own the decision, in writing,
    # in the file; the agent must never fill this in on its own initiative. It is
    # also forwarded into the request's meta.comment so the approval is visible in
    # the server's own `operations` row, not just in this repo.
    approved_by: str | None = None

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


class ProposalCurrentState(BaseModel):
    """Best-effort, live-fetched current server state for one proposal's target
    row - produced by batch_runner.fetch_current_values() (docs/06-staging-preview-
    design.md's Tier 2). `row` is the fetched row's fields if the fetch succeeded;
    `error` explains why nothing was fetched (network/auth failure, row not found,
    a `create` proposal with nothing to diff against yet, or person_id not yet
    resolved in this batch). Never both set - exactly one is non-None."""

    row: dict[str, Any] | None = None
    error: str | None = None

    @model_validator(mode="after")
    def _exactly_one_of_row_or_error(self) -> "ProposalCurrentState":
        if (self.row is None) == (self.error is None):
            raise ValueError(
                "ProposalCurrentState requires exactly one of row/error to be set "
                f"(got row={self.row!r}, error={self.error!r}) - a proposal with no "
                "entry in the current_values dict at all is how "
                "render_preview_markdown() represents 'not fetched, offline "
                "preview', not an instance with both fields None"
            )
        return self


def load_staging_file(path: str) -> StagingBatch:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return StagingBatch.model_validate(raw)


def load_input_batch(path: str, *, batch_id: str | None = None) -> StagingBatch:
    """Load an already-structured JSON batch (docs/01-implementation-plan.md
    section 7's `cli.py submit --input` path) as a StagingBatch, so both input
    paths share one validation/submission engine (batch_runner.py) instead of
    duplicating it.

    Input file shape: a JSON array of records, each with the same fields as a
    staging Proposal MINUS source_quote/confidence/conflicts (there's no
    extraction step here - the data is already structured, so there's nothing to
    cite a source for or flag a confidence level on). Those three fields are
    filled with fixed placeholders (`source_quote="(structured input, no
    extraction)"`, `confidence="high"`, `conflicts=[]`) so the resulting
    StagingBatch validates and runs through find_issues()/run_batch() unchanged.
    """
    with open(path, encoding="utf-8") as f:
        records = json.load(f)
    if not isinstance(records, list):
        raise StagingError(f"{path}: expected a JSON array of records, got {type(records).__name__}")

    proposals = []
    for i, record in enumerate(records):
        if not isinstance(record, dict):
            raise StagingError(f"{path}: record #{i} is not a JSON object")
        record_id = record.get("id", f"record-{i}")
        missing = [f for f in ("resource", "operation", "person_id") if f not in record]
        if missing:
            raise StagingError(
                f"{path}: record {record_id!r} is missing required field(s) {missing}"
            )
        proposals.append(
            Proposal(
                id=record_id,
                resource=record["resource"],
                operation=record["operation"],
                person_id=record["person_id"],
                target_pk=record.get("target_pk"),
                changes=record.get("changes", {}),
                source_quote="(structured input, no extraction)",
                confidence="high",
                conflicts=[],
                # Forward it rather than dropping it: without this, a JSON record
                # that DOES carry an approval fails validation with "it needs an
                # explicit approved_by" - an error that contradicts the input.
                approved_by=record.get("approved_by"),
            )
        )
    return StagingBatch(batch_id=batch_id or path, proposals=proposals)


def save_staging_file(batch: StagingBatch, path: str) -> None:
    # Known cosmetic limitation: plain yaml.safe_dump() does not reproduce the
    # multi-line `|` block-scalar style shown in docs/03 section 2.2's worked
    # example for long prose fields (source_quote, description, etc.) - it comes
    # back as a folded/quoted scalar instead. Round-trip data fidelity is still
    # exact (verified in tests/test_staging.py); this only affects how pleasant
    # the regenerated file is to read by hand. Worth a custom YAML representer if
    # this becomes a real friction point during Milestone 6/7 usage.
    data = batch.model_dump(exclude_none=False)
    # exclude_none=False is deliberate overall - `resolution: null` MUST stay visible,
    # since it is the submission blocker a human is meant to see and fill in. But
    # `approved_by: null` is different: it applies to almost no proposal, and leaving
    # it on every row is both noise and an invitation for an agent to fill it in,
    # which rule 12 forbids. Drop it only where it is unset.
    for proposal in data.get("proposals", []):
        if proposal.get("approved_by") is None:
            proposal.pop("approved_by", None)
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
        elif p.person_id == "NEW" and not (
            p.resource in ("basicinformation", "biogmain", "biog_main") and p.operation == "create"
        ):
            issues.append(
                Issue(
                    proposal_id=p.id,
                    severity="error",
                    message=(
                        "person_id 'NEW' is only valid on a basicinformation create - "
                        f"this proposal is resource={p.resource!r} operation={p.operation!r}"
                    ),
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

        # AGENTS.md rule 12: global reference data (code tables, entity aggregates)
        # is a different risk class from one person's record - it is referenced by
        # potentially tens of thousands of rows and the server offers NO delete path,
        # so a wrong row is permanent. Require a named human to have signed off, in
        # the file. This is an `error`, not a `conflict`: an unresolved conflict is a
        # normal mid-review state that `validate` reports and exits 0 on, whereas a
        # missing approval must make the batch structurally invalid.
        if spec.requires_explicit_approval and not (p.approved_by or "").strip():
            issues.append(
                Issue(
                    proposal_id=p.id,
                    severity="error",
                    message=(
                        f"resource {p.resource!r} is global reference data, not one "
                        "person's record (AGENTS.md rule 12) - it needs an explicit "
                        "`approved_by: <name of the human who decided>` on this "
                        "proposal before it can be submitted. Never fill that in on "
                        "the agent's own initiative."
                    ),
                )
            )
        elif spec.requires_explicit_approval:
            # Length is a submit-time rule in mutation_api; front-run it here, or a
            # batch signed with a pasted paragraph validates clean and then fails
            # per-proposal mid-run - the same class of gap as the key check above.
            from .mutation_api import MAX_APPROVED_BY_LEN

            signature = str(p.approved_by)
            if len(signature) > MAX_APPROVED_BY_LEN:
                issues.append(
                    Issue(
                        proposal_id=p.id,
                        severity="error",
                        message=(
                            f"approved_by is {len(signature)} characters; the "
                            f"server accepts at most {MAX_APPROVED_BY_LEN}. It "
                            f"records who decided, not why - the reasoning belongs "
                            f"in the batch's `source_excerpt` or the proposal's "
                            f"`source_quote`"
                        ),
                    )
                )

        # Some creates are meaningless without specific content - and for a resource
        # with no delete path (the code tables; API.md 13.3) permanently so - even
        # though the server accepts an empty `changes` on create (API.md 4.3).
        # Front-run that here so it surfaces in `validate`, not only at submit time
        # in mutation_api.
        if p.operation == "create" and spec.required_create_fields:
            blank = sorted(
                f for f in spec.required_create_fields
                if is_missing_value(p.changes.get(f))
            )
            if blank:
                issues.append(
                    Issue(
                        proposal_id=p.id,
                        severity="error",
                        message=(
                            f"{spec.key} create needs a non-empty {blank} - the server "
                            "would accept the row without it, and this is global "
                            "reference data, so a blank row is visible to every other "
                            "user. Whether it can be removed afterwards depends on the "
                            "resource: the code tables have no delete path at all "
                            "(API.md 13.3), the entity aggregates do, but only while "
                            "nothing references the row yet (API.md 13.4)"
                        ),
                    )
                )

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
            # A composite key with no server-assigned part must be complete HERE.
            # Left to mutation_api's own check it surfaces mid-run, after earlier
            # rows of the batch have committed - and for ADDR_BELONGS_DATA those
            # are rows with no delete path.
            required_pk = {
                f for f in spec.pk_fields
                if f != STAGING_PERSONID_FIELD
                and f not in spec.server_assigned_pk_fields
                and f not in spec.optional_pk_fields
            }
            # ONLY for a resource the server assigns nothing for. That is the
            # case this exists for: `ADDR_BELONGS_DATA`'s four-column key has no
            # "next id", so every column must arrive complete or the create 422s
            # mid-batch - after earlier, undeletable rows have committed.
            #
            # It must NOT fire where the server does assign a key. `postings` has
            # `c_office_id` in its PK tuple and a server-assigned `c_posting_id`
            # alongside; a posting staged with an unresolved office code is a
            # normal mid-review state that the reviewer settles or defers, not a
            # structural error - and making it one left every such batch
            # unsubmittable even after deferring the row.
            #
            # Value, not just key: a key column present but null or "" is a 422
            # server-side (completeness is checked before normalization, digest
            # 1.5) and a key-set difference could not see it. `{"ref": ...}`
            # counts as supplied - it resolves to a real id before the request is
            # built. `0` does not count as missing: it is a real year and a real
            # code.
            given = {**(p.changes or {}), **(p.target_pk or {})}
            missing_pk = sorted(
                f for f in required_pk
                if f not in given
                or (not is_pk_ref(given[f]) and is_missing_value(given[f]))
            ) if not spec.server_assigned_pk_fields else []
            if missing_pk:
                issues.append(
                    Issue(
                        proposal_id=p.id,
                        severity="error",
                        message=(
                            f"create on {p.resource!r} is missing primary-key "
                            f"field(s) {missing_pk} - the server assigns none of "
                            f"them, so the whole key must be given here"
                        ),
                    )
                )
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

    issues.extend(_pk_ref_issues(batch, by_id))
    issues.extend(_find_person_reference_cycles(batch))
    return issues


# --- cross-proposal primary-key references ----------------------------------
#
# A value of the form `{"ref": "<proposal id>"}` means "the primary key the server
# assigned to that sibling create". It exists for one shape that person data never
# had: a child row whose own COMPOSITE key is built out of server-assigned parent
# ids. `ADDR_BELONGS_DATA`'s key is (c_addr_id, c_belongs_to, c_firstyear,
# c_lastyear) and the first two are `ADDR_CODES.c_addr_id` values the server mints
# on create, so neither can be written down in advance.
#
# Why not submit the parents first and generate the children afterwards: the
# belongs-to rows are the *irreversible* half of this data. Code-table `delete` is
# 403 (API.md 13.3) and the update whitelist covers only c_source/c_pages/c_notes,
# so a wrong parent or a wrong year on an edge can never be corrected or removed.
# Generating that file only after the parents exist would mean the reviewer signs
# `approved_by` on a document that did not exist when they reviewed the data.
#
# Deliberately narrow:
#   * the target must be a `create` in the same batch;
#   * that create's resource must have EXACTLY ONE server-assigned PK field, so
#     "the key it was assigned" is unambiguous - no field name to get wrong;
#   * a reference is resolved at submit time by batch_runner, never by staging, and
#     an unresolved one is a hard error rather than a silently-null column.
PK_REF_KEY = "ref"


def is_pk_ref(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {PK_REF_KEY} \
        and isinstance(value[PK_REF_KEY], str)


def pk_ref_target(value: Any) -> str | None:
    return value[PK_REF_KEY] if is_pk_ref(value) else None


def iter_pk_refs(proposal: "Proposal"):
    """(where, field, target id) for every reference this proposal carries.

    Lists are walked too. The address pseudo-fields (`postings.c_addr`,
    `events.c_addr_id`, `possessions.c_addr_id`) are lists of address ids, and now
    that a batch can create an address they are the natural place to reference one.
    Looking only at top-level values left such a reference invisible to all three
    mechanisms at once: nothing ordered the proposal after its parent, nothing
    substituted it, and the "a dict where a scalar belongs" guard could not fire
    because a dict inside those lists is not obviously wrong. It went on the wire
    as a literal `{"ref": ...}`, which PHP casts to 1 - a plausible wrong address,
    not a 422.
    """
    for where, fieldname, value in _iter_pk_ref_slots(proposal):
        target = pk_ref_target(value)
        if target is not None:
            yield where, fieldname, target


def _iter_pk_ref_slots(proposal: "Proposal"):
    """(where, field label, value) for every slot a reference may legitimately sit
    in - top-level values and list elements."""
    for where, mapping in (("target_pk", proposal.target_pk or {}),
                           ("changes", proposal.changes or {})):
        for fieldname, value in (mapping or {}).items():
            yield from _walk_slot(where, fieldname, value)


def _walk_slot(where: str, label: str, value: Any):
    """One slot, recursing into lists. Depth rather than one level, because
    "one level" is an assumption about a shape nobody is validating."""
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _walk_slot(where, f"{label}[{index}]", item)
    else:
        yield where, label, value


def _pk_ref_issues(batch: "StagingBatch", by_id: dict) -> list["Issue"]:
    from .models import (
        FieldWhitelistError,
        find_spec_by_alias,
        pk_ref_target_resource,
    )

    issues: list[Issue] = []
    for p in batch.proposals:
        # A dict that ALMOST looks like a reference is the dangerous shape: not a
        # reference, so nothing orders this proposal after its parent and nothing
        # substitutes it; not a scalar either, so it goes on the wire as a dict
        # where a primary key belongs. `{"ref": 1}` (an int, not a proposal id),
        # `{"ref": "p1", "why": "..."}` and `{"Ref": "p1"}` are all plausible
        # hand-edits. These columns hold scalars only, so any dict that is not a
        # well-formed reference is a mistake worth naming.
        for where, fieldname, value in _iter_pk_ref_slots(p):
            if isinstance(value, dict) and not is_pk_ref(value):
                issues.append(Issue(
                    proposal_id=p.id, severity="error",
                    message=f"{where}.{fieldname} is a dict but not a valid "
                            f"reference: expected exactly "
                            f"{{'ref': '<proposal id>'}} with a string id, "
                            f"got {value!r}"))
        try:
            ref_spec = find_spec_by_alias(p.resource)
        except FieldWhitelistError:
            ref_spec = None
        for where, fieldname, target in iter_pk_refs(p):
            label = f"{where}.{fieldname}"
            # WHICH column, and WHICH kind of row. Without both, a reference in
            # `c_firstyear` had an address id substituted into it, and a reference
            # to the category create resolved into an address-id slot - each
            # writing a permanently wrong ADDR_BELONGS_DATA key.
            base_field = fieldname.split("[", 1)[0]
            expected = (pk_ref_target_resource(ref_spec.key, base_field)
                        if ref_spec else None)
            if expected is None:
                issues.append(Issue(
                    proposal_id=p.id, severity="error",
                    message=f"{label} is not a field that may carry a reference. "
                            f"A `{{'ref': ...}}` stands for a primary key another "
                            f"proposal is about to be assigned, so it belongs only "
                            f"in a foreign-key column - see models.PK_REF_TARGETS"))
                continue
            if target == p.id:
                issues.append(Issue(
                    proposal_id=p.id, severity="error",
                    message=f"{label} references its own proposal id {target!r}"))
                continue
            parent = by_id.get(target)
            if parent is not None and expected is not None:
                try:
                    parent_key = find_spec_by_alias(parent.resource).key
                except FieldWhitelistError:
                    parent_key = None
                if parent_key != expected:
                    issues.append(Issue(
                        proposal_id=p.id, severity="error",
                        message=f"{label} references {target!r}, which creates "
                                f"{parent.resource!r} - but this column holds a "
                                f"{expected!r} primary key. Substituting the wrong "
                                f"table's id here would be undetectable afterwards"))
                    continue
            if parent is None:
                issues.append(Issue(
                    proposal_id=p.id, severity="error",
                    message=f"{label} references {target!r}, which is not a "
                            f"proposal in this batch"))
                continue
            if parent.operation != "create":
                issues.append(Issue(
                    proposal_id=p.id, severity="error",
                    message=f"{label} references {target!r}, which is a "
                            f"{parent.operation}; only a create is assigned a new "
                            f"primary key"))
                continue
            try:
                pspec = find_spec_by_alias(parent.resource)
            except FieldWhitelistError:
                continue          # the parent's own alias error is reported separately
            assigned = sorted(pspec.server_assigned_pk_fields)
            if len(assigned) != 1:
                issues.append(Issue(
                    proposal_id=p.id, severity="error",
                    message=f"{label} references {target!r} ({parent.resource}), "
                            f"whose server-assigned primary key fields are "
                            f"{assigned or 'none'} - a reference needs exactly one, "
                            f"so there is no unambiguous value to substitute"))
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


def _dependencies(proposal: Proposal, by_id: dict[str, Proposal]) -> list[str]:
    """Every sibling this proposal must be submitted after.

    Two kinds, and both have to be here or the ordering is wrong in a way that
    only shows up against a live server: the person_id reference (a sub-resource
    after its person) and any `{"ref": ...}` primary-key reference (a child row
    after the parent whose assigned id it borrows).
    """
    out = []
    person = _sibling_dependency(proposal, by_id)
    if person is not None:
        out.append(person)
    for _where, _field, target in iter_pk_refs(proposal):
        if target in by_id and target != proposal.id and target not in out:
            out.append(target)
    return out


def _find_person_reference_cycles(batch: StagingBatch) -> list[Issue]:
    """Cycles over BOTH kinds of dependency, not just person_id.

    Walking person_id alone left a pure `{"ref": ...}` cycle invisible here: the
    batch validated clean, the preview said it was ready, and
    topological_submission_order raised mid-run instead - naming the child rather
    than the cycle.
    """
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
                    message=f"proposal reference cycle: {cycle}",
                )
            )
            return
        state[pid] = "visiting"
        for dep in (_dependencies(by_id[pid], by_id) if pid in by_id else []):
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
    that (directly or indirectly) depends on a deferred proposal - by person_id
    OR by a `{"ref": ...}` primary-key reference.

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
            # Both dependency kinds. Cascading only person_id left a child of a
            # deferred addr-codes create in the submittable set, which
            # topological_submission_order then refused - aborting the whole
            # batch for a batch find_issues had just called clean.
            if any(dep in excluded for dep in _dependencies(p, by_id)):
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
            pending = [d for d in _dependencies(p, by_id) if d not in resolved_ids]
            if not pending:
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


def substitute_pk_refs(mapping: dict[str, Any], assigned: dict[str, Any]) -> dict[str, Any]:
    """Replace every `{"ref": id}` with the primary key that proposal was assigned.

    Raises rather than leaving a hole: an unresolved reference would otherwise be
    sent as a literal dict and land as NULL or a type error, and for
    ADDR_BELONGS_DATA that NULL would be part of the primary key.
    """
    def resolve(key: str, value: Any) -> Any:
        target = pk_ref_target(value)
        if target is None:
            return value
        if target not in assigned:
            raise StagingError(
                f"{key}: reference to proposal {target!r} cannot be resolved - it "
                f"has not been submitted successfully in this run")
        return assigned[target]

    def walk(label: str, value: Any) -> Any:
        # The address pseudo-fields are lists of ids; a reference can sit in one.
        # Recursing rather than handling one level keeps this in step with
        # iter_pk_refs, which is what decides the submission order - the two
        # disagreeing is how an unsubstituted ref reaches the wire.
        if isinstance(value, (list, tuple)):
            return [walk(f"{label}[{i}]", item) for i, item in enumerate(value)]
        return resolve(label, value)

    return {key: walk(key, value) for key, value in (mapping or {}).items()}


def resolve_target_pk(
    proposal: Proposal, *, resolved_person_id: int, spec_key: str | None = None
) -> dict[str, Any]:
    """Build the full target.pk dict (including c_personid where applicable) to
    send to mutation_api.py, given the batch's already-resolved person_id for this
    proposal.

    On `create`, a staging Proposal is NOT required to set `target_pk` at all for
    a multi-field-PK resource (find_issues() rule 5 only requires it for
    update/delete - see docs/03-extraction-review-workflow.md section 2.2's own
    worked example, where an altnames create has no `target_pk`, just `changes`
    containing the same PK columns). But mutation_api.create()'s
    validate_target_pk_for_create() DOES require every non-server-assigned PK
    field to be present in target_pk. Reconcile the two here: for `create`, fill
    in any PK field missing from `target_pk` by reading it out of `changes` (where
    docs/04-field-whitelists.md's create whitelist for every resource already
    includes its own PK columns as ordinary settable fields).
    """
    # proposal.resource may be any valid alias (e.g. "socialinst"), not
    # necessarily this module's canonical RESOURCE_SPECS key - look it up by
    # alias unless the caller already knows and passed the canonical key.
    spec = get_resource_spec(spec_key) if spec_key else find_spec_by_alias(proposal.resource)
    full = dict(proposal.target_pk or {})
    if STAGING_PERSONID_FIELD in spec.pk_fields:
        full[STAGING_PERSONID_FIELD] = resolved_person_id
    if proposal.operation == "create":
        for pk_field in spec.pk_fields:
            if pk_field not in full and pk_field in proposal.changes:
                full[pk_field] = proposal.changes[pk_field]
    return full


def _preview_inline(text: str) -> str:
    """Make arbitrary text safe to interpolate into a single Markdown bullet line:
    collapse embedded newlines (which would otherwise break out of the bullet's
    indentation and merge into whatever follows) and neutralize backticks (which
    would otherwise leave an unbalanced inline code span for the rest of the
    line/paragraph)."""
    return " ".join(text.split("\n")).replace("`", "'")


def _preview_value(value: Any) -> str:
    """Render a field value for display. None (missing/blank in the current row,
    or a proposed value that's explicitly null) shows as `_(empty)_`, not the
    Python literal `None` - a bare `None` reads ambiguously as "we have no data"
    vs. "the value truly is null" vs. a field whose actual string content happens
    to be "None"."""
    if value is None:
        return "_(empty)_"
    return _preview_inline(repr(value))


def render_preview_markdown(
    batch: StagingBatch,
    issues: list[Issue] | None = None,
    current_values: dict[str, ProposalCurrentState] | None = None,
) -> str:
    """Render a read-only, human-friendly Markdown summary of a staging batch
    (docs/06-staging-preview-design.md's Tier 1). Pure string formatting, no
    network calls - `current_values` (Tier 2's best-effort live old-vs-new diff)
    is optional and supplied by the caller (see batch_runner.fetch_current_values);
    without it, every changed field shows only its proposed value.

    This output is generated-only - never hand-edit it. All edits happen in the
    YAML staging file itself (directly, or via chat asking the agent to update a
    specific `resolution`), per docs/03-extraction-review-workflow.md.
    """
    if issues is None:
        issues = find_issues(batch)
    current_values = current_values or {}

    known_ids = {p.id for p in batch.proposals}
    issues_by_proposal: dict[str | None, list[Issue]] = {}
    unattributed_issues: list[Issue] = []
    for issue in issues:
        if issue.proposal_id in known_ids:
            issues_by_proposal.setdefault(issue.proposal_id, []).append(issue)
        else:
            unattributed_issues.append(issue)

    error_count = sum(1 for i in issues if i.severity == "error")
    unresolved_count = sum(1 for i in issues if i.severity == "unresolved_conflict")
    ready = error_count == 0 and unresolved_count == 0

    lines: list[str] = [f"# Staging batch: {batch.batch_id}", ""]

    status_bits = f"{len(batch.proposals)} proposal(s)"
    if error_count:
        status_bits += f", {error_count} error(s)"
    if unresolved_count:
        status_bits += f", {unresolved_count} unresolved conflict(s)"
    status_word = "ready to submit" if ready else "NOT ready to submit"
    lines.append(f"**Status:** {status_bits} — {status_word}")
    lines.append("")
    lines.append(
        "_Generated preview - do not edit. Edit `proposal.yaml` instead; this file "
        "is refreshed by `validate --staging`._"
    )
    lines.append("")

    if batch.source_excerpt:
        lines.append("## Source")
        lines.append("")
        for excerpt_line in batch.source_excerpt.splitlines() or [""]:
            lines.append(f"> {excerpt_line}")
        lines.append("")

    lines.append("## Proposals")
    lines.append("")

    for idx, proposal in enumerate(batch.proposals, start=1):
        lines.append(
            f"### {idx}. `{proposal.id}` — {proposal.resource} / {proposal.operation} "
            f"(confidence: {proposal.confidence})"
        )
        lines.append("")

        meta_bits = [f"person_id: {proposal.person_id}"]
        if proposal.target_pk:
            pk_str = ", ".join(f"{k}={v}" for k, v in proposal.target_pk.items())
            meta_bits.append(f"target_pk: {pk_str}")
        lines.append("- " + " · ".join(meta_bits))
        # Surface the rule-12 signature in the artifact humans actually review. A
        # sign-off recorded only in raw YAML is not much of a sign-off - and the
        # MISSING case matters even more, since it is what blocks the batch.
        try:
            needs_approval = find_spec_by_alias(proposal.resource).requires_explicit_approval
        except FieldWhitelistError:
            needs_approval = False  # unknown resource is already an `error` issue
        if needs_approval or proposal.approved_by:
            signature = (proposal.approved_by or "").strip()
            if signature:
                lines.append(
                    f"- 🔓 **approved_by: {_preview_inline(signature)}** "
                    "(global reference data — AGENTS.md rule 12)"
                )
            else:
                lines.append(
                    "- 🔒 **approved_by: _(not set)_** — global reference data; this "
                    "proposal cannot be submitted until a named human signs off "
                    "(AGENTS.md rule 12)"
                )

        state = current_values.get(proposal.id)
        for field, proposed_value in proposal.changes.items():
            lines.append(f"- **{field}**")
            if proposal.operation == "create":
                lines.append(f"  - proposed: {_preview_value(proposed_value)}")
            elif state is None:
                lines.append("  - current:  _(not fetched — offline preview)_")
                lines.append(f"  - proposed: {_preview_value(proposed_value)}")
            elif state.error is not None:
                lines.append(f"  - current:  ⚠️ could not fetch ({_preview_inline(state.error)})")
                lines.append(f"  - proposed: {_preview_value(proposed_value)}")
            else:
                current_value = (state.row or {}).get(field)
                lines.append(f"  - current:  {_preview_value(current_value)}")
                lines.append(f"  - proposed: {_preview_value(proposed_value)}")

        if proposal.source_quote:
            lines.append(f'- source_quote: "{_preview_inline(proposal.source_quote)}"')

        for issue in issues_by_proposal.get(proposal.id, []):
            if issue.severity == "error":
                lines.append(f"- 🛑 **error**: {_preview_inline(issue.message)}")

        for conflict in proposal.conflicts:
            resolved = conflict.resolution is not None
            marker = "✅" if resolved else "⚠️"
            status = (
                f"resolved as `{_preview_inline(str(conflict.resolution))}`"
                if resolved
                else "UNRESOLVED"
            )
            lines.append(f"- {marker} **{conflict.id}** ({conflict.field}) — {status}")
            lines.append(f"  - {_preview_inline(conflict.description)}")
            if conflict.options:
                opts = " · ".join(
                    f"`{_preview_inline(str(o.value))}` ({_preview_inline(o.rationale)})"
                    for o in conflict.options
                )
                lines.append(f"  - options: {opts}")
            if conflict.agent_suggestion is not None:
                reasoning = (
                    f" — {_preview_inline(conflict.agent_reasoning)}"
                    if conflict.agent_reasoning
                    else ""
                )
                lines.append(
                    f"  - agent suggests: `{_preview_inline(str(conflict.agent_suggestion))}`{reasoning}"
                )

        lines.append("")

    if unattributed_issues:
        lines.append("## Unattributed issues")
        lines.append("")
        lines.append(
            "_These issues reference a proposal id that isn't in this batch (or no "
            "id at all) - shown here so nothing counted in the status line above "
            "goes unexplained._"
        )
        lines.append("")
        for issue in unattributed_issues:
            marker = "🛑" if issue.severity == "error" else "⚠️"
            lines.append(f"- {marker} [{issue.proposal_id!r}] {_preview_inline(issue.message)}")
        lines.append("")

    if batch.batch_notes:
        lines.append("## Batch notes")
        lines.append("")
        for notes_line in batch.batch_notes.splitlines() or [""]:
            lines.append(f"> {notes_line}")
        lines.append("")

    return "\n".join(lines)
