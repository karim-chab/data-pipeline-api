from datetime import datetime, timezone
from pydantic import BaseModel, Field


class CryptoPriceSchema(BaseModel):
    """Data contract for structured cryptocurrency records."""
    asset: str = Field(..., example="Bitcoin")
    symbol: str = Field(..., example="BTC")
    price_usd: float = Field(..., gt=0, description="Price must be a positive float")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = Field(default="processed")