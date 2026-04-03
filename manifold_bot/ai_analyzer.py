"""
AI-powered market analyzer using DeepSeek.

Reads a market question + current state and returns a structured
trading recommendation with confidence and reasoning.

Tries the local Ollama instance first (free, no rate limits),
falls back to the DeepSeek API if Ollama is unavailable.
"""

import json
import os
import re
import time
from typing import Dict, Optional

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
OLLAMA_BASE = "http://localhost:11434"
OLLAMA_MODEL = "deepseek-r1:14b"
DEEPSEEK_API_BASE = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"

SYSTEM_PROMPT = """You are an expert prediction market analyst. Your job is to assess whether a market's current probability is accurate or mispriced.

A prediction market works like this:
- A question has a YES or NO outcome that will resolve in the future
- The current probability (0-100%) reflects what the crowd believes
- If you think the crowd is WRONG and the real probability is HIGHER than shown, bet YES
- If you think the crowd is WRONG and the real probability is LOWER than shown, bet NO
- If the probability looks roughly correct, do NOT trade

Be a contrarian when the evidence supports it. Consider:
- Is the question well-defined and likely to resolve clearly?
- Does the current probability seem too high or too low given what you know?
- Are there reasons the crowd might be systematically biased?
- How confident are you in your assessment?

Always respond with valid JSON only. No markdown, no extra text."""

ANALYSIS_PROMPT = """Analyze this prediction market:

Question: {question}
Current probability: {probability:.0%}
Trading volume: ${volume:.0f}
Liquidity: ${liquidity:.0f}
Close date: {close_date}

Additional context:
{context}

Respond with this exact JSON structure:
{{
  "recommendation": "YES" or "NO" or "SKIP",
  "confidence": <float 0.0 to 1.0>,
  "estimated_true_probability": <float 0.0 to 1.0>,
  "reasoning": "<1-2 sentence explanation>",
  "risk_factors": "<brief note on what could make this trade wrong>"
}}

Only recommend YES or NO if you have genuine conviction the market is mispriced by at least 10 percentage points. Otherwise use SKIP."""


def _call_ollama(prompt: str) -> Optional[str]:
    """Call local Ollama instance."""
    try:
        import requests
        response = requests.post(
            f"{OLLAMA_BASE}/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "prompt": f"{SYSTEM_PROMPT}\n\n{prompt}",
                "stream": False,
                "options": {"temperature": 0.3}
            },
            timeout=60
        )
        if response.status_code == 200:
            return response.json().get("response", "")
    except Exception:
        pass
    return None


def _call_deepseek_api(prompt: str) -> Optional[str]:
    """Call DeepSeek API."""
    if not DEEPSEEK_API_KEY:
        return None
    try:
        import requests
        response = requests.post(
            f"{DEEPSEEK_API_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": DEEPSEEK_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.3,
                "max_tokens": 400
            },
            timeout=30
        )
        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"]
    except Exception:
        pass
    return None


def _parse_response(raw: str) -> Optional[Dict]:
    """Extract JSON from model response, handling reasoning tags."""
    if not raw:
        return None

    # Strip <think>...</think> blocks from reasoning models
    raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()

    # Find the JSON object
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if not match:
        return None

    try:
        parsed = json.loads(match.group())

        recommendation = parsed.get("recommendation", "SKIP").upper()
        if recommendation not in ("YES", "NO", "SKIP"):
            recommendation = "SKIP"

        confidence = float(parsed.get("confidence", 0.0))
        confidence = max(0.0, min(1.0, confidence))

        # If SKIP, force confidence to 0
        if recommendation == "SKIP":
            confidence = 0.0

        return {
            "recommendation": recommendation,
            "confidence": round(confidence, 3),
            "estimated_true_probability": round(
                float(parsed.get("estimated_true_probability", 0.5)), 3
            ),
            "reasoning": str(parsed.get("reasoning", ""))[:300],
            "risk_factors": str(parsed.get("risk_factors", ""))[:200],
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


def analyze_market(market: Dict) -> Optional[Dict]:
    """
    Run AI analysis on a single market.

    Args:
        market: Market dict from Manifold API (must have at least 'question' and 'probability')

    Returns:
        Dict with keys: recommendation, confidence, estimated_true_probability,
        reasoning, risk_factors, source ("ollama" or "deepseek_api")
        Returns None if both backends are unavailable.
    """
    question = market.get("question", "")
    probability = market.get("probability", 0.5)
    volume = market.get("volume", 0)
    liquidity = market.get("totalLiquidity", market.get("liquidity", 0))

    # Format close date
    close_time_ms = market.get("closeTime")
    if close_time_ms:
        from datetime import datetime, timezone
        close_date = datetime.fromtimestamp(
            close_time_ms / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d")
    else:
        close_date = "unknown"

    # Build extra context from available fields
    context_parts = []
    creator = market.get("creatorName") or market.get("creatorUsername")
    if creator:
        context_parts.append(f"Created by: {creator}")
    description = market.get("description") or market.get("textDescription", "")
    if description and isinstance(description, str) and len(description) > 10:
        context_parts.append(f"Description: {description[:300]}")
    if not context_parts:
        context_parts.append("No additional context available.")

    prompt = ANALYSIS_PROMPT.format(
        question=question,
        probability=probability,
        volume=volume,
        liquidity=liquidity,
        close_date=close_date,
        context="\n".join(context_parts),
    )

    # Try Ollama first (free), then DeepSeek API
    raw = _call_ollama(prompt)
    source = "ollama"
    if raw is None:
        raw = _call_deepseek_api(prompt)
        source = "deepseek_api"

    if raw is None:
        return None

    result = _parse_response(raw)
    if result:
        result["source"] = source
        result["market_id"] = market.get("id", "")
    return result


def batch_analyze(markets: list, max_markets: int = 20, delay: float = 1.0) -> Dict[str, Dict]:
    """
    Analyze a batch of markets.

    Args:
        markets: List of market dicts
        max_markets: Cap to avoid rate limits / long runtimes
        delay: Seconds between calls (be polite to APIs)

    Returns:
        Dict keyed by market_id -> analysis result
    """
    results = {}
    count = 0

    for market in markets[:max_markets]:
        market_id = market.get("id", "")
        if not market_id:
            continue

        result = analyze_market(market)
        if result:
            results[market_id] = result
            rec = result["recommendation"]
            conf = result["confidence"]
            source = result["source"]
            print(f"  AI [{source}] {market.get('question', '')[:60]}...")
            print(f"       → {rec} ({conf:.0%} confidence): {result['reasoning'][:80]}")
            # Apply rate-limiting only when using the paid DeepSeek API fallback
            effective_delay = 2.0 if source == "deepseek_api" else delay
        else:
            print(f"  AI analysis unavailable for {market_id}")
            effective_delay = delay

        count += 1
        if count < len(markets[:max_markets]):
            time.sleep(effective_delay)

    return results
