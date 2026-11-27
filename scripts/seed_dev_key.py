"""Dev-only utility: mint an API key and seed a subscriber row in SQLite.

This used to be a public HTTP route, which let anyone mint themselves a Pro key.
Run it from the repository root:

    python -m scripts.seed_dev_key --email dev@local --tier pro

The minted key is printed once, here in the operator's terminal -- never to the
application logs.
"""

import argparse
import asyncio
import json
import secrets
import sys

import aiosqlite

from app.core.config import settings
from app.db.database import DB_FILE, init_db


async def seed_key(email: str, tier: str) -> dict:
    """Insert (or reset) a subscriber and return their new key. Overwrites by design."""
    await init_db()

    limit = settings.PRO_RATE_LIMIT_PER_MIN if tier == "pro" else settings.FREE_RATE_LIMIT_PER_MIN
    new_key = f"sk_{tier}_" + secrets.token_hex(16)

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            """
            INSERT INTO api_keys (email, key, tier, rate_limit_per_min, is_active)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(email) DO UPDATE SET
                key = excluded.key,
                tier = excluded.tier,
                rate_limit_per_min = excluded.rate_limit_per_min,
                is_active = 1
            """,
            (email, new_key, tier, limit),
        )
        await db.commit()

    return {
        "email": email,
        "api_key": new_key,
        "tier": tier,
        "rate_limit_per_min": limit,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Mint a development API key.")
    parser.add_argument("--email", required=True, help="Subscriber email (unique per key).")
    parser.add_argument("--tier", choices=["free", "pro"], default="free")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow minting while ENVIRONMENT=production.",
    )
    args = parser.parse_args()

    if settings.ENVIRONMENT.lower() == "production" and not args.force:
        print(
            "Refusing to mint dev keys while ENVIRONMENT=production "
            "(pass --force if this is intentional).",
            file=sys.stderr,
        )
        return 2

    record = asyncio.run(seed_key(args.email, args.tier))
    print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
