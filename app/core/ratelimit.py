"""Per-caller rate limiting.

Bucketing rule — the important part:

* a **verified** API key gets its own budget, identified by a hash of the
  credential (raw keys are never retained in memory);
* **anonymous and unverified** traffic (no key, unknown key, deactivated key)
  shares a budget per **client IP**.

Bucketing unverified traffic by key would let a caller send a fresh random
`X-API-Key` on every request: each junk key would start a brand-new window, so
quotas would never bind, and every junk key would add an entry to the counter
map. Keying on the IP closes both holes.

Fixed-window counters live in process memory. Exact for a single worker; behind
several workers or replicas each process keeps its own budget, so production
should swap ``FixedWindowRateLimiter`` for a Redis-backed implementation of the
same ``hit(bucket, limit)`` interface.
"""

import hashlib
import secrets
import time

from fastapi import Request
from starlette.responses import JSONResponse

from app.auth import resolve_api_key
from app.core.config import settings

# Stripe retries webhooks on failure and must never be throttled by us.
_EXEMPT_PATHS = frozenset({"/", "/health", "/docs", "/redoc", "/webhooks/stripe"})

_PRUNE_INTERVAL_SECONDS = 300.0

# Hard ceiling on tracked buckets, so a flood of distinct IPs cannot exhaust
# memory between prunes.
_MAX_BUCKETS = 10_000


class FixedWindowRateLimiter:
    """Counts requests per bucket inside a rolling window."""

    def __init__(self, window_seconds: float = 60.0, max_buckets: int = _MAX_BUCKETS):
        self._window = window_seconds
        self._max_buckets = max(1, max_buckets)
        self._buckets: dict[str, tuple[int, float]] = {}
        self._last_prune = time.monotonic()

    def hit(self, bucket: str, limit: int) -> tuple[bool, int, int]:
        """Count one request. Returns (allowed, remaining, retry_after_seconds)."""
        now = time.monotonic()
        self._maybe_prune(now)

        count, window_start = self._buckets.get(bucket, (0, now))
        if now - window_start >= self._window:
            count, window_start = 0, now

        count += 1

        if bucket not in self._buckets and len(self._buckets) >= self._max_buckets:
            self._make_room(now)

        self._buckets[bucket] = (count, window_start)

        remaining = max(0, limit - count)
        retry_after = max(1, int(self._window - (now - window_start)) + 1)
        return count <= limit, remaining, retry_after

    @property
    def tracked_buckets(self) -> int:
        """Number of live counters (exposed for tests and metrics)."""
        return len(self._buckets)

    def _maybe_prune(self, now: float) -> None:
        """Drop expired buckets periodically so the map cannot grow unchecked."""
        if now - self._last_prune < _PRUNE_INTERVAL_SECONDS:
            return
        self._prune(now)

    def _prune(self, now: float) -> None:
        self._buckets = {
            bucket: (count, start)
            for bucket, (count, start) in self._buckets.items()
            if now - start < self._window
        }
        self._last_prune = now

    def _make_room(self, now: float) -> None:
        """Prune, then evict the counters closest to expiry until under the cap."""
        self._prune(now)
        while self._buckets and len(self._buckets) >= self._max_buckets:
            oldest = min(self._buckets, key=lambda bucket: self._buckets[bucket][1])
            del self._buckets[oldest]


rate_limiter = FixedWindowRateLimiter()


def _client_ip(request: Request) -> str:
    """The transport peer address.

    ``X-Forwarded-For`` is deliberately ignored: it is caller-controlled, so
    trusting it would let anyone pick their own bucket. Behind a reverse proxy,
    enable the proxy's trusted-header handling at that hop instead of here.
    """
    return request.client.host if request.client else "unknown"


def _bucket_key(user: dict | None, api_key: str | None, request: Request) -> str:
    """Verified credentials get their own budget; everything else shares the IP's."""
    if user is not None and api_key:
        return "key:" + hashlib.sha256(api_key.encode()).hexdigest()[:32]
    return "ip:" + _client_ip(request)


async def rate_limit_middleware(request: Request, call_next):
    """Enforce the caller's tier quota and expose the resolved user to routes."""
    if not settings.RATE_LIMIT_ENABLED:
        return await call_next(request)

    path = request.url.path
    if path in _EXEMPT_PATHS or path.endswith("/openapi.json"):
        return await call_next(request)

    api_key = request.headers.get("X-API-Key")

    # The admin key drives trusted automation and has no quota.
    admin_key = settings.ADMIN_API_KEY
    if api_key and admin_key and secrets.compare_digest(api_key.encode(), admin_key.encode()):
        request.state.api_user = {"email": "admin", "tier": "admin", "rate_limit": None}
        return await call_next(request)

    user = await resolve_api_key(api_key)
    request.state.api_user = user

    # Unverified callers fall back to the anonymous quota, charged to their IP.
    limit = user["rate_limit"] if user else settings.ANONYMOUS_RATE_LIMIT_PER_MIN
    allowed, remaining, retry_after = rate_limiter.hit(
        _bucket_key(user, api_key, request), limit
    )

    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"detail": f"Rate limit exceeded: {limit} requests per minute for this tier."},
            headers={
                "Retry-After": str(retry_after),
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": "0",
            },
        )

    response = await call_next(request)
    response.headers["X-RateLimit-Limit"] = str(limit)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    return response
