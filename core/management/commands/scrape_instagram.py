"""
Management command to scrape Instagram posts for all accounts.
"""
from django.core.management.base import BaseCommand
from core.models import InstagramAccount, InstagramPost
from core.services import instagram_service
from django.utils import timezone


class Command(BaseCommand):
    help = 'Scrape Instagram posts for all accounts'

    def handle(self, *args, **options):
        accounts = InstagramAccount.objects.all()
        
        if not accounts.exists():
            self.stdout.write(self.style.WARNING('No Instagram accounts found.'))
            return
        
        for account in accounts:
            self.stdout.write(f'Fetching posts for @{account.username}...')
            
            try:
                # Check if account has existing posts
                has_posts = account.posts.exists()
                
                if has_posts:
                    posts_data = instagram_service.get_all_posts_for_username(account.username, max_age_hours=48)
                else:
                    posts_data = instagram_service.get_all_posts_for_username(account.username)
                
                saved_count = 0
                for post_data in posts_data:
                    post, created = InstagramPost.objects.update_or_create(
                        account=account,
                        post_id=post_data['post_id'],
                        defaults={
                            'post_code': post_data.get('post_code', ''),
                            'caption': post_data.get('caption', ''),
                            'taken_at': post_data.get('taken_at'),
                            'image_url': post_data.get('image_url', ''),
                            'video_url': post_data.get('video_url', ''),
                            'is_video': post_data.get('is_video', False),
                            'is_reel': post_data.get('is_reel', False),
                            'is_carousel': post_data.get('is_carousel', False),
                            'carousel_media_count': post_data.get('carousel_media_count', 0),
                            'like_count': post_data.get('like_count', 0),
                            'comment_count': post_data.get('comment_count', 0),
                            'play_count': post_data.get('play_count', 0),
                        }
                    )
                    if created:
                        saved_count += 1
                
                account.last_scraped_at = timezone.now()
                account.save()
                
                self.stdout.write(
                    self.style.SUCCESS(f'Successfully fetched {saved_count} new posts for @{account.username}')
                )
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f'Error fetching posts for @{account.username}: {str(e)}')
                )

