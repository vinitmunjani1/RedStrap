"""
Optimal ranking matrix service for posts and tweets.
Calculates comprehensive scores based on multiple factors including engagement,
recency, media type, and keyword relevance.
"""
from datetime import datetime
from django.utils import timezone as django_timezone
from typing import List, Dict, Any
from core.models import InstagramPost, TwitterTweet


class RankingService:
    """
    Service for calculating optimal ranking scores for posts and tweets.
    Uses a weighted matrix approach considering multiple engagement and quality factors.
    """
    
    # Weight configuration for ranking factors
    # These can be adjusted based on performance analysis
    WEIGHTS = {
        'engagement': 0.40,      # 40% - Core engagement metrics
        'recency': 0.25,          # 25% - How recent the post is
        'media_type': 0.15,        # 15% - Video/reel content gets boost
        'engagement_rate': 0.10,   # 10% - Engagement relative to views/plays
        'keyword_relevance': 0.10, # 10% - Posts with keywords are more valuable
    }
    
    # Recency decay parameters
    RECENCY_HALF_LIFE_DAYS = 30  # Posts lose half their recency score after 30 days
    
    # Media type multipliers
    MEDIA_MULTIPLIERS = {
        'reel': 1.3,      # Reels get 30% boost
        'video': 1.2,      # Videos get 20% boost
        'carousel': 1.1,   # Carousels get 10% boost
        'image': 1.0,      # Regular images are baseline
        'text': 0.9,       # Text-only posts get slight penalty
    }
    
    @staticmethod
    def calculate_post_score(post: InstagramPost, now: datetime = None) -> float:
        """
        Calculate comprehensive ranking score for an Instagram post.
        
        Args:
            post: InstagramPost instance
            now: Current datetime (for testing, defaults to timezone.now())
        
        Returns:
            Float score (0-100 scale, higher is better)
        """
        if now is None:
            now = django_timezone.now()
        
        # Normalize all components to 0-100 scale, then apply weights
        engagement_score = RankingService._calculate_engagement_score_post(post)
        recency_score = RankingService._calculate_recency_score(post.taken_at, now)
        media_type_score = RankingService._calculate_media_type_score_post(post)
        engagement_rate_score = RankingService._calculate_engagement_rate_post(post)
        keyword_score = RankingService._calculate_keyword_score_post(post)
        
        # Weighted combination
        total_score = (
            engagement_score * RankingService.WEIGHTS['engagement'] +
            recency_score * RankingService.WEIGHTS['recency'] +
            media_type_score * RankingService.WEIGHTS['media_type'] +
            engagement_rate_score * RankingService.WEIGHTS['engagement_rate'] +
            keyword_score * RankingService.WEIGHTS['keyword_relevance']
        )
        
        return round(total_score, 2)
    
    @staticmethod
    def calculate_tweet_score(tweet: TwitterTweet, now: datetime = None) -> float:
        """
        Calculate comprehensive ranking score for a Twitter tweet.
        
        Args:
            tweet: TwitterTweet instance
            now: Current datetime (for testing, defaults to timezone.now())
        
        Returns:
            Float score (0-100 scale, higher is better)
        """
        if now is None:
            now = django_timezone.now()
        
        # Normalize all components to 0-100 scale, then apply weights
        engagement_score = RankingService._calculate_engagement_score_tweet(tweet)
        recency_score = RankingService._calculate_recency_score(tweet.created_at, now)
        media_type_score = RankingService._calculate_media_type_score_tweet(tweet)
        engagement_rate_score = RankingService._calculate_engagement_rate_tweet(tweet)
        keyword_score = RankingService._calculate_keyword_score_tweet(tweet)
        
        # Weighted combination
        total_score = (
            engagement_score * RankingService.WEIGHTS['engagement'] +
            recency_score * RankingService.WEIGHTS['recency'] +
            media_type_score * RankingService.WEIGHTS['media_type'] +
            engagement_rate_score * RankingService.WEIGHTS['engagement_rate'] +
            keyword_score * RankingService.WEIGHTS['keyword_relevance']
        )
        
        return round(total_score, 2)
    
    @staticmethod
    def _calculate_engagement_score_post(post: InstagramPost) -> float:
        """
        Calculate engagement score for Instagram post (0-100 scale).
        Considers likes, comments, and plays (for videos/reels).
        """
        # Base engagement: likes + comments
        base_engagement = (post.like_count or 0) + (post.comment_count or 0)
        
        # For videos/reels, add play count (weighted lower)
        if post.is_reel or post.is_video:
            play_engagement = (post.play_count or 0) * 0.01  # 100 plays = 1 engagement point
            base_engagement += play_engagement
        
        # Normalize using logarithmic scale to handle wide ranges
        # log(1 + engagement) / log(1 + max_expected) * 100
        # Using max_expected = 100,000 for normalization
        import math
        if base_engagement == 0:
            return 0.0
        
        normalized = (math.log10(1 + base_engagement) / math.log10(1 + 100000)) * 100
        return min(normalized, 100.0)  # Cap at 100
    
    @staticmethod
    def _calculate_engagement_score_tweet(tweet: TwitterTweet) -> float:
        """
        Calculate engagement score for Twitter tweet (0-100 scale).
        Considers favorites, retweets, replies, quotes, and views.
        """
        # Weighted engagement: favorites (1x), retweets (2x), replies (1.5x), quotes (2x)
        weighted_engagement = (
            (tweet.favorite_count or 0) * 1.0 +
            (tweet.retweet_count or 0) * 2.0 +
            (tweet.reply_count or 0) * 1.5 +
            (tweet.quote_count or 0) * 2.0
        )
        
        # Add view count (weighted very low)
        view_engagement = (tweet.view_count or 0) * 0.001  # 1000 views = 1 engagement point
        weighted_engagement += view_engagement
        
        # Normalize using logarithmic scale
        import math
        if weighted_engagement == 0:
            return 0.0
        
        normalized = (math.log10(1 + weighted_engagement) / math.log10(1 + 50000)) * 100
        return min(normalized, 100.0)  # Cap at 100
    
    @staticmethod
    def _calculate_recency_score(posted_at: datetime, now: datetime) -> float:
        """
        Calculate recency score based on exponential decay (0-100 scale).
        More recent posts get higher scores.
        """
        if posted_at.tzinfo is None:
            posted_at = django_timezone.make_aware(posted_at)
        if now.tzinfo is None:
            now = django_timezone.make_aware(now)
        
        delta = now - posted_at
        days_old = delta.total_seconds() / (24 * 3600)
        
        # Exponential decay: score = 100 * e^(-lambda * days)
        # lambda chosen so that score halves after RECENCY_HALF_LIFE_DAYS
        import math
        lambda_decay = math.log(2) / RankingService.RECENCY_HALF_LIFE_DAYS
        score = 100 * math.exp(-lambda_decay * days_old)
        
        return max(0.0, min(score, 100.0))
    
    @staticmethod
    def _calculate_media_type_score_post(post: InstagramPost) -> float:
        """
        Calculate media type score for Instagram post (0-100 scale).
        Videos and reels get higher scores.
        """
        if post.is_reel:
            multiplier = RankingService.MEDIA_MULTIPLIERS['reel']
        elif post.is_video:
            multiplier = RankingService.MEDIA_MULTIPLIERS['video']
        elif post.is_carousel:
            multiplier = RankingService.MEDIA_MULTIPLIERS['carousel']
        elif post.image_url:
            multiplier = RankingService.MEDIA_MULTIPLIERS['image']
        else:
            multiplier = RankingService.MEDIA_MULTIPLIERS['text']
        
        # Convert multiplier to 0-100 scale (1.0 = 50, 1.3 = 65, etc.)
        score = 50 * multiplier
        return min(score, 100.0)
    
    @staticmethod
    def _calculate_media_type_score_tweet(tweet: TwitterTweet) -> float:
        """
        Calculate media type score for Twitter tweet (0-100 scale).
        Tweets with media get higher scores.
        """
        has_media = tweet.media and len(tweet.media) > 0
        
        if has_media:
            # Check if it's video media
            media_types = [m.get('type', '') for m in tweet.media if isinstance(m, dict)]
            has_video = any('video' in mt.lower() for mt in media_types)
            
            if has_video:
                multiplier = RankingService.MEDIA_MULTIPLIERS['video']
            else:
                multiplier = RankingService.MEDIA_MULTIPLIERS['image']
        else:
            multiplier = RankingService.MEDIA_MULTIPLIERS['text']
        
        # Convert multiplier to 0-100 scale
        score = 50 * multiplier
        return min(score, 100.0)
    
    @staticmethod
    def _calculate_engagement_rate_post(post: InstagramPost) -> float:
        """
        Calculate engagement rate score (0-100 scale).
        Higher engagement relative to views/plays gets better score.
        """
        if post.is_reel or post.is_video:
            views = post.play_count or 0
            if views == 0:
                return 0.0
            
            engagement = (post.like_count or 0) + (post.comment_count or 0)
            rate = (engagement / views) * 100  # Percentage
            
            # Normalize: 10% engagement rate = 100 points
            score = min(rate * 10, 100.0)
            return score
        else:
            # For images, use likes as proxy for views
            likes = post.like_count or 0
            if likes == 0:
                return 0.0
            
            # Comments relative to likes
            comments = post.comment_count or 0
            rate = (comments / likes) * 100 if likes > 0 else 0
            
            # Normalize: 20% comment rate = 100 points
            score = min(rate * 5, 100.0)
            return score
    
    @staticmethod
    def _calculate_engagement_rate_tweet(tweet: TwitterTweet) -> float:
        """
        Calculate engagement rate score for tweet (0-100 scale).
        Higher engagement relative to views gets better score.
        """
        views = tweet.view_count or 0
        if views == 0:
            return 0.0
        
        engagement = (
            (tweet.favorite_count or 0) +
            (tweet.retweet_count or 0) +
            (tweet.reply_count or 0)
        )
        
        rate = (engagement / views) * 100  # Percentage
        
        # Normalize: 5% engagement rate = 100 points
        score = min(rate * 20, 100.0)
        return score
    
    @staticmethod
    def _calculate_keyword_score_post(post: InstagramPost) -> float:
        """
        Calculate keyword relevance score (0-100 scale).
        Posts with more keywords get higher scores.
        """
        keyword_count = post.keywords.count()
        
        # 0 keywords = 0 points, 5+ keywords = 100 points
        score = min(keyword_count * 20, 100.0)
        return score
    
    @staticmethod
    def _calculate_keyword_score_tweet(tweet: TwitterTweet) -> float:
        """
        Calculate keyword relevance score for tweet (0-100 scale).
        Tweets with more keywords get higher scores.
        """
        keyword_count = tweet.keywords.count()
        
        # 0 keywords = 0 points, 5+ keywords = 100 points
        score = min(keyword_count * 20, 100.0)
        return score
    
    @staticmethod
    def get_top_ranked_posts(user, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Get top ranked Instagram posts for a user.
        
        Args:
            user: User instance
            limit: Number of top posts to return
        
        Returns:
            List of dicts with 'post', 'score', and 'type'='instagram'
        """
        posts = InstagramPost.objects.filter(
            account__user=user
        ).select_related('account').prefetch_related('keywords')[:500]  # Limit for performance
        
        scored_posts = []
        for post in posts:
            score = RankingService.calculate_post_score(post)
            scored_posts.append({
                'post': post,
                'score': score,
                'type': 'instagram'
            })
        
        # Sort by score descending and return top N
        scored_posts.sort(key=lambda x: x['score'], reverse=True)
        return scored_posts[:limit]
    
    @staticmethod
    def get_top_ranked_tweets(user, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Get top ranked Twitter tweets for a user.
        
        Args:
            user: User instance
            limit: Number of top tweets to return
        
        Returns:
            List of dicts with 'tweet', 'score', and 'type'='twitter'
        """
        tweets = TwitterTweet.objects.filter(
            account__user=user
        ).select_related('account').prefetch_related('keywords')[:500]  # Limit for performance
        
        scored_tweets = []
        for tweet in tweets:
            score = RankingService.calculate_tweet_score(tweet)
            scored_tweets.append({
                'tweet': tweet,
                'score': score,
                'type': 'twitter'
            })
        
        # Sort by score descending and return top N
        scored_tweets.sort(key=lambda x: x['score'], reverse=True)
        return scored_tweets[:limit]
    
    @staticmethod
    def get_top_ranked_combined(user, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Get top ranked posts and tweets combined, sorted by score.
        
        Args:
            user: User instance
            limit: Total number of top items to return
        
        Returns:
            List of dicts with either 'post' or 'tweet', 'score', and 'type'
        """
        top_posts = RankingService.get_top_ranked_posts(user, limit=limit * 2)
        top_tweets = RankingService.get_top_ranked_tweets(user, limit=limit * 2)
        
        # Combine and sort by score
        combined = top_posts + top_tweets
        combined.sort(key=lambda x: x['score'], reverse=True)
        
        return combined[:limit]

