from abc import ABC, abstractmethod
from typing import Any
from app.core.logger import logger


class BasePipeline(ABC):
    """Abstract Base Class for all data pipelines."""

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    async def extract(self) -> Any:
        """Fetch raw data from source (API, DB, file, etc.)"""
        pass

    @abstractmethod
    async def transform(self, raw_data: Any) -> Any:
        """Clean, format, and validate raw data"""
        pass

    @abstractmethod
    async def load(self, cleaned_data: Any) -> None:
        """Persist cleaned data to destination (DB, storage, etc.)"""
        pass

    async def run(self) -> None:
        """Executes the full pipeline workflow sequentially."""
        logger.info(f"--- Starting Pipeline: {self.name} ---")
        try:
            raw_data = await self.extract()
            cleaned_data = await self.transform(raw_data)
            await self.load(cleaned_data)
            logger.info(f"--- Completed Pipeline: {self.name} Successfully ---")
        except Exception as e:
            logger.error(f"Pipeline '{self.name}' failed with error: {str(e)}")
            raise e