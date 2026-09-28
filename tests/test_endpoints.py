"""
Pre-deploy smoke tests
"""

import io
import sys
from unittest.mock import MagicMock

import pytest

sys.modules["services.auth"] = MagicMock()
sys.modules["services.llm_inference"] = MagicMock()
sys.modules["services.cache"] = MagicMock()
sys.modules["services.data_pipeline"] = MagicMock()
sys.modules["services.system_prompt"] = MagicMock()
sys.modules["anthropic"] = MagicMock()
sys.modules["redis"] = MagicMock()

from fastapi.testclient import TestClient

from app import app


@pytest.fixture
def client():
    return TestClient(app)


def test_root_ok(client):
    assert client.get("/").status_code == 200


def test_health_ok(client, monkeypatch):
    import services.auth as auth
    import services.cache as cache

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("POSTGRES_URL", "postgresql://test")
    monkeypatch.setenv("REDIS_URL", "redis://test")

    auth.check_postgres_connection.return_value = True
    cache.check_redis_connection.return_value = True

    response = client.get("/health")

    assert response.status_code == 200


def test_generate_key_ok(client):
    import services.auth as auth
    import services.cache as cache

    cache.check_rate_limit.return_value = (True, 2)
    auth.generate_api_key.return_value = "atna_testkey123"

    resp = client.post("/generate-key")

    assert resp.status_code == 201


def test_generate_key_rate_limited(client):
    import services.cache as cache

    cache.check_rate_limit.return_value = (False, 0)

    resp = client.post("/generate-key")

    assert resp.status_code == 429


def test_score_missing_api_key(client):
    # A file must be present, or FastAPI's own request validation
    # (file: UploadFile = File(...)) rejects the request with 422
    # before app.py's auth check ever runs.
    resp = client.post(
        "/score",
        files={"file": ("test.txt", io.BytesIO(b"data"), "text/plain")},
    )

    assert resp.status_code == 401


def test_score_invalid_api_key(client):
    import services.auth as auth

    auth.is_valid_api_key.return_value = False

    resp = client.post(
        "/score",
        headers={"X-API-Key": "atna_bogus"},
        files={"file": ("test.txt", io.BytesIO(b"data"), "text/plain")},
    )

    assert resp.status_code == 401


def test_score_missing_file_field(client):
    # No "file" part in the multipart body at all: FastAPI's own
    # request validation rejects this with 422 before the route body
    # runs, regardless of the API key or rate-limit state.
    import services.auth as auth
    import services.cache as cache

    auth.is_valid_api_key.return_value = True
    cache.check_rate_limit.return_value = (True, 59)

    resp = client.post(
        "/score",
        headers={"X-API-Key": "atna_ok"},
    )

    assert resp.status_code == 422


def test_score_rate_limited(client):
    import services.auth as auth
    import services.cache as cache

    auth.is_valid_api_key.return_value = True
    cache.check_rate_limit.return_value = (False, 0)

    resp = client.post(
        "/score",
        headers={"X-API-Key": "atna_ok"},
        files={"file": ("test.txt", io.BytesIO(b"data"), "text/plain")},
    )

    assert resp.status_code == 429