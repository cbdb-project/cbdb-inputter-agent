"""End-to-end checks of the offline review page, in a real browser.

Why this exists: `node --check` on the page's script block passes happily while the
page renders nothing. That is not hypothetical - it is exactly what happened when
`REVIEW_JSON_SCHEMA_VERSION` was bumped in Python and the page's matching `SCHEMA`
constant was not. Valid syntax, correct-looking diff, and a page that refuses every
export. Only actually loading it catches that class of bug.

Skipped when Playwright (or its browser binary) isn't installed, so the suite still
runs anywhere. Install with:  pip install playwright && playwright install chromium
"""

import json
import re

import pytest

from cbdb_agent.review import REVIEW_JSON_SCHEMA_VERSION, export_review_json
from cbdb_agent.staging import (
    Conflict,
    ConflictOption,
    Proposal,
    StagingBatch,
    find_issues,
)

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright not installed"
)

REPO_ROOT = pytest.importorskip("pathlib").Path(__file__).resolve().parents[1]
PAGE = REPO_ROOT / "tools" / "review" / "index.html"


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as pw:
        try:
            instance = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - browser binary not installed
            pytest.skip(f"chromium unavailable: {exc}")
        yield instance
        instance.close()


@pytest.fixture
def page(browser):
    context = browser.new_context()
    page = context.new_page()
    page.errors = []
    page.dialogs = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.on("console", lambda m: page.errors.append(m.text) if m.type == "error" else None)
    page.on("dialog", lambda d: (page.dialogs.append(d.message), d.dismiss()))
    yield page
    context.close()


def _batch():
    person = Proposal(
        id="p1",
        resource="basicinformation",
        operation="create",
        person_id="NEW",
        changes={"c_name_chn": "賀元忠", "c_dy": 18},
        source_quote="賀元忠，曲阜學正",
        confidence="high",
    )
    posting = Proposal(
        id="p1o1",
        resource="postings",
        operation="create",
        person_id="p1",
        target_pk={},
        changes={"c_addr": [17099], "c_source": 27144},
        source_quote="曲阜學正",
        confidence="low",
        conflicts=[
            Conflict(
                id="c1",
                field="c_office_id",
                description="which 學正 code",
                options=[
                    ConflictOption(value=64674, rationale="state level"),
                    ConflictOption(value=63343, rationale="generic"),
                    # A DECISION sharing the field with real codes - must not be
                    # reported as an unresolvable code.
                    ConflictOption(value="defer", rationale="skip it"),
                ],
                agent_suggestion=63343,
            )
        ],
    )
    return StagingBatch(batch_id="page-test", proposals=[person, posting])


def _labels():
    return {
        "office:64674": {"label": "州學正", "sub": "zhou xue zheng",
                         "lines": ["朝代 c_dy=18（元）", "類型 0 所有門類 › 18 元朝 › 諸州門"]},
        "office:63343": {"label": "學正", "sub": "xue zheng"},
        "addr:17099": {"label": "曲阜", "sub": "Qufu · Xian · 1235~1367",
                       "lines": ["隸屬：16776 元朝 › 17097 兗州"]},
        "text:27144": {"label": "全元文", "sub": "Quan Yuan Wen · 1998"},
        "dynasty:18": {"label": "元", "sub": "Yuan · 1234~1367"},
    }


def _load(page, tmp_path, *, code_labels=None, mangle=None, renders=True):
    batch = _batch()
    payload = json.loads(
        export_review_json(batch, find_issues(batch), code_labels=code_labels)
    )
    if mangle:
        mangle(payload)
    review = tmp_path / "review.json"
    review.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    page.goto(PAGE.as_uri())
    page.set_input_files("#file", str(review))
    # set_input_files returns before the page has read the file: the FileReader
    # is async, so a test that asserted on #stats straight away raced the render
    # and failed roughly one run in ten. Wait for the render, not for luck.
    # `renders=False` for the payloads the page is SUPPOSED to reject - there the
    # refusal is the behaviour under test and nothing ever renders.
    if renders:
        page.wait_for_function(
            "() => document.querySelector('#stats')"
            "        && document.querySelector('#stats').innerText.trim().length > 0"
        )
    return payload


def test_page_declares_the_same_schema_version_as_the_exporter():
    """The bug this whole file exists for: a bumped exporter and a stale page."""
    source = PAGE.read_text(encoding="utf-8")
    declared = re.search(r"const SCHEMA = (\d+)", source)
    assert declared, "the page must declare a SCHEMA constant"
    assert int(declared.group(1)) == REVIEW_JSON_SCHEMA_VERSION


def test_page_loads_a_batch_and_renders_a_group_per_person(page, tmp_path):
    _load(page, tmp_path, code_labels=_labels())
    page.wait_for_selector("details.group")
    assert page.locator("details.group").count() == 1     # one person
    assert page.locator("tbody tr").count() == 2          # person + posting
    assert page.locator("#drop").is_hidden()
    assert page.errors == []


def test_page_refuses_a_schema_version_it_does_not_understand(page, tmp_path):
    def bump(payload):
        payload["schema_version"] = REVIEW_JSON_SCHEMA_VERSION + 1

    _load(page, tmp_path, mangle=bump, renders=False)
    page.wait_for_timeout(300)
    assert any("schema_version" in d for d in page.dialogs)
    assert page.locator("details.group").count() == 0


def test_codes_render_their_resolved_name_and_detail_lines(page, tmp_path):
    _load(page, tmp_path, code_labels=_labels())
    page.wait_for_selector(".codeinfo")
    text = page.locator("#groups").inner_text()
    assert "全元文" in text          # c_source -> book title
    assert "曲阜" in text            # c_addr (a LIST field) -> address
    assert "1235~1367" in text       # the leaf's own validity years
    assert "隸屬：" in text          # the parent chain
    assert "元" in text              # c_dy


def test_code_detail_is_read_only(page, tmp_path):
    """A code's MEANING is not a value that could be saved - it must never be an input."""
    _load(page, tmp_path, code_labels=_labels())
    page.wait_for_selector(".codeinfo")
    assert page.locator(".codeinfo input").count() == 0
    assert page.locator(".codeinfo textarea").count() == 0


def test_conflict_option_chips_carry_the_code_name(page, tmp_path):
    """Choosing between 64674 and 63343 is impossible with bare integers."""
    _load(page, tmp_path, code_labels=_labels())
    page.wait_for_selector(".conflict .opt")
    chips = page.locator(".conflict .opt").all_inner_texts()
    assert any("州學正" in c for c in chips)
    assert any("學正" in c for c in chips)


def test_a_non_code_option_is_not_flagged_as_unresolvable(page, tmp_path):
    """'defer' shares the field with real codes but is a decision, not a code."""
    _load(page, tmp_path, code_labels=_labels())
    page.wait_for_selector(".conflict .opt")
    assert "defer — no match" not in page.locator("#groups").inner_text()
    assert page.locator(".cmiss").count() == 0


def test_missing_labels_say_the_export_had_none(page, tmp_path):
    """Distinguish 'not in that table' from 'no lookup ran' - different fixes."""
    _load(page, tmp_path, code_labels={})
    page.wait_for_selector(".codeinfo")
    assert "not resolved" in page.locator("#groups").inner_text()
    assert "code names unavailable" in page.locator("#stats").inner_text()


def test_resolving_a_conflict_stages_a_decision_and_exports_it(page, tmp_path):
    _load(page, tmp_path, code_labels=_labels())
    page.wait_for_selector(".conflict .opt")
    assert "0 decision" in page.locator("#pending").inner_text()
    page.locator(".conflict .opt").first.click()
    page.wait_for_timeout(150)
    assert "1 decision" in page.locator("#pending").inner_text()

    with page.expect_download() as download:
        page.click("#export")
    exported = json.loads(
        pytest.importorskip("pathlib").Path(download.value.path()).read_text(
            encoding="utf-8"
        )
    )
    assert exported["batch_id"] == "page-test"
    assert exported["schema_version"] == REVIEW_JSON_SCHEMA_VERSION
    # Every decision carries the content hash of the proposal it was made about -
    # not only the signatures. `apply-review` refuses one whose hash has moved.
    assert len(exported["decisions"]) == 1
    decision = exported["decisions"][0]
    assert decision["proposal_id"] == "p1o1"
    assert decision["conflict_id"] == "c1"
    assert decision["resolution"] == 64674
    by_id = {p["id"]: p for p in json.loads(
        (tmp_path / "review.json").read_text(encoding="utf-8"))["proposals"]}
    assert decision["content_hash"] == by_id["p1o1"]["content_hash"]
    assert page.errors == []


def test_editing_a_field_updates_the_code_meaning_shown_next_to_it(page, tmp_path):
    """The label follows the EDITED value - that is the point of having it on screen
    while editing."""
    _load(page, tmp_path, code_labels=_labels())
    page.wait_for_selector(".codeinfo")
    # c_source 27144 (全元文) -> 6088, which has no label in this fixture
    source_input = page.locator("tbody tr", has_text="postings").locator(
        "input.fedit"
    ).nth(1)
    source_input.fill("6088")
    source_input.press("Enter")
    page.wait_for_timeout(200)
    body = page.locator("#groups").inner_text()
    assert "全元文" not in body, "the old code's name must not linger"
    assert "6088" in body


def test_filtering_narrows_the_view(page, tmp_path):
    _load(page, tmp_path, code_labels=_labels())
    page.wait_for_selector("details.group")
    page.fill("#q", "no-such-person")
    page.wait_for_timeout(200)
    assert page.locator("details.group").count() == 0
    page.fill("#q", "賀元忠")
    page.wait_for_timeout(200)
    assert page.locator("details.group").count() == 1


def test_a_book_title_containing_markup_is_not_injected_as_html(page, tmp_path):
    """Labels come from CBDB data. Render them as text, always."""
    labels = _labels()
    labels["text:27144"] = {"label": "<img src=x onerror=alert(1)>惡意書名"}
    _load(page, tmp_path, code_labels=labels)
    page.wait_for_selector(".codeinfo")
    assert page.locator("#groups img").count() == 0
    assert "<img" in page.locator("#groups").inner_text()
    assert page.dialogs == []
    assert page.errors == []


# ===========================================================================
# Bulk approval - the rule-12 gate at batch scale
# ===========================================================================

def _gated_batch(n=3):
    """n approval-gated proposals, the shape of the salt-address batch."""
    return StagingBatch(
        batch_id="gated",
        source_excerpt="x",
        proposals=[
            Proposal(
                id=f"a{i}",
                resource="addr-codes",
                operation="create",
                person_id=0,
                changes={"c_name_chn": f"地名{i}"},
                source_quote="q",
                confidence="high",
            )
            for i in range(n)
        ],
    )


def _mixed_gated_batch():
    """Two tables whose irreversibility differs sharply - the real batch's shape.

    ADDR_CODES is correctable after the fact; an ADDR_BELONGS_DATA key never is.
    """
    return StagingBatch(
        batch_id="gated",
        source_excerpt="x",
        proposals=[
            Proposal(id="a1", resource="addr-codes", operation="create",
                     person_id=0, changes={"c_name_chn": "泰州"},
                     source_quote="q", confidence="high"),
            Proposal(id="e1", resource="addr-belongs-data", operation="create",
                     person_id=0,
                     target_pk={"c_addr_id": 1, "c_belongs_to": 2,
                                "c_firstyear": 1368, "c_lastyear": 1643},
                     changes={"c_source": 0}, source_quote="q", confidence="high"),
        ],
    )


def _load_batch(page, tmp_path, batch):
    payload = json.loads(export_review_json(batch, find_issues(batch)))
    review = tmp_path / "review.json"
    review.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    page.goto(PAGE.as_uri())
    page.set_input_files("#file", str(review))
    page.wait_for_function(
        "() => document.querySelector('#stats')"
        "        && document.querySelector('#stats').innerText.trim().length > 0"
    )
    return payload


def _load_gated(page, tmp_path, n=3):
    batch = _gated_batch(n)
    payload = json.loads(export_review_json(batch, find_issues(batch)))
    review = tmp_path / "review.json"
    review.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    page.goto(PAGE.as_uri())
    page.set_input_files("#file", str(review))
    # set_input_files returns before the page has read the file: the FileReader
    # is async, so a test that asserted on #stats straight away raced the render
    # and failed roughly one run in ten. Wait for the render, not for luck.
    page.wait_for_function(
        "() => document.querySelector('#stats')"
        "        && document.querySelector('#stats').innerText.trim().length > 0"
    )
    return payload


def test_every_gated_proposal_starts_unsigned(page, tmp_path):
    payload = _load_gated(page, tmp_path)
    assert all(p["needs_approval"] for p in payload["proposals"])
    assert all(not p["approved_by"] for p in payload["proposals"])
    assert "3 missing approvals" in page.inner_text("#stats").replace("\n", " ")


def test_the_bulk_control_appears_only_when_several_are_missing(page, tmp_path):
    _load_gated(page, tmp_path, n=1)
    assert page.locator(".bulkbtn").count() == 0, \
        "one signature is not a bulk problem"
    _load_gated(page, tmp_path, n=3)
    assert page.locator(".bulkbtn").count() == 1


def test_the_bulk_control_is_in_the_action_bar_not_the_header(page, tmp_path):
    """Grouping, not a barrier - and the code must not claim otherwise.

    Among the summary chips, "Sign all N" read as one more piece of status next to
    "N missing approvals". In the action bar it sits with Export and Discard, the
    other two batch-wide controls. The footer is `position:fixed`, so this buys no
    scrolling and the comment in the page says so; what actually stands between a
    reviewer and N signatures is the confirmation, tested above.
    """
    _load_gated(page, tmp_path, n=3)
    assert page.locator("#footer .bulkbtn").count() == 1
    assert page.locator("#stats .bulkbtn").count() == 0


def test_signing_all_records_the_name_on_every_proposal(page, tmp_path):
    """The whole point: one deliberate signature, recorded on each row.

    The confirm() is accepted here; the next test pins that declining it writes
    nothing.
    """
    _load_gated(page, tmp_path, n=3)
    # Stub confirm() rather than adding a second dialog listener: the `page`
    # fixture already registers one that dismisses, and both would fire.
    page.evaluate("window.confirm = () => true")
    page.fill(".bulksig", "Hongsu Wang")
    page.click(".bulkbtn")
    decisions = page.evaluate("buildDecisions()")["decisions"]
    signed = [d for d in decisions if d.get("approved_by")]
    assert len(signed) == 3
    assert {d["approved_by"] for d in signed} == {"Hongsu Wang"}
    assert "0 missing approvals" in page.inner_text("#stats").replace("\n", " ")


def test_declining_the_confirmation_signs_nothing(page, tmp_path):
    _load_gated(page, tmp_path, n=3)
    page.evaluate("window.confirm = () => false")
    page.fill(".bulksig", "Hongsu Wang")
    page.click(".bulkbtn")
    assert page.evaluate("buildDecisions()")["decisions"] == []


def test_an_empty_name_signs_nothing(page, tmp_path):
    _load_gated(page, tmp_path, n=3)
    page.evaluate("window.confirm = () => true")
    page.click(".bulkbtn")
    assert page.evaluate("buildDecisions()")["decisions"] == []


def test_the_confirmation_names_the_count_and_the_resources(page, tmp_path):
    _load_gated(page, tmp_path, n=3)
    page.evaluate("window.confirm = (m) => { window.__msg = m; return true; }")
    page.fill(".bulksig", "Hongsu Wang")
    page.click(".bulkbtn")
    msg = page.evaluate("window.__msg")
    assert msg and "3 proposals" in msg
    assert "3 × addr-codes" in msg
    assert "cannot be deleted" in msg


def test_the_confirmation_gives_each_table_its_own_risk(page, tmp_path):
    """The bulk dialog must not flatten what the per-row panel distinguishes.

    Its first version said one thing for the whole selection - "no delete path for
    a code table" - which understated ADDR_BELONGS_DATA (57 of the real batch's 114
    rows, whose four-column key can never be edited or deleted) and would have been
    simply false for an `office` proposal in the same batch.
    """
    _load_batch(page, tmp_path, _mixed_gated_batch())
    page.evaluate("window.confirm = (m) => { window.__msg = m; return true; }")
    page.fill(".bulksig", "Hongsu Wang")
    page.click(".bulkbtn")
    msg = page.evaluate("window.__msg")
    assert "1 × addr-codes" in msg and "1 × addr-belongs-data" in msg
    assert "every column is editable" in msg, "the correctable one"
    assert "four-column key" in msg, "the permanent one"


def test_an_unknown_resource_gets_no_invented_reassurance(page, tmp_path):
    """The fallback branch used to assert "no delete path for a code table" for any
    resource it did not recognise - the exact sentence AGENTS.md flags as false for
    the entity aggregates, which ARE deletable while unreferenced."""
    source = PAGE.read_text(encoding="utf-8")
    assert "The server offers no delete path for a code table.\"" not in source
    assert "depends " in source and "on the table" in source


def test_the_risk_text_is_specific_to_the_table(page, tmp_path):
    """A reviewer deciding whether to sign deserves the specific answer.

    ADDR_CODES is correctable after the fact; ADDR_BELONGS_DATA is not. Telling
    them the same worst case for both trains them to ignore it.
    """
    source = PAGE.read_text(encoding="utf-8")
    assert "four-column key" in source, "the belongs-data wording must be present"
    _load_gated(page, tmp_path, n=1)
    text = page.inner_text(".approval")
    assert "every column is editable" in text, text


def test_bulk_sign_never_reaches_a_row_the_filters_have_hidden(page, tmp_path):
    """The table honours the filters; "Sign all" has to as well.

    A reviewer who narrows to one table and clicks the button is signing what they
    are looking at. Signing the rows they filtered away would be the opposite of a
    deliberate signature - and the count on the button would be a number about a
    different set than the one it writes to.
    """
    _load_batch(page, tmp_path, _mixed_gated_batch())
    assert page.locator("#footer .bulkbtn").count() == 1, "2 unsigned to start"

    page.select_option("#fRes", "addr-codes")
    page.wait_for_timeout(100)
    # One visible row left, so the bulk control is gone - one signature is not a
    # bulk problem, and the per-row box is right there.
    assert page.locator("#footer .bulkbtn").count() == 0
    assert "2 missing approvals" in page.inner_text("#stats").replace("\n", " "), \
        "the header still counts the whole batch, which is what a header is for"

    page.select_option("#fRes", "")
    page.wait_for_timeout(100)
    page.evaluate("window.confirm = (m) => { window.__msg = m; return true; }")
    page.fill(".bulksig", "Hongsu Wang")
    page.click(".bulkbtn")
    signed = [d for d in page.evaluate("buildDecisions()")["decisions"]
              if d.get("approved_by")]
    assert len(signed) == 2


# ===========================================================================
# schema 3: a decision is only restored onto the proposal it was made about
# ===========================================================================


def _gated_payload(tmp_path, changes):
    batch = StagingBatch(
        batch_id="gated", source_excerpt="x",
        proposals=[Proposal(id="a1", resource="addr-codes", operation="create",
                            person_id=0, changes=changes, source_quote="q",
                            confidence="high")],
    )
    payload = json.loads(export_review_json(batch, find_issues(batch)))
    review = tmp_path / "review.json"
    review.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload, review


def _load_file(page, review):
    page.set_input_files("#file", str(review))
    page.wait_for_function(
        "() => document.querySelector('#stats')"
        "        && document.querySelector('#stats').innerText.trim().length > 0")


def test_a_signature_is_restored_when_the_proposal_has_not_changed(page, tmp_path):
    _, review = _gated_payload(tmp_path, {"c_name_chn": "\u6cf0\u5dde"})
    page.goto(PAGE.as_uri())
    _load_file(page, review)
    page.fill(".approval input", "Hongsu Wang")
    page.locator(".approval input").blur()
    page.wait_for_timeout(100)

    page.goto(PAGE.as_uri())          # same origin, so localStorage survives
    _load_file(page, review)
    assert "0 missing approvals" in page.inner_text("#stats").replace("\n", " ")
    assert "restored your earlier decisions" in page.inner_text("#saveHint")


def test_a_signature_is_dropped_when_the_proposal_has_changed(page, tmp_path):
    """The batch id and the proposal id both survive a regeneration.

    Without the content hash, re-running the generator after settling an open
    question brought every stored signature back onto rows whose values had
    changed: the header said "0 missing approvals" with nobody having typed
    anything.
    """
    _, review = _gated_payload(tmp_path, {"c_name_chn": "\u6cf0\u5dde"})
    page.goto(PAGE.as_uri())
    _load_file(page, review)
    page.fill(".approval input", "Hongsu Wang")
    page.locator(".approval input").blur()
    page.wait_for_timeout(100)

    # The regeneration: same batch id, same proposal id, different value.
    _, review2 = _gated_payload(tmp_path, {"c_name_chn": "\u901a\u5dde"})
    page.goto(PAGE.as_uri())
    _load_file(page, review2)
    stats = page.inner_text("#stats").replace("\n", " ")
    assert "1 missing approvals" in stats, stats
    hint = page.inner_text("#saveHint")
    assert "discarded 1" in hint and "signature" in hint, hint
    assert page.evaluate("buildDecisions()")["decisions"] == []


def test_every_exported_decision_carries_the_hash(page, tmp_path):
    _, review = _gated_payload(tmp_path, {"c_name_chn": "\u6cf0\u5dde"})
    page.goto(PAGE.as_uri())
    _load_file(page, review)
    page.fill(".approval input", "Hongsu Wang")
    page.locator(".approval input").blur()
    page.wait_for_timeout(100)
    decisions = page.evaluate("buildDecisions()")["decisions"]
    live = page.evaluate("DATA.proposals[0].content_hash")
    assert decisions and all(d["content_hash"] == live for d in decisions)


# ===========================================================================
# The wrong file
# ===========================================================================


def test_dropping_the_staging_yaml_names_the_file_this_page_wants(page, tmp_path):
    """`proposal.yaml` is in the same directory, is what every other command in the
    workflow takes, and is the name a person remembers - so it gets dropped here.

    "Not valid JSON: Unexpected token '#'" is true and useless: it says nothing
    about which of the two files this page reads.
    """
    yaml_file = tmp_path / "proposal.yaml"
    yaml_file.write_text(
        "# Ming/Qing salt administration - Track B: the place names.\n"
        "batch_id: 2026-09-11-salt-addresses\n"
        "proposals:\n"
        "- id: cat-yunsi\n",
        encoding="utf-8",
    )
    page.goto(PAGE.as_uri())
    page.set_input_files("#file", str(yaml_file))
    page.wait_for_timeout(300)

    assert page.dialogs, "the page must say something"
    message = page.dialogs[-1]
    assert "proposal.yaml" in message
    assert "review.json" in message
    assert "validate --staging" in message
    assert page.locator("details.group").count() == 0


def test_a_genuinely_corrupt_json_file_still_says_so(page, tmp_path):
    """The other half: not every parse failure is the wrong file, and claiming it
    was would send someone looking for a file they already have."""
    broken = tmp_path / "review.json"
    broken.write_text('{"schema_version": 3, "proposals": [', encoding="utf-8")
    page.goto(PAGE.as_uri())
    page.set_input_files("#file", str(broken))
    page.wait_for_timeout(300)

    assert page.dialogs
    message = page.dialogs[-1]
    assert "Not valid JSON" in message
    assert "proposal.yaml" not in message
