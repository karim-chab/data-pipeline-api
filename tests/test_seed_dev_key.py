"""The dev-only key minting CLI."""

import json
import sqlite3

import scripts.seed_dev_key as seed_module
from app.core.config import settings


def test_mints_a_key_with_the_tier_limit(mint_key):
    record = mint_key("dev@test", "pro")

    assert record["tier"] == "pro"
    assert record["rate_limit_per_min"] == settings.PRO_RATE_LIMIT_PER_MIN
    assert record["api_key"].startswith("sk_pro_")


def test_minting_twice_resets_the_key_without_duplicating_the_row(mint_key, db_path):
    first = mint_key("dev@test", "free")
    second = mint_key("dev@test", "pro")

    assert first["api_key"] != second["api_key"]
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT tier FROM api_keys WHERE email = ?", ("dev@test",)
        ).fetchall()

    assert rows == [("pro",)]


def test_cli_refuses_to_run_in_production(monkeypatch, capsys, db_path):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr("sys.argv", ["seed_dev_key", "--email", "x@y.z", "--tier", "pro"])

    assert seed_module.main() == 2
    assert "Refusing" in capsys.readouterr().err


def test_cli_allows_production_with_force(monkeypatch, capsys, db_path):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(
        "sys.argv", ["seed_dev_key", "--email", "x@y.z", "--tier", "pro", "--force"]
    )

    assert seed_module.main() == 0
    assert json.loads(capsys.readouterr().out)["tier"] == "pro"
