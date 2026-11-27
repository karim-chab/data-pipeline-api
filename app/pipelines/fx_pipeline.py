import httpx
from app.pipelines.base import BasePipeline
from app.core.logger import logger
from app.schemas.fx import FXRateSchema
from app.db.database import insert_fx_rates
from app.core.resilience import async_http_retry


class FXPipeline(BasePipeline):
    """Pipeline that fetches and records USD foreign exchange rates."""

    def __init__(self):
        super().__init__(name="FX Exchange Rate Collector")

    @async_http_retry
    async def extract(self) -> dict:
        """Step 1: Fetch raw currency exchange rates from Frankfurter API."""
        logger.info("Fetching FX exchange rate data...")
        url = "https://api.frankfurter.dev/v1/latest?from=USD&to=EUR,GBP,JPY,CAD"
        
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(url, timeout=5.0)
            response.raise_for_status()
            return response.json()

    async def transform(self, raw_data: dict) -> list[FXRateSchema]:
        """Step 2: Parse raw JSON response into a list of FXRateSchema models."""
        logger.info("Validating and structuring FX rates with Pydantic...")
        base = raw_data.get("base", "USD")
        rates_dict = raw_data.get("rates", {})
        
        cleaned_records = []
        for currency, rate in rates_dict.items():
            cleaned_records.append(
                FXRateSchema(
                    base_currency=base,
                    target_currency=currency,
                    rate=float(rate),
                )
            )
        return cleaned_records

    async def load(self, cleaned_data: list[FXRateSchema]) -> None:
        """Step 3: Persist cleaned FX rate records into SQLite."""
        logger.info(f"Persisting {len(cleaned_data)} FX rate records to SQLite...")
        await insert_fx_rates(cleaned_data)