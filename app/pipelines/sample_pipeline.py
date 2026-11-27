import httpx
from app.pipelines.base import BasePipeline
from app.core.logger import logger
from app.schemas.crypto import CryptoPriceSchema
from app.db.database import insert_crypto_price
from app.core.resilience import async_http_retry


class SamplePipeline(BasePipeline):
    """Concrete pipeline with automated retry resilience."""

    def __init__(self):
        super().__init__(name="Crypto Price Collector")

    @async_http_retry
    async def extract(self) -> dict:
        """Step 1: Fetch raw JSON data with exponential backoff retries."""
        logger.info("Fetching live crypto data...")
        url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=5.0)
            response.raise_for_status()
            return response.json()

    async def transform(self, raw_data: dict) -> CryptoPriceSchema:
        """Step 2: Parse raw JSON into a validated Pydantic model."""
        logger.info("Validating and structuring raw price data with Pydantic...")
        btc_price = raw_data.get("bitcoin", {}).get("usd", 0)
        
        return CryptoPriceSchema(
            asset="Bitcoin",
            symbol="BTC",
            price_usd=float(btc_price)
        )

    async def load(self, cleaned_data: CryptoPriceSchema) -> None:
        """Step 3: Persist cleaned record into the SQLite database."""
        logger.info(f"Persisting cleaned record to SQLite: {cleaned_data.model_dump()}")
        await insert_crypto_price(cleaned_data)