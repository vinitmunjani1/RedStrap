"""
Django app configuration for core app.
"""
import logging
from django.apps import AppConfig

logger = logging.getLogger(__name__)


class CoreConfig(AppConfig):
    """Configuration for the core app."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'
    verbose_name = 'Core'
    
    def ready(self):
        """
        Called when Django app is ready.
        Start the scheduler if automatic fetching is enabled.
        """
        # Only start scheduler when running the server (not during migrations or other commands)
        import sys
        if 'runserver' in sys.argv:
            try:
                from core.services.scheduler_service import start_scheduler
                start_scheduler()
            except Exception as e:
                logger.error(f"Error starting scheduler: {e}", exc_info=True)

