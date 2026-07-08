import pytest
import responses

from cbdb_agent.audit_log import AuditLog
from cbdb_agent.config import Config
from cbdb_agent.http_client import HttpClient
from cbdb_agent.person_id import (
    PersonIdError,
    get_max_person_id,
    is_person_id_taken,
    validate_new_person_id,
)


def make_client(tmp_path):
    config = Config(
        api_base_url="http://localhost:8000",
        api_token="test-token",
        dry_run=True,
        confirm_prod="",
        max_requests_per_minute=6000,
        local_audit_log_dir=tmp_path / "logs",
    )
    return HttpClient(config, AuditLog(config.local_audit_log_dir))


@responses.activate
def test_get_max_person_id_single_page(tmp_path):
    client = make_client(tmp_path)
    responses.add(
        responses.GET,
        "http://localhost:8000/api/v2/persons",
        json={
            "data": [{"c_personid": 10}, {"c_personid": 999}, {"c_personid": 5}],
            "meta": {"current_page": 1, "last_page": 1},
        },
        status=200,
    )
    assert get_max_person_id(client) == 999


@responses.activate
def test_get_max_person_id_scans_all_pages(tmp_path):
    client = make_client(tmp_path)
    responses.add(
        responses.GET,
        "http://localhost:8000/api/v2/persons",
        json={
            "data": [{"c_personid": 10}],
            "meta": {"current_page": 1, "last_page": 2},
        },
        status=200,
    )
    responses.add(
        responses.GET,
        "http://localhost:8000/api/v2/persons",
        json={
            "data": [{"c_personid": 5000}],
            "meta": {"current_page": 2, "last_page": 2},
        },
        status=200,
    )
    assert get_max_person_id(client) == 5000
    assert len(responses.calls) == 2


@responses.activate
def test_get_max_person_id_unrecognized_shape_raises(tmp_path):
    client = make_client(tmp_path)
    responses.add(
        responses.GET,
        "http://localhost:8000/api/v2/persons",
        json={"totally": "unexpected"},
        status=200,
    )
    with pytest.raises(PersonIdError):
        get_max_person_id(client)


@responses.activate
def test_get_max_person_id_gives_up_after_max_pages(tmp_path):
    client = make_client(tmp_path)
    for page in range(1, 4):
        responses.add(
            responses.GET,
            "http://localhost:8000/api/v2/persons",
            json={
                "data": [{"c_personid": page}],
                "meta": {"current_page": page, "last_page": 1000},
            },
            status=200,
        )
    with pytest.raises(PersonIdError, match="max_pages"):
        get_max_person_id(client, max_pages=3)


def test_validate_new_person_id_rejects_zero():
    with pytest.raises(PersonIdError):
        validate_new_person_id(0, max_existing_id=1000)


def test_validate_new_person_id_rejects_negative():
    with pytest.raises(PersonIdError):
        validate_new_person_id(-5, max_existing_id=1000)


def test_validate_new_person_id_rejects_too_far_beyond_max():
    with pytest.raises(PersonIdError):
        validate_new_person_id(11001, max_existing_id=1000)


def test_validate_new_person_id_accepts_within_gap():
    validate_new_person_id(11000, max_existing_id=1000)  # exactly at the boundary


def test_validate_new_person_id_accepts_within_existing_range():
    validate_new_person_id(500, max_existing_id=1000)


@responses.activate
def test_is_person_id_taken_true(tmp_path):
    client = make_client(tmp_path)
    responses.add(
        responses.GET,
        "http://localhost:8000/api/v2/get",
        json={"ok": True, "result": {"pk": {"c_personid": 42}, "row": {}}},
        status=200,
    )
    assert is_person_id_taken(client, 42) is True


@responses.activate
def test_is_person_id_taken_false(tmp_path):
    client = make_client(tmp_path)
    responses.add(
        responses.GET,
        "http://localhost:8000/api/v2/get",
        json={"ok": True, "result": None},
        status=200,
    )
    assert is_person_id_taken(client, 99999) is False
