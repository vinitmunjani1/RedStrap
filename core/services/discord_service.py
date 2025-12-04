"""
Discord webhook service for sending notifications about new Instagram posts.
"""
import logging
import requests
from typing import List, Optional
from datetime import datetime, timezone
from django.conf import settings
from django.utils import timezone as django_timezone

logger = logging.getLogger(__name__)


def send_discord_webhook(webhook_url: str, username: str, posts: List) -> bool:
    """
    Send a Discord webhook notification about new Instagram posts.
    
    Args:
        webhook_url: Discord webhook URL
        username: Instagram username (without @)
        posts: List of InstagramPost model instances from the last 24 hours
    
    Returns:
        True if message was sent successfully, False otherwise
    """
    if not webhook_url or not webhook_url.strip():
        logger.warning("Discord webhook URL is not configured")
        return False
    
    if not posts:
        logger.debug(f"No posts to send for @{username}")
        return False
    
    try:
        # Filter posts to only include those from last 24 hours
        cutoff_time = django_timezone.now() - django_timezone.timedelta(hours=24)
        recent_posts = [post for post in posts if post.taken_at and post.taken_at >= cutoff_time]
        
        if not recent_posts:
            logger.debug(f"No posts from last 24 hours for @{username}")
            return False
        
        # Get first post image for thumbnail
        thumbnail_url = None
        for post in recent_posts:
            if post.image_url:
                thumbnail_url = post.image_url
                break
            elif post.video_url:
                thumbnail_url = post.video_url
                break
        
        # Build embed fields for posts
        fields = []
        post_count = len(recent_posts)
        
        # Add summary field
        fields.append({
            "name": "📊 Summary",
            "value": f"**{post_count}** new post{'s' if post_count > 1 else ''} in the last 24 hours",
            "inline": False
        })
        
        # Add up to 5 most recent posts (Discord embed limit is 25 fields, so we limit to 5 posts)
        for i, post in enumerate(recent_posts[:5]):
            post_type = "🎬 Reel" if post.is_reel else "📹 Video" if post.is_video else "📷 Post"
            if post.is_carousel:
                post_type += f" ({post.carousel_media_count} items)"
            
            # Truncate caption if too long (Discord field value limit is 1024 chars)
            caption_preview = post.caption[:200] + "..." if post.caption and len(post.caption) > 200 else (post.caption or "No caption")
            
            # Build post link (if we have post_code, construct Instagram URL)
            post_link = f"https://www.instagram.com/p/{post.post_code}/" if post.post_code else "N/A"
            
            # Engagement metrics
            engagement = f"❤️ {post.like_count or 0} | 💬 {post.comment_count or 0}"
            if post.is_reel or post.is_video:
                engagement += f" | ▶️ {post.play_count or 0}"
            
            field_value = f"{post_type}\n{caption_preview}\n{engagement}\n[View Post]({post_link})"
            
            fields.append({
                "name": f"Post {i + 1}",
                "value": field_value,
                "inline": False
            })
        
        # If there are more than 5 posts, add a note
        if post_count > 5:
            fields.append({
                "name": "ℹ️ Note",
                "value": f"Showing 5 most recent posts. {post_count - 5} more post{'s' if post_count - 5 > 1 else ''} available.",
                "inline": False
            })
        
        # Build embed payload
        embed = {
            "title": f"📱 New Instagram Posts from @{username}",
            "description": f"Found **{post_count}** new post{'s' if post_count > 1 else ''} in the last 24 hours",
            "color": 14943594,  # Instagram brand color (#E4405F)
            "fields": fields,
            "footer": {
                "text": "REDSTRAP"
            },
            "timestamp": django_timezone.now().isoformat()
        }
        
        # Add thumbnail if available
        if thumbnail_url:
            embed["thumbnail"] = {
                "url": thumbnail_url
            }
        
        # Build webhook payload
        payload = {
            "embeds": [embed]
        }
        
        # Send webhook request
        response = requests.post(
            webhook_url,
            json=payload,
            timeout=10  # 10 second timeout
        )
        
        if response.status_code == 204:  # Discord returns 204 on success
            logger.info(f"Successfully sent Discord notification for @{username} ({post_count} posts)")
            return True
        else:
            logger.error(f"Discord webhook returned status {response.status_code}: {response.text}")
            return False
            
    except requests.exceptions.Timeout:
        logger.error(f"Discord webhook request timed out for @{username}")
        return False
    except requests.exceptions.RequestException as e:
        logger.error(f"Error sending Discord webhook for @{username}: {e}", exc_info=True)
        return False
    except Exception as e:
        logger.error(f"Unexpected error sending Discord webhook for @{username}: {e}", exc_info=True)
        return False

