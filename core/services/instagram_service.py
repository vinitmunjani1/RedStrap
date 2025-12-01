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
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from django.conf import settings
from django.utils import timezone
from threading import Lock
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

# Configuration constants for optimized fetching
# API limit: 2 calls per second per API key
# With 5 API keys, we can make 10 calls/sec total
MIN_DELAY_BETWEEN_REQUESTS = 0.1  # Minimum delay between requests (seconds) - reduced for speed
RATE_LIMIT_WINDOW = 60  # Rate limit window in seconds
MAX_REQUESTS_PER_WINDOW = 120  # Max requests per window per API key (2 calls/sec * 60 sec = 120)
CALLS_PER_SECOND_PER_KEY = 2  # API limit: 2 calls per second per key

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
            
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                logger.error(f"404 Not Found: {e.response.text[:200] if e.response else 'No response'}")
                return None
            logger.warning(f"HTTP error {e.response.status_code} (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(1)
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


def parse_instagram_post(post_node: Dict) -> Optional[Dict]:
    """
    Parse a single Instagram post from API response.
    Handles both regular posts and reels, with comprehensive timestamp extraction.
    
    Args:
        post_node: The post node from the API response
    
    Returns:
        Dictionary with parsed post data, or None if parsing failed
    """
    try:
        # Handle nested media structure (for reels endpoint)
        # Some endpoints return node.media, others return post data directly in node
        # For reels, taken_at can be in node, node.media, or both
        media_data = {}
        if "media" in post_node and isinstance(post_node.get("media"), dict):
            # Extract data from nested media object
            media_data = post_node.get("media", {})
            # Merge media data with node data (node takes precedence for overlapping fields)
            actual_post_data = {**media_data, **post_node}
        else:
            # Use node directly (standard posts endpoint structure)
            actual_post_data = post_node
        
        # Extract post ID (use pk as primary identifier)
        post_id = actual_post_data.get("pk") or actual_post_data.get("id", "")
        if not post_id:
            return None
        
        # Extract caption text
        caption_obj = actual_post_data.get("caption", {})
        caption = caption_obj.get("text", "") if isinstance(caption_obj, dict) else ""
        
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
        
        # Determine if this is a reel
        is_reel = actual_post_data.get("product_type") == "clips" or post_node.get("product_type") == "clips"
        
        # Extract media URLs
        image_url = ""
        video_url = ""
        
        # Check for video versions (reels and videos)
        if "video_versions" in actual_post_data and actual_post_data["video_versions"]:
            video_versions = actual_post_data["video_versions"]
            if isinstance(video_versions, list) and len(video_versions) > 0:
                video_url = video_versions[0].get("url", "")
        
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
        play_count = safe_int(actual_post_data.get("play_count") or actual_post_data.get("view_count"), 0)
        
        # Extract post code (shortcode)
        post_code = actual_post_data.get("code") or ""
        
        # Check if it's a carousel - handle None values explicitly
        carousel_media_count = safe_int(actual_post_data.get("carousel_media_count"), 0)
        is_carousel = carousel_media_count > 1
        
        return {
            "post_id": str(post_id),
            "post_code": post_code,
            "caption": caption,
            "taken_at": taken_at,
            "image_url": image_url,
            "video_url": video_url,
            "is_video": bool(video_url) or is_reel,
            "is_reel": is_reel,
            "is_carousel": is_carousel,
            "carousel_media_count": carousel_media_count,
            "like_count": like_count,
            "comment_count": comment_count,
            "play_count": play_count,
        }
    except Exception as e:
        logger.error(f"Error parsing Instagram post: {e}", exc_info=True)
        return None


def get_all_posts_for_username(username: str, max_age_hours: Optional[int] = None) -> List[Dict]:
    """
    Fetch all posts for a given Instagram username.
    
    Args:
        username: Instagram username (without @)
        max_age_hours: Optional. If provided, only fetch posts from the last N hours.
                      If None, fetch all available posts.
    
    Returns:
        List of parsed post dictionaries
    """
    # Clean username: remove @, trim whitespace, convert to lowercase
    username = str(username).strip().lstrip('@').lower()
    
    if not username:
        logger.error("Empty username provided")
        return []
    
    all_posts = []
    user_id = None
    end_cursor = None
    has_next_page = True
    
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
                    for edge in edges:
                        # Handle both edge format and direct node format
                        if isinstance(edge, dict):
                            node = edge.get("node", edge)  # Fallback to edge itself if no node key
                        else:
                            node = edge
                        
                        if not isinstance(node, dict):
                            continue
                            
                        parsed_post = parse_instagram_post(node)
                        if parsed_post:
                            # If max_age_hours is set, check if post is within time window
                            if cutoff_time is not None:
                                if parsed_post.get("taken_at") and parsed_post["taken_at"] < cutoff_time:
                                    # Post is too old, stop pagination (posts are returned newest first)
                                    logger.info(f"Reached posts older than {max_age_hours} hours, stopping pagination")
                                    has_next_page = False
                                    break
                            all_posts.append(parsed_post)
                    
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
                for post_data in result:
                    parsed_post = parse_instagram_post(post_data)
                    if parsed_post:
                        all_posts.append(parsed_post)
                has_next_page = False
            else:
                logger.warning(f"Unexpected result format for {username}: {type(result)}")
                has_next_page = False
        else:
            logger.error(f"No 'result' key in API response for {username}. Response keys: {list(response_data.keys())}")
            has_next_page = False
        
        # Small delay between pagination requests
        if has_next_page:
            time.sleep(0.2)
    
    logger.info(f"Fetched {len(all_posts)} posts for {username}")
    return all_posts


def fetch_instagram_reels(username: str) -> List[Dict]:
    """
    Fetch all reels for a given Instagram username using the reels endpoint.
    
    Args:
        username: Instagram username (without @)
    
    Returns:
        List of parsed reel dictionaries
    """
    # Clean username: remove @, trim whitespace, convert to lowercase
    username = str(username).strip().lstrip('@').lower()
    
    if not username:
        logger.error("Empty username provided")
        return []
    
    all_reels = []
    end_cursor = None
    has_next_page = True
    
    logger.info(f"Fetching reels for {username}")
    
    while has_next_page:
        # Try using the reels endpoint, fallback to posts if not available
        url = "https://instagram120.p.rapidapi.com/api/instagram/user_reels"
        payload = {
            "username": username,
            "maxId": end_cursor if end_cursor else ""
        }
        
        response_data = _make_api_request(url, payload, method="POST")
        
        # If reels endpoint doesn't work, try posts endpoint and filter for reels
        if not response_data:
            logger.info(f"Reels endpoint failed, trying posts endpoint for {username}")
            url = "https://instagram120.p.rapidapi.com/api/instagram/posts"
            response_data = _make_api_request(url, payload, method="POST")
        
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
                            
                        parsed_reel = parse_instagram_post(node)
                        if parsed_reel:
                            # Ensure is_reel is set to True
                            parsed_reel["is_reel"] = True
                            all_reels.append(parsed_reel)
                    
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
                    logger.warning(f"No reels found in response for {username}. Response keys: {list(result.keys())}")
                    has_next_page = False
            elif isinstance(result, list):
                # Handle direct list of reels
                for reel_data in result:
                    parsed_reel = parse_instagram_post(reel_data)
                    if parsed_reel:
                        parsed_reel["is_reel"] = True
                        all_reels.append(parsed_reel)
                has_next_page = False
            else:
                logger.warning(f"Unexpected result format for reels {username}: {type(result)}")
                has_next_page = False
        else:
            logger.error(f"No 'result' key in API response for reels {username}. Response keys: {list(response_data.keys())}")
            has_next_page = False
        
        # Small delay between pagination requests
        if has_next_page:
            time.sleep(0.2)
    
    logger.info(f"Fetched {len(all_reels)} reels for {username}")
    return all_reels


def get_all_reels_for_username(username: str) -> List[Dict]:
    """
    Alias for fetch_instagram_reels for consistency with get_all_posts_for_username.
    
    Args:
        username: Instagram username (without @)
    
    Returns:
        List of parsed reel dictionaries
    """
    return fetch_instagram_reels(username)


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

