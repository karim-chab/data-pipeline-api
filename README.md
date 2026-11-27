# Data Pipeline Engine

A small financial data service: two resilient ETL pipelines (crypto spot prices, FX rates)
persist to SQLite, and a FastAPI app exposes the stored history, aggregate analytics and a
Pro-only cross-asset valuation behind tiered API keys, with Stripe Checkout handling upgrades.

Built to be read as much as run: explicit retry policy, validated data contracts, enforced
per-tier quotas, and a webhook that is safe to be delivered twice.

## Architecture

```
   CoinGecko ─────────────┐
                          │  extract → transform → load
   Frankfurter ───────────┤  (httpx + tenacity retry, Pydantic validation)
                          ▼
                    ┌───────────┐
                    │ pipelines │  CryptoPricePipeline / FXPipeline
                    └─────┬─────┘
                          ▼
                 SQLite (data.db)
   crypto_prices · fx_rates · api_keys · processed_webhook_events
                          ▲
                          │ read
   FastAPI ── rate-limit middleware (per tier) ── API-key dependencies ── routes
                          ▲
                    Stripe webhook (idempotent provisioning)
```

`APScheduler` runs the two collectors on intervals (60 s crypto, 120 s FX) from the app's
lifespan, so the service is self-feeding in development.

| Path | Responsibility |
| --- | --- |
| `app/main.py` | App factory, lifespan, routes, Stripe webhook |
| `app/auth.py` | API-key resolution, tier gates, admin key check |
| `app/core/ratelimit.py` | Fixed-window limiter + middleware, bucketing rules |
| `app/core/config.py` | All settings and secrets (single source of truth) |
| `app/core/resilience.py` | Shared async retry policy for outbound HTTP |
| `app/core/scheduler.py` | Interval jobs for both pipelines |
| `app/pipelines/` | `BasePipeline` template (extract/transform/load/run) + implementations |
| `app/db/database.py` | Schema init, inserts, queries, webhook provisioning transaction |
| `app/schemas/` | Pydantic data contracts |
| `scripts/seed_dev_key.py` | Dev-only API-key minting CLI (deliberately not an HTTP route) |

## Quickstart

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env              # fill in values if you need billing
python -m scripts.seed_dev_key --email you@example.com --tier pro
uvicorn app.main:app --reload --port 8000
```

OpenAPI docs: <http://127.0.0.1:8000/docs>. The database file and its tables are created
on first startup.

The seed command prints the key once, in your terminal only — keys are never written to the
application log.

```bash
curl http://127.0.0.1:8000/data/crypto -H "X-API-Key: sk_pro_..."
curl http://127.0.0.1:8000/analytics/fx -H "X-API-Key: sk_free_..."
```

## API surface

| Method | Path | Required credential |
| --- | --- | --- |
| GET | `/` , `/health` | none |
| POST | `/pipelines/run-sample` , `/pipelines/run-fx` | `ADMIN_API_KEY` |
| GET | `/data/crypto` , `/data/fx` | any valid key, or anonymous |
| GET | `/analytics/crypto` , `/analytics/fx` | any valid key, or anonymous |
| GET | `/analytics/pro/arbitrage` | Pro tier |
| POST | `/create-checkout-session` | none |
| POST | `/webhooks/stripe` | Stripe signature |

## Tiers and rate limiting

| Tier | Requests/min | How you get it |
| --- | --- | --- |
| anonymous | 30 | No key sent |
| free | 30 | `scripts.seed_dev_key --tier free` |
| pro | 1000 | Stripe Checkout, or `scripts.seed_dev_key --tier pro` |
| admin | unlimited | `ADMIN_API_KEY`, for pipeline triggers |

The limit lives on the `api_keys` row (`rate_limit_per_min`) and is read on every request, so
changing a tier's quota is a data change, not a deploy.

**Bucketing.** A *verified* key gets its own counter, identified by `sha256(key)` — raw
credentials are never held in the limiter's memory. Anonymous and unverified traffic (no key,
unknown key, deactivated key) shares one counter **per client IP**, so sending a fresh junk
`X-API-Key` on every request cannot reset a window or allocate a counter. `X-Forwarded-For` is
ignored by design: it is caller-controlled, so trusting it would let anyone choose their own
bucket. The counter map is hard-capped (`_MAX_BUCKETS`) and prunes expired windows, then evicts
the counter nearest expiry, so memory stays bounded under a flood.

Every limited response carries `X-RateLimit-Limit` and `X-RateLimit-Remaining`; a blocked
request returns `429` with `Retry-After`. `/webhooks/stripe` is exempt — Stripe retries must
never be throttled by us.

## Billing

`POST /create-checkout-session` returns a Stripe Checkout URL. On
`checkout.session.completed`, the webhook:

1. verifies the Stripe signature;
2. ignores any session that is not a `subscription` or is not yet `paid`
   (async payment methods complete before settlement);
3. claims the event id in `processed_webhook_events` and upgrades the subscriber in the **same
   transaction** — a redelivered event rolls back and returns `duplicate`;
4. preserves the existing key on upgrade (`ON CONFLICT(email) DO UPDATE` without touching
   `key`), so a paying customer's live credential is never rotated out from under them;
5. logs `key_prefix` only, never the key itself.

## Data model

| Table | Contents |
| --- | --- |
| `crypto_prices` | `asset`, `symbol`, `price_usd`, `timestamp`, `status` |
| `fx_rates` | `base_currency`, `target_currency`, `rate`, `timestamp`, `status` |
| `api_keys` | `email` (unique), `key` (unique), `tier`, `rate_limit_per_min`, `is_active` |
| `processed_webhook_events` | `event_id` (primary key), `event_type`, `processed_at` — the idempotency ledger |

## Tests

```bash
pip install pytest
python -m pytest -q
```

The suite runs entirely against a temporary SQLite file with the scheduler stubbed: no network,
no writes to `data.db`. It covers key resolution and tier gates, the admin-only pipeline routes,
quota enforcement and the junk-key bucketing rules, the counter-map bound, and webhook
idempotency, non-rotation and log redaction.

## Design notes and known limitations

- The limiter is **in-process**. Behind multiple workers or replicas each process keeps its own
  budget; `FixedWindowRateLimiter.hit(bucket, limit)` is the seam for a shared (Redis) backend.
- Behind a reverse proxy, configure trusted-header handling at the proxy hop — otherwise all
  clients share one IP bucket.
- The scheduler runs inside the web process, so `--workers N` means N schedulers. Moving it to
  its own process is the intended fix.
- Only `checkout.session.completed` is handled: a cancelled subscription is **not** downgraded
  yet.
- Keys are stored in plaintext and the buyer's key is not delivered anywhere — mint it with the
  CLI or read it from the table. Hashing at rest and one-time delivery are the next steps.
- Timestamps record ingestion time, not source event time, and the FX source publishes daily
  while it is polled every 120 s — treat `fx_rates` as snapshots, not ticks.
- SQLite is a single-writer database; it is a deliberate development choice, not a scaling one.

## License

MIT — see [LICENSE](LICENSE).
