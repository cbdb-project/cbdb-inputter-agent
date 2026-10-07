"""`mirror_check`: the reverse row of a pair update, found the way the server finds it."""

import sqlite3

import pytest

from cbdb_agent.http_client import AuthenticationError, NotFoundError
from cbdb_agent.mirror_check import (
    MARK,
    PairCodes,
    apply_reports,
    check_batch,
)
from cbdb_agent.staging import Proposal, StagingBatch


def pairs():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE KINSHIP_CODES (c_kincode, c_kin_pair1, c_kin_pair2)")
    c.executemany("INSERT INTO KINSHIP_CODES VALUES (?,?,?)",
                  [(75, 180, 176), (111, 180, 176), (176, 75, 111), (180, 75, 111)])
    c.execute("CREATE TABLE ASSOC_CODES (c_assoc_code, c_assoc_pair, c_assoc_pair2)")
    c.executemany("INSERT INTO ASSOC_CODES VALUES (?,?,?)",
                  [(429, 430, None), (430, 429, None), (14, 13, None), (13, 14, None)])
    return PairCodes(c)


FORWARD_PK = {"c_assoc_code": 429, "c_assoc_id": 770, "c_kin_code": 0, "c_kin_id": 0,
              "c_assoc_kin_code": 0, "c_assoc_kin_id": 0, "c_text_title": "上徐兵部書",
              "c_assoc_first_year": -1}


def update(pid="assoc-003", changes=None, **kw):
    return Proposal(
        id=pid, resource="associations", operation="update", person_id=kw.get("person", 1762),
        target_pk=kw.get("pk", FORWARD_PK),
        changes=changes or {"c_assoc_first_year": 1045, "c_assoc_last_year": 1045},
        source_quote="q", confidence="high",
        conflicts=[{"id": f"{pid}-mirror", "field": "mirror", "description": "d",
                    "options": [{"value": "confirmed", "rationale": "r"},
                                {"value": "defer", "rationale": "r"}]}])


class FakeClient:
    def __init__(self, people):
        self.people = people

    def get(self, path, params=None, public=False):
        assert path == "/cbdbapi/person" and params["mode"] == "json" and public
        return {"Package": {"PersonAuthority": {"PersonInfo": {
            "Person": self.people.get(params["id"], {})}}}}


class FakeApi:
    def __init__(self, rows, people, error=None):
        self.rows, self.client, self.error, self.reads = rows, FakeClient(people), error, []

    def get(self, key, *, person_id, target_pk):
        if self.error:
            raise self.error
        self.reads.append(target_pk)
        key_ = tuple(sorted(target_pk.items()))
        if key_ not in self.rows:
            raise NotFoundError("404", status_code=404)
        return {"ok": True, "result": {"row": self.rows[key_]}}


def row_key(pk):
    return tuple(sorted(pk.items()))


FORWARD_FULL = dict(FORWARD_PK, c_personid=1762)
# What the server writes on a mirror row: the forward person's id in BOTH kin
# columns. Reading it back with the forward row's 0s is how it was once missed.
REVERSE_FULL = {"c_personid": 770, "c_assoc_code": 430, "c_assoc_id": 1762,
                "c_kin_code": 0, "c_kin_id": 1762, "c_assoc_kin_code": 0,
                "c_assoc_kin_id": 1762, "c_text_title": "上徐兵部書",
                "c_assoc_first_year": -1}
PERSON_770 = {"PersonSocialAssociation": {"Association": [
    {"AssocPersonId": "1762", "AssocCode": "430", "TextTitle": "上徐兵部書", "Year": "-1",
     "KinPersonId": "1762", "AssocKinPersonId": "1762"}]}}


def api(forward=None, reverse=None, people=None, error=None):
    rows = {row_key(FORWARD_FULL): forward or {"c_assoc_first_year": -1, "c_notes": "n"}}
    if reverse is not False:
        rows[row_key(REVERSE_FULL)] = reverse or {"c_assoc_first_year": -1, "c_notes": "n"}
    return FakeApi(rows, {770: PERSON_770} if people is None else people, error)


def run(proposals, fake):
    batch = StagingBatch(batch_id="b", proposals=proposals)
    return batch, check_batch(batch, fake, pairs())


class TestTheRead:
    def test_a_blank_reverse_is_safe_and_found_under_the_servers_key(self):
        fake = api()
        _, (report,) = run([update()], fake)
        assert report.status == "safe"
        assert report.reverse_pk["c_kin_id"] == 1762, "the mirror row's own kin ids"

    def test_reverse_content_of_its_own_is_divergent(self):
        _, (report,) = run([update(changes={"c_notes": "new"})],
                           api(reverse={"c_notes": "其他", "c_assoc_first_year": -1}))
        assert report.status == "divergent" and "其他" in report.evidence

    def test_reverse_equal_to_the_forward_old_value_is_safe(self):
        _, (report,) = run([update(changes={"c_notes": "new"})],
                           api(forward={"c_notes": "同"}, reverse={"c_notes": "同"}))
        assert report.status == "safe"

    def test_no_reverse_candidate_is_missing(self):
        _, (report,) = run([update()], api(people={770: {}}))
        assert report.status == "missing"

    def test_a_candidate_with_another_title_does_not_match(self):
        person = {"PersonSocialAssociation": {"Association": [
            dict(PERSON_770["PersonSocialAssociation"]["Association"][0], TextTitle="別")]}}
        _, (report,) = run([update()], api(people={770: person}))
        assert report.status == "missing"

    def test_two_candidates_are_ambiguous(self):
        entry = PERSON_770["PersonSocialAssociation"]["Association"][0]
        person = {"PersonSocialAssociation": {"Association": [entry, dict(entry)]}}
        _, (report,) = run([update()], api(people={770: person}))
        assert report.status == "ambiguous"

    def test_a_reverse_key_that_does_not_read_is_unknown_not_safe(self):
        _, (report,) = run([update()], api(reverse=False))
        assert report.status == "unknown"

    def test_no_snapshot_is_unknown(self):
        batch = StagingBatch(batch_id="b", proposals=[update()])
        (report,) = check_batch(batch, api(), None)
        assert report.status == "unknown"

    def test_dead_credentials_stop_the_check(self):
        with pytest.raises(AuthenticationError):
            run([update()], api(error=AuthenticationError("401", status_code=401)))

    def test_an_update_of_any_field_is_checked_because_the_whole_row_is_copied(self):
        """Changing only c_occasion_code still overwrites the reverse row's notes."""
        _, (report,) = run([update(changes={"c_occasion_code": 6})],
                           api(forward={"c_notes": "n"}, reverse={"c_notes": "其他"}))
        assert report.status == "divergent" and "c_notes" in report.evidence

    def test_an_unchanged_column_of_the_reverse_is_compared(self):
        """The review's case: only c_notes changes, the reverse has its own source."""
        _, (report,) = run(
            [update(changes={"c_notes": "new"})],
            api(forward={"c_notes": "n", "c_source": 1, "c_pages": None},
                reverse={"c_notes": "n", "c_source": 999, "c_pages": "p9"}))
        assert report.status == "divergent"
        assert "c_source" in report.evidence and "c_pages" in report.evidence

    def test_a_reverse_already_holding_the_new_value_is_safe(self):
        _, (report,) = run([update(changes={"c_notes": "new"})],
                           api(forward={"c_notes": "n"}, reverse={"c_notes": "new"}))
        assert report.status == "safe"

    def test_the_columns_the_server_rewrites_are_not_compared(self):
        """The mirror's own kin ids differ from the forward row's by design."""
        _, (report,) = run([update()], api(forward={"c_kin_id": 0, "c_assoc_kin_id": 0},
                                           reverse={"c_kin_id": 1762, "c_assoc_kin_id": 1762}))
        assert report.status == "safe"

    def test_a_missing_reverse_with_a_pair_code_is_a_backfill(self):
        _, (report,) = run([update(changes={"c_notes": "x", "c_assocship_pair": 430})],
                           api(people={770: {}}))
        assert report.status == "backfill" and "CREATE" in report.evidence

    def test_a_malformed_proposal_is_unknown_not_a_crash(self):
        bad = update(pk={k: v for k, v in FORWARD_PK.items() if k != "c_assoc_code"})
        _, (report,) = run([bad], api())
        assert report.status == "unknown"


class TestSameBatch:
    def test_an_update_of_the_reverse_row_is_a_race(self):
        reverse_pk = {k: v for k, v in REVERSE_FULL.items() if k != "c_personid"}
        _, reports = run([update(), update("assoc-x", person=770, pk=dict(
            reverse_pk, c_assoc_id=1762))], api())
        assert reports[0].status == "same-batch"

    def test_another_relationship_between_the_same_two_is_not(self):
        create = Proposal(
            id="assoc-013", resource="associations", operation="create", person_id=1762,
            target_pk={}, changes=dict(FORWARD_PK, c_text_title="[n/a]",
                                       c_assoc_first_year=1054),
            source_quote="q", confidence="high")
        _, reports = run([update(), create], api())
        assert reports[0].status == "safe"


class TestTheRecord:
    def test_safe_resolves_the_question(self):
        batch, reports = run([update()], api())
        apply_reports(batch, reports)
        conflict = batch.proposals[0].conflicts[0]
        assert conflict.resolution == "confirmed"
        assert conflict.agent_reasoning.startswith(MARK)

    def test_a_later_divergence_withdraws_its_own_resolution(self):
        batch, reports = run([update()], api())
        apply_reports(batch, reports)
        reports = check_batch(batch, api(people={770: {}}), pairs())
        apply_reports(batch, reports)
        assert batch.proposals[0].conflicts[0].resolution is None

    def test_a_reviewers_defer_survives_a_later_safe_run(self):
        """The review's sequence: missing -> reviewer defers -> safe. The tool's
        own reasoning was on the conflict, and must not make the defer its own."""
        batch, reports = run([update()], api(people={770: {}}))
        apply_reports(batch, reports)
        batch.proposals[0].conflicts[0].resolution = "defer"
        apply_reports(batch, check_batch(batch, api(), pairs()))
        assert batch.proposals[0].conflicts[0].resolution == "defer"

    def test_a_reviewers_confirm_after_divergence_survives_a_rerun(self):
        batch, reports = run([update(changes={"c_notes": "x"})],
                             api(reverse={"c_notes": "其他"}))
        apply_reports(batch, reports)
        batch.proposals[0].conflicts[0].resolution = "confirmed"
        apply_reports(batch, check_batch(batch, api(reverse={"c_notes": "其他"}), pairs()))
        assert batch.proposals[0].conflicts[0].resolution == "confirmed"

    def test_a_reviewers_decision_is_never_overridden(self):
        batch, reports = run([update()], api(people={770: {}}))
        batch.proposals[0].conflicts[0].resolution = "confirmed"
        batch.proposals[0].conflicts[0].agent_reasoning = "the reviewer read it"
        apply_reports(batch, reports)
        assert batch.proposals[0].conflicts[0].resolution == "confirmed"

    def test_a_proposal_without_the_question_gets_one_when_not_safe(self):
        p = update()
        p.conflicts.clear()
        batch, reports = run([p], api(people={770: {}}))
        apply_reports(batch, reports)
        assert [c.field for c in batch.proposals[0].conflicts] == ["mirror"]
        assert batch.proposals[0].conflicts[0].resolution is None


class TestKinship:
    def test_the_reverse_kin_row_is_located_by_the_union_of_codes(self):
        pk = {"c_kin_id": 7076, "c_kin_code": 176}
        proposal = Proposal(id="kin-1", resource="kinship", operation="update",
                            person_id=1762, target_pk=pk, changes={"c_notes": "x"},
                            source_quote="q", confidence="high")
        person = {"PersonKinshipInfo": {"Kinship": {"KinPersonId": "1762", "KinCode": "75"}}}
        rows = {row_key({"c_personid": 1762, **pk}): {"c_notes": None},
                row_key({"c_personid": 7076, "c_kin_id": 1762, "c_kin_code": 75}): {"c_notes": ""}}
        _, (report,) = run([proposal], FakeApi(rows, {7076: person}))
        assert report.status == "safe"

    def test_overwriting_a_nonblank_autogen_note_is_divergent(self):
        """Asymmetric by nature, but losing it is still a loss to be accepted."""
        pk = {"c_kin_id": 7076, "c_kin_code": 176}
        proposal = Proposal(id="kin-1", resource="kinship", operation="update",
                            person_id=1762, target_pk=pk, changes={"c_notes": "x"},
                            source_quote="q", confidence="high")
        person = {"PersonKinshipInfo": {"Kinship": {"KinPersonId": "1762", "KinCode": "75"}}}
        rows = {row_key({"c_personid": 1762, **pk}): {"c_autogen_notes": None},
                row_key({"c_personid": 7076, "c_kin_id": 1762, "c_kin_code": 75}):
                    {"c_autogen_notes": "Auto-generated from PersonID=1762"}}
        _, (report,) = run([proposal], FakeApi(rows, {7076: person}))
        assert report.status == "divergent" and "c_autogen_notes" in report.evidence

    def test_a_pair_only_update_with_no_reverse_is_a_backfill(self):
        """AGENTS.md's trap: the pair-only path creates a row under the other person."""
        pk = {"c_kin_id": 7076, "c_kin_code": 176}
        proposal = Proposal(id="kin-1", resource="kinship", operation="update",
                            person_id=1762, target_pk=pk, changes={"c_kinship_pair": 75},
                            source_quote="q", confidence="high")
        rows = {row_key({"c_personid": 1762, **pk}): {"c_notes": None}}
        batch, (report,) = run([proposal], FakeApi(rows, {7076: {}}))
        assert report.status == "backfill" and "CREATE" in report.evidence
        apply_reports(batch, [report])
        conflict = batch.proposals[0].conflicts[0]
        assert conflict.resolution is None and conflict.agent_suggestion == "defer"

    def test_an_ordinary_kinship_update_does_not_backfill(self):
        pk = {"c_kin_id": 7076, "c_kin_code": 176}
        proposal = Proposal(id="kin-1", resource="kinship", operation="update",
                            person_id=1762, target_pk=pk,
                            changes={"c_kinship_pair": 75, "c_notes": "x"},
                            source_quote="q", confidence="high")
        rows = {row_key({"c_personid": 1762, **pk}): {"c_notes": None}}
        _, (report,) = run([proposal], FakeApi(rows, {7076: {}}))
        assert report.status == "missing"

    def test_a_hand_chosen_reverse_code_lost_to_an_override_is_divergent(self):
        pk = {"c_kin_id": 7076, "c_kin_code": 176}
        proposal = Proposal(id="kin-1", resource="kinship", operation="update",
                            person_id=1762, target_pk=pk,
                            changes={"c_notes": "x", "c_kinship_pair": 180},
                            source_quote="q", confidence="high")
        person = {"PersonKinshipInfo": {"Kinship": {"KinPersonId": "1762", "KinCode": "75"}}}
        rows = {row_key({"c_personid": 1762, **pk}): {"c_notes": None, "c_kin_code": 176},
                row_key({"c_personid": 7076, "c_kin_id": 1762, "c_kin_code": 75}):
                    {"c_notes": None, "c_kin_code": 75}}
        _, (report,) = run([proposal], FakeApi(rows, {7076: person}))
        assert report.status == "divergent" and "c_kin_code" in report.evidence


class TestExactComparison:
    def test_trailing_whitespace_is_content(self):
        _, (report,) = run([update(changes={"c_notes": "text"})],
                           api(forward={"c_notes": "text"}, reverse={"c_notes": "text "}))
        assert report.status == "divergent"

    def test_a_numeric_looking_string_is_compared_as_text(self):
        _, (report,) = run([update(changes={"c_notes": "x"})],
                           api(forward={"c_pages": "12"}, reverse={"c_pages": "012"}))
        assert report.status == "divergent"

    def test_a_number_and_its_plain_text_are_equal(self):
        _, (report,) = run([update(changes={"c_assoc_first_year": 1045})],
                           api(forward={"c_assoc_first_year": -1},
                               reverse={"c_assoc_first_year": "1045"}))
        assert report.status == "safe"

    def test_a_third_party_in_the_reverse_kin_id_is_divergent(self):
        """The server writes the forward person there; a real other id is lost."""
        _, (report,) = run([update()], api(reverse={"c_kin_id": 5555}))
        assert report.status == "divergent" and "c_kin_id" in report.evidence


class TestOwnershipSequences:
    def test_a_reviewers_confirm_survives_safe_then_divergent(self):
        """divergent -> reviewer confirms -> safe -> divergent: still the reviewer's."""
        batch, reports = run([update(changes={"c_notes": "x"})],
                             api(reverse={"c_notes": "其他"}))
        apply_reports(batch, reports)
        batch.proposals[0].conflicts[0].resolution = "confirmed"
        apply_reports(batch, check_batch(batch, api(), pairs()))
        apply_reports(batch, check_batch(batch, api(reverse={"c_notes": "其他"}), pairs()))
        assert batch.proposals[0].conflicts[0].resolution == "confirmed"
