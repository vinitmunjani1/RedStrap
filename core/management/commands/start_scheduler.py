"""
Management command to manually start the scheduler in foreground.
Useful for production deployments where you want to run scheduler as a separate process.
"""
import signal
import sys
import logging
from django.core.management.base import BaseCommand
from core.services.scheduler_service import start_scheduler, stop_scheduler

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Start the APScheduler in foreground for automatic post fetching'

    def add_arguments(self, parser):
        parser.add_argument(
            '--interval',
            type=int,
            default=8,
            help='Fetch interval in hours (default: 8)',
        )

    def handle(self, *args, **options):
        from django.conf import settings
        
        # Override interval if provided
        if options['interval']:
            settings.AUTO_FETCH_INTERVAL_HOURS = options['interval']
        
        # Ensure auto fetch is enabled
        if not getattr(settings, 'ENABLE_AUTO_FETCH', False):
            self.stdout.write(
                self.style.WARNING(
                    'Automatic fetching is disabled. Set ENABLE_AUTO_FETCH=True in settings or environment.'
                )
            )
            self.stdout.write('Starting scheduler anyway (you can enable it later)...')
        
        # Setup signal handlers for graceful shutdown
        def signal_handler(sig, frame):
            self.stdout.write(self.style.WARNING('\nReceived shutdown signal. Stopping scheduler...'))
            stop_scheduler()
            sys.exit(0)
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        # Start scheduler
        self.stdout.write(self.style.SUCCESS('Starting scheduler...'))
        start_scheduler()
        
        if not getattr(settings, 'ENABLE_AUTO_FETCH', False):
            self.stdout.write(
                self.style.WARNING(
                    'Scheduler started but automatic fetching is disabled. '
                    'Set ENABLE_AUTO_FETCH=True to enable automatic fetching.'
                )
            )
        
        # Keep the process running
        try:
            self.stdout.write(self.style.SUCCESS('Scheduler is running. Press Ctrl+C to stop.'))
            import time
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            signal_handler(signal.SIGINT, None)

