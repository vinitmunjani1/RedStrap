"""
Django admin configuration for core models.
"""
from django.contrib import admin
from .models import (
    InstagramAccount, InstagramPost, InstagramCarouselItem, InstagramKeyword,
    Subreddit, RedditPost, RedditKeyword,
    TwitterAccount, TwitterTweet, TwitterKeyword, SocialUsername
)


@admin.register(InstagramAccount)
class InstagramAccountAdmin(admin.ModelAdmin):
    list_display = ['username', 'user', 'social_username', 'created_at', 'last_scraped_at']
    list_filter = ['created_at', 'last_scraped_at', 'social_username']
    search_fields = ['username', 'social_username__username']
    readonly_fields = ['created_at']
    raw_id_fields = ['user', 'social_username']


@admin.register(InstagramPost)
class InstagramPostAdmin(admin.ModelAdmin):
    list_display = ['post_id', 'account', 'taken_at', 'is_reel', 'is_video', 'like_count', 'play_count', 'keywords_extracted']
    list_filter = ['is_reel', 'is_video', 'is_carousel', 'keywords_extracted', 'taken_at', 'created_at']
    search_fields = ['post_id', 'caption', 'account__username']
    readonly_fields = ['created_at']
    date_hierarchy = 'taken_at'


@admin.register(InstagramKeyword)
class InstagramKeywordAdmin(admin.ModelAdmin):
    list_display = ['keyword', 'post', 'similarity', 'extracted_at']
    list_filter = ['extracted_at', 'similarity']
    search_fields = ['keyword', 'post__caption', 'post__post_id']
    readonly_fields = ['extracted_at']


@admin.register(InstagramCarouselItem)
class InstagramCarouselItemAdmin(admin.ModelAdmin):
    list_display = ['post', 'item_index', 'is_video']
    list_filter = ['is_video']
    search_fields = ['post__post_id']


@admin.register(Subreddit)
class SubredditAdmin(admin.ModelAdmin):
    list_display = ['name', 'user', 'created_at']
    list_filter = ['created_at']
    search_fields = ['name']
    readonly_fields = ['created_at']


@admin.register(RedditPost)
class RedditPostAdmin(admin.ModelAdmin):
    list_display = ['title', 'subreddit', 'score', 'scraped_at', 'keywords_extracted']
    list_filter = ['keywords_extracted', 'scraped_at', 'subreddit']
    search_fields = ['title', 'body', 'url']
    readonly_fields = ['scraped_at']
    date_hierarchy = 'scraped_at'


@admin.register(RedditKeyword)
class RedditKeywordAdmin(admin.ModelAdmin):
    list_display = ['keyword', 'post', 'similarity', 'extracted_at']
    list_filter = ['extracted_at', 'similarity']
    search_fields = ['keyword', 'post__title']
    readonly_fields = ['extracted_at']


@admin.register(SocialUsername)
class SocialUsernameAdmin(admin.ModelAdmin):
    list_display = ['username', 'user', 'instagram_count', 'twitter_count', 'created_at']
    list_filter = ['created_at']
    search_fields = ['username']
    readonly_fields = ['created_at', 'updated_at']
    date_hierarchy = 'created_at'
    raw_id_fields = ['user']
    inlines = []


@admin.register(TwitterAccount)
class TwitterAccountAdmin(admin.ModelAdmin):
    list_display = ['username', 'user', 'social_username', 'name', 'followers_count', 'verified', 'created_at', 'last_scraped_at']
    list_filter = ['verified', 'created_at', 'last_scraped_at', 'social_username']
    search_fields = ['username', 'name', 'description', 'social_username__username']
    readonly_fields = ['created_at', 'rest_id']
    date_hierarchy = 'created_at'
    raw_id_fields = ['user', 'social_username']


@admin.register(TwitterTweet)
class TwitterTweetAdmin(admin.ModelAdmin):
    list_display = ['tweet_id', 'account', 'created_at', 'favorite_count', 'retweet_count', 'view_count', 'is_retweet', 'is_quote', 'keywords_extracted']
    list_filter = ['is_retweet', 'is_quote', 'keywords_extracted', 'created_at', 'created_at_db']
    search_fields = ['tweet_id', 'text', 'account__username']
    readonly_fields = ['created_at_db', 'tweet_id']
    date_hierarchy = 'created_at'
    raw_id_fields = ['account']
    list_per_page = 50


@admin.register(TwitterKeyword)
class TwitterKeywordAdmin(admin.ModelAdmin):
    list_display = ['keyword', 'post', 'similarity', 'extracted_at']
    list_filter = ['extracted_at', 'similarity']
    search_fields = ['keyword', 'post__text', 'post__tweet_id']
    readonly_fields = ['extracted_at']

