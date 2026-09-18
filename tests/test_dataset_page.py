"""End-to-end checks of the DATA review page (`review/dataset.html`), in a browser.

Same argument as `tests/test_review_page.py`: a page whose only consumer is a human
eye has no failing state a Python test can see. `node --check` passes on a script
block that renders nothing, and this page has three fresh ways to render nothing
that Python never touches —

* a `SCHEMA` constant that must track `build_dataset`'s `schema_version`, the exact
  drift that broke the batch page once already;
* a `case` block in `dataset.json` that the page reads for its vocabulary, its
  dynasty axis and every word of its prose, so a build that stops writing it is a
  blank page and a green suite;
* `md()`, which renders case-supplied prose and therefore has to escape first.

Skipped when Playwright (or its browser binary) isn't installed. Install with:
    pip install playwright && playwright install chromium
"""

import json
import re

import pytest

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright not installed"
)

REPO_ROOT = pytest.importorskip("pathlib").Path(__file__).resolve().parents[1]
PAGE = REPO_ROOT / "review" / "dataset.html"


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
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    # The page tries its ?case= fetch on load; over file:// the browser refuses it
    # and the built-in picker takes over. That refusal is the designed path, not a
    # defect, so it is not counted as a page error.
    page.on("console", lambda m: page.errors.append(m.text)
            if m.type == "error" and "Fetch API" not in m.text else None)
    yield page
    context.close()


def page_schema() -> int:
    """The `SCHEMA` the page enforces, read out of its source."""
    m = re.search(r"^const SCHEMA = (\d+);", PAGE.read_text(encoding="utf-8"),
                  re.MULTILINE)
    assert m, "the page no longer declares `const SCHEMA = <n>;`"
    return int(m.group(1))


def _unit(name, kind, dynasty="明代", first=1368, last=1643, emitted=True):
    return {
        "key": f"probe:{name}", "dynasty": dynasty, "dynasty_code": 19, "kind": kind,
        "region": "兩淮", "name": name, "name_short": name,
        "emitted": emitted, "blocked": not emitted,
        "blocked_reason": None if emitted else "awaiting a decision",
        "romanization": "Romanized", "romanization_alt": "Romanized Alt",
        "admin_type": "Duzhuanyunyanshisi" if kind == "運司" else "Fensi",
        "unit_first": first, "unit_last": last, "succession": None,
        "existing_office_matches": [],
        "office": {
            "name": name, "name_alt": None, "translation": "A place",
            "translation_alt": None, "pinyin": "a place", "pinyin_alt": None,
            "dynasty_code": 19, "type_ids": ["0"],
            "type_labels": [{"id": "0", "chn": "節點"}],
            "source_id": 0, "pages": None, "notes": "notes",
        },
        "addresses": [{
            "key": f"probe:{name}@{first}", "first": first, "last": last,
            "end_rule": "unit 起/止", "clamped": False,
            "seat": {"raw": "揚州府", "matched": "揚州府", "addr_id": 100,
                     "admin_type": "fu", "rule": "exact", "rejected": [],
                     "duplicates": [], "note": None},
            "x": 119.4, "y": 32.4, "notes": "a note",
            "belongs": [{"parent_key": None, "parent_addr_id": 4329,
                         "parent_name": "明朝", "first": first, "last": last,
                         "source": 0}],
        }],
    }


def _dataset(**over):
    d = {
        "schema_version": page_schema(),
        "generated_at": "2026-09-18T07:00:00+00:00",
        "case": {
            "name": "probe", "design_doc": "docs/11-x.md",
            "root_kind": "運司", "branch_kind": "分司",
            "admin_categories": {
                "運司": {"id": "cat-a", "py": "Duzhuanyunyanshisi",
                         "hz": "都轉運鹽使司", "trans": "Commission"},
                "分司": {"id": "cat-b", "py": "Fensi", "hz": "分司",
                         "trans": "Branch"},
            },
            "dynasties": {"明代": {"code": 19, "first": 1368, "last": 1643},
                          "清代": {"code": 20, "first": 1644, "last": 1911}},
            "page": {
                "title": "Probe review",
                "headline": "A probe headline",
                "standfirst": "A **bold** standfirst with `code`.",
                "notices": ["A *notice*."],
                "tracks": [{"id": "b", "heading": "Track B", "pill": "API",
                            "verdict": "{address_rows} rows.",
                            "text": "Some `track` text."}],
            },
        },
        "source": {"xlsx": "probe.xlsx", "snapshot": "latest.sqlite3",
                   "snapshot_generated_at": "2026-09-01T00:00:00+00:00"},
        "source_note": "出處：某書。",
        "admin_cat_mode": "new",
        "stats": {"units_total": 2, "units_excluded": 0, "office_creates": 2,
                  "address_rows": 2, "belongs_edges": 2,
                  "findings_blocker": 0, "findings_warning": 0, "findings_note": 0},
        "findings": [],
        "coincident_points": [],
        "units": [_unit("兩淮都轉運鹽使司", "運司"),
                  _unit("兩淮都轉運鹽使司泰州分司", "分司")],
    }
    d.update(over)
    return d


def load(page, dataset, *, renders=True):
    """Serve the dataset as an inlined payload, the way a published copy would."""
    page.goto(PAGE.as_uri())
    page.evaluate("d => { window.CBDB_DATASET = d; }", dataset)
    page.reload()
    page.evaluate("d => { window.CBDB_DATASET = d; }", dataset)
    page.evaluate("() => boot(window.CBDB_DATASET)")
    if renders:
        page.wait_for_function("() => document.querySelector('.masthead') !== null")
    return page


class TestItActuallyRenders:
    def test_a_well_formed_dataset_renders_without_a_page_error(self, page):
        load(page, _dataset())
        assert page.errors == []
        assert page.locator(".masthead h1").inner_text() == "A probe headline"

    def test_the_tab_title_comes_from_the_case(self, page):
        load(page, _dataset())
        assert page.title() == "Probe review"

    def test_a_dataset_the_page_cannot_read_is_refused_not_mis_rendered(self, page):
        """Kills a `SCHEMA` bump on one side only - the drift that broke the batch
        page once, silently."""
        load(page, _dataset(schema_version=page_schema() + 1), renders=False)
        page.wait_for_function(
            "() => document.querySelector('#app .empty') !== null")
        body = page.locator("#app").inner_text()
        assert "schema_version" in body
        assert "Refusing to render" in body


class TestTheCaseBlockIsWhatDrivesIt:
    def test_the_page_speaks_the_case_vocabulary_not_the_salt_one(self, page):
        """Kills any re-hard-coding of 運司/分司 in the page: a case whose kinds are
        府/縣 must see 府/縣 in the controls and the section headings."""
        d = _dataset()
        d["case"]["root_kind"] = "府"
        d["case"]["branch_kind"] = "縣"
        for u in d["units"]:
            u["kind"] = "府" if u is d["units"][0] else "縣"
        load(page, d)
        controls = page.locator(".controls").inner_text()
        assert "府 only" in controls and "縣 only" in controls
        assert "運司" not in page.locator(".sec").first.inner_text()

    def test_a_dynasty_chip_exists_for_each_dynasty_the_case_declares(self, page):
        d = _dataset()
        d["case"]["dynasties"] = {"唐代": {"code": 6, "first": 618, "last": 907}}
        for u in d["units"]:
            u["dynasty"] = "唐代"
            u["addresses"][0]["first"], u["addresses"][0]["last"] = 618, 907
        load(page, d)
        assert "唐代" in page.locator(".controls").inner_text()

    def test_the_admin_type_shown_is_the_one_the_dataset_carries(self, page):
        """Kills a re-derivation of c_admin_type in the page: it must agree with
        what the batch will actually send."""
        d = _dataset()
        d["units"][0]["admin_type"] = "SomethingElse"
        load(page, d)
        assert "SomethingElse" in page.locator(".pane.b").first.inner_text()

    def test_the_design_doc_in_the_footer_comes_from_the_case(self, page):
        d = _dataset()
        d["case"]["design_doc"] = "docs/99-other.md"
        load(page, d)
        assert "docs/99-other.md" in page.locator("footer").inner_text()


class TestCaseProseIsEscapedBeforeItIsFormatted:
    def test_markup_in_case_prose_renders_as_markup(self, page):
        load(page, _dataset())
        html = page.locator(".masthead").inner_html()
        assert "<strong>bold</strong>" in html
        assert "<code>code</code>" in html
        assert "<em>notice</em>" in html

    def test_a_stat_placeholder_is_filled_from_the_dataset_stats(self, page):
        load(page, _dataset())
        assert "2 rows." in page.locator(".track").first.inner_text()

    def test_html_in_case_prose_is_escaped_not_executed(self, page):
        """Kills dropping `H()` from `md()`. The prose is data - it comes off disk
        from a case module - so the page must not be a way to inject markup."""
        d = _dataset()
        d["case"]["page"]["standfirst"] = (
            "<img src=x onerror=\"window.__pwned=1\"> and "
            "**<script>window.__pwned=1</script>**")
        load(page, d)
        html = page.locator(".masthead").inner_html()
        assert "&lt;img" in html and "<img" not in html
        assert "&lt;script&gt;" in html
        assert page.evaluate("() => window.__pwned") is None
        assert page.errors == []

    def test_an_unknown_placeholder_is_left_alone_rather_than_resolved(self, page):
        """`{constructor}` must not reach through to Object.prototype."""
        d = _dataset()
        d["case"]["page"]["standfirst"] = "{constructor} {nosuchstat}"
        load(page, d)
        text = page.locator(".masthead").inner_text()
        assert "{constructor}" in text and "{nosuchstat}" in text
        assert "native code" not in text


class TestTheDataItselfIsShown:
    def test_every_unit_appears(self, page):
        load(page, _dataset())
        assert page.locator(".unit").count() == 2

    def test_an_excluded_unit_is_labelled_rather_than_hidden(self, page):
        d = _dataset()
        d["units"][1]["emitted"] = False
        d["units"][1]["blocked"] = True
        d["stats"]["units_excluded"] = 1
        load(page, d)
        assert "excluded" in page.locator(".unit").nth(1).inner_text().lower()

    def test_a_blocker_finding_is_shown_against_its_unit(self, page):
        d = _dataset()
        d["findings"] = [{"severity": "blocker", "title": "bad", "detail": "why",
                          "unit_key": d["units"][0]["key"]}]
        d["stats"]["findings_blocker"] = 1
        load(page, d)
        assert "bad" in page.locator(".unit").first.inner_text()


class TestTheOptionalLabelsAreOptional:
    """The office pane, the office stat, the rows note and the source_id note all
    describe salt-administration's two-track history. A case without them must get a
    page with fewer labels, not a page with someone else's story on it."""

    def test_a_case_that_supplies_them_shows_them(self, page):
        d = _dataset()
        d["case"]["page"].update({
            "office_stat_label": "office creates (dropped)",
            "office_pane_heading": "Track A · OFFICE_CODES",
            "office_pane_pill": "dropped 2026-09-11",
            "rows_note": "The left pane is **history**.",
            "source_id_note": "by decision.",
        })
        load(page, d)
        # CSS uppercases the stat labels, so compare case-insensitively.
        stats = page.locator(".stats").inner_text().lower()
        assert "office creates (dropped)" in stats
        pane = page.locator(".pane.a").first.inner_text().lower()
        assert "track a" in pane and "dropped 2026-09-11" in pane
        html = page.evaluate(
            "() => [...document.querySelectorAll('.sec-note')]"
            ".map(e => e.innerHTML).join('')")
        assert "<strong>history</strong>" in html
        assert "by decision." in page.locator("footer").inner_text()

    def test_a_case_without_them_renders_anyway(self, page):
        load(page, _dataset())
        assert page.errors == []
        assert "dropped" not in page.locator(".stats").inner_text().lower()
        assert "track a" not in page.locator(".pane.a").first.inner_text().lower()
        assert "OFFICE_CODES" in page.locator(".pane.a").first.inner_text()

    def test_the_footer_reads_the_source_id_rather_than_asserting_one(self, page):
        """It used to state `source_id is 0` as a literal - a review page claiming a
        value it had never looked at."""
        d = _dataset()
        for u in d["units"]:
            u["office"]["source_id"] = 27144
        load(page, d)
        footer = page.locator("footer").inner_text()
        assert "27144" in footer
        assert "未詳" not in footer


class TestTheRealDatasetRenders:
    """The synthetic fixtures above pin the contract; this pins the actual file the
    reviewer opens. Skipped when it has not been generated."""

    def test_the_salt_administration_build_renders(self, page):
        built = REPO_ROOT / "data" / "build" / "salt-administration" / "dataset.json"
        if not built.exists():
            pytest.skip("run build_dataset --case salt-administration first")
        data = json.loads(built.read_text(encoding="utf-8"))
        load(page, data)
        assert page.errors == []
        assert page.locator(".unit").count() == len(data["units"])
        assert page.locator(".masthead h1").inner_text() == data["case"]["page"]["headline"]
