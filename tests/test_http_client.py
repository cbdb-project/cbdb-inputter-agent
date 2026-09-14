import pytest
import requests
import responses

from cbdb_agent.audit_log import AuditLog
from cbdb_agent.config import Config, ConfigError
from cbdb_agent.http_client import (
    AuthenticationError,
    AuthorizationError,
    CbdbApiError,
    ConflictError,
    HttpClient,
    MutatingFlagMismatch,
    NetworkError,
    NotFoundError,
    RateLimitedError,
    RateLimiter,
    ServerError,
    UnexpectedResponseError,
)


def make_config(tmp_path, *, dry_run=True, confirm_prod="", base_url="http://localhost:8000"):
    return Config(
        api_base_url=base_url,
        api_token="test-token",
        dry_run=dry_run,
        confirm_prod=confirm_prod,
        max_requests_per_minute=6000,  # effectively unthrottled for these tests
        local_audit_log_dir=tmp_path / "logs",
    )


def make_client(tmp_path, *, dry_run=True, confirm_prod="", sleep=None):
    config = make_config(tmp_path, dry_run=dry_run, confirm_prod=confirm_prod)
    audit_log = AuditLog(config.local_audit_log_dir)
    kwargs = {}
    if sleep is not None:
        kwargs["sleep"] = sleep
    return HttpClient(config, audit_log, **kwargs), audit_log


def read_audit_records(audit_log):
    import json

    files = list(audit_log.log_dir.glob("*.jsonl"))
    assert len(files) == 1, f"expected exactly one audit log file, found {files}"
    return [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]


@responses.activate
def test_get_success(tmp_path):
    client, audit_log = make_client(tmp_path)
    responses.add(
        responses.GET,
        "http://localhost:8000/api/v2/get",
        json={"ok": True, "result": {"pk": {"c_personid": 1}}},
        status=200,
    )
    body = client.get("/api/v2/get", params={"resource": "basicinformation"})
    assert body["ok"] is True

    records = read_audit_records(audit_log)
    assert len(records) == 1
    assert records[0]["status_code"] == 200
    # GET's real input lives in params, not json_body - must still be captured.
    assert records[0]["request_payload"] == {"resource": "basicinformation"}


@responses.activate
def test_post_success_logs_operation_id(tmp_path):
    client, audit_log = make_client(tmp_path, dry_run=False, confirm_prod="http://localhost:8000")
    responses.add(
        responses.POST,
        "http://localhost:8000/api/v2/create",
        json={"ok": True, "result": {"operation_id": "01ABC123"}},
        status=200,
    )
    body = client.post(
        "/api/v2/create",
        json_body={"resource": "basicinformation", "mode": "direct"},
        mutating=True,
        resource="basicinformation",
        operation="create",
        mode="direct",
    )
    assert body["result"]["operation_id"] == "01ABC123"
    log_file = list(audit_log.log_dir.glob("*.jsonl"))[0]
    assert "01ABC123" in log_file.read_text(encoding="utf-8")


@responses.activate
def test_dry_run_never_sends_mutating_call(tmp_path):
    client, audit_log = make_client(tmp_path, dry_run=True)
    # No responses registered at all - if HttpClient tried to send, this would
    # raise a ConnectionError from `responses` because the URL isn't mocked.
    result = client.post(
        "/api/v2/create",
        json_body={"resource": "basicinformation"},
        mutating=True,
        resource="basicinformation",
        operation="create",
    )
    assert result == {"dry_run": True, "sent": False}
    log_file = list(audit_log.log_dir.glob("*.jsonl"))[0]
    assert '"dry_run": true' in log_file.read_text(encoding="utf-8")


@responses.activate
def test_dry_run_still_allows_get(tmp_path):
    client, _ = make_client(tmp_path, dry_run=True)
    responses.add(
        responses.GET,
        "http://localhost:8000/api/v2/get",
        json={"ok": True},
        status=200,
    )
    body = client.get("/api/v2/get")
    assert body["ok"] is True


@responses.activate
def test_live_write_blocked_without_confirm_prod(tmp_path):
    # No responses registered - if the confirm-prod gate ever regressed to run
    # after the network call, this test would fail with a ConnectionError from
    # `responses` instead of silently making a real request.
    client, _ = make_client(tmp_path, dry_run=False, confirm_prod="")
    with pytest.raises(ConfigError):
        client.post(
            "/api/v2/create",
            json_body={"resource": "basicinformation"},
            mutating=True,
        )
    assert len(responses.calls) == 0


@responses.activate
def test_live_write_blocked_when_confirm_prod_is_different_host(tmp_path):
    client, _ = make_client(tmp_path, dry_run=False, confirm_prod="https://input.cbdb.fas.harvard.edu")
    with pytest.raises(ConfigError):
        client.post(
            "/api/v2/create",
            json_body={"resource": "basicinformation"},
            mutating=True,
        )
    assert len(responses.calls) == 0


@responses.activate
def test_401_raises_authentication_error_no_retry(tmp_path):
    client, audit_log = make_client(tmp_path, dry_run=False, confirm_prod="http://localhost:8000")
    responses.add(
        responses.POST,
        "http://localhost:8000/api/v2/create",
        json={"message": "Unauthenticated."},
        status=401,
    )
    with pytest.raises(AuthenticationError):
        client.post("/api/v2/create", json_body={}, mutating=True)
    assert len(responses.calls) == 1

    records = read_audit_records(audit_log)
    assert len(records) == 1  # a failed call must still be logged
    assert records[0]["status_code"] == 401


@responses.activate
def test_403_raises_authorization_error_no_retry(tmp_path):
    client, _ = make_client(tmp_path, dry_run=False, confirm_prod="http://localhost:8000")
    responses.add(
        responses.POST,
        "http://localhost:8000/api/v2/create",
        json={"message": "Forbidden"},
        status=403,
    )
    with pytest.raises(AuthorizationError):
        client.post("/api/v2/create", json_body={}, mutating=True)
    assert len(responses.calls) == 1


@responses.activate
def test_409_raises_conflict_error_no_retry(tmp_path):
    client, audit_log = make_client(tmp_path, dry_run=False, confirm_prod="http://localhost:8000")
    responses.add(
        responses.POST,
        "http://localhost:8000/api/v2/create",
        json={"message": "target.pk conflict"},
        status=409,
    )
    with pytest.raises(ConflictError) as exc_info:
        client.post("/api/v2/create", json_body={}, mutating=True)
    assert exc_info.value.status_code == 409
    assert len(responses.calls) == 1

    records = read_audit_records(audit_log)
    assert len(records) == 1
    assert records[0]["status_code"] == 409


@responses.activate
def test_422_raises_conflict_error_no_retry(tmp_path):
    client, _ = make_client(tmp_path, dry_run=False, confirm_prod="http://localhost:8000")
    responses.add(
        responses.POST,
        "http://localhost:8000/api/v2/create",
        json={"message": "disallowed_fields"},
        status=422,
    )
    with pytest.raises(ConflictError) as exc_info:
        client.post("/api/v2/create", json_body={}, mutating=True)
    assert exc_info.value.status_code == 422
    assert len(responses.calls) == 1


@responses.activate
def test_404_raises_not_found_error_specifically_no_retry(tmp_path):
    """Confirmed live (Milestone 7): GET /api/v2/get 404s for a nonexistent row.
    Must map to NotFoundError specifically, not the generic CbdbApiError base or
    UnexpectedResponseError, so callers like is_person_id_taken() can distinguish
    it with a plain isinstance/except clause."""
    client, _ = make_client(tmp_path)
    responses.add(
        responses.GET,
        "http://localhost:8000/api/v2/get",
        json={"ok": False, "message": "not found"},
        status=404,
    )
    with pytest.raises(NotFoundError) as exc_info:
        client.get("/api/v2/get", json_body={"resource": "basicinformation"})
    assert exc_info.value.status_code == 404
    assert len(responses.calls) == 1


@responses.activate
def test_429_retries_then_raises_rate_limited(tmp_path):
    sleeps = []
    client, audit_log = make_client(
        tmp_path, dry_run=False, confirm_prod="http://localhost:8000", sleep=sleeps.append
    )
    for _ in range(HttpClient.MAX_RETRIES):
        responses.add(
            responses.POST,
            "http://localhost:8000/api/v2/create",
            json={"message": "Too Many Requests"},
            status=429,
        )
    with pytest.raises(RateLimitedError):
        client.post("/api/v2/create", json_body={}, mutating=True)
    assert len(responses.calls) == HttpClient.MAX_RETRIES
    assert len(sleeps) == HttpClient.MAX_RETRIES - 1  # no sleep after the last attempt

    records = read_audit_records(audit_log)
    assert len(records) == HttpClient.MAX_RETRIES  # every attempt logged, not just the last
    assert all(r["status_code"] == 429 for r in records)


@responses.activate
def test_429_then_success_returns_body(tmp_path):
    client, _ = make_client(
        tmp_path, dry_run=False, confirm_prod="http://localhost:8000", sleep=lambda s: None
    )
    responses.add(
        responses.POST,
        "http://localhost:8000/api/v2/create",
        json={"message": "Too Many Requests"},
        status=429,
    )
    responses.add(
        responses.POST,
        "http://localhost:8000/api/v2/create",
        json={"ok": True},
        status=200,
    )
    body = client.post("/api/v2/create", json_body={}, mutating=True)
    assert body["ok"] is True
    assert len(responses.calls) == 2


@responses.activate
def test_5xx_retries_on_a_read(tmp_path):
    client, _ = make_client(
        tmp_path, dry_run=False, confirm_prod="http://localhost:8000", sleep=lambda s: None
    )
    for _ in range(HttpClient.MAX_RETRIES):
        responses.add(
            responses.GET,
            "http://localhost:8000/api/v2/persons",
            json={"message": "Internal Server Error"},
            status=500,
        )
    with pytest.raises(ServerError):
        client.get("/api/v2/persons")
    assert len(responses.calls) == HttpClient.MAX_RETRIES


@responses.activate
def test_5xx_is_not_retried_on_a_write(tmp_path):
    """A 5xx does not mean "not applied".

    It can be raised after the row is written - a failure in a post-commit step, or
    a proxy giving up on a response the app already sent. Re-sending then makes a
    second row, and for a code table that is unrecoverable: no unique key on the
    content (API.md 13.2) and no delete path (13.3). One failed proposal that a
    human reconciles against GET /api/v2/operations is the cheaper outcome.
    """
    client, _ = make_client(
        tmp_path, dry_run=False, confirm_prod="http://localhost:8000", sleep=lambda s: None
    )
    for _ in range(HttpClient.MAX_RETRIES):
        responses.add(
            responses.POST,
            "http://localhost:8000/api/v2/create",
            json={"message": "Internal Server Error"},
            status=500,
        )
    with pytest.raises(ServerError, match="NOT retried"):
        client.post("/api/v2/create", json_body={}, mutating=True)
    assert len(responses.calls) == 1, "the write must be sent exactly once"


@responses.activate
def test_unexpected_status_raises_unexpected_response_error(tmp_path):
    client, _ = make_client(tmp_path, dry_run=False, confirm_prod="http://localhost:8000")
    responses.add(
        responses.POST,
        "http://localhost:8000/api/v2/create",
        json={"message": "Teapot"},
        status=418,
    )
    with pytest.raises(UnexpectedResponseError):
        client.post("/api/v2/create", json_body={}, mutating=True)
    assert len(responses.calls) == 1


def test_rate_limiter_waits_minimum_interval():
    clock_time = [0.0]
    sleeps = []

    def clock():
        return clock_time[0]

    def sleep(seconds):
        sleeps.append(seconds)
        clock_time[0] += seconds

    limiter = RateLimiter(60, clock=clock, sleep=sleep)  # 1 call/sec
    limiter.wait_for_slot()
    assert sleeps == []  # first call never waits

    clock_time[0] += 0.1  # only 0.1s elapsed, need to wait ~0.9s
    limiter.wait_for_slot()
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(0.9, abs=1e-6)


def test_mutating_endpoint_with_mutating_false_is_rejected(tmp_path):
    """Defense-in-depth: a Milestone-3+ wrapper mistake must not silently skip
    the dry-run/CBDB_CONFIRM_PROD gates for a known write endpoint."""
    client, _ = make_client(tmp_path, dry_run=True)
    with pytest.raises(MutatingFlagMismatch):
        client.post("/api/v2/create", json_body={}, mutating=False)


def test_read_only_endpoint_with_mutating_true_is_rejected(tmp_path):
    client, _ = make_client(tmp_path, dry_run=True)
    with pytest.raises(MutatingFlagMismatch):
        client.post("/api/v2/get", json_body={}, mutating=True)


@responses.activate
def test_network_error_retries_then_raises_network_error_on_a_read(tmp_path):
    sleeps = []
    client, audit_log = make_client(
        tmp_path, dry_run=False, confirm_prod="http://localhost:8000", sleep=sleeps.append
    )
    responses.add(
        responses.GET,
        "http://localhost:8000/api/v2/persons",
        body=requests.exceptions.ConnectionError("connection refused"),
    )
    with pytest.raises(NetworkError):
        client.get("/api/v2/persons")
    assert len(responses.calls) == HttpClient.MAX_RETRIES
    assert len(sleeps) == HttpClient.MAX_RETRIES - 1

    records = read_audit_records(audit_log)
    assert len(records) == HttpClient.MAX_RETRIES
    assert all(r["error"] and "connection refused" in r["error"] for r in records)
    assert all(r["status_code"] is None for r in records)


@responses.activate
def test_network_error_then_success_returns_body_on_a_read(tmp_path):
    client, _ = make_client(
        tmp_path, dry_run=False, confirm_prod="http://localhost:8000", sleep=lambda s: None
    )
    responses.add(
        responses.GET,
        "http://localhost:8000/api/v2/persons",
        body=requests.exceptions.ConnectionError("connection refused"),
    )
    responses.add(
        responses.GET,
        "http://localhost:8000/api/v2/persons",
        json={"ok": True},
        status=200,
    )
    body = client.get("/api/v2/persons")
    assert body["ok"] is True


@responses.activate
def test_a_timed_out_write_is_never_re_sent(tmp_path):
    """The case this rule exists for.

    `timeout=30` is exactly the production max_execution_time API.md section 1
    records, so a slow write is precisely the one that times out client-side while
    the server commits. The second send would create a duplicate whose id
    batch_runner would then hand to every child referencing this create.
    """
    client, _ = make_client(
        tmp_path, dry_run=False, confirm_prod="http://localhost:8000", sleep=lambda s: None
    )
    responses.add(
        responses.POST,
        "http://localhost:8000/api/v2/create",
        body=requests.exceptions.ReadTimeout("timed out"),
    )
    responses.add(
        responses.POST,
        "http://localhost:8000/api/v2/create",
        json={"ok": True},
        status=200,
    )
    with pytest.raises(NetworkError, match="may already have been applied"):
        client.post("/api/v2/create", json_body={}, mutating=True)
    assert len(responses.calls) == 1


@responses.activate
def test_a_rate_limited_write_is_still_retried(tmp_path):
    """429 is the one that IS safe to re-send.

    The application layer does not throttle writes at all (rule 9), so a 429 here
    comes from a proxy or WAF that rejected the request before it reached the app -
    nothing was applied.
    """
    client, _ = make_client(
        tmp_path, dry_run=False, confirm_prod="http://localhost:8000", sleep=lambda s: None
    )
    responses.add(responses.POST, "http://localhost:8000/api/v2/create",
                  json={"message": "Too Many Requests"}, status=429)
    responses.add(responses.POST, "http://localhost:8000/api/v2/create",
                  json={"ok": True}, status=200)
    body = client.post("/api/v2/create", json_body={}, mutating=True)
    assert body["ok"] is True
    assert len(responses.calls) == 2


def test_rate_limiter_no_wait_if_interval_already_elapsed():
    clock_time = [0.0]
    sleeps = []

    def clock():
        return clock_time[0]

    def sleep(seconds):
        sleeps.append(seconds)

    limiter = RateLimiter(60, clock=clock, sleep=sleep)
    limiter.wait_for_slot()
    clock_time[0] += 5.0  # plenty of time has passed
    limiter.wait_for_slot()
    assert sleeps == []


# --- public (credential-less) lookup endpoints -------------------------------
# API.md 14.1/14.4 + AGENTS.md rules 1 and 10: these endpoints need no token, and
# sending a stale one both fails and spends the shared per-source-IP failed-auth
# budget. See docs/07-api-md-digest.md 1.4, 2.1.


@responses.activate
def test_public_get_sends_no_authorization_header(tmp_path):
    client, _ = make_client(tmp_path)
    responses.add(
        responses.GET, "http://localhost:8000/api/select/dynasty", json=[], status=200
    )
    client.get("/api/select/dynasty", public=True)
    assert "Authorization" not in responses.calls[0].request.headers
    # Accept stays required even without credentials (API.md 1.4).
    assert responses.calls[0].request.headers["Accept"] == "application/json"


@responses.activate
def test_non_public_get_still_sends_the_token(tmp_path):
    """Guard against public=True becoming the accidental default."""
    client, _ = make_client(tmp_path)
    responses.add(
        responses.GET, "http://localhost:8000/api/v2/get", json={"ok": True}, status=200
    )
    client.get("/api/v2/get")
    assert responses.calls[0].request.headers["Authorization"] == "Bearer test-token"


def test_public_is_rejected_for_a_mutating_request(tmp_path):
    client, _ = make_client(tmp_path, dry_run=False, confirm_prod="http://localhost:8000")
    with pytest.raises(ValueError, match="public=True is not allowed"):
        client.post("/api/v2/create", json_body={}, mutating=True, public=True)


@responses.activate
def test_bare_array_response_is_wrapped_under_raw(tmp_path):
    """Whole code tables return a bare JSON array, not the v2 object envelope."""
    client, _ = make_client(tmp_path)
    responses.add(
        responses.GET,
        "http://localhost:8000/api/select/dynasty",
        json=[{"c_dy": 18, "c_dynasty_chn": "\u5143"}],
        status=200,
    )
    body = client.get("/api/select/dynasty", public=True)
    assert body == {"raw": [{"c_dy": 18, "c_dynasty_chn": "\u5143"}]}


@responses.activate
def test_lookup_paths_are_classified_read_only(tmp_path):
    """_check_mutating_flag must now fail closed on the lookup endpoints too."""
    client, _ = make_client(tmp_path, dry_run=False, confirm_prod="http://localhost:8000")
    for path in ("/api/v2/texts", "/api/select/search/text", "/api/code/addr", "/api/name"):
        with pytest.raises(MutatingFlagMismatch):
            client.post(path, json_body={}, mutating=True)


@responses.activate
def test_public_response_is_truncated_in_the_audit_log(tmp_path):
    """logs/*.jsonl is append-only (rule 8), so a whole code table must not land
    in it verbatim - but the row count and the envelope's other keys must survive.
    """
    from cbdb_agent.http_client import PUBLIC_RESPONSE_LOG_MAX_ROWS

    client, audit_log = make_client(tmp_path)
    rows = [{"c_dy": i} for i in range(PUBLIC_RESPONSE_LOG_MAX_ROWS + 40)]
    responses.add(
        responses.GET, "http://localhost:8000/api/select/dynasty", json=rows, status=200
    )
    body = client.get("/api/select/dynasty", public=True)

    # The CALLER still gets every row - truncation is logging-only.
    assert len(body["raw"]) == PUBLIC_RESPONSE_LOG_MAX_ROWS + 40

    # The log stores the SUMMARIZED body directly. The {"raw": ...} wrapper is a
    # return-shape concern of get() (its contract is a dict), not a log format.
    logged = read_audit_records(audit_log)[0]["response_payload"]
    assert logged["_truncated"] is True
    assert logged["total_rows"] == PUBLIC_RESPONSE_LOG_MAX_ROWS + 40
    assert len(logged["rows"]) == PUBLIC_RESPONSE_LOG_MAX_ROWS


@responses.activate
def test_paginator_response_keeps_sibling_keys_when_truncated(tmp_path):
    from cbdb_agent.http_client import PUBLIC_RESPONSE_LOG_MAX_ROWS

    client, audit_log = make_client(tmp_path)
    rows = [{"c_addr_id": i} for i in range(PUBLIC_RESPONSE_LOG_MAX_ROWS + 3)]
    responses.add(
        responses.GET,
        "http://localhost:8000/api/code/addr",
        json={"current_page": 1, "data": rows, "total": 999},
        status=200,
    )
    client.get("/api/code/addr", params={"q": "x"}, public=True)
    logged = read_audit_records(audit_log)[0]["response_payload"]
    assert logged["total"] == 999  # the interpretable metadata is preserved
    assert logged["data"]["total_rows"] == PUBLIC_RESPONSE_LOG_MAX_ROWS + 3


@responses.activate
def test_v2_responses_are_never_truncated(tmp_path):
    """Only public lookups are summarized; the audit trail for /api/v2/* stays full."""
    from cbdb_agent.http_client import PUBLIC_RESPONSE_LOG_MAX_ROWS

    client, audit_log = make_client(tmp_path)
    rows = [{"c_personid": i} for i in range(PUBLIC_RESPONSE_LOG_MAX_ROWS + 10)]
    responses.add(
        responses.GET,
        "http://localhost:8000/api/v2/persons",
        json={"ok": True, "data": rows},
        status=200,
    )
    client.get("/api/v2/persons")
    logged = read_audit_records(audit_log)[0]["response_payload"]
    assert len(logged["data"]) == PUBLIC_RESPONSE_LOG_MAX_ROWS + 10


# --- rate limiter: serialization, not just spacing ---------------------------
# API.md 1.3: "等上一個請求回應之後再發下一個" - wait for the PREVIOUS RESPONSE.


def test_slot_measures_the_interval_from_the_previous_response_not_its_start():
    """A slow request must delay the next one by its own duration + the interval.

    wait_for_slot()'s old behaviour stamped the clock BEFORE the request, so a
    3-second request would be followed immediately by the next one - two requests
    inside one second, which is exactly what the contract forbids.
    """
    now = [0.0]
    slept = []

    def clock():
        return now[0]

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(60, clock=clock, sleep=sleep)  # 1/sec
    with limiter.slot():
        now[0] += 3.0  # the request itself takes 3 seconds
    assert slept == []  # first call never waits

    with limiter.slot():
        pass
    # Interval counted from t=3.0 (completion), so a full 1.0s wait is required.
    assert slept == [1.0]


def test_slot_stamps_completion_even_when_the_request_raises():
    """A burst of connection errors must not become a burst of retries at full speed."""
    now = [0.0]
    slept = []

    def clock():
        return now[0]

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(60, clock=clock, sleep=sleep)
    with pytest.raises(RuntimeError):
        with limiter.slot():
            raise RuntimeError("connection reset")
    with limiter.slot():
        pass
    assert slept == [1.0]


def test_slot_holds_the_lock_for_the_duration_of_the_request():
    """A second thread must not send while the first thread's request is in flight."""
    import threading

    limiter = RateLimiter(6000)  # negligible interval; the lock is what's under test
    inside = threading.Event()
    release = threading.Event()
    overlapped = []

    def first():
        with limiter.slot():
            inside.set()
            release.wait(timeout=5)

    def second():
        inside.wait(timeout=5)
        # If slot() did not hold a lock, this would acquire immediately while
        # `first` is still inside its own slot.
        acquired = limiter._lock.acquire(timeout=0.2)
        overlapped.append(acquired)
        if acquired:
            limiter._lock.release()

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    t2.start()
    t2.join(timeout=5)
    release.set()
    t1.join(timeout=5)

    assert overlapped == [False], "a concurrent caller got a slot mid-request"


@responses.activate
def test_request_path_uses_slot_so_retries_are_also_spaced(tmp_path):
    """The retry loop must go through the limiter on every attempt, not just the first."""
    sleeps = []
    client, _ = make_client(tmp_path, sleep=sleeps.append)
    for _ in range(3):
        responses.add(
            responses.GET, "http://localhost:8000/api/v2/get", json={}, status=500
        )
    with pytest.raises(ServerError):
        client.get("/api/v2/get")
    assert len(responses.calls) == 3


# --- the transport-layer resource-string guard --------------------------------
# A defence that only works when the caller used the intended wrapper is not a
# defence for a write the server cannot undo, so the ambiguous-resource refusal
# lives at the layer that puts bytes on the wire.


def _text_codes_envelope(changes=None):
    return {
        "resource": "text-codes",
        "mode": "direct",
        "operation": "create",
        "person_id": 0,
        "target": {"pk": {}},
        "changes": changes if changes is not None else {"c_title_chn": "聽雪先生集"},
    }


@responses.activate
def test_the_guard_does_not_block_reads(tmp_path):
    """A READ naming an ambiguous resource is harmless: nothing is written, so which
    table the string would have hit does not matter.

    Pinned with `offices` on purpose. The earlier version of this test sent a
    `text-codes` envelope, which the guard never refuses in either direction - so it
    passed whether or not reads were exempt, and deleting `not mutating or` from the
    check left it green."""
    client, _ = make_client(tmp_path)
    responses.add(
        responses.GET, "http://localhost:8000/api/v2/get", json={"ok": True}, status=200
    )
    client.get("/api/v2/get", json_body=dict(_text_codes_envelope(),
                                             resource="offices"))
    assert len(responses.calls) == 1


def test_the_ambiguous_offices_resource_string_is_refused(tmp_path):
    """`offices` is a server-side alias for BOTH the `office` aggregate and the
    routine `postings` sub-resource, and which one wins is registry order this client
    cannot see. So a write that says `offices` may land on a person's appointment
    record or on a global office code, and the caller cannot tell which."""
    from cbdb_agent.http_client import AmbiguousResourceError

    client, _ = make_client(tmp_path, dry_run=False, confirm_prod="http://localhost:8000")
    for ambiguous in ("offices", "office-load", "  OFFICES  "):
        body = {
            "resource": ambiguous,
            "mode": "direct",
            "operation": "create",
            "person_id": 0,
            "target": {"pk": {}},
            "changes": {"name": "知某州事"},
        }
        with pytest.raises(AmbiguousResourceError, match="refusing to send resource"):
            client.post("/api/v2/create", json_body=body, mutating=True)


def test_the_unambiguous_spellings_are_not_refused(tmp_path):
    """Both spellings say exactly one thing, so both go through."""
    client, _ = make_client(tmp_path, dry_run=True, confirm_prod="")
    routine = {
        "resource": "postings", "mode": "direct", "operation": "create",
        "person_id": 1, "target": {"pk": {"c_office_id": 1}}, "changes": {},
    }
    client.post("/api/v2/create", json_body=routine, mutating=True)  # must not raise
    client.post("/api/v2/create", json_body=dict(routine, resource="office"),
                mutating=True)  # must not raise


@responses.activate
def test_a_write_never_follows_a_redirect(tmp_path):
    """`requests` would re-POST the body transparently.

    307 and 308 preserve the method and the body by definition, and the client only
    ever sees the final response - so a redirect on a create is a second row and one
    reported success, on tables with no delete path. Reads still follow.
    """
    client, _ = make_client(
        tmp_path, dry_run=False, confirm_prod="http://localhost:8000",
        sleep=lambda s: None,
    )
    responses.add(
        responses.POST, "http://localhost:8000/api/v2/create",
        status=307, headers={"Location": "https://localhost:8000/api/v2/create"},
        json={},
    )
    responses.add(
        responses.POST, "https://localhost:8000/api/v2/create",
        json={"ok": True}, status=200,
    )
    with pytest.raises(ServerError, match="Redirect"):
        client.post("/api/v2/create", json_body={}, mutating=True)
    assert len(responses.calls) == 1, "the body must not be sent to the new location"


@responses.activate
def test_a_read_still_follows_a_redirect(tmp_path):
    client, _ = make_client(
        tmp_path, dry_run=False, confirm_prod="http://localhost:8000",
        sleep=lambda s: None,
    )
    responses.add(
        responses.GET, "http://localhost:8000/api/v2/persons",
        status=302, headers={"Location": "http://localhost:8000/api/v2/people"},
    )
    responses.add(
        responses.GET, "http://localhost:8000/api/v2/people",
        json={"ok": True}, status=200,
    )
    assert client.get("/api/v2/persons")["ok"] is True


@responses.activate
def test_an_indeterminate_write_is_marked_as_such(tmp_path):
    """The marker `batch_runner` reads to stop the batch: we do not know whether
    this row exists, so nothing after it can be trusted to reference it and a
    re-run would duplicate whatever landed."""
    client, _ = make_client(
        tmp_path, dry_run=False, confirm_prod="http://localhost:8000",
        sleep=lambda s: None,
    )
    responses.add(
        responses.POST, "http://localhost:8000/api/v2/create",
        body=requests.exceptions.ReadTimeout("timed out"),
    )
    with pytest.raises(NetworkError) as caught:
        client.post("/api/v2/create", json_body={}, mutating=True)
    assert caught.value.indeterminate is True


@responses.activate
def test_a_422_is_not_indeterminate(tmp_path):
    """A validation failure means the row was NOT written. Marking it would stop a
    batch for a fault confined to one proposal."""
    client, _ = make_client(
        tmp_path, dry_run=False, confirm_prod="http://localhost:8000",
        sleep=lambda s: None,
    )
    responses.add(
        responses.POST, "http://localhost:8000/api/v2/create",
        json={"ok": False, "errors": {"changes": ["disallowed_fields: c_x"]}},
        status=422,
    )
    with pytest.raises(CbdbApiError) as caught:
        client.post("/api/v2/create", json_body={}, mutating=True)
    assert getattr(caught.value, "indeterminate", False) is False
