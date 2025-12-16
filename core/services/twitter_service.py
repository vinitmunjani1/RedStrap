"""
Twitter service for fetching tweets from RapidAPI.
Handles API communication and data parsing for Twitter tweets.
Supports multiple API keys with random selection and automatic retry with different keys.
Optimized with smart rate limiting and API key rotation for faster fetching while respecting rate limits.
"""
import requests
import logging
import time
import random
import json
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from pathlib import Path
from django.conf import settings
from django.utils import timezone
from threading import Lock
from collections import deque
from dateutil import parser as date_parser

logger = logging.getLogger(__name__)

# Directory for saving debug responses
DEBUG_RESPONSES_DIR = Path(__file__).parent.parent.parent / "debug_responses"

# Configuration constants for rate limiting
# Twitter API rate limits may vary, using conservative defaults
RATE_LIMIT_WINDOW = 4  # Rate limit window in seconds
MAX_REQUESTS_PER_WINDOW = 1  # Max requests per window per API key

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
    
    # Ensure minimum delay between requests (4 seconds between requests per key)
    if limiter:
        time_since_last = now - limiter[-1] if limiter else 0
        min_interval = 4.0  # 4 seconds between requests
        if time_since_last < min_interval:
            wait_time = min_interval - time_since_last
            if wait_time > 0.01:
                logger.debug(f"Waiting {wait_time:.2f} seconds to respect 4-second interval for API key")
                time.sleep(wait_time)
                now = time.time()
    
    # Record this API call
    limiter.append(time.time())


def _get_random_api_key() -> str:
    """
    Get a random Twitter-specific API key from the configured list.
    """
    api_keys = getattr(settings, 'TWITTER_RAPIDAPI_KEYS', [])
    if not api_keys:
        api_keys = [getattr(settings, 'TWITTER_RAPIDAPI_KEY', '')]
    api_keys = [k for k in api_keys if k]
    if not api_keys:
        raise ValueError("No Twitter RapidAPI keys configured in settings")
    return random.choice(api_keys)


def _make_api_request(url: str, params: Dict, method: str = "GET", max_retries: int = 3) -> Optional[Dict]:
    """
    Make an API request with automatic retry using different API keys on failure.
    Handles rate limiting and API key rotation.
    Uses GET method with query parameters as per RapidAPI Twitter API requirements.
    
    Args:
        url: The API endpoint URL
        params: Query parameters for the request
        method: HTTP method (default: GET)
        max_retries: Maximum number of retry attempts with different keys
    
    Returns:
        JSON response as dict, or None if all retries failed
    """
    api_keys = getattr(settings, 'TWITTER_RAPIDAPI_KEYS', [])
    if not api_keys:
        api_keys = [getattr(settings, 'TWITTER_RAPIDAPI_KEY', '')]
    api_keys = [k for k in api_keys if k]
    
    if not api_keys:
        error_msg = "No Twitter RapidAPI keys configured. Please check your settings."
        logger.error(error_msg)
        logger.error(f"TWITTER_RAPIDAPI_KEYS from settings: {api_keys}")
        logger.error(f"TWITTER_RAPIDAPI_KEY from settings: {getattr(settings, 'TWITTER_RAPIDAPI_KEY', 'NOT SET')}")
        return None
    
    # Try each API key until one works
    for attempt in range(max_retries):
        api_key = _get_random_api_key()
        _wait_for_rate_limit(api_key)
        
        headers = {
            "x-rapidapi-key": api_key,
            "x-rapidapi-host": getattr(settings, 'TWITTER_RAPIDAPI_HOST', 'twitter241.p.rapidapi.com'),
        }
        
        try:
            # Log API request for debugging (without exposing full API key)
            logger.info(f"Making Twitter API request to {url} with API key: {api_key[:10]}... (attempt {attempt + 1}/{max_retries})")
            
            if method.upper() == "GET":
                response = requests.get(url, params=params, headers=headers, timeout=60)  # Increased timeout for Railway
            else:
                response = requests.post(url, json=params, headers=headers, timeout=60)  # Increased timeout for Railway
            
            # Handle 404 specifically - might mean user doesn't exist or endpoint changed
            if response.status_code == 404:
                logger.error(f"404 Not Found for URL: {url} with params: {params}")
                return None
            
            # Handle 403 - might still contain data, or might be authentication issue
            if response.status_code == 403:
                try:
                    response_data = response.json()
                    # Log the response to see if it contains data despite 403
                    logger.warning(f"403 Forbidden for {url}, but response contains: {json.dumps(response_data)[:500]}")
                    # Sometimes APIs return data with 403, try to use it
                    if response_data and ('result' in response_data or 'user' in response_data):
                        logger.info(f"403 response contains data, attempting to parse")
                        return response_data
                except:
                    pass
                error_text = response.text[:500] if hasattr(response, 'text') else 'No response text'
                logger.error(f"403 Forbidden for URL: {url} with params: {params}. Response: {error_text}")
                logger.error(f"This usually means the API key doesn't have access to Twitter API. Check your RapidAPI subscription.")
                if attempt < max_retries - 1:
                    # Try next key
                    continue
                else:
                    return None
            
            # Handle 429 (Too Many Requests) with exponential backoff
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 10))
                wait_time = min(retry_after, 120)
                logger.warning(f"Rate limit exceeded (429) for {url}. Waiting {wait_time} seconds before retry (attempt {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    time.sleep(wait_time)
                    continue
                else:
                    logger.error(f"Rate limit exceeded after {max_retries} attempts for URL: {url}")
                    return None
            
            response.raise_for_status()
            response_data = response.json()
            
            return response_data
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                logger.error(f"404 Not Found: {e.response.text[:200] if e.response else 'No response'}")
                return None
            elif e.response.status_code == 403:
                try:
                    response_data = e.response.json()
                    logger.warning(f"403 Forbidden in exception handler, response: {json.dumps(response_data)[:500]}")
                    if response_data and ('result' in response_data or 'user' in response_data):
                        logger.info(f"403 response contains data, attempting to parse")
                        return response_data
                except:
                    pass
                error_text = e.response.text[:500] if e.response else 'No response'
                logger.error(f"403 Forbidden: {error_text}")
                logger.error(f"API key may not have access to Twitter API. Check your RapidAPI subscription for Twitter241 API.")
                if attempt < max_retries - 1:
                    continue
                else:
                    return None
            elif e.response.status_code == 429:
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
                wait_time = min(2 ** attempt, 10)
                time.sleep(wait_time)
            else:
                logger.error(f"All API key attempts failed for URL: {url} with params: {params}")
                return None
        except requests.exceptions.Timeout as e:
            logger.error(f"Request timeout (60s) for {url} with API key {api_key[:10]}... (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                # Try a different key on next iteration with longer wait
                time.sleep(2)
            else:
                logger.error(f"All Twitter API requests timed out after {max_retries} attempts")
                return None
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error for {url} with API key {api_key[:10]}... (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                # Try a different key on next iteration
                time.sleep(2)
            else:
                logger.error(f"All Twitter API requests failed with connection errors after {max_retries} attempts")
                return None
        except requests.exceptions.RequestException as e:
            logger.warning(f"API request failed with key (attempt {attempt + 1}/{max_retries}): {e}")
            logger.warning(f"Request type: {type(e).__name__}, URL: {url}")
            if attempt < max_retries - 1:
                time.sleep(1)
            else:
                logger.error(f"All API key attempts failed for URL: {url} with params: {params}")
                return None
    
    return None


def get_user_by_username(username: str) -> Optional[Dict]:
    """
    Fetch Twitter user information by username.
    
    Args:
        username: Twitter username (without @)
    
    Returns:
        Dictionary with user data including rest_id, or None if failed
    """
    # Clean username: remove @, trim whitespace
    username = str(username).strip().lstrip('@')
    
    if not username:
        logger.error("Empty username provided")
        return None
    
    url = "https://twitter241.p.rapidapi.com/user"
    params = {"username": username}
    
    response_data = _make_api_request(url, params, method="GET")
    
    if not response_data:
        logger.error(f"Failed to fetch user info for {username}")
        return None
    
    # Log raw response for debugging missing users
    try:
        logger.info(f"Twitter user lookup raw response for {username}: {json.dumps(response_data)[:800]}")
    except Exception:
        pass
    
    # Extract user data from response
    # Common structures:
    # 1) {"user": {"result": {...}}}
    # 2) {"result": {"data": {"user": {"result": {...}}}}}
    user_data = response_data.get("user", {}).get("result")
    if not user_data:
        user_data = (
            response_data.get("result", {})
            .get("data", {})
            .get("user", {})
            .get("result")
        )
    
    if not user_data:
        logger.error(f"No user data found in response for {username}")
        return None
    
    # Extract rest_id which is needed for fetching tweets
    rest_id = user_data.get("rest_id")
    if not rest_id:
        logger.error(f"No rest_id found for user {username}")
        return None
    
    logger.info(f"Successfully fetched user info for {username}, rest_id: {rest_id}")
    
    return {
        "rest_id": rest_id,
        "username": user_data.get("legacy", {}).get("screen_name", username),
        "name": user_data.get("legacy", {}).get("name", username),
        "description": user_data.get("legacy", {}).get("description", ""),
        "followers_count": user_data.get("legacy", {}).get("followers_count", 0),
        "following_count": user_data.get("legacy", {}).get("friends_count", 0),
        "tweet_count": user_data.get("legacy", {}).get("statuses_count", 0),
        "verified": user_data.get("legacy", {}).get("verified", False),
        "profile_image_url": user_data.get("legacy", {}).get("profile_image_url_https", ""),
        "profile_banner_url": user_data.get("legacy", {}).get("profile_banner_url", ""),
        "created_at": user_data.get("legacy", {}).get("created_at", ""),
    }


def parse_tweet(entry: Dict) -> Optional[Dict]:
    """
    Parse a single tweet from the timeline entries.
    
    Args:
        entry: Dictionary containing tweet entry data from API response
    
    Returns:
        Dictionary with parsed tweet data, or None if parsing failed
    """
    try:
        # Navigate through the nested structure to get tweet data
        # Structure: entry -> content -> itemContent -> tweet_results -> result
        content = entry.get("content", {})
        item_content = content.get("itemContent", {})
        tweet_results = item_content.get("tweet_results", {})
        tweet_data = tweet_results.get("result", {})
        
        if not tweet_data or tweet_data.get("__typename") != "Tweet":
            return None
        
        # Extract tweet ID
        tweet_id = tweet_data.get("rest_id")
        if not tweet_id:
            return None
        
        # Extract legacy data (contains most tweet information)
        legacy = tweet_data.get("legacy", {})
        
        # Extract text
        text = legacy.get("full_text", "")
        
        # Extract created_at timestamp
        created_at_str = legacy.get("created_at", "")
        created_at = None
        if created_at_str:
            try:
                # Parse Twitter date format: "Mon Dec 01 16:01:30 +0000 2025"
                created_at = date_parser.parse(created_at_str)
                # Convert to timezone-aware datetime
                if created_at.tzinfo is None:
                    created_at = timezone.make_aware(created_at)
            except Exception as e:
                logger.warning(f"Error parsing created_at for tweet {tweet_id}: {e}")
                created_at = timezone.now()
        else:
            created_at = timezone.now()
        
        # Extract engagement metrics
        favorite_count = legacy.get("favorite_count", 0)
        retweet_count = legacy.get("retweet_count", 0)
        reply_count = legacy.get("reply_count", 0)
        quote_count = legacy.get("quote_count", 0)
        
        # Extract view count (if available)
        views = tweet_data.get("views", {})
        view_count = 0
        if isinstance(views, dict) and "count" in views:
            try:
                view_count = int(views.get("count", 0))
            except (ValueError, TypeError):
                view_count = 0
        
        # Extract media attachments
        media_items = []
        extended_entities = legacy.get("extended_entities", {})
        entities = legacy.get("entities", {})
        
        # Check extended_entities first (has full media info), then entities
        media_list = extended_entities.get("media", []) or entities.get("media", [])
        
        for media in media_list:
            media_type = media.get("type", "photo")
            media_url = media.get("media_url_https", "")
            
            # For videos, try to get video_info
            video_url = ""
            if media_type == "video":
                video_info = media.get("video_info", {})
                variants = video_info.get("variants", [])
                if variants:
                    # Get the highest bitrate variant
                    variants.sort(key=lambda x: x.get("bitrate", 0), reverse=True)
                    video_url = variants[0].get("url", "") if variants else ""
            
            media_items.append({
                "type": media_type,
                "url": media_url,
                "video_url": video_url,
                "display_url": media.get("display_url", ""),
                "expanded_url": media.get("expanded_url", ""),
            })
        
        # Extract hashtags and mentions
        hashtags = [tag.get("text", "") for tag in entities.get("hashtags", [])]
        mentions = [mention.get("screen_name", "") for mention in entities.get("user_mentions", [])]
        
        # Extract URLs
        urls = [url_obj.get("expanded_url", "") for url_obj in entities.get("urls", [])]
        
        return {
            "tweet_id": str(tweet_id),
            "text": text,
            "created_at": created_at,
            "favorite_count": favorite_count,
            "retweet_count": retweet_count,
            "reply_count": reply_count,
            "quote_count": quote_count,
            "view_count": view_count,
            "media": media_items,
            "hashtags": hashtags,
            "mentions": mentions,
            "urls": urls,
            "is_retweet": legacy.get("retweeted", False),
            "is_quote": legacy.get("is_quote_status", False),
            "lang": legacy.get("lang", ""),
        }
    except Exception as e:
        logger.error(f"Error parsing tweet: {e}", exc_info=True)
        return None


def get_user_tweets(rest_id: str, count: int = 20, cursor: Optional[str] = None, max_age_hours: Optional[int] = None, stop_tweet_id: Optional[str] = None) -> Dict:
    """
    Fetch tweets for a user by their rest_id.
    
    Args:
        rest_id: Twitter user rest_id (obtained from get_user_by_username)
        count: Number of tweets to fetch per request (default: 20)
        cursor: Pagination cursor for fetching more tweets (optional)
        max_age_hours: Optional. If provided, only fetch tweets from the last N hours.
    
    Returns:
        Dictionary with 'tweets' list and 'next_cursor' for pagination
    """
    if not rest_id:
        logger.error("Empty rest_id provided")
        return {"tweets": [], "next_cursor": None}
    
    url = "https://twitter241.p.rapidapi.com/user-tweets"
    params = {"user": rest_id, "count": str(count)}
    
    if cursor:
        params["cursor"] = cursor
    
    response_data = _make_api_request(url, params, method="GET")
    
    if not response_data:
        logger.error(f"Failed to fetch tweets for rest_id: {rest_id}")
        return {"tweets": [], "next_cursor": None}
    
    # Extract cursor for pagination
    cursor_data = response_data.get("cursor", {})
    next_cursor = cursor_data.get("bottom")  # Use bottom cursor for next page
    
    # Extract timeline data
    result = response_data.get("result", {})
    timeline = result.get("timeline", {})
    instructions = timeline.get("instructions", [])
    
    tweets = []
    cutoff_time = None
    
    if max_age_hours:
        cutoff_time = timezone.now() - timedelta(hours=max_age_hours)
        logger.info(f"Fetching tweets from last {max_age_hours} hours (cutoff: {cutoff_time})")
    
    # Find TimelineAddEntries instruction which contains the tweets
    for instruction in instructions:
        if instruction.get("type") == "TimelineAddEntries":
            entries = instruction.get("entries", [])
            
            for entry in entries:
                # Only process tweet entries (skip other entry types)
                entry_id = entry.get("entryId", "")
                if not entry_id.startswith("tweet-"):
                    continue
                
                parsed_tweet = parse_tweet(entry)
                if parsed_tweet:
                    # Stop if we reached an already-known tweet id
                    if stop_tweet_id and parsed_tweet.get("tweet_id") == stop_tweet_id:
                        logger.info(f"Reached existing tweet_id {stop_tweet_id}; stopping pagination")
                        next_cursor = None
                        break
                    
                    # Check time cutoff if specified
                    if cutoff_time and parsed_tweet.get("created_at"):
                        if parsed_tweet["created_at"] < cutoff_time:
                            logger.info(f"Reached tweets older than {max_age_hours} hours")
                            next_cursor = None  # Stop pagination
                            break
                    
                    tweets.append(parsed_tweet)
    
    logger.info(f"Fetched {len(tweets)} tweets for rest_id: {rest_id}")
    
    return {
        "tweets": tweets,
        "next_cursor": next_cursor,
    }


def get_all_tweets_for_user(
    username: str,
    max_age_hours: Optional[int] = None,
    max_tweets: Optional[int] = None,
    save_callback: Optional[callable] = None,
    stop_tweet_id: Optional[str] = None,
) -> List[Dict]:
    """
    Fetch all tweets for a given Twitter username with pagination.
    
    Args:
        username: Twitter username (without @)
        max_age_hours: Optional. If provided, only fetch tweets from the last N hours.
        max_tweets: Optional. Maximum number of tweets to fetch.
        save_callback: Optional callback function that receives a list of tweet dictionaries
                      after each API call. Called as: save_callback(tweets_batch)
    
    Returns:
        List of parsed tweet dictionaries
    """
    # First, get user info to obtain rest_id
    user_info = get_user_by_username(username)
    if not user_info:
        logger.error(f"Failed to get user info for {username}")
        return []
    
    rest_id = user_info.get("rest_id")
    if not rest_id:
        logger.error(f"No rest_id found for user {username}")
        return []
    
    all_tweets = []
    cursor = None
    count = 20  # Default count per request
    
    # Get test mode limit from settings
    test_mode_limit = getattr(settings, 'TEST_MODE_TWEETS_LIMIT', None)
    if test_mode_limit and (max_tweets is None or max_tweets > test_mode_limit):
        max_tweets = test_mode_limit
        logger.info(f"TEST MODE: Limiting tweet fetch to {test_mode_limit} most recent tweets")
    
    while True:
        # Fetch a batch of tweets
        result = get_user_tweets(rest_id, count=count, cursor=cursor, max_age_hours=max_age_hours, stop_tweet_id=stop_tweet_id)
        batch_tweets = result.get("tweets", [])
        next_cursor = result.get("next_cursor")
        
        if not batch_tweets:
            break
        
        # Process batch
        for tweet in batch_tweets:
            # Check max_tweets limit
            if max_tweets and len(all_tweets) >= max_tweets:
                logger.info(f"Reached max_tweets limit of {max_tweets}")
                break
            
            all_tweets.append(tweet)
        
        # Save batch via callback if provided
        if save_callback and batch_tweets:
            try:
                save_callback(batch_tweets)
                logger.info(f"Saved {len(batch_tweets)} tweets via callback")
            except Exception as e:
                logger.error(f"Error in save_callback: {e}", exc_info=True)
        
        # Check if we've reached limits
        if max_tweets and len(all_tweets) >= max_tweets:
            break
        
        # Check if there's a next page
        if not next_cursor:
            break
        
        cursor = next_cursor
        
        # Small delay between pagination requests
        time.sleep(0.5)
    
    logger.info(f"Fetched total of {len(all_tweets)} tweets for {username}")
    
    return all_tweets

