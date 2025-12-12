"""
Django models for Instagram and Reddit data.
"""
from django.db import models
from django.contrib.auth.models import User
from django.urls import reverse


class SocialUsername(models.Model):
    """
    Represents a unified username that can have both Instagram and Twitter accounts.
    This allows linking accounts from different platforms under the same username.
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='social_usernames')
    username = models.CharField(max_length=255, db_index=True, help_text="Unified username (case-insensitive)")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
        unique_together = [['username', 'user']]
        indexes = [
            models.Index(fields=['user', 'username']),
        ]
    
    def __str__(self):
        return f"@{self.username} (user: {self.user.username})"
    
    @property
    def has_instagram(self):
        return self.instagram_accounts.exists()
    
    @property
    def has_twitter(self):
        return self.twitter_accounts.exists()
    
    @property
    def instagram_count(self):
        return self.instagram_accounts.count()
    
    @property
    def twitter_count(self):
        return self.twitter_accounts.count()


class InstagramAccount(models.Model):
    """
    Represents an Instagram account to monitor.
    Each account belongs to a user and can have multiple posts.
    Linked to a SocialUsername for unified management.
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='instagram_accounts')
    social_username = models.ForeignKey('SocialUsername', on_delete=models.CASCADE, related_name='instagram_accounts', null=True, blank=True, help_text="Linked unified username")
    username = models.CharField(max_length=255, help_text="Instagram username without @")
    created_at = models.DateTimeField(auto_now_add=True)
    last_scraped_at = models.DateTimeField(null=True, blank=True, help_text="Last time posts were fetched for this account")
    
    class Meta:
        ordering = ['-created_at']
        unique_together = [['username', 'user']]
    
    def __str__(self):
        return f"{self.username} (user: {self.user.username})"


class InstagramPost(models.Model):
    """
    Represents a single Instagram post.
    Stores post metadata including caption, media URLs, engagement metrics, and timestamps.
    """
    account = models.ForeignKey(InstagramAccount, on_delete=models.CASCADE, related_name='posts')
    post_id = models.CharField(max_length=255, db_index=True, help_text="Unique Instagram post ID")
    post_code = models.CharField(max_length=255, blank=True, help_text="Instagram post shortcode for URL")
    caption = models.TextField(blank=True, help_text="Post caption/description")
    taken_at = models.DateTimeField(help_text="When the post was originally created on Instagram")
    image_url = models.URLField(max_length=500, blank=True, help_text="URL to the post image")
    video_url = models.URLField(max_length=500, blank=True, help_text="URL to the post video if it's a video")
    is_video = models.BooleanField(default=False, help_text="Whether this post is a video")
    is_reel = models.BooleanField(default=False, help_text="Whether this post is a reel (Instagram Reels)")
    is_carousel = models.BooleanField(default=False, help_text="Whether this post is a carousel with multiple media")
    carousel_media_count = models.IntegerField(default=0, help_text="Number of media items in carousel")
    like_count = models.IntegerField(default=0, help_text="Number of likes on the post")
    comment_count = models.IntegerField(default=0, help_text="Number of comments on the post")
    play_count = models.IntegerField(default=0, help_text="Number of plays/views on the post (for reels/videos)")
    created_at = models.DateTimeField(auto_now_add=True, help_text="When this post was added to our database")
    keywords_extracted = models.BooleanField(default=False, help_text="Whether keywords have been extracted from this post")
    
    class Meta:
        ordering = ['-taken_at']
        unique_together = [['account', 'post_id']]
        indexes = [
            models.Index(fields=['keywords_extracted', '-taken_at']),
        ]
    
    def __str__(self):
        return f"Post {self.post_id} by {self.account.username}"
    
    @property
    def instagram_url(self):
        """Generate the Instagram URL for this post."""
        if self.is_reel:
            return f"https://www.instagram.com/reel/{self.post_code}/"
        elif self.post_code:
            return f"https://www.instagram.com/p/{self.post_code}/"
        else:
            return f"https://www.instagram.com/p/{self.post_id}/"


class InstagramCarouselItem(models.Model):
    """
    Represents a single item in an Instagram carousel post.
    Carousel posts contain multiple images or videos.
    """
    post = models.ForeignKey(InstagramPost, on_delete=models.CASCADE, related_name='carousel_items')
    item_index = models.IntegerField(help_text="Index of this item in the carousel (0-based)")
    image_url = models.URLField(max_length=500, blank=True, help_text="URL to the image if this is an image")
    video_url = models.URLField(max_length=500, blank=True, help_text="URL to the video if this is a video")
    is_video = models.BooleanField(default=False, help_text="Whether this carousel item is a video")
    
    class Meta:
        ordering = ['item_index']
        unique_together = [['post', 'item_index']]
    
    def __str__(self):
        return f"Carousel item {self.item_index} of post {self.post.post_id}"


class InstagramKeyword(models.Model):
    """
    Represents a keyword extracted from an Instagram post caption.
    Keywords are extracted using Together AI or semantic similarity analysis.
    """
    post = models.ForeignKey(InstagramPost, on_delete=models.CASCADE, related_name='keywords')
    keyword = models.CharField(max_length=255, help_text="Extracted keyword/phrase")
    similarity = models.FloatField(null=True, blank=True, help_text="Similarity score (0.0 to 1.0) indicating how well the keyword represents the post. Null for Together AI keywords.")
    extracted_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-similarity', 'keyword']
        indexes = [
            models.Index(fields=['-similarity']),
        ]
    
    def __str__(self):
        if self.similarity is not None:
            return f"{self.keyword} (similarity: {self.similarity:.2f})"
        return f"{self.keyword}"


class Subreddit(models.Model):
    """
    Represents a subreddit to monitor.
    Each subreddit belongs to a user and can have multiple posts.
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='subreddits')
    name = models.CharField(max_length=255, help_text="Subreddit name without r/ prefix")
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
        unique_together = [['name', 'user']]
    
    def __str__(self):
        return f"r/{self.name} (user: {self.user.username})"
    
    @property
    def reddit_url(self):
        """Generate the Reddit URL for this subreddit."""
        return f"https://old.reddit.com/r/{self.name}"


class RedditPost(models.Model):
    """
    Represents a single Reddit post.
    Stores post metadata including title, body, score, media, and keywords.
    """
    subreddit = models.ForeignKey(Subreddit, on_delete=models.CASCADE, related_name='posts')
    title = models.CharField(max_length=500)
    url = models.URLField(max_length=500, unique=True, help_text="Full Reddit post URL")
    score = models.IntegerField(default=0, help_text="Reddit upvote score")
    body = models.TextField(blank=True, help_text="Post body text")
    flair = models.CharField(max_length=100, blank=True, help_text="Reddit post flair")
    thumbnail_url = models.URLField(max_length=500, blank=True, help_text="Thumbnail image URL")
    media_url = models.URLField(max_length=500, blank=True, help_text="Media URL (image or video)")
    is_video = models.BooleanField(default=False, help_text="Whether this post contains a video")
    post_type = models.CharField(max_length=50, blank=True, help_text="Post type: self, link, image, video, gallery")
    scraped_at = models.DateTimeField(auto_now_add=True, help_text="When this post was scraped")
    keywords_extracted = models.BooleanField(default=False, help_text="Whether keywords have been extracted from this post")
    
    class Meta:
        ordering = ['-scraped_at']
        indexes = [
            models.Index(fields=['keywords_extracted', '-scraped_at']),
        ]
    
    def __str__(self):
        return f"{self.title[:50]}... (r/{self.subreddit.name})"


class RedditKeyword(models.Model):
    """
    Represents a keyword extracted from a Reddit post.
    Keywords are extracted using semantic similarity analysis.
    """
    post = models.ForeignKey(RedditPost, on_delete=models.CASCADE, related_name='keywords')
    keyword = models.CharField(max_length=255, help_text="Extracted keyword/phrase")
    similarity = models.FloatField(null=True, blank=True, help_text="Similarity score (0.0 to 1.0) indicating how well the keyword represents the post")
    extracted_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-similarity', 'keyword']
        indexes = [
            models.Index(fields=['-similarity']),
        ]
    
    def __str__(self):
        if self.similarity is not None:
            return f"{self.keyword} (similarity: {self.similarity:.2f})"
        return f"{self.keyword}"


class TwitterAccount(models.Model):
    """
    Represents a Twitter account to monitor.
    Each account belongs to a user and can have multiple tweets.
    Linked to a SocialUsername for unified management.
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='twitter_accounts')
    social_username = models.ForeignKey('SocialUsername', on_delete=models.CASCADE, related_name='twitter_accounts', null=True, blank=True, help_text="Linked unified username")
    username = models.CharField(max_length=255, help_text="Twitter username without @")
    rest_id = models.CharField(max_length=255, blank=True, help_text="Twitter user rest_id (used for API calls)")
    name = models.CharField(max_length=255, blank=True, help_text="Twitter display name")
    description = models.TextField(blank=True, help_text="Twitter profile description")
    followers_count = models.IntegerField(default=0, help_text="Number of followers")
    following_count = models.IntegerField(default=0, help_text="Number of accounts following")
    tweet_count = models.IntegerField(default=0, help_text="Total number of tweets")
    verified = models.BooleanField(default=False, help_text="Whether the account is verified")
    profile_image_url = models.URLField(max_length=500, blank=True, help_text="Profile image URL")
    profile_banner_url = models.URLField(max_length=500, blank=True, help_text="Profile banner URL")
    created_at = models.DateTimeField(auto_now_add=True)
    last_scraped_at = models.DateTimeField(null=True, blank=True, help_text="Last time tweets were fetched for this account")
    
    class Meta:
        ordering = ['-created_at']
        unique_together = [['username', 'user']]
    
    def __str__(self):
        return f"@{self.username} (user: {self.user.username})"
    
    @property
    def twitter_url(self):
        """Generate the Twitter URL for this account."""
        return f"https://twitter.com/{self.username}"


class TwitterTweet(models.Model):
    """
    Represents a single Twitter tweet.
    Stores tweet metadata including text, media URLs, engagement metrics, and timestamps.
    """
    account = models.ForeignKey(TwitterAccount, on_delete=models.CASCADE, related_name='tweets')
    tweet_id = models.CharField(max_length=255, db_index=True, help_text="Unique Twitter tweet ID")
    text = models.TextField(help_text="Tweet text content")
    created_at = models.DateTimeField(help_text="When the tweet was originally created on Twitter")
    favorite_count = models.IntegerField(default=0, help_text="Number of favorites/likes on the tweet")
    retweet_count = models.IntegerField(default=0, help_text="Number of retweets")
    reply_count = models.IntegerField(default=0, help_text="Number of replies")
    quote_count = models.IntegerField(default=0, help_text="Number of quote tweets")
    view_count = models.IntegerField(default=0, help_text="Number of views on the tweet")
    media = models.JSONField(default=list, blank=True, help_text="List of media attachments (photos, videos, etc.)")
    hashtags = models.JSONField(default=list, blank=True, help_text="List of hashtags in the tweet")
    mentions = models.JSONField(default=list, blank=True, help_text="List of mentioned usernames")
    urls = models.JSONField(default=list, blank=True, help_text="List of URLs in the tweet")
    is_retweet = models.BooleanField(default=False, help_text="Whether this is a retweet")
    is_quote = models.BooleanField(default=False, help_text="Whether this is a quote tweet")
    lang = models.CharField(max_length=10, blank=True, help_text="Tweet language code")
    created_at_db = models.DateTimeField(auto_now_add=True, help_text="When this tweet was added to our database")
    keywords_extracted = models.BooleanField(default=False, help_text="Whether keywords have been extracted from this tweet")
    
    @property
    def twitter_url(self):
        """Generate the Twitter URL for this tweet."""
        return f"https://twitter.com/{self.account.username}/status/{self.tweet_id}"
    
    class Meta:
        ordering = ['-created_at']
        unique_together = [['account', 'tweet_id']]


class TwitterKeyword(models.Model):
    """
    Represents a keyword extracted from a Twitter tweet.
    Keywords are extracted using Together AI.
    """
    post = models.ForeignKey(TwitterTweet, on_delete=models.CASCADE, related_name='keywords')
    keyword = models.CharField(max_length=255, help_text="Extracted keyword/phrase")
    similarity = models.FloatField(null=True, blank=True, help_text="Similarity score (0.0 to 1.0) indicating how well the keyword represents the tweet. Null for Together AI keywords.")
    extracted_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-extracted_at', 'keyword']
        indexes = [
            models.Index(fields=['-extracted_at']),
        ]
    
    def __str__(self):
        if self.similarity is not None:
            return f"{self.keyword} (similarity: {self.similarity:.2f})"
        return f"{self.keyword}"

