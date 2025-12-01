import requests
import json
import csv
import time
import re
import random
from typing import List, Dict, Tuple, Optional

from bs4 import BeautifulSoup
import numpy as np
from sentence_transformers import SentenceTransformer


# ------------- CONFIG -------------

MODEL_NAME = "BAAI/bge-small-en"

# base delay between successful requests
REQUEST_DELAY = 2.0

# minimum Reddit post score to keep (from JSON ONLY)
MIN_SCORE = 100  # set to 0 if you want all posts regardless of score

# retry / backoff config
MAX_RETRIES = 3
BACKOFF_FACTOR = 2.0  # 2, 4, 8 seconds, etc

# scrape limits
MAX_SUBREDDITS_PER_RUN = 6     # don't hammer all at once
MAX_POSTS_PER_SUB = 50         # max posts per subreddit page


# ------------- MODEL -------------

print("Loading embedding model:", MODEL_NAME)
model = SentenceTransformer(MODEL_NAME)  # CPU by default


# ------------- KEYWORD FROM TITLE (CANDIDATE PHRASES) -------------

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "for", "to", "in",
    "on", "at", "is", "are", "was", "were", "be", "with",
    "this", "that", "it", "from", "by", "about", "how",
    "what", "when", "where", "why", "who", "your", "you",
    "my", "we", "they", "i"
}


def tokenize_title(title: str) -> List[str]:
    """
    Lowercase the title and split into simple word tokens,
    filtering out stopwords and tiny junk tokens.
    """
    title = title.lower()
    tokens = re.findall(r"\b[a-z0-9][a-z0-9+\-]*\b", title)
    tokens = [t for t in tokens if t not in STOPWORDS and len(t) > 2]
    return tokens


def get_candidate_phrases(title: str, max_phrases: int = 15) -> List[str]:
    """
    Build candidate phrases (n-grams) from the title tokens:
    1, 2, and 3 word phrases.
    """
    tokens = tokenize_title(title)
    if not tokens:
        return []

    phrases = set()
    n_tokens = len(tokens)
    for n in range(1, 10):  # 1, 2, 3-grams
        for i in range(n_tokens - n + 1):
            phrase = " ".join(tokens[i:i + n])
            phrases.add(phrase)

    # Sort: longer phrases first (more expressive), then alphabetically
    phrases_list = sorted(phrases, key=lambda x: (-len(x.split()), x))
    return phrases_list[:max_phrases]


def best_keyword_for_title(
    text: str,
    min_similarity: float = 0.30
) -> Tuple[Optional[str], float]:
    """
    Use BGE embeddings to pick the single best phrase that represents the text.
    The phrase is always built from the text itself.
    Returns (best_phrase or None, similarity).
    """
    text = text.strip()
    if not text:
        return None, 0.0

    candidates = get_candidate_phrases(text)
    if not candidates:
        return None, 0.0

    texts = [text] + candidates  # first = full text, rest = phrases

    embs = model.encode(texts, normalize_embeddings=True, batch_size=32)
    embs = np.array(embs)

    text_emb = embs[0]      # (dim,)
    cand_embs = embs[1:]    # (N, dim)

    # cosine similarity = dot product since embeddings are normalized
    sims = cand_embs @ text_emb  # shape (N,)
    best_idx = int(np.argmax(sims))
    best_score = float(sims[best_idx])
    best_phrase = candidates[best_idx]

    if best_score < min_similarity:
        return None, best_score

    return best_phrase, best_score


# ------------- HTTP HELPER WITH BACKOFF -------------

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

                print(f"[429] Rate limited on {url} (attempt {attempt}/{MAX_RETRIES}). "
                      f"Sleeping {sleep_seconds:.1f}s...")
                time.sleep(sleep_seconds)
                continue

            # Retry server errors a bit
            if 500 <= status < 600:
                print(f"[{status}] Server error on {url} (attempt {attempt}/{MAX_RETRIES}). "
                      f"Sleeping {delay:.1f}s...")
                time.sleep(delay)
                delay *= BACKOFF_FACTOR
                continue

            # Anything else: raise if error, otherwise return
            resp.raise_for_status()

            # small jitter so we don't look like a tight loop
            time.sleep(REQUEST_DELAY + random.uniform(0, 0.5))
            return resp

        except requests.RequestException as e:
            last_exc = e
            print(f"[ERROR] {e} on {url} (attempt {attempt}/{MAX_RETRIES})")
            time.sleep(delay)
            delay *= BACKOFF_FACTOR

    # Out of retries
    raise RuntimeError(f"Failed to fetch {url} after {MAX_RETRIES} attempts") from last_exc


# ------------- SCRAPER -------------

def scrape_reddit() -> List[Dict]:
    subreddits = [
        "https://old.reddit.com/r/ChatGpt",
        # "https://old.reddit.com/r/GenAI4all",
        # "https://old.reddit.com/r/grok",
        "https://old.reddit.com/r/LocalLLaMA",
        "https://old.reddit.com/r/LocalLLM",
        # "https://old.reddit.com/r/mcp",
        "https://old.reddit.com/r/midjourney",
        # "https://old.reddit.com/r/MistralAI",
        # "https://old.reddit.com/r/singularity",
        "https://old.reddit.com/r/StableDiffusion",
        # "https://old.reddit.com/r/SunoAI",
        # "https://old.reddit.com/r/Youtube_Automation",
        # "https://old.reddit.com/r/YT_Faceless",
        # "https://old.reddit.com/r/civitai",
        "https://old.reddit.com/r/comfyui",
        # "https://old.reddit.com/r/developersIndia",
        # "https://old.reddit.com/r/ArtificialInteligence",
        # "https://old.reddit.com/r/Automate",
        
    ]

    all_data: List[Dict] = []

    # hard cap how many subs we hit in one run
    subs_to_scrape = subreddits[:MAX_SUBREDDITS_PER_RUN]

    for url in subs_to_scrape:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }

        subreddit_name = url.rstrip("/").split("/")[-1]
        print(f"\nScraping: {subreddit_name}")

        try:
            response = get_with_backoff(url, headers=headers, timeout=10)
            soup = BeautifulSoup(response.content, "html.parser")

            subreddit_data: Dict = {
                "subreddit": subreddit_name,
                "url": url,
                "title": soup.title.string if soup.title else "No title",
                "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }

            topics = []
            discussions = []
            seen_urls = set()

            things = soup.find_all("div", class_="thing")
            print(f"  Found {len(things)} posts on listing")

            count_kept = 0

            for thing in things:
                if count_kept >= MAX_POSTS_PER_SUB:
                    break

                # skip promoted / ads if any
                if thing.get("data-promoted") == "True":
                    continue

                title_tag = thing.find("a", class_="title")
                if not title_tag:
                    continue

                title = title_tag.get_text(strip=True)

                href = title_tag.get("href", "")
                print(href)

                # build full permalink
                if href.startswith("/"):
                    permalink = "https://www.reddit.com" + href
                else:
                    permalink = href

                # keep only post URLs
                if "/comments/" not in permalink:
                    continue
                if permalink in seen_urls:
                    continue
                seen_urls.add(permalink)

                # fetch JSON for full post body + score + flair
                body = ""
                post_score = 0
                flair = ""
                try:
                    json_url = permalink + ".json"
                    post_resp = get_with_backoff(json_url, headers=headers, timeout=10)
                    j = post_resp.json()

                    post_data = j[0]["data"]["children"][0]["data"]
                    post_score = int(post_data.get("score", 0) or 0)
                    print(permalink,post_score)

                    # enforce score threshold ONLY based on JSON
                    if post_score < MIN_SCORE:
                        continue

                    body = post_data.get("selftext", "") or ""
                    flair = post_data.get("link_flair_text") or ""

                except Exception as e:
                    print(f"  ERROR fetching JSON for {permalink}: {e}")
                    continue

                # keyword from TITLE + BODY (but don't drop post if keyword fails)
                short_title = title[:100] + "..." if len(title) > 100 else title
                combined_text = (title + "\n\n" + body).strip()

                kw, similarity = best_keyword_for_title(combined_text)
                if kw is None:
                    kw = ""
                    similarity = 0.0

                discussions.append({
                    "title": short_title,
                    "url": permalink,
                    "score": post_score,       # Reddit score
                    "flair": flair,            # REAL flair, e.g. "Discussion", "News", etc
                    "type": "post",            # internal type
                    "best_keyword": kw,
                    "similarity": similarity,
                    "body": body,              # full post text
                })

                count_kept += 1

            print(f"  Kept {count_kept} posts with score >= {MIN_SCORE}")
            subreddit_data["topics"] = topics
            subreddit_data["discussions"] = discussions

            all_data.append(subreddit_data)

        except Exception as e:
            print(f"ERROR while scraping {subreddit_name}: {e}")

    return all_data


# ------------- SAVE TO JSON + CSV -------------

def save_scraped_data(
    data: List[Dict],
    filename_json: str = "topics.json",
    filename_csv: str = "topics.csv",
) -> None:
    if not data:
        print("No data to save")
        return

    # JSON
    try:
        with open(filename_json, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2, ensure_ascii=False)
        print(f"JSON saved: {filename_json}")
    except Exception as e:
        print("ERROR saving JSON:", e)

    # CSV
    try:
        with open(filename_csv, "w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow([
                "Subreddit",
                "Type",
                "Title",
                "URL",
                "Best Keyword",
                "Similarity",
                "Scraped At",
            ])

            for subreddit_data in data:
                subreddit = subreddit_data["subreddit"]
                scraped_at = subreddit_data["scraped_at"]

                for topic in subreddit_data.get("topics", []):
                    writer.writerow([
                        subreddit,
                        topic["type"],
                        topic["title"],
                        "",
                        topic.get("best_keyword", ""),
                        f"{topic.get('similarity', 0):.4f}",
                        scraped_at,
                    ])

                for discussion in subreddit_data.get("discussions", []):
                    writer.writerow([
                        subreddit,
                        discussion["type"],      # "post"
                        discussion["title"],
                        discussion["url"],
                        discussion.get("best_keyword", ""),
                        f"{discussion.get('similarity', 0):.4f}",
                        scraped_at,
                    ])

        print(f"CSV saved: {filename_csv}")
    except Exception as e:
        print("ERROR saving CSV:", e)


# ------------- MAIN -------------

def main() -> None:
    data = scrape_reddit()

    if data:
        total_topics = 0
        total_discussions = 0

        for subreddit_data in data:
            total_topics += len(subreddit_data.get("topics", []))
            total_discussions += len(subreddit_data.get("discussions", []))

        print(f"\nTotal: {total_topics} topics, {total_discussions} discussions")
        save_scraped_data(data)
    else:
        print("No data returned from scraping")


if __name__ == "__main__":
    main()
