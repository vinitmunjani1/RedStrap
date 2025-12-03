"""
Instagram service for fetching posts from RapidAPI.
Handles API communication and data parsing for Instagram posts.
Supports multiple API keys with random selection and automatic retry with different keys.
Optimized with smart rate limiting and API key rotation for faster fetching while respecting rate limits.
"""
import requests
import logging
import time
import random
import json
import os
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from pathlib import Path
from django.conf import settings
from django.utils import timezone
from threading import Lock
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

# Directory for saving debug responses
DEBUG_RESPONSES_DIR = Path(__file__).parent.parent.parent / "debug_responses"

# Configuration constants for optimized fetching
# API limit: 2 calls per second per API key
# With 5 API keys, we can make 10 calls/sec total
MIN_DELAY_BETWEEN_REQUESTS = 0.1  # Minimum delay between requests (seconds) - reduced for speed
RATE_LIMIT_WINDOW = 10  # Rate limit window in seconds
MAX_REQUESTS_PER_WINDOW = 50  # Max requests per window per API key (2 calls/sec * 60 sec = 120)
CALLS_PER_SECOND_PER_KEY = 1  # API limit: 2 calls per second per key

# Global rate limiter for each API key
_rate_limiters: Dict[str, deque] = {}
_rate_limiter_lock = Lock()


def _get_rate_limiter(api_key: str) -> deque:
    """
    Get or create a rate limiter deque for a specific API key.
    Uses a sliding window approach to track API calls.
    """
    with _rate_limiter_lock:
        if api_key not in _rate_limiters:
            _rate_limiters[api_key] = deque()
        return _rate_limiters[api_key]


def _wait_for_rate_limit(api_key: str):
    """
    Wait if necessary to respect rate limits for the given API key.
    Uses a sliding window approach: tracks timestamps of recent API calls.
    """
    limiter = _get_rate_limiter(api_key)
    now = time.time()
    
    # Remove timestamps older than the rate limit window
    while limiter and limiter[0] < now - RATE_LIMIT_WINDOW:
        limiter.popleft()
    
    # If we've hit the limit, wait until the oldest call falls outside the window
    if len(limiter) >= MAX_REQUESTS_PER_WINDOW:
        wait_time = limiter[0] + RATE_LIMIT_WINDOW - now + 0.1  # Add small buffer
        if wait_time > 0:
            logger.debug(f"Rate limit reached for API key, waiting {wait_time:.2f} seconds")
            time.sleep(wait_time)
            # Clean up again after waiting
            now = time.time()
            while limiter and limiter[0] < now - RATE_LIMIT_WINDOW:
                limiter.popleft()
    
    # Ensure minimum delay between requests
    if limiter:
        time_since_last = now - limiter[-1] if limiter else 0
        if time_since_last < (1.0 / CALLS_PER_SECOND_PER_KEY):
            wait_time = (1.0 / CALLS_PER_SECOND_PER_KEY) - time_since_last
            time.sleep(wait_time)
            now = time.time()
    
    # Record this API call
    limiter.append(time.time())


def _save_response_to_file(response_data: Dict, endpoint_type: str, username: str = "", additional_info: str = ""):
    """
    Save API response JSON to a file for debugging purposes.
    Only saves if DEBUG_SAVE_RESPONSES is enabled in settings.
    
    Args:
        response_data: The JSON response data to save
        endpoint_type: Type of endpoint (e.g., 'reels', 'posts', 'post_detail')
        username: Instagram username (optional, for filename)
        additional_info: Additional info for filename (optional)
    """
    if not getattr(settings, 'DEBUG_SAVE_RESPONSES', False):
        return
    
    try:
        # Create debug_responses directory if it doesn't exist
        DEBUG_RESPONSES_DIR.mkdir(parents=True, exist_ok=True)
        
        # Create endpoint-specific subdirectory
        endpoint_dir = DEBUG_RESPONSES_DIR / endpoint_type
        endpoint_dir.mkdir(parents=True, exist_ok=True)
        
        # Generate filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]  # Include milliseconds
        safe_username = username.replace('@', '').replace('/', '_') if username else "unknown"
        safe_info = additional_info.replace('/', '_').replace('\\', '_') if additional_info else ""
        
        filename_parts = [endpoint_type, safe_username, safe_info, timestamp]
        filename = "_".join([p for p in filename_parts if p]) + ".json"
        filepath = endpoint_dir / filename
        
        # Save JSON response
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(response_data, f, indent=2, ensure_ascii=False)
        
        logger.debug(f"Saved API response to: {filepath}")
        
        # Cleanup old files to prevent disk space issues
        _cleanup_old_response_files(endpoint_dir, endpoint_type)
        
    except Exception as e:
        logger.warning(f"Failed to save debug response to file: {e}")


def _cleanup_old_response_files(directory: Path, endpoint_type: str):
    """
    Remove old response files, keeping only the most recent N files.
    Prevents disk space issues from accumulating too many debug files.
    
    Args:
        directory: Directory containing response files
        endpoint_type: Type of endpoint (for logging)
    """
    try:
        max_files = getattr(settings, 'DEBUG_MAX_RESPONSE_FILES', 50)
        
        # Get all JSON files in directory, sorted by modification time (newest first)
        json_files = sorted(
            directory.glob("*.json"),
            key=lambda f: f.stat().st_mtime,
            reverse=True
        )
        
        # Remove files beyond the limit
        if len(json_files) > max_files:
            files_to_remove = json_files[max_files:]
            for file_to_remove in files_to_remove:
                try:
                    file_to_remove.unlink()
                    logger.debug(f"Removed old debug response file: {file_to_remove}")
                except Exception as e:
                    logger.warning(f"Failed to remove old debug file {file_to_remove}: {e}")
            
            logger.info(f"Cleaned up {len(files_to_remove)} old {endpoint_type} response files, kept {max_files} most recent")
    
    except Exception as e:
        logger.warning(f"Failed to cleanup old response files: {e}")


def _get_random_api_key() -> str:
    """
    Get a random API key from the configured list.
    This helps distribute load across multiple keys.
    """
    api_keys = getattr(settings, 'RAPIDAPI_KEYS', [])
    if not api_keys:
        api_keys = [getattr(settings, 'RAPIDAPI_KEY', '')]
    if not api_keys or not api_keys[0]:
        raise ValueError("No RapidAPI keys configured in settings")
    return random.choice(api_keys)


def _make_api_request(url: str, payload: Dict, method: str = "POST", max_retries: int = 3) -> Optional[Dict]:
    """
    Make an API request with automatic retry using different API keys on failure.
    Handles rate limiting and API key rotation.
    Uses POST method with JSON payload as per RapidAPI Instagram API requirements.
    
    Args:
        url: The API endpoint URL
        payload: JSON payload for the request
        method: HTTP method (default: POST)
        max_retries: Maximum number of retry attempts with different keys
    
    Returns:
        JSON response as dict, or None if all retries failed
    """
    api_keys = getattr(settings, 'RAPIDAPI_KEYS', [])
    if not api_keys:
        api_keys = [getattr(settings, 'RAPIDAPI_KEY', '')]
    
    if not api_keys or not api_keys[0]:
        logger.error("No RapidAPI keys configured")
        return None
    
    # Try each API key until one works
    for attempt in range(max_retries):
        api_key = _get_random_api_key()
        _wait_for_rate_limit(api_key)
        
        headers = {
            "x-rapidapi-key": api_key,
            "x-rapidapi-host": getattr(settings, 'RAPIDAPI_HOST', 'instagram120.p.rapidapi.com'),
            "Content-Type": "application/json"
        }
        
        try:
            if method.upper() == "POST":
                response = requests.post(url, json=payload, headers=headers, timeout=30)
            else:
                response = requests.get(url, params=payload, headers=headers, timeout=30)
            
            # Handle 404 specifically - might mean user doesn't exist or endpoint changed
            if response.status_code == 404:
                logger.error(f"404 Not Found for URL: {url} with payload: {payload}. This might mean:")
                logger.error("  - The username doesn't exist")
                logger.error("  - The API endpoint has changed")
                logger.error(f"  - Response: {response.text[:200]}")
                # Don't retry on 404, it's unlikely to succeed
                return None
            
            # Handle 429 (Too Many Requests) with exponential backoff
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 10))  # Default to 60 seconds
                wait_time = min(retry_after, 120)  # Cap at 2 minutes
                logger.warning(f"Rate limit exceeded (429) for {url}. Waiting {wait_time} seconds before retry (attempt {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    time.sleep(wait_time)
                    continue  # Retry with same or different key
                else:
                    logger.error(f"Rate limit exceeded after {max_retries} attempts for URL: {url}")
                    return None
            
            response.raise_for_status()
            response_data = response.json()
            
            # Save response to file if debug mode is enabled
            if getattr(settings, 'DEBUG_SAVE_RESPONSES', False):
                # Determine endpoint type from URL
                endpoint_type = "unknown"
                if "/reels" in url:
                    endpoint_type = "reels"
                elif "/posts" in url:
                    endpoint_type = "posts"
                elif "/post" in url:
                    endpoint_type = "post_detail"
                
                # Extract username from payload if available
                username = payload.get("username", "") if isinstance(payload, dict) else ""
                
                # Save the response
                _save_response_to_file(response_data, endpoint_type, username)
            
            return response_data
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                logger.error(f"404 Not Found: {e.response.text[:200] if e.response else 'No response'}")
                return None
            elif e.response.status_code == 429:
                # Handle 429 in exception handler as well (in case raise_for_status wasn't called)
                retry_after = int(e.response.headers.get('Retry-After', 60))
                wait_time = min(retry_after, 120)
                logger.warning(f"Rate limit exceeded (429) for {url}. Waiting {wait_time} seconds (attempt {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    time.sleep(wait_time)
                    continue
                else:
                    logger.error(f"Rate limit exceeded after {max_retries} attempts for URL: {url}")
                    return None
            logger.warning(f"HTTP error {e.response.status_code} (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                # Exponential backoff for other errors
                wait_time = min(2 ** attempt, 10)  # 1s, 2s, 4s, max 10s
                time.sleep(wait_time)
            else:
                logger.error(f"All API key attempts failed for URL: {url} with payload: {payload}")
                return None
        except requests.exceptions.RequestException as e:
            logger.warning(f"API request failed with key (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                # Try a different key on next iteration
                time.sleep(1)
            else:
                logger.error(f"All API key attempts failed for URL: {url} with payload: {payload}")
                return None
    
    return None


def _fetch_video_url_by_shortcode(shortcode: str) -> Optional[Dict[str, str]]:
    """
    Fetch video URL and caption for a reel/post using the mediaByShortcode endpoint.
    This endpoint provides the actual video URL that can be used for playback and the caption.
    
    Args:
        shortcode: Instagram post/reel shortcode (e.g., "DPRcWdvgI4P")
    
    Returns:
        Dictionary with 'video_url' and 'caption' keys if found, None otherwise.
        Format: {'video_url': '...', 'caption': '...'} or None
    """
    if not shortcode:
        return None
    
    try:
        url = "https://instagram120.p.rapidapi.com/api/instagram/mediaByShortcode"
        payload = {
            "shortcode": shortcode
        }
        
        response_data = _make_api_request(url, payload, method="POST")
        
        if not response_data:
            logger.warning(f"Could not fetch data from mediaByShortcode for shortcode: {shortcode}")
            return None
        
        # Response is an array: [{"urls": [{"url": "..."}], "meta": {"title": "...", ...}, ...}]
        # Extract video URL from response[0].urls[0].url
        # Extract caption from response[0].meta.title
        if isinstance(response_data, list) and len(response_data) > 0:
            first_item = response_data[0]
            if isinstance(first_item, dict):
                result = {}
                
                # Extract video URL
                urls = first_item.get("urls", [])
                if isinstance(urls, list) and len(urls) > 0:
                    video_url = urls[0].get("url", "")
                    if video_url:
                        result['video_url'] = video_url
                
                # Extract caption from meta.title
                meta = first_item.get("meta", {})
                if isinstance(meta, dict):
                    caption = meta.get("title", "")
                    if caption:
                        result['caption'] = caption
                
                if result:
                    logger.info(f"Successfully fetched data from mediaByShortcode for shortcode {shortcode}: video_url={'Yes' if 'video_url' in result else 'No'}, caption={'Yes' if 'caption' in result else 'No'}")
                    return result
        
        logger.warning(f"No data found in mediaByShortcode response for shortcode: {shortcode}")
        return None
            
    except Exception as e:
        logger.error(f"Error fetching data from mediaByShortcode for shortcode {shortcode}: {e}", exc_info=True)
        return None


def _fetch_reel_play_count(post_code: str) -> Optional[int]:
    """
    Fetch play_count for a reel using the post code.
    Uses the post detail endpoint to get full reel information including play_count.
    
    Args:
        post_code: Instagram post/reel shortcode (e.g., "DCwpUE4xY3M")
    
    Returns:
        Play count as integer if found, None otherwise
    """
    if not post_code:
        return None
    
    try:
        # Try using the post detail endpoint
        url = "https://instagram120.p.rapidapi.com/api/instagram/post"
        payload = {
            "shortcode": post_code
        }
        
        response_data = _make_api_request(url, payload, method="POST")
        
        if not response_data:
            logger.warning(f"Could not fetch post details for play_count, code: {post_code}")
            return None
        
        # Parse the response to extract play_count
        # The response structure may vary, so check multiple locations
        play_count = None
        
        # Check in result.post or result directly
        result = response_data.get("result", response_data)
        if isinstance(result, dict):
            # Check for play_count directly
            if "play_count" in result:
                play_count = result["play_count"]
            
            # Check for view_count (some APIs use view_count for reels)
            if play_count is None and "view_count" in result:
                view_count = result["view_count"]
                if view_count is not None:
                    play_count = view_count
            
            # Check in nested media structure
            if play_count is None and "media" in result:
                media = result["media"]
                if isinstance(media, dict):
                    if "play_count" in media:
                        play_count = media["play_count"]
                    elif "view_count" in media and media["view_count"] is not None:
                        play_count = media["view_count"]
        
        if play_count is not None:
            play_count = int(play_count)
            logger.info(f"Successfully fetched play_count {play_count} for reel code {post_code}")
            return play_count
        else:
            logger.warning(f"No play_count found in post detail response for code: {post_code}")
            return None
            
    except Exception as e:
        logger.error(f"Error fetching play_count for reel code {post_code}: {e}", exc_info=True)
        return None


def _fetch_reel_video_url(post_code: str) -> Optional[str]:
    """
    Fetch video URL for a reel using the post code.
    Uses the post detail endpoint to get full video information.
    
    Args:
        post_code: Instagram post/reel shortcode (e.g., "DCwpUE4xY3M")
    
    Returns:
        Video URL string if found, None otherwise
    """
    if not post_code:
        return None
    
    try:
        # Try using the post detail endpoint
        url = "https://instagram120.p.rapidapi.com/api/instagram/post"
        payload = {
            "shortcode": post_code
        }
        
        response_data = _make_api_request(url, payload, method="POST")
        
        if not response_data:
            logger.warning(f"Could not fetch post details for code: {post_code}")
            return None
        
        # Parse the response to extract video URL
        # The response structure may vary, so check multiple locations
        video_url = None
        
        # Check in result.post or result directly
        result = response_data.get("result", response_data)
        if isinstance(result, dict):
            # Check for video_versions
            if "video_versions" in result and result["video_versions"]:
                if isinstance(result["video_versions"], list) and len(result["video_versions"]) > 0:
                    video_url = result["video_versions"][0].get("url")
            
            # Check for video_url directly
            if not video_url and "video_url" in result:
                video_url = result["video_url"]
            
            # Check in nested media structure
            if not video_url and "media" in result:
                media = result["media"]
                if isinstance(media, dict):
                    if "video_versions" in media and media["video_versions"]:
                        if isinstance(media["video_versions"], list) and len(media["video_versions"]) > 0:
                            video_url = media["video_versions"][0].get("url")
                    if not video_url and "video_url" in media:
                        video_url = media["video_url"]
        
        if video_url:
            logger.info(f"Successfully fetched video URL for reel code {post_code}")
            return video_url
        else:
            logger.warning(f"No video URL found in post detail response for code: {post_code}")
            return None
            
    except Exception as e:
        logger.error(f"Error fetching video URL for reel code {post_code}: {e}", exc_info=True)
        return None


def _extract_timestamp_from_post_id(post_id: str) -> Optional[datetime]:
    """
    Extract timestamp from Instagram post ID (snowflake ID).
    Instagram uses a custom epoch and bit-shifting algorithm.
    
    Args:
        post_id: Instagram post ID as string
    
    Returns:
        datetime object if extraction successful, None otherwise
    """
    try:
        # Convert post ID to integer
        post_id_int = int(post_id)
        
        # Instagram epoch: January 1, 2010 00:00:00 UTC
        instagram_epoch = 1262304000  # Unix timestamp for 2010-01-01 00:00:00 UTC
        
        # Instagram snowflake ID structure:
        # - Bits 0-41: timestamp (milliseconds since Instagram epoch)
        # - Bits 42-51: machine ID
        # - Bits 52-63: sequence number
        
        # Extract timestamp: right shift by 22 bits (removes machine ID and sequence)
        timestamp_ms = (post_id_int >> 22) + instagram_epoch * 1000
        
        # Convert milliseconds to seconds
        timestamp_s = timestamp_ms / 1000.0
        
        # Convert to datetime
        extracted_dt = datetime.fromtimestamp(timestamp_s, tz=timezone.utc)
        
        # Validate the extracted timestamp is reasonable
        # Instagram launched in 2010, so timestamps before that are invalid
        instagram_start = datetime(2010, 1, 1, tzinfo=timezone.utc)
        now = timezone.now()
        max_future_date = now + timedelta(days=1)  # Allow up to 1 day in future for edge cases
        
        if extracted_dt < instagram_start:
            logger.warning(f"Extracted timestamp {extracted_dt} from post ID {post_id} is before Instagram existed")
            return None
        
        if extracted_dt > max_future_date:
            logger.warning(f"Extracted timestamp {extracted_dt} from post ID {post_id} is too far in the future")
            return None
        
        return extracted_dt
        
    except (ValueError, OSError, OverflowError) as e:
        logger.warning(f"Error extracting timestamp from post ID {post_id}: {e}")
        return None


def parse_instagram_post(post_node: Dict, skip_video_url: bool = False) -> Optional[Dict]:
    """
    Parse a single Instagram post from API response.
    Handles both regular posts and reels, with comprehensive timestamp extraction.
    
    Args:
        post_node: Dictionary containing post/reel data from API response
        skip_video_url: If True, skip video URL extraction (for lazy loading). Default False.
    
    Args:
        post_node: The post node from the API response
    
    Returns:
        Dictionary with parsed post data, or None if parsing failed
    """
    try:
        # Handle nested media structure (for reels endpoint)
        # Some endpoints return node.media, others return post data directly in node
        # For reels endpoint: data is directly in node (no nested media structure)
        # For posts endpoint: might have nested media structure
        media_data = {}
        if "media" in post_node and isinstance(post_node.get("media"), dict):
            # Extract data from nested media object (posts endpoint structure)
            media_data = post_node.get("media", {})
            # Merge media data with node data (media_data takes precedence for overlapping fields)
            # This ensures play_count from media is available in actual_post_data
            actual_post_data = {**post_node, **media_data}
            
            # Debug: Log play_count extraction for reels
            if media_data.get("product_type") == "clips" or post_node.get("product_type") == "clips":
                logger.info(f"Reel parsing DEBUG: Found nested media structure")
                logger.info(f"Reel parsing DEBUG: media_data keys: {list(media_data.keys())[:20]}")
                logger.info(f"Reel parsing DEBUG: media_data.play_count = {media_data.get('play_count')}")
                logger.info(f"Reel parsing DEBUG: actual_post_data.play_count = {actual_post_data.get('play_count')}")
        else:
            # Use node directly (reels endpoint structure - data is directly in node, no nested media)
            actual_post_data = post_node
            # For reels endpoint, there's no nested media, so media_data stays empty
        
        # Extract post ID (use pk as primary identifier)
        post_id = actual_post_data.get("pk") or actual_post_data.get("id", "")
        if not post_id:
            return None
        
        # Extract caption text
        # For reels endpoint: caption is in node.caption.text (caption is a dict with 'text' field)
        # For posts endpoint: caption might be in node.caption.text or directly as node.caption
        # Check multiple locations to ensure we capture captions for both posts and reels
        caption = ""
        caption_obj = None
        
        # Priority 1: Check post_node.caption (top level of node) - this is where reels endpoint stores it
        if post_node.get("caption"):
            caption_obj = post_node.get("caption")
        # Priority 2: Check media_data.caption (for reels with nested structure)
        elif media_data and media_data.get("caption"):
            caption_obj = media_data.get("caption")
        # Priority 3: Check actual_post_data.caption (merged data)
        elif actual_post_data.get("caption"):
            caption_obj = actual_post_data.get("caption")
        
        # Extract text from caption object
        # Reels endpoint returns caption as: {"text": "...", "pk": "...", "created_at": ...}
        # Posts endpoint might return caption as string or dict
        if caption_obj:
            if isinstance(caption_obj, dict):
                # Caption is a dict - extract the 'text' field (this is the structure from reels endpoint)
                caption = caption_obj.get("text", "") or caption_obj.get("caption", "")
                # If still empty, try to get any string value from the dict
                if not caption:
                    for key in ["text", "caption", "content"]:
                        if key in caption_obj and isinstance(caption_obj[key], str):
                            caption = caption_obj[key]
                            break
            elif isinstance(caption_obj, str):
                # Sometimes caption is directly a string
                caption = caption_obj
        
        # Log caption extraction for reels to help debug
        is_reel_check = actual_post_data.get("product_type") == "clips" or post_node.get("product_type") == "clips"
        if is_reel_check:
            if caption:
                logger.info(f"Reel {post_id}: Successfully extracted caption (length: {len(caption)}, preview: {caption[:50]}...)")
            else:
                # Enhanced debugging for reels caption extraction
                caption_debug_info = {
                    "post_node_has_caption": post_node.get("caption") is not None,
                    "post_node_caption_type": type(post_node.get("caption")).__name__ if post_node.get("caption") is not None else None,
                    "media_data_has_caption": media_data.get("caption") is not None if media_data else False,
                    "actual_post_data_has_caption": actual_post_data.get("caption") is not None,
                }
                logger.warning(f"Reel {post_id}: No caption found. Debug info: {caption_debug_info}")
                # Log the actual caption structure if it exists
                if post_node.get("caption"):
                    logger.debug(f"Reel {post_id}: post_node.caption structure: {type(post_node.get('caption'))}, keys: {list(post_node.get('caption').keys()) if isinstance(post_node.get('caption'), dict) else 'N/A'}")
        
        # Extract timestamp (taken_at is Unix timestamp)
        # For reels endpoint, taken_at is ALWAYS directly in node.taken_at as an integer Unix timestamp
        # Check node first (this is where reels have it), then media, then merged data
        taken_at_timestamp = None
        caption_created_at_timestamp = None
        
        # Priority 1: Check node.taken_at directly (this is where reels have it)
        if post_node.get("taken_at") is not None:
            taken_at_timestamp = post_node.get("taken_at")
        # Priority 2: Check media object (for nested structures)
        elif media_data and media_data.get("taken_at") is not None:
            taken_at_timestamp = media_data.get("taken_at")
        # Priority 3: Check merged data
        elif actual_post_data.get("taken_at") is not None:
            taken_at_timestamp = actual_post_data.get("taken_at")
        # Priority 4: Check alternative field names
        elif actual_post_data.get("taken_at_timestamp") is not None:
            taken_at_timestamp = actual_post_data.get("taken_at_timestamp")
        elif post_node.get("taken_at_timestamp") is not None:
            taken_at_timestamp = post_node.get("taken_at_timestamp")
        elif media_data and media_data.get("taken_at_timestamp") is not None:
            taken_at_timestamp = media_data.get("taken_at_timestamp")
        
        # Also extract caption.created_at as a fallback option for reels
        # This is useful when taken_at is in the future but caption.created_at is in the past
        # Structure: edges -> node -> caption -> created_at
        # post_node is the "node" object, so we access caption directly from it
        caption_data = post_node.get("caption") or actual_post_data.get("caption")
        if caption_data and isinstance(caption_data, dict):
            caption_created_at = caption_data.get("created_at")
            if caption_created_at is not None:
                caption_created_at_timestamp = caption_created_at
                # Log caption.created_at extraction for debugging
                is_reel_check_temp = actual_post_data.get("product_type") == "clips" or post_node.get("product_type") == "clips"
                if is_reel_check_temp:
                    logger.info(f"Reel {post_id}: Found caption.created_at = {caption_created_at_timestamp}")
            else:
                # Log when caption exists but created_at is missing
                is_reel_check_temp = actual_post_data.get("product_type") == "clips" or post_node.get("product_type") == "clips"
                if is_reel_check_temp:
                    logger.debug(f"Reel {post_id}: Caption object exists but created_at is None. Caption keys: {list(caption_data.keys())}")
        else:
            # Log when caption is missing
            is_reel_check_temp = actual_post_data.get("product_type") == "clips" or post_node.get("product_type") == "clips"
            if is_reel_check_temp:
                logger.debug(f"Reel {post_id}: No caption object found. post_node has caption: {post_node.get('caption') is not None}, actual_post_data has caption: {actual_post_data.get('caption') is not None}")
        
        # Print timestamps for reels to help debug
        is_reel_check = actual_post_data.get("product_type") == "clips" or post_node.get("product_type") == "clips"
        if is_reel_check:
            print(f"\n=== REEL {post_id} TIMESTAMPS ===")
            print(f"taken_at (raw timestamp): {taken_at_timestamp}")
            if taken_at_timestamp is not None:
                try:
                    taken_at_dt = datetime.fromtimestamp(float(taken_at_timestamp), tz=timezone.utc)
                    print(f"taken_at (converted): {taken_at_dt}")
                except Exception as e:
                    print(f"taken_at conversion error: {e}")
            print(f"caption.created_at (raw timestamp): {caption_created_at_timestamp}")
            if caption_created_at_timestamp is not None:
                try:
                    caption_dt = datetime.fromtimestamp(float(caption_created_at_timestamp), tz=timezone.utc)
                    print(f"caption.created_at (converted): {caption_dt}")
                    if taken_at_timestamp is not None:
                        try:
                            taken_at_dt = datetime.fromtimestamp(float(taken_at_timestamp), tz=timezone.utc)
                            diff = caption_dt - taken_at_dt
                            print(f"Difference (caption - taken_at): {diff.total_seconds()} seconds")
                        except:
                            pass
                except Exception as e:
                    print(f"caption.created_at conversion error: {e}")
            print("=" * 40 + "\n")
        
        # Log for debugging reels timestamp extraction
        if is_reel_check:
            logger.info(
                f"Reel {post_id}: taken_at extraction - "
                f"node.taken_at={post_node.get('taken_at')}, "
                f"media.taken_at={media_data.get('taken_at') if media_data else 'N/A'}, "
                f"actual_post_data.taken_at={actual_post_data.get('taken_at')}, "
                f"final_timestamp={taken_at_timestamp}"
            )
        
        # Handle both integer timestamps and string timestamps
        if taken_at_timestamp is not None and taken_at_timestamp != 0:
            try:
                # If it's already a datetime object, use it directly
                if isinstance(taken_at_timestamp, datetime):
                    taken_at = taken_at_timestamp
                    if taken_at.tzinfo is None:
                        taken_at = timezone.make_aware(taken_at)
                # If it's a string, try to parse it
                elif isinstance(taken_at_timestamp, str):
                    # Try parsing as ISO format first
                    try:
                        taken_at = datetime.fromisoformat(taken_at_timestamp.replace('Z', '+00:00'))
                        if taken_at.tzinfo is None:
                            taken_at = timezone.make_aware(taken_at)
                    except:
                        # Try parsing as Unix timestamp string
                        taken_at = datetime.fromtimestamp(float(taken_at_timestamp), tz=timezone.utc)
                # If it's a number (int or float), treat as Unix timestamp in seconds
                else:
                    # Convert to float first to handle both int and float
                    timestamp_float = float(taken_at_timestamp)
                    now = timezone.now()
                    instagram_start = datetime(2010, 1, 1, tzinfo=timezone.utc)
                    
                    # Convert timestamp (in seconds) to datetime
                    try:
                        taken_at = datetime.fromtimestamp(timestamp_float, tz=timezone.utc)
                        
                        # Validate the timestamp is reasonable
                        # Allow timestamps up to 1 year in the future (for scheduled posts or timezone differences)
                        # Only reject if it's clearly invalid (before Instagram existed or way too far in future)
                        max_future_date = now + timedelta(days=365)
                        
                        if taken_at < instagram_start:
                            # Timestamp is before Instagram existed - extract from post ID
                            logger.warning(
                                f"Timestamp {taken_at_timestamp} ({taken_at}) is before Instagram existed for reel {post_id}. "
                                f"Extracting from post ID instead."
                            )
                            extracted = _extract_timestamp_from_post_id(post_id)
                            # Only use extracted if it's reasonable (not in future)
                            if extracted and extracted <= now + timedelta(days=1):
                                taken_at = extracted
                                if is_reel_check:
                                    logger.info(f"Used post ID extraction -> {taken_at} for reel {post_id}")
                            else:
                                # Post ID extraction also failed, use API timestamp anyway (better than current time)
                                logger.warning(f"Post ID extraction also failed for reel {post_id}, using API timestamp {taken_at}")
                                if is_reel_check:
                                    logger.info(f"Using API timestamp despite being before Instagram start: {taken_at}")
                        elif taken_at > max_future_date:
                            # Timestamp is way too far in the future - try caption.created_at first, then post ID extraction
                            logger.warning(
                                f"Timestamp {taken_at_timestamp} ({taken_at}) is too far in the future (>1 year) for reel {post_id}. "
                                f"Trying caption.created_at as fallback."
                            )
                            
                            # Try caption.created_at first (it's usually very close to taken_at and often in the past)
                            if caption_created_at_timestamp is not None:
                                try:
                                    caption_timestamp_float = float(caption_created_at_timestamp)
                                    caption_taken_at = datetime.fromtimestamp(caption_timestamp_float, tz=timezone.utc)
                                    
                                    # Use caption.created_at if it's in the past (not in future)
                                    if caption_taken_at <= now + timedelta(days=1):
                                        taken_at = caption_taken_at
                                        if is_reel_check:
                                            logger.info(f"Used caption.created_at ({caption_created_at_timestamp}) -> {taken_at} for reel {post_id}")
                                    else:
                                        # caption.created_at is also in future, try post ID extraction
                                        logger.warning(f"caption.created_at ({caption_taken_at}) is also in future, trying post ID extraction")
                                        extracted = _extract_timestamp_from_post_id(post_id)
                                        if extracted and extracted <= now + timedelta(days=1):
                                            taken_at = extracted
                                            if is_reel_check:
                                                logger.info(f"Used post ID extraction -> {taken_at} for reel {post_id}")
                                        else:
                                            # Post ID extraction also failed, use caption.created_at anyway (better than taken_at)
                                            taken_at = caption_taken_at
                                            logger.warning(f"Post ID extraction failed, using caption.created_at {taken_at} for reel {post_id}")
                                except (ValueError, OSError, OverflowError) as e:
                                    logger.warning(f"Error parsing caption.created_at {caption_created_at_timestamp}: {e}. Trying post ID extraction.")
                                    extracted = _extract_timestamp_from_post_id(post_id)
                                    if extracted and extracted <= now + timedelta(days=1):
                                        taken_at = extracted
                                        if is_reel_check:
                                            logger.info(f"Used post ID extraction -> {taken_at} for reel {post_id}")
                                    else:
                                        # Post ID extraction also failed, use API timestamp anyway
                                        logger.warning(f"Post ID extraction also failed for reel {post_id}, using API timestamp {taken_at}")
                                        if is_reel_check:
                                            logger.info(f"Using API timestamp despite being in future: {taken_at}")
                            else:
                                # No caption.created_at available, try post ID extraction
                                extracted = _extract_timestamp_from_post_id(post_id)
                                if extracted and extracted <= now + timedelta(days=1):
                                    taken_at = extracted
                                    if is_reel_check:
                                        logger.info(f"Used post ID extraction -> {taken_at} for reel {post_id}")
                                else:
                                    # Post ID extraction also failed, use API timestamp anyway
                                    logger.warning(f"Post ID extraction also failed for reel {post_id}, using API timestamp {taken_at}")
                                    if is_reel_check:
                                        logger.info(f"Using API timestamp despite being in future: {taken_at}")
                        else:
                            # Timestamp is within acceptable range (even if slightly in future, trust the API)
                            # But for reels, if taken_at is in the future and caption.created_at is in the past, prefer caption.created_at
                            if is_reel_check and taken_at > now and caption_created_at_timestamp is not None:
                                try:
                                    caption_timestamp_float = float(caption_created_at_timestamp)
                                    caption_taken_at = datetime.fromtimestamp(caption_timestamp_float, tz=timezone.utc)
                                    
                                    # If caption.created_at is in the past (not future), use it instead
                                    if caption_taken_at <= now:
                                        taken_at = caption_taken_at
                                        logger.info(f"Reel {post_id}: taken_at ({taken_at_timestamp}) was in future, using caption.created_at ({caption_created_at_timestamp}) -> {taken_at}")
                                    else:
                                        # Both are in future, use taken_at (original)
                                        if is_reel_check:
                                            logger.info(f"Successfully parsed timestamp {taken_at_timestamp} -> {taken_at} for reel {post_id}")
                                except (ValueError, OSError, OverflowError) as e:
                                    # Error parsing caption.created_at, use taken_at
                                    if is_reel_check:
                                        logger.warning(f"Error parsing caption.created_at for reel {post_id}: {e}. Using taken_at {taken_at}")
                            else:
                                # Timestamp is valid (even if slightly in future, trust the API)
                                if is_reel_check:
                                    logger.info(f"Successfully parsed timestamp {taken_at_timestamp} -> {taken_at} for reel {post_id}")
                    except (ValueError, OSError, OverflowError) as e:
                        logger.warning(f"Error converting timestamp {taken_at_timestamp} to datetime for reel {post_id}: {e}. Extracting from post ID.")
                        extracted = _extract_timestamp_from_post_id(post_id)
                        # Only use extracted if it's reasonable (not in future)
                        if extracted and extracted <= now + timedelta(days=1):
                            taken_at = extracted
                            if is_reel_check:
                                logger.info(f"Used post ID extraction -> {taken_at} for reel {post_id}")
                        else:
                            # Post ID extraction failed, use current time as last resort
                            taken_at = now
                            logger.error(f"Both API timestamp and post ID extraction failed for reel {post_id}, using current time")
            except (ValueError, TypeError, OSError) as e:
                logger.warning(f"Error parsing timestamp {taken_at_timestamp} for post {post_id}: {e}. Extracting from post ID.")
                # Fallback: Extract timestamp from Instagram post ID (snowflake ID)
                extracted = _extract_timestamp_from_post_id(post_id)
                if extracted:
                    taken_at = extracted
                    if is_reel_check:
                        logger.info(f"Used fallback timestamp extraction for reel {post_id}: {taken_at}")
                else:
                    # Post ID extraction failed, use current time
                    taken_at = timezone.now()
                    logger.warning(f"Post ID extraction failed for reel {post_id}, using current time {taken_at}")
                    if is_reel_check:
                        logger.warning(f"Using current time as fallback for reel {post_id}: {taken_at}")
        else:
            # If no timestamp found in API response, extract from Instagram post ID (snowflake ID)
            # This should rarely happen for reels as the API provides taken_at directly in the node
            is_reel_check = actual_post_data.get("product_type") == "clips" or post_node.get("product_type") == "clips"
            if is_reel_check:
                logger.warning(
                    f"No taken_at timestamp found in API response for reel {post_id}. "
                    f"Available keys in node: {list(post_node.keys())[:20]}. "
                    f"Extracting from post ID as fallback."
                )
            extracted = _extract_timestamp_from_post_id(post_id)
            
            if extracted:
                taken_at = extracted
                # Log the extracted timestamp for verification
                if is_reel_check:
                    logger.info(f"Extracted timestamp {taken_at} from post ID {post_id} for reel")
            else:
                # Post ID extraction failed, use current time as last resort
                taken_at = timezone.now()
                logger.warning(f"Post ID extraction failed for reel {post_id}, using current time {taken_at}")
                if is_reel_check:
                    logger.warning(f"Using current time as fallback for reel {post_id}: {taken_at}")
        
        # Determine if this is a reel (check before extracting play_count so we can log properly)
        # Check both in actual_post_data (merged) and post_node (original)
        is_reel = (
            actual_post_data.get("product_type") == "clips" or 
            post_node.get("product_type") == "clips" or
            (media_data and media_data.get("product_type") == "clips")
        )
        
        # Extract media URLs
        image_url = ""
        video_url = ""
        
        # Skip video URL extraction if skip_video_url is True (for lazy loading)
        # This reduces initial API calls - video URLs will be fetched on-demand when user views the post
        if not skip_video_url:
            # Check for video versions (reels and videos)
            # Priority 1: Check in post_node first (this is where reels endpoint stores it directly)
            if "video_versions" in post_node and post_node["video_versions"]:
                video_versions = post_node["video_versions"]
                if isinstance(video_versions, list) and len(video_versions) > 0:
                    video_url = video_versions[0].get("url", "")
                    if video_url and is_reel:
                        logger.debug(f"Reel {post_id}: Found video_url in post_node.video_versions")
            
            # Priority 2: Check in actual_post_data (merged data from node and media)
            if not video_url and "video_versions" in actual_post_data and actual_post_data["video_versions"]:
                video_versions = actual_post_data["video_versions"]
                if isinstance(video_versions, list) and len(video_versions) > 0:
                    video_url = video_versions[0].get("url", "")
                    if video_url and is_reel:
                        logger.debug(f"Reel {post_id}: Found video_url in actual_post_data.video_versions")
            
            # Priority 3: Check in media_data (for nested media structure)
            if not video_url and media_data and "video_versions" in media_data and media_data["video_versions"]:
                video_versions = media_data["video_versions"]
                if isinstance(video_versions, list) and len(video_versions) > 0:
                    video_url = video_versions[0].get("url", "")
                    if video_url and is_reel:
                        logger.debug(f"Reel {post_id}: Found video_url in media_data.video_versions")
            
            # Priority 4: For reels, check if there's a direct video_url field
            if not video_url and is_reel:
                if "video_url" in actual_post_data and actual_post_data["video_url"]:
                    video_url = actual_post_data["video_url"]
                elif "video_url" in post_node and post_node["video_url"]:
                    video_url = post_node["video_url"]
                elif media_data and "video_url" in media_data and media_data["video_url"]:
                    video_url = media_data["video_url"]
        
        # Check for image versions
        if "image_versions2" in actual_post_data:
            image_versions = actual_post_data["image_versions2"]
            if isinstance(image_versions, dict) and "candidates" in image_versions:
                candidates = image_versions["candidates"]
                if isinstance(candidates, list) and len(candidates) > 0:
                    image_url = candidates[0].get("url", "")
        
        # If no image URL found but there's a video, try to get thumbnail
        if not image_url and video_url and "image_versions2" in actual_post_data:
            image_versions = actual_post_data["image_versions2"]
            if isinstance(image_versions, dict) and "candidates" in image_versions:
                candidates = image_versions["candidates"]
                if isinstance(candidates, list) and len(candidates) > 0:
                    image_url = candidates[0].get("url", "")
        
        # Log video URL extraction for reels
        if is_reel:
            if video_url:
                logger.info(f"Reel {post_id}: Found video_url: {video_url[:50]}...")
            else:
                logger.warning(f"Reel {post_id}: No video_url found. Checked video_versions in actual_post_data, post_node, and media_data. Available keys in media_data: {list(media_data.keys())[:20] if media_data else 'N/A'}")
        
        # Extract engagement metrics - handle None values explicitly and convert to int
        def safe_int(value, default=0):
            """Safely convert value to int, handling None and invalid values."""
            if value is None:
                return default
            try:
                return int(value)
            except (ValueError, TypeError):
                return default
        
        like_count = safe_int(actual_post_data.get("like_count"), 0)
        comment_count = safe_int(actual_post_data.get("comment_count"), 0)
        
        # For reels, extract play_count from various possible locations
        # Reels endpoint structure: data is directly in node (no nested media)
        # Posts endpoint structure: might have nested media with play_count
        play_count_value = None
        
        # Priority 1: Check actual_post_data directly (for reels endpoint - data is directly in node)
        # This is the most common location for reels endpoint responses
        if "play_count" in actual_post_data:
            play_count_in_actual = actual_post_data.get("play_count")
            if play_count_in_actual is not None:
                play_count_value = play_count_in_actual
                logger.info(f"{'Reel' if is_reel else 'Post'} {post_id}: ✓ Found play_count in actual_post_data: {play_count_value}")
        
        # Priority 2: Check media_data if it was extracted (for posts endpoint with nested media)
        if play_count_value is None and media_data and "play_count" in media_data:
            play_count_in_media = media_data.get("play_count")
            if play_count_in_media is not None:
                play_count_value = play_count_in_media
                logger.info(f"{'Reel' if is_reel else 'Post'} {post_id}: ✓ Found play_count in media_data: {play_count_value}")
        
        # Priority 3: Check post_node.media directly (for posts endpoint structure)
        if play_count_value is None and isinstance(post_node.get("media"), dict):
            media_obj = post_node.get("media", {})
            if "play_count" in media_obj:
                play_count_in_node_media = media_obj.get("play_count")
                if play_count_in_node_media is not None:
                    play_count_value = play_count_in_node_media
                    logger.info(f"{'Reel' if is_reel else 'Post'} {post_id}: ✓ Found play_count in post_node.media: {play_count_value}")
        
        # Priority 4: Try other possible locations with alternative field names
        if play_count_value is None:
            check_locations = [
                ("actual_post_data.video_play_count", lambda: actual_post_data.get("video_play_count")),
                ("actual_post_data.reel_play_count", lambda: actual_post_data.get("reel_play_count")),
                ("post_node.play_count", lambda: post_node.get("play_count")),
                ("actual_post_data.clips_metadata.play_count", lambda: actual_post_data.get("clips_metadata", {}).get("play_count") if isinstance(actual_post_data.get("clips_metadata"), dict) else None),
            ]
            
            for location_name, getter_func in check_locations:
                try:
                    value = getter_func()
                    if value is not None:  # Accept 0 as valid (some reels might have 0 plays)
                        play_count_value = value
                        if is_reel:
                            logger.info(f"Reel {post_id}: Found play_count in {location_name}: {value}")
                        break
                except Exception as e:
                    logger.debug(f"Error checking {location_name} for reel {post_id}: {e}")
        
        # Priority 5: For reels, if play_count is not found, try view_count as fallback
        # Note: Some API responses may have view_count instead of play_count
        if is_reel and play_count_value is None:
            # Try view_count as fallback (it exists in the API response structure)
            view_count_fallback = (
                media_data.get("view_count") if media_data else None
            ) or actual_post_data.get("view_count") or post_node.get("view_count")
            if view_count_fallback is not None:
                play_count_value = view_count_fallback
                logger.info(f"Reel {post_id}: Using view_count as play_count: {play_count_value}")
        
        play_count = safe_int(play_count_value, 0)
        
        # Enhanced debug logging for reels with missing play_count
        if is_reel and play_count == 0:
            # Check all possible numeric fields that might be play_count
            all_numeric_fields = {}
            for key, value in actual_post_data.items():
                if isinstance(value, (int, float)) and ('play' in key.lower() or 'view' in key.lower() or ('count' in key.lower() and ('play' in key.lower() or 'view' in key.lower()))):
                    all_numeric_fields[key] = value
            
            logger.warning(
                f"Reel {post_id}: play_count is 0 after extraction. "
                f"Checked locations: actual_post_data.play_count={actual_post_data.get('play_count')}, "
                f"media_data.play_count={media_data.get('play_count') if media_data else 'N/A'}, "
                f"post_node.media.play_count={post_node.get('media', {}).get('play_count') if isinstance(post_node.get('media'), dict) else 'N/A'}, "
                f"view_count={actual_post_data.get('view_count')}. "
                f"All play/view related numeric fields: {all_numeric_fields if all_numeric_fields else 'None found'}"
            )
        
        # Debug logging for reels with missing play_count
        if is_reel and play_count == 0:
            # Check all numeric fields that might be play_count (focus on 'play' not 'view')
            all_numeric_fields = {}
            for key, value in actual_post_data.items():
                if isinstance(value, (int, float)) and ('play' in key.lower() or ('count' in key.lower() and 'play' in key.lower())):
                    all_numeric_fields[key] = value
            
            if media_data:
                for key, value in media_data.items():
                    if isinstance(value, (int, float)) and ('play' in key.lower() or ('count' in key.lower() and 'play' in key.lower())):
                        all_numeric_fields[f"media.{key}"] = value
            
            # Also check nested structures
            if isinstance(actual_post_data.get("clips_metadata"), dict):
                for key, value in actual_post_data["clips_metadata"].items():
                    if isinstance(value, (int, float)) and ('play' in key.lower() or ('count' in key.lower() and 'play' in key.lower())):
                        all_numeric_fields[f"clips_metadata.{key}"] = value
            
            logger.warning(
                f"Reel {post_id}: play_count is 0. "
                f"Available numeric fields with 'play': {all_numeric_fields if all_numeric_fields else 'None found'}. "
                f"Top actual_post_data keys: {list(actual_post_data.keys())[:30]}"
            )
        
        # Extract post code (shortcode)
        post_code = actual_post_data.get("code") or ""
        
        # Check if it's a carousel - handle None values explicitly
        carousel_media_count = safe_int(actual_post_data.get("carousel_media_count"), 0)
        is_carousel = bool(carousel_media_count > 1)  # Ensure boolean
        
        # Ensure all boolean fields are proper booleans (not dicts or other types)
        is_video_value = bool(video_url) or bool(is_reel)
        is_reel_value = bool(is_reel)
        is_carousel_value = bool(is_carousel)
        
        return {
            "post_id": str(post_id),
            "post_code": post_code,
            "caption": caption,
            "taken_at": taken_at,
            "image_url": image_url,
            "video_url": video_url,
            "is_video": is_video_value,
            "is_reel": is_reel_value,
            "is_carousel": is_carousel_value,
            "carousel_media_count": carousel_media_count,
            "like_count": like_count,
            "comment_count": comment_count,
            "play_count": play_count,
        }
    except Exception as e:
        logger.error(f"Error parsing Instagram post: {e}", exc_info=True)
        return None


def get_all_posts_for_username(username: str, max_age_hours: Optional[int] = None, save_callback: Optional[callable] = None) -> List[Dict]:
    """
    Fetch all posts for a given Instagram username.
    
    Args:
        username: Instagram username (without @)
        max_age_hours: Optional. If provided, only fetch posts from the last N hours.
                      If None, fetch all available posts.
        save_callback: Optional callback function that receives a list of post dictionaries
                      after each API call. Called as: save_callback(posts_batch)
                      This allows saving posts incrementally instead of waiting for all posts.
    
    Returns:
        List of parsed post dictionaries (all posts fetched, even if saved via callback)
    """
    from django.conf import settings
    
    # Clean username: remove @, trim whitespace, convert to lowercase
    username = str(username).strip().lstrip('@').lower()
    
    if not username:
        logger.error("Empty username provided")
        return []
    
    all_posts = []
    user_id = None
    end_cursor = None
    has_next_page = True
    
    # Get test mode limit from settings (default 24 posts for testing)
    test_mode_limit = getattr(settings, 'TEST_MODE_POSTS_LIMIT', 24)
    if test_mode_limit and test_mode_limit > 0:
        logger.info(f"Test mode enabled: Fetching only {test_mode_limit} recent posts for {username}")
    
    # Calculate cutoff time if max_age_hours is provided
    cutoff_time = None
    if max_age_hours is not None:
        cutoff_time = timezone.now() - timedelta(hours=max_age_hours)
        logger.info(f"Fetching posts from last {max_age_hours} hours (cutoff: {cutoff_time})")
    else:
        logger.info(f"Fetching all available posts for {username}")
    
    while has_next_page:
        url = "https://instagram120.p.rapidapi.com/api/instagram/posts"
        payload = {
            "username": username,
            "maxId": end_cursor if end_cursor else ""
        }
        
        response_data = _make_api_request(url, payload, method="POST")
        
        if not response_data:
            logger.error(f"Failed to fetch posts for {username}")
            break
        
        # Extract user ID from first response
        if not user_id and "result" in response_data:
            user_data = response_data.get("result", {})
            if isinstance(user_data, dict):
                user_id = user_data.get("id")
        
        # Extract posts from response - handle different response formats
        if "result" in response_data:
            result = response_data["result"]
            if isinstance(result, dict):
                # Check for edges (GraphQL-style response)
                edges = result.get("edges", [])
                if not edges:
                    # Try alternative format - direct posts array
                    edges = result.get("posts", [])
                    if edges:
                        # Convert to edge format for consistency
                        edges = [{"node": post} if not isinstance(post, dict) or "node" not in post else post for post in edges]
                
                if edges:
                    batch_posts = []  # Collect posts from this API response batch
                    reels_count = 0  # Track reels included in this batch
                    
                    for edge in edges:
                        # Check test mode limit before processing each post - stop immediately if limit reached
                        if test_mode_limit and test_mode_limit > 0 and len(all_posts) >= test_mode_limit:
                            logger.info(f"Reached test mode limit of {test_mode_limit} posts, stopping immediately")
                            has_next_page = False
                            break
                        
                        # Handle both edge format and direct node format
                        if isinstance(edge, dict):
                            node = edge.get("node", edge)  # Fallback to edge itself if no node key
                        else:
                            node = edge
                        
                        if not isinstance(node, dict):
                            continue
                            
                        parsed_post = parse_instagram_post(node)
                        if parsed_post:
                            # Include both posts and reels - don't skip reels
                            if parsed_post.get("is_reel"):
                                reels_count += 1
                                logger.debug(f"Including reel {parsed_post.get('post_id')} in posts")
                            
                            # Check test mode limit again after filtering reels - stop if we've reached the limit
                            if test_mode_limit and test_mode_limit > 0 and len(all_posts) >= test_mode_limit:
                                logger.info(f"Reached test mode limit of {test_mode_limit} posts, stopping pagination")
                                has_next_page = False
                                break
                            
                            # If max_age_hours is set, check if post is within time window
                            if cutoff_time is not None:
                                if parsed_post.get("taken_at") and parsed_post["taken_at"] < cutoff_time:
                                    # Post is too old, stop pagination (posts are returned newest first)
                                    logger.info(f"Reached posts older than {max_age_hours} hours, stopping pagination")
                                    has_next_page = False
                                    break
                            
                            all_posts.append(parsed_post)
                            batch_posts.append(parsed_post)  # Add to batch for callback
                    
                    # Log batch statistics
                    total_items = len(edges)
                    regular_posts_count = len(batch_posts) - reels_count
                    logger.info(f"API call returned {total_items} items: {regular_posts_count} posts, {reels_count} reels (total: {len(batch_posts)})")
                    
                    # Save posts from this batch immediately via callback (if provided)
                    # This allows incremental saving after each API call
                    if save_callback and batch_posts:
                        try:
                            save_callback(batch_posts)
                            logger.info(f"Saved {len(batch_posts)} posts to database from this API call")
                        except Exception as e:
                            logger.error(f"Error in save_callback for batch: {e}", exc_info=True)
                            # Continue processing even if callback fails
                    
                    # Check test mode limit after processing batch - stop pagination if limit reached
                    # This ensures we stop even if the inner break didn't trigger
                    if test_mode_limit and test_mode_limit > 0 and len(all_posts) >= test_mode_limit:
                        logger.info(f"Reached test mode limit of {test_mode_limit} posts after processing batch, stopping pagination")
                        has_next_page = False
                    
                    # Check for pagination - handle different pagination formats
                    page_info = result.get("page_info", {})
                    if isinstance(page_info, dict):
                        has_next_page = page_info.get("has_next_page", False)
                        end_cursor = page_info.get("end_cursor") or page_info.get("maxId")
                    else:
                        # Try alternative pagination fields
                        has_next_page = result.get("has_more", False)
                        end_cursor = result.get("next_max_id") or result.get("maxId")
                        if not has_next_page:
                            has_next_page = bool(end_cursor)
                else:
                    logger.warning(f"No posts found in response for {username}. Response keys: {list(result.keys())}")
                    has_next_page = False
            elif isinstance(result, list):
                # Handle direct list of posts
                batch_posts = []  # Collect posts from this API response batch
                reels_count = 0  # Track reels included in this batch
                
                for post_data in result:
                    # Check test mode limit before processing more posts
                    if test_mode_limit and test_mode_limit > 0 and len(all_posts) >= test_mode_limit:
                        logger.info(f"Reached test mode limit of {test_mode_limit} posts, stopping")
                        has_next_page = False
                        break
                    
                    parsed_post = parse_instagram_post(post_data)
                    if parsed_post:
                        # Include both posts and reels - don't skip reels
                        if parsed_post.get("is_reel"):
                            reels_count += 1
                            logger.debug(f"Including reel {parsed_post.get('post_id')} in posts")
                        
                        # Check test mode limit again after filtering reels
                        if test_mode_limit and test_mode_limit > 0 and len(all_posts) >= test_mode_limit:
                            logger.info(f"Reached test mode limit of {test_mode_limit} posts, stopping")
                            has_next_page = False
                            break
                        
                        all_posts.append(parsed_post)
                        batch_posts.append(parsed_post)  # Add to batch for callback
                
                # Log batch statistics
                total_items = len(result)
                regular_posts_count = len(batch_posts) - reels_count
                logger.info(f"API call returned {total_items} items: {regular_posts_count} posts, {reels_count} reels (total: {len(batch_posts)})")
                
                # Save posts from this batch immediately via callback (if provided)
                if save_callback and batch_posts:
                    try:
                        save_callback(batch_posts)
                        logger.info(f"Saved {len(batch_posts)} posts to database from this API call")
                    except Exception as e:
                        logger.error(f"Error in save_callback for batch: {e}", exc_info=True)
                        # Continue processing even if callback fails
                
                has_next_page = False
            else:
                logger.warning(f"Unexpected result format for {username}: {type(result)}")
                has_next_page = False
        else:
            logger.error(f"No 'result' key in API response for {username}. Response keys: {list(response_data.keys())}")
            has_next_page = False
        
        # Small delay between pagination requests
        # Skip delay if we've reached test mode limit (already have enough posts)
        if has_next_page and not (test_mode_limit and test_mode_limit > 0 and len(all_posts) >= test_mode_limit):
            time.sleep(0.2)
    
    logger.info(f"Fetched {len(all_posts)} posts for {username}")
    return all_posts


def _fetch_reels_from_reels_endpoint(username: str, max_age_hours: Optional[int] = None) -> Dict[str, int]:
    """
    Fetch reels from the reels endpoint to extract play_count data.
    This endpoint provides play_count but not video URLs.
    
    Args:
        username: Instagram username (without @)
        max_age_hours: Optional. If provided, only fetch reels from the last N hours.
    
    Returns:
        Dictionary mapping post_id -> play_count and post_code -> play_count
        Format: {'post_id_map': {post_id: play_count}, 'post_code_map': {post_code: play_count}}
    """
    username = str(username).strip().lstrip('@').lower()
    
    if not username:
        logger.error("Empty username provided for reels endpoint")
        return {'post_id_map': {}, 'post_code_map': {}}
    
    play_count_lookup = {'post_id_map': {}, 'post_code_map': {}}
    end_cursor = None
    has_next_page = True
    consecutive_429_errors = 0
    max_429_errors = 3  # Stop after 3 consecutive 429 errors
    
    # Calculate cutoff time if max_age_hours is provided
    cutoff_time = None
    if max_age_hours is not None:
        cutoff_time = timezone.now() - timedelta(hours=max_age_hours)
        logger.info(f"Fetching play_count data from reels endpoint (last {max_age_hours} hours)")
    else:
        logger.info(f"Fetching play_count data from reels endpoint for {username}")
    
    while has_next_page:
        url = "https://instagram120.p.rapidapi.com/api/instagram/reels"
        payload = {
            "username": username,
            "maxId": end_cursor if end_cursor else ""
        }
        
        response_data = _make_api_request(url, payload, method="POST")
        
        if not response_data:
            # Check if this was a 429 error (rate limit)
            # The _make_api_request logs 429 errors, so we can infer from consecutive failures
            consecutive_429_errors += 1
            if consecutive_429_errors >= max_429_errors:
                logger.error(f"Reels endpoint returned {max_429_errors} consecutive rate limit errors for {username}. Skipping reels endpoint and using fallback extraction.")
                break
            else:
                logger.warning(f"Reels endpoint request failed (attempt {consecutive_429_errors}/{max_429_errors}) for {username}, retrying with delay...")
                # Exponential backoff: wait longer after each 429 error
                wait_time = min(2 ** consecutive_429_errors * 5, 60)  # 5s, 10s, 20s, max 60s
                time.sleep(wait_time)
                continue
        
        # Reset consecutive error counter on success
        consecutive_429_errors = 0
        
        # Add delay between requests to avoid rate limits
        # Reels endpoint is separate from posts endpoint, so we need to be careful
        time.sleep(0.5)
        
        # Extract play_count from reels endpoint response
        if "result" in response_data:
            result = response_data["result"]
            if isinstance(result, dict):
                edges = result.get("edges", [])
                if not edges:
                    edges = result.get("reels", [])
                    if edges:
                        edges = [{"node": reel} if not isinstance(reel, dict) or "node" not in reel else reel for reel in edges]
                
                if edges:
                    for edge in edges:
                        if isinstance(edge, dict):
                            node = edge.get("node", edge)
                        else:
                            node = edge
                        
                        if not isinstance(node, dict):
                            continue
                        
                        # Check if this is a reel (product_type == "clips")
                        if node.get("product_type") != "clips":
                            continue
                        
                        # Extract post_id and post_code for matching
                        post_id = str(node.get("pk") or node.get("id", ""))
                        post_code = node.get("code", "")
                        
                        # Extract play_count from reels endpoint
                        # Check multiple locations as the structure may vary
                        play_count = None
                        
                        # Priority 1: Check node directly (most common location)
                        if "play_count" in node:
                            play_count = node.get("play_count")
                        elif "view_count" in node:
                            view_count = node.get("view_count")
                            if view_count is not None:
                                play_count = view_count
                        
                        # Priority 2: Check nested media if it exists
                        if play_count is None and "media" in node and isinstance(node["media"], dict):
                            media = node["media"]
                            if "play_count" in media:
                                play_count = media.get("play_count")
                            elif "view_count" in media:
                                view_count = media.get("view_count")
                                if view_count is not None:
                                    play_count = view_count
                        
                        # Priority 3: Check clips_metadata (some APIs store metrics here)
                        if play_count is None and "clips_metadata" in node and isinstance(node["clips_metadata"], dict):
                            clips_meta = node["clips_metadata"]
                            if "play_count" in clips_meta:
                                play_count = clips_meta.get("play_count")
                            elif "view_count" in clips_meta:
                                view_count = clips_meta.get("view_count")
                                if view_count is not None:
                                    play_count = view_count
                        
                        # Priority 4: Search for any field with 'play' or 'view' in name that has a numeric value
                        if play_count is None:
                            for key, value in node.items():
                                if isinstance(value, (int, float)) and value is not None:
                                    if 'play' in key.lower() and 'count' in key.lower():
                                        play_count = value
                                        logger.debug(f"Found play_count in field '{key}': {value}")
                                        break
                                    elif 'view' in key.lower() and 'count' in key.lower() and play_count is None:
                                        play_count = value
                                        logger.debug(f"Found view_count in field '{key}': {value}")
                        
                        # Store play_count if found
                        if play_count is not None:
                            try:
                                play_count_int = int(play_count)
                                if post_id:
                                    play_count_lookup['post_id_map'][post_id] = play_count_int
                                if post_code:
                                    play_count_lookup['post_code_map'][post_code] = play_count_int
                                logger.debug(f"Extracted play_count {play_count_int} for reel {post_id} ({post_code}) from reels endpoint")
                            except (ValueError, TypeError):
                                logger.warning(f"Invalid play_count value {play_count} for reel {post_id}")
                        
                        # Check time cutoff if specified
                        if cutoff_time is not None:
                            taken_at_timestamp = node.get("taken_at")
                            if taken_at_timestamp:
                                try:
                                    taken_at = datetime.fromtimestamp(taken_at_timestamp, tz=timezone.utc)
                                    if taken_at < cutoff_time:
                                        logger.info(f"Reached reels older than {max_age_hours} hours in reels endpoint, stopping")
                                        has_next_page = False
                                        break
                                except (ValueError, OSError, OverflowError):
                                    pass
                    
                    # Check for pagination
                    page_info = result.get("page_info", {})
                    if isinstance(page_info, dict):
                        has_next_page = page_info.get("has_next_page", False)
                        end_cursor = page_info.get("end_cursor") or page_info.get("maxId")
                    else:
                        has_next_page = result.get("has_more", False)
                        end_cursor = result.get("next_max_id") or result.get("maxId")
                        if not has_next_page:
                            has_next_page = bool(end_cursor)
                else:
                    logger.warning(f"No reels found in reels endpoint response for {username}")
                    has_next_page = False
            elif isinstance(result, list):
                # Handle direct list format
                for reel_data in result:
                    if isinstance(reel_data, dict) and reel_data.get("product_type") == "clips":
                        post_id = str(reel_data.get("pk") or reel_data.get("id", ""))
                        post_code = reel_data.get("code", "")
                        play_count = reel_data.get("play_count") or reel_data.get("view_count")
                        if play_count is not None and post_id:
                            try:
                                play_count_lookup['post_id_map'][post_id] = int(play_count)
                                if post_code:
                                    play_count_lookup['post_code_map'][post_code] = int(play_count)
                            except (ValueError, TypeError):
                                pass
                has_next_page = False
            else:
                logger.warning(f"Unexpected result format in reels endpoint for {username}: {type(result)}")
                has_next_page = False
        else:
            logger.error(f"No 'result' key in reels endpoint response for {username}")
            has_next_page = False
        
        if has_next_page:
            time.sleep(0.2)
    
    total_play_counts = len(play_count_lookup['post_id_map']) + len(play_count_lookup['post_code_map'])
    logger.info(f"Fetched play_count data for {total_play_counts} reels from reels endpoint for {username}")
    return play_count_lookup


def fetch_instagram_reels(username: str, max_age_hours: Optional[int] = None) -> List[Dict]:
    """
    Fetch all reels for a given Instagram username using ONLY the reels endpoint.
    Video URLs will be fetched lazily when user views the post detail page.
    
    Args:
        username: Instagram username (without @)
        max_age_hours: Optional. If provided, only fetch reels from the last N hours.
                      If None, fetch all available reels.
    
    Returns:
        List of parsed reel dictionaries with play_count (video_url fetched on-demand)
    """
    # Clean username: remove @, trim whitespace, convert to lowercase
    username = str(username).strip().lstrip('@').lower()
    
    if not username:
        logger.error("Empty username provided")
        return []
    
    all_reels = []
    end_cursor = None
    has_next_page = True
    
    # Check for test mode limit from settings
    from django.conf import settings
    test_mode_limit = getattr(settings, 'TEST_MODE_REELS_LIMIT', None)
    
    # Calculate cutoff time if max_age_hours is provided
    cutoff_time = None
    if max_age_hours is not None:
        cutoff_time = timezone.now() - timedelta(hours=max_age_hours)
        logger.info(f"Fetching reels from reels endpoint (last {max_age_hours} hours)")
    else:
        logger.info(f"Fetching all available reels from reels endpoint for {username}")
    
    # Log test mode status
    if test_mode_limit:
        logger.info(f"TEST MODE: Limiting reel fetch to {test_mode_limit} most recent reels")
    
    while has_next_page:
        # Use ONLY the reels endpoint - posts should be fetched separately
        url = "https://instagram120.p.rapidapi.com/api/instagram/reels"
        payload = {
            "username": username,
            "maxId": end_cursor if end_cursor else ""
        }
        
        response_data = _make_api_request(url, payload, method="POST")
        
        # Also try the reel detail endpoint to get view counts if available
        # Note: This might require individual API calls per reel, which could be rate-limited
        
        if not response_data:
            logger.error(f"Failed to fetch reels for {username}")
            break
        
        # Extract reels from response - handle different response formats
        if "result" in response_data:
            result = response_data["result"]
            if isinstance(result, dict):
                # Check for edges (GraphQL-style response)
                edges = result.get("edges", [])
                if not edges:
                    # Try alternative format
                    edges = result.get("reels", [])
                    if edges:
                        edges = [{"node": reel} if not isinstance(reel, dict) or "node" not in reel else reel for reel in edges]
                
                if edges:
                    for edge in edges:
                        # Handle both edge format and direct node format
                        if isinstance(edge, dict):
                            node = edge.get("node", edge)
                        else:
                            node = edge
                        
                        if not isinstance(node, dict):
                            continue
                        
                        # Parse the reel - skip video URL extraction for lazy loading
                        # Video URLs will be fetched on-demand when user views the post detail page
                        parsed_reel = parse_instagram_post(node, skip_video_url=True)
                        
                        # Only include reels (filter by product_type or is_reel flag)
                        if parsed_reel and parsed_reel.get("is_reel"):
                            # Ensure is_reel is set to True
                            parsed_reel["is_reel"] = True
                            
                            # Play count should be extracted from the reels endpoint response
                            # Skip fallback API calls in test mode for speed optimization
                            post_id = parsed_reel.get("post_id", "")
                            post_code = parsed_reel.get("post_code", "")
                            
                            # Fallback: If no play_count from reels endpoint, try post detail endpoint
                            # SKIP in test mode to avoid slow individual API calls
                            if not test_mode_limit and parsed_reel.get("play_count", 0) == 0 and post_code:
                                logger.debug(f"Reel {post_id} has no play_count from reels endpoint, trying post detail endpoint")
                                play_count = _fetch_reel_play_count(post_code)
                                if play_count is not None and play_count > 0:
                                    parsed_reel["play_count"] = play_count
                                    logger.info(f"Fetched play_count {play_count} from post detail endpoint for reel {post_id}")
                            
                            # Video URL will be fetched lazily when user views the post detail page
                            # This reduces initial API calls and improves performance
                            logger.debug(f"Reel {post_id}: play_count={parsed_reel.get('play_count')}, video_url=Lazy load, post_code={post_code}")
                            
                            # If max_age_hours is set, check if reel is within time window
                            if cutoff_time is not None:
                                if parsed_reel.get("taken_at") and parsed_reel["taken_at"] < cutoff_time:
                                    # Reel is too old, stop pagination (reels are returned newest first)
                                    logger.info(f"Reached reels older than {max_age_hours} hours, stopping pagination")
                                    has_next_page = False
                                    break
                            
                            all_reels.append(parsed_reel)
                            
                            # Check test mode limit: stop immediately after reaching limit
                            # This prevents processing remaining reels in the current response
                            if test_mode_limit and len(all_reels) >= test_mode_limit:
                                logger.info(f"TEST MODE: Reached limit of {test_mode_limit} reels, stopping immediately")
                                has_next_page = False
                                break
                    
                    # Check for pagination - handle different pagination formats
                    # Skip pagination check if we've reached test mode limit
                    if test_mode_limit and len(all_reels) >= test_mode_limit:
                        has_next_page = False
                    else:
                        page_info = result.get("page_info", {})
                        if isinstance(page_info, dict):
                            has_next_page = page_info.get("has_next_page", False)
                            end_cursor = page_info.get("end_cursor") or page_info.get("maxId")
                        else:
                            # Try alternative pagination fields
                            has_next_page = result.get("has_more", False)
                            end_cursor = result.get("next_max_id") or result.get("maxId")
                            if not has_next_page:
                                has_next_page = bool(end_cursor)
                else:
                    logger.warning(f"No reels found in response for {username}. Response keys: {list(result.keys())}")
                    has_next_page = False
            elif isinstance(result, list):
                # Handle direct list of reels
                for reel_data in result:
                    # Parse reel - skip video URL extraction for lazy loading
                    parsed_reel = parse_instagram_post(reel_data, skip_video_url=True)
                    if parsed_reel:
                        parsed_reel["is_reel"] = True
                        # If max_age_hours is set, check if reel is within time window
                        if cutoff_time is not None:
                            if parsed_reel.get("taken_at") and parsed_reel["taken_at"] < cutoff_time:
                                # Reel is too old, stop processing
                                logger.info(f"Reached reels older than {max_age_hours} hours, stopping")
                                has_next_page = False
                                break
                        all_reels.append(parsed_reel)
                        
                        # Check test mode limit: stop fetching if we've reached the limit
                        if test_mode_limit and len(all_reels) >= test_mode_limit:
                            logger.info(f"TEST MODE: Reached limit of {test_mode_limit} reels, stopping fetch")
                            has_next_page = False
                            break
                has_next_page = False
            else:
                logger.warning(f"Unexpected result format for reels {username}: {type(result)}")
                has_next_page = False
        else:
            logger.error(f"No 'result' key in API response for reels {username}. Response keys: {list(response_data.keys())}")
            has_next_page = False
        
        # Small delay between pagination requests
        # Skip delay if we've reached test mode limit (already have enough reels)
        if has_next_page and not (test_mode_limit and len(all_reels) >= test_mode_limit):
            time.sleep(0.2)
    
    # Apply test mode limit to final results if needed (safety check)
    if test_mode_limit and len(all_reels) > test_mode_limit:
        logger.info(f"TEST MODE: Truncating results from {len(all_reels)} to {test_mode_limit} reels")
        all_reels = all_reels[:test_mode_limit]
    
    # Log summary of results
    merged_count = sum(1 for reel in all_reels if reel.get("play_count", 0) > 0)
    video_count = sum(1 for reel in all_reels if reel.get("video_url"))
    logger.info(f"Fetched {len(all_reels)} reels for {username}: {merged_count} with play_count, {video_count} with video_url")
    
    return all_reels


def get_all_reels_for_username(username: str, max_age_hours: Optional[int] = None) -> List[Dict]:
    """
    Alias for fetch_instagram_reels for consistency with get_all_posts_for_username.
    
    Args:
        username: Instagram username (without @)
        max_age_hours: Optional. If provided, only fetch reels from the last N hours.
                      If None, fetch all available reels.
    
    Returns:
        List of parsed reel dictionaries
    """
    return fetch_instagram_reels(username, max_age_hours=max_age_hours)


def fetch_reels_for_accounts(accounts: List) -> Dict:
    """
    Fetch reels for multiple accounts concurrently using ThreadPoolExecutor.
    Uses all available API keys to maximize throughput (10 calls/sec total with 5 keys).
    
    Args:
        accounts: List of InstagramAccount model instances
    
    Returns:
        Dictionary mapping account IDs to lists of reel data
    """
    results = {}
    
    def fetch_reels_for_account(account):
        """Helper function to fetch reels for a single account."""
        try:
            reels = get_all_reels_for_username(account.username)
            return account.id, reels, None
        except Exception as e:
            logger.error(f"Error fetching reels for {account.username}: {e}", exc_info=True)
            return account.id, [], str(e)
    
    # Use ThreadPoolExecutor for concurrent fetching
    # With 5 API keys at 2 calls/sec each, we can handle 10 concurrent requests
    # Use a pool size that matches our total capacity
    max_workers = min(len(accounts), 10)  # Don't exceed 10 workers (our total API capacity)
    
    logger.info(f"Fetching reels for {len(accounts)} accounts using {max_workers} workers")
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all tasks
        future_to_account = {
            executor.submit(fetch_reels_for_account, account): account
            for account in accounts
        }
        
        # Process completed tasks
        for future in as_completed(future_to_account):
            account = future_to_account[future]
            try:
                account_id, reels, error = future.result()
                results[account_id] = {
                    'reels': reels,
                    'error': error,
                    'account': account
                }
                if error:
                    logger.error(f"Error fetching reels for account {account.username}: {error}")
                else:
                    logger.info(f"Successfully fetched {len(reels)} reels for {account.username}")
            except Exception as e:
                logger.error(f"Exception fetching reels for account {account.username}: {e}", exc_info=True)
                results[account.id] = {
                    'reels': [],
                    'error': str(e),
                    'account': account
                }
    
    return results

