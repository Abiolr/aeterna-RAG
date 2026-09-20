"""
Pre-deploy smoke tests
"""

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

from app import app


def test_root_ok():
    client = app.test_client()

    assert client.get("/").status_code == 200

def test_health_ok(monkeypatch):
    import services.auth as auth
    import services.cache as cache

    monkeypatch.setenv("POSTGRES_URL", "postgresql://test")
    monkeypatch.setenv("REDIS_URL", "redis://test")

    auth.check_postgres_connection.return_value = True
    cache.check_redis_connection.return_value = True

    client = app.test_client()
    response = client.get("/health")

    assert response.status_code == 200

def test_generate_key_ok():
    import services.auth as auth
    import services.cache as cache

    cache.check_rate_limit.return_value = (True, 2)
    auth.generate_api_key.return_value = "atna_testkey123"

    client = app.test_client()
    resp = client.post("/generate-key")

    assert resp.status_code == 201


def test_generate_key_rate_limited():
    import services.cache as cache

    cache.check_rate_limit.return_value = (False, 0)

    client = app.test_client()
    resp = client.post("/generate-key")

    assert resp.status_code == 429


def test_score_missing_api_key():
    client = app.test_client()

    resp = client.post("/score")

    assert resp.status_code == 401


def test_score_invalid_api_key():
    import services.auth as auth

    auth.is_valid_api_key.return_value = False

    client = app.test_client()
    resp = client.post(
        "/score",
        headers={"X-API-Key": "atna_bogus"},
    )

    assert resp.status_code == 401


def test_score_no_file():
    import services.auth as auth
    import services.cache as cache

    auth.is_valid_api_key.return_value = True
    cache.check_rate_limit.return_value = (True, 59)

    client = app.test_client()
    resp = client.post(
        "/score",
        headers={"X-API-Key": "atna_ok"},
    )

    assert resp.status_code == 400


def test_score_rate_limited():
    import services.auth as auth
    import services.cache as cache

    auth.is_valid_api_key.return_value = True
    cache.check_rate_limit.return_value = (False, 0)

    client = app.test_client()
    resp = client.post(
        "/score",
        headers={"X-API-Key": "atna_ok"},
    )

    assert resp.status_code == 429