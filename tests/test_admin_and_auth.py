"""API-key resolution, tier gates and the operator-only pipeline routes."""

import sqlite3

import app.auth as auth
from app.core.config import settings


def test_dev_key_route_is_gone(client):
    """Minting keys must not be reachable over HTTP."""
    assert client.post("/dev/generate-key?email=a@b.c&tier=pro").status_code == 404


def test_pipeline_trigger_requires_admin_key(client, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "admin_test_key")

    assert client.post("/pipelines/run-sample").status_code == 403
    assert client.post(
        "/pipelines/run-fx", headers={"X-API-Key": "sk_free_wrong"}
    ).status_code == 403


def test_pipeline_trigger_fails_closed_when_admin_key_is_unset(client, monkeypatch):
    """An unconfigured admin key must disable the routes, not open them."""
    monkeypatch.setattr(settings, "ADMIN_API_KEY", None)

    assert client.post("/pipelines/run-sample").status_code == 503


def test_unknown_key_is_rejected(client):
    response = client.get("/data/crypto", headers={"X-API-Key": "sk_free_unknown"})

    assert response.status_code == 401


def test_deactivated_key_is_rejected(client, mint_key, db_path):
    key = mint_key("off@test", "free")["api_key"]
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE api_keys SET is_active = 0 WHERE email = ?", ("off@test",))

    assert client.get("/data/crypto", headers={"X-API-Key": key}).status_code == 401


def test_free_key_reports_its_stored_limit(client, mint_key):
    key = mint_key("free@test", "free")["api_key"]

    response = client.get("/data/crypto", headers={"X-API-Key": key})

    assert response.status_code == 200
    assert response.json()["user_tier"] == "free"
    assert response.headers["x-ratelimit-limit"] == "30"
    assert response.headers["x-ratelimit-remaining"] == "29"


def test_pro_key_reaches_pro_route_with_pro_limit(client, mint_key):
    key = mint_key("pro@test", "pro")["api_key"]

    response = client.get("/analytics/pro/arbitrage", headers={"X-API-Key": key})

    assert response.status_code == 200
    assert response.json()["tier"] == "pro"
    assert client.get("/data/fx", headers={"X-API-Key": key}).headers["x-ratelimit-limit"] == "1000"


def test_free_key_cannot_reach_pro_route(client, mint_key):
    key = mint_key("free2@test", "free")["api_key"]

    assert client.get("/analytics/pro/arbitrage", headers={"X-API-Key": key}).status_code == 403


def test_route_reuses_the_middleware_key_lookup(client, mint_key, monkeypatch):
    """The dependency must not repeat the lookup the middleware already made."""
    key = mint_key("free3@test", "free")["api_key"]
    calls = []

    async def counting_resolution(api_key):
        calls.append(api_key)
        return {"email": "stub@test", "tier": "free", "rate_limit": 30}

    monkeypatch.setattr(auth, "resolve_api_key", counting_resolution)

    assert client.get("/data/crypto", headers={"X-API-Key": key}).status_code == 200
    assert calls == []
