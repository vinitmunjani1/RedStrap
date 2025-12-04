"""
Management command to scrape Instagram posts for all accounts.
Enhanced with keyword extraction, Discord notifications, and concurrent processing.
"""
import logging
import os
from django.core.management.base import BaseCommand
from django.db import transaction
from django.conf import settings
from django.utils import timezone
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from core.models import InstagramAccount, InstagramPost, InstagramKeyword, InstagramCarouselItem
from core.services import instagram_service, keyword_service
from core.services.discord_service import send_discord_webhook
from core.views import _extract_keywords_for_post, filter_recent_posts

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Scrape Instagram posts for all accounts with keyword extraction and Discord notifications'

    def handle(self, *args, **options):
        accounts = list(InstagramAccount.objects.all())
        
        if not accounts:
            self.stdout.write(self.style.WARNING('No Instagram accounts found.'))
            return
        
        accounts_total = len(accounts)
        self.stdout.write(f'Found {accounts_total} account(s) to process...')
        
        total_posts = 0
        total_errors = 0
        all_new_posts_for_keywords = []
        
        # Get number of API keys from settings for optimal worker count
        api_keys = getattr(settings, 'RAPIDAPI_KEYS', [])
        num_api_keys = len(api_keys) if api_keys else 13
        max_workers = min(accounts_total, num_api_keys)
        
        self.stdout.write(f'Processing {accounts_total} accounts concurrently with {max_workers} workers...')
        
        def fetch_account_posts(account):
            """Fetch posts for a single account - designed for concurrent execution."""
            account_new_posts = []
            account_saved_count = 0
            
            try:
                username = account.username.strip().lstrip('@').lower()
                if not username:
                    logger.warning(f'Skipping account with empty username: {account.username}')
                    return 0, [], None
                
                has_posts = account.posts.exists()
                
                def save_posts_batch(posts_batch):
                    """Save a batch of posts incrementally."""
                    nonlocal account_saved_count, account_new_posts
                    batch_new_posts = 0
                    
                    for post_data in posts_batch:
                        def safe_bool(value, default=False):
                            if value is None:
                                return default
                            if isinstance(value, bool):
                                return value
                            if isinstance(value, dict) and not value:
                                return default
                            if isinstance(value, (list, dict, str)) and not value:
                                return default
                            return bool(value)
                        
                        is_reel = safe_bool(post_data.get('is_reel'), False)
                        
                        post, created = InstagramPost.objects.update_or_create(
                            account=account,
                            post_id=post_data['post_id'],
                            defaults={
                                'post_code': post_data.get('post_code', ''),
                                'caption': post_data.get('caption', ''),
                                'taken_at': post_data.get('taken_at'),
                                'image_url': post_data.get('image_url', ''),
                                'video_url': post_data.get('video_url', ''),
                                'is_video': safe_bool(post_data.get('is_video'), False),
                                'is_reel': is_reel,
                                'is_carousel': safe_bool(post_data.get('is_carousel'), False),
                                'carousel_media_count': post_data.get('carousel_media_count', 0),
                                'like_count': post_data.get('like_count', 0),
                                'comment_count': post_data.get('comment_count', 0),
                                'play_count': post_data.get('play_count', 0),
                            }
                        )
                        
                        if post.is_carousel and 'carousel_items' in post_data:
                            post.carousel_items.all().delete()
                            for item_idx, item_data in enumerate(post_data.get('carousel_items', [])):
                                InstagramCarouselItem.objects.create(
                                    post=post,
                                    item_index=item_idx,
                                    image_url=item_data.get('image_url', ''),
                                    video_url=item_data.get('video_url', ''),
                                    is_video=item_data.get('is_video', False),
                                )
                        
                        if created:
                            account_saved_count += 1
                            batch_new_posts += 1
                            if post.caption and post.caption.strip():
                                account_new_posts.append(post)
                
                # Fetch posts with conditional logic
                if has_posts:
                    # Fetch only 2 pages (24 posts) when posts exist in database
                    logger.info(f"Account {username} has existing posts, fetching 2 pages (24 posts) only")
                    posts_data = instagram_service.get_all_posts_for_username(
                        username, max_pages=2, save_callback=save_posts_batch
                    )
                else:
                    # No posts in database: fetch all posts (up to 600 limit from TEST_MODE_POSTS_LIMIT)
                    logger.info(f"Account {username} has no posts in database, fetching all available posts (up to 600)")
                    posts_data = instagram_service.get_all_posts_for_username(
                        username, save_callback=save_posts_batch
                    )
                
                account.last_scraped_at = timezone.now()
                account.save()
                
                # Send Discord notification for posts from last 24 hours
                if account_new_posts:
                    recent_posts = filter_recent_posts(account_new_posts, hours=24)
                    if recent_posts:
                        webhook_url = getattr(settings, 'DISCORD_WEBHOOK_URL', '')
                        if webhook_url:
                            try:
                                send_discord_webhook(webhook_url, username, recent_posts)
                                logger.info(f"Sent Discord notification for {len(recent_posts)} recent posts from @{username}")
                            except Exception as e:
                                logger.error(f"Error sending Discord notification for @{username}: {e}", exc_info=True)
                
                return account_saved_count, account_new_posts, None
                
            except Exception as e:
                logger.error(f"Error fetching posts for @{account.username}: {e}", exc_info=True)
                return 0, [], str(e)
        
        # Process accounts concurrently
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_account = {
                executor.submit(fetch_account_posts, account): account
                for account in accounts
            }
            
            completed_accounts = 0
            for future in as_completed(future_to_account):
                account = future_to_account[future]
                try:
                    saved_count, new_posts, error = future.result()
                    completed_accounts += 1
                    
                    if error:
                        total_errors += 1
                        self.stdout.write(
                            self.style.ERROR(f'Error fetching posts for @{account.username}: {error}')
                        )
                    else:
                        total_posts += saved_count
                        if new_posts:
                            all_new_posts_for_keywords.extend(new_posts)
                        self.stdout.write(
                            self.style.SUCCESS(
                                f'[{completed_accounts}/{accounts_total}] Fetched {saved_count} new posts for @{account.username}'
                            )
                        )
                except Exception as e:
                    total_errors += 1
                    logger.error(f"Exception processing account @{account.username}: {e}", exc_info=True)
                    self.stdout.write(
                        self.style.ERROR(f'Exception fetching posts for @{account.username}: {str(e)}')
                    )
        
        # Extract keywords for all newly fetched posts
        total_keywords_extracted = 0
        if all_new_posts_for_keywords:
            self.stdout.write(f'\nExtracting keywords from {len(all_new_posts_for_keywords)} new posts...')
            
            max_workers = min(len(all_new_posts_for_keywords), (os.cpu_count() or 4) * 2, 20)
            results = []
            keyword_errors = 0
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_post = {
                    executor.submit(_extract_keywords_for_post, post): post
                    for post in all_new_posts_for_keywords
                }
                
                for future in as_completed(future_to_post):
                    post = future_to_post[future]
                    try:
                        post_id, keywords, error = future.result()
                        results.append({
                            'post_id': post_id,
                            'keywords': keywords,
                            'error': error
                        })
                        if error:
                            keyword_errors += 1
                        else:
                            total_keywords_extracted += len(keywords)
                    except Exception as e:
                        logger.error(f"Exception extracting keywords for post {post.id}: {e}", exc_info=True)
                        keyword_errors += 1
                        results.append({
                            'post_id': post.id,
                            'keywords': [],
                            'error': str(e)
                        })
            
            # Batch database operations for keyword saving
            post_map = {post.id: post for post in all_new_posts_for_keywords}
            keywords_to_create = []
            posts_to_update = []
            post_ids_to_delete_keywords = []
            
            with transaction.atomic():
                for result in results:
                    post_id = result['post_id']
                    keywords = result['keywords']
                    error = result['error']
                    
                    if error:
                        continue
                    
                    post = post_map.get(post_id)
                    if not post:
                        continue
                    
                    post_ids_to_delete_keywords.append(post_id)
                    
                    for kw_data in keywords:
                        keywords_to_create.append(
                            InstagramKeyword(
                                post=post,
                                keyword=kw_data['keyword'],
                                similarity=kw_data['similarity']
                            )
                        )
                    
                    post.keywords_extracted = True
                    posts_to_update.append(post)
                
                if post_ids_to_delete_keywords:
                    InstagramKeyword.objects.filter(post_id__in=post_ids_to_delete_keywords).delete()
                
                if keywords_to_create:
                    InstagramKeyword.objects.bulk_create(keywords_to_create, batch_size=100)
                    logger.info(f"Bulk created {len(keywords_to_create)} keywords")
                
                if posts_to_update:
                    InstagramPost.objects.bulk_update(posts_to_update, ['keywords_extracted'], batch_size=100)
                    logger.info(f"Bulk updated {len(posts_to_update)} posts")
            
            if total_keywords_extracted > 0:
                self.stdout.write(
                    self.style.SUCCESS(
                        f'Extracted {total_keywords_extracted} keywords from {len(posts_to_update)} posts'
                    )
                )
            if keyword_errors > 0:
                self.stdout.write(
                    self.style.WARNING(f'Encountered {keyword_errors} errors during keyword extraction')
                )
        
        # Summary
        self.stdout.write('\n' + '='*60)
        if total_posts > 0:
            self.stdout.write(
                self.style.SUCCESS(f'Successfully fetched {total_posts} new posts total!')
            )
        if total_keywords_extracted > 0:
            self.stdout.write(
                self.style.SUCCESS(f'Successfully extracted {total_keywords_extracted} keywords!')
            )
        if total_errors > 0:
            self.stdout.write(
                self.style.WARNING(f'Encountered {total_errors} errors during fetching.')
            )
        self.stdout.write('='*60)

