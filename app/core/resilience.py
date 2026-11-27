import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)
import logging
from app.core.logger import logger

# Configure standard logger bridge for tenacity retry messages
tenacity_logger = logging.getLogger("Data Pipeline Engine")

# Reusable retry decorator for async network calls
async_http_retry = retry(
    stop=stop_after_attempt(3),  # Max 3 total attempts
    wait=wait_exponential(multiplier=1, min=2, max=10),  # Wait 2s, 4s, 8s...
    retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
    before_sleep=before_sleep_log(tenacity_logger, logging.WARNING),
    reraise=True,
)