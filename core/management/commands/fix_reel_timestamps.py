"""
Management command to fix reel timestamps by re-extracting from post IDs.
"""
from django.core.management.base import BaseCommand
from core.models import InstagramPost
from core.services.instagram_service import _extract_timestamp_from_post_id
from django.utils import timezone


class Command(BaseCommand):
    help = 'Fix reel timestamps by extracting from post IDs'

    def handle(self, *args, **options):
        reels = InstagramPost.objects.filter(is_reel=True)
        total = reels.count()
        
        self.stdout.write(f'Found {total} reels to process...')
        
        fixed_count = 0
        failed_count = 0
        
        for reel in reels:
            extracted = _extract_timestamp_from_post_id(reel.post_id)
            if extracted:
                reel.taken_at = extracted
                reel.save(update_fields=['taken_at'])
                fixed_count += 1
            else:
                failed_count += 1
        
        self.stdout.write(
            self.style.SUCCESS(
                f'Fixed {fixed_count} reels, {failed_count} failed (using current timestamp)'
            )
        )

