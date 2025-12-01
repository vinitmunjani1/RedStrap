"""
Management command to delete all reels from the database.
"""
from django.core.management.base import BaseCommand
from core.models import InstagramPost


class Command(BaseCommand):
    help = 'Delete all reels from the database'

    def add_arguments(self, parser):
        parser.add_argument(
            '--confirm',
            action='store_true',
            help='Confirm deletion',
        )

    def handle(self, *args, **options):
        reels = InstagramPost.objects.filter(is_reel=True)
        count = reels.count()
        
        if not options['confirm']:
            self.stdout.write(
                self.style.WARNING(
                    f'This will delete {count} reels. Use --confirm to proceed.'
                )
            )
            return
        
        deleted_count, _ = reels.delete()
        self.stdout.write(
            self.style.SUCCESS(f'Successfully deleted {deleted_count} reels')
        )

