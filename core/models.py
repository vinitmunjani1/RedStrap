"""
Django models for Instagram and Reddit data.
"""
from django.db import models
from django.contrib.auth.models import User
from django.urls import reverse


class InstagramAccount(models.Model):
    """
    Represents an Instagram account to monitor.
    Each account belongs to a user and can have multiple posts.
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='instagram_accounts')
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
    
    class Meta:
        ordering = ['-taken_at']
        unique_together = [['account', 'post_id']]
    
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
    Stores post metadata including title, body, score, and keywords.
    """
    subreddit = models.ForeignKey(Subreddit, on_delete=models.CASCADE, related_name='posts')
    title = models.CharField(max_length=500)
    url = models.URLField(max_length=500, unique=True, help_text="Full Reddit post URL")
    score = models.IntegerField(default=0, help_text="Reddit upvote score")
    body = models.TextField(blank=True, help_text="Post body text")
    flair = models.CharField(max_length=100, blank=True, help_text="Reddit post flair")
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
    similarity = models.FloatField(help_text="Similarity score (0.0 to 1.0) indicating how well the keyword represents the post")
    extracted_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-similarity', 'keyword']
        indexes = [
            models.Index(fields=['-similarity']),
        ]
    
    def __str__(self):
        return f"{self.keyword} (similarity: {self.similarity:.2f})"

