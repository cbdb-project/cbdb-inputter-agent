"""Typed wrappers for cbdb-online-main-server's /api/v2/* Mutation API.

Builds the JSON envelope from docs/00-target-system-brief.md section 3, validates
client-side against docs/04-field-whitelists.md (via models.py) before ever sending
a request, and always sets mode="direct" (AGENTS.md rule 1 - this repo never uses
proposal mode).

Design note on target_pk vs changes for `create` (see docs/03-extraction-review-
workflow.md section 2.5 for the same question in the staging-file context): the
target system's real request envelope includes `target.pk` for every operation,
including create, and each resource's create field whitelist (docs/04) includes the
composite PK columns as normal, settable fields. `create()` supplies the PK fields in
BOTH target.pk and changes (when they're part of that resource's create whitelist),
so whichever the server actually reads, the value is present and consistent. **This
was confirmed correct against a live response during Milestone 7** (a real
basicinformation create and a real addresses sub-resource create both succeeded
end-to-end against the user's local instance - see docs/00-target-system-brief.md's
"Confirmed live" section).
"""

from __future__ import annotations

from typing import Any

from .http_client import HttpClient
from .models import FieldWhitelistError, get_resource_spec
from .preflight import (
    assert_addr_create_is_not_a_duplicate,
    assert_office_create_is_not_a_duplicate,
)


def _build_envelope(
    *,
    resource_string: str,
    mode: str,
    operation: str,
    person_id: int | str,
    target_pk: dict[str, Any],
    changes: dict[str, Any],
    comment: str | None = None,
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "resource": resource_string,
        "mode": mode,
        "operation": operation,
        "person_id": person_id,
        "target": {"pk": target_pk},
        "changes": changes,
    }
    if comment is not None:
        envelope["meta"] = {"comment": comment}
    return envelope


# NOTE: `approved_by` and its gate were removed on 2026-09-14. They asked the person
# holding the token to countersign a row they were about to write with that same
# token, and the server stamps `user_id` on every operations row regardless - so the
# signature recorded nothing the operations log did not already have, in a field that
# only this client could read. See AGENTS.md rule 12 for what survives: an agent does
# not invent global reference data to unblock itself, which is a judgement rule about
# when to stop and ask, not a field to fill in.


class MutationApi:
    """Generic create/update/delete/get methods for any resource in models.RESOURCE_SPECS.

    Named convenience wrappers for the highest-value resources (person/address/
    kinship, per docs/01-implementation-plan.md milestone 3 scope) are defined below
    this class; they all delegate to these generic methods, so any resource can be
    used through the generic API even before a named wrapper exists for it.
    """

    def __init__(self, client: HttpClient) -> None:
        self._client = client

    @property
    def client(self) -> HttpClient:
        """The underlying HttpClient, for callers (e.g. batch_runner.py's
        allocate_person_id) that need to use person_id.py's functions directly."""
        return self._client

    def create(
        self,
        resource_key: str,
        *,
        person_id: int | str,
        target_pk: dict[str, Any],
        changes: dict[str, Any],
        resource_string: str | None = None,
        comment: str | None = None,
    ) -> dict[str, Any]:
        spec = get_resource_spec(resource_key)
        alias = resource_string or spec.key
        spec.resolve_alias(alias, "create")
        spec.validate_target_pk_for_create(target_pk)

        merged_changes = dict(changes)
        for pk_field, pk_value in target_pk.items():
            if pk_field not in spec.create_fields:
                continue
            if pk_field in merged_changes and merged_changes[pk_field] != pk_value:
                raise FieldWhitelistError(
                    f"{spec.key}: target_pk[{pk_field!r}]={pk_value!r} conflicts "
                    f"with changes[{pk_field!r}]={merged_changes[pk_field]!r} - "
                    "these must agree, since both are sent to the server"
                )
            merged_changes[pk_field] = pk_value

        spec.validate_changes("create", merged_changes)

        # Pre-create duplicate checks, for the two resources where the server has no
        # guard of its own: `office`, whose create allocates max+1 and inserts with
        # no name lookup, and `addr_codes`, which has no unique key on `c_name_chn`
        # and no delete path.
        #
        # They live HERE, at the layer that actually sends the request, and not only
        # where a batch is generated. Two reasons. A guard that exists only in
        # batch_runner is one a direct `MutationApi.create("office", ...)` walks
        # straight past. And the gap between generating a batch and submitting it is
        # the review, which is meant to take time - anything anyone else enters in
        # that window would otherwise land as a permanent duplicate. Raises
        # PreflightError (a CbdbApiError), which batch_runner isolates per proposal.
        #
        # Deliberately AFTER validate_changes(): a payload we can reject offline should
        # be rejected offline, both so the caller sees the real error (a bad field name
        # rather than "cannot check for duplicates of an empty office name") and so a
        # malformed create does not spend up to _PAGE_CAP rate-limited requests first.
        #
        # Skipped under dry-run for the same reason batch_runner skips person-id
        # allocation there: a dry run's job is to preview a batch without touching the
        # target system, and an unreachable host should not turn a previewed create
        # into a failed proposal. The real create cannot skip it.
        #
        # `ADMIN_CAT_CODES` cannot be checked here - it has no read endpoint at all
        # (API.md 13.2), so the only answer available is the snapshot-plus-operations
        # composition `src/cbdb_agent/places_and_offices/live_state.py` makes at generation time, and
        # replaying that per create would be minutes of rate-limited requests inside
        # the write loop. Two category rows are also a far smaller surface than 55
        # place names.
        if not self._client.dry_run:
            if spec.key == "office":
                assert_office_create_is_not_a_duplicate(
                    self._client,
                    name=merged_changes.get("name"),
                    dynasty_code=merged_changes.get("dynasty_code"),
                )
            elif spec.key == "addr_codes":
                # The years go with the name: this table holds one row per place
                # per period, so the name alone cannot say whether a live row is
                # the same place at the same time.
                assert_addr_create_is_not_a_duplicate(
                    self._client,
                    name=merged_changes.get("c_name_chn"),
                    first_year=merged_changes.get("c_firstyear"),
                    last_year=merged_changes.get("c_lastyear"),
                )

        envelope = _build_envelope(
            resource_string=alias,
            mode="direct",
            operation="create",
            person_id=person_id,
            target_pk=target_pk,
            changes=merged_changes,
            comment=comment,
        )
        return self._client.post(
            "/api/v2/create",
            json_body=envelope,
            mutating=True,
            resource=spec.key,
            operation="create",
            mode="direct",
        )

    def update(
        self,
        resource_key: str,
        *,
        person_id: int | str,
        target_pk: dict[str, Any],
        changes: dict[str, Any],
        resource_string: str | None = None,
        comment: str | None = None,
    ) -> dict[str, Any]:
        spec = get_resource_spec(resource_key)
        alias = resource_string or spec.key
        spec.resolve_alias(alias, "update")
        spec.validate_target_pk_for_update_or_delete(target_pk)
        spec.validate_changes("update", changes)

        envelope = _build_envelope(
            resource_string=alias,
            mode="direct",
            operation="update",
            person_id=person_id,
            target_pk=target_pk,
            changes=changes,
            comment=comment,
        )
        return self._client.post(
            "/api/v2/mutate",
            json_body=envelope,
            mutating=True,
            resource=spec.key,
            operation="update",
            mode="direct",
        )

    def delete(
        self,
        resource_key: str,
        *,
        person_id: int | str,
        target_pk: dict[str, Any],
        resource_string: str | None = None,
        comment: str | None = None,
    ) -> dict[str, Any]:
        spec = get_resource_spec(resource_key)
        alias = resource_string or spec.key
        spec.resolve_alias(alias, "delete")
        spec.validate_target_pk_for_update_or_delete(target_pk)

        envelope = _build_envelope(
            resource_string=alias,
            mode="direct",
            operation="delete",
            person_id=person_id,
            target_pk=target_pk,
            changes={},
            comment=comment,
        )
        return self._client.post(
            "/api/v2/delete",
            json_body=envelope,
            mutating=True,
            resource=spec.key,
            operation="delete",
            mode="direct",
        )

    def get(
        self,
        resource_key: str,
        *,
        person_id: int | str,
        target_pk: dict[str, Any],
        resource_string: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/v2/get requires the same envelope shape as the write endpoints
        (resource, person_id, target.pk) - confirmed live during Milestone 7
        against app/Http/Controllers/Api/MutationController::get() in the target
        repo, which 422s "缺少 target.pk" without a nested target.pk and separately
        requires person_id. Sent as a JSON body on the GET request (Laravel reads
        the JSON body first, same as for POST).

        Known caveat (also confirmed live, via
        app/Services/Mutations/MutationReadService.php): the alias list for GET is
        NOT always identical to the create/update/delete alias lists in
        docs/04-field-whitelists.md - e.g. GET accepts "socialinstitution" (no
        underscore) instead of "socialinst", and accepts "source" (singular) as an
        extra alias for `sources`. Passing a canonical `resource_key` (the
        default, when resource_string is omitted) is always safe; only override
        `resource_string` with a value you've confirmed is in the GET-specific
        alias list.
        """
        spec = get_resource_spec(resource_key)
        alias = resource_string or spec.key
        envelope = {"resource": alias, "person_id": person_id, "target": {"pk": target_pk}}
        return self._client.get("/api/v2/get", json_body=envelope, resource=spec.key)

    # -- Named convenience wrappers (docs/01-implementation-plan.md milestone 3) --

    def create_person(
        self, c_personid: int, changes: dict[str, Any], *, comment: str | None = None
    ) -> dict[str, Any]:
        return self.create(
            "basicinformation",
            person_id=c_personid,
            target_pk={"c_personid": c_personid},
            changes=changes,
            comment=comment,
        )

    def update_person(
        self, c_personid: int, changes: dict[str, Any], *, comment: str | None = None
    ) -> dict[str, Any]:
        return self.update(
            "basicinformation",
            person_id=c_personid,
            target_pk={"c_personid": c_personid},
            changes=changes,
            comment=comment,
        )

    def delete_person(self, c_personid: int, *, comment: str | None = None) -> dict[str, Any]:
        return self.delete(
            "basicinformation",
            person_id=c_personid,
            target_pk={"c_personid": c_personid},
            comment=comment,
        )

    def create_address(
        self,
        c_personid: int,
        *,
        c_addr_id: Any,
        c_addr_type: Any,
        c_sequence: Any,
        changes: dict[str, Any],
        comment: str | None = None,
    ) -> dict[str, Any]:
        target_pk = {
            "c_personid": c_personid,
            "c_addr_id": c_addr_id,
            "c_addr_type": c_addr_type,
            "c_sequence": c_sequence,
        }
        return self.create(
            "addresses",
            person_id=c_personid,
            target_pk=target_pk,
            changes=changes,
            comment=comment,
        )

    def update_address(
        self,
        c_personid: int,
        *,
        c_addr_id: Any,
        c_addr_type: Any,
        c_sequence: Any,
        changes: dict[str, Any],
        comment: str | None = None,
    ) -> dict[str, Any]:
        target_pk = {
            "c_personid": c_personid,
            "c_addr_id": c_addr_id,
            "c_addr_type": c_addr_type,
            "c_sequence": c_sequence,
        }
        return self.update(
            "addresses",
            person_id=c_personid,
            target_pk=target_pk,
            changes=changes,
            comment=comment,
        )

    def delete_address(
        self,
        c_personid: int,
        *,
        c_addr_id: Any,
        c_addr_type: Any,
        c_sequence: Any,
        comment: str | None = None,
    ) -> dict[str, Any]:
        target_pk = {
            "c_personid": c_personid,
            "c_addr_id": c_addr_id,
            "c_addr_type": c_addr_type,
            "c_sequence": c_sequence,
        }
        return self.delete(
            "addresses", person_id=c_personid, target_pk=target_pk, comment=comment
        )

    def create_kinship(
        self,
        c_personid: int,
        *,
        c_kin_id: int,
        c_kin_code: Any,
        changes: dict[str, Any],
        comment: str | None = None,
    ) -> dict[str, Any]:
        target_pk = {
            "c_personid": c_personid,
            "c_kin_id": c_kin_id,
            "c_kin_code": c_kin_code,
        }
        return self.create(
            "kinship",
            person_id=c_personid,
            target_pk=target_pk,
            changes=changes,
            comment=comment,
        )

    def update_kinship(
        self,
        c_personid: int,
        *,
        c_kin_id: int,
        c_kin_code: Any,
        changes: dict[str, Any],
        comment: str | None = None,
    ) -> dict[str, Any]:
        target_pk = {
            "c_personid": c_personid,
            "c_kin_id": c_kin_id,
            "c_kin_code": c_kin_code,
        }
        return self.update(
            "kinship",
            person_id=c_personid,
            target_pk=target_pk,
            changes=changes,
            comment=comment,
        )

    def delete_kinship(
        self,
        c_personid: int,
        *,
        c_kin_id: int,
        c_kin_code: Any,
        comment: str | None = None,
    ) -> dict[str, Any]:
        target_pk = {
            "c_personid": c_personid,
            "c_kin_id": c_kin_id,
            "c_kin_code": c_kin_code,
        }
        return self.delete(
            "kinship", person_id=c_personid, target_pk=target_pk, comment=comment
        )
