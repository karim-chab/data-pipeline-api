# app/auth.py
import secrets

import aiosqlite
from fastapi import Request, Security, HTTPException, status
from fastapi.security import APIKeyHeader

from app.core.config import settings
from app.db.database import DB_FILE

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


async def resolve_api_key(api_key: str | None) -> dict | None:
    """Look up an active API key. Returns None when the key is unknown or deactivated.

    Never raises: callers decide whether a missing user is a 401 (a key was sent)
    or an anonymous request (no key was sent).
    """
    if not api_key:
        return None

    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT email, tier, rate_limit_per_min, is_active FROM api_keys WHERE key = ?",
            (api_key,),
        ) as cursor:
            user = await cursor.fetchone()

    if not user or user[3] == 0:
        return None

    return {"email": user[0], "tier": user[1], "rate_limit": user[2]}


async def get_current_user_tier(
    request: Request,
    api_key: str = Security(API_KEY_HEADER),
) -> dict:
    """Verifies API Key and returns user info. Allows public access if no key provided (defaults to Free limits).

    The rate-limiting middleware resolves the key before the route runs, so its
    result is reused here instead of hitting SQLite a second time per request.
    """
    if hasattr(request.state, "api_user"):
        user = request.state.api_user
    else:
        user = await resolve_api_key(api_key)

    if user is None:
        if api_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or deactivated API Key.",
            )
        return {
            "email": "anonymous",
            "tier": "free",
            "rate_limit": settings.ANONYMOUS_RATE_LIMIT_PER_MIN,
        }

    return user


def require_pro_tier(user: dict = Security(get_current_user_tier)):
    """Dependency for routes restricted ONLY to paying subscribers."""
    if user["tier"] != "pro":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint requires a Pro Tier subscription ($29/mo). Upgrade to unlock."
        )
    return user


async def require_admin_key(api_key: str | None = Security(API_KEY_HEADER)) -> None:
    """Dependency for operator-only routes (manual pipeline triggers).

    Fails closed: if ADMIN_API_KEY is not configured the routes stay disabled
    rather than silently becoming public.
    """
    admin_key = settings.ADMIN_API_KEY

    if not admin_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin API is not configured (ADMIN_API_KEY is unset).",
        )

    if not api_key or not secrets.compare_digest(api_key.encode(), admin_key.encode()):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin API Key required.",
        )
