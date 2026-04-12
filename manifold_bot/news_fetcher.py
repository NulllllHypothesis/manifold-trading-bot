"""
News fetcher for Manifold Markets research.

Queries newsapi.org for recent headlines relevant to a market question.
Free tier: 100 requests/day. Results are cached by (market_id, UTC date) so
a market that stays in the top-3 candidates across multiple hourly runs only
costs one API call per day.

Usage:
    from manifold_bot.news_fetcher import fetch_headlines
    headlines = fetch_headlines("abc123", "Will Bitcoin reach $100k by 2026?")
    # Returns: [{"title": "...", "source": "Reuters", "published_at": "...", "age_hours": 6.2}]
    # Returns: [] when NEWS_API_KEY is absent, network fails, or no results found.

Requires NEWS_API_KEY in .env (or environment). Get a free key at newsapi.org/register.
"""

import json
import os
import re
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from manifold_bot.config import NEWS_API_KEY

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE_PATH = os.path.join(_ROOT, "data", "news_cache.json")
_NEWS_API_ENDPOINT = "https://newsapi.org/v2/everything"
_OLLAMA_URL = "http://localhost:11434/api/generate"

# Fetch headlines published within the last N days
_MAX_AGE_DAYS = 7
# Abort if newsapi.org doesn't respond within this many seconds
_TIMEOUT_SECONDS = 5
# Abort keyword extraction via Ollama if it takes longer than this
_OLLAMA_KEYWORD_TIMEOUT = 10
# Maximum articles to retrieve per request (free tier allows up to 100)
_MAX_RESULTS = 5

# Words stripped from questions before building the search query.
# Year tokens, modal verbs, prepositions, and resolution-framing words don't
# help find relevant articles — they just dilute the search with noise.
_STOPWORDS = frozenset({
    'will', 'the', 'a', 'an', 'be', 'by', 'to', 'of', 'in', 'is', 'it',
    'its', 'this', 'that', 'was', 'for', 'on', 'are', 'as', 'at', 'we',
    'if', 'or', 'and', 'but', 'not', 'with', 'from', 'any', 'more', 'get',
    'has', 'have', 'had', 'his', 'her', 'been', 'can', 'who', 'what',
    'when', 'how', 'than', 'they', 'there', 'which', 'does', 'do', 'did',
    # Resolution framing + generic verbs that add no topic signal
    'reach', 'happen', 'occur', 'complete', 'before', 'after', 'until',
    'end', 'year', 'month', 'day', 'first', 'second', 'third', 'fourth',
    'hit', 'make', 'take', 'give', 'come', 'go', 'say', 'new', 'use',
    'win', 'lose', 'start', 'stop', 'get', 'set', 'put', 'run', 'try',
    'above', 'below', 'ever', 'still', 'think', 'receive', 'release',
    # Numeric scale words
    'percent', 'million', 'billion', 'trillion',
    # Year tokens
    '2023', '2024', '2025', '2026', '2027', '2028', '2029', '2030',
    # Months (full and abbreviations)
    'january', 'february', 'march', 'april', 'june',
    'july', 'august', 'september', 'october', 'november', 'december',
    'jan', 'feb', 'mar', 'apr', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec',
    # Quarter labels
    'q1', 'q2', 'q3', 'q4',
})


def _extract_keywords_regex(question: str) -> str:
    """
    Fast regex-based keyword extraction — fallback when Ollama is unavailable.

    Strips punctuation, lowercases, removes stopwords, generic verbs, and
    tokens that contain digits. Takes the first 2 surviving words.
    """
    q = re.sub(r'[^\w\s]', ' ', question.lower())
    words = q.split()
    keywords = [
        w for w in words
        if w not in _STOPWORDS
        and len(w) >= 3
        and not re.search(r'\d', w)
    ]
    return ' '.join(keywords[:2]) if keywords else question[:50]


def _extract_keywords_ollama(question: str) -> str:
    """
    Use the local Ollama model to extract 2-3 high-quality search terms.

    The model sees the market question and returns just the search terms —
    no explanation, no reasoning. This is a ~2-3 second call on a warm
    model, which is acceptable because we only call it 3 times per hourly
    run (once per news candidate).

    Returns "" on any failure (timeout, Ollama down, bad output) so the
    caller can fall back to the regex extractor.
    """
    prompt = (
        "Extract 2-3 search keywords for finding news articles about this "
        "prediction market question. Return ONLY the keywords separated by "
        "spaces, nothing else. No explanation, no punctuation, no quotes.\n\n"
        f"Question: {question}\n\n"
        "Keywords:"
    )

    payload = json.dumps({
        "model": "llama3.2:3b",
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.0,
            "num_predict": 20,   # keywords only — very short output
        },
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            _OLLAMA_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=_OLLAMA_KEYWORD_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        raw = data.get("response", "").strip()
        # Clean: lowercase, strip punctuation, take first 3 tokens
        cleaned = re.sub(r'[^\w\s]', ' ', raw.lower()).split()
        # Filter out anything that looks like filler the model might add
        cleaned = [w for w in cleaned if w not in _STOPWORDS and len(w) >= 2]
        result = ' '.join(cleaned[:3])
        return result if result else ""

    except Exception:
        return ""


def extract_keywords(question: str) -> str:
    """
    Extract search keywords from a market question for NewsAPI queries.

    Tries Ollama first (smarter — understands the topic entity), falls back
    to regex (fast, always works) if Ollama is unavailable or returns garbage.

    Examples (Ollama):
        "Will Bitcoin hit $150k by December 2026?"       → "bitcoin price"
        "Next James Bond is Callum Turner or Aaron...?"  → "james bond casting"
        "Will Project Hail Mary receive Oscar noms?"     → "project hail mary oscar"

    Examples (regex fallback):
        "Will Bitcoin hit $150k by December 2026?"       → "bitcoin"
        "Next James Bond is Callum Turner or Aaron...?"  → "next james"
    """
    # Try Ollama — returns "" on failure
    result = _extract_keywords_ollama(question)
    if result:
        return result

    # Fallback to regex
    return _extract_keywords_regex(question)


def _load_cache() -> dict:
    """Load the news cache from disk. Returns {} on any read error."""
    try:
        with open(_CACHE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    """Persist the news cache to disk. Silently skips on write error."""
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        with open(_CACHE_PATH, 'w') as f:
            json.dump(cache, f)
    except Exception:
        pass


def fetch_headlines(market_id: str, question: str) -> list:
    """
    Fetch recent news headlines for a market question.

    Checks a daily cache keyed by (market_id, UTC-date) before hitting the API.
    A market that stays in the top-N candidates across multiple hourly runs only
    costs one NewsAPI request per day.

    Args:
        market_id: Manifold market ID (used as cache key)
        question:  Market question text (used for keyword extraction)

    Returns:
        List of up to _MAX_RESULTS headline dicts:
          {"title": str, "source": str, "published_at": str, "age_hours": float}
        Returns [] when:
          - NEWS_API_KEY is not set
          - Network request fails or times out
          - NewsAPI returns an error status
          - No articles found for the extracted keywords
    """
    if not NEWS_API_KEY:
        return []

    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    cache_key = f"{market_id}:{today}"

    cache = _load_cache()
    if cache_key in cache:
        return cache[cache_key]

    keywords = extract_keywords(question)
    if not keywords:
        return []

    from_date = (datetime.now(timezone.utc) - timedelta(days=_MAX_AGE_DAYS)).strftime('%Y-%m-%d')
    params = urllib.parse.urlencode({
        'q': keywords,
        'from': from_date,
        'sortBy': 'publishedAt',
        'pageSize': _MAX_RESULTS,
        'apiKey': NEWS_API_KEY,
        'language': 'en',
    })
    url = f"{_NEWS_API_ENDPOINT}?{params}"

    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'ManifoldBot/1.0'})
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except Exception:
        return []

    if data.get('status') != 'ok':
        return []

    now = datetime.now(timezone.utc)
    results = []
    for article in data.get('articles', []):
        published_str = article.get('publishedAt', '')
        try:
            published = datetime.fromisoformat(published_str.replace('Z', '+00:00'))
            age_hours = (now - published).total_seconds() / 3600
        except Exception:
            age_hours = float(_MAX_AGE_DAYS * 24)

        title = (article.get('title') or '').encode('ascii', errors='replace').decode('ascii').strip()
        if not title or title == '[Removed]':
            continue

        results.append({
            'title': title[:200],
            'source': ((article.get('source') or {}).get('name') or 'Unknown')[:50],
            'published_at': published_str,
            'age_hours': round(age_hours, 1),
        })

    cache[cache_key] = results
    _save_cache(cache)
    return results
