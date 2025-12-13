"""
Google Gemini AI service for video idea extraction.
Handles video downloading, duration calculation, and AI-powered idea generation.
"""
import google.generativeai as genai
import uuid
import json
import cv2
import numpy as np
import requests
import logging
from django.conf import settings

logger = logging.getLogger(__name__)


def convert_video_url_to_mp4_bytes(url):
    """
    Download a video from the provided URL and return its mp4 content as bytes (do not save to file).
    Handles 403 errors by sending comprehensive headers that mimic a browser request.
    
    Args:
        url (str): Direct video URL (should end with .mp4)
    
    Returns:
        bytes: The content of the downloaded mp4 video
    
    Raises:
        requests.RequestException: If the download fails
    """
    # Comprehensive headers to mimic browser request and avoid 403 errors
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.instagram.com/",
        "Origin": "https://www.instagram.com",
        "Sec-Fetch-Dest": "video",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "cross-site",
        "Connection": "keep-alive",
        "DNT": "1",
    }
    
    try:
        response = requests.get(url, headers=headers, stream=True, timeout=30)
        response.raise_for_status()
        video_bytes = b""
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                video_bytes += chunk
        logger.info(f"Video content downloaded ({len(video_bytes)} bytes).")
        return video_bytes
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 403:
            logger.warning(f"403 Forbidden error for URL. This may be due to Instagram CDN protection.")
            # Try with session to maintain cookies
            session = requests.Session()
            session.headers.update(headers)
            try:
                response = session.get(url, stream=True, timeout=30)
                response.raise_for_status()
                video_bytes = b""
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        video_bytes += chunk
                logger.info(f"Video content downloaded with session ({len(video_bytes)} bytes).")
                return video_bytes
            except Exception as e2:
                logger.error(f"Failed to download video even with session: {str(e2)}")
                raise requests.exceptions.RequestException(
                    f"Unable to download video: Instagram CDN returned 403 Forbidden. "
                    f"The video URL may be expired or require authentication. Original error: {str(e)}"
                )
        raise


def get_video_duration(video_bytes):
    """
    Get video duration from video bytes using OpenCV.
    
    Args:
        video_bytes (bytes): Video content as bytes
    
    Returns:
        float: Duration in seconds, or None if unable to determine
    """
    try:
        # Convert bytes to numpy array for OpenCV
        nparr = np.frombuffer(video_bytes, np.uint8)
        # Create a temporary file-like object in memory
        # OpenCV can read from memory using cv2.imdecode for images, but for video
        # we need to use a different approach - write to temp file or use VideoCapture with bytes
        # For now, we'll use a workaround: write to temp file
        import tempfile
        import os
        
        with tempfile.NamedTemporaryFile(delete=False, suffix='.mp4') as tmp_file:
            tmp_file.write(video_bytes)
            tmp_path = tmp_file.name
        
        try:
            cap = cv2.VideoCapture(tmp_path)
            if not cap.isOpened():
                return None
            fps = cap.get(cv2.CAP_PROP_FPS)
            frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
            cap.release()
            duration = frame_count / fps if fps > 0 else None
            return duration
        finally:
            # Clean up temp file
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
    except Exception as e:
        logger.error(f"Error getting video duration: {str(e)}")
        return None


def extract_composite_json(response, duration_for_prompt):
    """
    Extract and parse JSON from Gemini API response.
    Handles markdown code blocks and ensures all required fields are present.
    
    Args:
        response: Gemini API response object
        duration_for_prompt (float): Duration value to use as fallback
    
    Returns:
        dict: Parsed JSON response with all required fields
    """
    output = response.candidates[0].content.parts[0].text
    try:
        # If model returns ```json wrapper
        if output.strip().startswith("```json"):
            output = output.strip()[7:]
        output = output.strip().strip("`").strip()
        json_start = output.find('{')
        json_end = output.rfind('}')
        json_str = output[json_start:json_end+1]
        composite = json.loads(json_str)
        
        # Robustness: inject missing IDs or durations if missing at top level
        if "video_analysis" in composite:
            va = composite["video_analysis"]
            if not va.get("id"):
                va["id"] = str(uuid.uuid4())
            if not va.get("duration"):
                va["duration"] = duration_for_prompt
        if "video_ideas" in composite:
            for idea in composite["video_ideas"]:
                if not idea.get("idea_id"):
                    idea["idea_id"] = str(uuid.uuid4())
        if "best_idea" in composite and not composite["best_idea"].get("idea_id"):
            composite["best_idea"]["idea_id"] = str(uuid.uuid4())
        return composite
    except Exception as e:
        logger.error(f"Error parsing Gemini response: {str(e)}")
        return {"error": "Failed to parse Gemini output as JSON.", "raw": output, "exception": str(e)}


def extract_video_ideas(video_bytes, caption, duration=None):
    """
    Extract video ideas using Google Gemini AI.
    
    Analyzes the video and generates:
    - Video analysis (title, keywords, context, transcript)
    - 5 creative video ideas
    - Best idea selection
    - Video generation prompt for the best idea
    
    Args:
        video_bytes (bytes): Video content as bytes
        caption (str): Post/tweet caption for additional context
        duration (float, optional): Video duration in seconds. If not provided, will be calculated.
    
    Returns:
        dict: Structured response containing:
            - video_analysis: dict with id, title, duration, keywords, context, transcript
            - video_ideas: list of 5 idea objects
            - best_idea: selected best idea object
            - video_prompt: dict with prompt, negative_prompt, shot_list
    
    Raises:
        ValueError: If Gemini API key is not configured
        Exception: If API call fails or response is invalid
    """
    # Check if API key is configured
    api_key = getattr(settings, 'GEMINI_API_KEY', '')
    if not api_key:
        raise ValueError("GEMINI_API_KEY is not configured in settings. Please set it in your .env file.")
    
    # Configure Gemini API
    genai.configure(api_key=api_key)
    
    # Get video duration if not provided
    if duration is None:
        duration = get_video_duration(video_bytes)
    
    duration_for_prompt = round(duration or 0, 2)
    
    # Build prompt with caption context
    caption_context = f"\n\nAdditional context from the caption: {caption}" if caption else ""
    
    single_prompt = f"""
You are an expert in video analysis and creative video concept generation.

Given the following video input (mp4, content provided), please:

1. ** Deeply Analyze the video context of the video and the caption ** and output a JSON object with:
   - id: unique uuid4
   - title: concise and descriptive
   - duration: use this value if you cannot determine or as a reference: {duration_for_prompt}
   - keywords: a list of exactly 5 highly relevant keywords
   - context: a detailed, multi-sentence description of what happens in the video
   - transcript: a complete audio transcript; if no speech is present, use "No speech/audio detected"
{caption_context}

2. **Propose 5 unique, creative ideas**, inspired by the video's keywords and context and audio transcript, output as a JSON array. Each idea must be a JSON object with:
   - idea_id: unique uuid4
   - title: short, catchy
   - concept: 1-2 sentences outlining what happens in the new video
   - main visual elements: string (comma-separated bullet points of visuals)
   - intended mood/impact: single line

3. **Select and output the best single idea** (based on originality, creativity, likely audience engagement), as a JSON object.

4. For this best idea, **provide a professional video prompt** for generating a new 10 second social media video, as a JSON object with:
   - prompt: detailed, positive and creative description for a video generator (pacing, visual style, atmosphere, camera moves, product/hook,video timestamp etc.)
   - negative_prompt: things to avoid (bad quality, blurry, off-topic, etc.)
   - shot_list: an array of 4-6 key visual moments or camera moves with timestamp in seconds

**FORMAT:** Respond with a top-level JSON object, containing these top-level fields exactly (no commentary, no extra prose):
{{
  "video_analysis": {{ ...(fields from step 1)... }},
  "video_ideas": [ ...(array from step 2, length 5)... ],
  "best_idea": {{ ...(object from step 3)... }},
  "video_prompt": {{ ...(object from step 4)... }}
}}

RESPONSE MUST BE STRICT VALID JSON. DO NOT add explanations, notes, or any preamble or markdown. All uuids must be v4.
"""
    
    # Initialize Gemini model
    model = genai.GenerativeModel('gemini-2.5-flash')
    
    # Call Gemini API with video bytes
    try:
        response = model.generate_content(
            [
                {
                    "inline_data": {
                        "mime_type": "video/mp4",
                        "data": video_bytes
                    }
                },
                {
                    "text": single_prompt
                }
            ]
        )
        
        # Extract and parse JSON response
        full_result_json = extract_composite_json(response, duration_for_prompt)
        
        # Check for errors in response
        if "error" in full_result_json:
            raise Exception(f"Gemini API error: {full_result_json.get('error')}")
        
        return full_result_json
        
    except Exception as e:
        logger.error(f"Error calling Gemini API: {str(e)}")
        raise

