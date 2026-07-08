"""Typed wrappers for cbdb-online-main-server's /api/v2/* Mutation API.

Builds the JSON envelope from docs/00-target-system-brief.md section 3, validates
client-side against docs/04-field-whitelists.md (via models.py) before ever sending
a request, and always sets mode="direct" (AGENTS.md rule 1 - this repo never uses
proposal mode).

Design note on target_pk vs changes for `create` (see docs/03-extraction-review-
workflow.md section 2.5 for the same question in the staging-file context): the
target system's real request envelope includes `target.pk` for every operation,
including create, and each resource's create field whitelist (docs/04) includes the
composite PK columns as normal, settable fields. Since the exact interaction between
target.pk and changes on create was not confirmed against a live response as of this
module's initial implementation, `create()` conservatively supplies the PK fields in
BOTH target.pk and changes (when they're part of that resource's create whitelist),
so whichever the server actually reads, the value is present and consistent. Correct
this if Milestone 7's live testing reveals the server behaves differently.
"""

from __future__ import annotations

from typing import Any

from .http_client import HttpClient
from .models import FieldWhitelistError, get_resource_spec


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


class MutationApi:
    """Generic create/update/delete/get methods for any resource in models.RESOURCE_SPECS.

    Named convenience wrappers for the highest-value resources (person/address/
    kinship, per docs/01-implementation-plan.md milestone 3 scope) are defined below
    this class; they all delegate to these generic methods, so any resource can be
    used through the generic API even before a named wrapper exists for it.
    """

    def __init__(self, client: HttpClient) -> None:
        self._client = client

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
        target_pk: dict[str, Any],
        resource_string: str | None = None,
    ) -> dict[str, Any]:
        # No alias validation here (unlike create/update/delete) - docs/00 and
        # docs/04 don't document a per-alias whitelist for GET, so any string is
        # passed through as-is; resource_string exists only for symmetry with the
        # write methods in case a caller needs a specific alias.
        spec = get_resource_spec(resource_key)
        alias = resource_string or spec.key
        params = {"resource": alias, **target_pk}
        return self._client.get("/api/v2/get", params=params, resource=spec.key)

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
