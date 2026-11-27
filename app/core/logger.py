import logging
import sys
from app.core.config import settings

def setup_logger() -> logging.Logger:
    logger = logging.getLogger(settings.PROJECT_NAME)
    
    # Avoid duplicate handlers if logger is already initialized
    if logger.hasHandlers():
        return logger

    logger.setLevel(logging.DEBUG if settings.DEBUG else logging.INFO)

    # Standardized output format: Timestamp - Log Level - Message
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Output logs to standard stdout terminal
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger

logger = setup_logger()