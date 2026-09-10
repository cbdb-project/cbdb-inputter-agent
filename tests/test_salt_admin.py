"""The salt-administration dataset generator (`tools/salt-admin/`).

This generator writes global reference data - `OFFICE_CODES` rows through the API and
`ADDR_CODES`/`ADDR_BELONGS_DATA` rows through a hand-run SQL script. Nothing
downstream re-derives its arithmetic, and the SQL is applied by a human against a
live database, so a wrong interval or a collided variable name lands as data that
looks right.

A review pass mutation-tested an earlier version of this file: 17 separate breakages
of the design's load-bearing rules left every test green. The end-to-end class below
exists because of that - unit tests of `close_interval` cannot tell you that
`SUCCESSIONS` is wired up, that the Ming window is 1643 and not 1644, or that a
blocked unit really is absent from the CSV. Each assertion here names the mutation it
is meant to kill.

Design: `docs/11-salt-administration-design.md`.
"""

import csv
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "tools" / "salt-admin"
sys.path.insert(0, str(TOOLS))

BD = pytest.importorskip("build_dataset", reason="openpyxl not installed")
import salt_data as SD  # noqa: E402

XLSX = Path(r"C:\Users\sudos\Dropbox\cbdb_helpers\Salt Administration - Ning Hao"
            r"\明清六個鹽運使司情況（完整版）.xlsx")
SNAPSHOT = REPO / "data" / "cbdb-sqlite" / "cbdb_20260815.sqlite3"

needs_sources = pytest.mark.skipif(
    not (XLSX.exists() and SNAPSHOT.exists()),
    reason="needs the source spreadsheet and a CBDB snapshot")


# ===========================================================================
# Interval arithmetic - design section 5.1
# ===========================================================================

class TestCloseInterval:
    def test_a_handover_is_decremented_so_the_boundary_year_is_not_shared(self):
        # 明 滄州分司: 滄州 1373-1611 then 靜海 1611-1644. Both periods claiming 1611
        # would put one unit in two places, and duplicate every parent edge.
        assert BD.close_interval(1373, 1611, dyn_hi=1643, successor_years={1611},
                                 cross_year=None) == (1610, "in-region handover")

    def test_a_terminus_keeps_its_year(self):
        # 明 南港分司 1547-1580, 備註 萬曆八年裁. Nothing begins in 1580; decrementing
        # asserts the office was already gone the year it was abolished.
        assert BD.close_interval(1547, 1580, dyn_hi=1643, successor_years=set(),
                                 cross_year=None) == (1580, "terminus")

    def test_the_beiru_verb_does_not_decide_it_the_successor_does(self):
        # 清 黃崎分司 1664-1677 「併入水口分司」 - 水口分司 has run since 1644, so it
        # does not *start* in 1677: terminus despite the 併入.
        assert BD.close_interval(1664, 1677, dyn_hi=1911, successor_years=set(),
                                 cross_year=None)[0] == 1677
        # 清 溫台分司 1644-1685 「裁溫台分司併入寧紹分司，改名寧紹溫台分司」 - 寧紹溫台分司
        # does begin in 1685: handover, identical verb.
        assert BD.close_interval(1644, 1685, dyn_hi=1911, successor_years=set(),
                                 cross_year=1685)[0] == 1684

    def test_b_the_dynasty_terminus_is_clamped_not_decremented(self):
        assert BD.close_interval(1368, 1644, dyn_hi=1643, successor_years=set(),
                                 cross_year=None) == (1643, "dynasty-clamp")
        assert BD.close_interval(1781, 1911, dyn_hi=1911, successor_years=set(),
                                 cross_year=None)[0] == 1911

    def test_a_cross_unit_succession_fires_only_on_its_listed_year(self):
        assert BD.close_interval(1644, 1781, dyn_hi=1911, successor_years=set(),
                                 cross_year=1781)[0] == 1780
        assert BD.close_interval(1644, 1700, dyn_hi=1911, successor_years=set(),
                                 cross_year=1781)[0] == 1700


class TestSuccessorYearCollection:
    """Rule (d) before rule (a), at unit granularity."""

    def _units(self):
        return [
            BD.RawUnit("分司", "兩浙", "寧紹分司", None, None,
                       [("紹興府", 1644, 1793), ("杭州府", 1793, 1685)], None, 12),
            BD.RawUnit("分司", "兩浙", "溫台分司", None, None,
                       [("溫州府", 1644, 1685)], None, 13),
        ]

    def test_a_reversed_unit_donates_nothing_even_when_not_hand_blocked(self):
        """Kills `_valid -> True` and the period-vs-unit granularity slip.

        An earlier version of this test passed `blocked={"寧紹分司"}`, which skips the
        unit before `_valid` is ever consulted - so it stayed green with `_valid`
        hard-wired to True. With an empty blocked set, rule (d) has to do the work.

        The unit is tested ALONE, because the assertion is that it contributes
        nothing at all - not merely that its bad year is dropped. Its well-formed
        first period starts 1644, and 1644 must not appear either.
        """
        reversed_only = [u for u in self._units() if u.name_short == "寧紹分司"]
        years = BD.collect_successor_years(reversed_only, blocked=set())
        assert years["兩浙"] == set(), (
            "a unit with a reversed range contributes NO years, not just the bad one")

    def test_a_valid_neighbour_still_contributes(self):
        years = BD.collect_successor_years(self._units(), blocked=set())
        assert years["兩浙"] == {1644}, "only 溫台分司's start survives"

    def test_a_hand_blocked_unit_also_donates_nothing(self):
        years = BD.collect_successor_years(
            [BD.RawUnit("分司", "兩浙", "嘉松分司", None, None,
                        [("杭州府", 1704, 1911)], None, 11)],
            blocked={"嘉松分司"})
        assert years["兩浙"] == set()


class TestIntersect:
    def test_disjoint_is_none_not_an_empty_range(self):
        assert BD.intersect((1644, 1676), (1781, 1911)) is None

    def test_touching_at_one_year_still_overlaps(self):
        assert BD.intersect((1644, 1677), (1677, 1911)) == (1677, 1677)


# ===========================================================================
# Coordinate comparison and duplicate detection - design section 5.2 rule 4
# ===========================================================================

class TestSamePoint:
    def test_tolerance_is_a_distance_not_a_rounding_grid(self):
        # The real 明 通州 pair: 2e-6 apart but either side of a 5-dp boundary.
        a, b = (120.85464478, 32.010471344), (120.854645, 32.010471)
        assert round(a[0], 5) != round(b[0], 5)
        assert SD.same_point(*a, *b)

    def test_genuinely_different_places_are_not_the_same_point(self):
        assert not SD.same_point(119.26123047, 33.766372681,
                                 124.38265991, 40.134700775)

    def test_two_missing_coordinates_are_the_same_absence(self):
        assert SD.same_point(None, None, None, None)
        assert not SD.same_point(None, None, 119.0, 33.0)


def _row(**kw):
    """A stand-in sqlite3.Row for the duplicate test."""
    base = dict(c_addr_id=1, x_coord=1.0, y_coord=2.0, c_firstyear=1644,
                c_lastyear=1911, c_admin_type="Xian")
    base.update(kw)
    return base


class TestSameRowTwice:
    def test_identical_point_period_and_type_is_a_duplicate(self):
        assert BD._same_row_twice(_row(c_addr_id=4631), _row(c_addr_id=4632))

    def test_same_point_different_admin_type_is_not_a_duplicate(self):
        """Kills `if all(_same_row_twice(...))` -> `if True`.

        Real case: 清 天津 `7242` (Xian, 1644-1911) and `700000` (Wei, 1644-1910) sit
        on one point. Calling those a duplicate and taking the lower id makes the
        天津縣/天津衛 choice (design 3.11) by accident, while reporting it as CBDB
        having entered one row twice.
        """
        assert not BD._same_row_twice(
            _row(c_addr_id=7242, c_admin_type="Xian", c_lastyear=1911),
            _row(c_addr_id=700000, c_admin_type="Wei", c_lastyear=1910))

    def test_same_point_and_type_but_different_period_is_not_a_duplicate(self):
        assert not BD._same_row_twice(_row(c_lastyear=1903), _row(c_lastyear=1911))


# ===========================================================================
# SQL emission
# ===========================================================================

class TestSqlVars:
    def test_keys_differing_only_in_chinese_get_different_variables(self):
        """Regression: transliterating the key collapsed three regions onto one var."""
        v = BD.SqlVars()
        names = {v.assign(f"salt:qing:{r}:{r}都轉運鹽使司@1644")
                 for r in ("河東", "山東", "福建", "兩淮", "兩浙", "長蘆")}
        assert len(names) == 6

    def test_assigning_the_same_key_twice_is_refused(self):
        v = BD.SqlVars()
        v.assign("salt:ming:兩淮:泰州分司@1368")
        with pytest.raises(ValueError, match="duplicate address key"):
            v.assign("salt:ming:兩淮:泰州分司@1368")

    def test_referencing_an_unassigned_parent_is_refused(self):
        # An unbound @variable evaluates to NULL in MySQL, so this must not be
        # allowed to reach the file.
        with pytest.raises(KeyError, match="never assigned"):
            BD.SqlVars().get("salt:ming:兩淮:兩淮都轉運鹽使司@1368")

    def test_names_are_legal_mysql_user_variables(self):
        name = BD.SqlVars().assign("salt:qing:長蘆:滄州分司@1644")
        assert name.startswith("@") and name[1:].isalnum()


class TestSqlStr:
    def test_a_quote_is_doubled(self):
        assert BD._sql_str("Huai'an") == "'Huai''an'"

    def test_a_newline_stays_a_real_newline(self):
        """Kills "drop the backslash escape" and pins the sql_mode reasoning.

        Emitting `\\n` relies on MySQL expanding backslash escapes, which
        `sql_mode=NO_BACKSLASH_ESCAPES` turns off - silently writing a literal
        backslash-n into every c_notes instead of failing.
        """
        out = BD._sql_str("治所 泰州\n出處：明清方志")
        assert "\n" in out
        assert "\\n" not in out

    def test_a_backslash_is_refused_rather_than_guessed(self):
        with pytest.raises(ValueError, match="sql_mode"):
            BD._sql_str(r"C:\path")

    def test_empty_and_none_are_null_not_an_empty_string(self):
        assert BD._sql_str(None) == "NULL"
        assert BD._sql_str("") == "NULL"


# ===========================================================================
# Naming
# ===========================================================================

class TestNaming:
    def test_a_branch_name_is_qualified_by_its_commission(self):
        assert BD.qualified_name("分司", "兩淮都轉運鹽使司", "泰州分司") \
            == "兩淮都轉運鹽使司泰州分司"

    def test_a_commission_keeps_its_own_name(self):
        assert BD.qualified_name("運司", "兩淮都轉運鹽使司", "兩淮都轉運鹽使司") \
            == "兩淮都轉運鹽使司"

    def test_name_alt_is_null_on_a_commission_never_a_copy(self):
        """Kills `name_alt -> always name_short`.

        A 運司's qualified name IS its short name; repeating it would make
        c_office_chn_alt look like an attested variant when it is not.
        """
        assert BD.name_alt("運司", "兩淮都轉運鹽使司") is None
        assert BD.name_alt("分司", "泰州分司") == "泰州分司"

    def test_region_is_stripped_from_the_commission_name(self):
        assert BD._region_of("兩淮都轉運鹽使司") == "兩淮"
        assert BD._region_of("北平河間都轉運鹽使司") == "北平河間"

    def test_branch_is_stripped_from_the_branch_name(self):
        assert BD._branch_of("泰州分司") == "泰州"
        assert BD._branch_of("東分司") == "東"

    def test_the_qing_region_node_is_added_on_top_of_the_generic_one(self):
        """Kills "drop the Qing region node"."""
        assert BD.type_ids_for("運司", "清代", "兩淮") == ["20070402", "20070405"]
        assert BD.type_ids_for("分司", "清代", "兩淮") == ["20070402"], \
            "a 分司 is not the commissioner, so it gets only the bureau node"
        assert BD.type_ids_for("運司", "清代", "河東") == ["20070402"], \
            "河東 has no named node"

    def test_ming_uses_the_ming_node_alone(self):
        """Kills `MING_TYPE_ID -> anything else` and the bare-dynasty-node revert."""
        assert BD.type_ids_for("運司", "明代", "兩淮") == ["19072801"]

    def test_translation_names_the_region_and_the_branch(self):
        assert BD.translation("分司", "兩淮", "淮安分司") == \
            "Lianghuai Salt Distribution Commission, Huai'an Branch Office"
        assert BD.translation("運司", "長蘆", "長蘆都轉運鹽使司") == \
            "Changlu Salt Distribution Commission"


class TestRomanization:
    def test_the_address_name_is_one_token_per_name_part(self):
        """Kills `romanize_compact` over the whole name.

        That produced `Lianghuaiduzhuanyunyanshisitaizhoufensi` on all 55 rows -
        neither the four-token form the design chose nor anything ADDR_CODES holds.
        """
        assert SD.address_romanization("兩淮", "分司", "泰州") == \
            "Lianghuai Duzhuanyunyanshisi Taizhou Fensi"
        assert SD.address_romanization("兩淮", "運司", None) == \
            "Lianghuai Duzhuanyunyanshisi"

    def test_an_unmapped_character_raises_rather_than_being_dropped(self):
        with pytest.raises(KeyError):
            SD.pinyin_of("兩淮都轉運鹽使司龍")

    def test_the_office_pinyin_is_space_separated_lowercase(self):
        assert SD.pinyin_of("兩淮都轉運鹽使司") == "liang huai du zhuan yun yan shi si"

    def test_the_compact_form_capitalizes_once(self):
        assert SD.romanize_compact("分司") == "Fensi"


class TestCuratedTablesAreWellFormed:
    def test_every_table_key_names_a_known_dynasty(self):
        for table in (SD.SEAT_BOXES, SD.SUCCESSIONS, SD.BLOCKED, SD.SEAT_DECISIONS):
            for dynasty, _ in table:
                assert dynasty in SD.DYNASTIES

    def test_the_source_code_is_the_unknown_sentinel(self):
        """Kills `SOURCE_ID -> a real c_textid`. Decided by the user 2026-09-10."""
        assert SD.SOURCE_ID == 0

    def test_the_windows_follow_addr_codes_not_dynasties(self):
        """Kills `Ming window -> (1368, 1644)`.

        DYNASTIES says Ming ends 1644, but ADDR_CODES ends Ming rows at 1643 and
        starts Qing at 1644; the looser window makes 18 of 19 Ming seats ambiguous.
        """
        assert SD.DYNASTIES["明代"]["window"] == (1368, 1643)
        assert SD.DYNASTIES["清代"]["window"] == (1644, 1911)

    def test_the_dynasty_roots_are_the_right_addresses(self):
        assert SD.DYNASTIES["明代"]["root_addr_id"] == 4329
        assert SD.DYNASTIES["清代"]["root_addr_id"] == 6756


# ===========================================================================
# Seat resolution, against a miniature snapshot
# ===========================================================================

@pytest.fixture
def mini_snapshot(tmp_path):
    path = tmp_path / "mini.sqlite3"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE ADDR_CODES (
            c_addr_id INT, c_name TEXT, c_name_chn TEXT, c_firstyear INT,
            c_lastyear INT, c_admin_type TEXT, c_admin_cat_code INT,
            x_coord REAL, y_coord REAL, CHGIS_PT_ID INT, c_notes TEXT,
            c_alt_names TEXT);
        CREATE TABLE OFFICE_CODES (
            c_office_id INT, c_dy INT, c_office_chn TEXT, c_office_chn_alt TEXT);
        CREATE TABLE OFFICE_TYPE_TREE (
            c_office_type_node_id TEXT, c_office_type_desc TEXT,
            c_office_type_desc_chn TEXT, c_parent_id TEXT);
        """)
    con.executemany(
        "INSERT INTO ADDR_CODES (c_addr_id, c_name_chn, c_name, c_admin_type, "
        "c_firstyear, c_lastyear, x_coord, y_coord) VALUES (?,?,?,?,?,?,?,?)",
        [
            (0, "[未詳]", "[Unknown]", "[Unknown]", None, None, None, None),
            (4393, "通州", "Tong Zhou", "Zhou", 1368, 1643, 116.65880585, 39.905921936),
            (4634, "通州", "Tong Zhou", "Zhou", 1368, 1643, 120.85464478, 32.010471344),
            (4635, "通州", "Tong Zhou", "Zhou", 1368, 1643, 120.854645, 32.010471),
            (5420, "溫州府", "Wenzhou Fu", "Fu", 1368, 1643, 120.65322113, 28.018291473),
            (6794, "安東", "Andong", "Xian", 1644, 1911, 124.38265991, 40.134700775),
            (7583, "安東", "Andong", "Xian", 1644, 1911, 119.26123047, 33.766372681),
            (7242, "天津", "Tianjin", "Xian", 1644, 1911, 117.18782043, 39.13697052),
            (700000, "天津", "Tianjin Wei", "Wei", 1644, 1910, 117.18782043, 39.13697052),
            (700058, "淮揚巡撫", "huaiyang xunfu", "Xunfu", 1368, 1643, 0.0, 0.0),
            # Two 石城 in the Ming window, different places, with NO registered box:
            # the "must refuse" path.
            (9001, "石城", "Shicheng A", "Xian", 1368, 1643, 116.0, 26.0),
            (9002, "石城", "Shicheng B", "Xian", 1368, 1643, 110.0, 21.0),
        ])
    # For the advisory-scan tests. The second row's c_office_chn_alt is deliberately
    # non-NULL: `LIKE` against NULL never matches, so a fixture of all-NULL aliases
    # cannot detect a dropped escape.
    con.executemany(
        "INSERT INTO OFFICE_CODES (c_office_id, c_dy, c_office_chn, c_office_chn_alt)"
        " VALUES (?,?,?,?)",
        [(999, 19, "泰州分司", None),
         (998, 19, "某某官", "別名甲;別名乙")])
    con.commit()
    con.close()
    with BD.Snapshot(path) as s:
        yield s


class TestResolveSeat:
    def test_a_listed_variant_is_applied_after_an_exact_miss(self, mini_snapshot):
        f = BD.Findings()
        r = BD.resolve_seat(mini_snapshot, "明代", "温州府", f, "u")   # simplified 温
        assert r.addr_id == 5420 and "variant" in r.rule

    def test_the_coordinate_box_picks_the_right_of_two_real_places(self, mini_snapshot):
        f = BD.Findings()
        r = BD.resolve_seat(mini_snapshot, "清代", "安東", f, "u")
        assert r.addr_id == 7583, "淮安分司's 安東 is in Jiangsu, not Liaoning"

    def test_the_box_is_consulted_even_when_only_one_row_matches(self, tmp_path):
        """A box is registered because the NAME is dangerous.

        Skipping it on `len(rows) == 1` means that if CBDB ever retires the Jiangsu
        安東, 淮安分司 resolves silently to Dandong with nothing left to catch it.
        """
        path = tmp_path / "one.sqlite3"
        con = sqlite3.connect(path)
        con.executescript(
            "CREATE TABLE ADDR_CODES (c_addr_id INT, c_name TEXT, c_name_chn TEXT,"
            " c_firstyear INT, c_lastyear INT, c_admin_type TEXT,"
            " c_admin_cat_code INT, x_coord REAL, y_coord REAL, CHGIS_PT_ID INT,"
            " c_notes TEXT, c_alt_names TEXT);")
        con.execute("INSERT INTO ADDR_CODES (c_addr_id, c_name_chn, c_admin_type,"
                    " c_firstyear, c_lastyear, x_coord, y_coord)"
                    " VALUES (6794, '安東', 'Xian', 1644, 1911, 124.38, 40.13)")
        con.commit()
        con.close()
        f = BD.Findings()
        with BD.Snapshot(path) as snap:
            r = BD.resolve_seat(snap, "清代", "安東", f, "u")
        assert r.addr_id is None
        assert f.count("blocker") == 1

    def test_duplicate_rows_tiebreak_only_after_the_box_narrows_them(self, mini_snapshot):
        f = BD.Findings()
        r = BD.resolve_seat(mini_snapshot, "明代", "通州", f, "u")
        assert r.addr_id == 4634 and r.duplicates == [4635]

    def test_two_real_places_with_no_rule_are_refused_not_guessed(self, mini_snapshot):
        """The branch the whole scheme exists for, and previously untested.

        Two 石城 rows, different places, no coordinate box. Taking the lowest id here
        is exactly the 900 km 安東 error; the generator must stop instead.
        """
        f = BD.Findings()
        r = BD.resolve_seat(mini_snapshot, "明代", "石城", f, "u")
        assert r.addr_id is None
        assert r.rule == "ambiguous"
        assert f.count("blocker") == 1

    def test_a_recorded_decision_settles_candidates_that_are_not_duplicates(
            self, mini_snapshot):
        # 清 天津: same point, different admin_type and span. Not a duplicate, so it
        # needs the explicit SEAT_DECISIONS entry rather than an id sort.
        f = BD.Findings()
        r = BD.resolve_seat(mini_snapshot, "清代", "天津", f, "u")
        assert r.addr_id == 7242
        assert f.count("blocker") == 0
        assert f.count("warning") == 1, "the decision is surfaced, not silent"

    def test_an_unresolvable_name_is_a_blocker_not_a_guess(self, mini_snapshot):
        f = BD.Findings()
        r = BD.resolve_seat(mini_snapshot, "明代", "運城", f, "u")
        assert r.addr_id is None and f.count("blocker") == 1

    def test_weizhi_resolves_to_the_sentinel_row_not_to_nothing(self, mini_snapshot):
        f = BD.Findings()
        r = BD.resolve_seat(mini_snapshot, "明代", "未詳", f, "u")
        assert r.addr_id == SD.UNKNOWN_ADDR_ID and f.count("blocker") == 0

    def test_the_zero_zero_placeholder_is_not_treated_as_a_coordinate(self, mini_snapshot):
        # 0.0/0.0 is a point in the Gulf of Guinea. Six CBDB jurisdiction rows carry it.
        f = BD.Findings()
        r = BD.resolve_seat(mini_snapshot, "明代", "淮揚巡撫", f, "u")
        assert r.addr_id == 700058 and (r.x, r.y) == (None, None)
        assert f.count("warning") == 1


class TestReadSheetRefusesAmbiguousShapes:
    def test_a_branch_before_any_commission_raises(self, tmp_path):
        """Kills "region carries forward from None".

        The grouping is positional; a 分司 with no 運司 above it would otherwise be
        named 泰州分司泰州分司 and translated "None Salt Distribution Commission".
        """
        openpyxl = pytest.importorskip("openpyxl")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "明代"
        ws.append(["明代", "鹽運總司", "分司", "起", "止", "治所", "起", "止"])
        ws.append([None, None, "泰州分司", None, None, "泰州", 1368, 1644])
        p = tmp_path / "bad.xlsx"
        wb.save(p)
        with pytest.raises(BD.SheetError, match="before any"):
            BD.read_sheet(p, "明代")


# ===========================================================================
# End to end, against the real spreadsheet and snapshot
# ===========================================================================

@pytest.fixture(scope="module")
def dataset():
    if not (XLSX.exists() and SNAPSHOT.exists()):
        pytest.skip("needs the source spreadsheet and a CBDB snapshot")
    with BD.Snapshot(SNAPSHOT) as snap:
        return BD.build(XLSX, snap)


def unit(dataset, dynasty, short):
    for u in dataset["units"]:
        if u["dynasty"] == dynasty and u["name_short"] == short:
            return u
    raise AssertionError(f"{dynasty} {short} not in the dataset")


@needs_sources
class TestEndToEnd:
    def test_the_counts_are_what_the_design_promises(self, dataset):
        s = dataset["stats"]
        assert s["units_total"] == 51
        assert s["office_creates"] == 49
        assert s["address_rows"] == 55
        assert s["belongs_edges"] == 57

    def test_the_header_row_is_not_read_as_a_unit(self, dataset):
        """Kills "stop skipping the header row"."""
        assert not any(u["name_short"] in ("鹽運總司", "分司") for u in dataset["units"])

    def test_the_two_source_defects_are_excluded_from_every_output(self, dataset):
        """Kills `BLOCKED = {}`."""
        for short in ("寧紹分司", "嘉松分司"):
            assert not unit(dataset, "清代", short)["emitted"]
        assert {u["name_short"] for u in dataset["units"] if not u["emitted"]} \
            == {"寧紹分司", "嘉松分司"}

    def test_a_cross_unit_succession_is_applied(self, dataset):
        """Kills `SUCCESSIONS = {}`."""
        huai_an = unit(dataset, "清代", "淮安分司")
        assert huai_an["addresses"][0]["last"] == 1762, \
            "海州分司 begins 1763, so 淮安分司 must end 1762"
        qingzhou = unit(dataset, "清代", "青州分司")
        assert qingzhou["addresses"][0]["last"] == 1780, \
            "天津分司 begins 1781 - 「青州分司改稱天津分司」"

    def test_a_terminus_is_not_decremented(self, dataset):
        assert unit(dataset, "明代", "南港分司")["addresses"][0]["last"] == 1580
        assert unit(dataset, "清代", "黃崎分司")["addresses"][0]["last"] == 1677

    def test_the_ming_window_resolves_ming_rows_not_qing_ones(self, dataset):
        """Kills `Ming window -> 1644`, which admits the whole Qing block."""
        assert unit(dataset, "明代", "兩淮都轉運鹽使司")["addresses"][0]["seat"]["addr_id"] \
            == 4622, "揚州府 4622 is the Ming row; 7569 is the Qing one"

    def test_seat_moves_split_into_two_non_overlapping_rows(self, dataset):
        cang = unit(dataset, "明代", "滄州分司")["addresses"]
        assert [(a["first"], a["last"]) for a in cang] == [(1373, 1610), (1611, 1643)]
        assert [a["seat"]["raw"] for a in cang] == ["滄州", "靜海"]

    def test_no_unit_has_overlapping_seat_periods(self, dataset):
        for u in dataset["units"]:
            spans = [(a["first"], a["last"]) for a in u["addresses"]]
            for (a1, b1), (a2, _) in zip(spans, spans[1:]):
                assert b1 < a2, f"{u['name']} overlaps at {b1}"

    def test_every_commission_row_hangs_off_its_dynasty(self, dataset):
        """Kills "drop the 運司 -> dynasty root edge"."""
        for u in dataset["units"]:
            if u["kind"] != "運司" or not u["emitted"]:
                continue
            for a in u["addresses"]:
                roots = [b["parent_addr_id"] for b in a["belongs"]]
                assert roots == [SD.DYNASTIES[u["dynasty"]]["root_addr_id"]]

    def test_every_branch_row_sits_inside_a_real_parent_period(self, dataset):
        by_key = {a["key"]: a for u in dataset["units"] for a in u["addresses"]}
        for u in dataset["units"]:
            if u["kind"] != "分司" or not u["emitted"]:
                continue
            for a in u["addresses"]:
                assert a["belongs"], f"{u['name']} {a['key']} is an orphan"
                for b in a["belongs"]:
                    p = by_key[b["parent_key"]]
                    assert p["first"] <= b["first"] and b["last"] <= p["last"]
                    assert a["first"] <= b["first"] and b["last"] <= a["last"]

    def test_a_branch_is_clamped_to_its_parents_start(self, dataset):
        """明 濱樂/膠萊 take 濟南府 1368 from the sheet; 山東運司 starts 1369."""
        for short in ("濱樂分司", "膠萊分司"):
            assert unit(dataset, "明代", short)["addresses"][0]["first"] == 1369

    def test_the_seatless_commission_takes_its_own_span_and_the_sentinel(self, dataset):
        u = unit(dataset, "明代", "北平河間都轉運鹽使司")
        (a,) = u["addresses"]
        assert (a["first"], a["last"]) == (1369, 1372), "長蘆 succeeds it in 1373"
        assert a["seat"]["addr_id"] == SD.UNKNOWN_ADDR_ID
        assert a["x"] is None

    def test_the_source_note_is_on_every_row_of_both_tracks(self, dataset):
        for u in dataset["units"]:
            assert SD.SOURCE_NOTE in u["office"]["notes"]
            for a in u["addresses"]:
                assert SD.SOURCE_NOTE in a["notes"]

    def test_a_resolved_seat_always_records_its_addr_id_in_the_note(self, dataset):
        for u in dataset["units"]:
            for a in u["addresses"]:
                if a["seat"]["addr_id"] is not None:
                    assert f"ADDR_CODES {a['seat']['addr_id']}" in a["notes"] \
                        or a["seat"]["raw"] == "未詳"

    def test_the_coincidence_count_is_reported(self, dataset):
        groups = dataset["coincident_points"]
        assert len(groups) == 9
        biggest = max(g["count"] for g in groups)
        assert biggest == 4, "明/清 河東 put four rows on 安邑"

    def test_no_blocker_finding_lands_on_an_emitted_unit(self, dataset):
        assert BD.unexpected_blockers(dataset) == []


@needs_sources
class TestExports:
    @pytest.fixture(scope="class")
    def written(self, tmp_path_factory):
        out = tmp_path_factory.mktemp("salt")
        with BD.Snapshot(SNAPSHOT) as snap:
            ds = BD.build(XLSX, snap)
        BD.write_track_b(ds, out)
        sql = BD.write_track_b_sql(ds, out)
        return ds, out, sql.read_text(encoding="utf-8")

    def test_the_csv_carries_one_row_per_address(self, written):
        ds, out, _ = written
        rows = list(csv.DictReader(
            (out / "addresses.csv").open(encoding="utf-8-sig")))
        assert len(rows) == ds["stats"]["address_rows"]

    def test_the_csv_never_leaves_the_not_null_category_column_blank(self, written):
        _, out, _ = written
        for r in csv.DictReader((out / "addresses.csv").open(encoding="utf-8-sig")):
            assert r["c_admin_cat_code"], "SMALLINT NOT NULL with an FK"

    def test_the_csv_romanization_is_the_four_token_form(self, written):
        _, out, _ = written
        rows = list(csv.DictReader((out / "addresses.csv").open(encoding="utf-8-sig")))
        names = {r["c_name"] for r in rows}
        assert "Lianghuai Duzhuanyunyanshisi Taizhou Fensi" in names
        assert not any(len(n.split()) == 1 for n in names)

    def test_blocked_units_reach_no_export(self, written):
        # By symbolic key, not by bare name: 明代 寧紹分司 is a perfectly good unit and
        # IS exported. Only the 清代 one is blocked, and a substring search for the
        # name cannot tell them apart.
        _, out, sql = written
        addresses = (out / "addresses.csv").read_text(encoding="utf-8-sig")
        belongs = (out / "addr_belongs.csv").read_text(encoding="utf-8-sig")
        for key in ("salt:qing:兩浙:寧紹分司", "salt:qing:兩浙:嘉松分司"):
            for blob, where in ((addresses, "addresses.csv"),
                                (belongs, "addr_belongs.csv"), (sql, "the SQL")):
                assert key not in blob, f"{key} leaked into {where}"
        assert "salt:ming:兩浙:寧紹分司" in addresses,             "the Ming unit of the same name must still be exported"

    def test_every_address_row_gets_its_own_sql_variable(self, written):
        """The real-data version of the SqlVars regression."""
        import re
        ds, _, sql = written
        assigned = re.findall(r"^SET (@a\d+) :=", sql, re.M)
        assert len(assigned) == ds["stats"]["address_rows"]
        assert len(set(assigned)) == len(assigned)

    def test_every_referenced_variable_is_bound_before_use(self, written):
        import re
        _, _, sql = written
        bound, seen_error = set(), []
        for line in sql.splitlines():
            m = re.match(r"^SET (@a\d+) :=", line)
            if m:
                bound.add(m.group(1))
                continue
            if line.startswith("INSERT INTO ADDR_BELONGS_DATA"):
                for ref in re.findall(r"@a\d+", line):
                    if ref not in bound:
                        seen_error.append(ref)
        assert not seen_error

    def test_the_script_declares_its_charset(self, written):
        # Every name and note is Chinese; a latin1 client would commit mojibake.
        _, _, sql = written
        assert "SET NAMES utf8mb4" in sql

    def test_the_script_does_not_depend_on_backslash_escapes(self, written):
        _, _, sql = written
        assert "\\n" not in sql

    def test_the_verification_counts_are_scoped_to_this_run(self, written):
        # Scoped to the exact allocated range AND to this dataset's c_admin_type.
        # `> @addr_base` alone still reads 55 when one of our rows failed on a PK
        # collision and a concurrent session's row filled the gap.
        _, _, sql = written
        flat = " ".join(sql.split())
        assert "BETWEEN @addr_base + 1 AND @addr_base + 55" in flat
        assert ("AND c_admin_type IN ('Duzhuanyunyanshisi','Fensi')) "
                "AS actual_addresses") in flat
        assert "c_addr_id > @addr_base)" not in flat, "the unscoped form must be gone"
        assert "NOT EXISTS" in flat, "an orphan check must be present"
        assert "NOT IN ('Duzhuanyunyanshisi','Fensi')" in flat, \
            "nothing foreign may have landed inside the allocated range"


    def test_the_isolation_level_the_gap_lock_needs_is_pinned(self, written):
        # The id allocation relies on a next-key lock, which InnoDB only takes under
        # REPEATABLE READ. On a READ COMMITTED server there is no gap lock and a
        # concurrent session can land inside the allocated range.
        _, _, sql = written
        assert "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ;" in sql

    def test_the_category_table_is_locked_before_it_is_read(self, written):
        # c_admin_cat_py has no unique key, so an unlocked read-then-insert lets two
        # sessions both find the name absent and both add it - two rows, no error.
        _, _, sql = written
        assert "SELECT COUNT(*) INTO @cat_rows FROM ADMIN_CAT_CODES FOR UPDATE;" in sql

    def test_the_category_codes_are_read_back_by_name(self, written):
        # Not COALESCE'd from the arithmetic: read what actually landed.
        _, _, sql = written
        assert "COALESCE(@cat_yunsi" not in sql
        assert "COALESCE(@cat_fensi" not in sql
        after = sql.split("Re-read by NAME", 1)
        assert len(after) == 2
        assert after[1].count("SELECT c_admin_cat_code FROM ADMIN_CAT_CODES") == 2

    def test_the_categories_are_verified_too(self, written):
        _, _, sql = written
        flat = " ".join(sql.split())
        assert ("SELECT c_admin_cat_py, COUNT(*) AS n FROM ADMIN_CAT_CODES "
                "WHERE c_admin_cat_py IN ('Duzhuanyunyanshisi','Fensi')") in flat

    def test_a_repeat_load_aborts_rather_than_reporting(self, written):
        """The only guard against running the script twice, and it must ERROR.

        Every check in the verification section is scoped to the range THIS run
        allocated, so a second run inserts a complete duplicate set under fresh ids
        and all seven still report success. A `SELECT COUNT(*)` would print a number
        the operator can scroll past; the scalar subquery below matches 2+ rows when
        the dataset is present and MySQL fails the statement with 1242.
        """
        _, _, sql = written
        flat = " ".join(sql.split())
        assert ("SELECT (SELECT 1 FROM ADDR_CODES "
                "WHERE c_admin_type IN ('Duzhuanyunyanshisi','Fensi') "
                "UNION ALL SELECT 1) AS guard_dataset_not_already_loaded;") in flat

    def test_the_guard_runs_after_the_lock_and_before_any_insert(self, written):
        """Order is the whole point.

        Before the lock, two concurrent runs both see an empty table and both
        proceed. After it, the second blocks until the first commits and then sees
        its rows.
        """
        _, _, sql = written
        lock = sql.index("ORDER BY c_addr_id DESC LIMIT 1 FOR UPDATE")
        guard = sql.index("guard_dataset_not_already_loaded")
        first_insert = sql.index("INSERT INTO ADDR_CODES")
        assert lock < guard < first_insert

    def test_the_previous_isolation_level_is_reported(self, written):
        # SET SESSION outlives the transaction and cannot be restored from a
        # variable, so the script at least tells the operator what it replaced.
        _, _, sql = written
        assert "isolation_level_before_this_script" in sql

    def test_commit_is_left_commented_out(self, written):
        _, _, sql = written
        assert "-- COMMIT;" in sql
        assert "\nCOMMIT;" not in sql

    def test_the_admin_cat_section_can_be_switched_off(self, written):
        ds, out, _ = written
        ds2 = dict(ds, admin_cat_mode="zero")
        sql = BD.write_track_b_sql(ds2, out).read_text(encoding="utf-8")
        assert "INSERT INTO ADMIN_CAT_CODES" not in sql
        assert "SET @cat_yunsi := 0;" in sql


# ===========================================================================
# Gaps a second review round found: mutations that left the suite green
# ===========================================================================

def _fixture_workbook(tmp_path, ming_rows, qing_rows=()):
    """A minimal two-sheet workbook in the source's column layout."""
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    for i, (title, rows) in enumerate((("明代", ming_rows), ("清代", qing_rows))):
        ws = wb.active if i == 0 else wb.create_sheet()
        ws.title = title
        ws.append([title, "鹽運總司", "分司", "起", "止", "治所", "起", "止",
                   "新治所", "起", "止", "備註"])
        for r in rows:
            ws.append(list(r) + [None] * (12 - len(r)))
    p = tmp_path / "fixture.xlsx"
    wb.save(p)
    return p


class TestBlockerExclusionEndToEnd:
    """The mechanism itself, which no test previously exercised.

    Two mutations survived the earlier suite: dropping the `blocked_keys` term from
    `emitted`, and stubbing `unexpected_blockers()` to return []. Both leave a unit
    with an unresolvable 治所 in the CSVs and the SQL, with the run exiting 0.
    """

    @pytest.fixture
    def broken(self, tmp_path, mini_snapshot):
        xlsx = _fixture_workbook(tmp_path, [
            [None, "兩淮都轉運鹽使司", None, 1368, 1644, "通州", 1368, 1644],
            # 治所 that resolves to nothing in the mini snapshot:
            [None, None, "泰州分司", None, None, "無此地", 1368, 1644],
        ])
        return BD.build(xlsx, mini_snapshot)

    def test_the_unit_is_not_emitted(self, broken):
        bad = [u for u in broken["units"] if u["name_short"] == "泰州分司"][0]
        assert bad["emitted"] is False
        assert bad["blocked"] is False, "not hand-blocked - excluded by a finding"

    def test_the_run_reports_it_as_unexpected(self, broken):
        assert BD.unexpected_blockers(broken) == ["salt:ming:兩淮:泰州分司"]

    def test_it_reaches_neither_csv_nor_the_sql(self, broken, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        BD.write_track_b(broken, out)
        sql = BD.write_track_b_sql(broken, out).read_text(encoding="utf-8")
        for blob in ((out / "addresses.csv").read_text(encoding="utf-8-sig"),
                     (out / "addr_belongs.csv").read_text(encoding="utf-8-sig"), sql):
            assert "泰州分司" not in blob

    def test_main_exits_non_zero(self, broken, tmp_path, monkeypatch, capsys):
        xlsx = _fixture_workbook(tmp_path, [
            [None, "兩淮都轉運鹽使司", None, 1368, 1644, "通州", 1368, 1644],
            [None, None, "泰州分司", None, None, "無此地", 1368, 1644],
        ])
        rc = BD.main(["--xlsx", str(xlsx), "--snapshot", str(mini_snapshot_path(tmp_path)),
                      "--out", str(tmp_path / "o")])
        assert rc == 1
        assert "No CSV and no SQL written" in capsys.readouterr().err


def mini_snapshot_path(tmp_path):
    """The same fixture rows as `mini_snapshot`, as a file path for main()."""
    path = tmp_path / "mini-for-main.sqlite3"
    if path.exists():
        return path
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE ADDR_CODES (
            c_addr_id INT, c_name TEXT, c_name_chn TEXT, c_firstyear INT,
            c_lastyear INT, c_admin_type TEXT, c_admin_cat_code INT,
            x_coord REAL, y_coord REAL, CHGIS_PT_ID INT, c_notes TEXT,
            c_alt_names TEXT);
        CREATE TABLE OFFICE_CODES (
            c_office_id INT, c_dy INT, c_office_chn TEXT, c_office_chn_alt TEXT);
        CREATE TABLE OFFICE_TYPE_TREE (
            c_office_type_node_id TEXT, c_office_type_desc TEXT,
            c_office_type_desc_chn TEXT, c_parent_id TEXT);
        """)
    con.execute("INSERT INTO ADDR_CODES (c_addr_id, c_name_chn, c_admin_type,"
                " c_firstyear, c_lastyear, x_coord, y_coord)"
                " VALUES (4634, '通州', 'Zhou', 1368, 1643, 120.85464478, 32.010471344)")
    con.commit()
    con.close()
    return path


class TestDanglingParentsAreRefused:
    def test_an_edge_to_a_non_emitted_row_stops_the_export(self):
        """Serious: `_attach_belongs` used to pick parents by hand-blocking only.

        A 運司 excluded by a finding was still used as a parent, so both CSVs landed
        referencing a symbolic key the SQL never binds - and only the SQL raised,
        after the CSVs were already on disk.
        """
        ds = {
            "units": [
                {"key": "p", "emitted": False, "kind": "運司",
                 "addresses": [{"key": "p@1", "belongs": []}]},
                {"key": "c", "emitted": True, "kind": "分司",
                 "addresses": [{"key": "c@1", "belongs": [
                     {"parent_key": "p@1", "parent_addr_id": None,
                      "parent_name": "x", "first": 1, "last": 2, "source": 0}]}]},
            ],
        }
        with pytest.raises(ValueError, match="not emitted"):
            BD.assert_exportable(ds)


@needs_sources
class TestSqlEdgeDirection:
    """Swapping child and parent in every INSERT survived the earlier suite.

    Every hierarchy edge reversed, in a script a human runs against the live
    database - and the script's own verification only catches the 運司 edges.
    """

    @pytest.fixture(scope="class")
    def parsed(self, tmp_path_factory):
        import re
        out = tmp_path_factory.mktemp("sqldir")
        with BD.Snapshot(SNAPSHOT) as snap:
            ds = BD.build(XLSX, snap)
        sql = BD.write_track_b_sql(ds, out).read_text(encoding="utf-8")
        var_of = dict(re.findall(r"^-- (\S+)  \(.*\n^SET (@a\d+) :=", sql, re.M))
        edges = re.findall(
            r"INSERT INTO ADDR_BELONGS_DATA .*? VALUES \((@a\d+|\d+), (@a\d+|\d+),"
            r" (\d+), (\d+),", sql)
        return ds, var_of, edges

    def test_every_edge_names_the_child_first(self, parsed):
        ds, var_of, edges = parsed
        expected = []
        for u in ds["units"]:
            if not u["emitted"]:
                continue
            for a in u["addresses"]:
                for b in a["belongs"]:
                    parent = (str(b["parent_addr_id"]) if b["parent_addr_id"] is not None
                              else var_of[b["parent_key"]])
                    expected.append((var_of[a["key"]], parent,
                                     str(b["first"]), str(b["last"])))
        assert edges == expected

    def test_a_commission_edge_points_at_the_dynasty_not_the_other_way(self, parsed):
        ds, var_of, edges = parsed
        roots = {"4329", "6756"}
        for child, parent, _, _ in edges:
            assert child not in roots, "the dynasty must never be the child"
        assert any(p in roots for _, p, _, _ in edges)


@needs_sources
class TestRowShapeDetails:
    """Mutants that were correct in the code but unpinned by any test."""

    def test_chgis_pt_id_is_never_borrowed_from_the_seat(self, dataset, tmp_path):
        # We are copying a coordinate, not claiming to BE that CHGIS point.
        BD.write_track_b(dataset, tmp_path)
        for r in csv.DictReader((tmp_path / "addresses.csv").open(encoding="utf-8-sig")):
            assert r["CHGIS_PT_ID"] == ""

    def test_alt_names_is_empty_on_a_commission(self, dataset, tmp_path):
        BD.write_track_b(dataset, tmp_path)
        rows = list(csv.DictReader(
            (tmp_path / "addresses.csv").open(encoding="utf-8-sig")))
        for r in rows:
            if r["c_admin_type"] == "Duzhuanyunyanshisi":
                assert r["c_alt_names"] == ""
            else:
                assert r["c_alt_names"]

    def test_the_admin_type_matches_the_kind(self, dataset, tmp_path):
        # Swapping these put the wrong c_admin_type on all 55 rows, unnoticed.
        BD.write_track_b(dataset, tmp_path)
        by_key = {a["key"]: u["kind"] for u in dataset["units"] if u["emitted"]
                  for a in u["addresses"]}
        for r in csv.DictReader((tmp_path / "addresses.csv").open(encoding="utf-8-sig")):
            want = "Duzhuanyunyanshisi" if by_key[r["symbolic_key"]] == "運司" else "Fensi"
            assert r["c_admin_type"] == want

    def test_every_belongs_edge_cites_the_unknown_source(self, dataset):
        for u in dataset["units"]:
            for a in u["addresses"]:
                for b in a["belongs"]:
                    assert b["source"] == 0

    def test_pinyin_alt_is_left_for_the_server_to_derive(self, dataset):
        for u in dataset["units"]:
            assert u["office"]["pinyin_alt"] is None
            assert u["office"]["translation_alt"] is None

    def test_the_office_note_lists_every_seat(self, dataset):
        u = unit(dataset, "明代", "滄州分司")
        for a in u["addresses"]:
            assert a["seat"]["raw"] in u["office"]["notes"]
            assert str(a["first"]) in u["office"]["notes"]

    def test_the_two_token_alternative_is_carried_for_the_reviewer(self, dataset):
        # Design 5 Track B promises the reviewer both romanizations, not one.
        u = unit(dataset, "明代", "泰州分司")
        assert u["romanization"] == "Lianghuai Duzhuanyunyanshisi Taizhou Fensi"
        assert u["romanization_alt"] == "Taizhou Fensi"

    def test_a_box_narrowing_is_reported_not_just_recorded(self, dataset):
        # The three decisions the whole scheme exists for must be visible.
        titles = [f["title"] for f in dataset["findings"]]
        assert sum("coordinate box discarded" in t for t in titles) == 3


class TestIntervalGuards:
    def test_closing_below_the_start_is_refused(self):
        # A one-year period whose end is also a successor's start. Downstream this
        # only showed up as an empty intersect and a warning, exiting 0.
        with pytest.raises(BD.IntervalError, match=r"not\s+a period"):
            BD.close_interval(1700, 1700, dyn_hi=1911, successor_years={1700},
                              cross_year=None)

    def test_the_dynasty_clamp_is_checked_before_the_handover(self):
        # Reordering these silently turned 1911 into 1910 for any unit whose end
        # coincided with a neighbour's start.
        assert BD.close_interval(1781, 1911, dyn_hi=1911,
                                 successor_years={1911}, cross_year=None) \
            == (1911, "dynasty-clamp")


class TestUnknownSentinel:
    def test_the_unknown_address_is_zero(self):
        # Both resolve tests compared against the constant, so changing it to 1 was
        # invisible. 0 is CBDB's own [未詳] row.
        assert SD.UNKNOWN_ADDR_ID == 0


class TestAdvisoryScanIsSafe:
    def test_a_wildcard_in_a_name_cannot_widen_the_search(self, mini_snapshot):
        # The name goes into a LIKE; unescaped, `_` matches any character and the
        # advisory panel starts crying wolf.
        rows = mini_snapshot.office_name_matches(["%"], 19)
        assert rows == []

    def test_the_short_name_is_searched_too(self, mini_snapshot):
        # Nothing in CBDB is called 兩淮都轉運鹽使司泰州分司; what could collide is the
        # short form, 泰州分司. Searching only the qualified name reports a
        # reassuring zero for all 49 rows.
        assert mini_snapshot.office_name_matches(
            ["兩淮都轉運鹽使司泰州分司", "泰州分司"], 19)
        assert not mini_snapshot.office_name_matches(["兩淮都轉運鹽使司泰州分司"], 19)


class TestExcludedParentsCannotBeUsed:
    """The real path for the dangling-parent bug, which a synthetic dataset misses.

    No 運司 in the real spreadsheet is excluded by a finding, so the bug is
    unobservable there. Here the 運司's 治所 does not resolve, which excludes it -
    and its 分司 must then be excluded too, not given an edge to a row no export
    contains.
    """

    @pytest.fixture
    def built(self, tmp_path, mini_snapshot):
        xlsx = _fixture_workbook(tmp_path, [
            [None, "兩淮都轉運鹽使司", None, 1368, 1644, "無此地", 1368, 1644],
            [None, None, "泰州分司", None, None, "通州", 1368, 1644],
        ])
        return BD.build(xlsx, mini_snapshot)

    def test_the_commission_is_excluded(self, built):
        yunsi = [u for u in built["units"] if u["kind"] == "運司"][0]
        assert yunsi["emitted"] is False

    def test_its_branch_gets_no_edge_to_it(self, built):
        branch = [u for u in built["units"] if u["kind"] == "分司"][0]
        for a in branch["addresses"]:
            assert a["belongs"] == [], "an edge to an excluded row is worse than none"

    def test_the_branch_is_excluded_too_and_says_why(self, built):
        branch = [u for u in built["units"] if u["kind"] == "分司"][0]
        assert branch["emitted"] is False
        detail = " ".join(f["detail"] for f in built["findings"]
                          if f["unit_key"] == branch["key"])
        assert "itself excluded" in detail

    def test_the_export_would_have_been_refused(self, built, tmp_path):
        # Belt and braces: even if the edges came back, nothing may be written.
        BD.assert_exportable(built)   # nothing emitted -> nothing dangling
        out = tmp_path / "o"
        out.mkdir()
        n_addr, n_edges = BD.write_track_b(built, out)
        assert (n_addr, n_edges) == (0, 0)


@needs_sources
class TestSqlRowShape:
    """The SQL writer has its own copy of the row shape; the CSV tests do not cover it."""

    @pytest.fixture(scope="class")
    def sql(self, tmp_path_factory):
        out = tmp_path_factory.mktemp("sqlshape")
        with BD.Snapshot(SNAPSHOT) as snap:
            ds = BD.build(XLSX, snap)
        return ds, BD.write_track_b_sql(ds, out).read_text(encoding="utf-8")

    def test_the_admin_type_matches_the_kind_in_the_sql_too(self, sql):
        # Swapping these put the wrong c_admin_type on all 55 INSERTs while every
        # CSV assertion stayed green.
        import re
        ds, text = sql
        kinds = {}
        for u in ds["units"]:
            if u["emitted"]:
                for a in u["addresses"]:
                    kinds[a["key"]] = u["kind"]
        blocks = re.findall(
            r"^-- (\S+)  \(.*?^  '(Duzhuanyunyanshisi|Fensi)', @cat_(yunsi|fensi),",
            text, re.M | re.S)
        assert len(blocks) == ds["stats"]["address_rows"]
        for key, admin_type, cat in blocks:
            want = "Duzhuanyunyanshisi" if kinds[key] == "運司" else "Fensi"
            assert admin_type == want, key
            assert cat == ("yunsi" if kinds[key] == "運司" else "fensi"), key


class TestAdvisoryScanCoversTheShortName:
    def test_the_scan_finds_a_short_name_collision(self, mini_snapshot):
        """Kills `names = [u["name"]]`.

        Against the real data the scan finds nothing either way - nothing in CBDB
        carries a qualified salt name - so only a fixture with a real short-name
        collision can tell the two apart.
        """
        findings = BD.Findings()
        units = [{"key": "u", "blocked": False, "dynasty_code": 19,
                  "name": "兩淮都轉運鹽使司泰州分司", "name_short": "泰州分司"}]
        BD._advisory_duplicate_scan(units, mini_snapshot, findings)
        assert [m["c_office_id"] for m in units[0]["existing_office_matches"]] == [999]
        assert findings.count("warning") == 1

    def test_a_wildcard_cannot_match_an_unrelated_alias(self, mini_snapshot):
        # 998's c_office_chn_alt is non-NULL, so an unescaped `%` would match it.
        findings = BD.Findings()
        units = [{"key": "u", "blocked": False, "dynasty_code": 19,
                  "name": "%", "name_short": "%"}]
        BD._advisory_duplicate_scan(units, mini_snapshot, findings)
        assert units[0]["existing_office_matches"] == []


@needs_sources
class TestSqlValuesMatchTheDataset:
    """Every scalar in the INSERT, compared field by field against dataset.json.

    An independent review mutated the SQL writer to emit `a["y"]` for `x_coord` -
    every longitude replaced by its latitude, on all 55 rows - and the whole suite
    stayed green. The CSV had assertions; the SQL, which is the artifact a human
    actually runs against the live database, had none for its values.
    """

    @pytest.fixture(scope="class")
    def rows(self, tmp_path_factory):
        import re
        out = tmp_path_factory.mktemp("sqlvals")
        with BD.Snapshot(SNAPSHOT) as snap:
            ds = BD.build(XLSX, snap)
        sql = BD.write_track_b_sql(ds, out).read_text(encoding="utf-8")
        # -- <key>  (...)\nSET @aNNN := ...\nINSERT ... VALUES (\n <4 value lines>
        pat = re.compile(
            r"^-- (?P<key>\S+)  \(.*?\n"
            r"^SET @a\d+ := @addr_base \+ \d+;\n"
            r"^INSERT INTO ADDR_CODES .*?VALUES \(\n"
            r"^  @a\d+, (?P<chn>.*?), (?P<rom>.*?), (?P<alt>.*?),\n"
            r"^  (?P<atype>.*?), (?P<cat>@cat_\w+), (?P<first>\d+), (?P<last>\d+),\n"
            r"^  (?P<x>[^,]+), (?P<y>[^,]+), NULL,\n",
            re.M | re.S)
        return ds, {m.group("key"): m.groupdict() for m in pat.finditer(sql)}, sql

    def test_every_emitted_row_is_present(self, rows):
        ds, parsed, _ = rows
        keys = {a["key"] for u in ds["units"] if u["emitted"] for a in u["addresses"]}
        assert set(parsed) == keys

    def test_x_is_longitude_and_y_is_latitude(self, rows):
        ds, parsed, _ = rows
        checked = 0
        for u in ds["units"]:
            if not u["emitted"]:
                continue
            for a in u["addresses"]:
                got = parsed[a["key"]]
                if a["x"] is None:
                    assert got["x"] == "NULL" and got["y"] == "NULL"
                else:
                    assert float(got["x"]) == a["x"], f"{a['key']} x"
                    assert float(got["y"]) == a["y"], f"{a['key']} y"
                    # The two are never equal in this dataset, so a swap is visible.
                    assert a["x"] != a["y"]
                    checked += 1
        assert checked >= 50

    def test_the_names_years_and_category_match_the_dataset(self, rows):
        ds, parsed, _ = rows
        for u in ds["units"]:
            if not u["emitted"]:
                continue
            want_cat = "@cat_yunsi" if u["kind"] == "運司" else "@cat_fensi"
            want_alt = ("'" + u["name_short"] + "'") if u["kind"] == "分司" else "NULL"
            for a in u["addresses"]:
                got = parsed[a["key"]]
                assert got["chn"] == "'" + u["name"] + "'"
                assert got["rom"] == "'" + u["romanization"] + "'"
                assert got["alt"] == want_alt
                assert got["cat"] == want_cat
                assert int(got["first"]) == a["first"]
                assert int(got["last"]) == a["last"]

    def test_the_allocation_is_locked_and_the_range_is_checked(self, rows):
        # A plain MAX() lets a concurrent insert take an id already handed out; the
        # colliding INSERT then fails on the PK and, if the client was told to keep
        # going, the edges bind to the other row. Worse, a count of "everything
        # above @addr_base" still reads 55 in that case.
        import re
        _, _, sql = rows
        # The STATEMENT, not the substring: the explanatory comment above it also
        # contains "FOR UPDATE", so a bare `in sql` passes with the lock removed.
        assert re.search(
            r"^SELECT c_addr_id INTO @addr_base FROM ADDR_CODES.*FOR UPDATE;$",
            sql, re.M), "the allocation must lock the tail of the index"
        assert "SET @addr_base := (SELECT MAX(c_addr_id)" not in sql,             "the unlocked MAX() form must be gone"
        assert re.search(r"BETWEEN @addr_base \+ 1 AND @addr_base \+ \d+", sql)
        assert "must_be_zero_range_already_occupied" in sql
        assert "do NOT pass --force" in sql
