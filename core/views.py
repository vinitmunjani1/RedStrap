"""
Django views for Instagram and Reddit scraping application.
"""
import logging
import json
from collections import defaultdict, Counter
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.contrib import messages
from django.utils import timezone
from django.db.models import Q, Count, Avg, Sum
from django.db import transaction
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
import os
import uuid
import threading
import time
import requests

logger = logging.getLogger(__name__)
from .models import (
    InstagramAccount, InstagramPost, InstagramCarouselItem, InstagramKeyword,
    Subreddit, RedditPost, RedditKeyword,
    TwitterAccount, TwitterTweet, TwitterKeyword, SocialUsername,
    VideoIdeaExtraction, IdeaVideoPrompt
)
from django.contrib.contenttypes.models import ContentType
from .forms import InstagramAccountForm, SubredditForm, TwitterAccountForm, SocialAccountForm
from .services import instagram_service, reddit_service, twitter_service
from .services.together_ai_service import extract_keywords_with_together_ai, generate_video_prompt_from_idea
from .services.ranking_service import RankingService
from .services.gemini_service import convert_video_url_to_mp4_bytes, extract_video_ideas


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

    # Recent Instagram posts (latest 6 overall)
    recent_instagram_posts = InstagramPost.objects.filter(
        account__user=request.user
    ).select_related('account').prefetch_related('keywords').order_by('-taken_at')[:6]

    # Recent tweets (latest 6 overall) with keywords
    recent_tweets = TwitterTweet.objects.filter(
        account__user=request.user
    ).select_related('account').prefetch_related('keywords').order_by('-created_at')[:6]
    
    # Compute top engagement tweets (faves + retweets + replies) from recent tweets
    RECENT_ENGAGEMENT_SAMPLE = 200
    TOP_ENGAGEMENT_LIMIT = 5
    recent_tweets_for_engagement = list(
        TwitterTweet.objects.filter(account__user=request.user)
        .select_related('account')
        .prefetch_related('keywords')
        .order_by('-created_at')[:RECENT_ENGAGEMENT_SAMPLE]
    )
    top_tw_engagement_posts = sorted(
        recent_tweets_for_engagement,
        key=lambda t: (t.favorite_count or 0) + (t.retweet_count or 0) + (t.reply_count or 0),
        reverse=True
    )[:TOP_ENGAGEMENT_LIMIT]
    top_tw_engagement = [
        {
            'tweet': tweet,
            'engagement_total': (tweet.favorite_count or 0) + (tweet.retweet_count or 0) + (tweet.reply_count or 0),
        }
        for tweet in top_tw_engagement_posts
    ]
    
    # Compute top engagement Instagram posts (likes + comments) from recent posts
    recent_posts_for_engagement = list(
        InstagramPost.objects.filter(account__user=request.user)
        .select_related('account')
        .prefetch_related('keywords')
        .order_by('-taken_at')[:RECENT_ENGAGEMENT_SAMPLE]
    )
    top_ig_engagement_posts = sorted(
        recent_posts_for_engagement,
        key=lambda p: (p.like_count or 0) + (p.comment_count or 0),
        reverse=True
    )[:TOP_ENGAGEMENT_LIMIT]
    top_ig_engagement = [
        {
            'post': post,
            'engagement_total': (post.like_count or 0) + (post.comment_count or 0),
        }
        for post in top_ig_engagement_posts
    ]
    
    # Get recent posts/tweets per username (last 48 hours)
    recent_window = timezone.now() - timedelta(hours=48)
    all_posts = list(
        InstagramPost.objects.filter(
            account__user=request.user,
            taken_at__gte=recent_window
        ).select_related('account').prefetch_related('keywords').order_by('-taken_at')
    )
    all_tweets = list(
        TwitterTweet.objects.filter(
            account__user=request.user,
            created_at__gte=recent_window
        ).select_related('account').prefetch_related('keywords').order_by('-created_at')
    )

    # Group by username combining IG and Twitter
    by_username = defaultdict(lambda: {
        'ig_posts': [],
        'tw_tweets': [],
        'ig_account_id': None,
        'tw_account_id': None,
    })

    for post in all_posts:
        entry = by_username[post.account.username]
        entry['ig_posts'].append(post)
        entry['ig_account_id'] = entry['ig_account_id'] or post.account.id

    for tweet in all_tweets:
        entry = by_username[tweet.account.username]
        entry['tw_tweets'].append(tweet)
        entry['tw_account_id'] = entry['tw_account_id'] or tweet.account.id

    username_cards = []
    for username, data in by_username.items():
        ig_sorted = sorted(data['ig_posts'], key=lambda p: p.taken_at, reverse=True)
        tw_sorted = sorted(data['tw_tweets'], key=lambda t: t.created_at, reverse=True)
        latest_dt = None
        if ig_sorted:
            latest_dt = ig_sorted[0].taken_at
        if tw_sorted:
            latest_dt = max(latest_dt, tw_sorted[0].created_at) if latest_dt else tw_sorted[0].created_at
        username_cards.append({
            'username': username,
            'ig_account_id': data['ig_account_id'],
            'tw_account_id': data['tw_account_id'],
            'ig_posts': ig_sorted,
            'tw_tweets': tw_sorted,
            'ig_count': len(ig_sorted),
            'tw_count': len(tw_sorted),
            'latest_dt': latest_dt or timezone.now() - timedelta(days=365),
        })

    username_cards.sort(key=lambda x: x['latest_dt'], reverse=True)
    
    context = {
        'accounts': accounts,
        'username_cards': username_cards,
        'recent_instagram_posts': recent_instagram_posts,
        'recent_tweets': recent_tweets,
        'top_ig_engagement': top_ig_engagement,
        'top_tw_engagement': top_tw_engagement,
    }
    return render(request, 'core/dashboard.html', context)


@login_required
def posts_view(request):
    """
    View all posts grouped by username with sorting and search functionality.
    Supports sorting by likes count, comments count, and time (taken_at).
    Supports searching by username.
    """
    from collections import defaultdict
    RECENT_ENGAGEMENT_SAMPLE = 200
    TOP_ENGAGEMENT_LIMIT = 5
    
    # Get all posts/tweets for the user (kept as queryset until after search to avoid unnecessary work)
    all_posts_qs = InstagramPost.objects.filter(
        account__user=request.user
    ).select_related('account').prefetch_related('keywords')
    all_tweets_qs = TwitterTweet.objects.filter(
        account__user=request.user
    ).select_related('account')

    # Compute top engagement Instagram posts (likes + comments) from recent posts
    recent_posts_for_engagement = list(
        InstagramPost.objects.filter(account__user=request.user)
        .select_related('account')
        .order_by('-taken_at')[:RECENT_ENGAGEMENT_SAMPLE]
    )
    top_ig_engagement_posts = sorted(
        recent_posts_for_engagement,
        key=lambda p: (p.like_count or 0) + (p.comment_count or 0),
        reverse=True
    )[:TOP_ENGAGEMENT_LIMIT]
    
    # Check for existing extractions for top engagement posts
    top_post_ids = [post.id for post in top_ig_engagement_posts]
    top_extractions = VideoIdeaExtraction.objects.filter(
        source_type='instagram',
        source_id__in=top_post_ids
    ).values_list('source_id', flat=True)
    top_extraction_set = set(top_extractions)
    
    top_ig_engagement = [
        {
            'post': post,
            'engagement_total': (post.like_count or 0) + (post.comment_count or 0),
            'has_extraction': post.id in top_extraction_set,
        }
        for post in top_ig_engagement_posts
    ]

    # Compute top engagement tweets (faves + retweets + replies) from recent tweets
    recent_tweets_for_engagement = list(
        TwitterTweet.objects.filter(account__user=request.user)
        .select_related('account')
        .order_by('-created_at')[:RECENT_ENGAGEMENT_SAMPLE]
    )
    top_tw_engagement_posts = sorted(
        recent_tweets_for_engagement,
        key=lambda t: (t.favorite_count or 0) + (t.retweet_count or 0) + (t.reply_count or 0),
        reverse=True
    )[:TOP_ENGAGEMENT_LIMIT]
    
    # Check for existing extractions for top engagement tweets
    top_tweet_ids = [tweet.id for tweet in top_tw_engagement_posts]
    top_tweet_extractions = VideoIdeaExtraction.objects.filter(
        source_type='twitter',
        source_id__in=top_tweet_ids
    ).values_list('source_id', flat=True)
    top_tweet_extraction_set = set(top_tweet_extractions)
    
    top_tw_engagement = [
        {
            'tweet': tweet,
            'engagement_total': (tweet.favorite_count or 0) + (tweet.retweet_count or 0) + (tweet.reply_count or 0),
            'has_extraction': tweet.id in top_tweet_extraction_set,
        }
        for tweet in top_tw_engagement_posts
    ]
    
    # Get search query from request
    search_query = request.GET.get('search', '').strip().lower()
    
    # Filter by username if search query provided
    if search_query:
        all_posts_qs = all_posts_qs.filter(account__username__icontains=search_query)
        all_tweets_qs = all_tweets_qs.filter(account__username__icontains=search_query)
    
    # Materialize lists only after search filtering
    all_posts = list(all_posts_qs)
    all_tweets = list(all_tweets_qs)
    
    # Get sort parameter from request (default: time, descending)
    sort_by = request.GET.get('sort', 'time_desc')
    tw_sort_by = request.GET.get('tw_sort', 'time_desc')
    
    # Group posts by username
    posts_by_username = defaultdict(list)
    account_id_map = {}
    
    for post in all_posts:
        posts_by_username[post.account.username].append(post)
        if post.account.username not in account_id_map:
            account_id_map[post.account.username] = post.account.id
    
    # Sort posts within each username group based on sort parameter
    username_posts_list = []
    for username, posts in posts_by_username.items():
        if sort_by == 'likes_desc':
            posts.sort(key=lambda x: x.like_count or 0, reverse=True)
        elif sort_by == 'likes_asc':
            posts.sort(key=lambda x: x.like_count or 0, reverse=False)
        elif sort_by == 'comments_desc':
            posts.sort(key=lambda x: x.comment_count or 0, reverse=True)
        elif sort_by == 'comments_asc':
            posts.sort(key=lambda x: x.comment_count or 0, reverse=False)
        elif sort_by == 'time_desc':
            posts.sort(key=lambda x: x.taken_at, reverse=True)
        elif sort_by == 'time_asc':
            posts.sort(key=lambda x: x.taken_at, reverse=False)
        else:
            # Default: sort by time descending
            posts.sort(key=lambda x: x.taken_at, reverse=True)
        
        account_id = account_id_map.get(username)
        if account_id:
            # Initially show only first 6 posts for faster page load
            # Remaining posts will be loaded via AJAX on scroll
            initial_posts = posts[:6]
            username_posts_list.append({
                'username': username,
                'account_id': account_id,
                'posts': initial_posts,  # Only first 6 posts initially
                'all_posts': posts,  # Keep all posts for reference
                'count': len(posts),  # Total count
                'loaded_count': len(initial_posts),  # Currently loaded count
                'has_more': len(posts) > 6  # Whether there are more posts to load
            })
    
    # Check for existing extractions for posts
    post_ids = [post.id for posts in posts_by_username.values() for post in posts]
    existing_extractions = VideoIdeaExtraction.objects.filter(
        source_type='instagram',
        source_id__in=post_ids
    ).values_list('source_id', flat=True)
    extraction_set = set(existing_extractions)
    
    # Add has_extraction flag to each post
    for username_data in username_posts_list:
        for post in username_data['posts']:
            post.has_extraction = post.id in extraction_set
        for post in username_data.get('all_posts', []):
            post.has_extraction = post.id in extraction_set
    
    # Sort username groups by most recent post (or by username if search is active)
    if search_query:
        # When searching, sort by username alphabetically
        username_posts_list.sort(key=lambda x: x['username'].lower())
    else:
        # Otherwise, sort by most recent post across all usernames
        username_posts_list.sort(
            key=lambda x: max(p.taken_at for p in x['posts']) if x['posts'] else timezone.now() - timedelta(days=365),
            reverse=True
        )
    
    # Group tweets by username
    tweets_by_username = defaultdict(list)
    tw_account_id_map = {}
    for tweet in all_tweets:
        tweets_by_username[tweet.account.username].append(tweet)
        if tweet.account.username not in tw_account_id_map:
            tw_account_id_map[tweet.account.username] = tweet.account.id

    # Check for existing extractions for tweets
    tweet_ids = [tweet.id for tweets in tweets_by_username.values() for tweet in tweets]
    existing_tweet_extractions = VideoIdeaExtraction.objects.filter(
        source_type='twitter',
        source_id__in=tweet_ids
    ).values_list('source_id', flat=True)
    tweet_extraction_set = set(existing_tweet_extractions)
    
    tweets_username_list = []
    for username, tweets in tweets_by_username.items():
        if tw_sort_by == 'likes_desc':
            tweets.sort(key=lambda t: t.favorite_count or 0, reverse=True)
        elif tw_sort_by == 'likes_asc':
            tweets.sort(key=lambda t: t.favorite_count or 0, reverse=False)
        elif tw_sort_by == 'retweets_desc':
            tweets.sort(key=lambda t: t.retweet_count or 0, reverse=True)
        elif tw_sort_by == 'retweets_asc':
            tweets.sort(key=lambda t: t.retweet_count or 0, reverse=False)
        elif tw_sort_by == 'replies_desc':
            tweets.sort(key=lambda t: t.reply_count or 0, reverse=True)
        elif tw_sort_by == 'replies_asc':
            tweets.sort(key=lambda t: t.reply_count or 0, reverse=False)
        elif tw_sort_by == 'engagement_desc':
            tweets.sort(key=lambda t: (t.favorite_count or 0) + (t.retweet_count or 0) + (t.reply_count or 0), reverse=True)
        elif tw_sort_by == 'engagement_asc':
            tweets.sort(key=lambda t: (t.favorite_count or 0) + (t.retweet_count or 0) + (t.reply_count or 0), reverse=False)
        elif tw_sort_by == 'time_asc':
            tweets.sort(key=lambda t: t.created_at, reverse=False)
        else:
            tweets.sort(key=lambda t: t.created_at, reverse=True)

        account_id = tw_account_id_map.get(username)
        if account_id:
            initial_tweets = tweets[:6]
            tweets_username_list.append({
                'username': username,
                'account_id': account_id,
                'tweets': initial_tweets,
                'count': len(tweets),
                'loaded_count': len(initial_tweets),
                'has_more': len(tweets) > 6,
            })

    # Sort tweet groups by most recent tweet (or username if searching)
    if search_query:
        tweets_username_list.sort(key=lambda x: x['username'].lower())
    else:
        tweets_username_list.sort(
            key=lambda x: max(t.created_at for t in x['tweets']) if x['tweets'] else timezone.now() - timedelta(days=365),
            reverse=True
        )

    context = {
        'username_posts_list': username_posts_list,
        'search_query': search_query,
        'sort_by': sort_by,
        'total_posts': len(all_posts),
        'top_ig_engagement': top_ig_engagement,
        'tweets_username_list': tweets_username_list,
        'tw_sort_by': tw_sort_by,
        'total_tweets': len(all_tweets),
        'top_tw_engagement': top_tw_engagement,
    }
    return render(request, 'core/posts.html', context)


@login_required
@require_http_methods(["GET"])
def load_more_posts_view(request):
    """
    AJAX endpoint to load more posts for infinite scroll.
    Returns next 6 posts for a specific username.
    """
    username = request.GET.get('username', '').strip()
    offset = int(request.GET.get('offset', 0))
    sort_by = request.GET.get('sort', 'time_desc')
    search_query = request.GET.get('search', '').strip().lower()
    limit = 6  # Load 6 posts at a time
    
    if not username:
        return JsonResponse({'error': 'Username required'}, status=400)
    
    # Get posts for this username
    posts_query = InstagramPost.objects.filter(
        account__user=request.user,
        account__username=username
    ).select_related('account').prefetch_related('keywords')
    
    # Apply search filter if provided
    if search_query:
        posts_query = posts_query.filter(account__username__icontains=search_query)
    
    # Get all posts and sort them
    all_posts = list(posts_query)
    
    # Sort posts based on sort parameter
    if sort_by == 'likes_desc':
        all_posts.sort(key=lambda x: x.like_count or 0, reverse=True)
    elif sort_by == 'likes_asc':
        all_posts.sort(key=lambda x: x.like_count or 0, reverse=False)
    elif sort_by == 'comments_desc':
        all_posts.sort(key=lambda x: x.comment_count or 0, reverse=True)
    elif sort_by == 'comments_asc':
        all_posts.sort(key=lambda x: x.comment_count or 0, reverse=False)
    elif sort_by == 'time_desc':
        all_posts.sort(key=lambda x: x.taken_at, reverse=True)
    elif sort_by == 'time_asc':
        all_posts.sort(key=lambda x: x.taken_at, reverse=False)
    else:
        all_posts.sort(key=lambda x: x.taken_at, reverse=True)
    
    # Get next batch of posts
    next_posts = all_posts[offset:offset + limit]
    has_more = (offset + limit) < len(all_posts)
    
    # Serialize posts to JSON
    posts_data = []
    for post in next_posts:
        posts_data.append({
            'id': post.id,
            'post_id': post.post_id,
            'post_code': post.post_code,
            'caption': post.caption or '',
            'taken_at': post.taken_at.isoformat() if post.taken_at else '',
            'image_url': post.image_url or '',
            'video_url': post.video_url or '',
            'is_video': post.is_video,
            'is_reel': post.is_reel,
            'is_carousel': post.is_carousel,
            'carousel_media_count': post.carousel_media_count,
            'like_count': post.like_count or 0,
            'comment_count': post.comment_count or 0,
            'play_count': post.play_count or 0,
            'keywords': [
                {
                    'keyword': kw.keyword,
                    'similarity': float(kw.similarity)
                }
                for kw in post.keywords.all()
            ]
        })
    
    return JsonResponse({
        'posts': posts_data,
        'has_more': has_more,
        'next_offset': offset + len(next_posts),
        'total_count': len(all_posts)
    })


@login_required
@require_http_methods(["GET"])
def load_more_tweets_view(request):
    """
    AJAX endpoint to load more tweets for infinite scroll/pagination on the posts page.
    Returns next 6 tweets for a specific username.
    """
    username = request.GET.get('username', '').strip()
    offset = int(request.GET.get('offset', 0))
    sort_by = request.GET.get('tw_sort', 'time_desc')
    search_query = request.GET.get('search', '').strip().lower()
    limit = 6
    
    if not username:
        return JsonResponse({'error': 'Username required'}, status=400)
    
    tweets_qs = TwitterTweet.objects.filter(
        account__user=request.user,
        account__username=username
    ).select_related('account')
    
    if search_query:
        tweets_qs = tweets_qs.filter(account__username__icontains=search_query)
    
    tweets = list(tweets_qs)
    
    if sort_by == 'likes_desc':
        tweets.sort(key=lambda t: t.favorite_count or 0, reverse=True)
    elif sort_by == 'likes_asc':
        tweets.sort(key=lambda t: t.favorite_count or 0, reverse=False)
    elif sort_by == 'retweets_desc':
        tweets.sort(key=lambda t: t.retweet_count or 0, reverse=True)
    elif sort_by == 'retweets_asc':
        tweets.sort(key=lambda t: t.retweet_count or 0, reverse=False)
    elif sort_by == 'replies_desc':
        tweets.sort(key=lambda t: t.reply_count or 0, reverse=True)
    elif sort_by == 'replies_asc':
        tweets.sort(key=lambda t: t.reply_count or 0, reverse=False)
    elif sort_by == 'engagement_desc':
        tweets.sort(key=lambda t: (t.favorite_count or 0) + (t.retweet_count or 0) + (t.reply_count or 0), reverse=True)
    elif sort_by == 'engagement_asc':
        tweets.sort(key=lambda t: (t.favorite_count or 0) + (t.retweet_count or 0) + (t.reply_count or 0), reverse=False)
    elif sort_by == 'time_asc':
        tweets.sort(key=lambda t: t.created_at, reverse=False)
    else:
        tweets.sort(key=lambda t: t.created_at, reverse=True)
    
    next_tweets = tweets[offset:offset + limit]
    has_more = (offset + limit) < len(tweets)
    
    tweets_data = []
    for tweet in next_tweets:
        # Prepare one representative media item (first)
        media_info = {}
        if tweet.media:
            media_item = tweet.media[0]
            media_info = {
                'type': media_item.type,
                'url': media_item.url,
                'video_url': media_item.video_url,
            }
        tweets_data.append({
            'id': tweet.id,
            'text': tweet.text or '',
            'created_at': tweet.created_at.isoformat() if tweet.created_at else '',
            'favorite_count': tweet.favorite_count or 0,
            'retweet_count': tweet.retweet_count or 0,
            'reply_count': tweet.reply_count or 0,
            'twitter_url': tweet.twitter_url,
            'media': media_info,
            'username': tweet.account.username,
        })
    
    return JsonResponse({
        'tweets': tweets_data,
        'has_more': has_more,
        'next_offset': offset + len(next_tweets),
        'total_count': len(tweets),
    })


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
        # Pass as Python lists - Django's json_script filter will handle JSON encoding safely
        'chart_labels': chart_labels if chart_labels else [],
        'avg_likes_per_hour_data': avg_likes_per_hour_data if avg_likes_per_hour_data else [],
        'posts_per_day_labels': posts_per_day_labels if posts_per_day_labels else [],
        'posts_per_day_data': posts_per_day_counts if posts_per_day_counts else [],
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
def add_instagram_account_view(request, username=None):
    """
    Add a new Instagram account to monitor.
    If username parameter is provided, link the account to that SocialUsername.
    """
    # Get username from URL parameter or query string
    parent_username = username or request.GET.get('username')
    social_username = None
    redirect_to = 'instagram_accounts'
    
    # If parent username is provided, get or create the SocialUsername
    if parent_username:
        parent_username_clean = str(parent_username).strip().lstrip('@').lower()
        try:
            social_username = SocialUsername.objects.get(user=request.user, username=parent_username_clean)
            redirect_to = 'social_user_analytics'
        except SocialUsername.DoesNotExist:
            # If parent username doesn't exist, create it
            social_username = SocialUsername.objects.create(
                user=request.user,
                username=parent_username_clean
            )
            redirect_to = 'social_user_analytics'
    
    if request.method == 'POST':
        form = InstagramAccountForm(request.POST)
        if form.is_valid():
            username_input = form.cleaned_data['username'].strip().lstrip('@').lower()
            
            # Check if account already exists
            existing_account = InstagramAccount.objects.filter(
                user=request.user,
                username=username_input
            ).first()
            
            if existing_account:
                # If account exists, just link it to the social_username if provided
                if social_username and existing_account.social_username != social_username:
                    existing_account.social_username = social_username
                    existing_account.save()
                    messages.info(request, f'Instagram account @{username_input} already exists. Linked to @{parent_username_clean}.')
                else:
                    messages.info(request, f'Instagram account @{username_input} is already being monitored.')
                
                if redirect_to == 'social_user_analytics':
                    return redirect('social_user_analytics', username=parent_username_clean)
                return redirect(redirect_to)
            
            # Create new account
            account = form.save(commit=False)
            account.user = request.user
            account.username = username_input
            if social_username:
                account.social_username = social_username
            account.save()
            
            messages.success(request, f'Instagram account @{account.username} added successfully!')
            if redirect_to == 'social_user_analytics':
                return redirect('social_user_analytics', username=parent_username_clean)
            return redirect(redirect_to)
    else:
        form = InstagramAccountForm()
    
    context = {'form': form}
    if parent_username:
        context['parent_username'] = parent_username_clean
        context['redirect_url'] = f"/social/analytics/{parent_username_clean}/"
    
    return render(request, 'core/add_instagram.html', context)


@login_required
def delete_instagram_account_view(request, account_id):
    """Delete an Instagram account."""
    account = get_object_or_404(InstagramAccount, id=account_id, user=request.user)
    if request.method == 'POST':
        username = account.username
        account.delete()
        messages.success(request, f'Instagram account @{username} deleted successfully!')
    return redirect('social_dashboard')


def _update_progress(task_id, **kwargs):
    """Update progress in cache for a given task."""
    cache_key = f'fetch_progress_{task_id}'
    progress = cache.get(cache_key, {})
    progress.update(kwargs)
    # Cache for 30 minutes
    cache.set(cache_key, progress, 1800)
    return progress


def filter_recent_posts(posts, hours=24):
    """
    Filter posts to only include those from the last N hours based on taken_at timestamp.
    
    Args:
        posts: List of InstagramPost model instances
        hours: Number of hours to look back (default: 24)
    
    Returns:
        List of posts from the last N hours
    """
    if not posts:
        return []
    
    cutoff_time = timezone.now() - timedelta(hours=hours)
    return [post for post in posts if post.taken_at and post.taken_at >= cutoff_time]


def _fetch_posts_with_progress(user, task_id):
    """
    Background function to fetch posts and extract keywords with progress tracking.
    This runs in a separate thread to allow immediate response to AJAX requests.
    """
    try:
        accounts = list(InstagramAccount.objects.filter(user=user))
        accounts_total = len(accounts)
        
        if accounts_total == 0:
            _update_progress(task_id, phase='error', message='No Instagram accounts found', error='No accounts')
            return
        
        # Initialize progress
        _update_progress(
            task_id,
            phase='fetching_posts',
            current_account='',
            accounts_total=accounts_total,
            accounts_processed=0,
            posts_fetched=0,
            posts_total=0,  # Will be estimated
            keywords_extracted=0,
            keywords_total=0,
            start_time=time.time(),
            message='Starting to fetch posts...'
        )
        
        total_posts = 0
        total_errors = 0
        new_posts_for_keywords = []
        accounts_processed_lock = threading.Lock()  # Thread-safe counter for accounts processed
        
        def fetch_account_posts(account):
            """Fetch posts for a single account - designed for concurrent execution."""
            nonlocal total_posts, total_errors, new_posts_for_keywords
            account_new_posts = []
            account_saved_count = 0
            account_has_posts = False  # Track if account had existing posts
            
            try:
                username = account.username.strip().lstrip('@').lower()
                if not username:
                    return account_saved_count, account_new_posts, False, None
                
                # Update current account being processed
                _update_progress(
                    task_id,
                    current_account=username,
                    message=f'Fetching posts for @{username}...'
                )
                
                has_posts = account.posts.exists()
                account_has_posts = has_posts  # Store for later use
                
                def save_posts_batch(posts_batch):
                    """Save a batch of posts and update progress - thread-safe."""
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
                        else:
                            # Only queue for keywords if not already extracted
                            if (not post.keywords_extracted) and post.caption and post.caption.strip():
                                account_new_posts.append(post)
                    
                    # Thread-safe progress update after processing batch
                    if batch_new_posts > 0:
                        with accounts_processed_lock:
                            current_progress = cache.get(f'fetch_progress_{task_id}', {})
                            current_fetched = current_progress.get('posts_fetched', 0)
                            new_fetched = current_fetched + batch_new_posts
                            current_total = current_progress.get('posts_total', 0)
                            new_total = max(new_fetched, current_total)
                            _update_progress(
                                task_id, 
                                posts_fetched=new_fetched,
                                posts_total=new_total
                            )
                
                # Fetch posts (concurrently with other accounts)
                if has_posts:
                    # Fetch until we reach the most recent post already in DB (stop_post_id boundary)
                    last_post = account.posts.order_by('-taken_at', '-created_at').first()
                    stop_post_id = last_post.post_id if last_post else None
                    logger.info(f"Account {username} has existing posts, fetching until stop_post_id={stop_post_id}")
                    posts_data = instagram_service.get_all_posts_for_username(
                        username,
                        save_callback=save_posts_batch,
                        stop_post_id=stop_post_id,
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
                        from django.conf import settings
                        from core.services.discord_service import send_discord_webhook
                        
                        webhook_url = getattr(settings, 'DISCORD_WEBHOOK_URL', '')
                        if webhook_url:
                            try:
                                send_discord_webhook(webhook_url, username, recent_posts)
                            except Exception as e:
                                logger.error(f"Error sending Discord notification for @{username}: {e}", exc_info=True)
                                # Don't fail the entire fetch if Discord fails
                
                return account_saved_count, account_new_posts, account_has_posts, None
                
            except Exception as e:
                logger.error(f"Error fetching posts for @{account.username}: {e}", exc_info=True)
                return 0, [], False, str(e)
        
        # Process accounts concurrently using ThreadPoolExecutor
        # Use up to 13 workers (one per API key) to maximize throughput
        # Get number of API keys from settings
        from django.conf import settings
        api_keys = getattr(settings, 'RAPIDAPI_KEYS', [])
        num_api_keys = len(api_keys) if api_keys else 13  # Default to 13 if not found
        max_workers = min(len(accounts), num_api_keys)  # Match number of API keys for optimal distribution
        
        logger.info(f"Processing {accounts_total} accounts concurrently with {max_workers} workers")
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all account fetching tasks
            future_to_account = {
                executor.submit(fetch_account_posts, account): account
                for account in accounts
            }
            
            # Process completed tasks as they finish
            completed_accounts = 0
            for future in as_completed(future_to_account):
                account = future_to_account[future]
                completed_accounts += 1
                
                try:
                    saved_count, account_new_posts, account_has_posts, error = future.result()
                    
                    # Thread-safe updates
                    with accounts_processed_lock:
                        total_posts += saved_count
                        # Only add posts for keyword extraction if account had existing posts (recent fetch, not initial bulk)
                        if account_has_posts:
                            new_posts_for_keywords.extend(account_new_posts)
                        if error:
                            total_errors += 1
                        
                        # Update progress
                        current_progress = cache.get(f'fetch_progress_{task_id}', {})
                        current_total = current_progress.get('posts_total', 0)
                        new_total = max(total_posts, current_total)
                        _update_progress(
                            task_id,
                            accounts_processed=completed_accounts,
                            posts_total=new_total,
                            message=f'Completed @{account.username}: {saved_count} posts ({completed_accounts}/{accounts_total} accounts)...'
                        )
                        
                except Exception as e:
                    logger.error(f"Exception processing account {account.username}: {e}", exc_info=True)
                    with accounts_processed_lock:
                        total_errors += 1
                        completed_accounts += 1
                        _update_progress(
                            task_id,
                            accounts_processed=completed_accounts,
                            message=f'Error processing @{account.username}: {str(e)}'
                        )
        
        # Update progress after fetching all accounts
        _update_progress(
            task_id,
            accounts_processed=accounts_total,
            posts_total=total_posts,
            posts_fetched=total_posts,
            message=f'Fetched {total_posts} posts from {accounts_total} account(s). Extracting keywords...'
        )
        
        # Extract keywords
        total_keywords_extracted = 0
        if new_posts_for_keywords:
            # Estimate total keywords: each post can have 3-5 keywords, use average of 4
            estimated_keywords_total = len(new_posts_for_keywords) * 4
            _update_progress(
                task_id,
                phase='extracting_keywords',
                keyword_start_time=time.time(),  # Track when keyword extraction started
                keywords_total=estimated_keywords_total,  # Estimated total (will be updated as we discover more)
                keywords_extracted=0,
                message=f'Extracting keywords from {len(new_posts_for_keywords)} posts...'
            )
            
            max_workers = min(len(new_posts_for_keywords), (os.cpu_count() or 4) * 2, 20)
            results = []
            keyword_errors = 0
            keywords_extracted_count = 0
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_post = {
                    executor.submit(_extract_keywords_for_post, post): post
                    for post in new_posts_for_keywords
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
                            keywords_extracted_count += len(keywords)
                            total_keywords_extracted += len(keywords)
                            # Update total if we've extracted more than estimated
                            current_progress = cache.get(f'fetch_progress_{task_id}', {})
                            current_total = current_progress.get('keywords_total', 0)
                            new_total = max(keywords_extracted_count, current_total)  # Update total as we discover more
                            _update_progress(
                                task_id, 
                                keywords_extracted=keywords_extracted_count,
                                keywords_total=new_total
                            )
                    except Exception as e:
                        logger.error(f"Exception extracting keywords for post {post.id}: {e}", exc_info=True)
                        keyword_errors += 1
                        results.append({
                            'post_id': post.id,
                            'keywords': [],
                            'error': str(e)
                        })
            
            post_map = {post.id: post for post in new_posts_for_keywords}
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
                                similarity=kw_data.get('similarity')  # Together AI keywords have no similarity
                            )
                        )
                    
                    post.keywords_extracted = True
                    posts_to_update.append(post)
                
                if post_ids_to_delete_keywords:
                    InstagramKeyword.objects.filter(post_id__in=post_ids_to_delete_keywords).delete()
                
                if keywords_to_create:
                    InstagramKeyword.objects.bulk_create(keywords_to_create, batch_size=100)
                
                if posts_to_update:
                    InstagramPost.objects.bulk_update(posts_to_update, ['keywords_extracted'], batch_size=100)
        
        # Mark as completed
        _update_progress(
            task_id,
            phase='completed',
            message=f'Successfully fetched {total_posts} posts and extracted {total_keywords_extracted} keywords!',
            posts_fetched=total_posts,
            posts_total=total_posts,
            keywords_extracted=total_keywords_extracted,
            keywords_total=total_keywords_extracted  # Set final total to actual extracted count
        )
        
    except Exception as e:
        logger.error(f"Error in background fetch: {e}", exc_info=True)
        _update_progress(
            task_id,
            phase='error',
            message=f'An error occurred: {str(e)}',
            error=str(e)
        )


@login_required
def scrape_instagram_view(request):
    """
    Scrape Instagram posts for all user's accounts and automatically extract keywords.
    Supports both AJAX (with progress tracking) and regular POST requests.
    """
    if request.method != 'POST':
        return redirect('dashboard')
    
    accounts = InstagramAccount.objects.filter(user=request.user)
    if not accounts.exists():
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'error': 'No Instagram accounts found'}, status=400)
        messages.warning(request, 'Please add an Instagram account first.')
        return redirect('add_instagram')
    
    # Check if this is an AJAX request
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    
    if is_ajax:
        # Generate unique task ID
        task_id = str(uuid.uuid4())
        
        # Start background thread
        thread = threading.Thread(
            target=_fetch_posts_with_progress,
            args=(request.user, task_id),
            daemon=True
        )
        thread.start()
        
        # Return task_id immediately
        return JsonResponse({'task_id': task_id})
    
    # Regular POST request - process synchronously (backward compatibility)
    total_posts = 0
    total_errors = 0
    new_posts_for_keywords = []
    
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
            all_new_posts = []  # Track all new posts for Discord notifications
            
            # Define callback function to save posts immediately after each API call
            def save_posts_batch(posts_batch):
                """Save a batch of posts (including reels) to database immediately after API call."""
                nonlocal saved_count, skipped_reels, new_posts_for_keywords, all_new_posts
                
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
                        all_new_posts.append(post)  # Track all new posts for Discord
                        # Collect new posts with captions for keyword extraction
                        if post.caption and post.caption.strip():
                            new_posts_for_keywords.append(post)
            
            # Fetch posts with callback to save incrementally
            if has_posts:
                # Fetch until we reach the most recent stored post_id
                last_post = account.posts.order_by('-taken_at', '-created_at').first()
                stop_post_id = last_post.post_id if last_post else None
                logger.info(f"Account {username} has existing posts, fetching until stop_post_id={stop_post_id}")
                posts_data = instagram_service.get_all_posts_for_username(
                    username,
                    save_callback=save_posts_batch,
                    stop_post_id=stop_post_id
                )
            else:
                # No posts in database: fetch all posts (up to 600 limit from TEST_MODE_POSTS_LIMIT)
                logger.info(f"Account {username} has no posts in database, fetching all available posts (up to 600)")
                posts_data = instagram_service.get_all_posts_for_username(
                    username,
                    save_callback=save_posts_batch
                )
            
            account.last_scraped_at = timezone.now()
            account.save()
            
            # Send Discord notification for posts from last 24 hours
            if all_new_posts:
                recent_posts = filter_recent_posts(all_new_posts, hours=24)
                if recent_posts:
                    from django.conf import settings
                    from core.services.discord_service import send_discord_webhook
                    
                    webhook_url = getattr(settings, 'DISCORD_WEBHOOK_URL', '')
                    if webhook_url:
                        try:
                            send_discord_webhook(webhook_url, username, recent_posts)
                        except Exception as e:
                            logger.error(f"Error sending Discord notification for @{username}: {e}", exc_info=True)
                            # Don't fail the entire fetch if Discord fails
            
            total_posts += saved_count
            if skipped_reels > 0:
                messages.info(request, f'Fetched {saved_count} new posts for @{account.username} (skipped {skipped_reels} reels)')
            else:
                messages.success(request, f'Fetched {saved_count} new posts for @{account.username}')
            
        except Exception as e:
            total_errors += 1
            messages.error(request, f'Error fetching posts for @{account.username}: {str(e)}')
    
    # Automatically extract keywords for all newly fetched posts (only for recent posts, not initial bulk fetch)
    total_keywords_extracted = 0
    # Only extract keywords if account had existing posts (recent fetch, not initial bulk)
    # Skip keyword extraction for initial bulk fetch when new account is added
    if new_posts_for_keywords and has_posts:
        logger.info(f"Automatically extracting keywords for {len(new_posts_for_keywords)} newly fetched posts (recent posts only)")
        
        # Determine optimal number of workers for keyword extraction
        max_workers = min(len(new_posts_for_keywords), (os.cpu_count() or 4) * 2, 20)
        
        # Process keyword extraction concurrently
        results = []
        keyword_errors = 0
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all extraction tasks
            future_to_post = {
                executor.submit(_extract_keywords_for_post, post): post
                for post in new_posts_for_keywords
            }
            
            # Process completed tasks as they finish
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
        post_map = {post.id: post for post in new_posts_for_keywords}
        keywords_to_create = []
        posts_to_update = []
        post_ids_to_delete_keywords = []
        
        with transaction.atomic():
            for result in results:
                post_id = result['post_id']
                keywords = result['keywords']
                error = result['error']
                
                if error:
                    continue  # Skip posts with errors
                
                post = post_map.get(post_id)
                if not post:
                    continue
                
                post_ids_to_delete_keywords.append(post_id)
                
                for kw_data in keywords:
                    keywords_to_create.append(
                        InstagramKeyword(
                            post=post,
                            keyword=kw_data['keyword'],
                            similarity=kw_data.get('similarity')  # Together AI keywords have no similarity
                        )
                    )
                
                post.keywords_extracted = True
                posts_to_update.append(post)
            
            # Bulk operations
            if post_ids_to_delete_keywords:
                InstagramKeyword.objects.filter(post_id__in=post_ids_to_delete_keywords).delete()
            
            if keywords_to_create:
                InstagramKeyword.objects.bulk_create(keywords_to_create, batch_size=100)
                logger.info(f"Auto-extracted and saved {len(keywords_to_create)} keywords for {len(posts_to_update)} posts")
            
            if posts_to_update:
                InstagramPost.objects.bulk_update(posts_to_update, ['keywords_extracted'], batch_size=100)
        
        if total_keywords_extracted > 0:
            messages.success(request, f'Automatically extracted {total_keywords_extracted} keywords from {len(posts_to_update)} new posts!')
        if keyword_errors > 0:
            messages.warning(request, f'Encountered {keyword_errors} errors during automatic keyword extraction.')
    
    if total_posts > 0:
        messages.success(request, f'Successfully fetched {total_posts} new posts total!')
    if total_errors > 0:
        messages.warning(request, f'Encountered {total_errors} errors during fetching.')
    
    return redirect('dashboard')


@login_required
def fetch_single_account_posts_view(request, account_id):
    """
    Fetch posts for a single Instagram account.
    Supports both AJAX (with progress tracking) and regular POST requests.
    """
    if request.method != 'POST':
        return redirect('instagram_accounts')
    
    account = get_object_or_404(InstagramAccount, id=account_id, user=request.user)
    
    # Check if this is an AJAX request
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    
    if is_ajax:
        # Generate unique task ID
        task_id = str(uuid.uuid4())
        
        # Start background thread for single account
        def fetch_single_account_with_progress(user, account, task_id):
            try:
                # Initialize progress
                _update_progress(
                    task_id,
                    phase='fetching_posts',
                    current_account=account.username,
                    accounts_total=1,
                    accounts_processed=0,
                    posts_fetched=0,
                    posts_total=0,
                    start_time=time.time(),
                    message=f'Fetching posts for @{account.username}...'
                )
                
                username = account.username.strip().lstrip('@').lower()
                has_posts = account.posts.exists()
                account_new_posts = []
                account_saved_count = 0
                
                def save_posts_batch(posts_batch):
                    nonlocal account_saved_count, account_new_posts
                    batch_new_posts = 0
                    
                    for post_data in posts_batch:
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
                            account_saved_count += 1
                            batch_new_posts += 1
                            if post.caption and post.caption.strip():
                                account_new_posts.append(post)
                    
                    # Update progress
                    if batch_new_posts > 0:
                        current_progress = cache.get(f'fetch_progress_{task_id}', {})
                        current_fetched = current_progress.get('posts_fetched', 0)
                        new_fetched = current_fetched + batch_new_posts
                        current_total = current_progress.get('posts_total', 0)
                        new_total = max(new_fetched, current_total)
                        _update_progress(
                            task_id,
                            posts_fetched=new_fetched,
                            posts_total=new_total
                        )
                
                # Fetch posts
                if has_posts:
                    last_post = account.posts.order_by('-taken_at', '-created_at').first()
                    stop_post_id = last_post.post_id if last_post else None
                    logger.info(f"Account {username} has existing posts, fetching until stop_post_id={stop_post_id}")
                    posts_data = instagram_service.get_all_posts_for_username(
                        username, save_callback=save_posts_batch, stop_post_id=stop_post_id
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
                        from django.conf import settings
                        from core.services.discord_service import send_discord_webhook
                        
                        webhook_url = getattr(settings, 'DISCORD_WEBHOOK_URL', '')
                        if webhook_url:
                            try:
                                send_discord_webhook(webhook_url, username, recent_posts)
                            except Exception as e:
                                logger.error(f"Error sending Discord notification for @{username}: {e}", exc_info=True)
                
                # Extract keywords for new posts (only for recent posts, not initial bulk fetch)
                total_keywords_extracted = 0
                # Only extract keywords if account had existing posts (recent fetch, not initial bulk)
                if account_new_posts and has_posts:
                    # Update progress to show keyword extraction phase
                    estimated_keywords_total = len(account_new_posts) * 4
                    _update_progress(
                        task_id,
                        phase='extracting_keywords',
                        keyword_start_time=time.time(),
                        keywords_total=estimated_keywords_total,
                        keywords_extracted=0,
                        message=f'Extracting keywords from {len(account_new_posts)} posts...'
                    )
                    
                    max_workers = min(len(account_new_posts), (os.cpu_count() or 4) * 2, 20)
                    results = []
                    keyword_errors = 0
                    keywords_extracted_count = 0
                    
                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        future_to_post = {
                            executor.submit(_extract_keywords_for_post, post): post
                            for post in account_new_posts
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
                                    keywords_extracted_count += len(keywords)
                                    total_keywords_extracted += len(keywords)
                                    # Update progress
                                    current_progress = cache.get(f'fetch_progress_{task_id}', {})
                                    current_total = current_progress.get('keywords_total', 0)
                                    new_total = max(keywords_extracted_count, current_total)
                                    _update_progress(
                                        task_id,
                                        keywords_extracted=keywords_extracted_count,
                                        keywords_total=new_total
                                    )
                            except Exception as e:
                                logger.error(f"Exception extracting keywords for post {post.id}: {e}", exc_info=True)
                                keyword_errors += 1
                                results.append({
                                    'post_id': post.id,
                                    'keywords': [],
                                    'error': str(e)
                                })
                    
                    # Batch database operations for keyword saving
                    post_map = {post.id: post for post in account_new_posts}
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
                                        similarity=kw_data.get('similarity')  # Together AI keywords have no similarity
                                    )
                                )
                            
                            post.keywords_extracted = True
                            posts_to_update.append(post)
                        
                        if post_ids_to_delete_keywords:
                            InstagramKeyword.objects.filter(post_id__in=post_ids_to_delete_keywords).delete()
                        
                        if keywords_to_create:
                            InstagramKeyword.objects.bulk_create(keywords_to_create, batch_size=100)
                        
                        if posts_to_update:
                            InstagramPost.objects.bulk_update(posts_to_update, ['keywords_extracted'], batch_size=100)
                
                # Mark as completed
                _update_progress(
                    task_id,
                    phase='completed',
                    accounts_processed=1,
                    posts_fetched=account_saved_count,
                    posts_total=account_saved_count,
                    keywords_extracted=total_keywords_extracted,
                    keywords_total=total_keywords_extracted,
                    message=f'Successfully fetched {account_saved_count} posts and extracted {total_keywords_extracted} keywords for @{account.username}!'
                )
                
            except Exception as e:
                logger.error(f"Error fetching posts for @{account.username}: {e}", exc_info=True)
                _update_progress(
                    task_id,
                    phase='error',
                    error=str(e),
                    message=f'Error fetching posts for @{account.username}: {str(e)}'
                )
        
        thread = threading.Thread(
            target=fetch_single_account_with_progress,
            args=(request.user, account, task_id),
            daemon=True
        )
        thread.start()
        
        # Return task_id immediately
        return JsonResponse({'task_id': task_id})
    
    # Regular POST request - process synchronously
    try:
        username = account.username.strip().lstrip('@').lower()
        if not username:
            messages.warning(request, f'Invalid username for account: {account.username}')
            return redirect('instagram_accounts')
        
        has_posts = account.posts.exists()
        saved_count = 0
        skipped_reels = 0
        all_new_posts = []
        new_posts_for_keywords = []
        
        def save_posts_batch(posts_batch):
            nonlocal saved_count, skipped_reels, new_posts_for_keywords, all_new_posts
            
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
                
                if created:
                    saved_count += 1
                    all_new_posts.append(post)
                    if post.caption and post.caption.strip():
                        new_posts_for_keywords.append(post)
        
        # Fetch posts
        if has_posts:
            last_post = account.posts.order_by('-taken_at', '-created_at').first()
            stop_post_id = last_post.post_id if last_post else None
            logger.info(f"Account {username} has existing posts, fetching until stop_post_id={stop_post_id}")
            posts_data = instagram_service.get_all_posts_for_username(
                username, save_callback=save_posts_batch, stop_post_id=stop_post_id
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
        if all_new_posts:
            recent_posts = filter_recent_posts(all_new_posts, hours=24)
            if recent_posts:
                from django.conf import settings
                from core.services.discord_service import send_discord_webhook
                
                webhook_url = getattr(settings, 'DISCORD_WEBHOOK_URL', '')
                if webhook_url:
                    try:
                        send_discord_webhook(webhook_url, username, recent_posts)
                    except Exception as e:
                        logger.error(f"Error sending Discord notification for @{username}: {e}", exc_info=True)
        
        # Automatically extract keywords for all newly fetched posts (only for recent posts, not initial bulk fetch)
        total_keywords_extracted = 0
        # Only extract keywords if account had existing posts (recent fetch, not initial bulk)
        if new_posts_for_keywords and has_posts:
            logger.info(f"Automatically extracting keywords for {len(new_posts_for_keywords)} newly fetched posts (recent posts only)")
            
            # Determine optimal number of workers for keyword extraction
            max_workers = min(len(new_posts_for_keywords), (os.cpu_count() or 4) * 2, 20)
            
            # Process keyword extraction concurrently
            results = []
            keyword_errors = 0
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                # Submit all extraction tasks
                future_to_post = {
                    executor.submit(_extract_keywords_for_post, post): post
                    for post in new_posts_for_keywords
                }
                
                # Process completed tasks as they finish
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
            post_map = {post.id: post for post in new_posts_for_keywords}
            keywords_to_create = []
            posts_to_update = []
            post_ids_to_delete_keywords = []
            
            with transaction.atomic():
                for result in results:
                    post_id = result['post_id']
                    keywords = result['keywords']
                    error = result['error']
                    
                    if error:
                        continue  # Skip posts with errors
                    
                    # Get the post instance from our map (no database query needed)
                    post = post_map.get(post_id)
                    if not post:
                        logger.warning(f"Post {post_id} not found in post map, skipping")
                        continue
                    
                    # Collect post IDs for bulk keyword deletion
                    post_ids_to_delete_keywords.append(post_id)
                    
                    # Prepare new keywords for bulk creation
                    for kw_data in keywords:
                        keywords_to_create.append(
                            InstagramKeyword(
                                post=post,
                                keyword=kw_data['keyword'],
                                similarity=kw_data.get('similarity')  # Together AI keywords have no similarity
                            )
                        )
                    
                    # Mark post as processed
                    post.keywords_extracted = True
                    posts_to_update.append(post)
                
                # Bulk delete old keywords for all posts at once (more efficient than per-post deletion)
                if post_ids_to_delete_keywords:
                    InstagramKeyword.objects.filter(post_id__in=post_ids_to_delete_keywords).delete()
                    logger.info(f"Bulk deleted old keywords for {len(post_ids_to_delete_keywords)} posts")
                
                # Bulk create all keywords at once
                if keywords_to_create:
                    InstagramKeyword.objects.bulk_create(keywords_to_create, batch_size=100)
                    logger.info(f"Bulk created {len(keywords_to_create)} keywords")
                
                # Bulk update posts
                if posts_to_update:
                    InstagramPost.objects.bulk_update(posts_to_update, ['keywords_extracted'], batch_size=100)
                    logger.info(f"Bulk updated {len(posts_to_update)} posts")
        
        if total_keywords_extracted > 0:
            messages.success(request, f'Fetched {saved_count} new posts and extracted {total_keywords_extracted} keywords for @{account.username}!')
        else:
            messages.success(request, f'Fetched {saved_count} new posts for @{account.username}')
        if keyword_errors > 0:
            messages.warning(request, f'Encountered {keyword_errors} errors during automatic keyword extraction.')
        
    except Exception as e:
        messages.error(request, f'Error fetching posts for @{account.username}: {str(e)}')
    
    return redirect('instagram_accounts')


@login_required
def instagram_post_detail_view(request, post_id):
    """View details of a specific Instagram post."""
    post = get_object_or_404(InstagramPost.objects.prefetch_related('keywords'), id=post_id, account__user=request.user)
    carousel_items = post.carousel_items.all() if post.is_carousel else []
    
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
    
    # Serialize posts to JSON for JavaScript modal
    posts_json = []
    for post in posts:
        posts_json.append({
            'id': post.id,
            'title': post.title,
            'body': post.body,
            'url': post.url,
            'score': post.score,
            'flair': post.flair,
            'thumbnail_url': post.thumbnail_url,
            'media_url': post.media_url,
            'is_video': post.is_video,
            'post_type': post.post_type,
            'subreddit': post.subreddit.name,
            'scraped_at': post.scraped_at.isoformat(),
            'keywords': [{'keyword': kw.keyword, 'similarity': kw.similarity} for kw in post.keywords.all()],
        })
    
    # Fetch extracted keywords - get top keywords by similarity (for separate section)
    keywords = RedditKeyword.objects.filter(
        post__subreddit__user=request.user
    ).select_related('post', 'post__subreddit').order_by('-similarity')[:50]
    
    # Group keywords by keyword text to show frequency and average similarity
    keyword_stats = defaultdict(lambda: {'count': 0, 'total_similarity': 0.0, 'similarity_count': 0, 'posts': []})
    for kw in keywords:
        keyword_stats[kw.keyword]['count'] += 1
        if kw.similarity is not None:
            keyword_stats[kw.keyword]['total_similarity'] += kw.similarity
            keyword_stats[kw.keyword]['similarity_count'] += 1
        keyword_stats[kw.keyword]['posts'].append(kw.post)
    
    # Convert to list and calculate average similarity
    keyword_list = []
    for keyword, stats in keyword_stats.items():
        similarity_count = stats['similarity_count']
        avg_similarity = (stats['total_similarity'] / similarity_count) if similarity_count > 0 else 0.0
        keyword_list.append({
            'keyword': keyword,
            'count': stats['count'],
            'avg_similarity': avg_similarity,
            'posts': stats['posts'][:3]  # Show up to 3 posts per keyword
        })
    
    # Sort by average similarity (descending)
    keyword_list.sort(key=lambda x: x['avg_similarity'], reverse=True)
    
    context = {
        'subreddits': subreddits,
        'posts': posts,
        'posts_json': json.dumps(posts_json),
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
                        'thumbnail_url': post_data.get('thumbnail_url', ''),
                        'media_url': post_data.get('media_url', ''),
                        'is_video': post_data.get('is_video', False),
                        'post_type': post_data.get('post_type', ''),
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
    """Extract keywords from Reddit posts using Together AI."""
    if request.method != 'POST':
        return redirect('reddit')
    
    posts = list(RedditPost.objects.filter(
        subreddit__user=request.user,
        keywords_extracted=False
    )[:100])  # Process up to 100 posts at a time
    
    if not posts:
        messages.info(request, 'No posts need keyword extraction.')
        return redirect('reddit')
    
    max_workers = min(len(posts), (os.cpu_count() or 4) * 2, 20)
    logger.info(f"Extracting keywords for {len(posts)} Reddit posts using {max_workers} concurrent workers")
    
    results = []
    total_keywords = 0
    total_errors = 0
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_post = {
            executor.submit(_extract_keywords_for_reddit_post, post): post
            for post in posts
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
                    total_errors += 1
                else:
                    total_keywords += len(keywords)
            except Exception as e:
                logger.error(f"Exception extracting keywords for Reddit post {post.id}: {e}", exc_info=True)
                total_errors += 1
                results.append({
                    'post_id': post.id,
                    'keywords': [],
                    'error': str(e)
                })
    
    post_map = {post.id: post for post in posts}
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
                    RedditKeyword(
                        post=post,
                        keyword=kw_data['keyword'],
                        similarity=kw_data.get('similarity')  # Together AI keywords have no similarity
                    )
                )
            
            post.keywords_extracted = True
            posts_to_update.append(post)
        
        if post_ids_to_delete_keywords:
            RedditKeyword.objects.filter(post_id__in=post_ids_to_delete_keywords).delete()
        
        if keywords_to_create:
            RedditKeyword.objects.bulk_create(keywords_to_create, batch_size=100)
        
        if posts_to_update:
            RedditPost.objects.bulk_update(posts_to_update, ['keywords_extracted'], batch_size=100)
    
    if total_keywords > 0:
        messages.success(request, f'Extracted {total_keywords} keywords from {len(posts_to_update)} posts using Together AI!')
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


def _extract_keywords_for_post(post):
    """
    Extract keywords for a single Instagram post using Together AI.
    Designed for concurrent processing.
    
    Args:
        post: InstagramPost instance
    
    Returns:
        Tuple of (post_id, keywords_list, error_message)
        keywords_list contains dicts with 'keyword' field (similarity is None for Together AI)
    """
    try:
        if post.keywords_extracted:
            return post.id, [], None
        if not post.caption or not post.caption.strip():
            logger.debug(f"Skipping keyword extraction for post {post.id} - empty caption")
            return post.id, [], None
        
        # Extract keywords using Together AI
        keywords = extract_keywords_with_together_ai(str(post.post_id), post.caption)
        
        return post.id, keywords, None
    except Exception as e:
        logger.error(f"Error extracting keywords for Instagram post {post.id}: {e}", exc_info=True)
        return post.id, [], str(e)


def _extract_keywords_for_tweet(tweet):
    """
    Extract keywords for a single Twitter tweet using Together AI.
    Designed for concurrent processing.
    
    Args:
        tweet: TwitterTweet instance
    
    Returns:
        Tuple of (tweet_id, keywords_list, error_message)
        keywords_list contains dicts with 'keyword' field (similarity is None for Together AI)
    """
    try:
        if tweet.keywords_extracted:
            return tweet.id, [], None
        if not tweet.text or not tweet.text.strip():
            logger.debug(f"Skipping keyword extraction for tweet {tweet.id} - empty text")
            return tweet.id, [], None
        
        # Extract keywords using Together AI
        keywords = extract_keywords_with_together_ai(str(tweet.tweet_id), tweet.text)
        
        return tweet.id, keywords, None
    except Exception as e:
        logger.error(f"Error extracting keywords for Twitter tweet {tweet.id}: {e}", exc_info=True)
        return tweet.id, [], str(e)


def _extract_keywords_for_reddit_post(post):
    """
    Extract keywords for a single Reddit post using Together AI.
    Designed for concurrent processing.
    
    Args:
        post: RedditPost instance
    
    Returns:
        Tuple of (post_id, keywords_list, error_message)
        keywords_list contains dicts with 'keyword' field (similarity is None for Together AI)
    """
    try:
        if getattr(post, 'keywords_extracted', False):
            return post.id, [], None
        # Combine title and body for keyword extraction
        combined_text = (post.title + "\n\n" + post.body).strip()
        
        if not combined_text:
            logger.debug(f"Skipping keyword extraction for Reddit post {post.id} - empty text")
            return post.id, [], None
        
        # Use post URL as unique identifier (since RedditPost doesn't have a post_id field like Instagram)
        # We'll use the post's database ID converted to string
        post_identifier = str(post.id)
        
        # Extract keywords using Together AI
        keywords = extract_keywords_with_together_ai(post_identifier, combined_text)
        
        return post.id, keywords, None
    except Exception as e:
        logger.error(f"Error extracting keywords for Reddit post {post.id}: {e}", exc_info=True)
        return post.id, [], str(e)


@login_required
def extract_twitter_keywords_view(request):
    """
    Extract keywords from Twitter tweets using Together AI.
    Processes multiple tweets in parallel using ThreadPoolExecutor.
    """
    if request.method != 'POST':
        return redirect('twitter_accounts')
    
    # Get tweets that need keyword extraction
    tweets = list(TwitterTweet.objects.filter(
        account__user=request.user,
        keywords_extracted=False,
        text__isnull=False
    ).exclude(text='').select_related('account')[:100])  # Process up to 100 tweets at a time
    
    if not tweets:
        messages.info(request, 'No tweets need keyword extraction.')
        return redirect('twitter_accounts')
    
    max_workers = min(len(tweets), (os.cpu_count() or 4) * 2, 20)
    logger.info(f"Extracting keywords for {len(tweets)} tweets using {max_workers} concurrent workers")
    
    results = []
    total_keywords = 0
    total_errors = 0
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_tweet = {
            executor.submit(_extract_keywords_for_tweet, tweet): tweet
            for tweet in tweets
        }
        
        for future in as_completed(future_to_tweet):
            tweet = future_to_tweet[future]
            try:
                tweet_id, keywords, error = future.result()
                results.append({
                    'tweet_id': tweet_id,
                    'keywords': keywords,
                    'error': error
                })
                if error:
                    total_errors += 1
                else:
                    total_keywords += len(keywords)
            except Exception as e:
                logger.error(f"Exception extracting keywords for tweet {tweet.id}: {e}", exc_info=True)
                total_errors += 1
                results.append({
                    'tweet_id': tweet.id,
                    'keywords': [],
                    'error': str(e)
                })
    
    tweet_map = {tweet.id: tweet for tweet in tweets}
    keywords_to_create = []
    tweets_to_update = []
    tweet_ids_to_delete_keywords = []
    
    with transaction.atomic():
        for result in results:
            tweet_id = result['tweet_id']
            keywords = result['keywords']
            error = result['error']
            
            if error:
                continue
            
            tweet = tweet_map.get(tweet_id)
            if not tweet:
                continue
            
            tweet_ids_to_delete_keywords.append(tweet_id)
            
            for kw_data in keywords:
                keywords_to_create.append(
                    TwitterKeyword(
                        post=tweet,
                        keyword=kw_data['keyword'],
                        similarity=kw_data.get('similarity')  # Together AI keywords have no similarity
                    )
                )
            
            tweet.keywords_extracted = True
            tweets_to_update.append(tweet)
        
        if tweet_ids_to_delete_keywords:
            TwitterKeyword.objects.filter(post_id__in=tweet_ids_to_delete_keywords).delete()
        
        if keywords_to_create:
            TwitterKeyword.objects.bulk_create(keywords_to_create, batch_size=100)
        
        if tweets_to_update:
            TwitterTweet.objects.bulk_update(tweets_to_update, ['keywords_extracted'], batch_size=100)
    
    if total_keywords > 0:
        messages.success(request, f'Extracted {total_keywords} keywords from {len(tweets_to_update)} tweets using Together AI!')
    if total_errors > 0:
        messages.warning(request, f'Encountered {total_errors} errors during keyword extraction.')
    
    return redirect('twitter_accounts')


@login_required
def extract_instagram_keywords_view(request):
    """
    Extract keywords from Instagram posts using concurrent processing for optimal performance.
    Processes multiple posts in parallel using ThreadPoolExecutor.
    """
    if request.method != 'POST':
        return redirect('dashboard')
    
    # Get posts that need keyword extraction
    posts = list(InstagramPost.objects.filter(
        account__user=request.user,
        keywords_extracted=False,
        caption__isnull=False
    ).exclude(caption='').select_related('account')[:100])  # Process up to 100 posts at a time
    
    if not posts:
        messages.info(request, 'No posts need keyword extraction.')
        return redirect('dashboard')
    
    # Determine optimal number of workers
    # Use CPU count but cap at reasonable limit to avoid overwhelming the system
    max_workers = min(len(posts), (os.cpu_count() or 4) * 2, 20)  # Cap at 20 workers
    
    logger.info(f"Extracting keywords for {len(posts)} posts using {max_workers} concurrent workers")
    
    # Process posts concurrently
    results = []
    total_keywords = 0
    total_errors = 0
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all extraction tasks
        future_to_post = {
            executor.submit(_extract_keywords_for_post, post): post
            for post in posts
        }
        
        # Process completed tasks as they finish
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
                    total_errors += 1
                else:
                    total_keywords += len(keywords)
            except Exception as e:
                logger.error(f"Exception extracting keywords for post {post.id}: {e}", exc_info=True)
                total_errors += 1
                results.append({
                    'post_id': post.id,
                    'keywords': [],
                    'error': str(e)
                })
    
    # Create post_id to post mapping for efficient lookup (avoid redundant queries)
    post_map = {post.id: post for post in posts}
    
    # Batch database operations for efficiency
    # Group operations by post to minimize database queries
    keywords_to_create = []
    posts_to_update = []
    post_ids_to_delete_keywords = []
    
    with transaction.atomic():
        for result in results:
            post_id = result['post_id']
            keywords = result['keywords']
            error = result['error']
            
            if error:
                continue  # Skip posts with errors
            
            # Get the post instance from our map (no database query needed)
            post = post_map.get(post_id)
            if not post:
                logger.warning(f"Post {post_id} not found in post map, skipping")
                continue
            
            # Collect post IDs for bulk keyword deletion
            post_ids_to_delete_keywords.append(post_id)
            
            # Prepare new keywords for bulk creation
            for kw_data in keywords:
                keywords_to_create.append(
                    InstagramKeyword(
                        post=post,
                        keyword=kw_data['keyword'],
                        similarity=kw_data.get('similarity')  # Together AI keywords have no similarity
                    )
                )
            
            # Mark post as processed
            post.keywords_extracted = True
            posts_to_update.append(post)
        
        # Bulk delete old keywords for all posts at once (more efficient than per-post deletion)
        if post_ids_to_delete_keywords:
            InstagramKeyword.objects.filter(post_id__in=post_ids_to_delete_keywords).delete()
            logger.info(f"Bulk deleted old keywords for {len(post_ids_to_delete_keywords)} posts")
        
        # Bulk create all keywords at once
        if keywords_to_create:
            InstagramKeyword.objects.bulk_create(keywords_to_create, batch_size=100)
            logger.info(f"Bulk created {len(keywords_to_create)} keywords")
        
        # Bulk update posts
        if posts_to_update:
            InstagramPost.objects.bulk_update(posts_to_update, ['keywords_extracted'], batch_size=100)
            logger.info(f"Bulk updated {len(posts_to_update)} posts")
    
    # Show results to user
    if total_keywords > 0:
        messages.success(request, f'Extracted {total_keywords} keywords from {len(posts_to_update)} posts using concurrent processing!')
    if total_errors > 0:
        messages.warning(request, f'Encountered {total_errors} errors during keyword extraction.')
    
    return redirect('instagram_keywords')


@login_required
@require_http_methods(["GET"])
def check_fetch_progress_view(request, task_id):
    """
    Check progress of a fetch operation by task_id.
    Returns JSON with current progress data.
    """
    cache_key = f'fetch_progress_{task_id}'
    progress = cache.get(cache_key)
    
    if not progress:
        return JsonResponse({'error': 'Task not found or expired'}, status=404)
    
    # Calculate elapsed time
    if 'start_time' in progress:
        elapsed = time.time() - progress['start_time']
        progress['elapsed_time'] = elapsed
    
    return JsonResponse(progress)


@login_required
def instagram_keywords_view(request):
    """View extracted keywords from Instagram posts, grouped by post to show context."""
    # Get posts with keywords, ordered by most recent
    posts_with_keywords = InstagramPost.objects.filter(
        account__user=request.user,
        keywords__isnull=False
    ).distinct().prefetch_related('keywords', 'account').order_by('-taken_at')[:50]
    
    # Group keywords by post for context display
    posts_data = []
    for post in posts_with_keywords:
        keywords_list = list(post.keywords.all().order_by('-similarity'))
        if keywords_list:  # Only include posts that have keywords
            posts_data.append({
                'post': post,
                'keywords': keywords_list,
            })
    
    context = {
        'posts_data': posts_data,
    }
    return render(request, 'core/instagram_keywords.html', context)


# ========== Twitter Views ==========

@login_required
def twitter_accounts_view(request):
    """View all Twitter accounts with stats."""
    accounts = TwitterAccount.objects.filter(user=request.user).annotate(
        tweets_count=Count('tweets')
    )
    
    # Prepare data for each account
    accounts_data = []
    for account in accounts:
        tweets = TwitterTweet.objects.filter(account=account)
        
        # Calculate basic metrics
        total_tweets = tweets.count()
        total_favorites = tweets.aggregate(Sum('favorite_count'))['favorite_count__sum'] or 0
        total_retweets = tweets.aggregate(Sum('retweet_count'))['retweet_count__sum'] or 0
        avg_favorites = tweets.aggregate(Avg('favorite_count'))['favorite_count__avg'] or 0 if total_tweets > 0 else 0
        
        accounts_data.append({
            'account': account,
            'total_tweets': total_tweets,
            'total_favorites': total_favorites,
            'total_retweets': total_retweets,
            'avg_favorites': avg_favorites,
        })
    
    return render(request, 'core/twitter_accounts.html', {'accounts_data': accounts_data})


@login_required
def add_twitter_account_view(request, username=None):
    """
    Add a new Twitter account to monitor.
    If username parameter is provided, link the account to that SocialUsername.
    """
    # Get username from URL parameter or query string
    parent_username = username or request.GET.get('username')
    social_username = None
    redirect_to = 'twitter_accounts'
    
    # If parent username is provided, get or create the SocialUsername
    if parent_username:
        parent_username_clean = str(parent_username).strip().lstrip('@').lower()
        try:
            social_username = SocialUsername.objects.get(user=request.user, username=parent_username_clean)
            redirect_to = 'social_user_analytics'
        except SocialUsername.DoesNotExist:
            # If parent username doesn't exist, create it
            social_username = SocialUsername.objects.create(
                user=request.user,
                username=parent_username_clean
            )
            redirect_to = 'social_user_analytics'
    
    if request.method == 'POST':
        form = TwitterAccountForm(request.POST)
        if form.is_valid():
            username_input = form.cleaned_data['username'].strip().lstrip('@').lower()
            
            # Check if account already exists
            existing_account = TwitterAccount.objects.filter(
                user=request.user,
                username=username_input
            ).first()
            
            if existing_account:
                # If account exists, just link it to the social_username if provided
                if social_username and existing_account.social_username != social_username:
                    existing_account.social_username = social_username
                    existing_account.save()
                    messages.info(request, f'Twitter account @{username_input} already exists. Linked to @{parent_username_clean}.')
                else:
                    messages.info(request, f'Twitter account @{username_input} is already being monitored.')
                
                if redirect_to == 'social_user_analytics':
                    return redirect('social_user_analytics', username=parent_username_clean)
                return redirect(redirect_to)
            
            # Fetch user info from Twitter API to get rest_id
            try:
                user_info = twitter_service.get_user_by_username(username_input)
                if not user_info:
                    messages.error(request, f'Could not find Twitter account @{username_input}. Please check the username and try again.')
                    if redirect_to == 'social_user_analytics':
                        return redirect('social_user_analytics', username=parent_username_clean)
                    return redirect(redirect_to)
                
                # Create account with fetched info
                account = TwitterAccount(
                    user=request.user,
                    username=username_input,
                    rest_id=user_info.get('rest_id', ''),
                    name=user_info.get('name', ''),
                    description=user_info.get('description', ''),
                    followers_count=user_info.get('followers_count', 0),
                    following_count=user_info.get('following_count', 0),
                    tweet_count=user_info.get('tweet_count', 0),
                    verified=user_info.get('verified', False),
                    profile_image_url=user_info.get('profile_image_url', ''),
                    profile_banner_url=user_info.get('profile_banner_url', ''),
                )
                if social_username:
                    account.social_username = social_username
                account.save()
                messages.success(request, f'Twitter account @{username_input} added successfully!')
                if redirect_to == 'social_user_analytics':
                    return redirect('social_user_analytics', username=parent_username_clean)
                return redirect(redirect_to)
            except Exception as e:
                logger.error(f"Error adding Twitter account {username_input}: {e}", exc_info=True)
                messages.error(request, f'Error adding Twitter account: {str(e)}')
    else:
        form = TwitterAccountForm()
    
    context = {'form': form}
    if parent_username:
        context['parent_username'] = parent_username_clean
        context['redirect_url'] = f"/social/analytics/{parent_username_clean}/"
    
    return render(request, 'core/add_twitter.html', context)


@login_required
def delete_twitter_account_view(request, account_id):
    """Delete a Twitter account."""
    account = get_object_or_404(TwitterAccount, id=account_id, user=request.user)
    if request.method == 'POST':
        username = account.username
        account.delete()
        messages.success(request, f'Twitter account @{username} deleted successfully!')
    return redirect('social_dashboard')


@login_required
def fetch_single_twitter_account_tweets_view(request, account_id):
    """Fetch tweets for a single Twitter account with progress tracking."""
    account = get_object_or_404(TwitterAccount, id=account_id, user=request.user)
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        # AJAX request - return JSON response
        task_id = str(uuid.uuid4())
        
        # Start background thread to fetch tweets
        thread = threading.Thread(
            target=_fetch_twitter_tweets_with_progress,
            args=(request.user, account, task_id),
            daemon=True
        )
        thread.start()
        
        return JsonResponse({'task_id': task_id, 'status': 'started'})
    else:
        # Regular request - start fetch and redirect
        task_id = str(uuid.uuid4())
        thread = threading.Thread(
            target=_fetch_twitter_tweets_with_progress,
            args=(request.user, account, task_id),
            daemon=True
        )
        thread.start()
        messages.info(request, f'Started fetching tweets for @{account.username}. This may take a few moments.')
        return redirect('twitter_accounts')


def _fetch_twitter_tweets_with_progress(user, account, task_id):
    """Background function to fetch tweets with progress tracking."""
    try:
        _update_progress(task_id, status='fetching', message=f'Fetching tweets for @{account.username}...', progress=0)
        
        # Check if account has existing tweets
        has_tweets = TwitterTweet.objects.filter(account=account).exists()
        
        # Track new tweets for keyword extraction
        new_tweets_for_keywords = []
        
        # Determine fetching strategy
        stop_tweet_id = None
        if has_tweets:
            last_tweet = TwitterTweet.objects.filter(account=account).order_by('-created_at', '-created_at_db').first()
            stop_tweet_id = last_tweet.tweet_id if last_tweet else None
            logger.info(f"Account @{account.username} has existing tweets, fetching until stop_tweet_id={stop_tweet_id}")
        else:
            logger.info(f"Account @{account.username} has no tweets in database, fetching all available tweets")
        
        # Fetch tweets
        tweets_data = twitter_service.get_all_tweets_for_user(
            account.username,
            save_callback=lambda batch: _save_twitter_tweets_batch(account, batch, task_id, new_tweets_for_keywords),
            stop_tweet_id=stop_tweet_id
        )
        
        # Extract keywords for new tweets (only for recent tweets, not initial bulk fetch)
        total_keywords_extracted = 0
        if new_tweets_for_keywords:
            logger.info(f"Automatically extracting keywords for {len(new_tweets_for_keywords)} newly fetched tweets (including first fetch)")
            
            max_workers = min(len(new_tweets_for_keywords), (os.cpu_count() or 4) * 2, 20)
            results = []
            keyword_errors = 0
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_tweet = {
                    executor.submit(_extract_keywords_for_tweet, tweet): tweet
                    for tweet in new_tweets_for_keywords
                }
                
                for future in as_completed(future_to_tweet):
                    tweet = future_to_tweet[future]
                    try:
                        tweet_id, keywords, error = future.result()
                        results.append({
                            'tweet_id': tweet_id,
                            'keywords': keywords,
                            'error': error
                        })
                        if error:
                            keyword_errors += 1
                        else:
                            total_keywords_extracted += len(keywords)
                    except Exception as e:
                        logger.error(f"Exception extracting keywords for tweet {tweet.id}: {e}", exc_info=True)
                        keyword_errors += 1
                        results.append({
                            'tweet_id': tweet.id,
                            'keywords': [],
                            'error': str(e)
                        })
            
            # Batch database operations
            tweet_map = {tweet.id: tweet for tweet in new_tweets_for_keywords}
            keywords_to_create = []
            tweets_to_update = []
            tweet_ids_to_delete_keywords = []
            
            with transaction.atomic():
                for result in results:
                    tweet_id = result['tweet_id']
                    keywords = result['keywords']
                    error = result['error']
                    
                    if error:
                        continue
                    
                    tweet = tweet_map.get(tweet_id)
                    if not tweet:
                        continue
                    
                    tweet_ids_to_delete_keywords.append(tweet_id)
                    
                    for kw_data in keywords:
                        keywords_to_create.append(
                            TwitterKeyword(
                                post=tweet,
                                keyword=kw_data['keyword'],
                                similarity=kw_data.get('similarity')  # Together AI keywords have no similarity
                            )
                        )
                    
                    tweet.keywords_extracted = True
                    tweets_to_update.append(tweet)
                
                if tweet_ids_to_delete_keywords:
                    TwitterKeyword.objects.filter(post_id__in=tweet_ids_to_delete_keywords).delete()
                
                if keywords_to_create:
                    TwitterKeyword.objects.bulk_create(keywords_to_create, batch_size=100)
                    logger.info(f"Auto-extracted and saved {len(keywords_to_create)} keywords for {len(tweets_to_update)} tweets")
                
                if tweets_to_update:
                    TwitterTweet.objects.bulk_update(tweets_to_update, ['keywords_extracted'], batch_size=100)
        
        # Update progress
        message = f'Fetched {len(tweets_data)} tweets for @{account.username}'
        if total_keywords_extracted > 0:
            message += f' and extracted {total_keywords_extracted} keywords'
        _update_progress(task_id, status='completed', message=message, progress=100)
        
        # Update account last_scraped_at
        account.last_scraped_at = timezone.now()
        account.save()
        
    except Exception as e:
        logger.error(f"Error fetching tweets for @{account.username}: {e}", exc_info=True)
        _update_progress(task_id, status='error', message=f'Error: {str(e)}', progress=0)


def _save_twitter_tweets_batch(account, tweets_batch, task_id, new_tweets_list=None):
    """Save a batch of tweets to the database."""
    saved_count = 0
    new_count = 0
    
    for tweet_data in tweets_batch:
        tweet, created = TwitterTweet.objects.update_or_create(
            account=account,
            tweet_id=tweet_data['tweet_id'],
            defaults={
                'text': tweet_data.get('text', ''),
                'created_at': tweet_data.get('created_at'),
                'favorite_count': tweet_data.get('favorite_count', 0),
                'retweet_count': tweet_data.get('retweet_count', 0),
                'reply_count': tweet_data.get('reply_count', 0),
                'quote_count': tweet_data.get('quote_count', 0),
                'view_count': tweet_data.get('view_count', 0),
                'media': tweet_data.get('media', []),
                'hashtags': tweet_data.get('hashtags', []),
                'mentions': tweet_data.get('mentions', []),
                'urls': tweet_data.get('urls', []),
                'is_retweet': tweet_data.get('is_retweet', False),
                'is_quote': tweet_data.get('is_quote', False),
                'lang': tweet_data.get('lang', ''),
            }
        )
        
        saved_count += 1
        if created:
            new_count += 1
            # Track new tweets with text for keyword extraction
        if new_tweets_list is not None and tweet.text and tweet.text.strip() and not tweet.keywords_extracted:
                new_tweets_list.append(tweet)
    
    # Update progress
    _update_progress(task_id, message=f'Saved {saved_count} tweets ({new_count} new)', progress=50)
    logger.info(f"Saved {saved_count} tweets for @{account.username} ({new_count} new)")


@login_required
def scrape_twitter_view(request):
    """Fetch tweets for all Twitter accounts."""
    if request.method != 'POST':
        messages.error(request, 'Invalid request method.')
        return redirect('twitter_accounts')
    
    accounts = TwitterAccount.objects.filter(user=request.user)
    if not accounts.exists():
        messages.warning(request, 'No Twitter accounts added yet.')
        return redirect('twitter_accounts')
    
    task_id = str(uuid.uuid4())
    
    # Start background thread
    thread = threading.Thread(
        target=_fetch_all_twitter_accounts_tweets,
        args=(request.user, task_id),
        daemon=True
    )
    thread.start()
    
    messages.info(request, f'Started fetching tweets for {accounts.count()} Twitter account(s). This may take a few moments.')
    return redirect('twitter_accounts')


def _fetch_all_twitter_accounts_tweets(user, task_id):
    """Fetch tweets for all Twitter accounts concurrently."""
    try:
        accounts = list(TwitterAccount.objects.filter(user=user))
        total_accounts = len(accounts)
        
        _update_progress(task_id, status='fetching', message=f'Fetching tweets for {total_accounts} account(s)...', progress=0)
        
        def fetch_account_tweets(account):
            """Fetch tweets for a single account."""
            try:
                has_tweets = TwitterTweet.objects.filter(account=account).exists()
                new_tweets_for_keywords = []
                
                if has_tweets:
                    last_tweet = TwitterTweet.objects.filter(account=account).order_by('-created_at', '-created_at_db').first()
                    stop_tweet_id = last_tweet.tweet_id if last_tweet else None
                else:
                    stop_tweet_id = None
                
                tweets_data = twitter_service.get_all_tweets_for_user(
                    account.username,
                    save_callback=lambda batch: _save_twitter_tweets_batch(account, batch, task_id, new_tweets_for_keywords),
                    stop_tweet_id=stop_tweet_id
                )
                
                # Extract keywords for new tweets (only for recent tweets, not initial bulk fetch)
                if new_tweets_for_keywords and has_tweets:
                    logger.info(f"Extracting keywords for {len(new_tweets_for_keywords)} new tweets for @{account.username}")
                    try:
                        max_workers = min(len(new_tweets_for_keywords), (os.cpu_count() or 4) * 2, 20)
                        results = []
                        
                        with ThreadPoolExecutor(max_workers=max_workers) as executor:
                            future_to_tweet = {
                                executor.submit(_extract_keywords_for_tweet, tweet): tweet
                                for tweet in new_tweets_for_keywords
                            }
                            
                            for future in as_completed(future_to_tweet):
                                tweet = future_to_tweet[future]
                                try:
                                    tweet_id, keywords, error = future.result()
                                    if not error:
                                        results.append((tweet, keywords))
                                except Exception as e:
                                    logger.error(f"Exception extracting keywords for tweet {tweet.id}: {e}", exc_info=True)
                        
                        # Batch save keywords
                        if results:
                            keywords_to_create = []
                            tweets_to_update = []
                            
                            with transaction.atomic():
                                for tweet, keywords in results:
                                    # Delete old keywords
                                    TwitterKeyword.objects.filter(post=tweet).delete()
                                    
                                    # Create new keywords
                                    for kw_data in keywords:
                                        keywords_to_create.append(
                                            TwitterKeyword(
                                                post=tweet,
                                                keyword=kw_data['keyword'],
                                                similarity=kw_data.get('similarity')
                                            )
                                        )
                                    
                                    tweet.keywords_extracted = True
                                    tweets_to_update.append(tweet)
                                
                                if keywords_to_create:
                                    TwitterKeyword.objects.bulk_create(keywords_to_create, batch_size=100)
                                if tweets_to_update:
                                    TwitterTweet.objects.bulk_update(tweets_to_update, ['keywords_extracted'], batch_size=100)
                    except Exception as e:
                        logger.error(f"Error extracting keywords for @{account.username}: {e}", exc_info=True)
                
                account.last_scraped_at = timezone.now()
                account.save()
                
                return len(tweets_data)
            except Exception as e:
                logger.error(f"Error fetching tweets for @{account.username}: {e}", exc_info=True)
                return 0
        
        # Fetch tweets concurrently
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(fetch_account_tweets, account): account for account in accounts}
            total_tweets = 0
            
            for future in as_completed(futures):
                account = futures[future]
                try:
                    count = future.result()
                    total_tweets += count
                    logger.info(f"Fetched {count} tweets for @{account.username}")
                except Exception as e:
                    logger.error(f"Error processing @{account.username}: {e}", exc_info=True)
        
        _update_progress(task_id, status='completed', message=f'Fetched tweets for {total_accounts} account(s)', progress=100)
        
    except Exception as e:
        logger.error(f"Error in _fetch_all_twitter_accounts_tweets: {e}", exc_info=True)
        _update_progress(task_id, status='error', message=f'Error: {str(e)}', progress=0)


@login_required
def twitter_tweets_view(request):
    """View all tweets from all Twitter accounts."""
    # Get all tweets for the user
    tweets = TwitterTweet.objects.filter(account__user=request.user).select_related('account').order_by('-created_at')
    
    # Group tweets by account
    tweets_by_account = defaultdict(list)
    for tweet in tweets:
        tweets_by_account[tweet.account].append(tweet)
    
    # Convert to list of dictionaries
    account_tweets_list = []
    for account, account_tweets in tweets_by_account.items():
        account_tweets_list.append({
            'account': account,
            'tweets': account_tweets,
            'count': len(account_tweets)
        })
    
    # Sort by most recent tweet
    account_tweets_list.sort(key=lambda x: x['tweets'][0].created_at if x['tweets'] else timezone.now(), reverse=True)
    
    context = {
        'account_tweets_list': account_tweets_list,
    }
    return render(request, 'core/twitter_tweets.html', context)


@login_required
def twitter_account_tweets_view(request, account_id):
    """View tweets for a single Twitter account."""
    account = get_object_or_404(TwitterAccount, id=account_id, user=request.user)
    tweets = TwitterTweet.objects.filter(account=account).order_by('-created_at')
    
    context = {
        'account': account,
        'tweets': tweets,
        'count': tweets.count(),
    }
    return render(request, 'core/twitter_account_tweets.html', context)


@login_required
def social_dashboard_view(request):
    """
    Unified dashboard: add IG/Twitter accounts and list handles with analytics links.
    Accounts are linked under a unified SocialUsername.
    """
    form = SocialAccountForm(request.POST or None)
    if request.method == 'POST':
        if form.is_valid():
            ig_username = form.cleaned_data.get('instagram_username')
            tw_username = form.cleaned_data.get('twitter_username')
            
            if not ig_username and not tw_username:
                messages.error(request, "Please provide at least one username.")
                return redirect('social_dashboard')
            
            # Determine the unified username
            # If both are provided, use Instagram username as primary (or Twitter if IG not provided)
            # If only one is provided, use that one
            if ig_username and tw_username:
                # Both provided - use Instagram as primary, but link both to same SocialUsername
                unified_username = ig_username.lower()
            elif ig_username:
                unified_username = ig_username.lower()
            else:
                unified_username = tw_username.lower()
            
            try:
                # Get or create the unified SocialUsername
                social_username, created = SocialUsername.objects.get_or_create(
                    user=request.user,
                    username=unified_username
                )
                
                # Create Instagram account if provided
                if ig_username:
                    ig_username_clean = ig_username.strip().lstrip('@').lower()
                    ig_account, ig_created = InstagramAccount.objects.get_or_create(
                        user=request.user,
                        username=ig_username_clean,
                        defaults={'social_username': social_username}
                    )
                    # Always link to social_username (update if needed)
                    if ig_account.social_username != social_username:
                        ig_account.social_username = social_username
                        ig_account.save()
                    if not ig_created:
                        messages.info(request, f'Instagram account @{ig_username_clean} already exists. Linked to @{unified_username}.')
                
                # Create Twitter account if provided
                if tw_username:
                    tw_username_clean = tw_username.strip().lstrip('@').lower()
                    tw_account, tw_created = TwitterAccount.objects.get_or_create(
                        user=request.user,
                        username=tw_username_clean,
                        defaults={'social_username': social_username}
                    )
                    # Always link to social_username (update if needed)
                    if tw_account.social_username != social_username:
                        tw_account.social_username = social_username
                        tw_account.save()
                    if not tw_created:
                        messages.info(request, f'Twitter account @{tw_username_clean} already exists. Linked to @{unified_username}.')
                
                # Only show success if at least one new account was created
                if ig_username and ig_created or tw_username and tw_created:
                    messages.success(request, "Accounts added successfully.")
                elif ig_username or tw_username:
                    # Both accounts already existed, just linked
                    pass  # Info messages already shown above
                return redirect('social_dashboard')
            except Exception as e:
                logger.error(f"Error adding social accounts: {e}", exc_info=True)
                messages.error(request, f"Error adding accounts: {str(e)}")
        else:
            messages.error(request, "Please fix the errors in the form.")

    # Get all SocialUsernames for this user with their linked accounts
    social_usernames = SocialUsername.objects.filter(user=request.user).prefetch_related(
        'instagram_accounts', 'twitter_accounts'
    ).order_by('username')

    handle_cards = []
    for social_username in social_usernames:
        handle_cards.append({
            'username': social_username.username,
            'ig': list(social_username.instagram_accounts.all()),
            'tw': list(social_username.twitter_accounts.all()),
            'social_username': social_username
        })

    context = {
        'form': form,
        'handle_cards': handle_cards,
    }
    return render(request, 'core/social_dashboard.html', context)


@login_required
def delete_social_username_view(request, username):
    """Delete a SocialUsername and all associated accounts."""
    username_clean = str(username).strip().lstrip('@').lower()
    social_username = get_object_or_404(SocialUsername, username=username_clean, user=request.user)
    
    if request.method == 'POST':
        username_display = social_username.username
        # Count accounts that will be deleted (for user feedback)
        ig_count = social_username.instagram_accounts.count()
        tw_count = social_username.twitter_accounts.count()
        
        # Delete the SocialUsername (this will cascade delete all linked accounts)
        social_username.delete()
        
        # Build success message
        account_info = []
        if ig_count > 0:
            account_info.append(f"{ig_count} Instagram account{'s' if ig_count > 1 else ''}")
        if tw_count > 0:
            account_info.append(f"{tw_count} Twitter account{'s' if tw_count > 1 else ''}")
        
        if account_info:
            messages.success(request, f'Username @{username_display} and {", ".join(account_info)} deleted successfully!')
        else:
            messages.success(request, f'Username @{username_display} deleted successfully!')
    
    return redirect('social_dashboard')


@login_required
def social_user_analytics_view(request, username):
    """
    Show combined analytics for a specific username across Instagram and Twitter.
    Includes advanced metrics: engagement rates, time-based analysis, content performance, etc.
    """
    username_clean = str(username).strip().lstrip('@').lower()
    
    # Try to find by SocialUsername first, then fallback to direct username matching
    try:
        social_username = SocialUsername.objects.get(user=request.user, username=username_clean)
        ig_accounts = list(social_username.instagram_accounts.all())
        tw_accounts = list(social_username.twitter_accounts.all())
    except SocialUsername.DoesNotExist:
        # Fallback: find accounts by username directly (for backward compatibility)
        ig_accounts = list(InstagramAccount.objects.filter(user=request.user, username__iexact=username_clean))
        tw_accounts = list(TwitterAccount.objects.filter(user=request.user, username__iexact=username_clean))

    has_ig_accounts = len(ig_accounts) > 0
    has_tw_accounts = len(tw_accounts) > 0

    ig_posts = list(
        InstagramPost.objects.filter(account__in=ig_accounts)
        .select_related('account')
        .order_by('-taken_at')
    )
    tw_tweets = list(
        TwitterTweet.objects.filter(account__in=tw_accounts)
        .select_related('account')
        .order_by('-created_at')
    )

    # Helper functions for analytics
    def weekday_counts(items, dt_getter):
        counts = [0] * 7
        for item in items:
            dt = dt_getter(item)
            if dt:
                counts[dt.weekday()] += 1
        return counts

    def hour_counts(items, dt_getter):
        """Count items by hour of day (0-23)"""
        counts = [0] * 24
        for item in items:
            dt = dt_getter(item)
            if dt:
                counts[dt.hour] += 1
        return counts

    def hour_engagement(items, dt_getter, engagement_getter):
        """Calculate average engagement by hour of day"""
        hour_data = defaultdict(lambda: {'count': 0, 'total': 0})
        for item in items:
            dt = dt_getter(item)
            if dt:
                hour = dt.hour
                hour_data[hour]['count'] += 1
                hour_data[hour]['total'] += engagement_getter(item)
        
        # Convert to list with averages
        result = []
        for hour in range(24):
            if hour_data[hour]['count'] > 0:
                result.append(hour_data[hour]['total'] / hour_data[hour]['count'])
            else:
                result.append(0)
        return result

    def month_counts(items, dt_getter):
        """Count items by month (YYYY-MM format)"""
        month_data = defaultdict(int)
        for item in items:
            dt = dt_getter(item)
            if dt:
                month_key = dt.strftime('%Y-%m')
                month_data[month_key] += 1
        return dict(month_data)

    def top_performers(items, metric_getter, limit=5):
        """Get top N items by a metric"""
        sorted_items = sorted(items, key=metric_getter, reverse=True)
        return sorted_items[:limit]

    # Basic counts
    ig_total_posts = len(ig_posts)
    ig_total_likes = sum(p.like_count for p in ig_posts)
    ig_total_comments = sum(p.comment_count for p in ig_posts)
    
    tw_total_tweets = len(tw_tweets)
    tw_total_faves = sum(t.favorite_count for t in tw_tweets)
    tw_total_retweets = sum(t.retweet_count for t in tw_tweets)
    tw_total_replies = sum(t.reply_count for t in tw_tweets)
    tw_total_views = sum(t.view_count for t in tw_tweets)

    # Phase 1: Enhanced Metrics Calculations
    ig_avg_likes = ig_total_likes / ig_total_posts if ig_total_posts > 0 else 0
    ig_avg_comments = ig_total_comments / ig_total_posts if ig_total_posts > 0 else 0
    ig_avg_engagement = (ig_total_likes + ig_total_comments) / ig_total_posts if ig_total_posts > 0 else 0

    tw_avg_faves = tw_total_faves / tw_total_tweets if tw_total_tweets > 0 else 0
    tw_avg_retweets = tw_total_retweets / tw_total_tweets if tw_total_tweets > 0 else 0
    tw_avg_views = tw_total_views / tw_total_tweets if tw_total_tweets > 0 else 0
    tw_avg_engagement = (tw_total_faves + tw_total_retweets + tw_total_replies) / tw_total_tweets if tw_total_tweets > 0 else 0

    # Phase 1.2: Content Type Distribution
    ig_regular_posts = sum(1 for p in ig_posts if not p.is_reel and not p.is_video and not p.is_carousel)
    ig_reels = sum(1 for p in ig_posts if p.is_reel)
    ig_videos = sum(1 for p in ig_posts if p.is_video and not p.is_reel)
    ig_carousels = sum(1 for p in ig_posts if p.is_carousel)
    
    tw_original = sum(1 for t in tw_tweets if not t.is_retweet and not t.is_quote)
    tw_retweets = sum(1 for t in tw_tweets if t.is_retweet)
    tw_quotes = sum(1 for t in tw_tweets if t.is_quote)

    # Phase 2: Time-Based Analytics
    ig_weekday_counts = weekday_counts(ig_posts, lambda p: p.taken_at)
    tw_weekday_counts = weekday_counts(tw_tweets, lambda t: t.created_at)
    
    # Weekday engagement averages
    ig_weekday_engagement = [0] * 7
    tw_weekday_engagement = [0] * 7
    for i in range(7):
        ig_day_posts = [p for p in ig_posts if p.taken_at and p.taken_at.weekday() == i]
        tw_day_tweets = [t for t in tw_tweets if t.created_at and t.created_at.weekday() == i]
        
        if ig_day_posts:
            ig_weekday_engagement[i] = sum(p.like_count + p.comment_count for p in ig_day_posts) / len(ig_day_posts)
        if tw_day_tweets:
            tw_weekday_engagement[i] = sum(t.favorite_count + t.retweet_count for t in tw_day_tweets) / len(tw_day_tweets)

    # Hour of day analysis
    ig_hour_counts = hour_counts(ig_posts, lambda p: p.taken_at)
    tw_hour_counts = hour_counts(tw_tweets, lambda t: t.created_at)
    ig_hour_engagement = hour_engagement(ig_posts, lambda p: p.taken_at, lambda p: p.like_count + p.comment_count)
    tw_hour_engagement = hour_engagement(tw_tweets, lambda t: t.created_at, lambda t: t.favorite_count + t.retweet_count)

    # Monthly trends
    ig_month_counts = month_counts(ig_posts, lambda p: p.taken_at)
    tw_month_counts = month_counts(tw_tweets, lambda t: t.created_at)

    # Phase 3: Top Performing Content
    ig_top_by_likes = top_performers(ig_posts, lambda p: p.like_count, 5)
    ig_top_by_comments = top_performers(ig_posts, lambda p: p.comment_count, 5)
    ig_top_by_engagement = top_performers(ig_posts, lambda p: p.like_count + p.comment_count, 5)
    
    tw_top_by_faves = top_performers(tw_tweets, lambda t: t.favorite_count, 5)
    tw_top_by_retweets = top_performers(tw_tweets, lambda t: t.retweet_count, 5)
    tw_top_by_views = top_performers(tw_tweets, lambda t: t.view_count, 5)
    tw_top_by_engagement = top_performers(tw_tweets, lambda t: t.favorite_count + t.retweet_count + t.reply_count, 5)

    # Phase 5: Enhanced Twitter Analytics
    hashtag_counter = Counter()
    hashtag_engagement = defaultdict(lambda: {'count': 0, 'total_engagement': 0})
    mention_counter = Counter()
    mention_engagement = defaultdict(lambda: {'count': 0, 'total_engagement': 0})
    url_counter = Counter()
    url_engagement = defaultdict(lambda: {'count': 0, 'total_engagement': 0})
    lang_counter = Counter()
    lang_engagement = defaultdict(lambda: {'count': 0, 'total_engagement': 0})
    
    for t in tw_tweets:
        engagement = t.favorite_count + t.retweet_count + t.reply_count
        
        # Hashtags
        for h in t.hashtags or []:
            h_lower = h.lower()
            hashtag_counter[h_lower] += 1
            hashtag_engagement[h_lower]['count'] += 1
            hashtag_engagement[h_lower]['total_engagement'] += engagement
        
        # Mentions
        for m in t.mentions or []:
            m_lower = m.lower()
            mention_counter[m_lower] += 1
            mention_engagement[m_lower]['count'] += 1
            mention_engagement[m_lower]['total_engagement'] += engagement
        
        # URLs
        for url in t.urls or []:
            url_counter[url] += 1
            url_engagement[url]['count'] += 1
            url_engagement[url]['total_engagement'] += engagement
        
        # Language
        if t.lang:
            lang_counter[t.lang] += 1
            lang_engagement[t.lang]['count'] += 1
            lang_engagement[t.lang]['total_engagement'] += engagement

    top_hashtags = hashtag_counter.most_common(10)
    top_hashtags_by_engagement = sorted(
        [(tag, hashtag_engagement[tag]['total_engagement'] / hashtag_engagement[tag]['count'] if hashtag_engagement[tag]['count'] > 0 else 0) 
         for tag in hashtag_counter.keys()],
        key=lambda x: x[1], reverse=True
    )[:10]
    
    top_mentions = mention_counter.most_common(10)
    top_mentions_by_engagement = sorted(
        [(mention, mention_engagement[mention]['total_engagement'] / mention_engagement[mention]['count'] if mention_engagement[mention]['count'] > 0 else 0)
         for mention in mention_counter.keys()],
        key=lambda x: x[1], reverse=True
    )[:10]

    # Phase 6: Instagram-Specific Analytics
    ig_reels_posts = [p for p in ig_posts if p.is_reel]
    ig_regular_posts_list = [p for p in ig_posts if not p.is_reel and not p.is_video and not p.is_carousel]
    ig_video_posts = [p for p in ig_posts if p.is_video and not p.is_reel]
    ig_image_posts = [p for p in ig_posts if not p.is_video and not p.is_reel and not p.is_carousel]
    ig_carousel_posts = [p for p in ig_posts if p.is_carousel]

    # Reel performance
    ig_reels_avg_likes = sum(p.like_count for p in ig_reels_posts) / len(ig_reels_posts) if ig_reels_posts else 0
    ig_reels_avg_comments = sum(p.comment_count for p in ig_reels_posts) / len(ig_reels_posts) if ig_reels_posts else 0
    ig_reels_avg_engagement = (sum(p.like_count + p.comment_count for p in ig_reels_posts) / len(ig_reels_posts)) if ig_reels_posts else 0
    
    ig_regular_avg_engagement = (sum(p.like_count + p.comment_count for p in ig_regular_posts_list) / len(ig_regular_posts_list)) if ig_regular_posts_list else 0
    
    # Video vs Image
    ig_video_avg_engagement = (sum(p.like_count + p.comment_count for p in ig_video_posts) / len(ig_video_posts)) if ig_video_posts else 0
    ig_image_avg_engagement = (sum(p.like_count + p.comment_count for p in ig_image_posts) / len(ig_image_posts)) if ig_image_posts else 0
    
    # Carousel analysis
    ig_carousel_avg_engagement = (sum(p.like_count + p.comment_count for p in ig_carousel_posts) / len(ig_carousel_posts)) if ig_carousel_posts else 0
    ig_single_avg_engagement = (sum(p.like_count + p.comment_count for p in ig_image_posts) / len(ig_image_posts)) if ig_image_posts else 0

    # Content length analysis
    ig_caption_lengths = [len(p.caption or '') for p in ig_posts]
    tw_text_lengths = [len(t.text or '') for t in tw_tweets]

    context = {
        'username_display': username_clean,
        'ig_accounts': ig_accounts,
        'tw_accounts': tw_accounts,
        'ig_account_ids': [acc.id for acc in ig_accounts],
        'tw_account_ids': [acc.id for acc in tw_accounts],
        'has_ig_accounts': has_ig_accounts,
        'has_tw_accounts': has_tw_accounts,
        'ig_posts': ig_posts[:12],
        'tw_tweets': tw_tweets[:12],
        'ig_all_posts': ig_posts,  # All posts for modal display
        'tw_all_tweets': tw_tweets,  # All tweets for modal display
        'ig_posts_json': json.dumps({
            str(p.id): {
                'id': p.id,
                'username': p.account.username,
                'caption': p.caption or '',
                'image_url': p.image_url or '',
                'video_url': p.video_url or '',
                'is_video': p.is_video,
                'is_reel': p.is_reel,
                'is_carousel': p.is_carousel,
                'like_count': p.like_count,
                'comment_count': p.comment_count,
                'taken_at': p.taken_at.strftime('%b %d, %Y %I:%M %p') if p.taken_at else '',
                'instagram_url': p.instagram_url,
            }
            for p in ig_posts
        }),
        'tw_tweets_json': json.dumps({
            str(t.id): {
                'id': t.id,
                'username': t.account.username,
                'text': t.text or '',
                'media': t.media or [],
                'hashtags': t.hashtags or [],
                'mentions': t.mentions or [],
                'favorite_count': t.favorite_count,
                'retweet_count': t.retweet_count,
                'reply_count': t.reply_count,
                'view_count': t.view_count,
                'created_at': t.created_at.strftime('%b %d, %Y %I:%M %p') if t.created_at else '',
                'twitter_url': t.twitter_url,
            }
            for t in tw_tweets
        }),
        
        # Basic metrics
        'ig_total_posts': ig_total_posts,
        'ig_total_likes': ig_total_likes,
        'ig_total_comments': ig_total_comments,
        'tw_total_tweets': tw_total_tweets,
        'tw_total_faves': tw_total_faves,
        'tw_total_retweets': tw_total_retweets,
        'tw_total_replies': tw_total_replies,
        'tw_total_views': tw_total_views,
        
        # Phase 1: Enhanced metrics
        'ig_avg_likes': ig_avg_likes,
        'ig_avg_comments': ig_avg_comments,
        'ig_avg_engagement': ig_avg_engagement,
        'tw_avg_faves': tw_avg_faves,
        'tw_avg_retweets': tw_avg_retweets,
        'tw_avg_views': tw_avg_views,
        'tw_avg_engagement': tw_avg_engagement,
        
        # Phase 1.2: Content type distribution
        'ig_regular_posts': ig_regular_posts,
        'ig_reels': ig_reels,
        'ig_videos': ig_videos,
        'ig_carousels': ig_carousels,
        'tw_original': tw_original,
        'tw_retweets': tw_retweets,
        'tw_quotes': tw_quotes,
        
        # Phase 2: Time-based analytics
        'ig_weekday_counts': ig_weekday_counts,
        'tw_weekday_counts': tw_weekday_counts,
        'ig_weekday_engagement': ig_weekday_engagement,
        'tw_weekday_engagement': tw_weekday_engagement,
        'ig_hour_counts': ig_hour_counts,
        'tw_hour_counts': tw_hour_counts,
        'ig_hour_engagement': ig_hour_engagement,
        'tw_hour_engagement': tw_hour_engagement,
        'ig_month_counts': ig_month_counts,
        'tw_month_counts': tw_month_counts,
        'weekday_labels': ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'],
        'hour_labels': [f'{h:02d}:00' for h in range(24)],
        
        # Phase 3: Top performers
        'ig_top_by_likes': ig_top_by_likes,
        'ig_top_by_comments': ig_top_by_comments,
        'ig_top_by_engagement': ig_top_by_engagement,
        'tw_top_by_faves': tw_top_by_faves,
        'tw_top_by_retweets': tw_top_by_retweets,
        'tw_top_by_views': tw_top_by_views,
        'tw_top_by_engagement': tw_top_by_engagement,
        
        # Phase 5: Twitter analytics
        'top_hashtags': top_hashtags[:5],
        'top_hashtags_by_engagement': top_hashtags_by_engagement[:5],
        'top_mentions': top_mentions[:5],
        'top_mentions_by_engagement': top_mentions_by_engagement[:5],
        'lang_counter': dict(lang_counter),
        'lang_engagement': {lang: lang_engagement[lang]['total_engagement'] / lang_engagement[lang]['count'] 
                           if lang_engagement[lang]['count'] > 0 else 0 
                           for lang in lang_counter.keys()},
        
        # Phase 6: Instagram analytics
        'ig_reels_avg_likes': ig_reels_avg_likes,
        'ig_reels_avg_comments': ig_reels_avg_comments,
        'ig_reels_avg_engagement': ig_reels_avg_engagement,
        'ig_regular_avg_engagement': ig_regular_avg_engagement,
        'ig_video_avg_engagement': ig_video_avg_engagement,
        'ig_image_avg_engagement': ig_image_avg_engagement,
        'ig_carousel_avg_engagement': ig_carousel_avg_engagement,
        'ig_single_avg_engagement': ig_single_avg_engagement,
        'ig_reels_count': len(ig_reels_posts),
        'ig_regular_count': len(ig_regular_posts_list),
        'ig_video_count': len(ig_video_posts),
        'ig_image_count': len(ig_image_posts),
        'ig_carousel_count': len(ig_carousel_posts),
    }
    return render(request, 'core/social_user_analytics.html', context)


@login_required
def ideas_view(request):
    """
    Ideas page showing top 5 highest-ranked posts and tweets from recently fetched content.
    Uses the optimal ranking matrix to identify the best performing content from the last 7 days.
    """
    # Get top 5 ranked items from recently fetched posts and tweets (last 7 days)
    top_items = RankingService.get_top_ranked_combined(request.user, limit=5, days_recent=7)
    
    # Check which items already have video idea extractions and add to each item
    for item in top_items:
        source_type = item['type']  # 'instagram' or 'twitter'
        source_id = item['post'].id if source_type == 'instagram' else item['tweet'].id
        item['has_extraction'] = VideoIdeaExtraction.objects.filter(
            source_type=source_type,
            source_id=source_id
        ).exists()
    
    context = {
        'top_items': top_items,
    }
    
    return render(request, 'core/ideas.html', context)


@login_required
@require_http_methods(["POST"])
def extract_video_idea_view(request):
    """
    Extract video ideas from Instagram post or Twitter tweet using Gemini AI.
    
    Accepts POST request with:
    - source_type: 'instagram' or 'twitter'
    - source_id: ID of the post or tweet
    
    Returns JSON response with success/error status.
    """
    try:
        # Get request data
        source_type = request.POST.get('source_type')
        source_id = request.POST.get('source_id')
        
        if not source_type or not source_id:
            return JsonResponse({'error': 'Missing source_type or source_id'}, status=400)
        
        if source_type not in ['instagram', 'twitter']:
            return JsonResponse({'error': 'Invalid source_type. Must be "instagram" or "twitter"'}, status=400)
        
        try:
            source_id = int(source_id)
        except ValueError:
            return JsonResponse({'error': 'Invalid source_id. Must be an integer'}, status=400)
        
        # Get the post or tweet and verify ownership
        if source_type == 'instagram':
            post = get_object_or_404(InstagramPost, id=source_id, account__user=request.user)
            video_url = post.video_url
            caption = post.caption
        else:  # twitter
            tweet = get_object_or_404(TwitterTweet, id=source_id, account__user=request.user)
            # Find first video media item
            video_url = None
            for media_item in tweet.media or []:
                if media_item.get('type') == 'video' and media_item.get('video_url'):
                    video_url = media_item.get('video_url')
                    break
            caption = tweet.text
        
        # Check if video URL exists
        if not video_url:
            return JsonResponse({'error': 'No video URL found for this post/tweet'}, status=400)
        
        # Check if extraction already exists (prevent duplicates)
        if VideoIdeaExtraction.objects.filter(source_type=source_type, source_id=source_id).exists():
            return JsonResponse({'error': 'Video ideas have already been extracted for this post/tweet'}, status=400)
        
        # Download video bytes
        try:
            video_bytes = convert_video_url_to_mp4_bytes(video_url)
            if not video_bytes or len(video_bytes) == 0:
                return JsonResponse({'error': 'Downloaded video is empty. The video URL may be expired or invalid.'}, status=500)
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 403:
                logger.error(f"403 Forbidden error downloading video from Instagram CDN")
                return JsonResponse({
                    'error': 'Unable to download video: Instagram CDN returned 403 Forbidden. The video URL may be expired, require authentication, or be protected. Please try fetching the post again to get a fresh video URL.'
                }, status=500)
            else:
                logger.error(f"HTTP error downloading video: {str(e)}")
                return JsonResponse({'error': f'Failed to download video (HTTP {e.response.status_code}): {str(e)}'}, status=500)
        except requests.exceptions.RequestException as e:
            logger.error(f"Error downloading video: {str(e)}")
            error_msg = str(e)
            if '403' in error_msg or 'Forbidden' in error_msg:
                return JsonResponse({
                    'error': 'Unable to download video: The video URL is protected or expired. Instagram CDN URLs often expire. Please try fetching the post again to get a fresh video URL.'
                }, status=500)
            return JsonResponse({'error': f'Failed to download video: {error_msg}'}, status=500)
        except Exception as e:
            logger.error(f"Unexpected error downloading video: {str(e)}")
            return JsonResponse({'error': f'Failed to download video: {str(e)}'}, status=500)
        
        # Extract video ideas using Gemini
        try:
            result = extract_video_ideas(video_bytes, caption)
            
            # Check for errors in result
            if 'error' in result:
                return JsonResponse({'error': f"Gemini API error: {result.get('error')}"}, status=500)
            
        except Exception as e:
            logger.error(f"Error extracting video ideas: {str(e)}")
            return JsonResponse({'error': f'Failed to extract video ideas: {str(e)}'}, status=500)
        
        # Get ContentType for the source object
        if source_type == 'instagram':
            content_type = ContentType.objects.get_for_model(InstagramPost)
            content_object = post
        else:
            content_type = ContentType.objects.get_for_model(TwitterTweet)
            content_object = tweet
        
        # Save extraction to database
        extraction = VideoIdeaExtraction.objects.create(
            content_type=content_type,
            object_id=source_id,
            source_type=source_type,
            source_id=source_id,
            video_analysis=result.get('video_analysis', {}),
            video_ideas=result.get('video_ideas', []),
            best_idea=result.get('best_idea', {}),
            video_prompt=result.get('video_prompt', {}),
        )
        
        return JsonResponse({
            'success': True,
            'message': 'Video ideas extracted successfully',
            'extraction_id': extraction.id
        })
        
    except Exception as e:
        logger.error(f"Error in extract_video_idea_view: {str(e)}")
        return JsonResponse({'error': f'An error occurred: {str(e)}'}, status=500)


@login_required
def ai_ideas_view(request):
    """
    Display all extracted AI ideas from video extractions.
    Shows video_ideas array from all VideoIdeaExtraction records.
    """
    # Get all video idea extractions for the user
    # Filter by checking if the content_object belongs to user's accounts
    extractions = VideoIdeaExtraction.objects.filter(
        source_type='instagram',
        source_id__in=InstagramPost.objects.filter(account__user=request.user).values_list('id', flat=True)
    ) | VideoIdeaExtraction.objects.filter(
        source_type='twitter',
        source_id__in=TwitterTweet.objects.filter(account__user=request.user).values_list('id', flat=True)
    )
    
    # Order by most recent
    extractions = extractions.order_by('-extracted_at')
    
    # Build list of all ideas with source information
    all_ideas = []
    for extraction in extractions:
        # Get source object
        if extraction.source_type == 'instagram':
            try:
                source_post = InstagramPost.objects.get(id=extraction.source_id, account__user=request.user)
                source_name = f"@{source_post.account.username}"
                source_url = source_post.instagram_url
            except InstagramPost.DoesNotExist:
                continue
        else:  # twitter
            try:
                source_tweet = TwitterTweet.objects.get(id=extraction.source_id, account__user=request.user)
                source_name = f"@{source_tweet.account.username}"
                source_url = source_tweet.twitter_url
            except TwitterTweet.DoesNotExist:
                continue
        
        # Add each idea from video_ideas array
        video_ideas = extraction.video_ideas or []
        for idea in video_ideas:
            idea_id = idea.get('idea_id', '')
            # Check if prompt already exists for this idea
            has_prompt = IdeaVideoPrompt.objects.filter(
                extraction=extraction,
                idea_id=idea_id
            ).exists()
            
            all_ideas.append({
                'idea': idea,
                'extraction': extraction,
                'extraction_id': extraction.id,
                'idea_id': idea_id,
                'source_type': extraction.source_type,
                'source_name': source_name,
                'source_url': source_url,
                'extracted_at': extraction.extracted_at,
                'has_prompt': has_prompt,
            })
    
    context = {
        'all_ideas': all_ideas,
    }
    
    return render(request, 'core/ai_ideas.html', context)


@login_required
def video_prompts_view(request):
    """
    Display all extracted video prompts from video extractions and idea-generated prompts.
    Shows video_prompt objects from both VideoIdeaExtraction records and IdeaVideoPrompt records.
    """
    # Get all video idea extractions for the user
    extractions = VideoIdeaExtraction.objects.filter(
        source_type='instagram',
        source_id__in=InstagramPost.objects.filter(account__user=request.user).values_list('id', flat=True)
    ) | VideoIdeaExtraction.objects.filter(
        source_type='twitter',
        source_id__in=TwitterTweet.objects.filter(account__user=request.user).values_list('id', flat=True)
    )
    
    # Order by most recent
    extractions = extractions.order_by('-extracted_at')
    
    # Build list of prompts with source information
    all_prompts = []
    
    # Add prompts from VideoIdeaExtraction (original extraction prompts)
    for extraction in extractions:
        # Get source object
        if extraction.source_type == 'instagram':
            try:
                source_post = InstagramPost.objects.get(id=extraction.source_id, account__user=request.user)
                source_name = f"@{source_post.account.username}"
                source_url = source_post.instagram_url
                source_caption = source_post.caption[:100] if source_post.caption else "No caption"
            except InstagramPost.DoesNotExist:
                continue
        else:  # twitter
            try:
                source_tweet = TwitterTweet.objects.get(id=extraction.source_id, account__user=request.user)
                source_name = f"@{source_tweet.account.username}"
                source_url = source_tweet.twitter_url
                source_caption = source_tweet.text[:100] if source_tweet.text else "No text"
            except TwitterTweet.DoesNotExist:
                continue
        
        # Add video prompt from extraction
        video_prompt = extraction.video_prompt or {}
        if video_prompt:  # Only add if prompt exists
            all_prompts.append({
                'prompt': video_prompt,
                'extraction': extraction,
                'source_type': extraction.source_type,
                'source_name': source_name,
                'source_url': source_url,
                'source_caption': source_caption,
                'extracted_at': extraction.extracted_at,
                'best_idea': extraction.best_idea or {},
                'prompt_type': 'extraction',  # Mark as extraction prompt
            })
    
    # Add prompts from IdeaVideoPrompt (idea-generated prompts)
    idea_prompts = IdeaVideoPrompt.objects.filter(
        source_type='instagram',
        source_id__in=InstagramPost.objects.filter(account__user=request.user).values_list('id', flat=True)
    ) | IdeaVideoPrompt.objects.filter(
        source_type='twitter',
        source_id__in=TwitterTweet.objects.filter(account__user=request.user).values_list('id', flat=True)
    )
    
    idea_prompts = idea_prompts.order_by('-generated_at')
    
    for idea_prompt in idea_prompts:
        # Get source object
        if idea_prompt.source_type == 'instagram':
            try:
                source_post = InstagramPost.objects.get(id=idea_prompt.source_id, account__user=request.user)
                source_name = f"@{source_post.account.username}"
                source_url = source_post.instagram_url
                source_caption = source_post.caption[:100] if source_post.caption else "No caption"
            except InstagramPost.DoesNotExist:
                continue
        else:  # twitter
            try:
                source_tweet = TwitterTweet.objects.get(id=idea_prompt.source_id, account__user=request.user)
                source_name = f"@{source_tweet.account.username}"
                source_url = source_tweet.twitter_url
                source_caption = source_tweet.text[:100] if source_tweet.text else "No text"
            except TwitterTweet.DoesNotExist:
                continue
        
        # Add idea-generated prompt
        all_prompts.append({
            'prompt': idea_prompt.video_prompt or {},
            'extraction': idea_prompt.extraction,
            'source_type': idea_prompt.source_type,
            'source_name': source_name,
            'source_url': source_url,
            'source_caption': source_caption,
            'extracted_at': idea_prompt.generated_at,
            'best_idea': {'title': idea_prompt.idea_title},  # Use idea title as best idea
            'prompt_type': 'idea',  # Mark as idea-generated prompt
            'idea_title': idea_prompt.idea_title,
        })
    
    # Sort all prompts by date (most recent first)
    all_prompts.sort(key=lambda x: x['extracted_at'], reverse=True)
    
    context = {
        'all_prompts': all_prompts,
    }
    
    return render(request, 'core/video_prompts.html', context)


@login_required
@require_http_methods(["POST"])
def generate_idea_video_prompt_view(request):
    """
    Generate a video prompt for a specific AI idea using Together AI.
    
    Accepts POST request with:
    - extraction_id: ID of the VideoIdeaExtraction
    - idea_id: UUID of the specific idea from video_ideas array
    
    Returns JSON response with success/error status.
    """
    try:
        # Get request data
        extraction_id = request.POST.get('extraction_id')
        idea_id = request.POST.get('idea_id')
        
        if not extraction_id or not idea_id:
            return JsonResponse({'error': 'Missing extraction_id or idea_id'}, status=400)
        
        try:
            extraction_id = int(extraction_id)
        except ValueError:
            return JsonResponse({'error': 'Invalid extraction_id. Must be an integer'}, status=400)
        
        # Get the extraction and verify ownership
        extraction = get_object_or_404(VideoIdeaExtraction, id=extraction_id)
        
        # Verify user owns the source post/tweet
        if extraction.source_type == 'instagram':
            post = get_object_or_404(InstagramPost, id=extraction.source_id, account__user=request.user)
        else:  # twitter
            tweet = get_object_or_404(TwitterTweet, id=extraction.source_id, account__user=request.user)
        
        # Check if prompt already exists for this idea
        if IdeaVideoPrompt.objects.filter(extraction=extraction, idea_id=idea_id).exists():
            return JsonResponse({'error': 'Video prompt has already been generated for this idea'}, status=400)
        
        # Find the specific idea in video_ideas array
        video_ideas = extraction.video_ideas or []
        target_idea = None
        for idea in video_ideas:
            if idea.get('idea_id') == idea_id:
                target_idea = idea
                break
        
        if not target_idea:
            return JsonResponse({'error': 'Idea not found in extraction'}, status=404)
        
        # Generate video prompt using Together AI
        try:
            video_prompt = generate_video_prompt_from_idea(target_idea)
        except Exception as e:
            logger.error(f"Error generating video prompt: {str(e)}")
            return JsonResponse({'error': f'Failed to generate video prompt: {str(e)}'}, status=500)
        
        # Save to database
        idea_prompt = IdeaVideoPrompt.objects.create(
            extraction=extraction,
            idea_id=idea_id,
            idea_title=target_idea.get('title', 'Untitled Idea'),
            video_prompt=video_prompt,
            source_type=extraction.source_type,
            source_id=extraction.source_id,
        )
        
        return JsonResponse({
            'success': True,
            'message': 'Video prompt generated successfully',
            'prompt_id': idea_prompt.id
        })
        
    except Exception as e:
        logger.error(f"Error in generate_idea_video_prompt_view: {str(e)}")
        return JsonResponse({'error': f'An error occurred: {str(e)}'}, status=500)

