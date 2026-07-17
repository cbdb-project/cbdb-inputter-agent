import json

import pytest
import responses
import yaml

from cbdb_agent import cli


def write_env(path, **overrides):
    defaults = {
        "CBDB_API_BASE_URL": "http://localhost:8000",
        "CBDB_API_TOKEN": "test-token",
        "CBDB_DRY_RUN": "true",
        "CBDB_CONFIRM_PROD": "",
        "CBDB_MAX_REQUESTS_PER_MINUTE": "6000",
        "CBDB_LOCAL_AUDIT_LOG_DIR": str(path / "logs"),
    }
    defaults.update(overrides)
    env_path = path / ".env"
    env_path.write_text("\n".join(f"{k}={v}" for k, v in defaults.items()), encoding="utf-8")
    return env_path


def write_input_json(path, records):
    input_path = path / "input.json"
    input_path.write_text(json.dumps(records), encoding="utf-8")
    return input_path


def test_validate_input_clean_batch_returns_zero(tmp_path, capsys):
    input_path = write_input_json(
        tmp_path,
        [
            {
                "id": "p1",
                "resource": "basicinformation",
                "operation": "create",
                "person_id": 900001,
                "changes": {"c_name_chn": "柳宗元"},
            }
        ],
    )
    rc = cli.main(["validate", "--input", str(input_path)])
    assert rc == 0
    assert "no issues found" in capsys.readouterr().out


def test_validate_input_bad_field_returns_one(tmp_path, capsys):
    input_path = write_input_json(
        tmp_path,
        [
            {
                "id": "p1",
                "resource": "basicinformation",
                "operation": "create",
                "person_id": 900001,
                "changes": {"c_not_a_real_field": "x"},
            }
        ],
    )
    rc = cli.main(["validate", "--input", str(input_path)])
    assert rc == cli.EXIT_VALIDATION_ERROR
    assert "error" in capsys.readouterr().out


def test_validate_staging_unresolved_conflict_still_returns_zero(tmp_path, capsys):
    staging_path = tmp_path / "proposal.yaml"
    staging_path.write_text(
        yaml.safe_dump(
            {
                "batch_id": "b1",
                "proposals": [
                    {
                        "id": "p1",
                        "resource": "basicinformation",
                        "operation": "create",
                        "person_id": "NEW",
                        "changes": {"c_name_chn": "x"},
                        "source_quote": "x",
                        "confidence": "high",
                        "conflicts": [
                            {"id": "c1", "field": "c_name_chn", "description": "x", "options": [], "resolution": None}
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    # --env points at a nonexistent path so this test doesn't depend on (or try
    # to load) any real .env - preview.md's Tier 2 live diff isn't what this
    # test is about; see test_validate_staging_writes_preview_md_without_env.
    rc = cli.main(
        ["validate", "--staging", str(staging_path), "--env", str(tmp_path / "does-not-exist.env")]
    )
    # unresolved conflicts are reported but don't fail `validate` per docs/03 sec 2.5
    assert rc == 0
    out = capsys.readouterr().out
    assert "unresolved_conflict" in out


def _write_kinship_staging(tmp_path):
    staging_path = tmp_path / "proposal.yaml"
    staging_path.write_text(
        yaml.safe_dump(
            {
                "batch_id": "b1",
                "proposals": [
                    {
                        "id": "p1",
                        "resource": "kinship",
                        "operation": "update",
                        "person_id": 900001,
                        "target_pk": {"c_kin_id": 900002, "c_kin_code": 243},
                        "changes": {"c_notes": "new note"},
                        "source_quote": "x",
                        "confidence": "high",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return staging_path


def test_validate_staging_writes_preview_md_without_env(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    staging_path = _write_kinship_staging(tmp_path)
    # --env points at a path with nothing there: load_config() raises ConfigError
    # (missing CBDB_API_BASE_URL) - _write_preview must swallow it and still
    # write a Tier-1-only preview rather than letting `validate` crash.
    rc = cli.main(["validate", "--staging", str(staging_path), "--env", str(tmp_path / "does-not-exist.env")])
    assert rc == 0
    preview_path = staging_path.parent / "preview.md"
    assert preview_path.exists()
    content = preview_path.read_text(encoding="utf-8")
    assert "b1" in content
    assert "not fetched — offline preview" in content
    assert "Preview written to" in capsys.readouterr().out


@responses.activate
def test_validate_staging_writes_preview_md_with_live_diff(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env_path = write_env(tmp_path)
    staging_path = _write_kinship_staging(tmp_path)
    responses.add(
        responses.GET,
        "http://localhost:8000/api/v2/get",
        json={"ok": True, "result": {"row": {"c_notes": "old note"}}},
        status=200,
    )
    rc = cli.main(["validate", "--staging", str(staging_path), "--env", str(env_path)])
    assert rc == 0
    content = (staging_path.parent / "preview.md").read_text(encoding="utf-8")
    assert "old note" in content
    assert "new note" in content


def test_validate_staging_preview_falls_back_when_env_is_broken(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Malformed .env (missing the required CBDB_API_BASE_URL) - load_config()
    # raises ConfigError; _write_preview must swallow it and still write a
    # Tier-1-only preview rather than letting `validate` crash.
    env_path = write_env(tmp_path, CBDB_API_BASE_URL="")
    staging_path = _write_kinship_staging(tmp_path)
    rc = cli.main(["validate", "--staging", str(staging_path), "--env", str(env_path)])
    assert rc == 0
    content = (staging_path.parent / "preview.md").read_text(encoding="utf-8")
    assert "not fetched — offline preview" in content


def test_validate_input_does_not_write_preview(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    input_path = write_input_json(
        tmp_path,
        [
            {
                "id": "p1",
                "resource": "basicinformation",
                "operation": "create",
                "person_id": 900001,
                "changes": {"c_name_chn": "x"},
            }
        ],
    )
    rc = cli.main(["validate", "--input", str(input_path)])
    assert rc == 0
    assert not (tmp_path / "preview.md").exists()


def test_submit_requires_staging_or_input(capsys):
    with pytest.raises(SystemExit):
        cli.main(["submit"])


@responses.activate
def test_submit_dry_run_via_cli_flag_never_calls_network(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env_path = write_env(tmp_path, CBDB_DRY_RUN="false", CBDB_CONFIRM_PROD="http://localhost:8000")
    input_path = write_input_json(
        tmp_path,
        [
            {
                "id": "p1",
                "resource": "basicinformation",
                "operation": "create",
                "person_id": 900001,
                "changes": {"c_name_chn": "x"},
            }
        ],
    )
    # No responses registered - a real call would raise ConnectionError.
    rc = cli.main(
        ["submit", "--input", str(input_path), "--env", str(env_path), "--dry-run"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry_run=True" in out
    # dry-run must not archive the input file - it's still there for iteration.
    assert input_path.exists()


@responses.activate
def test_submit_real_run_archives_input_file(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env_path = write_env(tmp_path, CBDB_DRY_RUN="false", CBDB_CONFIRM_PROD="http://localhost:8000")
    input_path = write_input_json(
        tmp_path,
        [
            {
                "id": "p1",
                "resource": "basicinformation",
                "operation": "create",
                "person_id": 900001,
                "changes": {"c_name_chn": "x"},
            }
        ],
    )
    responses.add(
        responses.POST,
        "http://localhost:8000/api/v2/create",
        json={"ok": True, "result": {"pk": {"c_personid": 900001}}},
        status=200,
    )
    rc = cli.main(["submit", "--input", str(input_path), "--env", str(env_path)])
    assert rc == 0
    assert not input_path.exists()  # moved out of the original location

    processed_dir = tmp_path / "data" / "processed"
    subdirs = list(processed_dir.iterdir())
    assert len(subdirs) == 1
    assert (subdirs[0] / "input.json").exists()
    results = json.loads((subdirs[0] / "results.json").read_text(encoding="utf-8"))
    assert results[0]["status"] == "success"


@responses.activate
def test_submit_reattempt_does_not_overwrite_previous_archive(tmp_path, monkeypatch):
    """Regression test: resubmitting the same batch_id must not silently clobber
    a previous attempt's results.json/source file."""
    monkeypatch.chdir(tmp_path)
    env_path = write_env(tmp_path, CBDB_DRY_RUN="false", CBDB_CONFIRM_PROD="http://localhost:8000")

    records = [
        {
            "id": "p1",
            "resource": "basicinformation",
            "operation": "create",
            "person_id": 900001,
            "changes": {"c_name_chn": "x"},
        }
    ]
    # First attempt.
    input_path_1 = tmp_path / "input.json"
    input_path_1.write_text(json.dumps(records), encoding="utf-8")
    responses.add(
        responses.POST,
        "http://localhost:8000/api/v2/create",
        json={"ok": True, "result": {}},
        status=200,
    )
    rc1 = cli.main(["submit", "--input", str(input_path_1), "--env", str(env_path)])
    assert rc1 == 0

    # Second attempt reuses the same batch_id (input.json's own path, per
    # load_input_batch's default batch_id=path) - recreate the source file since
    # it was moved away by the first attempt's archiving.
    input_path_2 = tmp_path / "input.json"
    input_path_2.write_text(json.dumps(records), encoding="utf-8")
    responses.add(
        responses.POST,
        "http://localhost:8000/api/v2/create",
        json={"ok": True, "result": {}},
        status=200,
    )
    rc2 = cli.main(["submit", "--input", str(input_path_2), "--env", str(env_path)])
    assert rc2 == 0

    processed_dir = tmp_path / "data" / "processed"
    subdirs = sorted(p.name for p in processed_dir.iterdir())
    assert len(subdirs) == 2  # two distinct archive directories, neither overwritten
    for name in subdirs:
        assert (processed_dir / name / "input.json").exists()
        assert (processed_dir / name / "results.json").exists()


@responses.activate
def test_archive_rejects_dot_only_batch_id_path_traversal(tmp_path, monkeypatch):
    """Regression test: a batch_id of '..' must not let the archive escape
    data/processed/ via filesystem dot-segment resolution."""
    monkeypatch.chdir(tmp_path)
    env_path = write_env(tmp_path, CBDB_DRY_RUN="false", CBDB_CONFIRM_PROD="http://localhost:8000")

    staging_path = tmp_path / "proposal.yaml"
    staging_path.write_text(
        yaml.safe_dump(
            {
                "batch_id": "..",
                "proposals": [
                    {
                        "id": "p1",
                        "resource": "basicinformation",
                        "operation": "create",
                        "person_id": 900001,
                        "changes": {"c_name_chn": "x"},
                        "source_quote": "x",
                        "confidence": "high",
                        "conflicts": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    responses.add(
        responses.POST,
        "http://localhost:8000/api/v2/create",
        json={"ok": True, "result": {}},
        status=200,
    )
    rc = cli.main(["submit", "--staging", str(staging_path), "--env", str(env_path)])
    assert rc == 0

    processed_dir = tmp_path / "data" / "processed"
    # Must have archived INSIDE data/processed/, not escaped to data/ or tmp_path root.
    assert processed_dir.exists()
    archived_files = list(processed_dir.rglob("proposal.yaml"))
    assert len(archived_files) == 1
    assert processed_dir in archived_files[0].parents


@responses.activate
def test_submit_structural_error_blocks_before_any_network_call(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env_path = write_env(tmp_path, CBDB_DRY_RUN="false", CBDB_CONFIRM_PROD="http://localhost:8000")
    input_path = write_input_json(
        tmp_path,
        [
            {
                "id": "p1",
                "resource": "basicinformation",
                "operation": "create",
                "person_id": 900001,
                "changes": {"c_not_a_real_field": "x"},
            }
        ],
    )
    rc = cli.main(["submit", "--input", str(input_path), "--env", str(env_path)])
    assert rc == cli.EXIT_VALIDATION_ERROR
    assert len(responses.calls) == 0
    assert input_path.exists()  # never archived - nothing was submitted
