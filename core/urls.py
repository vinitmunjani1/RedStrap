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
    
    # Instagram
    path('instagram/', views.instagram_accounts_view, name='instagram_accounts'),
    path('instagram/add/', views.add_instagram_account_view, name='add_instagram'),
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
]

