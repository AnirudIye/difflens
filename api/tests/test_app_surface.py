"""What the API exposes to anyone who points a browser at it.

Small surface, small file, but both of these are one-line regressions: a
default argument reinstated, or a middleware reordered.
"""

from fastapi.testclient import TestClient

from app.config import settings
from app.logging_setup import setup_logging
from app.main import create_app


def test_every_response_says_it_is_not_to_be_sniffed(client):
    response = client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_every_response_carries_its_request_id(client):
    response = client.get("/health")
    assert response.headers["X-Request-ID"]


def test_a_supplied_request_id_is_echoed_back(client):
    response = client.get("/health", headers={"X-Request-ID": "trace-me"})
    assert response.headers["X-Request-ID"] == "trace-me"


def _production_app():
    original = settings.environment
    settings.environment = "production"
    try:
        return create_app()
    finally:
        settings.environment = original
        setup_logging(original)  # create_app reconfigures structlog globally


def test_production_does_not_publish_its_own_api_schema():
    """The schema is a map of every route and payload the service accepts.

    Useful in development, and nothing a deployed service needs to hand out.
    """
    production = _production_app()
    assert production.openapi_url is None
    assert production.docs_url is None
    assert production.redoc_url is None

    with TestClient(production) as probe:
        assert probe.get("/openapi.json").status_code == 404
        assert probe.get("/docs").status_code == 404
        assert probe.get("/redoc").status_code == 404


def test_development_still_publishes_it():
    with TestClient(create_app()) as probe:
        assert probe.get("/openapi.json").status_code == 200
        assert probe.get("/docs").status_code == 200
