"""
Together AI keyword extraction service for Instagram posts and Twitter tweets.
Uses Together AI's LLM to extract 5 context-aware keywords from captions.
"""
import json
import logging
import os
import time
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

# Try to import together, but make it optional
try:
    from together import Together
    TOGETHER_AVAILABLE = True
    
    # Initialize client (auth defaults to os.environ.get("TOGETHER_API_KEY"))
    _client = None
    
    def get_client():
        """Get or create Together AI client."""
        global _client
        if _client is None:
            api_key = os.environ.get("TOGETHER_API_KEY","tgp_v1_uBdJehjPB5CF1JnO3GTH26nVzQEednvHfU2qZLXKYss     ")
            if not api_key:
                raise ValueError("TOGETHER_API_KEY environment variable is not set")
            _client = Together(api_key=api_key)
        return _client
except ImportError:
    TOGETHER_AVAILABLE = False
    logger.warning("together package not installed. Together AI keyword extraction will be disabled.")
    
    def get_client():
        raise ImportError("together package is required for Together AI keyword extraction")


# Configuration
MODEL_NAME = "meta-llama/Llama-3.3-70B-Instruct-Turbo"
MAX_RETRIES = 3
RETRY_DELAY = 1.0  # seconds
NUM_KEYWORDS = 5
VIDEO_PROMPT_MAX_TOKENS = 2000  # More tokens needed for detailed video prompts


def extract_keywords_with_together_ai(post_id: str, caption: str) -> List[Dict]:
    """
    Extract exactly 5 keywords from a post caption using Together AI.
    
    Args:
        post_id: Unique identifier for the post/tweet
        caption: The caption or text content to extract keywords from
    
    Returns:
        List of keyword dictionaries with 'keyword' field (similarity is None for Together AI keywords)
        Example: [{'keyword': 'keyword1'}, {'keyword': 'keyword2'}, ...]
    
    Raises:
        ValueError: If Together AI is not available or API key is missing
        Exception: If API call fails after retries
    """
    if not TOGETHER_AVAILABLE:
        logger.warning("Together AI keyword extraction disabled: together package not installed")
        return []
    
    if not caption or not caption.strip():
        logger.debug(f"Empty caption for post {post_id}, skipping keyword extraction")
        return []
    
    # Prepare the prompt for structured JSON output
    prompt = f"""Extract exactly 5 keywords from the following social media post caption that best describe its context and main topics. Return only a JSON object with this exact structure:
{{
  "post_id": "{post_id}",
  "keywords": ["keyword1", "keyword2", "keyword3", "keyword4", "keyword5"]
}}

Post ID: {post_id}
Caption: {caption}

Return only the JSON object, no additional text or explanation."""
    print(prompt)

    client = get_client()
    
    # Retry logic for API calls
    last_exception = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.debug(f"Extracting keywords for post {post_id} (attempt {attempt}/{MAX_RETRIES})")
            
            response = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                temperature=0.3,  # Lower temperature for more consistent results
                max_tokens=200,  # Enough for JSON response
            )   
            
            # Extract response content
            response_text = response.choices[0].message.content.strip()
            
            # Try to parse JSON from response
            # Sometimes the model returns text with JSON, so we try to extract it
            json_start = response_text.find('{')
            json_end = response_text.rfind('}') + 1
            
            if json_start >= 0 and json_end > json_start:
                json_text = response_text[json_start:json_end]
            else:
                json_text = response_text
            
            # Parse JSON
            try:
                result = json.loads(json_text)
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse JSON from Together AI response: {response_text[:200]}")
                # Try to extract keywords from text if JSON parsing fails
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY * attempt)
                    continue
                raise ValueError(f"Invalid JSON response from Together AI: {e}")
            
            # Validate response structure
            if 'keywords' not in result:
                logger.warning(f"Response missing 'keywords' field: {result}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY * attempt)
                    continue
                return []
            
            keywords_list = result.get('keywords', [])
            
            # Ensure we have exactly 5 keywords (pad or truncate if needed)
            if len(keywords_list) < NUM_KEYWORDS:
                logger.warning(f"Received {len(keywords_list)} keywords, expected {NUM_KEYWORDS} for post {post_id}")
            elif len(keywords_list) > NUM_KEYWORDS:
                keywords_list = keywords_list[:NUM_KEYWORDS]
                logger.debug(f"Truncated to {NUM_KEYWORDS} keywords for post {post_id}")
            
            # Convert to list of dicts (no similarity score for Together AI keywords)
            keywords = [{'keyword': str(kw).strip()} for kw in keywords_list if kw and str(kw).strip()]
            
            # Filter out empty keywords
            keywords = [kw for kw in keywords if kw['keyword']]
            
            if len(keywords) < NUM_KEYWORDS:
                logger.warning(f"Only extracted {len(keywords)} valid keywords for post {post_id}, expected {NUM_KEYWORDS}")
            
            logger.info(f"Successfully extracted {len(keywords)} keywords for post {post_id}")
            return keywords
            
        except Exception as e:
            last_exception = e
            logger.warning(f"Error extracting keywords for post {post_id} (attempt {attempt}/{MAX_RETRIES}): {e}")
            
            if attempt < MAX_RETRIES:
                # Exponential backoff
                sleep_time = RETRY_DELAY * (2 ** (attempt - 1))
                time.sleep(sleep_time)
                continue
            else:
                logger.error(f"Failed to extract keywords for post {post_id} after {MAX_RETRIES} attempts: {e}", exc_info=True)
                raise
    
    # Should not reach here, but handle it anyway
    if last_exception:
        raise last_exception
    return []


def generate_video_prompt_from_idea(idea: Dict) -> Dict:
    """
    Generate a professional video prompt from an AI idea using Together AI.
    
    Creates a detailed video generation prompt following the format:
    - prompt: detailed description with pacing, visual style, atmosphere, camera moves, product/hook, video timestamps
    - negative_prompt: things to avoid
    - shot_list: array of 4-6 key visual moments with timestamps in seconds
    
    Args:
        idea: Dictionary containing idea data with keys:
            - title: Idea title
            - concept: Idea concept description
            - main_visual_elements: Visual elements description
            - intended_mood/impact: Mood/impact description
    
    Returns:
        Dictionary with video_prompt structure:
        {
            "prompt": str,
            "negative_prompt": str,
            "shot_list": [str, ...]  # 4-6 shots with timestamps
        }
    
    Raises:
        ValueError: If Together AI is not available or API key is missing
        Exception: If API call fails after retries
    """
    if not TOGETHER_AVAILABLE:
        logger.warning("Together AI video prompt generation disabled: together package not installed")
        raise ValueError("Together AI is not available")
    
    idea_title = idea.get('title', 'Untitled Idea')
    idea_concept = idea.get('concept', '')
    visual_elements = idea.get('main_visual_elements', '')
    mood_impact = idea.get('intended_mood/impact', idea.get('intended_mood', idea.get('impact', '')))
    
    # Build comprehensive prompt for Together AI
    prompt = f"""You are an expert video prompt engineer. Generate a professional video generation prompt for a 10 second social media video based on the following creative idea.

IDEA DETAILS:
Title: {idea_title}
Concept: {idea_concept}
Visual Elements: {visual_elements}
Intended Mood/Impact: {mood_impact}

Generate a detailed video prompt following this exact JSON structure:
{{
  "prompt": "A detailed, positive and creative description for a video generator. Include: pacing (fast/slow/medium), visual style (cinematic/realistic/stylized), atmosphere (mood, lighting, colors), camera movements (tracking shots, close-ups, wide angles), product/hook elements, and video timestamps (0-10 seconds). Make it vivid and specific.",
  "negative_prompt": "Things to avoid: bad quality, blurry, off-topic elements, etc. Be specific about what NOT to include.",
  "shot_list": [
    "Shot description with timestamp in seconds (e.g., '0-2s: Close-up of...')",
    "Shot description with timestamp in seconds",
    "Shot description with timestamp in seconds",
    "Shot description with timestamp in seconds",
    "Shot description with timestamp in seconds",
    "Shot description with timestamp in seconds"
  ]
}}

REQUIREMENTS:
- The prompt should be detailed and creative, suitable for AI video generation
- Include specific timestamps in the shot_list (e.g., "0-2s:", "2-5s:", etc.)
- shot_list must contain exactly 4-6 shots
- Each shot should have a clear timestamp range
- The prompt should capture the mood and visual style described in the idea
- Negative prompt should list specific things to avoid

Return ONLY the JSON object, no additional text or explanation."""
    
    client = get_client()
    
    # Retry logic for API calls
    last_exception = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.debug(f"Generating video prompt for idea '{idea_title}' (attempt {attempt}/{MAX_RETRIES})")
            
            response = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                temperature=0.7,  # Slightly higher for more creative prompts
                max_tokens=VIDEO_PROMPT_MAX_TOKENS,
            )
            
            # Extract response content
            response_text = response.choices[0].message.content.strip()
            
            # Try to parse JSON from response
            json_start = response_text.find('{')
            json_end = response_text.rfind('}') + 1
            
            if json_start >= 0 and json_end > json_start:
                json_text = response_text[json_start:json_end]
            else:
                json_text = response_text
            
            # Parse JSON
            try:
                result = json.loads(json_text)
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse JSON from Together AI response: {response_text[:200]}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY * attempt)
                    continue
                raise ValueError(f"Invalid JSON response from Together AI: {e}")
            
            # Validate response structure
            if 'prompt' not in result:
                logger.warning(f"Response missing 'prompt' field: {result}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY * attempt)
                    continue
                raise ValueError("Response missing required 'prompt' field")
            
            # Ensure shot_list exists and is a list
            if 'shot_list' not in result:
                result['shot_list'] = []
            elif not isinstance(result['shot_list'], list):
                logger.warning(f"shot_list is not a list, converting: {result['shot_list']}")
                result['shot_list'] = [str(result['shot_list'])]
            
            # Ensure negative_prompt exists
            if 'negative_prompt' not in result:
                result['negative_prompt'] = "Low quality, blurry, distorted, bad framing, off-topic elements"
            
            # Validate shot_list has 4-6 items
            shot_list = result.get('shot_list', [])
            if len(shot_list) < 4:
                logger.warning(f"shot_list has only {len(shot_list)} items, expected 4-6")
            elif len(shot_list) > 6:
                result['shot_list'] = shot_list[:6]
                logger.debug(f"Truncated shot_list to 6 items")
            
            logger.info(f"Successfully generated video prompt for idea '{idea_title}'")
            return result
            
        except Exception as e:
            last_exception = e
            logger.warning(f"Error generating video prompt for idea '{idea_title}' (attempt {attempt}/{MAX_RETRIES}): {e}")
            
            if attempt < MAX_RETRIES:
                # Exponential backoff
                sleep_time = RETRY_DELAY * (2 ** (attempt - 1))
                time.sleep(sleep_time)
                continue
            else:
                logger.error(f"Failed to generate video prompt for idea '{idea_title}' after {MAX_RETRIES} attempts: {e}", exc_info=True)
                raise
    
    # Should not reach here, but handle it anyway
    if last_exception:
        raise last_exception
    raise Exception("Failed to generate video prompt")



