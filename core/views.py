"""
Django views for Instagram and Reddit scraping application.
"""
import logging
from collections import defaultdict
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.contrib import messages
from django.utils import timezone
from django.db.models import Q, Count, Avg, Sum
from django.db import transaction
from datetime import timedelta

logger = logging.getLogger(__name__)
from .models import (
    InstagramAccount, InstagramPost, InstagramCarouselItem,
    Subreddit, RedditPost, RedditKeyword
)
from .forms import InstagramAccountForm, SubredditForm
from .services import instagram_service, reddit_service, keyword_service


def register_view(request):
    """User registration view."""
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(request, 'Registration successful!')
            return redirect('dashboard')
    else:
        form = UserCreationForm()
    return render(request, 'core/register.html', {'form': form})


@login_required
def dashboard_view(request):
    """Main dashboard showing recent posts grouped by username."""
    from collections import defaultdict
    
    # Get user's Instagram accounts
    accounts = InstagramAccount.objects.filter(user=request.user)
    
    # Get all posts and reels - show only last 48 hours per username
    # Include both regular posts and reels
    recent_posts_time = timezone.now() - timedelta(hours=48)
    all_posts = list(InstagramPost.objects.filter(
        account__user=request.user,
        taken_at__gte=recent_posts_time
    ).select_related('account').order_by('-taken_at'))
    
    # Log for debugging
    posts_count = sum(1 for p in all_posts if not p.is_reel)
    reels_count = sum(1 for p in all_posts if p.is_reel)
    logger.info(f"Dashboard: Found {len(all_posts)} items ({posts_count} posts, {reels_count} reels, last 48 hours) for user {request.user.username}")
    
    # Group posts by username - only include usernames with posts from last 48 hours
    posts_by_username = defaultdict(list)
    account_id_map = {}  # Map username to account_id
    
    for post in all_posts:
        posts_by_username[post.account.username].append(post)
        # Store account_id mapping
        if post.account.username not in account_id_map:
            account_id_map[post.account.username] = post.account.id
    
    # Only include accounts that have posts in the last 48 hours
    # Accounts with no recent posts will not appear on dashboard
    
    # Sort usernames by most recent post and create list of dictionaries
    # Posts are already filtered to last 48 hours, so show all of them
    username_posts_list = []
    for username, posts in posts_by_username.items():
        # Sort posts by taken_at for each username (most recent first)
        posts.sort(key=lambda x: x.taken_at, reverse=True)
        # Show all posts from last 48 hours (no limit needed since already filtered)
        limited_posts = posts
        
        # Get account_id from map
        account_id = account_id_map.get(username)
        
        if account_id:  # Only add if we have a valid account_id
            username_posts_list.append({
                'username': username,
                'account_id': account_id,
                'posts': limited_posts,
                'count': len(limited_posts)
            })
    
    # Sort by most recent post across all usernames
    username_posts_list.sort(
        key=lambda x: max(p.taken_at for p in x['posts']) if x['posts'] else timezone.now() - timedelta(days=365),
        reverse=True
    )
    
    context = {
        'accounts': accounts,
        'username_posts_list': username_posts_list,
    }
    return render(request, 'core/dashboard.html', context)


@login_required
def instagram_accounts_view(request):
    """View all Instagram accounts with inline analytics in card layout."""
    import json
    from collections import defaultdict
    
    accounts = InstagramAccount.objects.filter(user=request.user).annotate(
        posts_count=Count('posts', filter=Q(posts__is_reel=False))
    )
    
    # Prepare data for each account
    accounts_data = []
    for account in accounts:
        # Get posts only (not reels)
        posts = InstagramPost.objects.filter(account=account, is_reel=False)
        
        # Calculate basic metrics
        total_posts = posts.count()
        total_likes = posts.aggregate(Sum('like_count'))['like_count__sum'] or 0
        avg_likes = posts.aggregate(Avg('like_count'))['like_count__avg'] or 0 if total_posts > 0 else 0
        
        accounts_data.append({
            'account': account,
            'total_posts': total_posts,
            'total_likes': total_likes,
            'avg_likes': avg_likes,
        })
    
    return render(request, 'core/instagram_accounts.html', {'accounts_data': accounts_data})


@login_required
def account_analytics_view(request, account_id):
    """Analytics for a specific Instagram account (posts only, reels shown separately)."""
    import json
    from collections import defaultdict
    
    account = get_object_or_404(InstagramAccount, id=account_id, user=request.user)
    
    # Get ALL content (both posts and reels) for analytics
    # The "Posts Analytics" page should show analytics for all content
    all_content = InstagramPost.objects.filter(account=account).order_by('taken_at')
    
    # Also get separate counts for display
    posts_only = InstagramPost.objects.filter(account=account, is_reel=False)
    reels_only = InstagramPost.objects.filter(account=account, is_reel=True)
    
    # Debug logging to verify filtering
    logger.info(f"Analytics for account {account.username} (ID: {account_id}):")
    logger.info(f"  Posts only (is_reel=False): {posts_only.count()}")
    logger.info(f"  Reels only (is_reel=True): {reels_only.count()}")
    logger.info(f"  Total content: {all_content.count()}")
    
    # Calculate metrics for ALL content (posts + reels combined)
    total_posts = all_content.count()
    
    # Get aggregation results in one query for efficiency (for ALL content)
    post_aggregates = all_content.aggregate(
        total_likes_sum=Sum('like_count'),
        total_comments_sum=Sum('comment_count'),
        avg_likes_avg=Avg('like_count'),
        avg_comments_avg=Avg('comment_count')
    )
    
    total_likes = int(post_aggregates['total_likes_sum'] or 0)
    total_comments = int(post_aggregates['total_comments_sum'] or 0)
    
    # Calculate averages safely
    if total_posts > 0:
        avg_likes_result = post_aggregates['avg_likes_avg']
        avg_comments_result = post_aggregates['avg_comments_avg']
        avg_likes = float(avg_likes_result) if avg_likes_result is not None else 0.0
        avg_comments = float(avg_comments_result) if avg_comments_result is not None else 0.0
    else:
        avg_likes = 0.0
        avg_comments = 0.0
    
    # Also calculate separate metrics for display
    total_reels = reels_only.count()
    total_regular_posts = posts_only.count()
    
    reel_aggregates = reels_only.aggregate(
        total_likes_sum=Sum('like_count'),
        total_plays_sum=Sum('play_count')
    )
    total_reel_likes = int(reel_aggregates['total_likes_sum'] or 0)
    total_reel_plays = int(reel_aggregates['total_plays_sum'] or 0)
    
    # Calculate average likes/comments per hour
    avg_likes_per_hour = 0.0
    avg_comments_per_hour = 0.0
    
    if total_posts > 0:
        oldest_post = all_content.order_by('taken_at').first()
        if oldest_post:
            time_span = timezone.now() - oldest_post.taken_at
            total_hours = time_span.total_seconds() / 3600
            
            if total_hours > 0:
                avg_likes_per_hour = float(total_likes) / total_hours
                avg_comments_per_hour = float(total_comments) / total_hours
    
    # Prepare histogram data - group ALL content by hour of day (0-23)
    # This shows which hours of the day get the most engagement
    histogram_data_by_hour = defaultdict(lambda: {'likes': 0, 'comments': 0, 'count': 0})
    
    if total_posts > 0:
        # Use values() to get only needed fields for better performance (ALL content)
        posts_data = all_content.values('taken_at', 'like_count', 'comment_count')
        
        for post_data in posts_data:
            taken_at = post_data['taken_at']
            if taken_at:
                # Get hour of day (0-23)
                hour_of_day = taken_at.hour
                
                histogram_data_by_hour[hour_of_day]['likes'] += post_data['like_count'] or 0
                histogram_data_by_hour[hour_of_day]['comments'] += post_data['comment_count'] or 0
                histogram_data_by_hour[hour_of_day]['count'] += 1
        
        # Sort by hour (0-23) and calculate averages
        sorted_hours = sorted(histogram_data_by_hour.keys())
        chart_labels = []
        avg_likes_per_hour_data = []
        
        for hour in sorted_hours:
            data = histogram_data_by_hour[hour]
            # Format hour label (e.g., "0:00", "1:00", "14:00")
            hour_label = f"{hour}:00"
            chart_labels.append(hour_label)
            
            # Calculate average likes per post for this hour
            if data['count'] > 0:
                avg_likes_for_hour = data['likes'] / data['count']
            else:
                avg_likes_for_hour = 0
            
            avg_likes_per_hour_data.append(round(avg_likes_for_hour, 2))
        
        # Also prepare posts per weekday data (grouped by day of week) for the second chart
        # weekday() returns 0=Monday, 1=Tuesday, ..., 6=Sunday
        histogram_data_by_weekday = defaultdict(lambda: {'count': 0})
        weekday_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        
        for post_data in posts_data:
            taken_at = post_data['taken_at']
            if taken_at:
                weekday_index = taken_at.weekday()  # 0=Monday, 6=Sunday
                histogram_data_by_weekday[weekday_index]['count'] += 1
        
        # Sort by weekday index (0-6, Monday to Sunday)
        sorted_weekdays = sorted(histogram_data_by_weekday.keys())
        posts_per_day_labels = []
        posts_per_day_counts = []
        
        for weekday_index in sorted_weekdays:
            weekday_name = weekday_names[weekday_index]
            posts_per_day_labels.append(weekday_name)
            posts_per_day_counts.append(histogram_data_by_weekday[weekday_index]['count'])
    else:
        chart_labels = []
        avg_likes_per_hour_data = []
        posts_per_day_labels = []
        posts_per_day_counts = []
    
    # Top posts by likes - get top 5 content (posts + reels) ordered by likes
    top_posts_by_likes = all_content.order_by('-like_count', '-taken_at')[:5]
    
    # Top posts by comments - get top 5 content (posts + reels) ordered by comments
    top_posts_by_comments = all_content.order_by('-comment_count', '-taken_at')[:5]
    
    # Ensure all values are properly formatted
    context = {
        'account': account,
        'total_posts': int(total_posts),  # Ensure integer
        'total_likes': int(total_likes),  # Ensure integer
        'total_comments': int(total_comments),  # Ensure integer
        'avg_likes': round(float(avg_likes), 2),  # Round to 2 decimals
        'avg_comments': round(float(avg_comments), 2),  # Round to 2 decimals
        'avg_likes_per_hour': round(float(avg_likes_per_hour), 2),  # Round to 2 decimals
        'avg_comments_per_hour': round(float(avg_comments_per_hour), 2),  # Round to 2 decimals
        'top_posts_by_likes': list(top_posts_by_likes),  # Top 5 by likes
        'top_posts_by_comments': list(top_posts_by_comments),  # Top 5 by comments
        'is_reels': False,
        'chart_labels': json.dumps(chart_labels) if chart_labels else json.dumps([]),
        'avg_likes_per_hour_data': json.dumps(avg_likes_per_hour_data) if avg_likes_per_hour_data else json.dumps([]),
        'posts_per_day_labels': json.dumps(posts_per_day_labels) if posts_per_day_labels else json.dumps([]),
        'posts_per_day_data': json.dumps(posts_per_day_counts) if posts_per_day_counts else json.dumps([]),
        # Additional metrics for reference (separate counts)
        'total_reels': int(total_reels),  # Reels count
        'total_regular_posts': int(total_regular_posts),  # Regular posts count (non-reels)
        'total_reel_likes': int(total_reel_likes),  # Reels likes
        'total_reel_plays': int(total_reel_plays),  # Reels plays
    }
    
    # Log final context values for debugging
    logger.info(f"Analytics context for {account.username}: total_posts={context['total_posts']}, total_reels={context['total_reels']}, total_likes={context['total_likes']}, chart_data_points={len(chart_labels)}")
    
    return render(request, 'core/account_analytics.html', context)


@login_required
def add_instagram_account_view(request):
    """Add a new Instagram account to monitor."""
    if request.method == 'POST':
        form = InstagramAccountForm(request.POST)
        if form.is_valid():
            account = form.save(commit=False)
            account.user = request.user
            account.save()
            messages.success(request, f'Instagram account @{account.username} added successfully!')
            return redirect('instagram_accounts')
    else:
        form = InstagramAccountForm()
    
    return render(request, 'core/add_instagram.html', {'form': form})


@login_required
def delete_instagram_account_view(request, account_id):
    """Delete an Instagram account."""
    account = get_object_or_404(InstagramAccount, id=account_id, user=request.user)
    if request.method == 'POST':
        username = account.username
        account.delete()
        messages.success(request, f'Instagram account @{username} deleted successfully!')
    return redirect('instagram_accounts')


@login_required
def scrape_instagram_view(request):
    """Scrape Instagram posts for all user's accounts."""
    if request.method != 'POST':
        return redirect('dashboard')
    
    accounts = InstagramAccount.objects.filter(user=request.user)
    if not accounts.exists():
        messages.warning(request, 'Please add an Instagram account first.')
        return redirect('add_instagram')
    
    total_posts = 0
    total_errors = 0
    
    for account in accounts:
        try:
            # Clean username before fetching
            username = account.username.strip().lstrip('@').lower()
            if not username:
                messages.warning(request, f'Skipping account with empty username: {account.username}')
                continue
            
            # Check if account has existing posts to determine fetch mode
            has_posts = account.posts.exists()
            
            # Track saved posts and skipped reels
            saved_count = 0
            skipped_reels = 0
            
            # Define callback function to save posts immediately after each API call
            def save_posts_batch(posts_batch):
                """Save a batch of posts (including reels) to database immediately after API call."""
                nonlocal saved_count, skipped_reels
                
                for post_data in posts_batch:
                    # Ensure boolean fields are always True/False, not empty dicts or other values
                    def safe_bool(value, default=False):
                        """Safely convert value to boolean, handling None, empty dicts, etc."""
                        if value is None:
                            return default
                        if isinstance(value, bool):
                            return value
                        if isinstance(value, dict) and not value:  # Empty dict
                            return default
                        if isinstance(value, (list, dict, str)) and not value:  # Empty collections
                            return default
                        return bool(value)
                    
                    # Include both posts and reels - save reels with is_reel=True
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
                            'is_reel': is_reel,  # Save reels with is_reel=True
                            'is_carousel': safe_bool(post_data.get('is_carousel'), False),
                            'carousel_media_count': post_data.get('carousel_media_count', 0),
                            'like_count': post_data.get('like_count', 0),
                            'comment_count': post_data.get('comment_count', 0),
                            'play_count': post_data.get('play_count', 0),
                        }
                    )
                    
                    # Save carousel items if this is a carousel post
                    if post.is_carousel and 'carousel_items' in post_data:
                        # Delete existing carousel items
                        post.carousel_items.all().delete()
                        # Create new carousel items
                        for idx, item_data in enumerate(post_data.get('carousel_items', [])):
                            InstagramCarouselItem.objects.create(
                                post=post,
                                item_index=idx,
                                image_url=item_data.get('image_url', ''),
                                video_url=item_data.get('video_url', ''),
                                is_video=item_data.get('is_video', False),
                            )
                    
                    if created:
                        saved_count += 1
            
            # Fetch posts with callback to save incrementally
            if has_posts:
                # Fetch only posts from last 48 hours
                logger.info(f"Account {username} has existing posts, fetching last 48 hours only")
                posts_data = instagram_service.get_all_posts_for_username(
                    username, 
                    max_age_hours=48,
                    save_callback=save_posts_batch
                )
            else:
                # First time: fetch all posts
                logger.info(f"Account {username} has no posts, fetching all available posts")
                posts_data = instagram_service.get_all_posts_for_username(
                    username,
                    save_callback=save_posts_batch
                )
            
            account.last_scraped_at = timezone.now()
            account.save()
            
            total_posts += saved_count
            if skipped_reels > 0:
                messages.info(request, f'Fetched {saved_count} new posts for @{account.username} (skipped {skipped_reels} reels)')
            else:
                messages.success(request, f'Fetched {saved_count} new posts for @{account.username}')
            
        except Exception as e:
            total_errors += 1
            messages.error(request, f'Error fetching posts for @{account.username}: {str(e)}')
    
    if total_posts > 0:
        messages.success(request, f'Successfully fetched {total_posts} new posts total!')
    if total_errors > 0:
        messages.warning(request, f'Encountered {total_errors} errors during fetching.')
    
    return redirect('dashboard')


@login_required
def instagram_post_detail_view(request, post_id):
    """View details of a specific Instagram post."""
    post = get_object_or_404(InstagramPost, id=post_id, account__user=request.user)
    carousel_items = post.carousel_items.all() if post.is_carousel else []
    
    # Lazy-load video URL and caption for reels if not already in database
    # This reduces initial API calls and only fetches video URLs when user actually views the post
    if post.is_reel and post.post_code:
        needs_video_url = not post.video_url
        needs_caption = not post.caption
        
        if needs_video_url or needs_caption:
            logger.info(f"Lazy-loading video URL and caption for reel {post.id} (shortcode: {post.post_code})")
            try:
                data = instagram_service._fetch_video_url_by_shortcode(post.post_code)
                if data:
                    update_fields = []
                    
                    # Save video URL if fetched and not already in database
                    if needs_video_url and 'video_url' in data and data['video_url']:
                        post.video_url = data['video_url']
                        update_fields.append('video_url')
                        logger.info(f"Successfully fetched and saved video URL for reel {post.id}")
                    
                    # Save caption if fetched and not already in database
                    if needs_caption and 'caption' in data and data['caption']:
                        post.caption = data['caption']
                        update_fields.append('caption')
                        logger.info(f"Successfully fetched and saved caption for reel {post.id}")
                    
                    # Save only if we have fields to update
                    if update_fields:
                        post.save(update_fields=update_fields)
                else:
                    logger.warning(f"Could not fetch data for reel {post.id} with shortcode {post.post_code}")
            except Exception as e:
                logger.error(f"Error lazy-loading data for reel {post.id}: {e}", exc_info=True)
                # Continue without video URL/caption - template will show thumbnail/fallback
    
    context = {
        'post': post,
        'carousel_items': carousel_items,
    }
    return render(request, 'core/post_detail.html', context)


# Reddit views (simplified versions)
@login_required
def reddit_view(request):
    """Reddit monitoring page."""
    subreddits = Subreddit.objects.filter(user=request.user)
    # Prefetch keywords for each post to display them below posts
    posts = RedditPost.objects.filter(
        subreddit__user=request.user
    ).prefetch_related('keywords').order_by('-scraped_at')[:50]
    
    # Fetch extracted keywords - get top keywords by similarity (for separate section)
    keywords = RedditKeyword.objects.filter(
        post__subreddit__user=request.user
    ).select_related('post', 'post__subreddit').order_by('-similarity')[:50]
    
    # Group keywords by keyword text to show frequency and average similarity
    keyword_stats = defaultdict(lambda: {'count': 0, 'total_similarity': 0.0, 'posts': []})
    for kw in keywords:
        keyword_stats[kw.keyword]['count'] += 1
        keyword_stats[kw.keyword]['total_similarity'] += kw.similarity
        keyword_stats[kw.keyword]['posts'].append(kw.post)
    
    # Convert to list and calculate average similarity
    keyword_list = []
    for keyword, stats in keyword_stats.items():
        keyword_list.append({
            'keyword': keyword,
            'count': stats['count'],
            'avg_similarity': stats['total_similarity'] / stats['count'],
            'posts': stats['posts'][:3]  # Show up to 3 posts per keyword
        })
    
    # Sort by average similarity (descending)
    keyword_list.sort(key=lambda x: x['avg_similarity'], reverse=True)
    
    context = {
        'subreddits': subreddits,
        'posts': posts,
        'keywords': keyword_list[:30],  # Show top 30 keywords
        'total_keywords': RedditKeyword.objects.filter(post__subreddit__user=request.user).count(),
    }
    return render(request, 'core/reddit.html', context)


@login_required
def add_subreddit_view(request):
    """Add a new subreddit to monitor."""
    if request.method == 'POST':
        form = SubredditForm(request.POST)
        if form.is_valid():
            subreddit = form.save(commit=False)
            subreddit.user = request.user
            subreddit.save()
            messages.success(request, f'Subreddit r/{subreddit.name} added successfully!')
            return redirect('reddit')
    else:
        form = SubredditForm()
    
    subreddits = Subreddit.objects.filter(user=request.user)
    return render(request, 'core/add_subreddit.html', {'form': form, 'subreddits': subreddits})


@login_required
def delete_subreddit_view(request, subreddit_id):
    """Delete a subreddit."""
    subreddit = get_object_or_404(Subreddit, id=subreddit_id, user=request.user)
    if request.method == 'POST':
        name = subreddit.name
        subreddit.delete()
        messages.success(request, f'Subreddit r/{name} deleted successfully!')
    return redirect('reddit')


@login_required
def scrape_reddit_view(request):
    """Scrape Reddit posts for all user's subreddits."""
    if request.method != 'POST':
        return redirect('reddit')
    
    subreddits = Subreddit.objects.filter(user=request.user)
    if not subreddits.exists():
        messages.warning(request, 'Please add a subreddit first.')
        return redirect('add_subreddit')
    
    total_posts = 0
    total_errors = 0
    
    for subreddit in subreddits:
        try:
            posts_data = reddit_service.scrape_subreddit(subreddit.name)
            
            saved_count = 0
            for post_data in posts_data:
                post, created = RedditPost.objects.update_or_create(
                    subreddit=subreddit,
                    url=post_data['url'],
                    defaults={
                        'title': post_data['title'],
                        'score': post_data['score'],
                        'body': post_data['body'],
                        'flair': post_data.get('flair', ''),
                    }
                )
                if created:
                    saved_count += 1
            
            total_posts += saved_count
            messages.success(request, f'Fetched {saved_count} new posts from r/{subreddit.name}')
            
        except Exception as e:
            total_errors += 1
            messages.error(request, f'Error scraping r/{subreddit.name}: {str(e)}')
    
    if total_posts > 0:
        messages.success(request, f'Successfully fetched {total_posts} new posts total!')
    if total_errors > 0:
        messages.warning(request, f'Encountered {total_errors} errors during scraping.')
    
    return redirect('reddit')


@login_required
def extract_keywords_view(request):
    """Extract keywords from Reddit posts that don't have keywords yet."""
    if request.method != 'POST':
        return redirect('reddit')
    
    posts = RedditPost.objects.filter(
        subreddit__user=request.user,
        keywords_extracted=False
    )[:100]  # Process up to 100 posts at a time
    
    if not posts.exists():
        messages.info(request, 'No posts need keyword extraction.')
        return redirect('reddit')
    
    total_keywords = 0
    total_errors = 0
    
    for post in posts:
        try:
            # Combine title and body for keyword extraction
            combined_text = (post.title + "\n\n" + post.body).strip()
            
            keywords = keyword_service.extract_keywords(combined_text)
            
            # Delete old keywords for this post
            post.keywords.all().delete()
            
            # Save new keywords
            for kw_data in keywords:
                RedditKeyword.objects.create(
                    post=post,
                    keyword=kw_data['keyword'],
                    similarity=kw_data['similarity']
                )
                total_keywords += 1
            
            post.keywords_extracted = True
            post.save()
            
        except Exception as e:
            total_errors += 1
            logger.error(f"Error extracting keywords for post {post.id}: {e}", exc_info=True)
    
    if total_keywords > 0:
        messages.success(request, f'Extracted {total_keywords} keywords from {posts.count()} posts!')
    if total_errors > 0:
        messages.warning(request, f'Encountered {total_errors} errors during keyword extraction.')
    
    return redirect('reddit_keywords')


@login_required
def reddit_keywords_view(request):
    """View extracted keywords."""
    keywords = RedditKeyword.objects.filter(post__subreddit__user=request.user).order_by('-similarity')[:100]
    
    context = {
        'keywords': keywords,
    }
    return render(request, 'core/reddit_keywords.html', context)

