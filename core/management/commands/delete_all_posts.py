"""
Management command to delete all posts and reels from the database.
"""
from django.core.management.base import BaseCommand
from core.models import InstagramPost, InstagramCarouselItem


class Command(BaseCommand):
    help = 'Delete all posts and reels from the database'

    def add_arguments(self, parser):
        parser.add_argument(
            '--confirm',
            action='store_true',
            help='Confirm deletion',
        )

    def handle(self, *args, **options):
        # Count all posts (including reels)
        all_posts = InstagramPost.objects.all()
        posts_count = all_posts.filter(is_reel=False).count()
        reels_count = all_posts.filter(is_reel=True).count()
        total_count = all_posts.count()
        
        if not options['confirm']:
            self.stdout.write(
                self.style.WARNING(
                    f'This will delete:\n'
                    f'  - {posts_count} posts\n'
                    f'  - {reels_count} reels\n'
                    f'  - Total: {total_count} items\n\n'
                    f'Use --confirm to proceed.'
                )
            )
            return
        
        # Delete carousel items first (they have foreign key to posts)
        carousel_items_count = InstagramCarouselItem.objects.count()
        InstagramCarouselItem.objects.all().delete()
        
        # Delete all posts (this will cascade delete carousel items, but we already deleted them)
        deleted_count, _ = all_posts.delete()
        
        self.stdout.write(
            self.style.SUCCESS(
                f'Successfully deleted:\n'
                f'  - {posts_count} posts\n'
                f'  - {reels_count} reels\n'
                f'  - {carousel_items_count} carousel items\n'
                f'  - Total: {deleted_count} items'
            )
        )

