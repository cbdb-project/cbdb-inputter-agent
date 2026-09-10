# -*- coding: utf-8 -*-
"""Hand-curated constants for the Ming/Qing salt-administration import.

Everything in this file is a *decision*, not a derivation. It lives apart from
`build_dataset.py` so a reviewer can read the judgement calls without reading the
machinery, and so that changing one (a romanization, a coordinate box, a succession)
is a one-line edit in an obvious place.

The design and the reasoning behind each table: `docs/11-salt-administration-design.md`.
Nothing here may be replaced by a heuristic. In particular the variant map, the
coordinate boxes and the succession table exist precisely *because* fuzzy matching and
year-collision inference produce plausible-looking wrong answers (design §5.1(a),
§5.2 rules 2-4).
"""

from __future__ import annotations

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
# Emitted nowhere until Ning Hao answers. Design §3.1, §3.10, §9.
BLOCKED = {
    ("清代", "寧紹分司"):
        "治所 dates are self-contradictory: 紹興府 1644-1793 overlaps 寧紹溫台分司 "
        "(1685-1911) by 108 years and contradicts its 備註 「裁溫台分司併入寧紹分司，"
        "改名寧紹溫台分司」, and the second seat 杭州府 1793-1685 is reversed. The "
        "consistent reading is 1644-1684, with 1793 a slip for 1685 in both cells.",
    ("清代", "嘉松分司"):
        "備註 「裁松江併入嘉興分司，改名嘉松分司」 says the surviving unit is 嘉興分司, "
        "but the 治所 moves from 嘉興府 to 杭州府. One of the two is wrong.",
}

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
    ("清代", "寧紹分司"): (1685, "寧紹溫台分司", "same 備註 (unit is blocked)"),
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

# --- Romanization -------------------------------------------------------------
#
# Supplied rather than left to the server's per-character derivation, because CBDB's
# own rows disagree about 都 (70238 `du zhuan yun yan shi` vs 72239 `dou zhuan yun yan
# shi si pan guan`) and a reading should be a reviewable decision. `pinyin_alt` is
# deliberately left null and derived server-side from name_alt. Design §5 Track A.
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


def pinyin_of(chinese: str) -> str:
    """Space-separated lowercase syllables, CBDB `c_office_pinyin` style.

    Raises on an unmapped character rather than silently dropping it - a missing
    syllable in a romanization is exactly the kind of defect nobody notices in review.
    """
    out = []
    for ch in chinese:
        if ch not in PINYIN:
            raise KeyError(f"no pinyin registered for {ch!r} (in {chinese!r})")
        out.append(PINYIN[ch])
    return " ".join(out)


# --- Seat choices that are NOT duplicate rows --------------------------------
#
# (dynasty, seat name) -> (chosen c_addr_id, why). For candidate sets that share a
# point but are NOT the same row twice - different c_admin_type, different span - so
# the duplicate-row tiebreak has no licence to fire and picking by id would be making
# a historical decision by accident. Design §3.11, §5.2 rule 4.
SEAT_DECISIONS = {
    ("清代", "天津"): (
        7242,
        "7242 天津 (Xian, 1644-1911) and 700000 天津 (Wei, 1644-1910) sit on the same "
        "point but are different rows: a county and a guard. The sheet writes 天津府 "
        "for the 運司 seat and bare 天津 for 青州/天津分司, and 天津 was 天津衛 until "
        "1725 - so 700000 is defensible for the 17th-century rows. Taking the county "
        "keeps all three 長蘆 rows on one consistent series; flagged in the design "
        "(§3.11) as a question for Ning Hao rather than settled here.",
    ),
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


def romanize_compact(chinese: str) -> str:
    """`Duzhuanyunyanshisi` - capitalized, syllables run together.

    The `ADDR_CODES` style for the type half of a place name (`Duzhihuishisi`,
    `Buzhengsi`). Note the table has no single convention - it also holds lowercase
    forms (`Shanxi xunfu`) - so this is a choice, surfaced in the review page rather
    than asserted as house style. Design §5 Track B.
    """
    joined = "".join(PINYIN[ch] for ch in chinese)
    return joined[:1].upper() + joined[1:]
