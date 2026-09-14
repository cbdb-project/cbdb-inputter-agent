"""Cross-proposal primary-key references: `{"ref": "<proposal id>"}`.

These exist for one shape person data never had - a child row whose own COMPOSITE
key is built out of primary keys the server assigns to sibling creates.
`ADDR_BELONGS_DATA`'s key is (c_addr_id, c_belongs_to, c_firstyear, c_lastyear) and
the first two are `ADDR_CODES.c_addr_id` values that do not exist until the parent
create returns.

What makes this worth pinning hard rather than trusting: an unresolved reference
would be sent as a literal dict and land as a NULL **inside a primary key**, on a
table whose rows can never be deleted and whose key can never be updated
(API.md 13.3, and the update whitelist covers only c_source/c_pages/c_notes).
"""

import json

import pytest
import responses

from cbdb_agent.audit_log import AuditLog
from cbdb_agent.batch_runner import run_batch
from cbdb_agent.config import Config
from cbdb_agent.http_client import HttpClient
from cbdb_agent.mutation_api import MutationApi
from cbdb_agent.staging import (
    Proposal,
    StagingBatch,
    StagingError,
    find_issues,
    is_pk_ref,
    substitute_pk_refs,
    topological_submission_order,
)

BASE = "http://localhost:8000"


def make_api(tmp_path, *, dry_run=False):
    config = Config(
        api_base_url=BASE,
        api_token="test-token",
        dry_run=dry_run,
        confirm_prod=BASE,
        max_requests_per_minute=6000,
        local_audit_log_dir=tmp_path / "logs",
    )
    return MutationApi(HttpClient(config, AuditLog(config.local_audit_log_dir)))


def addr(pid, name="兩淮都轉運鹽使司", **kw):
    return Proposal(
        id=pid, resource="addr-codes", operation="create", person_id=0,
        changes=dict({"c_name_chn": name}, **kw),
        source_quote="q", confidence="high",
    )


def edge(pid, child, parent, first=1368, last=1643):
    return Proposal(
        id=pid, resource="addr-belongs-data", operation="create", person_id=0,
        target_pk={"c_addr_id": child, "c_belongs_to": parent,
                   "c_firstyear": first, "c_lastyear": last},
        changes={"c_source": 0},
        source_quote="q", confidence="high",
    )


# ===========================================================================
# The value form
# ===========================================================================

class TestRefShape:
    def test_only_a_lone_string_ref_key_counts(self):
        assert is_pk_ref({"ref": "p1"})
        assert not is_pk_ref({"ref": 1}), "an int is a value, not a proposal id"
        assert not is_pk_ref({"ref": "p1", "other": 1}), "must be exactly one key"
        assert not is_pk_ref("p1")
        assert not is_pk_ref(4329), "a real c_addr_id must pass through untouched"

    def test_substitution_replaces_only_references(self):
        out = substitute_pk_refs(
            {"c_addr_id": {"ref": "p1"}, "c_belongs_to": 4329, "c_firstyear": 1368},
            {"p1": 700123})
        assert out == {"c_addr_id": 700123, "c_belongs_to": 4329, "c_firstyear": 1368}

    def test_an_unresolved_reference_raises_rather_than_writing_null(self):
        # The whole point. A dict left in place would be serialised into a PK
        # column on a table with no delete.
        with pytest.raises(StagingError, match="cannot be resolved"):
            substitute_pk_refs({"c_addr_id": {"ref": "missing"}}, {})


# ===========================================================================
# Validation
# ===========================================================================

def issues_for(*proposals):
    batch = StagingBatch(batch_id="b", source_excerpt="x", proposals=list(proposals))
    return [i.message for i in find_issues(batch) if i.severity == "error"]


class TestValidation:
    def test_a_good_reference_raises_nothing(self):
        msgs = issues_for(addr("a1"), edge("e1", {"ref": "a1"}, 4329))
        assert not [m for m in msgs if "ref" in m.lower()]

    def test_a_dangling_reference_is_an_error(self):
        msgs = issues_for(edge("e1", {"ref": "nope"}, 4329))
        assert any("not a proposal in this batch" in m for m in msgs)

    def test_a_self_reference_is_an_error(self):
        msgs = issues_for(edge("e1", {"ref": "e1"}, 4329))
        assert any("its own proposal id" in m for m in msgs)

    def test_referencing_an_update_is_an_error(self):
        upd = Proposal(id="u1", resource="addr-codes", operation="update",
                       person_id=0, target_pk={"c_addr_id": 5}, changes={"c_name": "x"},
                       source_quote="q", confidence="high")
        msgs = issues_for(upd, edge("e1", {"ref": "u1"}, 4329))
        assert any("only a create is assigned a new primary key" in m for m in msgs)

    def test_referencing_the_wrong_table_is_an_error(self):
        """`c_addr_id` holds an ADDR_CODES id and nothing else.

        Pointing it at another ADDR_BELONGS_DATA create is doubly wrong - that
        resource supplies its own whole key, so "the id it was assigned" does not
        exist - but the reason to refuse it is the first one: substituting some
        other table's id into this column would be undetectable afterwards.
        """
        msgs = issues_for(edge("e1", {"ref": "a1"}, 4329),
                          edge("a1", 1, 2))
        assert any("this column holds a 'addr_codes' primary key" in m
                   for m in msgs), msgs

    def test_a_reference_to_the_category_create_cannot_land_in_an_address_slot(self):
        """Both creates have exactly one server-assigned key, so "is the target a
        create with one minted id?" cannot tell them apart - and an
        ADMIN_CAT_CODES code substituted into `c_belongs_to` files the place under
        a row that is not a place, permanently."""
        cat = Proposal(
            id="cat1", resource="admin-cat-codes", operation="create", person_id=0,
            changes={"c_admin_cat_py": "Fensi", "c_admin_cat_hz": "分司"},
            source_quote="q", confidence="high",
        )
        msgs = issues_for(cat, edge("e1", {"ref": "a1"}, {"ref": "cat1"}),
                          addr("a1"))
        assert any("this column holds a 'addr_codes' primary key" in m
                   for m in msgs), msgs

    def test_a_reference_in_a_year_column_is_refused(self):
        """`c_firstyear` is a year, not a foreign key.

        Nothing about "the target is a create with one minted id" says which column
        the id belongs in, so a reference here validated and then had an ADDR_CODES
        id substituted into the first year of a key that can never be corrected.
        """
        e = Proposal(
            id="e1", resource="addr-belongs-data", operation="create", person_id=0,
            target_pk={"c_addr_id": {"ref": "a1"}, "c_belongs_to": 4329,
                       "c_firstyear": {"ref": "a1"}, "c_lastyear": 1643},
            changes={"c_source": 0}, source_quote="q", confidence="high",
        )
        msgs = issues_for(e, addr("a1"))
        assert any("not a field that may carry a reference" in m for m in msgs), msgs

    def test_the_two_legitimate_slots_are_still_accepted(self):
        """The other half, so the whitelist cannot be "fixed" by emptying it."""
        assert issues_for(addr("a1"),
                          edge("e1", {"ref": "a1"}, 4329)) == []

    def test_a_reference_inside_changes_is_checked_too(self):
        # ADDR_CODES.c_admin_cat_code references the category create.
        msgs = issues_for(addr("a1", c_admin_cat_code={"ref": "nope"}))
        assert any("not a proposal in this batch" in m for m in msgs)


class TestOrdering:
    def test_a_child_is_submitted_after_the_parent_it_references(self):
        batch = StagingBatch(batch_id="b", source_excerpt="x", proposals=[
            edge("e1", {"ref": "a1"}, 4329), addr("a1")])
        assert [p.id for p in topological_submission_order(batch)] == ["a1", "e1"]

    def test_a_chain_of_references_orders_transitively(self):
        batch = StagingBatch(batch_id="b", source_excerpt="x", proposals=[
            edge("e1", {"ref": "a2"}, {"ref": "a1"}),
            addr("a2", c_admin_cat_code={"ref": "cat"}),
            addr("a1"),
            Proposal(id="cat", resource="admin-cat-codes", operation="create",
                     person_id=0,
                     changes={"c_admin_cat_py": "Fensi", "c_admin_cat_hz": "分司"},
                     source_quote="q", confidence="high"),
        ])
        order = [p.id for p in topological_submission_order(batch)]
        assert order.index("cat") < order.index("a2") < order.index("e1")
        assert order.index("a1") < order.index("e1")

    def test_a_reference_cycle_is_refused(self):
        batch = StagingBatch(batch_id="b", source_excerpt="x", proposals=[
            edge("e1", {"ref": "e2"}, 1), edge("e2", {"ref": "e1"}, 1)])
        with pytest.raises(StagingError, match="cycle|unresolved sibling"):
            topological_submission_order(batch)


# ===========================================================================
# End to end against a mocked server
# ===========================================================================

def _no_live_duplicates():
    """Answer the pre-create duplicate check with "nothing by that name".

    `mutation_api` asks `/api/select/search/addr` immediately before every
    ADDR_CODES create - the generate-to-submit window is where someone else's row
    would appear, and the table has no unique key and no delete. Every submission
    test here therefore needs the endpoint mocked; leaving it out would make the
    tests pass or fail on whether `responses` happened to have a matching stub.
    """
    responses.add(responses.GET, f"{BASE}/api/select/search/addr",
                  json={"data": []}, status=200)


def _capture():
    """Record every create body the runner sends."""
    _no_live_duplicates()
    sent = []

    def callback(request):
        body = json.loads(request.body)
        sent.append(body)
        # The server mints c_addr_id; hand back a distinct one per call.
        pk = {"c_addr_id": 700000 + len(sent)}
        if body.get("resource") == "admin-cat-codes":
            pk = {"c_admin_cat_code": 226}
        elif body.get("resource") == "addr-belongs-data":
            pk = body["target"]["pk"]
        return (200, {}, json.dumps({"ok": True, "result": {"pk": pk, "row": pk}}))

    responses.add_callback(responses.POST, f"{BASE}/api/v2/create",
                           callback=callback, content_type="application/json")
    return sent


class TestSubmission:
    @responses.activate
    def test_the_child_is_sent_the_id_the_server_gave_the_parent(self, tmp_path):
        sent = _capture()
        batch = StagingBatch(batch_id="b", source_excerpt="x", proposals=[
            addr("a1"), edge("e1", {"ref": "a1"}, 4329)])
        results = run_batch(batch, make_api(tmp_path))

        assert [r.status for r in results] == ["success", "success"]
        parent_id = sent[0]  # first create is the address
        assert parent_id["resource"] == "addr-codes"
        child = sent[1]
        assert child["resource"] == "addr-belongs-data"
        assert child["target"]["pk"]["c_addr_id"] == 700001, \
            "the reference must become the id the server actually returned"
        assert child["target"]["pk"]["c_belongs_to"] == 4329, \
            "a literal parent id passes through untouched"

    @responses.activate
    def test_a_reference_inside_changes_is_substituted(self, tmp_path):
        sent = _capture()
        cat = Proposal(id="cat", resource="admin-cat-codes", operation="create",
                       person_id=0,
                       changes={"c_admin_cat_py": "Fensi", "c_admin_cat_hz": "分司"},
                       source_quote="q", confidence="high")
        batch = StagingBatch(batch_id="b", source_excerpt="x", proposals=[
            addr("a1", c_admin_cat_code={"ref": "cat"}), cat])
        results = run_batch(batch, make_api(tmp_path))
        assert [r.status for r in results] == ["success", "success"]
        address = [b for b in sent if b["resource"] == "addr-codes"][0]
        assert address["changes"]["c_admin_cat_code"] == 226

    @responses.activate
    def test_a_child_is_skipped_when_its_parent_fails(self, tmp_path):
        """Never sent with a hole where the key should be.

        The alternative - substituting nothing and letting the request go - would
        put a NULL into a primary key on an undeletable table.
        """
        sent = []

        def callback(request):
            body = json.loads(request.body)
            sent.append(body)
            if body["resource"] == "addr-codes":
                return (422, {}, json.dumps(
                    {"ok": False, "errors": {"changes": ["invalid_value"]}}))
            return (200, {}, json.dumps({"ok": True, "result": {"pk": {}}}))

        _no_live_duplicates()
        responses.add_callback(responses.POST, f"{BASE}/api/v2/create",
                               callback=callback, content_type="application/json")
        batch = StagingBatch(batch_id="b", source_excerpt="x", proposals=[
            addr("a1"), edge("e1", {"ref": "a1"}, 4329)])
        results = run_batch(batch, make_api(tmp_path))

        by_id = {r.proposal_id: r for r in results}
        assert by_id["a1"].status == "failed"
        assert by_id["e1"].status == "skipped_dependency_failed"
        assert [b["resource"] for b in sent] == ["addr-codes"], \
            "the edge must never reach the wire"

    @responses.activate
    def test_dry_run_uses_an_unmistakable_placeholder(self, tmp_path):
        """A dry run has no real id, but must still exercise the child rows.

        Left unresolved, all 57 edges of the real batch would report "dependency
        failed" and the reviewer would never see the shape of the rows that
        cannot afterwards be fixed. The placeholder is a string that names its own
        proposal and says dry-run, so it cannot be read as a key - and nothing is
        sent in this mode anyway.
        """
        batch = StagingBatch(batch_id="b", source_excerpt="x", proposals=[
            addr("a1"), edge("e1", {"ref": "a1"}, 4329)])
        results = run_batch(batch, make_api(tmp_path, dry_run=True))
        by_id = {r.proposal_id: r for r in results}
        assert by_id["a1"].status == "success"
        assert by_id["e1"].status == "success"
        assert by_id["e1"].resolved_target_pk["c_addr_id"] == "<dry-run pk of a1>"
        assert by_id["e1"].resolved_target_pk["c_belongs_to"] == 4329
        assert not responses.calls, "a dry run must send nothing"

    @responses.activate
    def test_a_live_run_never_uses_the_placeholder(self, tmp_path):
        sent = _capture()
        batch = StagingBatch(batch_id="b", source_excerpt="x", proposals=[
            addr("a1"), edge("e1", {"ref": "a1"}, 4329)])
        run_batch(batch, make_api(tmp_path))
        blob = json.dumps(sent, ensure_ascii=False)
        assert "dry-run" not in blob


# ===========================================================================
# The gaps a review pass found: three behaviours the suite claimed and did not
# actually pin, plus the shape that slipped past all three mechanisms at once.
# ===========================================================================


class TestReferencesInsideLists:
    """The address pseudo-fields are LISTS of address ids.

    `postings.c_addr`, `events.c_addr_id` and `possessions.c_addr_id` are the
    natural place to reference a place this batch is creating - and a reference
    there used to be invisible to `iter_pk_refs`, to the dict-where-a-scalar-belongs
    guard, and to `substitute_pk_refs` simultaneously. It went out as a literal
    `{"ref": ...}` inside the list, which PHP casts to `1`: a plausible wrong
    address rather than an error.
    """

    def _posting(self, addrs):
        return Proposal(
            id="po1", resource="postings", operation="create", person_id=703334,
            changes={"c_office_id": 5, "c_addr": addrs},
            source_quote="q", confidence="high",
        )

    def test_a_reference_in_a_list_is_seen_by_validation(self):
        # The posting is FIRST in the batch on purpose. With it second, the order
        # assertion held whether or not the dependency was seen at all, because
        # topological_submission_order preserves input order for unrelated rows -
        # so the test passed with the list walk deleted.
        batch = StagingBatch(batch_id="b", source_excerpt="x", proposals=[
            self._posting([{"ref": "a1"}, 12345]), addr("a1")])
        assert [i for i in find_issues(batch) if i.severity == "error"] == []
        order = [p.id for p in topological_submission_order(batch)]
        assert order.index("a1") < order.index("po1"), \
            "the parent must be created before the row that references it"

    def test_a_reference_in_a_list_is_substituted(self):
        out = substitute_pk_refs({"c_addr": [{"ref": "a1"}, 12345]}, {"a1": 44512})
        assert out == {"c_addr": [44512, 12345]}

    def test_an_unresolvable_reference_in_a_list_raises(self):
        with pytest.raises(StagingError, match=r"c_addr\[0\]"):
            substitute_pk_refs({"c_addr": [{"ref": "a1"}]}, {})

    def test_a_malformed_reference_in_a_list_is_an_error(self):
        batch = StagingBatch(batch_id="b", source_excerpt="x", proposals=[
            addr("a1"), self._posting([{"ref": 1}, 12345])])
        errors = [i for i in find_issues(batch) if i.severity == "error"]
        assert any("c_addr[0]" in i.message for i in errors), errors

    @responses.activate
    def test_nothing_leaves_the_process_with_a_ref_still_in_it(self, tmp_path):
        sent = _capture()
        batch = StagingBatch(batch_id="b", source_excerpt="x", proposals=[
            addr("a1"), self._posting([{"ref": "a1"}, 12345])])
        run_batch(batch, make_api(tmp_path))
        blob = json.dumps(sent, ensure_ascii=False)
        assert '"ref"' not in blob, blob
        posting = next(b for b in sent if b["resource"] == "postings")
        assert posting["changes"]["c_addr"] == [700001, 12345]


class TestTheDryRunPlaceholderStaysInTheDryRun:
    """A live create that returns no usable key must SKIP the child.

    The placeholder `"<dry-run pk of a1>"` exists so a dry run still shows the
    shape of the child rows. Guarded only by "did we get a pk?", a live
    `200 {"ok":true,"result":{}}` would write that literal string into an
    ADDR_BELONGS_DATA primary key - a row that can never be edited or deleted.
    """

    @responses.activate
    def test_a_live_create_with_no_pk_skips_the_child_and_sends_nothing_more(
            self, tmp_path):
        sent = []

        def callback(request):
            sent.append(json.loads(request.body))
            return (200, {}, json.dumps({"ok": True, "result": {}}))

        _no_live_duplicates()
        responses.add_callback(responses.POST, f"{BASE}/api/v2/create",
                               callback=callback,
                               content_type="application/json")
        batch = StagingBatch(batch_id="b", source_excerpt="x", proposals=[
            addr("a1"), edge("e1", {"ref": "a1"}, 4329)])
        results = run_batch(batch, make_api(tmp_path))
        by_id = {r.proposal_id: r for r in results}
        assert by_id["a1"].status == "success"
        assert by_id["e1"].status != "success", by_id["e1"].status
        assert len(sent) == 1, "the edge must not be sent at all"
        assert "dry-run" not in json.dumps(sent, ensure_ascii=False)

    @responses.activate
    def test_the_same_response_in_dry_run_does_produce_the_placeholder(
            self, tmp_path):
        """The other half: the placeholder is not dead code, it is mode-gated."""
        batch = StagingBatch(batch_id="b", source_excerpt="x", proposals=[
            addr("a1"), edge("e1", {"ref": "a1"}, 4329)])
        results = run_batch(batch, make_api(tmp_path, dry_run=True))
        by_id = {r.proposal_id: r for r in results}
        assert by_id["e1"].resolved_target_pk["c_addr_id"] == "<dry-run pk of a1>"


class TestCyclesAreCaughtAtValidateTime:
    """`find_issues` must report a pure reference cycle, not leave it to submit.

    `topological_submission_order` raised on these before the cycle walker was
    widened, so a test that only exercises the order function passes either way -
    while the whole point of the change is that the batch never validates clean.
    """

    def test_find_issues_names_the_cycle(self):
        batch = StagingBatch(batch_id="b", source_excerpt="x", proposals=[
            addr("a1", c_admin_cat_code={"ref": "a2"}),
            addr("a2", c_admin_cat_code={"ref": "a1"}),
        ])
        errors = [i for i in find_issues(batch) if i.severity == "error"]
        assert any("reference cycle" in i.message for i in errors), errors

    def test_a_three_step_cycle_is_caught_too(self):
        batch = StagingBatch(batch_id="b", source_excerpt="x", proposals=[
            addr("a1", c_admin_cat_code={"ref": "a2"}),
            addr("a2", c_admin_cat_code={"ref": "a3"}),
            addr("a3", c_admin_cat_code={"ref": "a1"}),
        ])
        errors = [i for i in find_issues(batch) if i.severity == "error"]
        assert any("reference cycle" in i.message for i in errors), errors

    def test_a_diamond_is_not_a_cycle(self):
        """Two children of one parent must not be mistaken for one."""
        batch = StagingBatch(batch_id="b", source_excerpt="x", proposals=[
            addr("a1"),
            edge("e1", {"ref": "a1"}, 4329),
            edge("e2", {"ref": "a1"}, 6756),
        ])
        assert [i for i in find_issues(batch) if i.severity == "error"] == []


class TestTheWholeCompositeKeyIsRequiredAtValidateTime:
    """ADDR_BELONGS_DATA assigns none of its four key columns.

    Left to the submit-time check, a missing or empty key column surfaces mid-run -
    after earlier rows of the same batch have committed, and those rows have no
    delete path.
    """

    def _edge_with(self, target_pk):
        return Proposal(
            id="e1", resource="addr-belongs-data", operation="create", person_id=0,
            target_pk=target_pk, changes={"c_source": 0},
            source_quote="q", confidence="high",
        )

    def test_a_missing_key_column_is_an_error(self):
        batch = StagingBatch(batch_id="b", source_excerpt="x", proposals=[
            self._edge_with({"c_addr_id": 1, "c_belongs_to": 2,
                             "c_firstyear": 1368})])
        errors = [i for i in find_issues(batch) if i.severity == "error"]
        assert any("c_lastyear" in i.message and "primary-key" in i.message
                   for i in errors), errors

    @pytest.mark.parametrize("empty", [None, ""])
    def test_a_present_but_empty_key_column_is_an_error(self, empty):
        """Key completeness is checked server-side BEFORE normalization, so `null`
        and `""` are 422s - and a key-set difference cannot see either."""
        batch = StagingBatch(batch_id="b", source_excerpt="x", proposals=[
            self._edge_with({"c_addr_id": 1, "c_belongs_to": 2,
                             "c_firstyear": 1368, "c_lastyear": empty})])
        errors = [i for i in find_issues(batch) if i.severity == "error"]
        assert any("c_lastyear" in i.message for i in errors), errors

    def test_a_reference_counts_as_supplied(self):
        """It is resolved to a real id before the request is built."""
        batch = StagingBatch(batch_id="b", source_excerpt="x", proposals=[
            addr("a1"),
            self._edge_with({"c_addr_id": {"ref": "a1"}, "c_belongs_to": 2,
                             "c_firstyear": 1368, "c_lastyear": 1643})])
        assert [i for i in find_issues(batch) if i.severity == "error"] == []

    def test_year_zero_is_a_value_not_an_absence(self):
        """0 is a real year in these columns; only null and "" are absences."""
        batch = StagingBatch(batch_id="b", source_excerpt="x", proposals=[
            self._edge_with({"c_addr_id": 1, "c_belongs_to": 2,
                             "c_firstyear": 0, "c_lastyear": 0})])
        assert [i for i in find_issues(batch) if i.severity == "error"] == []


class TestTheDuplicateCheckRunsAtSubmitTime:
    """Not only where the batch was generated.

    `emit_addresses.py` checks when it builds the file; the review then takes
    minutes or days, and that delay is the point. Anything anyone else enters in
    the window becomes a permanent duplicate otherwise - `ADDR_CODES` has no unique
    key on `c_name_chn` and no delete, and the duplicate then collects
    ADDR_BELONGS_DATA edges whose keys can never be changed.
    """

    @responses.activate
    def test_a_name_that_appeared_since_generation_blocks_the_create(self, tmp_path):
        responses.add(
            responses.GET, f"{BASE}/api/select/search/addr",
            json={"data": [{"c_addr_id": 90001,
                            "c_name_chn": "\u5169\u6dee\u90fd\u8f49\u904b\u9e7d\u4f7f\u53f8"}]},
            status=200)
        sent = []
        responses.add_callback(
            responses.POST, f"{BASE}/api/v2/create",
            callback=lambda r: (sent.append(json.loads(r.body)),
                                (200, {}, json.dumps(
                                    {"ok": True,
                                     "result": {"pk": {"c_addr_id": 1}}})))[1],
            content_type="application/json")

        batch = StagingBatch(batch_id="b", source_excerpt="x", proposals=[
            addr("a1"), edge("e1", {"ref": "a1"}, 4329)])
        results = run_batch(batch, make_api(tmp_path))
        by_id = {r.proposal_id: r for r in results}
        assert by_id["a1"].status == "failed"
        assert "90001" in by_id["a1"].error
        assert by_id["e1"].status == "skipped_dependency_failed"
        assert sent == [], "nothing may be written once the name is known to exist"

    @responses.activate
    def test_a_dry_run_does_not_spend_the_check(self, tmp_path):
        """A dry run previews without touching the target system; an unreachable
        host must not turn a previewed create into a failed proposal."""
        batch = StagingBatch(batch_id="b", source_excerpt="x", proposals=[addr("a1")])
        results = run_batch(batch, make_api(tmp_path, dry_run=True))
        assert [r.status for r in results] == ["success"]
        assert not responses.calls
