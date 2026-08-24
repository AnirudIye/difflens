"""Schema rejections wear the same envelope as everything else.

FastAPI's stock 422 is a bare list under "detail" that the frontend has no
reader for, and it echoes the offending "input" back to the caller. On
PUT /settings/ai-key that input is an API key, so it would be returned in a
response body and written to the access log on the way out.
"""

import uuid


def test_validation_errors_use_the_error_envelope(client, make_user_with_session):
    make_user_with_session("alice")
    response = client.post("/reviews", json={"pull_request_id": "not-a-uuid"})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["fields"] == [
        {"field": "pull_request_id", "message": error["fields"][0]["message"]}
    ]
    assert "uuid" in error["message"].lower()
    assert error["request_id"]


def test_a_rejected_api_key_is_never_echoed_back(client, make_user_with_session):
    make_user_with_session("alice")
    secret = "sk-ant-too-short"[:9]  # under the 10 character minimum
    response = client.put("/settings/ai-key", json={"provider": "anthropic", "api_key": secret})

    assert response.status_code == 422
    body = response.text
    assert secret not in body, "the rejected key was returned to the caller"
    assert "input" not in response.json()["error"]
    assert response.json()["error"]["fields"][0]["field"] == "api_key"


def test_an_unknown_provider_names_the_field_not_the_value(client, make_user_with_session):
    make_user_with_session("alice")
    response = client.put(
        "/settings/ai-key",
        json={"provider": "definitely-not-a-provider", "api_key": "0123456789abcdef"},
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["fields"][0]["field"] == "provider"
    assert "definitely-not-a-provider" not in response.text


def test_several_bad_fields_are_all_named(client, make_user_with_session):
    make_user_with_session("alice")
    response = client.put("/settings/ai-key", json={"provider": "nope", "api_key": "short"})

    fields = {item["field"] for item in response.json()["error"]["fields"]}
    assert fields == {"provider", "api_key"}


def test_a_missing_body_is_reported_as_a_missing_field(client, make_user_with_session):
    make_user_with_session("alice")
    response = client.post("/reviews", content=b"", headers={"Content-Type": "application/json"})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["fields"], "a rejected request must say which field was wrong"


def test_a_bad_path_id_is_a_validation_error_not_a_crash(client, make_user_with_session):
    make_user_with_session("alice")
    response = client.get("/reviews/not-a-uuid")

    assert response.status_code == 422
    assert response.json()["error"]["fields"][0]["field"] == "review_id"


def test_the_message_is_capped():
    """Tested directly, because no model in this API can currently produce a
    message long enough to reach the cap.

    A list where a scalar is expected is one validation error, not two
    hundred, so a request cannot get there today. The cap exists for the
    model that has fifty fields, and a cap that is never exercised is a cap
    that quietly stops working.
    """
    from app.main import MAX_VALIDATION_MESSAGE_CHARS, cap

    short = "api_key: String should have at least 10 characters"
    assert cap(short) == short

    long = "field: " + "x" * (MAX_VALIDATION_MESSAGE_CHARS * 2)
    capped = cap(long)
    assert len(capped) == MAX_VALIDATION_MESSAGE_CHARS
    assert capped.endswith("...")
    assert capped.startswith("field: xxx")

    exact = "y" * MAX_VALIDATION_MESSAGE_CHARS
    assert cap(exact) == exact, "a message exactly at the limit is not truncated"


def test_ordinary_http_errors_still_use_the_same_envelope(client, make_user_with_session):
    make_user_with_session("alice")
    response = client.get(f"/reviews/{uuid.uuid4()}")

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["request_id"]


def test_an_unexpected_exception_still_answers_in_the_envelope(
    client, monkeypatch, make_user_with_session
):
    """Only RequestValidationError and HTTPException had handlers, so anything
    unforeseen returned Starlette's bare text/plain 500 with no request id and
    no nosniff header. The frontend reads error.code off every failure, so
    that response is one it cannot describe to the user at all."""
    from app.services import repo_service

    def boom(*args, **kwargs):
        raise RuntimeError("something nobody predicted")

    _user, token = make_user_with_session("envelope")
    # Looked up on the module at call time, so patching it reaches the route
    monkeypatch.setattr(repo_service, "list_user_repositories", boom)

    # raise_server_exceptions=False so the handler's response is returned
    # rather than the exception being re-raised into the test, which is what
    # a real client over HTTP sees
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as raw:
        raw.cookies.set("session", token)
        response = raw.get("/repositories?sync=false")

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers.get("x-content-type-options") == "nosniff"
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["request_id"]
    # The exception text can carry anything the request contained
    assert "something nobody predicted" not in response.text
