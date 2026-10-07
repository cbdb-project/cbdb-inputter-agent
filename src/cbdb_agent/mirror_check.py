"""Read the reverse row of a kinship/association update the way the server finds it.

    python -m cbdb_agent check-mirrors --staging <proposal.yaml>

An `update` on one direction of a `kinship` or `associations` pair also writes the
other direction, in the same transaction (AGENTS.md, "Reverse-pair mirror sync").
AGENTS.md asks for both directions to be read first: blank or identical on the far
side is safe, different content needs a human. This module does that read and
records the answer in the staging file, so it is not done by hand - and not done
wrong, as it was once (docs/02, 2026-10-06): the reverse row was looked up by
`/api/v2/get` with the forward row's `c_kin_id`/`c_assoc_kin_id`, which the server's
mirror rows do not carry (it writes the forward person's id into both), got a 404,
and was reported as damaged.

**How the server finds the reverse row** (target repo, `origin/develop`,
`BiogMainRepository::syncAssocMirrorOnUpdate` / `syncKinMirrorOnUpdate`, the same
rule as `RelationshipMirrorService::locateOppositeEdges`):

* associations: `c_personid` = the other person, `c_assoc_id` = this person, the
  forward row's OLD `c_text_title` and `c_assoc_first_year`, and `c_assoc_code` in
  the forward code's `c_assoc_pair`/`c_assoc_pair2`.
* kinship: `c_personid` = the other person, `c_kin_id` = this person, `c_kin_code`
  in `kinReverseLocatorCodes(forward code)` - the codes whose pair is the forward
  code, together with the forward code's own pairs.

Here the candidates come from `GET /cbdbapi/person` of the other person (AGENTS.md
rule 1: the read for "what does this person already have"), and the one candidate's
raw row from `GET /api/v2/get` under its own key. The pair codes are reference data,
read from the SQLite snapshot - what a code means, never whether a row exists.

**The server copies the whole row.** `afterDirectUpdate` builds the mirror from the
forward row as it is AFTER the update (`$dataMirror = $newArray`), rewrites only the
key and id columns, and `update()`s the reverse row with all of it. So an update that
changes one field overwrites every column of the reverse row, and the server's 409
guard (`conflictBaselines`) only looks at the content fields that changed. A
reverse row's own `c_source` or `c_pages` would be lost without a word.

**What counts as safe**, column by column over the whole reverse row (minus audit
columns and the two people): a reverse value that is blank or a sentinel, or exactly
the value the server will write there, or - for a copied column - equal to the
forward value before the update (the pair was in step). Text compares byte for byte.
Every kinship/associations update is checked, whatever it changes; one that makes the
server CREATE a missing reverse row is reported as `backfill`, never safe.

**A verdict is as old as the read.** Run this immediately before `submit`, not only
when the batch is staged: a reverse row edited during review would otherwise be
overwritten under a stale "safe".

Anything else - no candidate, more than one, a key that does not read back, no
snapshot, another proposal in the same batch touching the same pair (the race the
server's guard cannot see) - leaves the question open for the reviewer, with what
was found. The server's own 409 on a divergent mirror remains the backstop.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable

from .http_client import (
    AuthenticationError,
    AuthorizationError,
    CbdbApiError,
    NotFoundError,
    RateLimitedError,
)
from .models import find_spec_by_alias
from .staging import (
    Conflict,
    ConflictOption,
    Proposal,
    StagingBatch,
    resolve_target_pk,
)

PAIR_RESOURCES = frozenset({"kinship", "associations"})

_AUDIT = frozenset({"c_created_by", "c_created_date", "c_modified_by", "c_modified_date"})
# Columns `afterDirectUpdate` sets itself on the mirror instead of copying (target
# repo, AssociationMutationHandler / KinshipMutationHandler). The audit columns and
# the two people carry nothing of the reverse row's own and are skipped. The rest -
# the relationship codes and, for associations, the kin ids - are compared with the
# value the server WILL write there (`_server_values`), not skipped: a reverse row
# holding a real third party in c_kin_id, or a hand-chosen alternative reverse code,
# loses it just the same. `c_autogen_notes` is copied like any column and compared
# like one: it differs between the two sides by nature, but overwriting a non-blank
# one is still a loss a reviewer has to accept.
_SKIP: dict[str, frozenset[str]] = {
    "associations": _AUDIT | {"c_personid", "c_assoc_id"},
    "kinship": _AUDIT | {"c_personid", "c_kin_id"},
}

# Prefix of the reasoning this module writes.
MARK = "mirror-check:"

SAFE, DIVERGENT, MISSING, AMBIGUOUS, SAME_BATCH, UNKNOWN, BACKFILL = (
    "safe", "divergent", "missing", "ambiguous", "same-batch", "unknown", "backfill")

# Appended to the reasoning when - and only when - the tool itself set the
# resolution. Anything a reviewer set lacks it, whatever the verdict says.
SET_BY_TOOL = " [resolved by check-mirrors]"


def is_pair_update(resource_key: str, operation: str) -> bool:
    return operation == "update" and resource_key in PAIR_RESOURCES


@dataclass
class MirrorReport:
    proposal_id: str
    status: str
    evidence: str
    reverse_pk: dict | None = None


class PairCodes:
    """Reverse-code sets, as the server computes them, from the code tables."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._kin = {r[0]: (r[1], r[2]) for r in connection.execute(
            "SELECT c_kincode, c_kin_pair1, c_kin_pair2 FROM KINSHIP_CODES")}
        self._assoc = {r[0]: (r[1], r[2]) for r in connection.execute(
            "SELECT c_assoc_code, c_assoc_pair, c_assoc_pair2 FROM ASSOC_CODES")}

    @staticmethod
    def _nonzero(values: Iterable[Any]) -> list[int]:
        return [int(v) for v in values if v is not None and int(v) != 0]

    def assoc_reverse(self, code: int) -> list[int]:
        return self._nonzero(self._assoc.get(int(code), ()))

    def kin_reverse(self, code: int) -> list[int]:
        return self._nonzero(self._kin.get(int(code), ()))

    def kin_locator(self, code: int) -> list[int]:
        """`kinReverseLocatorCodes`: codes pointing at `code`, plus its own pairs."""
        code = int(code)
        if code == 0:
            return []
        pointing = [k for k, pair in self._kin.items()
                    if code in (pair[0], pair[1])]
        return sorted(set(pointing) | set(self.kin_reverse(code)))


# --- the read ------------------------------------------------------------------


def check_batch(batch: StagingBatch, api, pairs: PairCodes | None) -> list[MirrorReport]:
    """A report for every kinship/associations update. Never raises for one
    proposal - a malformed one is `unknown`; a batch-wide error (credentials, rate
    budget) propagates, as everywhere else in the client."""
    reports = []
    for proposal in batch.proposals:
        try:
            key = find_spec_by_alias(proposal.resource).key
        except Exception:  # noqa: BLE001 - an unknown alias is reported by validate
            continue
        if not is_pair_update(key, proposal.operation):
            continue
        try:
            reports.append(_check(proposal, key, batch, api, pairs))
        except (AuthenticationError, AuthorizationError, RateLimitedError):
            raise
        except Exception as exc:  # noqa: BLE001 - one bad proposal is "cannot tell"
            reports.append(MirrorReport(proposal.id, UNKNOWN,
                                        f"could not check: {type(exc).__name__}: {exc}"))
    return reports


def _check(proposal: Proposal, key: str, batch: StagingBatch,
           api, pairs: PairCodes | None) -> MirrorReport:
    def report(status, evidence, reverse_pk=None):
        return MirrorReport(proposal.id, status, evidence, reverse_pk)

    if not isinstance(proposal.person_id, int):
        return report(UNKNOWN, "person_id is not a known c_personid yet")
    me = proposal.person_id
    pk = proposal.target_pk or {}
    other_field = "c_kin_id" if key == "kinship" else "c_assoc_id"
    other = pk.get(other_field)
    if not isinstance(other, int):
        return report(UNKNOWN, f"target_pk has no integer {other_field}")

    if pairs is None:
        return report(UNKNOWN, "no SQLite snapshot, so the reverse codes are unknown")
    racing = [p.id for p in batch.proposals if p.id != proposal.id
              and _same_relationship(p, key, _identity(proposal, key), pairs)]
    if racing:
        return report(SAME_BATCH, f"{', '.join(racing)} in this batch also writes this "
                                  f"relationship ({me}↔{other}); whichever runs last wins "
                                  "on both rows, and the server's guard cannot see it")

    try:
        forward = _row(api, key, me, resolve_target_pk(proposal, resolved_person_id=me,
                                                       spec_key=key))
        if forward is None:
            return report(UNKNOWN, "the forward row does not read back under target_pk")
        candidates = _candidates(api, key, me, other, pk, pairs)
        if not candidates:
            if _backfills(key, proposal.changes):
                return report(BACKFILL, f"{other} has no reverse row the server would "
                                        f"match, and this update makes the server CREATE "
                                        f"one under {other} - a row of a second person's")
            return report(MISSING, f"{other} has no reverse row the server would match; "
                                   "the update writes only this direction")
        if len(candidates) > 1:
            return report(AMBIGUOUS, f"{other} has {len(candidates)} rows the server "
                                     f"would match: {candidates}")
        reverse_pk = candidates[0]
        reverse = _row(api, key, other, reverse_pk)
    except (AuthenticationError, AuthorizationError, RateLimitedError):
        raise                         # batch-wide, AGENTS.md rule 10
    except CbdbApiError as exc:
        return report(UNKNOWN, f"live read failed: {exc}")
    if reverse is None:
        return report(UNKNOWN, f"the reverse row {reverse_pk} does not read back", reverse_pk)

    # What lands on the reverse: the forward row after the update (pseudo-fields
    # such as c_kinship_pair are not columns and have no reverse value to compare),
    # with the server's own values in the columns it sets.
    new = dict(forward, **proposal.changes)
    written = dict(new, **_server_values(key, me, new, reverse, proposal.changes, pairs))
    columns = sorted(set(reverse) - _SKIP[key])
    differing = []
    for col in columns:
        r, o, n = reverse.get(col), forward.get(col), written.get(col)
        if _blank(r) or _same(r, n):
            continue
        # In step with the forward row - but only for a copied column; a column the
        # server sets is judged against what it sets.
        if col not in _server_set(key) and _same(r, o):
            continue
        differing.append(f"{col}: reverse {r!r} would become {n!r}")
    if differing:
        return report(DIVERGENT, "the whole row is copied onto the reverse, which has "
                                 "content of its own: " + "; ".join(differing), reverse_pk)
    return report(SAFE, f"reverse row {_pk_text(reverse_pk)} read live: every one of its "
                        f"{len(columns)} columns the server writes is blank, in step with "
                        "this row, or already the value it will get", reverse_pk)


def _identity(p: Proposal, key: str) -> tuple | None:
    """(person, other, code[, title, first year]) of the row a proposal touches:
    the key as it is now for an update or delete, the new row for a create. None
    when any part is not a plain value (a person or title created in this batch)."""
    def value(field):
        if p.operation == "create":
            return p.changes.get(field, (p.target_pk or {}).get(field))
        return (p.target_pk or {}).get(field)
    if key == "kinship":
        parts = (p.person_id, value("c_kin_id"), value("c_kin_code"))
    else:
        parts = (p.person_id, value("c_assoc_id"), value("c_assoc_code"),
                 value("c_text_title"), _int(value("c_assoc_first_year")))
    if any(isinstance(v, dict) or v is None for v in parts[:3]):
        return None
    return parts[:2] + (_int(parts[2]),) + parts[3:]


def _same_relationship(p: Proposal, key: str, mine: tuple | None,
                       pairs: PairCodes) -> bool:
    """Whether `p` writes this row or its reverse - the race the server's guard
    cannot see. Another relationship between the same two people (a different
    code, title or year) is a different row and is not a race."""
    try:
        if find_spec_by_alias(p.resource).key != key:
            return False
    except Exception:  # noqa: BLE001 - an unknown alias is reported by validate
        return False
    theirs = _identity(p, key)
    if mine is None or theirs is None:
        return False
    if theirs == mine:
        return True
    me, other, code = mine[:3]
    reverse = (pairs.kin_locator(code) if key == "kinship" else pairs.assoc_reverse(code))
    return (theirs[0] == other and theirs[1] == me and theirs[2] in reverse
            and theirs[3:] == mine[3:])


def _candidates(api, key: str, me: int, other: int, pk: dict,
                pairs: PairCodes) -> list[dict]:
    person = _person(api, other)
    if key == "kinship":
        codes = set(pairs.kin_locator(pk["c_kin_code"]))
        return [{"c_personid": other, "c_kin_id": me, "c_kin_code": int(e["KinCode"])}
                for e in _entries(person, "PersonKinshipInfo", "Kinship")
                if _int(e.get("KinPersonId")) == me and _int(e.get("KinCode")) in codes]
    codes = set(pairs.assoc_reverse(pk["c_assoc_code"]))
    title, first_year = pk.get("c_text_title"), pk.get("c_assoc_first_year")
    out = []
    for e in _entries(person, "PersonSocialAssociation", "Association"):
        if (_int(e.get("AssocPersonId")) == me and _int(e.get("AssocCode")) in codes
                and _title(e.get("TextTitle")) == _title(title)
                and _int(e.get("Year")) == _int(first_year)):
            out.append({
                "c_personid": other, "c_assoc_code": int(e["AssocCode"]), "c_assoc_id": me,
                # Mirror rows carry the server's own values here: the forward kin
                # codes' pairs (0 stays 0) and the forward person's id.
                "c_kin_code": _paired(pairs, pk.get("c_kin_code")),
                "c_kin_id": _int(e.get("KinPersonId")) or 0,
                "c_assoc_kin_code": _paired(pairs, pk.get("c_assoc_kin_code")),
                "c_assoc_kin_id": _int(e.get("AssocKinPersonId")) or 0,
                "c_text_title": title, "c_assoc_first_year": first_year,
            })
    return out


def _title(value: Any) -> str:
    """MySQL compares with trailing spaces ignored (PAD SPACE collations)."""
    return str(value if value is not None else "").rstrip()


_PAIR_FIELDS = ("c_assocship_pair", "c_kinship_pair", "c_assoc_kinship_pair")


def _backfills(key: str, changes: dict) -> bool:
    """Whether the server creates a missing reverse row for this update.

    associations: whenever a pair code is sent (`allowBackfill = pendingPairs
    ['maintain']`). kinship: only on the pair-only path - `c_kinship_pair` and no
    KIN_DATA column (`handlePairOnlyMirrorSync`, allowBackfill=true); an ordinary
    kinship update never back-fills (AGENTS.md's pair-only trap)."""
    if key == "associations":
        return any(f in changes for f in _PAIR_FIELDS)
    return "c_kinship_pair" in changes and set(changes) <= {"c_kinship_pair"}


def _server_set(key: str) -> frozenset[str]:
    return (frozenset({"c_assoc_code", "c_kin_code", "c_kin_id", "c_assoc_kin_code",
                       "c_assoc_kin_id"}) if key == "associations"
            else frozenset({"c_kin_code"}))


def _server_values(key: str, me: int, new: dict, reverse: dict, changes: dict,
                   pairs: PairCodes) -> dict:
    """The values `afterDirectUpdate` puts in the columns it sets itself."""
    if key == "associations":
        def pair_of(field, table):
            sent = changes.get(field)
            if sent is not None:
                return _int(sent)
            code = _int(new.get(table))
            reverse_codes = (pairs.assoc_reverse(code) if table == "c_assoc_code"
                             else pairs.kin_reverse(code)) if code else []
            return reverse_codes[0] if reverse_codes else 0
        return {
            "c_assoc_code": pair_of("c_assocship_pair", "c_assoc_code"),
            "c_kin_code": pair_of("c_kinship_pair", "c_kin_code"),
            "c_assoc_kin_code": pair_of("c_assoc_kinship_pair", "c_assoc_kin_code"),
            "c_kin_id": me, "c_assoc_kin_id": me,
        }
    # kinship: the reverse code is rewritten only when the code changes or an
    # override is sent; otherwise the reverse row keeps its own.
    if "c_kinship_pair" in changes:
        return {"c_kin_code": _int(changes["c_kinship_pair"])}
    if "c_kin_code" in changes:
        reverse_codes = pairs.kin_reverse(changes["c_kin_code"])
        return {"c_kin_code": reverse_codes[0] if reverse_codes else 0}
    return {"c_kin_code": reverse.get("c_kin_code")}


def _paired(pairs: PairCodes, code: Any) -> int:
    if not _int(code):
        return 0
    reverse = pairs.kin_reverse(code)
    return reverse[0] if reverse else 0


def _person(api, person_id: int) -> dict:
    body = api.client.get("/cbdbapi/person", params={"id": person_id, "mode": "json"},
                          public=True)
    try:
        return body["Package"]["PersonAuthority"]["PersonInfo"]["Person"]
    except (KeyError, TypeError):
        raise CbdbApiError(f"/cbdbapi/person?id={person_id} returned an unexpected "
                           "shape") from None


def _entries(person: dict, group: str, item: str) -> list[dict]:
    """An absent collection means "none" (the endpoint strips empty ones)."""
    group_value = person.get(group) if isinstance(person, dict) else None
    value = (group_value.get(item) if isinstance(group_value, dict) else None) or []
    return [value] if isinstance(value, dict) else [e for e in value if isinstance(e, dict)]


def _row(api, key: str, person_id: int, pk: dict) -> dict | None:
    try:
        body = api.get(key, person_id=person_id, target_pk=pk)
    except NotFoundError:
        return None
    result = body.get("result") if isinstance(body, dict) else None
    row = result.get("row") if isinstance(result, dict) else None
    return row if isinstance(row, dict) else None


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


_SENTINELS = (0, -1, -999, -9999)


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _blank(value: Any) -> bool:
    """The server's 「對面空白／哨兵」: nothing, or a sentinel number - written as a
    number or as exactly that number's text. Whitespace is content."""
    if value is None or value == "":
        return True
    if _number(value):
        return value in _SENTINELS
    return isinstance(value, str) and value in {str(n) for n in _SENTINELS}


def _same(a: Any, b: Any) -> bool:
    """Equal as stored. Text is compared byte for byte - "text " is not "text",
    "012" is not "12" (AGENTS.md: existing content is preserved exactly). Numbers
    compare as numbers, and a number equals only its own plain text form."""
    if _number(a) and _number(b):
        return a == b
    if _number(a) or _number(b):
        number, text = (a, b) if _number(a) else (b, a)
        return isinstance(text, str) and text == str(int(number) if float(number).is_integer()
                                                     else number)
    return a == b


def _pk_text(pk: dict) -> str:
    return "(" + ", ".join(f"{k}={v}" for k, v in pk.items()) + ")"


# --- the record ----------------------------------------------------------------


def apply_reports(batch: StagingBatch, reports: list[MirrorReport]) -> list[str]:
    """Write each answer into the proposal's `mirror` conflict.

    Safe: resolved `confirmed`, unless a reviewer already decided. Anything else:
    the conflict is (re)opened with what was found, and a resolution this module set
    earlier is withdrawn. A resolution the reviewer set is never changed, in either
    direction (`SET_BY_TOOL` marks the ones that are not theirs).
    """
    by_id = {p.id: p for p in batch.proposals}
    lines = []
    for r in reports:
        proposal = by_id[r.proposal_id]
        conflict = next((c for c in proposal.conflicts if c.field == "mirror"), None)
        if conflict is None:
            conflict = new_conflict(proposal.id, find_spec_by_alias(proposal.resource).key)
            proposal.conflicts.append(conflict)
        # Ours only if the tool itself wrote this resolution - recorded explicitly,
        # since a verdict of `safe` on its own does not say who chose `confirmed`.
        ours = conflict.resolution is None or (
            conflict.resolution == "confirmed"
            and str(conflict.agent_reasoning or "").endswith(SET_BY_TOOL))
        reasoning = f"{MARK} {r.status} - {r.evidence}"
        if r.status == SAFE:
            conflict.agent_suggestion = "confirmed"
            if ours:
                conflict.resolution = "confirmed"
                reasoning += SET_BY_TOOL
        else:
            conflict.agent_suggestion = "defer" if r.status in (
                DIVERGENT, AMBIGUOUS, SAME_BATCH, BACKFILL) else None
            if ours:
                conflict.resolution = None
        conflict.agent_reasoning = reasoning
        lines.append(f"{r.proposal_id}: {r.status} - {r.evidence}")
    return lines


def new_conflict(pid: str, resource_key: str) -> Conflict:
    return Conflict(
        id=f"{pid}-mirror", field="mirror",
        description=(
            f"The server copies this whole {resource_key} row onto its reverse row, "
            "not only the fields that change. Blank or in step there is safe; content "
            "of its own needs a decision (AGENTS.md, reverse-pair mirror sync). "
            "`python -m cbdb_agent check-mirrors` reads the reverse row live and "
            "records the answer here; run it again just before submit."),
        options=[
            ConflictOption(value="confirmed",
                           rationale="the reverse row is blank or the same"),
            ConflictOption(value="defer", rationale="leave it out of this batch"),
        ],
    )
