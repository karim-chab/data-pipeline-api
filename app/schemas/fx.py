from datetime import datetime, timezone
from pydantic import BaseModel, Field


class FXRateSchema(BaseModel):
    """Data contract for Foreign Exchange rates."""
    base_currency: str = Field(default="USD")
    target_currency: str = Field(..., example="EUR")
    rate: float = Field(..., gt=0, description="Exchange rate must be positive")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = Field(default="processed")