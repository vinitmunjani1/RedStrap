"""
Reddit service for scraping posts from subreddits.
Uses BeautifulSoup to parse old.reddit.com HTML.
"""
import requests
import time
import random
import logging
from typing import List, Dict, Optional
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Configuration
REQUEST_DELAY = 2.0
MAX_RETRIES = 3
BACKOFF_FACTOR = 2.0
MAX_POSTS_PER_SUB = 50
MIN_SCORE = 0  # Minimum score threshold (set to 0 to get all posts)


def get_with_backoff(url: str, headers: Dict[str, str], timeout: int = 10) -> requests.Response:
    """
    Perform GET with basic backoff on 429 and 5xx errors.
    Respects Retry-After if present for 429.
    """
    delay = REQUEST_DELAY
    last_exc = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            status = resp.status_code

            # Handle rate-limit explicitly
            if status == 429:
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    sleep_seconds = int(retry_after)
                else:
                    sleep_seconds = delay
                    delay *= BACKOFF_FACTOR

                logger.warning(f"[429] Rate limited on {url} (attempt {attempt}/{MAX_RETRIES}). Sleeping {sleep_seconds:.1f}s...")
                time.sleep(sleep_seconds)
                continue

            # Retry server errors
            if 500 <= status < 600:
                logger.warning(f"[{status}] Server error on {url} (attempt {attempt}/{MAX_RETRIES}). Sleeping {delay:.1f}s...")
                time.sleep(delay)
                delay *= BACKOFF_FACTOR
                continue

            # Anything else: raise if error, otherwise return
            resp.raise_for_status()

            # Small jitter so we don't look like a tight loop
            time.sleep(REQUEST_DELAY + random.uniform(0, 0.5))
            return resp

        except requests.RequestException as e:
            last_exc = e
            logger.warning(f"[ERROR] {e} on {url} (attempt {attempt}/{MAX_RETRIES})")
            time.sleep(delay)
            delay *= BACKOFF_FACTOR

    # Out of retries
    raise RuntimeError(f"Failed to fetch {url} after {MAX_RETRIES} attempts") from last_exc


def scrape_subreddit(subreddit_name: str) -> List[Dict]:
    """
    Scrape posts from a subreddit using old.reddit.com.
    
    Args:
        subreddit_name: Name of the subreddit (without r/)
    
    Returns:
        List of post dictionaries with title, url, score, body, flair
    """
    url = f"https://old.reddit.com/r/{subreddit_name}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    logger.info(f"Scraping subreddit r/{subreddit_name}")
    
    posts = []
    seen_urls = set()
    
    try:
        response = get_with_backoff(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.content, "html.parser")
        
        things = soup.find_all("div", class_="thing")
        logger.info(f"Found {len(things)} posts on listing")
        
        count_kept = 0
        
        for thing in things:
            if count_kept >= MAX_POSTS_PER_SUB:
                break
            
            # Skip promoted / ads
            if thing.get("data-promoted") == "True":
                continue
            
            title_tag = thing.find("a", class_="title")
            if not title_tag:
                continue
            
            title = title_tag.get_text(strip=True)
            href = title_tag.get("href", "")
            
            # Build full permalink
            if href.startswith("/"):
                permalink = "https://www.reddit.com" + href
            else:
                permalink = href
            
            # Keep only post URLs
            if "/comments/" not in permalink:
                continue
            if permalink in seen_urls:
                continue
            seen_urls.add(permalink)
            
            # Fetch JSON for full post body + score + flair
            body = ""
            post_score = 0
            flair = ""
            try:
                json_url = permalink + ".json"
                post_resp = get_with_backoff(json_url, headers=headers, timeout=10)
                j = post_resp.json()
                
                post_data = j[0]["data"]["children"][0]["data"]
                post_score = int(post_data.get("score", 0) or 0)
                
                # Enforce score threshold
                if post_score < MIN_SCORE:
                    continue
                
                body = post_data.get("selftext", "") or ""
                flair = post_data.get("link_flair_text") or ""
                
            except Exception as e:
                logger.warning(f"Error fetching JSON for {permalink}: {e}")
                continue
            
            posts.append({
                "title": title,
                "url": permalink,
                "score": post_score,
                "body": body,
                "flair": flair,
            })
            
            count_kept += 1
        
        logger.info(f"Kept {count_kept} posts with score >= {MIN_SCORE}")
        
    except Exception as e:
        logger.error(f"Error while scraping r/{subreddit_name}: {e}", exc_info=True)
    
    return posts
