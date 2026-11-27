from contextlib import asynccontextmanager
import secrets

import stripe
from fastapi import Request, Header, HTTPException, Depends, FastAPI

from app.core.config import settings
from app.core.logger import logger
from app.core.ratelimit import rate_limit_middleware
from app.pipelines.sample_pipeline import SamplePipeline
from app.pipelines.fx_pipeline import FXPipeline
from app.schemas.crypto import CryptoPriceSchema
from app.schemas.fx import FXRateSchema
from app.db.database import (
    init_db, 
    get_all_crypto_prices, 
    get_all_fx_rates,
    get_crypto_analytics,
    get_fx_analytics,
    get_pro_cross_asset_analytics,
    provision_pro_subscription,
)
from app.core.scheduler import start_scheduler, stop_scheduler
from app.auth import get_current_user_tier, require_pro_tier, require_admin_key


# Settings reads the .env file, so this is the single source of truth for secrets.
stripe.api_key = settings.STRIPE_SECRET_KEY
STRIPE_WEBHOOK_SECRET = settings.STRIPE_WEBHOOK_SECRET


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle manager."""
    logger.info("Starting up application and initializing database...")
    await init_db()
    start_scheduler()  
    yield
    stop_scheduler()   
    logger.info("Shutting down application...")


app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan
)

# Enforces each caller's rate_limit_per_min before the route runs.
app.middleware("http")(rate_limit_middleware)


# --- System & Health Endpoints ---

@app.get("/")
async def root():
    return {
        "status": "online",
        "app_name": settings.PROJECT_NAME,
        "environment": settings.ENVIRONMENT
    }


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


# --- Manual Pipeline Trigger Endpoints (operator-only) ---
# Dev key minting lives in scripts/seed_dev_key.py: it must never be reachable
# over HTTP, otherwise anyone can mint themselves a Pro key.

@app.post(
    "/pipelines/run-sample",
    response_model=CryptoPriceSchema,
    dependencies=[Depends(require_admin_key)],
)
async def run_sample_pipeline():
    """Trigger the Crypto ETL pipeline manually."""
    pipeline = SamplePipeline()
    raw_data = await pipeline.extract()
    cleaned_data = await pipeline.transform(raw_data)
    await pipeline.load(cleaned_data)
    return cleaned_data


@app.post(
    "/pipelines/run-fx",
    response_model=list[FXRateSchema],
    dependencies=[Depends(require_admin_key)],
)
async def run_fx_pipeline():
    """Trigger the FX ETL pipeline manually."""
    pipeline = FXPipeline()
    raw_data = await pipeline.extract()
    cleaned_data = await pipeline.transform(raw_data)
    await pipeline.load(cleaned_data)
    return cleaned_data


# --- Standard Data & Analytics Endpoints ---

@app.get("/data/crypto")
async def get_stored_crypto_data(user: dict = Depends(get_current_user_tier)):
    """Fetch all stored historical crypto records."""
    records = await get_all_crypto_prices()
    return {"user_tier": user["tier"], "total_records": len(records), "data": records}


@app.get("/data/fx")
async def get_stored_fx_data(user: dict = Depends(get_current_user_tier)):
    """Fetch all stored historical FX rate records."""
    records = await get_all_fx_rates()
    return {"user_tier": user["tier"], "total_records": len(records), "data": records}


@app.get("/analytics/crypto")
async def get_crypto_analytics_summary(user: dict = Depends(get_current_user_tier)):
    """Fetch min/max/avg statistical summary and total change percentage for BTC."""
    return await get_crypto_analytics()


@app.get("/analytics/fx")
async def get_fx_analytics_summary(user: dict = Depends(get_current_user_tier)):
    """Fetch rate aggregations (min/max/avg) grouped by currency pair."""
    return await get_fx_analytics()


# --- Monetized Pro-Tier Exclusive Endpoints ---

@app.get("/analytics/pro/arbitrage")
async def get_pro_arbitrage_analytics(user: dict = Depends(require_pro_tier)):
    """Pro-only endpoint calculating real-time cross-asset multi-currency BTC values."""
    data = await get_pro_cross_asset_analytics()
    return {
        "status": "success",
        "subscriber": user["email"],
        "tier": user["tier"],
        "analytics": data
    }


# --- Stripe Monetization Routes ---

@app.post("/create-checkout-session")
async def create_checkout_session(email: str):
    """Generates a Stripe Checkout URL for $29/mo Pro Subscription."""
    try:
        checkout_session = stripe.checkout.Session.create(
            customer_email=email,
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product_data': {
                        'name': 'Financial Data API - Pro Tier',
                        'description': '1,000 req/min + Real-Time Arbitrage Analytics',
                    },
                    'unit_amount': 2900,  # $29.00 in cents
                    'recurring': {'interval': 'month'},
                },
                'quantity': 1,
            }],
            mode='subscription',
            success_url='http://localhost:8000/docs?payment=success',
            cancel_url='http://localhost:8000/docs?payment=cancelled',
        )
        return {"checkout_url": checkout_session.url}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request, stripe_signature: str | None = Header(None)):
    """Automated Webhook listener that issues Pro Keys immediately upon payment.

    Idempotent by event id: Stripe retries deliveries for up to three days, and
    a replay must never re-provision or rotate a customer's existing key.
    """
    payload = await request.body()

    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Stripe webhook is not configured.")

    try:
        event = stripe.Webhook.construct_event(
            payload, stripe_signature, STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    event_id = event["id"]
    event_type = event["type"]

    if event_type != "checkout.session.completed":
        return {"status": "ignored", "event_id": event_id}

    session = event["data"]["object"]

    # Safely extract dictionary attributes from Stripe object
    session_dict = session.to_dict() if hasattr(session, "to_dict") else dict(session)

    # checkout.session.completed also fires for async payment methods before the
    # money settles, so only provision once Stripe reports the charge as paid.
    payment_status = session_dict.get("payment_status")
    if session_dict.get("mode") != "subscription" or payment_status not in ("paid", "no_payment_required"):
        logger.warning(
            f"Stripe event {event_id} skipped: mode={session_dict.get('mode')} "
            f"payment_status={payment_status}"
        )
        return {"status": "skipped", "event_id": event_id}

    customer_email = (
        session_dict.get("customer_email")
        or session_dict.get("customer_details", {}).get("email")
    )

    if not customer_email:
        # Acknowledge so Stripe stops retrying, but surface it for reconciliation.
        logger.error(
            f"Stripe event {event_id} has no customer email; manual provisioning required."
        )
        return {"status": "unprocessable", "event_id": event_id}

    result = await provision_pro_subscription(
        event_id=event_id,
        event_type=event_type,
        email=customer_email,
        candidate_key="sk_pro_" + secrets.token_hex(16),
    )

    if result["outcome"] == "duplicate":
        logger.info(f"Stripe event {event_id} already processed; no key change.")
        return {"status": "duplicate", "event_id": event_id}

    # Never log the full key: logs are shipped, indexed and retained.
    logger.info(
        f"New Pro Subscriber: {customer_email} | "
        f"key_prefix={result['key_prefix']} | event_id={event_id} | {result['outcome']}"
    )

    return {"status": result["outcome"], "event_id": event_id}
