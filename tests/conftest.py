"""Shared fixtures for the API test suite.

Every test runs against its own temporary SQLite file with the scheduler stubbed:
no network access, and no writes to the development ``data.db``.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

import app.auth as auth
import app.db.database as database
import app.main as main
import scripts.seed_dev_key as seed_module
from app.core.ratelimit import rate_limiter


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """The limiter is process-wide, so its counters must not leak between tests."""
    rate_limiter._buckets.clear()
    yield
    rate_limiter._buckets.clear()


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Point every module that holds a database path at a throwaway file."""
    path = str(tmp_path / "test.db")

    # auth and seed_dev_key bind DB_FILE by value at import time, so each module
    # holds its own reference and must be patched individually.
    monkeypatch.setattr(database, "DB_FILE", path)
    monkeypatch.setattr(auth, "DB_FILE", path)
    monkeypatch.setattr(seed_module, "DB_FILE", path)

    return path


@pytest.fixture
def client(db_path, monkeypatch):
    """The real app, running its lifespan against the temporary database."""
    monkeypatch.setattr(main, "start_scheduler", lambda: None)
    monkeypatch.setattr(main, "stop_scheduler", lambda: None)
    monkeypatch.setattr(main, "STRIPE_WEBHOOK_SECRET", "whsec_test")

    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture
def mint_key(db_path):
    """Mint a subscriber row; returns {"email", "api_key", "tier", "rate_limit_per_min"}."""

    def _mint(email: str = "user@test", tier: str = "free") -> dict:
        return asyncio.run(seed_module.seed_key(email, tier))

    return _mint
