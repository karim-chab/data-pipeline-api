import sqlite3

import aiosqlite
from datetime import datetime, timezone

from app.core.config import settings
from app.schemas.crypto import CryptoPriceSchema
from app.schemas.fx import FXRateSchema

DB_FILE = "data.db"


async def init_db():
    """Initialize SQLite database tables if they do not exist."""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS crypto_prices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset TEXT NOT NULL,
                symbol TEXT NOT NULL,
                price_usd REAL NOT NULL,
                timestamp TIMESTAMP NOT NULL,
                status TEXT DEFAULT 'processed'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS fx_rates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                base_currency TEXT NOT NULL,
                target_currency TEXT NOT NULL,
                rate REAL NOT NULL,
                timestamp TIMESTAMP NOT NULL,
                status TEXT DEFAULT 'processed'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                key TEXT UNIQUE NOT NULL,
                tier TEXT DEFAULT 'free',
                rate_limit_per_min INTEGER DEFAULT 30,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Idempotency ledger: Stripe redelivers events for days, so each event id
        # must be processed at most once.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS processed_webhook_events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()


# --- Database Insert Operations ---

async def insert_crypto_price(data: CryptoPriceSchema):
    """Insert a single validated Crypto price record."""
    now = datetime.now(timezone.utc).isoformat()
    ts = data.timestamp.isoformat() if data.timestamp else now
    
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            """
            INSERT INTO crypto_prices (asset, symbol, price_usd, timestamp, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (data.asset, data.symbol, data.price_usd, ts, getattr(data, "status", "processed")),
        )
        await db.commit()


async def insert_fx_rates(data_list: list[FXRateSchema]):
    """Insert multiple FX rate records."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_FILE) as db:
        await db.executemany(
            """
            INSERT INTO fx_rates (base_currency, target_currency, rate, timestamp, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    item.base_currency,
                    item.target_currency,
                    item.rate,
                    item.timestamp.isoformat() if getattr(item, "timestamp", None) else now,
                    getattr(item, "status", "processed"),
                )
                for item in data_list
            ],
        )
        await db.commit()


async def provision_pro_subscription(
    event_id: str,
    event_type: str,
    email: str,
    candidate_key: str,
) -> dict:
    """Record a Stripe event and upgrade `email` to Pro in one transaction.

    The event row is the idempotency claim: a redelivered event collides on the
    primary key and the whole transaction rolls back, so a customer is upgraded
    exactly once per event. On conflict the existing key is preserved, so a
    customer's live credential is never rotated out from under them.

    Returns {"outcome": "duplicate" | "provisioned" | "upgraded", "key_prefix": str | None}.
    The full key is deliberately never returned to the caller's logging path.
    """
    async with aiosqlite.connect(DB_FILE) as db:
        try:
            await db.execute(
                "INSERT INTO processed_webhook_events (event_id, event_type) VALUES (?, ?)",
                (event_id, event_type),
            )
        except sqlite3.IntegrityError:
            await db.rollback()
            return {"outcome": "duplicate", "key_prefix": None}

        await db.execute(
            """
            INSERT INTO api_keys (email, key, tier, rate_limit_per_min, is_active)
            VALUES (?, ?, 'pro', ?, 1)
            ON CONFLICT(email) DO UPDATE SET
                tier = 'pro',
                rate_limit_per_min = excluded.rate_limit_per_min,
                is_active = 1
            """,
            (email, candidate_key, settings.PRO_RATE_LIMIT_PER_MIN),
        )

        async with db.execute("SELECT key FROM api_keys WHERE email = ?", (email,)) as cursor:
            row = await cursor.fetchone()

        await db.commit()

    stored_key = row[0]
    return {
        "outcome": "provisioned" if stored_key == candidate_key else "upgraded",
        "key_prefix": stored_key[:12],
    }


# --- Database Query Operations ---

async def get_all_crypto_prices():
    """Fetch all crypto historical records."""
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM crypto_prices ORDER BY id DESC") as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_all_fx_rates():
    """Fetch all FX historical records."""
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM fx_rates ORDER BY id DESC") as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_crypto_analytics():
    """Calculate min/max/avg metrics for BTC."""
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT MIN(price_usd), MAX(price_usd), AVG(price_usd), COUNT(*) FROM crypto_prices"
        ) as cursor:
            row = await cursor.fetchone()
            return {
                "asset": "BTC",
                "min_price": row[0],
                "max_price": row[1],
                "avg_price": round(row[2], 2) if row[2] else 0,
                "total_samples": row[3]
            }


async def get_fx_analytics():
    """Calculate rate aggregations grouped by currency pair."""
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT target_currency, MIN(rate) as min_rate, MAX(rate) as max_rate, AVG(rate) as avg_rate 
            FROM fx_rates GROUP BY target_currency
            """
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_pro_cross_asset_analytics():
    """Pro Analytics: Calculates live BTC valuation across tracked fiat currencies."""
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        
        async with db.execute(
            "SELECT price_usd, timestamp FROM crypto_prices ORDER BY id DESC LIMIT 1"
        ) as cursor:
            latest_crypto = await cursor.fetchone()

        async with db.execute(
            """
            SELECT target_currency, rate FROM fx_rates 
            WHERE id IN (SELECT MAX(id) FROM fx_rates GROUP BY target_currency)
            """
        ) as cursor:
            latest_fx = await cursor.fetchall()

        if not latest_crypto:
            return {"error": "No crypto data available."}

        btc_usd = latest_crypto["price_usd"]
        conversions = {"USD": btc_usd}

        for row in latest_fx:
            conversions[row["target_currency"]] = round(btc_usd * row["rate"], 2)

        return {
            "asset": "Bitcoin",
            "base_price_usd": btc_usd,
            "fiat_valuations": conversions,
            "calculated_at": latest_crypto["timestamp"]
        }
