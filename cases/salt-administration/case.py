# -*- coding: utf-8 -*-
"""The Ming/Qing salt administration: one case.

Everything here is a fact about THIS job - Ning Hao's spreadsheet, the two dynasty
windows it covers, the seat names it spells its own way, the units whose source rows
contradicted themselves, and the naming conventions chosen for it in docs/11. None
of it is a rule about how a place-name import works; those live in
`src/cbdb_agent/places_and_offices/`.

The names this module must expose are listed in
`cbdb_agent.places_and_offices.pipeline.build()`'s docstring, and
`cbdb_agent.places_and_offices.build_dataset.load_case` refuses a case that is missing any of
them. The next job of this shape means writing another `cases/<name>/case.py`, not
touching the tool.

    python -m cbdb_agent.places_and_offices.build_dataset \
        --case salt-administration --xlsx <path to the xlsx>
"""

from __future__ import annotations

import functools
from pathlib import Path

from track_b_sql import extra_exports  # noqa: F401  (build_dataset hook)
from cbdb_agent.places_and_offices import romanize as _romanize
from cbdb_agent.places_and_offices.seats import SeatRules
from cbdb_agent.places_and_offices.units import RawUnit, SheetError

try:
    import openpyxl
except ImportError as exc:  # pragma: no cover - environment-dependent
    # Re-raised, not sys.exit(): this module is imported by tests, and SystemExit
    # at import time breaks collection instead of producing a clean skip.
    raise ImportError(
        "openpyxl is required to read this case's source (see requirements-dev.txt)"
    ) from exc


# The directory this case's generated output goes in: data/build/<CASE_NAME>/.
CASE_NAME = "salt-administration"

# The title and the design reference that go at the top of the staging file.
BATCH_TITLE = "Ming/Qing salt administration - Track B: the place names."
DESIGN_DOC = "docs/11-salt-administration-design.md"

# Everything the review page says that is about THIS job rather than about the
# shape of the data. `{...}` is filled from dataset.json's `stats`; `**bold**` and
# `backtick code` are the only markup, applied after escaping.
#
# In the case rather than in the page because the page renders any job of this
# shape, and a masthead describing the salt administration over someone else's
# gazetteer would be worse than no masthead at all.
REVIEW_PAGE = {
    "title": "鹽運使司 Review",
    "headline": "明清鹽運使司與分司",
    "standfirst": "Ning Hao's compilation of the Ming and Qing salt administration, "
                  "prepared for CBDB as place names. Every row below is a proposed "
                  "change to global reference data — visible to every CBDB user and "
                  "referenced by any number of person records. Nothing here has been "
                  "submitted.",
    "notices": [
        "**Track A was dropped on 2026-09-11.** A separate import had already "
        "covered the salt-administration *post titles*, and Ning Hao's list names "
        "the *institutions* — which belong in the place-name tables, as he "
        "proposed. The office panels below are kept only as the working that led "
        "to that decision; **nothing in Track A is being submitted**. What ships "
        "is Track B, and it now goes through the API like anything else.",
    ],
    # Labels the page only shows if a case supplies them - all of these describe
    # THIS job's two-track history and would be nonsense over anyone else's data.
    "office_stat_label": "office creates (dropped)",
    "office_pane_heading": "Track A · OFFICE_CODES",
    "office_pane_pill": "dropped 2026-09-11",
    "rows_note": "The left pane is the office reading of each unit, kept for "
                 "reference after Track A was dropped; **the right pane is what "
                 "actually ships**, now through the API rather than a SQL script. "
                 "They are two representations of one unit, which is the whole "
                 "point of the request.",
    "source_id_note": "by decision — the dataset rests on four bodies of material "
                      "and the column holds one.",
    "tracks": [
        {"id": "a", "heading": "Track A · 官名表", "pill": "dropped",
         "pill_tone": "warn",
         "verdict": "{office_creates} `office` aggregate creates — NOT submitted.",
         "text": "Superseded on 2026-09-11 by a separate import of the salt post "
                 "titles. Kept here as the working behind that decision, not as a "
                 "plan."},
        {"id": "b", "heading": "Track B · 地名表", "pill": "API",
         "verdict": "{address_rows} `ADDR_CODES` rows, {belongs_edges} "
                    "`ADDR_BELONGS_DATA` edges.",
         "text": "`/api/v2` gained create paths for both tables on 2026-09-11, so "
                 "these go through the normal staged, previewed, audit-logged "
                 "path. Every row is global reference data — visible to every CBDB "
                 "user and referenced by any number of person records. Emitted by "
                 "`cbdb_agent/places_and_offices/emit_addresses.py`; the SQL script this "
                 "page used to point at is superseded."},
    ],
}

# The two kinds of unit these sheets contain, and how they nest. ROOT_KIND hangs
# off its dynasty; every other kind hangs off its region's root row. Which kinds
# exist is a fact about this source - a gazetteer import would have 府/州/縣 - so
# the method reads them from here rather than testing for 運司 itself.
ROOT_KIND = "運司"
BRANCH_KIND = "分司"

# What the staging batch calls the units it left out. Per-job because who has to
# decide is a fact about the job.
EXCLUDED_HEADING = "Excluded pending a decision from Ning Hao:"

# The namespace `unit_key` puts on the front of every key. Stripped back off when
# a key becomes a proposal id: it is the same on all of them, so it carries no
# information there and only makes them longer.
KEY_PREFIX = "salt:"

# ADDR_CODES.c_admin_type per kind: a romanized label, chosen to match the
# romanizations above rather than derived by the server.
ADMIN_TYPE = {"運司": "Duzhuanyunyanshisi", "分司": "Fensi"}

# The ADMIN_CAT_CODES rows this dataset needs, and the proposal ids they get.
#
# Content, not method: every jurisdiction contribution needs *some* category, and
# which one is a fact about the units being described. `ADMIN_CAT_CODES` has no code
# for either of these (211 rows, nothing matching 鹽 or 分司), while all 362
# comparable jurisdiction rows in CBDB carry a real one and none uses 0 - which is
# why the default is to create them rather than fall back to `0 [Unknown]`.
ADMIN_CATEGORIES = {
    "運司": dict(id="cat-yunsi", py="Duzhuanyunyanshisi", hz="都轉運鹽使司",
                trans="Salt Distribution Commission"),
    "分司": dict(id="cat-fensi", py="Fensi", hz="分司",
                trans="Salt Distribution Branch Office"),
}

# --- Source provenance --------------------------------------------------------
#
# `source_id` stays 0 (`TEXT_CODES` 0 = 未詳) by the user's decision, 2026-09-10, and
# the real provenance goes into c_notes as prose. c_source holds ONE c_textid and this
# dataset rests on four bodies of material at once - 明清方志 is a genre with no
# c_textid at all, and 福建運司志 / 增修河東鹽法備覽 have no TEXT_CODES row (reported
# as findings under AGENTS.md rule 12, never created to unblock this). Design §5.
SOURCE_NOTE = "出處：明清方志、兩淮鹽法志、福建運司志、增修河東鹽法備覽。"
SOURCE_ID = 0

# --- Dynasty framing ----------------------------------------------------------
#
# The windows are the ADDR_CODES convention, NOT the DYNASTIES span. DYNASTIES says
# Ming 1368-1644, but ADDR_CODES ends Ming rows at 1643 and starts Qing rows at 1644,
# so testing overlap against 1644 admits the entire Qing block: 18 of 19 Ming 治所
# names come back ambiguous, with identical coordinates on both rows so no coordinate
# test can separate them. Design §5.2.
DYNASTIES = {
    "明代": {
        "code": 19,
        "window": (1368, 1643),
        "root_addr_id": 4329,   # 明朝, itself 1368-1643
        "root_name": "明朝",
    },
    "清代": {
        "code": 20,
        "window": (1644, 1911),
        "root_addr_id": 6756,   # 清朝, itself 1644-1911
        "root_name": "清朝",
    },
}

# ADDR_CODES 0 = [未詳] / [Unknown]: CBDB's standing sentinel for an unknown place.
# Used for 明代 北平河間, whose 治所 the source gives as 未詳. Design §3.3.
UNKNOWN_ADDR_ID = 0

# --- OFFICE_TYPE_TREE attachment ----------------------------------------------
#
# No new tree nodes are needed (and none could be created - no API path). The bare
# dynasty node is deliberately NOT included: only 16 of the 38 offices under 19072801
# carry it, and the four closest analogues (72734 兩淮鹽運使, 72747 河東鹽運使,
# 72504 兩浙運使, 72505 兩淮運使) carry none. Design §5 Track A.
MING_TYPE_ID = "19072801"          # 都轉鹽運使司
QING_GENERIC_TYPE_ID = "20070402"  # 鹽運司使衙門 - the bureau; every Qing row gets it

# Region-specific Qing nodes, added ON TOP of the generic one, for 運司 rows only.
# All four are worded as the officer rather than the bureau, so a 分司 - which is not
# the commissioner - gets only the generic node.
QING_REGION_TYPE_IDS = {
    "兩淮": "20070405",   # 兩淮鹽運使兼兵備銜
    "兩浙": "20070406",   # 兩浙江南鹽運使
    "長蘆": "20070403",   # 長蘆鹽運使兼鹽法道
    "山東": "20070404",   # 山東鹽運使兼鹽法道
    # 河東 and 福建 have no named node; they get the generic one alone.
}

# --- Blocked units ------------------------------------------------------------
#
# Emitted nowhere - not in the batch, not in the CSVs - until the source
# contradiction behind them is answered. Design §3.1, §3.10, §9.
#
# EMPTY since 2026-09-18. Both entries were 清代 兩浙, and both are settled:
#
#   寧紹分司  The sheet read 紹興府 1644-1793 plus a reversed 杭州府 1793-1685. It
#             now reads 紹興府 1644-1685 and nothing else - the row was fixed
#             upstream, matching the corrected 清代兩浙 table the user supplied.
#             1685 closes to 1684 under §5.1(a), since 寧紹溫台分司 begins that
#             year, so this unit behaves exactly like 溫台分司 beside it.
#   嘉松分司  Not a data defect after all. The question was that 備註 「裁松江併入
#             嘉興分司，改名嘉松分司」 names 嘉興分司 as the survivor while the 治所
#             moves 嘉興府 → 杭州府. Answered by the user, 2026-09-18: "并入嘉興
#             分司之后，确实搬到杭州府". Both cells were right; the seat move is
#             real. No data change - only the block lifted.
#
# Kept as an empty dict rather than deleted: this is the mechanism that keeps a
# defective row out of an irreversible batch, and it is the first thing the next
# contribution will need.
BLOCKED: dict[tuple[str, str], str] = {}

# --- Cross-unit successions ---------------------------------------------------
#
# A handover, unlike a terminus, means the predecessor's last year must be decremented
# so predecessor and successor are not both in the database that year. In-unit seat
# moves are detected mechanically; cross-unit ones cannot be, and are listed here with
# their evidence rather than inferred from a year collision. Design §5.1(a).
#
# Note the 備註 verb does NOT decide this: 併入 appears on both sides. 黃崎分司's
# 「併入水口分司」 in 1677 is a terminus (水口分司 was already running since 1644, so it
# does not *start* in 1677); 溫台分司's 「裁溫台分司併入寧紹分司，改名寧紹溫台分司」 in
# 1685 is a handover (寧紹溫台分司 begins that year).
SUCCESSIONS = {
    ("明代", "北平河間都轉運鹽使司"): (1373, "長蘆都轉運鹽使司", "長蘆運司 begins the year 北平河間 ends"),
    ("清代", "淮安分司"): (1763, "海州分司", "海州分司 begins 1763"),
    ("清代", "青州分司"): (1781, "天津分司", "備註 「青州分司改稱天津分司」"),
    ("清代", "嘉興分司"): (1704, "嘉松分司", "備註 「裁松江併入嘉興分司，改名嘉松分司」"),
    ("清代", "松江分司"): (1704, "嘉松分司", "same 備註 as 嘉興分司"),
    ("清代", "溫台分司"): (1685, "寧紹溫台分司", "備註 「裁溫台分司併入寧紹分司，改名寧紹溫台分司」"),
    ("清代", "寧紹分司"): (1685, "寧紹溫台分司",
                          "same 備註; confirmed by the user's corrected 清代兩浙 "
                          "table, 2026-09-18, which ends 寧紹分司 at 1685"),
}

# --- 治所 name variants -------------------------------------------------------
#
# Applied only after an exact match fails, and only for these listed pairs. No fuzzy
# matching and no prefix fallback: a prefix match is how 古田 in Fujian silently
# becomes 古田 in Guangxi. Design §5.2 rule 2.
SEAT_VARIANTS = {
    "温州府": "溫州府",   # sheet has U+6E29, CBDB has U+6EAB
    "蒲台": "蒲臺",       # sheet has simplified 台, CBDB has 臺
}

# --- Coordinate boxes for genuinely ambiguous seat names ----------------------
#
# (dynasty, seat name) -> (min_x, min_y, max_x, max_y) plus why. These are the cases
# where two ADDR_CODES rows are two DIFFERENT PLACES, so the duplicate-row tiebreak
# has no licence to fire. Without them, 淮安分司 lands in Manchuria. Design §5.2 rule 3.
SEAT_BOXES = {
    ("明代", "通州"): ((119.0, 31.0, 122.0, 33.5),
                       "兩淮's 通州 is modern Nantong (120.85, 32.01); "
                       "4393/4394 are Beijing's 通州 (116.66, 39.91)"),
    ("清代", "安東"): ((118.0, 32.5, 121.0, 35.0),
                       "淮安分司's 安東 is 漣水 in Jiangsu (119.26, 33.77); "
                       "6794 is 安東/丹東 in Liaoning (124.38, 40.13), 900 km away"),
    ("明代", "古田"): ((117.5, 25.5, 120.0, 27.5),
                       "水口分司's 古田 is in Fujian (118.78, 26.60); "
                       "6228 is 古田 in Guangxi (109.75, 25.12)"),
}

# Two coordinates count as the same point when they differ by at most COORD_TOL
# degrees (1e-5 deg ~ 1 m). Bit-equality is too strict: CBDB stores one point at
# different precisions in different rows - 4634 is 120.85464478/32.010471344 and 4635
# is 120.854645/32.010471. Under exact equality the 明 通州分司 tiebreak fails and the
# generator refuses the very case it was written for.
#
# It is an ABSOLUTE TOLERANCE, not a rounding. "Agree to 5 decimal places" sounds
# equivalent and is not: those two 通州 values differ by 2e-6 but straddle a 5-dp
# boundary, so round(x, 5) puts them in different buckets and the comparison fails
# on the one case it exists for. Rounding compares positions on a grid; what we mean
# is distance. Design §5.2 rule 4, §5.3.
COORD_TOL = 1e-5


def same_point(ax, ay, bx, by) -> bool:
    """Within COORD_TOL on both axes. Two NULLs count as the same 'no point'."""
    if ax is None or bx is None:
        return ax is None and bx is None
    return abs(ax - bx) <= COORD_TOL and abs(ay - by) <= COORD_TOL


PINYIN = {
    "兩": "liang", "淮": "huai", "都": "du", "轉": "zhuan", "運": "yun", "鹽": "yan",
    "使": "shi", "司": "si", "泰": "tai", "州": "zhou", "分": "fen", "浙": "zhe",
    "長": "chang", "蘆": "lu", "河": "he", "東": "dong", "山": "shan", "福": "fu",
    "建": "jian", "北": "bei", "平": "ping", "間": "jian", "安": "an", "松": "song",
    "嘉": "jia", "興": "xing", "溫": "wen", "台": "tai", "寧": "ning", "紹": "shao",
    "滄": "cang", "青": "qing", "場": "chang", "中": "zhong", "西": "xi",
    "濱": "bin", "樂": "le", "膠": "jiao", "萊": "lai", "水": "shui", "口": "kou",
    "黃": "huang", "崎": "qi", "南": "nan", "港": "gang", "海": "hai",
    "天": "tian", "津": "jin", "薊": "ji", "永": "yong",
    "江": "jiang", "通": "tong",
}


def pinyin_of(chinese: str) -> str:
    """This case's characters, read through the tool's joining rule."""
    return _romanize.pinyin_of(chinese, PINYIN)


def romanize_compact(chinese: str) -> str:
    return _romanize.romanize_compact(chinese, PINYIN)


# Region romanizations for the English translation, keyed by the 運司's region prefix.
REGION_EN = {
    "兩淮": "Lianghuai", "兩浙": "Liangzhe", "長蘆": "Changlu",
    "河東": "Hedong", "山東": "Shandong", "福建": "Fujian",
    "北平河間": "Beiping-Hejian",
}

# 分司 short-name romanizations, for the English translation.
BRANCH_EN = {
    "泰州": "Taizhou", "通州": "Tongzhou", "淮安": "Huai'an", "海州": "Haizhou",
    "松江": "Songjiang", "嘉興": "Jiaxing", "嘉松": "Jiasong", "溫台": "Wentai",
    "寧紹": "Ningshao", "寧紹溫台": "Ningshaowentai",
    "滄州": "Cangzhou", "青州": "Qingzhou", "天津": "Tianjin", "薊永": "Jiyong",
    "東場": "Dongchang", "中場": "Zhongchang", "西場": "Xichang",
    "東": "Dong", "中": "Zhong", "西": "Xi",
    "濱樂": "Binle", "膠萊": "Jiaolai",
    "水口": "Shuikou", "黃崎": "Huangqi", "南港": "Nangang",
}
# --- Seat choices that are NOT duplicate rows --------------------------------
#
# (dynasty, seat name) -> (chosen c_addr_id, why). For candidate sets that share a
# point but are NOT the same row twice - different c_admin_type, different span - so
# the duplicate-row tiebreak has no licence to fire and picking by id would be making
# a historical decision by accident. Design §3.11, §5.2 rule 4.
SEAT_DECISIONS = {
    ("清代", "天津"): {
        "addr_id": 7242,
        # The date the user ruled on it, or None while the pick is this generator's
        # own. That difference is the whole point of the field: an unconfirmed
        # decision raises a WARNING, because something chose between two real places
        # and a human has not yet looked; a confirmed one raises a note, because
        # they have. Without it, the ninth ambiguous seat would look exactly as
        # settled as the one that was actually asked about.
        "confirmed": "2026-09-18",
        "why":
            "7242 天津 (Xian, 1644-1911) and 700000 天津 (Wei, 1644-1910) sit on the "
            "same point but are different rows: a county and a guard. The sheet "
            "writes 天津府 for the 運司 seat and bare 天津 for 青州/天津分司, and 天津 "
            "was 天津衛 until 1725 - so 700000 was defensible for the 17th-century "
            "rows. Taking the county keeps all three 長蘆 rows on one consistent "
            "series. Confirmed by the user on 2026-09-18: 「7242 天津（縣，"
            "1644–1911）」. Design §3.11.",
    },
}


def address_romanization(region: str, kind: str, branch: str | None) -> str:
    """`Lianghuai Duzhuanyunyanshisi Taizhou Fensi` - one token per name part.

    NOT `romanize_compact` over the whole string: that produces a 39-character run-on
    (`Lianghuaiduzhuanyunyanshisitaizhoufensi`) which is neither the four-token form
    this design chose nor anything `ADDR_CODES` contains. `romanize_compact` is for a
    single part.
    """
    parts = [region, "都轉運鹽使司"]
    if kind == "分司":
        parts += [branch, "分司"]
    return " ".join(romanize_compact(p) for p in parts if p)


# The rules `cbdb_agent.places_and_offices.seats.resolve_seat` consults, gathered from the tables above.
SEAT_RULES = SeatRules(
    dynasties=DYNASTIES,
    unknown_addr_id=UNKNOWN_ADDR_ID,
    unknown_seat_name="未詳",
    variants=SEAT_VARIANTS,
    boxes=SEAT_BOXES,
    decisions=SEAT_DECISIONS,
)


def admin_type_of(kind: str) -> str:
    """`ADDR_CODES.c_admin_type` for a unit of this kind."""
    return ADMIN_TYPE[kind]


def address_alt_names(kind: str, name_short: str) -> str | None:
    """`ADDR_CODES.c_alt_names`.

    The short name, for a 分司 only. Its `c_name_chn` is the qualified
    兩淮都轉運鹽使司泰州分司, and 泰州分司 is what a reader searching for it will
    type. A 運司's short name IS its qualified name, so repeating it would only
    duplicate the column.
    """
    return name_short if kind == BRANCH_KIND else None


def romanization_alt(kind: str, branch: str) -> str:
    """The second reading shown beside `romanization` in the review page.

    Two readings, not one, because docs/11 section 3.7 leaves the choice between
    the run-together and the spaced form to the reviewer rather than settling it
    in code.
    """
    return (f"{romanize_compact(branch)} Fensi" if kind == BRANCH_KIND
            else romanize_compact("都轉運鹽使司"))


def batch_summary(n_addr: int, n_edge: int, xlsx: str) -> list[str]:
    """What the staging batch says about itself, for a human reading the file."""
    return [
        f"{n_addr} place names and {n_edge} belongs-to edges for the Ming and Qing "
        f"salt administration, from {xlsx} (Ning Hao).",
        "",
        "The office half of this dataset was dropped on 2026-09-11: a separate "
        "import had already covered the salt-administration POST titles, and "
        "Ning Hao's list names the INSTITUTIONS, which belong here as place "
        "names (which was his proposal to begin with).",
    ]


def unit_key(dynasty: str, region: str, name_short: str) -> str:
    """The symbolic identity of one unit, stable across regenerations.

    Stable is the point: it becomes the staging proposal id, so re-running the
    generator after settling a question produces the same ids with possibly
    different values - which is why every proposal also carries a content hash.
    """
    return f"salt:{'ming' if dynasty == '明代' else 'qing'}:{region}:{name_short}"


@functools.lru_cache(maxsize=4)
def _workbook(xlsx: Path):
    """Cached: `build()` reads two sheets, and each load re-parses the whole file."""
    return openpyxl.load_workbook(xlsx, data_only=True)


def read_units(xlsx: Path, sheet: str) -> list[RawUnit]:
    """Rows in sheet order. A 分司 inherits the region of the 運司 above it.

    That inheritance is why this raises rather than defaulting: the sheet's grouping
    is positional, so a 分司 appearing before any 運司 - a re-sort, a new tab, a
    deleted header - would otherwise be attributed to `None` and come out named
    泰州分司泰州分司, translated "None Salt Distribution Commission", and clamped to
    the whole dynasty. Every one of those failures is silent.
    """
    ws = _workbook(xlsx)[sheet]
    units: list[RawUnit] = []
    region: str | None = None
    for i, row in enumerate(ws.iter_rows(values_only=True), start=1):
        c = [x.strip() if isinstance(x, str) else x for x in row]
        c += [None] * (12 - len(c))
        _, yunsi, fensi, uq, uz, s1, s1q, s1z, s2, s2q, s2z, note = c[:12]
        if yunsi == "鹽運總司" or (yunsi is None and fensi in (None, "分司")):
            continue
        seats = []
        if s1:
            seats.append((str(s1), s1q, s1z))
        if s2:
            seats.append((str(s2), s2q, s2z))
        if yunsi:
            region = region_of(str(yunsi))
            units.append(RawUnit("運司", region, str(yunsi), uq, uz, seats, note, i))
        else:
            if region is None:
                raise SheetError(
                    f"{sheet} row {i}: 分司 {fensi!r} appears before any 鹽運總司, so "
                    f"there is no region to attach it to. The grouping is positional; "
                    f"fix the row order rather than guessing.")
            units.append(RawUnit("分司", region, str(fensi), uq, uz, seats, note, i))
    return units


def region_of(yunsi_name: str) -> str:
    """兩淮都轉運鹽使司 -> 兩淮."""
    for suffix in ("都轉運鹽使司", "轉運鹽使司", "運司"):
        if yunsi_name.endswith(suffix):
            return yunsi_name[: -len(suffix)]
    return yunsi_name


def branch_of(fensi_name: str) -> str:
    """泰州分司 -> 泰州."""
    return fensi_name[:-2] if fensi_name.endswith("分司") else fensi_name


def qualified_name(kind: str, yunsi_full: str, name_short: str) -> str:
    """兩淮都轉運鹽使司泰州分司 for a 分司; the 運司's own name for a 運司."""
    return yunsi_full if kind == "運司" else yunsi_full + name_short


def name_alt(kind: str, name_short: str) -> str | None:
    """The short form - but never a copy of the head name.

    On a 運司 the qualified name IS the short name, so repeating it would make
    c_office_chn_alt look like it carries an attested variant when it does not.
    """
    return None if kind == "運司" else name_short


def translation(kind: str, region: str, name_short: str) -> str:
    region_en = REGION_EN.get(region, region)
    if kind == "運司":
        return f"{region_en} Salt Distribution Commission"
    branch = branch_of(name_short)
    branch_en = BRANCH_EN.get(branch, branch)
    return f"{region_en} Salt Distribution Commission, {branch_en} Branch Office"


def type_ids_for(kind: str, dynasty: str, region: str) -> list[str]:
    if dynasty == "明代":
        return [MING_TYPE_ID]
    ids = [QING_GENERIC_TYPE_ID]
    if kind == "運司" and region in QING_REGION_TYPE_IDS:
        ids.append(QING_REGION_TYPE_IDS[region])
    return ids
