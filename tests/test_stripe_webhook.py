"""Stripe webhook: signature handling, idempotency, non-rotation and log redaction."""

import logging
import sqlite3

import pytest
import stripe

import app.main as main


def make_event(event_id="evt_1", **session_overrides):
    session = {
        "mode": "subscription",
        "payment_status": "paid",
        "customer_email": "buyer@test",
        "customer_details": {"email": "buyer@test"},
    }
    session.update(session_overrides)
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {"object": session},
    }


@pytest.fixture
def deliver(client, monkeypatch):
    """Post a webhook whose signature verification is stubbed for the given event."""

    def _deliver(event):
        monkeypatch.setattr(
            stripe.Webhook,
            "construct_event",
            lambda payload, signature, secret: event,
        )
        return client.post(
            "/webhooks/stripe",
            headers={"stripe-signature": "t=1,v1=stub"},
            content=b"{}",
        )

    return _deliver


def test_first_delivery_provisions_a_pro_key(deliver, db_path):
    response = deliver(make_event("evt_1"))

    assert response.status_code == 200
    assert response.json()["status"] == "provisioned"

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT key, tier, rate_limit_per_min FROM api_keys WHERE email = ?",
            ("buyer@test",),
        ).fetchall()

    assert len(rows) == 1
    key, tier, limit = rows[0]
    assert tier == "pro"
    assert limit == 1000
    assert key.startswith("sk_pro_")
    assert len(key) == len("sk_pro_") + 32


def test_replayed_event_is_a_duplicate_and_keeps_the_key(deliver, db_path):
    first = deliver(make_event("evt_1"))
    second = deliver(make_event("evt_1"))

    assert first.json()["status"] == "provisioned"
    assert second.json()["status"] == "duplicate"

    with sqlite3.connect(db_path) as connection:
        keys = connection.execute(
            "SELECT key FROM api_keys WHERE email = ?", ("buyer@test",)
        ).fetchall()
        events = connection.execute("SELECT COUNT(*) FROM processed_webhook_events").fetchone()[0]

    assert len(keys) == 1
    assert events == 1


def test_later_event_upgrades_without_rotating_the_key(deliver, db_path):
    deliver(make_event("evt_1"))

    response = deliver(make_event("evt_2"))

    assert response.json()["status"] == "upgraded"
    with sqlite3.connect(db_path) as connection:
        keys = connection.execute(
            "SELECT key FROM api_keys WHERE email = ?", ("buyer@test",)
        ).fetchall()
    assert len(keys) == 1  # no second credential minted


def test_unpaid_session_does_not_provision(deliver, db_path):
    response = deliver(make_event("evt_unpaid", payment_status="unpaid", customer_email="late@test"))

    assert response.json()["status"] == "skipped"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM api_keys WHERE email = ?", ("late@test",)
        ).fetchone()[0] == 0


def test_non_subscription_session_does_not_provision(deliver, db_path):
    response = deliver(make_event("evt_payment", mode="payment", customer_email="oneoff@test"))

    assert response.json()["status"] == "skipped"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM api_keys WHERE email = ?", ("oneoff@test",)
        ).fetchone()[0] == 0


def test_missing_email_is_acknowledged_not_retried_forever(deliver):
    response = deliver(make_event("evt_noemail", customer_email=None, customer_details={}))

    assert response.status_code == 200
    assert response.json()["status"] == "unprocessable"


def test_unrelated_event_types_are_ignored(deliver):
    response = deliver({"id": "evt_other", "type": "customer.created", "data": {"object": {}}})

    assert response.json()["status"] == "ignored"


def test_invalid_signature_is_rejected(client, monkeypatch):
    def reject(payload, signature, secret):
        raise stripe.error.SignatureVerificationError("bad signature", "t=1,v1=stub")

    monkeypatch.setattr(stripe.Webhook, "construct_event", reject)

    response = client.post(
        "/webhooks/stripe", headers={"stripe-signature": "t=1,v1=stub"}, content=b"{}"
    )

    assert response.status_code == 400


def test_malformed_payload_is_rejected(client, monkeypatch):
    def reject(payload, signature, secret):
        raise ValueError("malformed payload")

    monkeypatch.setattr(stripe.Webhook, "construct_event", reject)

    response = client.post(
        "/webhooks/stripe", headers={"stripe-signature": "t=1,v1=stub"}, content=b"{}"
    )

    assert response.status_code == 400


def test_webhook_is_unavailable_without_a_secret(client, monkeypatch):
    # main snapshots the secret at import, so patch the module attribute it reads.
    monkeypatch.setattr(main, "STRIPE_WEBHOOK_SECRET", None)

    response = client.post(
        "/webhooks/stripe", headers={"stripe-signature": "t=1,v1=stub"}, content=b"{}"
    )

    assert response.status_code == 503


def test_raw_key_never_reaches_the_logs(deliver, db_path, caplog):
    with caplog.at_level(logging.INFO, logger="Data Pipeline Engine"):
        deliver(make_event("evt_log"))

    with sqlite3.connect(db_path) as connection:
        key = connection.execute(
            "SELECT key FROM api_keys WHERE email = ?", ("buyer@test",)
        ).fetchone()[0]

    assert key not in caplog.text
    assert "key_prefix=sk_pro_" in caplog.text
    assert "buyer@test" in caplog.text
