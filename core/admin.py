"""
Django admin configuration for core models.
"""
from django.contrib import admin
from .models import (
    InstagramAccount, InstagramPost, InstagramCarouselItem, InstagramKeyword,
    Subreddit, RedditPost, RedditKeyword
)


@admin.register(InstagramAccount)
class InstagramAccountAdmin(admin.ModelAdmin):
    list_display = ['username', 'user', 'created_at', 'last_scraped_at']
    list_filter = ['created_at', 'last_scraped_at']
    search_fields = ['username']
    readonly_fields = ['created_at']


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

