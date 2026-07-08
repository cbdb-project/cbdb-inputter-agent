import json

from freezegun import freeze_time

from cbdb_agent.audit_log import AuditLog, new_correlation_id


def test_record_writes_one_jsonl_line(tmp_path):
    log = AuditLog(tmp_path)
    cid = new_correlation_id()
    log.record(
        correlation_id=cid,
        method="POST",
        url="http://localhost:8000/api/v2/create",
        request_payload={"resource": "basicinformation"},
        dry_run=False,
        resource="basicinformation",
        operation="create",
        mode="direct",
        status_code=200,
        response_payload={"ok": True},
        operation_id="01ABC",
    )
    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["correlation_id"] == cid
    assert record["resource"] == "basicinformation"
    assert record["operation_id"] == "01ABC"
    assert record["dry_run"] is False


def test_multiple_records_append_not_overwrite(tmp_path):
    log = AuditLog(tmp_path)
    for i in range(3):
        log.record(
            correlation_id=new_correlation_id(),
            method="GET",
            url=f"http://localhost:8000/api/v2/get?i={i}",
            dry_run=False,
        )
    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3


def test_dry_run_record_flagged(tmp_path):
    log = AuditLog(tmp_path)
    record = log.record(
        correlation_id=new_correlation_id(),
        method="POST",
        url="http://localhost:8000/api/v2/create",
        request_payload={"resource": "basicinformation"},
        dry_run=True,
        response_payload={"dry_run": True, "sent": False},
    )
    assert record.dry_run is True
    assert record.status_code is None


def test_correlation_ids_are_unique():
    ids = {new_correlation_id() for _ in range(100)}
    assert len(ids) == 100


@freeze_time("2026-07-08T12:34:56+00:00")
def test_timestamp_is_iso8601_utc(tmp_path):
    log = AuditLog(tmp_path)
    record = log.record(
        correlation_id=new_correlation_id(),
        method="GET",
        url="http://localhost:8000/api/v2/get",
        dry_run=False,
    )
    assert record.timestamp == "2026-07-08T12:34:56+00:00"
    log_file = list(tmp_path.glob("2026-07-08.jsonl"))
    assert len(log_file) == 1  # filename also derived from the frozen date


def test_log_dir_created_if_missing(tmp_path):
    nested = tmp_path / "does" / "not" / "exist"
    log = AuditLog(nested)
    log.record(
        correlation_id=new_correlation_id(),
        method="GET",
        url="http://localhost:8000/api/v2/get",
        dry_run=False,
    )
    assert list(nested.glob("*.jsonl"))
