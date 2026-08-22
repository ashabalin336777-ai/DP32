"""Optional in-container daily run (alternative to host cron)."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from src.pipeline import run_pipeline, setup_pipeline_logging


def _job() -> None:
    logger = logging.getLogger("mcu.pipeline")
    logger.info("scheduler firing pipeline")
    run_pipeline()


def main() -> None:
    setup_pipeline_logging()
    logger = logging.getLogger("mcu.pipeline")
    tz = os.getenv("TZ", "Europe/Moscow")
    hour = int(os.getenv("SCHEDULE_HOUR", "3"))
    minute = int(os.getenv("SCHEDULE_MINUTE", "0"))
    scheduler = BlockingScheduler(timezone=tz)
    scheduler.add_job(_job, CronTrigger(hour=hour, minute=minute, timezone=tz))
    logger.info("scheduler started tz=%s daily=%02d:%02d", tz, hour, minute)
    if os.getenv("SCHEDULE_RUN_ON_START", "").strip() in {"1", "true", "yes"}:
        _job()
    scheduler.start()


if __name__ == "__main__":
    main()
