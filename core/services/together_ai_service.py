"""
Together AI keyword extraction service for Instagram posts and Twitter tweets.
Uses Together AI's LLM to extract 5 context-aware keywords from captions.
"""
import json
import logging
import os
import re
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
#MODEL_NAME = "Qwen/Qwen3-Next-80B-A3B-Thinking"
MAX_RETRIES = 3
RETRY_DELAY = 1.0  # seconds
NUM_KEYWORDS = 5
VIDEO_PROMPT_MAX_TOKENS = 2000  # More tokens needed for detailed video prompts
VIDEO_PROMPT_MAX_CHARS = 2000  # Hard character ceiling to keep responses manageable


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
                temperature=0.2,  # Lower temperature for more consistent results
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
    prompt = f"""You are an expert video prompt engineer specialized in maximum audience engagement and user retention. Generate a professional video generation prompt for a 10 second social media video based on the following creative idea. The video MUST tell a clear, coherent story with meaning that the audience can understand and derive value from.

IDEA DETAILS:
Title: {idea_title}
Concept: {idea_concept}
Visual Elements: {visual_elements}
Intended Mood/Impact: {mood_impact}

Generate a detailed video prompt following this exact JSON structure:
{{
  "prompt": "A detailed, positive and creative description for a video generator that tells a COMPLETE STORY with CLEAR MEANING. MUST START WITH A POWERFUL HOOK (first 1-2 seconds) that immediately grabs attention and creates curiosity, intrigue, or emotional connection. The hook should introduce a character, situation, or question that sets up the story. Then the video must progress through a clear narrative arc: establish a goal/challenge, show progression or transformation, and deliver a meaningful resolution or insight that the audience can understand and take away. The story must have visual continuity - the same subject/character throughout, consistent setting, and logical progression. Include: pacing (fast/slow/medium), visual style (cinematic/realistic/stylized), atmosphere (mood, lighting, colors), camera movements (tracking shots, close-ups, wide angles), and video timestamps (0-10 seconds). The story must be self-contained and meaningful - the audience should understand what happened and why it matters. Make it vivid, specific, and emotionally resonant.",
  "negative_prompt": "Things to avoid: bad quality, blurry, off-topic elements, slow starts, boring openings, generic visuals, random disconnected shots, no story progression, confusing narrative, inconsistent characters/subjects, meaningless visuals, abstract concepts without context, etc. Be specific about what NOT to include.",
  "shot_list": [
    "[0-2s]: HOOK - Introduce the main character/subject and the central question, challenge, or situation. Establish the story's starting point with specific visual details. Camera + lighting + mood.",
    "[2-4s]: BUILD - Show the character/subject taking action, facing the challenge, or progressing toward a goal. Develop the story with clear visual progression. Camera + movement + mood.",
    "[4-6s]: BUILD - Escalate the story - show transformation, revelation, or approaching resolution. Maintain visual continuity with the same subject. Camera + framing + motion.",
    "[6-8s]: TRANSITION - Lead toward the resolution or reveal the outcome. Show the story reaching its climax or turning point. Camera + visual elements.",
    "[8-10s]: PAYOFF - Deliver the resolution, insight, or meaningful conclusion. The audience should understand the story's message or takeaway. Camera + composition."
  ]
}}

CRITICAL STORY REQUIREMENTS:
- The video MUST tell a complete, coherent story with a clear beginning, middle, and end
- The story must have MEANING - the audience should be able to understand what happened and derive value or insight from it
- Visual continuity is essential - the same subject/character must appear throughout (or clearly connected elements)
- Each shot must logically connect to the previous one, building a narrative arc
- The story should have a clear message, transformation, or resolution that the audience can understand
- Avoid random, disconnected visuals - every element should serve the story
- The hook should set up a question or situation that the rest of the video answers or resolves
- The payoff must deliver a clear conclusion, insight, or emotional resolution

TECHNICAL REQUIREMENTS:
- The prompt MUST start with an optimal hook (first 1-2 seconds) designed for maximum user retention - this is critical
- The hook should be visually striking, create curiosity, intrigue, or emotional connection
- shot_list must contain exactly 4-6 shots with timestamps
- Each shot should have a clear timestamp range in format "[X-Ys]:" followed by description
- CRITICAL: shot_list items must be plain text strings WITHOUT bullet points, numbers, or list markers
- CRITICAL: Each shot_list item MUST include the story structure label: "HOOK", "BUILD", "TRANSITION", or "PAYOFF"
- shot_list format should be: "[timestamp]: LABEL - Description text" where LABEL is HOOK, BUILD, TRANSITION, or PAYOFF
- The first shot (0-2s) MUST be labeled "HOOK"
- The last shot (8-10s) MUST be labeled "PAYOFF"
- Middle shots should be labeled "BUILD" or "TRANSITION"
- Each shot description must specify what happens in that moment to advance the story
- The prompt should capture the mood and visual style described in the idea
- Negative prompt should list specific things to avoid
- The opening hook is the most important element for user retention - make it compelling

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
                temperature=0.01,  # Slightly higher for more creative prompts
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
            
            # Clean up shot_list: Remove bullet points, numbers, and list markers
            # BUT PRESERVE HOOK, BUILD, TRANSITION, PAYOFF labels
            cleaned_shot_list = []
            for shot in result.get('shot_list', []):
                if isinstance(shot, str):
                    # Remove common bullet point patterns: "1.", "1-", "- ", "• ", "* ", etc.
                    cleaned = shot.strip()
                    # Remove leading numbers with dots, dashes, or spaces (e.g., "1.", "1-", "1 ", "2.", etc.)
                    cleaned = re.sub(r'^\d+[\.\-\s]+\s*', '', cleaned)
                    # Remove common bullet markers (•, -, *, +, >, →, etc.) and Unicode bullets
                    # BUT only if they're at the very start, before the timestamp
                    cleaned = re.sub(r'^[•\-\*\+\>\→\u2022\u2023\u25E6\u2043\u2219\s]+\s*', '', cleaned)
                    # Remove patterns like "- [timestamp]" or "• [timestamp]" or "1. [timestamp]"
                    # BUT preserve the timestamp and everything after it
                    cleaned = re.sub(r'^[\d\-\•\*\+\>\→\s]*\[', '[', cleaned)
                    # Ensure HOOK, BUILD, TRANSITION, PAYOFF labels are preserved
                    # If the shot doesn't have a label, try to infer from position or add one
                    if not re.search(r'\b(HOOK|BUILD|TRANSITION|PAYOFF)\b', cleaned, re.IGNORECASE):
                        # Try to add label based on timestamp
                        if re.match(r'\[0-2s?\]', cleaned, re.IGNORECASE):
                            cleaned = re.sub(r'(\[0-2s?\]:\s*)', r'\1HOOK - ', cleaned, flags=re.IGNORECASE)
                        elif re.match(r'\[8-10s?\]', cleaned, re.IGNORECASE):
                            cleaned = re.sub(r'(\[8-10s?\]:\s*)', r'\1PAYOFF - ', cleaned, flags=re.IGNORECASE)
                        elif re.match(r'\[6-8s?\]', cleaned, re.IGNORECASE):
                            cleaned = re.sub(r'(\[6-8s?\]:\s*)', r'\1TRANSITION - ', cleaned, flags=re.IGNORECASE)
                        else:
                            cleaned = re.sub(r'(\[\d+-\d+s?\]:\s*)', r'\1BUILD - ', cleaned, flags=re.IGNORECASE)
                    # Remove any leading/trailing whitespace
                    cleaned = cleaned.strip()
                    if cleaned:
                        cleaned_shot_list.append(cleaned)
                else:
                    cleaned_shot_list.append(str(shot))
            
            result['shot_list'] = cleaned_shot_list
            
            # Enforce max length on prompt-related fields to avoid oversized responses
            def _truncate_field(field_name: str, limit: int):
                val = result.get(field_name)
                if isinstance(val, str) and len(val) > limit:
                    truncated = val[:limit].rsplit(' ', 1)[0] or val[:limit]
                    logger.warning(
                        f"{field_name} exceeded {limit} chars ({len(val)}). Truncated to {len(truncated)}."
                    )
                    result[field_name] = truncated

            _truncate_field('prompt', VIDEO_PROMPT_MAX_CHARS)
            _truncate_field('negative_prompt', VIDEO_PROMPT_MAX_CHARS)
            
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



