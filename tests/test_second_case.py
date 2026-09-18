"""The tool, run against a case that is not the salt administration.

Why this exists. `src/cbdb_agent/places_and_offices/` claims to be abstract, and the
rest of the suite cannot check that claim: every other test drives it with
`cases/salt-administration/case.py`, so a rule hard-coded to that job's vocabulary
passes everything. A review pass proved the gap was real — `_attach_belongs` tested
`u["kind"] == "運司"` directly, and a prefecture/county gazetteer came out with every
unit blocked and a diagnostic telling the operator to go and find a 運司.

So this file is a second case: a Ming gazetteer whose root kind is 府 and whose
branch kind is 縣, with its own dynasty window, its own category rows, its own
character readings and no SQL export at all. Nothing here resembles salt, which is
the point — if the tool needs to be edited for this to pass, the edit is a job fact
that leaked into `src/`.

It is deliberately small and synthetic rather than a second real contribution: it
has to run in the suite, off a two-row SQLite stub, with no spreadsheet.
"""

import csv
import sqlite3

import pytest

from cbdb_agent.places_and_offices import build_dataset, csv_export, pipeline
from cbdb_agent.places_and_offices import romanize
from cbdb_agent.places_and_offices.seats import SeatRules
from cbdb_agent.places_and_offices.snapshot import Snapshot
from cbdb_agent.places_and_offices.units import RawUnit

PINYIN = {"福": "fu", "州": "zhou", "府": "fu", "閩": "min", "縣": "xian",
          "侯": "hou", "官": "guan"}


class GazetteerCase:
    """A case module, as a class - `load_case` only ever does `getattr`."""

    CASE_NAME = "gazetteer-probe"
    BATCH_TITLE = "A Ming gazetteer"
    DESIGN_DOC = "docs/nonexistent.md"
    ROOT_KIND = "府"
    BRANCH_KIND = "縣"
    KEY_PREFIX = "gaz:"
    UNKNOWN_ADDR_ID = 0
    EXCLUDED_HEADING = "Left out:"
    SOURCE_NOTE = "出處：某志。"
    SOURCE_ID = 0
    BLOCKED: dict = {}
    SUCCESSIONS: dict = {}
    ADMIN_CATEGORIES = {
        "府": dict(id="cat-fu", py="Fu", hz="府", trans="Prefecture"),
        "縣": dict(id="cat-xian", py="Xian", hz="縣", trans="County"),
    }
    DYNASTIES = {"明代": {"window": (1368, 1643), "code": 19,
                         "root_addr_id": 4329, "root_name": "明朝"}}
    SEAT_RULES = SeatRules(dynasties=DYNASTIES, unknown_addr_id=0,
                           unknown_seat_name="未詳")
    REVIEW_PAGE = {"title": "Gazetteer", "headline": "A gazetteer",
                   "standfirst": "s", "notices": [], "tracks": []}

    # Two counties under one prefecture, so both edge rules are exercised: root to
    # dynasty, branch to root.
    ROWS = [("府", "福州", "福州府", 1368, 1643, "福州府"),
            ("縣", "福州", "閩縣", 1368, 1643, "閩縣"),
            ("縣", "福州", "侯官縣", 1368, 1643, "閩縣")]

    @staticmethod
    def read_units(xlsx, dynasty):
        return [RawUnit(kind, region, short, q, z, [(seat, q, z)], None, i)
                for i, (kind, region, short, q, z, seat)
                in enumerate(GazetteerCase.ROWS, start=2)]

    @staticmethod
    def unit_key(dynasty, region, name_short):
        return f"gaz:ming:{region}:{name_short}"

    @staticmethod
    def qualified_name(kind, root_full, name_short):
        return name_short if kind == "府" else root_full + name_short

    @staticmethod
    def name_alt(kind, name_short):
        return None

    @staticmethod
    def translation(kind, region, name_short):
        return f"{region} {'Prefecture' if kind == '府' else 'County'}"

    @staticmethod
    def type_ids_for(kind, dynasty, region):
        return []

    @staticmethod
    def branch_of(name_short):
        return name_short[:-1] if name_short.endswith("縣") else name_short

    @staticmethod
    def address_romanization(region, kind, branch):
        return f"{GazetteerCase.romanize_compact(region)} {kind}"

    @staticmethod
    def romanization_alt(kind, branch):
        return GazetteerCase.romanize_compact(branch or "")

    @staticmethod
    def pinyin_of(chinese):
        return romanize.pinyin_of(chinese, PINYIN)

    @staticmethod
    def romanize_compact(chinese):
        return romanize.romanize_compact(chinese, PINYIN)

    @staticmethod
    def admin_type_of(kind):
        return {"府": "Fu", "縣": "Xian"}[kind]

    @staticmethod
    def address_alt_names(kind, name_short):
        return name_short if kind == "縣" else None

    @staticmethod
    def batch_summary(n_addr, n_edge, xlsx):
        return [f"{n_addr} places, {n_edge} edges."]


@pytest.fixture(scope="module")
def snapshot_path(tmp_path_factory):
    """Two ADDR_CODES rows, and the empty tables the scans touch."""
    path = tmp_path_factory.mktemp("gaz") / "mini.sqlite3"
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE ADDR_CODES (c_addr_id INT, c_name_chn TEXT, c_name TEXT,
      c_firstyear INT, c_lastyear INT, x_coord REAL, y_coord REAL,
      c_admin_type TEXT);
    INSERT INTO ADDR_CODES VALUES
      (9001,'福州府','Fuzhou',1368,1643,119.3,26.0,'fu'),
      (9002,'閩縣','Minxian',1368,1643,119.31,26.01,'xian');
    CREATE TABLE OFFICE_CODES (c_office_id INT, c_office_chn TEXT,
      c_office_chn_alt TEXT, c_office_pinyin TEXT, c_dy INT);
    CREATE TABLE OFFICE_TYPE_TREE (c_office_type_node_id TEXT,
      c_office_type_desc_chn TEXT);
    CREATE TABLE ADDR_BELONGS_DATA (c_addr_id INT, c_belongs_to INT,
      c_firstyear INT, c_lastyear INT);
    """)
    con.commit()
    con.close()
    return path


@pytest.fixture(scope="module")
def built(snapshot_path, tmp_path_factory):
    with Snapshot(snapshot_path) as snap:
        return pipeline.build(GazetteerCase, snapshot_path, snap)


class TestTheToolServesASecondCase:
    def test_nothing_is_blocked(self, built):
        """Kills `u["kind"] == "運司"` anywhere the hierarchy is decided.

        The exact failure this replaces: the root row never matched, so it got no
        dynasty edge and was orphaned; both branches were then orphaned because
        their parent was excluded. Three blockers from one string literal.
        """
        blockers = [f for f in built["findings"] if f["severity"] == "blocker"]
        assert blockers == [], [f["title"] for f in blockers]
        assert all(u["emitted"] for u in built["units"])

    def test_no_finding_mentions_the_other_case_vocabulary(self, built):
        """A diagnostic naming 運司 sends the operator looking for a kind their
        source does not have."""
        for f in built["findings"]:
            blob = f["title"] + f["detail"]
            assert "運司" not in blob and "分司" not in blob, f

    def test_the_root_row_hangs_off_the_dynasty(self, built):
        root = next(u for u in built["units"] if u["kind"] == "府")
        edges = root["addresses"][0]["belongs"]
        assert [(b["parent_addr_id"], b["parent_name"]) for b in edges] == [
            (4329, "明朝")]

    def test_a_branch_hangs_off_its_own_root_row_by_reference(self, built):
        branch = next(u for u in built["units"] if u["name_short"] == "閩縣")
        edge, = branch["addresses"][0]["belongs"]
        assert edge["parent_addr_id"] is None       # server-assigned, carried as a ref
        assert edge["parent_key"].startswith("gaz:ming:福州:福州府@")
        # The parent's own name, not one rebuilt from the region plus a type word.
        assert edge["parent_name"].startswith("福州府@")

    def test_the_qualified_names_are_the_case_convention(self, built):
        names = sorted(u["name"] for u in built["units"])
        assert names == ["福州府", "福州府侯官縣", "福州府閩縣"]

    def test_each_unit_carries_its_own_admin_type(self, built):
        got = {u["name_short"]: u["admin_type"] for u in built["units"]}
        assert got == {"福州府": "Fu", "閩縣": "Xian", "侯官縣": "Xian"}

    def test_the_case_block_describes_this_case_not_the_other_one(self, built):
        c = built["case"]
        assert c["name"] == "gazetteer-probe"
        assert (c["root_kind"], c["branch_kind"]) == ("府", "縣")
        assert list(c["dynasties"]) == ["明代"]
        assert set(c["admin_categories"]) == {"府", "縣"}


class TestTheCsvExportServesASecondCase:
    """Kills `ADMIN_CAT_PLACEHOLDER[u["kind"]]` and the `== "分司"` alt-name test:
    both raised `KeyError` / silently mis-filled for any other vocabulary."""

    @pytest.fixture(scope="class")
    def rows(self, built, tmp_path_factory):
        out = tmp_path_factory.mktemp("gaz-out")
        n_addr, n_edges = csv_export.write_track_b(built, out)
        # csv.reader, not splitlines(): `c_notes` carries real newlines inside a
        # quoted field, so splitting on lines cuts rows in half.
        with (out / "addresses.csv").open(encoding="utf-8-sig", newline="") as fh:
            parsed = list(csv.reader(fh))[1:]
        return n_addr, n_edges, parsed

    def test_it_writes_a_row_per_address_and_an_edge_per_belongs(self, rows):
        n_addr, n_edges, _ = rows
        assert (n_addr, n_edges) == (3, 3)

    def test_the_category_placeholder_names_this_case_categories(self, rows):
        _, _, parsed = rows
        cats = {r[5] for r in parsed}
        assert cats == {"<new:Fu>", "<new:Xian>"}

    def test_only_a_branch_gets_an_alt_name(self, rows):
        _, _, parsed = rows
        by_name = {r[1]: r[3] for r in parsed}
        assert by_name["福州府"] == ""
        assert by_name["福州府閩縣"] == "閩縣"


class TestACaseWithoutOneIsNotForcedToHaveOne:
    """`extra_exports` is the only optional name in the contract, and codex pointed
    out that asserting the attribute is absent proves nothing about the branch in
    `build_dataset.main()` that guards it: every `main()` test used the salt case,
    which has the hook. So these drive `main()` itself, both ways."""

    def test_this_case_declares_no_extra_exports(self):
        """The superseded SQL loader is salt-administration's own. A case that does
        not want it must not have to supply one."""
        assert not hasattr(GazetteerCase, "extra_exports")

    def test_main_completes_for_a_case_with_no_hook(
            self, snapshot_path, tmp_path, monkeypatch, capsys):
        """Kills making the hook mandatory: `case.extra_exports(...)` without the
        `hasattr` guard raises `AttributeError` here and writes no CSV."""
        monkeypatch.setattr(build_dataset, "load_case", lambda name: GazetteerCase)
        xlsx = tmp_path / "unused.xlsx"
        xlsx.write_bytes(b"")           # read_units ignores it
        out = tmp_path / "out"
        rc = build_dataset.main(["--case", "gazetteer-probe", "--xlsx", str(xlsx),
                                 "--snapshot", str(snapshot_path), "--out", str(out)])
        assert rc == 0
        assert (out / "dataset.json").exists()
        assert (out / "addresses.csv").exists()
        assert not list(out.glob("*.sql"))
        assert "case export" not in capsys.readouterr().out

    def test_main_calls_the_hook_for_a_case_that_has_one(
            self, snapshot_path, tmp_path, monkeypatch, capsys):
        """The other direction: a case that supplies one gets it run, and the run
        says so."""
        written = []

        class WithHook(GazetteerCase):
            @staticmethod
            def extra_exports(dataset, out_dir):
                p = out_dir / "extra.txt"
                p.write_text("ok", encoding="utf-8")
                written.append(p)
                return [p]

        monkeypatch.setattr(build_dataset, "load_case", lambda name: WithHook)
        xlsx = tmp_path / "unused.xlsx"
        xlsx.write_bytes(b"")
        out = tmp_path / "out"
        rc = build_dataset.main(["--case", "gazetteer-probe", "--xlsx", str(xlsx),
                                 "--snapshot", str(snapshot_path), "--out", str(out)])
        assert rc == 0
        assert written and written[0].exists()
        assert "extra.txt" in capsys.readouterr().out

    def test_an_unmapped_character_names_the_case_table(self):
        with pytest.raises(KeyError, match="case's PINYIN table"):
            GazetteerCase.pinyin_of("鹽")
