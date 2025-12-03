"""
Keyword extraction service for Reddit posts using sentence transformers.
Uses BGE embeddings to extract the best keyword phrase from text.
Optimized for concurrent processing with thread-safe model loading.
"""
import re
import logging
from typing import List, Dict, Tuple, Optional
import numpy as np
from threading import Lock

logger = logging.getLogger(__name__)

# Try to import sentence_transformers, but make it optional
try:
    from sentence_transformers import SentenceTransformer
    MODEL_AVAILABLE = True
    # Lazy load model with thread-safe initialization
    _model = None
    _model_lock = Lock()
    
    def get_model():
        """
        Lazy load the sentence transformer model with thread-safe initialization.
        The model is thread-safe once loaded, so we only need to protect the loading phase.
        """
        global _model
        if _model is None:
            with _model_lock:
                # Double-check pattern: another thread might have loaded it while we waited
                if _model is None:
                    logger.info("Loading embedding model: BAAI/bge-small-en")
                    _model = SentenceTransformer("BAAI/bge-small-en")
                    logger.info("Model loaded successfully")
        return _model
except ImportError:
    MODEL_AVAILABLE = False
    logger.warning("sentence-transformers not installed. Keyword extraction will be disabled.")
    
    def get_model():
        raise ImportError("sentence-transformers is required for keyword extraction")


# Configuration constants for keyword extraction
DEFAULT_NUM_KEYWORDS = 5  # Number of keywords to extract per post
MIN_SIMILARITY_THRESHOLD = 0.30  # Minimum similarity score
DIVERSITY_LAMBDA = 0.5  # Balance between relevance (1.0) and diversity (0.0)
MAX_CANDIDATE_PHRASES = 50  # Maximum candidate phrases to consider

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


def get_candidate_phrases(text: str, max_phrases: int = MAX_CANDIDATE_PHRASES) -> List[str]:
    """
    Build candidate phrases (n-grams) from the text tokens:
    1, 2, and 3 word phrases.
    
    Args:
        text: Input text to extract phrases from
        max_phrases: Maximum number of candidate phrases to return (default: 50)
    
    Returns:
        List of candidate phrases, sorted by length (longer first) then alphabetically
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


def extract_diverse_keywords(
    post_text: str, 
    num_keywords: int = DEFAULT_NUM_KEYWORDS,
    min_similarity: float = MIN_SIMILARITY_THRESHOLD,
    diversity_lambda: float = DIVERSITY_LAMBDA
) -> List[Dict]:
    """
    Extract multiple diverse keywords that together represent the full context.
    
    Uses MMR (Maximal Marginal Relevance) approach:
    - Selects keywords that are similar to the text
    - But diverse from already selected keywords
    - Ensures coverage of different aspects of the caption
    
    Args:
        post_text: The caption text
        num_keywords: Number of keywords to extract (default 5)
        min_similarity: Minimum similarity score threshold
        diversity_lambda: Balance between relevance (1.0) and diversity (0.0)
    
    Returns:
        List of keyword dictionaries with 'keyword' and 'similarity' fields,
        ordered by relevance score (highest first)
    """
    if not MODEL_AVAILABLE:
        logger.warning("Keyword extraction disabled: sentence-transformers not installed")
        return []
    
    post_text = post_text.strip()
    if not post_text:
        return []
    
    try:
        # Generate candidate phrases from the text
        candidates = get_candidate_phrases(post_text)
        if not candidates:
            return []
        
        # Limit candidates to reasonable number for performance
        candidates = candidates[:MAX_CANDIDATE_PHRASES]
        
        model = get_model()
        
        # Prepare texts for embedding: full text first, then all candidates
        texts = [post_text] + candidates
        # Use larger batch size for better performance (model can handle it)
        # Batch size of 64 is optimal for most GPU/CPU setups
        embs = model.encode(texts, normalize_embeddings=True, batch_size=64, show_progress_bar=False)
        embs = np.array(embs)
        
        text_emb = embs[0]      # Full text embedding (dim,)
        cand_embs = embs[1:]    # Candidate phrase embeddings (N, dim)
        
        # Calculate similarity scores for all candidates
        # Cosine similarity = dot product since embeddings are normalized
        sims = cand_embs @ text_emb  # shape (N,)
        
        # Filter candidates by minimum similarity threshold
        valid_indices = np.where(sims >= min_similarity)[0]
        if len(valid_indices) == 0:
            # If no candidates meet threshold, return empty list
            return []
        
        # Start with the best candidate (highest similarity)
        selected_indices = []
        remaining_indices = list(valid_indices)
        
        # Select first keyword: highest similarity to caption
        best_idx = int(np.argmax(sims[valid_indices]))
        first_idx = valid_indices[best_idx]
        selected_indices.append(first_idx)
        remaining_indices.remove(first_idx)
        
        # Select remaining keywords using MMR
        # MMR = λ * sim(candidate, caption) - (1-λ) * max(sim(candidate, selected))
        for _ in range(min(num_keywords - 1, len(remaining_indices))):
            if not remaining_indices:
                break
            
            best_mmr = -np.inf
            best_candidate_idx = None
            
            for cand_idx in remaining_indices:
                # Relevance: similarity to caption
                relevance = sims[cand_idx]
                
                # Diversity: maximum similarity to already selected keywords
                if selected_indices:
                    max_sim_to_selected = max([
                        float(cand_embs[cand_idx] @ cand_embs[sel_idx])
                        for sel_idx in selected_indices
                    ])
                else:
                    max_sim_to_selected = 0.0
                
                # MMR score: balance relevance and diversity
                mmr_score = (diversity_lambda * relevance) - ((1 - diversity_lambda) * max_sim_to_selected)
                
                if mmr_score > best_mmr:
                    best_mmr = mmr_score
                    best_candidate_idx = cand_idx
            
            if best_candidate_idx is not None:
                selected_indices.append(best_candidate_idx)
                remaining_indices.remove(best_candidate_idx)
            else:
                break
        
        # Build result list with selected keywords, sorted by similarity (highest first)
        results = []
        for idx in selected_indices:
            results.append({
                "keyword": candidates[idx],
                "similarity": float(sims[idx])
            })
        
        # Sort by similarity descending
        results.sort(key=lambda x: x["similarity"], reverse=True)
        
        logger.info(f"Extracted {len(results)} diverse keywords from text (length: {len(post_text)})")
        return results
        
    except Exception as e:
        logger.error(f"Error extracting diverse keywords: {e}", exc_info=True)
        return []


def extract_keywords(post_text: str, num_keywords: int = DEFAULT_NUM_KEYWORDS) -> List[Dict]:
    """
    Extract keywords from post text using semantic similarity with diversity.
    
    This function extracts multiple diverse keywords (3-5 by default) that together
    provide comprehensive context understanding of the caption.
    
    Args:
        post_text: The text content of the post
        num_keywords: Number of keywords to extract (default 5)
    
    Returns:
        List of keyword dictionaries with 'keyword' and 'similarity' fields,
        ordered by relevance score (highest first)
    """
    if not MODEL_AVAILABLE:
        logger.warning("Keyword extraction disabled: sentence-transformers not installed")
        return []
    
    logger.info(f"Extracting keywords from text (length: {len(post_text)})")
    
    # Use diverse keyword extraction to get multiple context-aware keywords
    return extract_diverse_keywords(post_text, num_keywords=num_keywords)
