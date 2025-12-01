"""
Django views for Instagram and Reddit scraping application.
"""
import logging
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
    """Main dashboard showing recent posts and reels."""
    # Get user's Instagram accounts
    accounts = InstagramAccount.objects.filter(user=request.user)
    
    # Get recent regular posts (last 24 hours)
    recent_posts_time = timezone.now() - timedelta(hours=24)
    regular_posts = InstagramPost.objects.filter(
        account__user=request.user,
        is_reel=False,
        taken_at__gte=recent_posts_time
    ).select_related('account').order_by('-taken_at')[:50]
    
    # Get recent reels (last 48 hours)
    recent_reels_time = timezone.now() - timedelta(hours=48)
    recent_reels = InstagramPost.objects.filter(
        account__user=request.user,
        is_reel=True,
        taken_at__gte=recent_reels_time
    ).select_related('account').order_by('-taken_at')[:50]
    
    # Combine and sort
    posts = list(regular_posts) + list(recent_reels)
    posts.sort(key=lambda x: x.taken_at, reverse=True)
    
    context = {
        'accounts': accounts,
        'posts': posts[:50],  # Limit to 50 most recent
        'regular_posts_count': regular_posts.count(),
        'reels_count': recent_reels.count(),
    }
    return render(request, 'core/dashboard.html', context)


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
            return redirect('dashboard')
    else:
        form = InstagramAccountForm()
    
    accounts = InstagramAccount.objects.filter(user=request.user)
    return render(request, 'core/add_instagram.html', {'form': form, 'accounts': accounts})


@login_required
def delete_instagram_account_view(request, account_id):
    """Delete an Instagram account."""
    account = get_object_or_404(InstagramAccount, id=account_id, user=request.user)
    if request.method == 'POST':
        username = account.username
        account.delete()
        messages.success(request, f'Instagram account @{username} deleted successfully!')
    return redirect('add_instagram')


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
            
            if has_posts:
                # Fetch only posts from last 48 hours
                logger.info(f"Account {username} has existing posts, fetching last 48 hours only")
                posts_data = instagram_service.get_all_posts_for_username(username, max_age_hours=48)
            else:
                # First time: fetch all posts
                logger.info(f"Account {username} has no posts, fetching all available posts")
                posts_data = instagram_service.get_all_posts_for_username(username)
            
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
            
            total_posts += saved_count
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
def scrape_reels_view(request):
    """Scrape Instagram reels for all user's accounts concurrently."""
    if request.method != 'POST':
        return redirect('dashboard')
    
    accounts = InstagramAccount.objects.filter(user=request.user)
    if not accounts.exists():
        messages.warning(request, 'Please add an Instagram account first.')
        return redirect('add_instagram')
    
    try:
        # Fetch reels concurrently for all accounts
        results = instagram_service.fetch_reels_for_accounts(list(accounts))
        
        total_reels = 0
        total_errors = 0
        
        for account_id, result_data in results.items():
            account = result_data['account']
            reels = result_data['reels']
            error = result_data.get('error')
            
            if error:
                total_errors += 1
                messages.error(request, f'Error fetching reels for @{account.username}: {error}')
                continue
            
            saved_count = 0
            for reel_data in reels:
                # Use taken_at from API if available and valid, otherwise extract from post ID
                taken_at = reel_data.get('taken_at')
                if not taken_at:
                    # Fallback: extract from post ID
                    from .services.instagram_service import _extract_timestamp_from_post_id
                    taken_at = _extract_timestamp_from_post_id(reel_data.get('post_id'))
                    if not taken_at:
                        taken_at = timezone.now()
                
                post, created = InstagramPost.objects.update_or_create(
                    account=account,
                    post_id=reel_data['post_id'],
                    defaults={
                        'post_code': reel_data.get('post_code', ''),
                        'caption': reel_data.get('caption', ''),
                        'taken_at': taken_at,
                        'image_url': reel_data.get('image_url', ''),
                        'video_url': reel_data.get('video_url', ''),
                        'is_video': True,
                        'is_reel': True,
                        'is_carousel': reel_data.get('is_carousel', False),
                        'carousel_media_count': reel_data.get('carousel_media_count', 0),
                        'like_count': reel_data.get('like_count', 0),
                        'comment_count': reel_data.get('comment_count', 0),
                        'play_count': reel_data.get('play_count', 0),
                    }
                )
                if created:
                    saved_count += 1
            
            account.last_scraped_at = timezone.now()
            account.save()
            
            total_reels += saved_count
            if saved_count > 0:
                messages.success(request, f'Fetched {saved_count} new reels for @{account.username}')
        
        if total_reels > 0:
            messages.success(request, f'Successfully fetched {total_reels} new reels total!')
        elif total_errors == 0:
            messages.info(request, 'No new reels found.')
        if total_errors > 0:
            messages.warning(request, f'Encountered {total_errors} errors during fetching.')
            
    except Exception as e:
        messages.error(request, f'Error fetching reels: {str(e)}')
    
    return redirect('dashboard')


@login_required
def instagram_post_detail_view(request, post_id):
    """View details of a specific Instagram post."""
    post = get_object_or_404(InstagramPost, id=post_id, account__user=request.user)
    carousel_items = post.carousel_items.all() if post.is_carousel else []
    
    context = {
        'post': post,
        'carousel_items': carousel_items,
    }
    return render(request, 'core/post_detail.html', context)


@login_required
def analytics_view(request):
    """Analytics overview page."""
    accounts = InstagramAccount.objects.filter(user=request.user)
    
    context = {
        'accounts': accounts,
    }
    return render(request, 'core/analytics.html', context)


@login_required
def account_analytics_view(request, account_id):
    """Analytics for a specific Instagram account (regular posts only)."""
    account = get_object_or_404(InstagramAccount, id=account_id, user=request.user)
    
    # Filter for regular posts only (not reels)
    posts = InstagramPost.objects.filter(account=account, is_reel=False)
    
    # Calculate metrics
    total_posts = posts.count()
    total_likes = posts.aggregate(Sum('like_count'))['like_count__sum'] or 0
    total_comments = posts.aggregate(Sum('comment_count'))['comment_count__sum'] or 0
    avg_likes = posts.aggregate(Avg('like_count'))['like_count__avg'] or 0
    avg_comments = posts.aggregate(Avg('comment_count'))['comment_count__avg'] or 0
    
    # Top posts by likes
    top_posts = posts.order_by('-like_count')[:10]
    
    context = {
        'account': account,
        'total_posts': total_posts,
        'total_likes': total_likes,
        'total_comments': total_comments,
        'avg_likes': avg_likes,
        'avg_comments': avg_comments,
        'top_posts': top_posts,
        'is_reels': False,
    }
    return render(request, 'core/account_analytics.html', context)


@login_required
def account_reels_analytics_view(request, account_id):
    """Analytics for a specific Instagram account (reels only)."""
    account = get_object_or_404(InstagramAccount, id=account_id, user=request.user)
    
    # Filter for reels only
    reels = InstagramPost.objects.filter(account=account, is_reel=True)
    
    # Calculate metrics
    total_reels = reels.count()
    total_plays = reels.aggregate(Sum('play_count'))['play_count__sum'] or 0
    total_likes = reels.aggregate(Sum('like_count'))['like_count__sum'] or 0
    total_comments = reels.aggregate(Sum('comment_count'))['comment_count__sum'] or 0
    avg_plays = reels.aggregate(Avg('play_count'))['play_count__avg'] or 0
    avg_likes = reels.aggregate(Avg('like_count'))['like_count__avg'] or 0
    avg_comments = reels.aggregate(Avg('comment_count'))['comment_count__avg'] or 0
    
    # Top reels by plays
    top_reels = reels.order_by('-play_count')[:5]
    
    context = {
        'account': account,
        'total_posts': total_reels,
        'total_plays': total_plays,
        'total_likes': total_likes,
        'total_comments': total_comments,
        'avg_plays': avg_plays,
        'avg_likes': avg_likes,
        'avg_comments': avg_comments,
        'top_posts': top_reels,
        'is_reels': True,
    }
    return render(request, 'core/account_analytics.html', context)


# Reddit views (simplified versions)
@login_required
def reddit_view(request):
    """Reddit monitoring page."""
    subreddits = Subreddit.objects.filter(user=request.user)
    posts = RedditPost.objects.filter(subreddit__user=request.user).order_by('-scraped_at')[:50]
    
    context = {
        'subreddits': subreddits,
        'posts': posts,
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

