"""`person_workbook`: a 人物資料標準化 template workbook -> a staging batch.

Synthetic rows only - no real case is loaded here, so deleting a case cannot break
the tool's tests (AGENTS.md, "Where code goes", rule 8).
"""

import openpyxl
import pytest

from cbdb_agent.person_workbook import (
    COLUMNS,
    SHEETS,
    CaseConfig,
    Row,
    WorkbookError,
    build_batch,
    check_template,
    clean_notes,
)
from cbdb_agent.staging import find_issues, submittable_proposals


def row(sheet, record, **values):
    values.setdefault("記錄編號", record)
    values.setdefault("標準化狀態", "已標準化")
    return Row(sheet, record, values)


def sheets(**by_sheet):
    out = {name: [] for name in SHEETS}
    for name, rows in by_sheet.items():
        out[name] = rows
    return out


def case(**kw):
    kw.setdefault("source_text", {"create": {"c_title_chn": "某年譜"}})
    return CaseConfig(batch_id="b", **kw)


MAIN = row("基本資料", "基本-001", 人物ID=1762, 人物姓名="王安石", disambiguation="exist")
DAUGHTER = row("基本資料", "基本-002", 人物ID="TMP-001", 人物姓名="王氏(王安石女)",
               姓氏="王", 名="氏(王安石女)", 性別代碼=1, 朝代代碼=15, 指數年類型代碼="00",
               disambiguation="new", 標準化狀態="待確認", 待確認事項="查無其人")


def by_id(result):
    return {p.id: p for p in result.batch.proposals}


class TestPeople:
    def test_a_tmp_person_is_a_new_create_with_its_source(self):
        result = build_batch(sheets(基本資料=[MAIN, DAUGHTER]), case())
        got = by_id(result)
        person = got["p-tmp-001"]
        assert person.person_id == "NEW" and person.operation == "create"
        assert person.changes["c_index_year_type_code"] == "00", "leading zero kept"
        assert got["p-tmp-001-src"].target_pk == {"c_textid": {"ref": "txt-source"}}
        assert "bio-1762" not in got, "an `exist` person row writes nothing"

    def test_an_open_person_row_is_a_conflict(self):
        person = by_id(build_batch(sheets(基本資料=[MAIN, DAUGHTER]), case()))["p-tmp-001"]
        assert [c.field for c in person.conflicts] == ["row"]
        assert "查無其人" in person.conflicts[0].description

    def test_a_relative_created_here_is_named_by_reference(self):
        kin = row("親屬", "親屬-001", 人物ID=1762, 親屬人物ID="TMP-001",
                  親屬關係代碼=176, disambiguation="new")
        got = by_id(build_batch(sheets(基本資料=[MAIN, DAUGHTER], 親屬=[kin]), case()))
        assert got["kin-001"].changes["c_kin_id"] == {"ref": "p-tmp-001"}
        assert got["kin-001"].target_pk["c_kin_id"] == {"ref": "p-tmp-001"}

    def test_deferring_the_new_person_holds_back_their_relatives(self):
        kin = row("親屬", "親屬-001", 人物ID=1762, 親屬人物ID="TMP-001",
                  親屬關係代碼=176, disambiguation="new")
        batch = build_batch(sheets(基本資料=[MAIN, DAUGHTER], 親屬=[kin]),
                            case(source_text={"textid": 7596})).batch
        batch.proposals[0].conflicts[0].resolution = "defer"
        assert submittable_proposals(batch) == []

    def test_a_tmp_id_with_no_person_row_is_refused(self):
        kin = row("親屬", "親屬-001", 人物ID=1762, 親屬人物ID="TMP-009",
                  親屬關係代碼=176, disambiguation="new")
        with pytest.raises(WorkbookError, match="TMP-009"):
            build_batch(sheets(基本資料=[MAIN], 親屬=[kin]), case())

    def test_a_row_naming_an_open_person_asks_who_they_are(self):
        open_person = row("基本資料", "基本-003", 人物ID=770, 人物姓名="徐的",
                          disambiguation="exist", 標準化狀態="待確認",
                          待確認事項="據往來篇名定位")
        post = row("官名", "官名-004", 人物ID=770, 官名ID=2512, 官名朝代代碼=15,
                   disambiguation="new", 待確認事項="人物身分見 基本-003")
        got = by_id(build_batch(sheets(基本資料=[MAIN, open_person], 官名=[post]), case()))
        assert [c.field for c in got["post-004"].conflicts] == ["person"]
        assert "據往來篇名定位" in got["post-004"].conflicts[0].description


class TestUpdates:
    def test_only_the_listed_fields_and_their_year_are_written(self):
        post = row("官名", "官名-026", 人物ID=1762, 官名ID=2314, 官名朝代代碼=15,
                   任命類型代碼=88, **{"起始年(西元)": 1061, "起始年號代碼": 526,
                                     "起始年號年": 6, "起始年範圍代碼": 2},
                   備註="原文「尋除」", disambiguation="update",
                   待確認事項="CBDB 已有此記錄，可補：起始年(西元)　"
                         "與 CBDB 既有記錄不同：任命類型代碼：88／1")
        result = build_batch(
            sheets(基本資料=[MAIN], 官名=[post]),
            case(update_targets={"官名-026": {"c_office_id": 2314, "c_posting_id": 62109}}))
        update = by_id(result)["post-026"]
        assert update.operation == "update"
        assert update.target_pk == {"c_office_id": 2314, "c_posting_id": 62109}
        assert update.changes == {"c_firstyear": 1061, "c_fy_nh_code": 526,
                                  "c_fy_nh_year": 6, "c_fy_range": 2}
        assert update.conflicts == [], "keep-CBDB differences are settled, not open"
        assert any("官名-026 (CBDB kept)" in f for f in result.findings)

    def test_an_update_with_no_target_is_a_finding_not_a_guess(self):
        post = row("官名", "官名-010", 人物ID=1762, 官名ID=793, 就任狀況代碼=1,
                   disambiguation="update", 待確認事項="可補：就任狀況代碼")
        result = build_batch(sheets(基本資料=[MAIN], 官名=[post]), case())
        assert "post-010" not in by_id(result)
        assert any("no target in the case file" in f for f in result.findings)

    def test_a_field_the_api_does_not_take_is_reported(self):
        text = row("著述", "著述-002", 人物ID=1762, 文本ID=8417, 角色代碼=1,
                   **{"年(西元)": 1082}, disambiguation="update",
                   待確認事項="可補：年(西元)")
        result = build_batch(sheets(基本資料=[MAIN], 著述=[text]),
                             case(update_targets={"著述-002": {"c_textid": 8417,
                                                              "c_role_id": 1}}))
        assert "text-002" not in by_id(result)
        assert any("does not accept" in f for f in result.findings)

    def test_a_person_update_fills_only_what_is_listed(self):
        main = row("基本資料", "基本-001", 人物ID=1762, 人物姓名="王安石", 卒年月=4,
                   **{"卒年(西元)": 1086}, 備註="長的備註", disambiguation="update",
                   待確認事項="CBDB 已有此記錄，可補：卒年月")
        update = by_id(build_batch(sheets(基本資料=[main]), case()))["bio-1762"]
        assert update.changes == {"c_dy_month": 4}


class TestCreates:
    def test_an_association_gets_its_key_sentinels_and_no_genre(self):
        assoc = row("社會關係", "關係-014", 人物ID=1762, 關係人物ID=3967,
                    關係類型代碼=438, 文類代碼=1, 序號=10, disambiguation="new")
        result = build_batch(sheets(基本資料=[MAIN], 社會關係=[assoc]), case())
        changes = by_id(result)["assoc-014"].changes
        assert changes["c_text_title"] == "[n/a]"
        assert changes["c_assoc_first_year"] == -9999
        assert "c_litgenre_code" not in changes
        assert "c_sequence" not in changes, "the workbook's numbering is not CBDB's"
        assert any("c_litgenre_code" in f for f in result.findings)

    def test_a_posting_place_is_a_list(self):
        post = row("官名", "官名-024", 人物ID=1762, 官名ID=762, 官名朝代代碼=15,
                   任官地ID=12824, disambiguation="new")
        changes = by_id(build_batch(sheets(基本資料=[MAIN], 官名=[post]), case()))[
            "post-024"].changes
        assert changes["c_addr"] == [12824]
        assert changes["c_source"] == {"ref": "txt-source"}

    def test_a_text_to_be_created_is_its_own_proposal_with_a_conflict(self):
        text = row("著述", "著述-001", 人物ID=1762, 標準篇名="三經新義",
                   文本ID狀態="需新建", 文本類型代碼="010110", 角色代碼=1,
                   disambiguation="new")
        got = by_id(build_batch(sheets(基本資料=[MAIN], 著述=[text]),
                                case(source_text={"textid": 7596},
                                     new_texts={"著述-001": {"c_text_dy": 15}})))
        assert got["txt-001"].changes == {"c_title_chn": "三經新義",
                                          "c_text_type_id": "010110", "c_text_dy": 15}
        assert got["txt-001"].conflicts[0].resolution is None
        assert got["text-001"].changes["c_textid"] == {"ref": "txt-001"}
        assert got["text-001"].changes["c_source"] == 7596

    def test_the_whole_batch_is_structurally_valid(self):
        kin = row("親屬", "親屬-001", 人物ID=1762, 親屬人物ID="TMP-001",
                  親屬關係代碼=176, disambiguation="new")
        assoc = row("社會關係", "關係-008", 人物ID=1762, 關係人物ID="TMP-001",
                    關係類型代碼=44, 相關文本篇名="鄞女墓誌",
                    **{"起始年(西元)": 1047}, disambiguation="new")
        batch = build_batch(sheets(基本資料=[MAIN, DAUGHTER], 親屬=[kin],
                                   社會關係=[assoc]), case()).batch
        assert [i for i in find_issues(batch) if i.severity == "error"] == []


class TestNotes:
    def test_merge_bookkeeping_is_removed_and_prose_kept(self):
        text = ("慶曆六年（1046）揚州官滿（來源列 18），結束年據此。　"
                "【併入來源列 18】來源列 18 追述。　"
                "本列由來源列 6;18 合併而成（判定鍵：人物ID＋官名ID）。")
        assert clean_notes(text) == "慶曆六年（1046）揚州官滿，結束年據此。"

    def test_nothing_left_is_none(self):
        assert clean_notes("本列由來源列 1;2 合併而成。") is None


class TestTemplateCheck:
    def _wb(self, *rows):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "說明"
        for r in rows:
            ws.append(list(r))
        return wb

    def test_a_matching_declaration_passes(self):
        check_template(self._wb(("官名", "官名ID", "c_office_id", "整數"),
                                ("官名", "任官地ID", "POSTED_TO_ADDR_DATA.c_addr_id")))

    def test_a_column_pointed_elsewhere_is_refused(self):
        with pytest.raises(WorkbookError, match="官名.官名ID"):
            check_template(self._wb(("官名", "官名ID", "c_inst_code")))

    def test_no_instructions_sheet_is_refused(self):
        wb = openpyxl.Workbook()
        with pytest.raises(WorkbookError, match="說明"):
            check_template(wb)

    def test_every_sheet_has_a_column_map(self):
        assert set(COLUMNS) == set(SHEETS)


class TestReviewFindings:
    """The 2026-10-06 review: each test pins one finding."""

    def test_a_person_not_to_be_entered_is_not_created_and_names_of_them_fail(self):
        dropped = row("基本資料", "基本-002", 人物ID="TMP-001", 人物姓名="某",
                      disambiguation="new", 標準化狀態="不入庫", 待確認事項="泛稱")
        result = build_batch(sheets(基本資料=[MAIN, dropped]), case())
        assert "p-tmp-001" not in by_id(result)
        assert any("不入庫" in f for f in result.findings)
        kin = row("親屬", "親屬-001", 人物ID=1762, 親屬人物ID="TMP-001",
                  親屬關係代碼=176, disambiguation="new")
        with pytest.raises(WorkbookError, match="TMP-001"):
            build_batch(sheets(基本資料=[MAIN, dropped], 親屬=[kin]), case())

    def test_an_open_row_with_no_note_is_still_a_conflict(self):
        post = row("官名", "官名-030", 人物ID=1762, 官名ID=948, 官名朝代代碼=15,
                   disambiguation="new", 標準化狀態="待確認")
        got = by_id(build_batch(sheets(基本資料=[MAIN], 官名=[post]), case()))
        assert [c.field for c in got["post-030"].conflicts] == ["row"]

    def test_a_number_typed_text_code_is_refused(self):
        bad = row("基本資料", "基本-002", 人物ID="TMP-001", 人物姓名="某",
                  指數年類型代碼=1, disambiguation="new")
        with pytest.raises(WorkbookError, match="text cell"):
            build_batch(sheets(基本資料=[MAIN, bad]), case())

    def test_notes_on_the_update_path_are_cleaned(self):
        post = row("官名", "官名-010", 人物ID=1762, 官名ID=793,
                   備註="【併入來源列3】x\u3000到任。", disambiguation="update",
                   待確認事項="可補：備註")
        update = by_id(build_batch(
            sheets(基本資料=[MAIN], 官名=[post]),
            case(update_targets={"官名-010": {"c_office_id": 793,
                                             "c_posting_id": 1}})))["post-010"]
        assert update.changes == {"c_notes": "到任。"}

    def test_an_association_update_asks_about_the_reverse_row(self):
        assoc = row("社會關係", "關係-011", 人物ID=1762, 關係人物ID=1384, 關係類型代碼=14,
                    **{"起始年(西元)": 1053}, disambiguation="update",
                    待確認事項="可補：起始年(西元)")
        update = by_id(build_batch(
            sheets(基本資料=[MAIN], 社會關係=[assoc]),
            case(update_targets={"關係-011": {"c_assoc_code": 14}})))["assoc-011"]
        assert [c.field for c in update.conflicts] == ["mirror"]

    def test_a_label_that_maps_to_nothing_is_reported(self):
        post = row("官名", "官名-027", 人物ID=1762, 官名ID=1930, 就任狀況代碼=1,
                   disambiguation="update", 待確認事項="可補：就任狀況代碼、起始年；與 CBDB…")
        result = build_batch(
            sheets(基本資料=[MAIN], 官名=[post]),
            case(update_targets={"官名-027": {"c_office_id": 1930, "c_posting_id": 2}}))
        assert by_id(result)["post-027"].changes == {"c_assume_office_code": 1}
        assert any("'起始年'" in f for f in result.findings)


def test_a_text_created_since_is_cited_not_created_again():
    text = row("著述", "著述-001", 人物ID=1762, 標準篇名="三經新義",
               文本ID狀態="需新建", 角色代碼=1, disambiguation="new")
    got = by_id(build_batch(sheets(基本資料=[MAIN], 著述=[text]),
                            case(source_text={"textid": 72223},
                                 new_texts={"著述-001": {"textid": 72224}})))
    assert not [p for p in got.values() if p.resource == "text-codes"]
    assert got["text-001"].changes["c_textid"] == 72224
    assert got["text-001"].changes["c_source"] == 72223


def test_a_text_code_from_the_case_file_must_be_a_string():
    """YAML reads an unquoted 010110 as octal 4168; that must not reach TEXT_CODES."""
    with pytest.raises(WorkbookError, match="text cell"):
        build_batch(sheets(基本資料=[MAIN]),
                    case(source_text={"create": {"c_title_chn": "某", "c_text_type_id": 4168}}))


def test_rows_naming_a_refused_existing_person_are_questions():
    refused = row("基本資料", "基本-003", 人物ID=770, 人物姓名="徐的",
                  disambiguation="exist", 標準化狀態="無法對應", 待確認事項="不是此人")
    post = row("官名", "官名-004", 人物ID=770, 官名ID=2512, 官名朝代代碼=15,
               disambiguation="new")
    got = by_id(build_batch(sheets(基本資料=[MAIN, refused], 官名=[post]), case()))
    assert [c.field for c in got["post-004"].conflicts] == ["person"]


def test_every_pair_update_gets_the_mirror_question():
    assoc = row("社會關係", "關係-011", 人物ID=1762, 關係人物ID=1384, 關係類型代碼=14,
                場合代碼=6, disambiguation="update", 待確認事項="可補：場合代碼")
    update = by_id(build_batch(sheets(基本資料=[MAIN], 社會關係=[assoc]),
                               case(update_targets={"關係-011": {"c_assoc_code": 14}})))["assoc-011"]
    assert update.changes == {"c_occasion_code": 6}
    assert [c.field for c in update.conflicts] == ["mirror"]


def test_an_identity_question_survives_a_text_typed_id():
    open_person = row("基本資料", "基本-003", 人物ID=770, 人物姓名="徐的",
                      disambiguation="exist", 標準化狀態="待確認", 待確認事項="未定")
    assoc = row("社會關係", "關係-003", 人物ID=1762, 關係人物ID="770", 關係類型代碼=429,
                disambiguation="new")
    got = by_id(build_batch(sheets(基本資料=[MAIN, open_person], 社會關係=[assoc]), case()))
    assert [c.field for c in got["assoc-003"].conflicts] == ["person"]


def test_a_row_of_a_refused_tmp_person_is_a_named_error():
    dropped = row("基本資料", "基本-002", 人物ID="TMP-001", 人物姓名="某",
                  disambiguation="new", 標準化狀態="不入庫")
    alt = row("別名", "別名-010", 人物ID="TMP-001", **{"別名(標準)": "鄱陽"},
              別名類型代碼=3, disambiguation="new")
    with pytest.raises(WorkbookError, match="別名-010"):
        build_batch(sheets(基本資料=[MAIN, dropped], 別名=[alt]), case())
