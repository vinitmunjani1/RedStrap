"""
Scheduler service for automatic post fetching using APScheduler.
"""
import logging
import os
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.memory import MemoryJobStore
from django.conf import settings
from django.core.management import call_command
from django.utils import timezone

logger = logging.getLogger(__name__)

# Global scheduler instance
_scheduler = None


def get_scheduler():
    """
    Get the global scheduler instance.
    
    Returns:
        BackgroundScheduler instance or None if not initialized
    """
    return _scheduler


def start_scheduler():
    """
    Initialize and start the APScheduler with automatic post fetching job.
    The job runs the scrape_instagram management command at configured intervals.
    """
    global _scheduler
    
    if _scheduler is not None and _scheduler.running:
        logger.warning("Scheduler is already running")
        return
    
    # Get configuration from settings
    enable_auto_fetch = getattr(settings, 'ENABLE_AUTO_FETCH', False)
    interval_hours = getattr(settings, 'AUTO_FETCH_INTERVAL_HOURS', 8)
    
    if not enable_auto_fetch:
        logger.info("Automatic fetching is disabled. Set ENABLE_AUTO_FETCH=True to enable.")
        return
    
    # Configure job stores and executors
    jobstores = {
        'default': MemoryJobStore()
    }
    executors = {
        'default': ThreadPoolExecutor(5)
    }
    job_defaults = {
        'coalesce': True,  # Combine multiple pending jobs into one
        'max_instances': 1,  # Only one instance of the job can run at a time
        'misfire_grace_time': 3600  # Allow job to run up to 1 hour late
    }
    
    # Create scheduler
    _scheduler = BackgroundScheduler(
        jobstores=jobstores,
        executors=executors,
        job_defaults=job_defaults,
        timezone=settings.TIME_ZONE
    )
    
    # Add the scheduled job
    _scheduler.add_job(
        run_fetch_posts_job,
        'interval',
        hours=interval_hours,
        id='fetch_instagram_posts',
        name='Fetch Instagram Posts',
        replace_existing=True
    )
    
    # Start the scheduler
    _scheduler.start()
    logger.info(
        f"Scheduler started. Automatic post fetching enabled with {interval_hours}-hour interval."
    )
    logger.info(f"Next run scheduled for: {_scheduler.get_job('fetch_instagram_posts').next_run_time}")


def stop_scheduler():
    """
    Gracefully shutdown the scheduler.
    """
    global _scheduler
    
    if _scheduler is None:
        return
    
    if _scheduler.running:
        _scheduler.shutdown(wait=True)
        logger.info("Scheduler stopped")
    
    _scheduler = None


def run_fetch_posts_job():
    """
    Job function that runs the scrape_instagram management command.
    This function is called by the scheduler at configured intervals.
    """
    try:
        logger.info("Starting scheduled post fetch job...")
        call_command('scrape_instagram')
        logger.info("Scheduled post fetch job completed successfully")
    except Exception as e:
        logger.error(f"Error in scheduled post fetch job: {e}", exc_info=True)
        # Don't re-raise - allow scheduler to continue running

