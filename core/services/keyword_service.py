"""
Keyword extraction service for Reddit posts using sentence transformers.
Uses BGE embeddings to extract the best keyword phrase from text.
"""
import re
import logging
from typing import List, Dict, Tuple, Optional
import numpy as np

logger = logging.getLogger(__name__)

# Try to import sentence_transformers, but make it optional
try:
    from sentence_transformers import SentenceTransformer
    MODEL_AVAILABLE = True
    # Lazy load model
    _model = None
    
    def get_model():
        """Lazy load the sentence transformer model."""
        global _model
        if _model is None:
            logger.info("Loading embedding model: BAAI/bge-small-en")
            _model = SentenceTransformer("BAAI/bge-small-en")
        return _model
except ImportError:
    MODEL_AVAILABLE = False
    logger.warning("sentence-transformers not installed. Keyword extraction will be disabled.")
    
    def get_model():
        raise ImportError("sentence-transformers is required for keyword extraction")


# Stopwords for filtering
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


def get_candidate_phrases(text: str, max_phrases: int = 15) -> List[str]:
    """
    Build candidate phrases (n-grams) from the text tokens:
    1, 2, and 3 word phrases.
    """
    tokens = tokenize_title(text)
    if not tokens:
        return []

    phrases = set()
    n_tokens = len(tokens)
    for n in range(1, min(4, n_tokens + 1)):  # 1, 2, 3-grams
        for i in range(n_tokens - n + 1):
            phrase = " ".join(tokens[i:i + n])
            phrases.add(phrase)

    # Sort: longer phrases first (more expressive), then alphabetically
    phrases_list = sorted(phrases, key=lambda x: (-len(x.split()), x))
    return phrases_list[:max_phrases]


def best_keyword_for_text(
    text: str,
    min_similarity: float = 0.30
) -> Tuple[Optional[str], float]:
    """
    Use BGE embeddings to pick the single best phrase that represents the text.
    The phrase is always built from the text itself.
    Returns (best_phrase or None, similarity).
    """
    if not MODEL_AVAILABLE:
        return None, 0.0
    
    text = text.strip()
    if not text:
        return None, 0.0

    try:
        candidates = get_candidate_phrases(text)
        if not candidates:
            return None, 0.0

        model = get_model()
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
    except Exception as e:
        logger.error(f"Error extracting keyword: {e}", exc_info=True)
        return None, 0.0


def extract_keywords(post_text: str, num_keywords: int = 5) -> List[Dict]:
    """
    Extract keywords from post text using semantic similarity.
    
    Args:
        post_text: The text content of the post
        num_keywords: Number of keywords to extract (currently returns best one)
    
    Returns:
        List of keyword dictionaries with 'keyword' and 'similarity' fields
    """
    if not MODEL_AVAILABLE:
        logger.warning("Keyword extraction disabled: sentence-transformers not installed")
        return []
    
    logger.info(f"Extracting keywords from text (length: {len(post_text)})")
    
    keyword, similarity = best_keyword_for_text(post_text)
    
    if keyword:
        return [{"keyword": keyword, "similarity": similarity}]
    else:
        return []
