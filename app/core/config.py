from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    PROJECT_NAME: str = "Data Pipeline Engine"
    API_V1_STR: str = "/api/v1"
    ENVIRONMENT: str = "development"
    DATABASE_URL: str = "sqlite:///./data.db"
    DEBUG: bool = False

    # --- Billing (Stripe) ---
    STRIPE_SECRET_KEY: str | None = None
    STRIPE_WEBHOOK_SECRET: str | None = None

    # --- Admin-only automation (/pipelines/run-*) ---
    # When unset, admin endpoints are disabled entirely (fail closed).
    ADMIN_API_KEY: str | None = None

    # --- Rate limiting (requests per minute, per API key) ---
    RATE_LIMIT_ENABLED: bool = True
    ANONYMOUS_RATE_LIMIT_PER_MIN: int = 30
    FREE_RATE_LIMIT_PER_MIN: int = 30
    PRO_RATE_LIMIT_PER_MIN: int = 1000

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
