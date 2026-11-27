from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.pipelines.sample_pipeline import SamplePipeline
from app.pipelines.fx_pipeline import FXPipeline
from app.core.logger import logger

scheduler = AsyncIOScheduler()


async def scheduled_crypto_job() -> None:
    """Automated Crypto ETL job."""
    logger.info("⏰ Cron Trigger: Running automated Crypto ETL job...")
    try:
        pipeline = SamplePipeline()
        await pipeline.run()
    except Exception as e:
        logger.error(f"Scheduled Crypto job failed: {str(e)}")


async def scheduled_fx_job() -> None:
    """Automated FX Rates ETL job."""
    logger.info("⏰ Cron Trigger: Running automated FX ETL job...")
    try:
        pipeline = FXPipeline()
        await pipeline.run()
    except Exception as e:
        logger.error(f"Scheduled FX job failed: {str(e)}")


def start_scheduler() -> None:
    """Start background scheduler and register interval jobs."""
    # Run Crypto pipeline every 60 seconds
    scheduler.add_job(
        scheduled_crypto_job,
        trigger="interval",
        seconds=60,
        id="crypto_collector_job",
        replace_existing=True,
    )
    
    # Run FX pipeline every 2 minutes (120 seconds)
    scheduler.add_job(
        scheduled_fx_job,
        trigger="interval",
        seconds=120,
        id="fx_collector_job",
        replace_existing=True,
    )
    
    scheduler.start()
    logger.info("Background scheduler initialized (Crypto: 60s, FX: 120s).")


def stop_scheduler() -> None:
    """Gracefully stop background scheduler."""
    if scheduler.running:
        scheduler.shutdown()
        logger.info("Background scheduler shut down successfully.")