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
    
    # Analytics
    path('analytics/', views.analytics_view, name='analytics'),
    
    # Instagram
    path('instagram/add/', views.add_instagram_account_view, name='add_instagram'),
    path('instagram/delete/<int:account_id>/', views.delete_instagram_account_view, name='delete_instagram'),
    path('instagram/scrape/', views.scrape_instagram_view, name='scrape_instagram'),
    path('instagram/scrape-reels/', views.scrape_reels_view, name='scrape_reels'),
    path('instagram/post/<int:post_id>/', views.instagram_post_detail_view, name='instagram_post_detail'),
    path('instagram/analytics/<int:account_id>/', views.account_analytics_view, name='account_analytics'),
    path('instagram/reels-analytics/<int:account_id>/', views.account_reels_analytics_view, name='account_reels_analytics'),
    
    # Reddit
    path('reddit/', views.reddit_view, name='reddit'),
    path('reddit/add/', views.add_subreddit_view, name='add_subreddit'),
    path('reddit/delete/<int:subreddit_id>/', views.delete_subreddit_view, name='delete_subreddit'),
    path('reddit/scrape/', views.scrape_reddit_view, name='scrape_reddit'),
    path('reddit/extract-keywords/', views.extract_keywords_view, name='extract_keywords'),
    path('reddit/keywords/', views.reddit_keywords_view, name='reddit_keywords'),
]

