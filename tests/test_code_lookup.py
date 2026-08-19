"""Code-label resolution, against a tiny purpose-built SQLite fixture.

The fixture mirrors the real schema for the handful of tables the labels need, with
enough rows to exercise the two hierarchy walks (address belongs-chain, office type
tree) including their awkward cases: a multi-parent address, a cycle, and the
`0`/未詳 root that every real chain terminates in.
"""

import sqlite3

import pytest
import responses

from cbdb_agent.code_lookup import (
    FIELD_CODE_TABLES,
    CodeResolver,
    code_table_names,
    collect_code_values,
    is_code_value,
)
from cbdb_agent.staging import Conflict, ConflictOption, Proposal, StagingBatch


@pytest.fixture
def snapshot():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE DYNASTIES (c_dy INT, c_dynasty TEXT, c_dynasty_chn TEXT,
                                c_start INT, c_end INT);
        INSERT INTO DYNASTIES VALUES (18, 'Yuan', '元', 1234, 1367);

        CREATE TABLE ADDR_CODES (c_addr_id INT, c_name TEXT, c_name_chn TEXT,
                                 c_firstyear INT, c_lastyear INT, c_admin_type TEXT,
                                 c_alt_names TEXT);
        INSERT INTO ADDR_CODES VALUES
            (18444, 'Fuqing Zhou', '福清州', 1280, 1367, 'Zhou', ''),
            (18434, 'Fuzhou Lu',   '福州路', 1280, 1367, 'Lu',   ''),
            (18233, 'Jiangzhe',    '江浙行中書省', 1280, 1367, 'Sheng', ''),
            (16776, 'Yuan',        '元朝', 1234, 1367, 'Dynasty', ''),
            (555,   'Forked',      '雙親地', 1000, 1100, 'Xian', ''),
            (556,   'Rejoined',    '兩期同父地', 1150, 1250, 'Xian', ''),
            (557,   'SamePar',     '同一上級', 1100, 1300, 'Xian', ''),
            (558,   'LateXian',    '晚縣', 1478, 1643, 'Xian', ''),
            (559,   'EarlyFu',     '早府', 1368, 1477, 'Fu',   ''),
            (560,   'LateFu',      '晚府', 1478, 1643, 'Fu',   ''),
            (901,   'CycleA',      '甲', 1, 2, 'Xian', ''),
            (902,   'CycleB',      '乙', 1, 2, 'Xian', '');

        CREATE TABLE ADDR_BELONGS_DATA (c_addr_id INT, c_belongs_to INT,
                                        c_firstyear INT, c_lastyear INT);
        INSERT INTO ADDR_BELONGS_DATA VALUES
            (18444, 18434, 1280, 1367),
            (18434, 18233, 1280, 1367),
            (18233, 16776, 1280, 1367),
            (16776, 0,     1234, 1367),   -- the 未詳 root every chain ends at
            (555,   18434, 1000, 1050),   -- two DIFFERENT parents: a real fork
            (555,   18233, 1050, 1100),
            (556,   557,   1150, 1200),   -- the SAME parent in two periods:
            (556,   557,   1210, 1250),   -- one parent, not a fork
            (557,   16776, 1100, 1300),
            (558,   559,   1368, 1477),   -- listed first, but the wrong period
            (558,   560,   1478, 1643),   -- the one that actually fits 558
            (901,   902,   1, 2),          -- a cycle
            (902,   901,   1, 2);

        CREATE TABLE OFFICE_CODES (c_office_id INT, c_dy INT, c_office_pinyin TEXT,
                                   c_office_chn TEXT, c_office_chn_alt TEXT,
                                   c_office_trans TEXT);
        INSERT INTO OFFICE_CODES VALUES
            (63057, 18, 'xian wei', '縣尉', '尉;邑尉', 'District Defender');

        CREATE TABLE OFFICE_TYPE_TREE (c_office_type_node_id TEXT,
                                       c_office_type_desc TEXT,
                                       c_office_type_desc_chn TEXT, c_parent_id TEXT);
        INSERT INTO OFFICE_TYPE_TREE VALUES
            ('0',      'All',   '所有門類', '0'),
            ('18',     'Yuan',  '元朝',     '0'),
            ('1810',   'Local', '地方官類', '18'),
            ('181003', 'Xian',  '諸縣門',   '1810');

        CREATE TABLE OFFICE_CODE_TYPE_REL (c_office_id INT, c_office_tree_id TEXT);
        INSERT INTO OFFICE_CODE_TYPE_REL VALUES
            (63057, '18'),        -- dynasty node: a strict prefix of the one below
            (63057, '181003');

        CREATE TABLE TEXT_CODES (c_textid INT, c_title_chn TEXT, c_title TEXT,
                                 c_text_year INT, c_source INT, c_title_alt_chn TEXT);
        INSERT INTO TEXT_CODES VALUES
            (27144, '全元文', 'Quan Yuan Wen', 1998, 0, NULL),
            (6088,  '俟菴集', 'Sian ji',       1340, 27144, NULL);

        CREATE TABLE ENTRY_CODES (c_entry_code INT, c_entry_desc TEXT,
                                  c_entry_desc_chn TEXT);
        INSERT INTO ENTRY_CODES VALUES (124, 'jinshi', '科舉: 國子監進士');
        """
    )
    return con


def resolver(snapshot):
    return CodeResolver(snapshot=snapshot)


# --- basics -------------------------------------------------------------------


def test_source_name_reports_which_backend_was_used(snapshot):
    assert resolver(snapshot).source_name == "sqlite-snapshot"


def test_resolver_requires_a_source():
    with pytest.raises(ValueError):
        CodeResolver()


def test_simple_code_gets_chinese_label_and_english_sub(snapshot):
    label = resolver(snapshot).label_for("entry", 124)
    assert label["label"] == "科舉: 國子監進士"
    assert label["sub"] == "jinshi"


def test_dynasty_label_includes_its_span(snapshot):
    label = resolver(snapshot).label_for("dynasty", 18)
    assert label["label"] == "元"
    assert "1234~1367" in label["sub"]


def test_unknown_code_returns_none(snapshot):
    assert resolver(snapshot).label_for("entry", 999999) is None


def test_sentinel_zero_is_labelled_unknown_not_looked_up(snapshot):
    label = resolver(snapshot).label_for("addr", 0)
    assert label["label"] == "未詳"


# --- addresses ----------------------------------------------------------------


def test_address_shows_its_own_validity_years(snapshot):
    """The leaf's window is what a reviewer checks a Yuan address against."""
    label = resolver(snapshot).label_for("addr", 18444)
    assert label["label"] == "福清州"
    assert "1280~1367" in label["sub"]


def test_address_chain_is_full_root_first_and_drops_the_unknown_root(snapshot):
    label = resolver(snapshot).label_for("addr", 18444)
    chain = next(l for l in label["lines"] if l.startswith("隸屬："))
    # Root-first, all the way up, and WITHOUT the ADDR_BELONGS_DATA 0/未詳 root.
    assert chain.index("元朝") < chain.index("江浙行中書省") < chain.index("福州路")
    assert "未詳" not in chain


def test_multi_parent_address_is_flagged_rather_than_silently_resolved(snapshot):
    """Which parent is right can be a real historical question - say so."""
    label = resolver(snapshot).label_for("addr", 555)
    assert any("多個隸屬對象" in line for line in label["lines"])


def test_the_same_parent_in_two_periods_is_not_a_fork(snapshot):
    """A repeated (child, parent) edge with different windows is ONE parent the place
    belonged to twice. The real snapshot has 39 of these; calling them a fork produced
    a warning that was simply untrue."""
    label = resolver(snapshot).label_for("addr", 556)
    assert not any("多個隸屬對象" in line for line in label["lines"])
    assert any("同一上級" in line for line in label["lines"])


def test_the_parent_whose_period_fits_is_chosen_not_the_first_row(snapshot):
    """558 (1478~1643) must land under 560 (1478~1643), not 559 (1368~1477), even
    though 559's row comes first. On the real snapshot this changes 29 addresses."""
    label = resolver(snapshot).label_for("addr", 558)
    chain = next(l for l in label["lines"] if l.startswith("隸屬："))
    assert "晚府" in chain and "早府" not in chain


def test_a_chain_node_shows_the_nodes_own_years_not_the_membership_window(snapshot):
    """A reader seeing "X 1121~1234" takes it to mean when X existed. Attaching the
    child's membership window to the parent's label made that a false claim."""
    label = resolver(snapshot).label_for("addr", 556)
    chain = next(l for l in label["lines"] if l.startswith("隸屬："))
    assert "1100~1300" in chain      # 557's own lifetime
    assert "1150~1200" not in chain  # the membership window
    assert "隸屬 1" not in chain.replace("隸屬：", "")


def test_a_chain_deeper_than_the_cap_says_so(snapshot):
    """Silence would present a truncated chain as a complete one."""
    import cbdb_agent.code_lookup as module

    con = snapshot
    rows = [(9000 + i, 9000 + i + 1, 1, 100) for i in range(module.MAX_CHAIN_DEPTH + 3)]
    con.executemany("INSERT INTO ADDR_BELONGS_DATA VALUES (?,?,?,?)", rows)
    con.executemany(
        "INSERT INTO ADDR_CODES VALUES (?,?,?,?,?,?,?)",
        [(9000 + i, f"n{i}", f"地{i}", 1, 100, "Xian", "") for i in range(len(rows) + 1)],
    )
    label = resolver(con).label_for("addr", 9000)
    assert any("超過" in line for line in label["lines"])


def test_address_chain_survives_a_cycle(snapshot):
    label = resolver(snapshot).label_for("addr", 901)
    assert label["label"] == "甲"  # terminated instead of looping


# --- offices ------------------------------------------------------------------


def test_office_shows_dynasty_code_with_its_chinese_name(snapshot):
    label = resolver(snapshot).label_for("office", 63057)
    assert label["label"] == "縣尉"
    assert any("c_dy=18" in line and "元" in line for line in label["lines"])


def test_office_shows_the_type_tree_chain_root_first(snapshot):
    lines = resolver(snapshot).label_for("office", 63057)["lines"]
    type_lines = [l for l in lines if l.startswith("類型")]
    assert len(type_lines) == 1, "the dynasty-only chain is a prefix and must be dropped"
    assert type_lines[0].index("所有門類") < type_lines[0].index("諸縣門")


def test_office_shows_alternative_names(snapshot):
    lines = resolver(snapshot).label_for("office", 63057)["lines"]
    assert any("邑尉" in line for line in lines)


# --- texts --------------------------------------------------------------------


def test_text_label_is_the_book_title(snapshot):
    label = resolver(snapshot).label_for("text", 27144)
    assert label["label"] == "全元文"


def test_text_shows_the_collection_it_came_from(snapshot):
    label = resolver(snapshot).label_for("text", 6088)
    assert label["label"] == "俟菴集"
    assert any("全元文" in line for line in label.get("lines", []))


# --- what counts as a code ----------------------------------------------------


def test_is_code_value_accepts_integers_and_rejects_decisions():
    for good in (0, 18, -999, "18", " 124 ", "-1"):
        assert is_code_value(good) is True, good
    # Conflict options share a field with codes but often hold a DECISION.
    for bad in ("defer", "new_office_code", "both", "<c_textid>", None, True, 3.5, []):
        assert is_code_value(bad) is False, bad


def test_resolve_values_skips_non_code_options(snapshot):
    out = resolver(snapshot).resolve_values(
        [("c_entry_code", 124), ("c_entry_code", "defer"), ("c_office_id", 63057)]
    )
    assert set(out) == {"entry:124", "office:63057"}


def test_resolve_values_labels_every_element_of_a_list_field(snapshot):
    """postings' c_addr is a LIST of address ids."""
    out = resolver(snapshot).resolve_values([("c_addr", [18444, 18434])])
    assert set(out) == {"addr:18444", "addr:18434"}


def test_unmapped_field_is_ignored(snapshot):
    assert resolver(snapshot).resolve_values([("c_notes", 18444)]) == {}


# --- collect_code_values ------------------------------------------------------


def test_collect_covers_changes_target_pk_and_every_conflict_option():
    """A reviewer choosing between two office codes needs BOTH labelled, not just
    whichever one the agent happened to put in `changes`."""
    proposal = Proposal(
        id="p1",
        resource="postings",
        operation="update",
        person_id=1,
        target_pk={"c_office_id": 63057, "c_posting_id": 9},
        changes={"c_addr": [18444], "c_source": 27144},
        source_quote="q",
        confidence="high",
        conflicts=[
            Conflict(
                id="c1",
                field="c_office_id",
                description="which office",
                options=[ConflictOption(value=64674, rationale="a"),
                         ConflictOption(value=63343, rationale="b")],
                agent_suggestion=63343,
                resolution=64682,
            )
        ],
    )
    pairs = collect_code_values(StagingBatch(batch_id="b", proposals=[proposal]))
    values = {(f, v) for f, v in pairs if not isinstance(v, list)}
    assert ("c_source", 27144) in values
    assert ("c_office_id", 63057) in values          # from target_pk
    assert ("c_office_id", 64674) in values          # option
    assert ("c_office_id", 63343) in values          # option + suggestion
    assert ("c_office_id", 64682) in values          # existing resolution
    assert ("c_addr", [18444]) in pairs


def test_field_map_and_table_names_are_consistent():
    """Every field maps to a table key that code_table_names() knows about."""
    names = code_table_names()
    assert set(FIELD_CODE_TABLES.values()) <= set(names)
    assert names["office"] == "OFFICE_CODES"
    assert names["addr"] == "ADDR_CODES"


# --- the HTTP fallback --------------------------------------------------------


def _config(tmp_path):
    from cbdb_agent.config import Config

    return Config(
        api_base_url="http://localhost:8000",
        api_token="t",
        dry_run=True,
        confirm_prod="",
        max_requests_per_minute=6000,
        local_audit_log_dir=tmp_path / "logs",
    )


def _http_resolver(tmp_path):
    from cbdb_agent.audit_log import AuditLog
    from cbdb_agent.http_client import HttpClient

    config = _config(tmp_path)
    # Inject a no-op sleep: http_client retries 5xx with exponential backoff, and the
    # failure tests below would otherwise spend 3 real seconds each proving it.
    client = HttpClient(
        config, AuditLog(config.local_audit_log_dir), sleep=lambda _s: None
    )
    return CodeResolver(client=client)


@responses.activate
def test_http_fallback_labels_a_whole_table_code(tmp_path):
    responses.add(
        responses.GET,
        "http://localhost:8000/api/select/dynasty",
        json=[{"c_dy": 18, "c_dynasty": "Yuan", "c_dynasty_chn": "元",
               "c_start": 1234, "c_end": 1367}],
        status=200,
    )
    r = _http_resolver(tmp_path)
    assert r.source_name == "public-api"
    assert r.label_for("dynasty", 18)["label"] == "元"
    # The whole table is fetched once, not per lookup.
    r.label_for("dynasty", 18)
    assert len(responses.calls) == 1


@responses.activate
def test_http_fallback_sends_no_credentials(tmp_path):
    """These endpoints are public; a stale token would fail them AND spend the
    shared per-IP failed-auth budget (AGENTS.md rule 10)."""
    responses.add(
        responses.GET, "http://localhost:8000/api/select/dynasty", json=[], status=200
    )
    _http_resolver(tmp_path).label_for("dynasty", 18)
    assert "Authorization" not in responses.calls[0].request.headers


@responses.activate
def test_an_unreachable_endpoint_degrades_to_no_label(tmp_path):
    """validate --staging must keep working offline; a lookup failure is not an error."""
    responses.add(
        responses.GET, "http://localhost:8000/api/select/dynasty", status=500
    )
    assert _http_resolver(tmp_path).label_for("dynasty", 18) is None


@responses.activate
def test_a_failed_endpoint_is_not_retried_for_every_code(tmp_path):
    """One failure is enough - an export must not become a series of timeouts."""
    responses.add(responses.GET, "http://localhost:8000/api/select/range", status=500)
    r = _http_resolver(tmp_path)
    for code in (0, 1, 2, -1):
        assert r.label_for("year_range", code) is None
    # 3 retries of the FIRST call only (http_client's retry budget), nothing after.
    assert len(responses.calls) == 3


# --- the HTTP fallback's hierarchy walks --------------------------------------
# This is the whole no-snapshot path, and it was previously untested: the `q=<id>`
# assumption, the `[[...]]` parser, the per-level walk, and the two legacy endpoints.
# Verified against the live public endpoints while writing these (office/addr/text/
# entry/status/kinship/assoc all resolve by numeric id); the mocks below encode that
# shape so a change in it fails here rather than silently emptying every label.


def _addr_row(addr_id, name, first, last, parent=None):
    row = {
        "c_addr_id": addr_id,
        "c_name": f"Addr{addr_id}",
        "c_name_chn": name,
        "c_firstyear": first,
        "c_lastyear": last,
        "c_admin_type": "Xian",
        "c_alt_names": "",
    }
    text = f"{addr_id} Addr{addr_id} {name}  {first}~{last}"
    if parent:
        pid, pname, pfirst, plast = parent
        text += f" [[{pid} {pname} {pfirst}~{plast}]]"
    row["text"] = text
    return row


def _mock_addr(addr_id, rows):
    responses.add(
        responses.GET,
        "http://localhost:8000/api/select/search/addr",
        json={"current_page": 1, "data": rows, "total": len(rows)},
        status=200,
        match=[responses.matchers.query_param_matcher({"q": str(addr_id)})],
    )


@responses.activate
def test_http_addr_chain_walks_every_level(tmp_path):
    _mock_addr(3, [_addr_row(3, "縣", 1280, 1367, (2, "路", 1280, 1367))])
    _mock_addr(2, [_addr_row(2, "路", 1280, 1367, (1, "行省", 1280, 1367))])
    _mock_addr(1, [_addr_row(1, "行省", 1280, 1367)])
    label = _http_resolver(tmp_path).label_for("addr", 3)
    assert label["label"] == "縣"
    chain = next(l for l in label["lines"] if l.startswith("隸屬："))
    assert chain.index("行省") < chain.index("路")


@responses.activate
def test_http_addr_chain_renders_the_same_node_shape_as_the_snapshot(tmp_path):
    """The two sources are meant to be substitutable in the page, not just in type."""
    _mock_addr(3, [_addr_row(3, "縣", 1280, 1367, (2, "路", 1280, 1367))])
    _mock_addr(2, [_addr_row(2, "路", 1280, 1367)])
    chain = next(
        l for l in _http_resolver(tmp_path).label_for("addr", 3)["lines"]
        if l.startswith("隸屬：")
    )
    assert chain == "隸屬：2 路 1280~1367"


@responses.activate
def test_http_addr_chain_stops_at_the_unknown_root(tmp_path):
    _mock_addr(3, [_addr_row(3, "縣", 1280, 1367, (0, "未詳", 0, 0))])
    label = _http_resolver(tmp_path).label_for("addr", 3)
    assert not any(l.startswith("隸屬：") for l in label.get("lines", []))


@responses.activate
def test_http_addr_chain_flags_distinct_parents_only(tmp_path):
    """Two rows naming the SAME parent are one parent listed twice, not a fork."""
    same = [
        _addr_row(3, "縣", 1280, 1367, (2, "路", 1280, 1300)),
        _addr_row(3, "縣", 1280, 1367, (2, "路", 1300, 1367)),
    ]
    _mock_addr(3, same)
    _mock_addr(2, [_addr_row(2, "路", 1280, 1367)])
    label = _http_resolver(tmp_path).label_for("addr", 3)
    assert not any("多個隸屬對象" in l for l in label["lines"])


@responses.activate
def test_http_addr_chain_survives_a_cycle(tmp_path):
    _mock_addr(3, [_addr_row(3, "甲", 1, 2, (4, "乙", 1, 2))])
    _mock_addr(4, [_addr_row(4, "乙", 1, 2, (3, "甲", 1, 2))])
    label = _http_resolver(tmp_path).label_for("addr", 3)
    assert label["label"] == "甲"


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "3 Addr 縣 1280~1367",                 # no fragment at all
        "3 Addr 縣 [[",                        # unterminated
        "3 Addr 縣 [[]]",                      # empty
        "3 Addr 縣 [[未詳 something]]",         # non-numeric id
    ],
)
@responses.activate
def test_http_addr_chain_refuses_a_malformed_belongs_fragment(tmp_path, text):
    """An oddly-shaped site-UI response must yield NO chain, not a pseudo-parent.
    Upstream does not guarantee this endpoint's shape (API.md 14.4), which is exactly
    why it has to fail closed."""
    row = _addr_row(3, "縣", 1280, 1367)
    row["text"] = text
    _mock_addr(3, [row])
    label = _http_resolver(tmp_path).label_for("addr", 3)
    assert label["label"] == "縣"
    assert not any(l.startswith("隸屬：") for l in label.get("lines", []))


@responses.activate
def test_http_office_resolves_with_dynasty_and_type_tree(tmp_path):
    responses.add(
        responses.GET,
        "http://localhost:8000/api/select/search/office",
        json={"data": [{"c_office_id": 63057, "c_dy": 18, "c_office_pinyin": "xian wei",
                        "c_office_chn": "縣尉", "c_office_chn_alt": None,
                        "c_office_trans": "District Defender"}]},
        status=200,
    )
    responses.add(
        responses.GET, "http://localhost:8000/api/select/dynasty",
        json=[{"c_dy": 18, "c_dynasty": "Yuan", "c_dynasty_chn": "元",
               "c_start": 1234, "c_end": 1367}], status=200,
    )
    responses.add(
        responses.GET, "http://localhost:8000/api/OFFICE_CODE_TYPE_REL",
        json=[{"c_office_id": 63057, "c_office_tree_id": "181003"}], status=200,
    )
    responses.add(
        responses.GET, "http://localhost:8000/api/OFFICE_TYPE_TREE",
        json=[{"c_office_type_node_id": "181003", "c_office_type_desc": "Xian",
               "c_office_type_desc_chn": "諸縣門", "c_parent_id": "18"},
              {"c_office_type_node_id": "18", "c_office_type_desc": "Yuan",
               "c_office_type_desc_chn": "元朝", "c_parent_id": "0"},
              {"c_office_type_node_id": "0", "c_office_type_desc": "All",
               "c_office_type_desc_chn": "所有門類", "c_parent_id": "0"}],
        status=200,
    )
    label = _http_resolver(tmp_path).label_for("office", 63057)
    assert label["label"] == "縣尉"
    assert any("c_dy=18" in l and "元" in l for l in label["lines"])
    type_line = next(l for l in label["lines"] if l.startswith("類型"))
    assert type_line.index("所有門類") < type_line.index("諸縣門")


@responses.activate
def test_http_office_without_the_legacy_endpoints_still_names_the_office(tmp_path):
    """Those two endpoints are undocumented and may vanish; losing them must cost the
    type tree, not the label."""
    responses.add(
        responses.GET,
        "http://localhost:8000/api/select/search/office",
        json={"data": [{"c_office_id": 63057, "c_dy": 18, "c_office_pinyin": "xian wei",
                        "c_office_chn": "縣尉", "c_office_chn_alt": None,
                        "c_office_trans": "District Defender"}]},
        status=200,
    )
    responses.add(responses.GET, "http://localhost:8000/api/select/dynasty",
                  json=[], status=200)
    responses.add(responses.GET, "http://localhost:8000/api/OFFICE_CODE_TYPE_REL",
                  status=404)
    responses.add(responses.GET, "http://localhost:8000/api/OFFICE_TYPE_TREE",
                  status=404)
    label = _http_resolver(tmp_path).label_for("office", 63057)
    assert label["label"] == "縣尉"
    assert not any(l.startswith("類型") for l in label.get("lines", []))


@responses.activate
def test_http_text_prefers_the_v2_endpoint(tmp_path):
    """`/api/v2/texts` is a real v2 endpoint and the documented way to do this."""
    responses.add(
        responses.GET, "http://localhost:8000/api/v2/texts",
        json={"ok": True, "data": [{"c_textid": 27144, "c_title_chn": "全元文",
                                    "c_title": "Quan Yuan Wen", "c_text_year": 1998,
                                    "c_source": 0, "c_title_alt_chn": None}]},
        status=200,
    )
    assert _http_resolver(tmp_path).label_for("text", 27144)["label"] == "全元文"


@responses.activate
def test_the_two_sources_agree_on_the_same_code(tmp_path, snapshot):
    """The property that makes them interchangeable at all."""
    responses.add(
        responses.GET,
        "http://localhost:8000/api/select/search/entry",
        json={"data": [{"c_entry_code": 124, "c_entry_desc": "jinshi",
                        "c_entry_desc_chn": "科舉: 國子監進士"}]},
        status=200,
    )
    from_http = _http_resolver(tmp_path).label_for("entry", 124)
    from_db = CodeResolver(snapshot=snapshot).label_for("entry", 124)
    assert from_http == from_db
