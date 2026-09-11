"""`tools/salt-admin/emit_staging.py` - the Track A staging emitter.

Separate from `test_salt_admin.py` because what matters here is different: not
whether the arithmetic is right, but whether the batch this produces is the shape
`staging.py` validates and whether the rule-12 approval gate survives contact with a
generator. A review pass found four mutations of this module that the suite did not
notice, including `approved_by: None -> "auto"` and `resource: office -> offices` -
the second being the exact alias trap `AGENTS.md` rule 12 warns about, since
`offices` resolves to the *postings* sub-resource and wins the server's dispatch.
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools" / "salt-admin"))

ES = pytest.importorskip("emit_staging", reason="PyYAML not installed")

from cbdb_agent import models  # noqa: E402
from cbdb_agent.staging import StagingBatch, find_issues  # noqa: E402


def _unit(key, *, emitted=True, kind="分司", region="兩淮", short="泰州分司",
          dynasty="明代", code=19):
    return {
        "key": key, "dynasty": dynasty, "dynasty_code": code, "kind": kind,
        "region": region, "name": f"兩淮都轉運鹽使司{short}", "name_short": short,
        "emitted": emitted, "blocked": not emitted,
        "blocked_reason": None if emitted else "test",
        "addresses": [{"seat": {"raw": "泰州"}, "first": 1368, "last": 1643}],
        "office": {
            "name": f"兩淮都轉運鹽使司{short}", "name_alt": short,
            "translation": "Lianghuai Salt Distribution Commission, Taizhou Branch Office",
            "translation_alt": None,
            "pinyin": "liang huai du zhuan yun yan shi si tai zhou fen si",
            "pinyin_alt": None, "dynasty_code": code,
            "type_ids": ["19072801"], "source_id": 0, "pages": None,
            "notes": "治所 泰州（ADDR_CODES 4631） 1368–1643\n出處：…",
        },
    }


def _dataset(units, **kw):
    d = {
        "schema_version": ES.DATASET_SCHEMA,
        "source": {"xlsx": "test.xlsx"},
        "findings": [],
        "stats": {"office_creates": sum(1 for u in units if u["emitted"])},
        "units": units,
    }
    d.update(kw)
    return d


@pytest.fixture
def batch():
    return ES.build_batch(_dataset([_unit("salt:ming:兩淮:泰州分司")]), "b1")


class TestApprovalGate:
    def test_approved_by_is_null_on_every_proposal(self, batch):
        """The rule-12 gate. Kills `approved_by: None -> "auto"`.

        `approved_by` records that a named human decided to write global reference
        data. Anything this script could put there would be a forgery.
        """
        assert batch["proposals"]
        for p in batch["proposals"]:
            assert p["approved_by"] is None

    def test_the_emitter_never_writes_a_name(self):
        src = (REPO / "tools" / "salt-admin" / "emit_staging.py").read_text(
            encoding="utf-8")
        assert '"approved_by": None' in src
        assert "approved_by\": \"" not in src

    def test_validate_refuses_the_batch_and_says_why(self, batch):
        issues = find_issues(StagingBatch.model_validate(batch))
        errors = [i for i in issues if i.severity == "error"]
        assert len(errors) == len(batch["proposals"])
        assert all("approved_by" in e.message for e in errors)


class TestEnvelopeShape:
    def test_the_resource_is_office_not_offices(self, batch):
        """Kills `office -> offices`.

        `offices` is a server-side alias for BOTH the office aggregate and the
        postings sub-resource, and postings wins the dispatch - so this typo writes
        a person's appointment record instead of an office code, and slips the
        approval gate on the way (AGENTS.md rule 12).
        """
        for p in batch["proposals"]:
            assert p["resource"] == "office"

    def test_no_target_pk_on_a_create(self, batch):
        # c_office_id is server-assigned. `{}` passes staging's check silently, so
        # the key is omitted rather than emptied.
        for p in batch["proposals"]:
            assert "target_pk" not in p

    def test_person_id_is_the_global_reference_convention(self, batch):
        for p in batch["proposals"]:
            assert p["person_id"] == 0

    def test_changes_are_exactly_the_aggregate_field_set(self, batch):
        for p in batch["proposals"]:
            assert set(p["changes"]) == models._OFFICE_AGGREGATE_FIELDS

    def test_type_ids_stay_strings(self, batch):
        """Kills `[int(t) for t in type_ids]`.

        OFFICE_TYPE_TREE node ids are varchar and the leading zeros are significant;
        `int("06091204")` is a different node, and `models.list_fields` would let it
        through.
        """
        for p in batch["proposals"]:
            assert p["changes"]["type_ids"]
            assert all(isinstance(t, str) for t in p["changes"]["type_ids"])

    def test_scalars_have_the_types_the_server_validates(self, batch):
        for p in batch["proposals"]:
            assert isinstance(p["changes"]["dynasty_code"], int)
            assert p["changes"]["source_id"] == 0
            assert isinstance(p["changes"]["source_id"], int)

    def test_every_proposal_has_an_id_quote_and_confidence(self, batch):
        ids = [p["id"] for p in batch["proposals"]]
        assert len(set(ids)) == len(ids)
        for p in batch["proposals"]:
            assert p["source_quote"] and p["confidence"] in ("high", "medium", "low")


class TestExclusion:
    def test_an_unemitted_unit_produces_no_proposal(self):
        """Kills `if u["emitted"]` -> every unit."""
        ds = _dataset([_unit("a"), _unit("b", emitted=False, short="通州分司")])
        batch = ES.build_batch(ds, "b1")
        assert len(batch["proposals"]) == 1
        assert "通州分司" not in str(batch["proposals"])

    def test_an_excluded_unit_is_named_in_the_batch_preamble(self):
        ds = _dataset([_unit("a"), _unit("b", emitted=False, short="通州分司")])
        batch = ES.build_batch(ds, "b1")
        assert "通州分司" in batch["source_excerpt"]


class TestTrackAIsDropped:
    """The emitter must not still offer to run.

    Track A was dropped on 2026-09-11: a separate import had already covered the
    salt post titles, and Ning Hao's list names the institutions, which belong in
    the place-name tables. `OFFICE_CODES` has no delete path, so an accidental run
    producing 49 office creates would not be undoable. `build_batch` stays
    importable - the tests above are what records the decision - but nothing
    writes a file any more.
    """

    def test_main_refuses_and_says_what_to_run_instead(self, tmp_path, capsys):
        import json
        p = tmp_path / "dataset.json"
        p.write_text(json.dumps(_dataset([_unit("salt:ming:兩淮:泰州分司")]),
                                ensure_ascii=False), encoding="utf-8")
        rc = ES.main(["--dataset", str(p), "--batch-id", "b",
                      "--staging-root", str(tmp_path)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "dropped on 2026-09-11" in err
        assert "emit_addresses.py" in err

    def test_it_writes_nothing_at_all(self, tmp_path):
        import json
        p = tmp_path / "dataset.json"
        p.write_text(json.dumps(_dataset([_unit("salt:ming:兩淮:泰州分司")]),
                                ensure_ascii=False), encoding="utf-8")
        ES.main(["--dataset", str(p), "--batch-id", "b",
                 "--staging-root", str(tmp_path)])
        assert not (tmp_path / "b").exists()

    def test_the_docstring_no_longer_calls_track_b_unsubmittable(self):
        """It used to say "the address rows have no API path at all", which is the
        claim docs/11's header supersedes and the claim that would send a reader
        back to the SQL script."""
        src = (REPO / "tools" / "salt-admin" / "emit_staging.py").read_text(
            encoding="utf-8")
        assert "no API path at all" not in src
        assert "DROPPED on 2026-09-11" in src
