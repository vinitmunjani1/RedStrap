"""
URL routing for core app.
"""
from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [
    # Authentication
    path('login/', auth_views.LoginView.as_view(template_name='core/login.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('register/', views.register_view, name='register'),
    
    # Dashboard
    path('', views.dashboard_view, name='dashboard'),
    path('posts/', views.posts_view, name='posts'),
    path('posts/load-more/', views.load_more_posts_view, name='load_more_posts'),
    path('posts/load-more-tweets/', views.load_more_tweets_view, name='load_more_tweets'),
    
    # Instagram
    path('instagram/', views.instagram_accounts_view, name='instagram_accounts'),
    path('instagram/add/', views.add_instagram_account_view, name='add_instagram'),
    path('instagram/add/<str:username>/', views.add_instagram_account_view, name='add_instagram_with_username'),
    path('instagram/delete/<int:account_id>/', views.delete_instagram_account_view, name='delete_instagram'),
    path('instagram/scrape/', views.scrape_instagram_view, name='scrape_instagram'),
    path('instagram/fetch/<int:account_id>/', views.fetch_single_account_posts_view, name='fetch_single_account_posts'),
    path('instagram/fetch-progress/<str:task_id>/', views.check_fetch_progress_view, name='check_fetch_progress'),
    path('instagram/post/<int:post_id>/', views.instagram_post_detail_view, name='instagram_post_detail'),
    path('instagram/analytics/<int:account_id>/', views.account_analytics_view, name='account_analytics'),
    path('instagram/extract-keywords/', views.extract_instagram_keywords_view, name='extract_instagram_keywords'),
    path('instagram/keywords/', views.instagram_keywords_view, name='instagram_keywords'),
    
    # Reddit
    path('reddit/', views.reddit_view, name='reddit'),
    path('reddit/add/', views.add_subreddit_view, name='add_subreddit'),
    path('reddit/delete/<int:subreddit_id>/', views.delete_subreddit_view, name='delete_subreddit'),
    path('reddit/scrape/', views.scrape_reddit_view, name='scrape_reddit'),
    path('reddit/extract-keywords/', views.extract_keywords_view, name='extract_keywords'),
    path('reddit/keywords/', views.reddit_keywords_view, name='reddit_keywords'),
    
    # Twitter
    path('twitter/', views.twitter_accounts_view, name='twitter_accounts'),
    path('twitter/add/', views.add_twitter_account_view, name='add_twitter'),
    path('twitter/add/<str:username>/', views.add_twitter_account_view, name='add_twitter_with_username'),
    path('twitter/delete/<int:account_id>/', views.delete_twitter_account_view, name='delete_twitter'),
    path('twitter/scrape/', views.scrape_twitter_view, name='scrape_twitter'),
    path('twitter/fetch/<int:account_id>/', views.fetch_single_twitter_account_tweets_view, name='fetch_single_twitter_account_tweets'),
    path('twitter/tweets/', views.twitter_tweets_view, name='twitter_tweets'),
    path('twitter/account/<int:account_id>/tweets/', views.twitter_account_tweets_view, name='twitter_account_tweets'),
    path('twitter/extract-keywords/', views.extract_twitter_keywords_view, name='extract_twitter_keywords'),
    
    # Unified social dashboard
    path('social/', views.social_dashboard_view, name='social_dashboard'),
    path('social/delete/<str:username>/', views.delete_social_username_view, name='delete_social_username'),
    path('social/analytics/<str:username>/', views.social_user_analytics_view, name='social_user_analytics'),
    
    # Ideas
    path('ideas/', views.ideas_view, name='ideas'),
]

