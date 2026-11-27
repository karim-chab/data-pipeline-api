"""Tier quotas, the bucketing rule for unverified traffic, and the counter-map bound."""

from app.core.config import settings
from app.core.ratelimit import FixedWindowRateLimiter, rate_limiter


def test_anonymous_quota_blocks_with_retry_after(client, monkeypatch):
    monkeypatch.setattr(settings, "ANONYMOUS_RATE_LIMIT_PER_MIN", 2)

    codes = [client.get("/analytics/fx").status_code for _ in range(3)]

    assert codes == [200, 200, 429]
    blocked = client.get("/analytics/fx")
    assert blocked.headers["retry-after"].isdigit()
    assert blocked.headers["x-ratelimit-remaining"] == "0"


def test_junk_keys_cannot_mint_buckets(client, monkeypatch):
    """Rotating a junk key per request must not reset the window or add counters."""
    monkeypatch.setattr(settings, "ANONYMOUS_RATE_LIMIT_PER_MIN", 3)

    codes = [
        client.get("/data/crypto", headers={"X-API-Key": f"sk_junk_{index}"}).status_code
        for index in range(50)
    ]

    assert 401 in codes          # junk keys are rejected
    assert 429 in codes          # ...and the IP quota still binds
    assert rate_limiter.tracked_buckets == 1


def test_verified_key_keeps_its_own_budget_while_its_ip_is_flooded(client, mint_key, monkeypatch):
    key = mint_key("pro@test", "pro")["api_key"]
    monkeypatch.setattr(settings, "ANONYMOUS_RATE_LIMIT_PER_MIN", 1)

    for index in range(10):
        client.get("/data/crypto", headers={"X-API-Key": f"sk_junk_{index}"})

    assert client.get("/data/crypto", headers={"X-API-Key": key}).status_code == 200
    assert rate_limiter.tracked_buckets == 2  # one IP bucket, one key bucket


def test_stripe_webhook_is_exempt_from_rate_limiting(client, monkeypatch):
    """Throttling Stripe's retries would lose revenue."""
    monkeypatch.setattr(settings, "ANONYMOUS_RATE_LIMIT_PER_MIN", 1)

    first = client.post("/webhooks/stripe", headers={"stripe-signature": "t=1,v1=x"}, content=b"{}")
    second = client.post("/webhooks/stripe", headers={"stripe-signature": "t=1,v1=x"}, content=b"{}")

    assert first.status_code != 429
    assert second.status_code != 429


def test_counter_map_is_bounded():
    limiter = FixedWindowRateLimiter(max_buckets=5)

    for index in range(100):
        limiter.hit(f"ip:10.0.0.{index}", limit=10)

    assert limiter.tracked_buckets == 5


def test_window_rollover_resets_the_counter():
    limiter = FixedWindowRateLimiter(window_seconds=0)

    outcomes = [limiter.hit("ip:192.0.2.1", limit=1)[0] for _ in range(3)]

    assert outcomes == [True, True, True]
