"""
AI-powered market analyzer using DeepSeek.

Reads a market question + current state and returns a structured
trading recommendation with confidence and reasoning.

Tries the local Ollama instance first (free, no rate limits),
falls back to the DeepSeek API if Ollama is unavailable.
"""

import concurrent.futures
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

LLM_LOG_DIR = Path(os.environ.get('LLM_LOG_DIR', Path(__file__).resolve().parent.parent / 'logs'))
OLLAMA_BASE = "http://localhost:11434"
OLLAMA_MODEL = "llama3.2:3b"
DEEPSEEK_API_BASE = "https://api.deepseek.com/v1"
# Switched from "deepseek-chat" to "deepseek-v4-flash" on 2026-04-27.
# Per https://api-docs.deepseek.com/quick_start/pricing the older
# "deepseek-chat" alias "will be deprecated in the future" and now
# "corresponds to non-thinking mode of deepseek-v4-flash" — i.e. they map
# to the same backend today, but only "deepseek-v4-flash" is guaranteed to
# keep working when the alias is retired. Same auth, same endpoint, no
# breaking changes. Use "deepseek-v4-pro" (thinking mode) only for offline
# backfill where extra reasoning latency is acceptable.
DEEPSEEK_MODEL = "deepseek-v4-flash"

# DeepSeek pricing in USD per 1M tokens, keyed by model id.
# Source: https://api-docs.deepseek.com/quick_start/pricing (checked 2026-06-12).
# DeepSeek's usage payload splits prompt tokens into cache-hit and cache-miss
# (prompt_tokens == prompt_cache_hit_tokens + prompt_cache_miss_tokens), and
# the two are billed at very different rates, so we price them separately.
# The deprecated aliases "deepseek-chat"/"deepseek-reasoner" map to the
# v4-flash backend until their retirement (2026-07-24) and are priced the same.
DEEPSEEK_PRICING_USD_PER_MTOK = {
    "deepseek-v4-flash": {
        "input_cache_hit": 0.0028,
        "input_cache_miss": 0.14,
        "output": 0.28,
    },
    "deepseek-v4-pro": {
        "input_cache_hit": 0.003625,
        "input_cache_miss": 0.435,
        "output": 0.87,
    },
    "deepseek-chat": {           # alias of v4-flash non-thinking
        "input_cache_hit": 0.0028,
        "input_cache_miss": 0.14,
        "output": 0.28,
    },
    "deepseek-reasoner": {       # alias of v4-flash thinking
        "input_cache_hit": 0.0028,
        "input_cache_miss": 0.14,
        "output": 0.28,
    },
}


def _compute_deepseek_cost_usd(usage: Optional[Dict], model: str = DEEPSEEK_MODEL) -> Optional[float]:
    """Compute the USD cost of one DeepSeek call from its API-reported usage.

    Args:
        usage: the ``usage`` object from a DeepSeek chat-completion response.
               Expected fields (https://api-docs.deepseek.com/api/create-chat-completion):
                 prompt_cache_hit_tokens, prompt_cache_miss_tokens,
                 prompt_tokens, completion_tokens
        model: DeepSeek model id used for the call (pricing lookup key).

    Returns:
        Cost in USD (float, >= 0), or None when the cost cannot be computed
        honestly — usage missing/malformed or unknown model. Callers should
        OMIT the inference_cost field in that case rather than guess, so the
        scorekeeper's flat-estimate fallback applies
        (see workbench scorekeeper.adapters.manifold_bot: an explicit
        inference_cost on a trade record wins over --inference-cost-per-ai-call).
    """
    if not isinstance(usage, dict):
        return None
    pricing = DEEPSEEK_PRICING_USD_PER_MTOK.get(model)
    if pricing is None:
        return None
    try:
        hit = usage.get("prompt_cache_hit_tokens")
        miss = usage.get("prompt_cache_miss_tokens")
        if hit is None and miss is None:
            # No cache breakdown (older payload shape): bill all prompt
            # tokens at the cache-miss rate — a conservative upper bound.
            hit = 0
            miss = usage.get("prompt_tokens")
        if miss is None:
            return None
        completion = usage.get("completion_tokens", 0) or 0
        hit = int(hit or 0)
        miss = int(miss)
        completion = int(completion)
        if hit < 0 or miss < 0 or completion < 0:
            return None
        cost = (
            hit * pricing["input_cache_hit"]
            + miss * pricing["input_cache_miss"]
            + completion * pricing["output"]
        ) / 1_000_000
        # Round to 8 decimals: sub-cent costs stay meaningful, JSON stays tidy.
        return round(cost, 8)
    except (TypeError, ValueError):
        return None


# JSON Schema for the structured market-analysis output. Passed to Ollama
# via `format=<schema>` to enable constrained decoding (Ollama 0.5+) —
# the model literally cannot emit non-JSON or non-conforming JSON, including
# unexpected fields (additionalProperties: False).
#
# Note on DeepSeek: this schema is NOT passed to DeepSeek and is NOT used
# to validate DeepSeek output. DeepSeek only supports
# `response_format={"type":"json_object"}` (syntax-only). The DeepSeek leg
# relies on `_parse_response`'s manual per-field extraction-with-clamps to
# accept the response — the existing logic tolerates missing/wrong-typed
# fields by clamping or defaulting (e.g. invalid `recommendation` → SKIP,
# `confidence` clamped to [0, 1]). That manual path is the de-facto safety
# net; this schema constant is the source of truth for what shape we ASK
# for, not what we enforce on DeepSeek's reply.
#
# Source for Ollama format: https://docs.ollama.com/capabilities/structured-outputs
# Source for DeepSeek json_object: https://api-docs.deepseek.com/api/create-chat-completion
ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "recommendation": {
            "type": "string",
            "enum": ["YES", "NO", "SKIP"],
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
        },
        "estimated_true_probability": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
        },
        "reasoning": {"type": "string"},
        "risk_factors": {"type": "string"},
    },
    "required": [
        "recommendation",
        "confidence",
        "estimated_true_probability",
        "reasoning",
        "risk_factors",
    ],
    # Reject extra keys outright. Constrained decoding then can't even
    # generate them, eliminating any noise from chatty model outputs.
    "additionalProperties": False,
}


def _get_deepseek_api_key() -> str:
    """Read DEEPSEEK_API_KEY from os.environ at CALL time, not import time.

    Previously this was a module-level constant captured at import. That worked
    in the standard cron path only because manifold_api's transitive import of
    config.py happened to load .env before ai_analyzer was imported — an
    accidental ordering contract. Any caller that imported ai_analyzer first
    (e.g. a future ai_backfill cron, or an isolated unit test) silently got an
    empty key and the DeepSeek fallback never fired.

    Reading at call time removes the ordering dependency. config.py loads .env
    via os.environ.setdefault on first import, and we read os.environ here, so
    as long as anything has imported config before the first AI call, this
    returns the right value. Costs one dict lookup per AI call.
    """
    return os.environ.get("DEEPSEEK_API_KEY", "")

# Path to calibration table (generated by scripts/analyze_calibration.py)
_CALIBRATION_TABLE_PATH = Path(__file__).resolve().parent.parent / "data" / "calibration_table.json"

# Cached at import time so we don't re-read the JSON on every AI call.
_CALIBRATION_DATA: dict = {}
try:
    with open(_CALIBRATION_TABLE_PATH) as _f:
        _CALIBRATION_DATA = json.load(_f)
except Exception:
    pass   # falls back to empty dict → no calibration note injected


def _build_query_calibration_note(question: str, probability: float) -> str:
    """
    Build a market-specific calibration prior for the AI prompt.

    Lookup order (most-specific first, fall back if sample size too small):
      1. (inferred_category, probability_bucket) — needs n ≥ min_cell_samples
      2. global probability_bucket                — needs reliable=True
      3. empty string                             — no data yet

    This replaces the old global table injection. The AI now sees something like:
      "Historical prior for this market:
        Category: sports | Probability range: 50–60%
        Past Manifold markets like this: 61 resolved
        Crowd said ~55%, actually resolved YES 52.5% → crowd overestimates by +2.5pp
        Suggested adjustment: treat current 58% as closer to 55.5%."

    instead of a full 10-row global table that mostly describes unrelated markets.

    Strategy weights are intentionally NOT included here. They belong only in the
    stat-scoring layer (auto_research.py) and the sizing layer (auto_trader.py).
    """
    from manifold_bot.strategies import _infer_market_category

    if not _CALIBRATION_DATA:
        return ""

    category = _infer_market_category(question)
    prob_pct  = max(0.0, min(1.0, probability))
    bucket_idx = min(int(prob_pct * 10), 9)   # 0–9
    bucket_low  = bucket_idx * 0.10
    bucket_high = bucket_low + 0.10

    # Try specific (category, bucket) cell first
    cell = None
    for c in _CALIBRATION_DATA.get("by_category_bucket", []):
        if (c["category"] == category
                and abs(c["bucket_low"] - bucket_low) < 0.001
                and c.get("reliable")):
            cell = c
            break

    # Fall back to global bucket
    if cell is None:
        for b in _CALIBRATION_DATA.get("buckets", []):
            if (abs(b["bucket_low"] - bucket_low) < 0.001
                    and b.get("reliable")):
                cell = b
                break

    if cell is None:
        return ""

    n       = cell["sample_size"]
    yes_pct = round((cell["actual_yes_rate"] or 0) * 100, 1)
    crowd   = round((cell.get("crowd_midpoint") or (bucket_low + 0.05)) * 100, 0)
    bias    = (cell["bias"] or 0) * 100
    adj     = round(prob_pct * 100 - bias, 1)
    source  = f"category: {category}" if "category" in cell else "global average"

    if bias > 0:
        bias_direction = f"crowd overestimates YES by {bias:+.1f}pp"
    elif bias < 0:
        bias_direction = f"crowd underestimates YES by {abs(bias):.1f}pp"
    else:
        bias_direction = "crowd is well calibrated (0pp bias)"

    lines = [
        f"Historical prior for this market ({source}, {int(bucket_low*100)}–{int(bucket_high*100)}% bucket):",
        f"  Past Manifold markets like this: {n} resolved",
        f"  Crowd said ~{crowd:.0f}%, actually resolved YES {yes_pct:.1f}% → {bias_direction}",
        f"  Suggested adjustment: treat the current {prob_pct*100:.0f}% as closer to {adj:.1f}%.",
        f"  Adjust your estimated_true_probability accordingly before deciding YES/NO/SKIP.",
    ]
    return "\n".join(lines)

# Maximum size for llm_calls.jsonl before rotation (100 MB)
LLM_LOG_MAX_BYTES = 100 * 1024 * 1024
# Number of rotated backup files to keep
LLM_LOG_BACKUP_COUNT = 3

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a disciplined, well-calibrated prediction-market analyst. Your job is to decide whether a market's current probability is mispriced enough to trade — and most of the time, it is not.

A prediction market works like this:
- A question resolves YES or NO in the future.
- The current probability (0-100%) is the crowd's aggregate estimate. On Manifold this crowd is usually well-informed and hard to beat. Treat it as a STRONG PRIOR, not a number to nudge.
- Bet YES only if your own estimate of the true probability is clearly HIGHER than the market's, for a specific reason.
- Bet NO only if your own estimate is clearly LOWER, for a specific reason.
- If the price looks roughly right, or your reason is vague, do NOT trade.

How to think before deciding:
1. Estimate the TRUE probability yourself from the base rate / reference class for this kind of event and the concrete evidence given. Form this estimate on its own merits — do not just echo or lightly nudge the current price.
2. Compare your independent estimate to the market price.
3. Trade ONLY when they diverge by a clear margin AND you can name a specific, verifiable reason the crowd is wrong. "Momentum", "sentiment", "feels overpriced/underpriced", or restating the price are NOT reasons — those are SKIP.
4. Guard against your own overconfidence: a weak reason attached to a big number is still a bad trade. When genuinely uncertain, SKIP.

Confidence means: out of 10 independent markets where you felt this certain, how many would you actually get right? Report it honestly — a true coin-flip is 0.5, not 0.8.

Always respond with valid JSON only. No markdown, no extra text."""

# Stable identifier for the system prompt; recomputed once at import time.
# Recording the hash instead of the full text avoids duplicating the static
# prompt on every log record while still allowing offline verification that
# the prompt has not changed between runs.
SYSTEM_PROMPT_SHA256 = hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()

ANALYSIS_PROMPT = """Analyze this prediction market:

Question: {question}
Current probability: {probability:.0%}
Trading volume: ${volume:.0f}
Liquidity: ${liquidity:.0f}
Close date: {close_date}

Additional context:
{context}

{news_note}{calibration_note}

First, estimate the true probability yourself from the base rate for this kind of event and the specific evidence above — do not just nudge the current price. Then compare your estimate to the market.

Respond with this exact JSON structure:
{{
  "recommendation": "YES" or "NO" or "SKIP",
  "confidence": <float 0.0 to 1.0>,
  "estimated_true_probability": <float 0.0 to 1.0>,
  "reasoning": "<your independent estimate, and the specific concrete reason it differs from the market — or why you SKIP>",
  "risk_factors": "<brief note on what could make this trade wrong>"
}}

Recommend YES or NO only when your independent estimate diverges from the market by at least 10 percentage points AND you can name a specific, verifiable reason. If your reason is vague, generic, or just restates the price, use SKIP. Defaulting to SKIP is correct and expected for most markets."""


def _rotate_log_if_needed(log_path: Path) -> None:
    """
    Rotate log_path if it exceeds LLM_LOG_MAX_BYTES.

    Keeps up to LLM_LOG_BACKUP_COUNT numbered backups:
      llm_calls.jsonl.1  (most recent previous)
      llm_calls.jsonl.2
      llm_calls.jsonl.3  (oldest kept)

    The oldest backup beyond the count is deleted.
    """
    if not log_path.exists():
        return
    if log_path.stat().st_size < LLM_LOG_MAX_BYTES:
        return

    # Shift existing backups: .2 -> .3, .1 -> .2 (high to low to avoid clobbering)
    for i in range(LLM_LOG_BACKUP_COUNT, 1, -1):
        src = log_path.parent / (log_path.name + f".{i - 1}")
        older = log_path.parent / (log_path.name + f".{i}")
        if src.exists():
            if older.exists():
                older.unlink()
            src.rename(older)

    # Rename the active log to .1
    dest = log_path.parent / (log_path.name + ".1")
    if dest.exists():
        dest.unlink()
    log_path.rename(dest)


def _log_llm_call(
    model: str,
    system_prompt_hash: str,
    market_id: str,
    parsed_output: Optional[Dict] = None,
    source: str = "unknown",
    latency_ms: Optional[float] = None,
    ollama_timed_out: bool = False,
    ollama_returned_none: bool = False,
    ollama_parse_failed: bool = False,
    deepseek_attempted: bool = False,
    deepseek_returned_value: bool = False,
    deepseek_saved_a_parse_failure: bool = False,
    usage: Optional[Dict] = None,
    inference_cost_usd: Optional[float] = None,
) -> None:
    """Append a JSONL record for every LLM call (for future distillation).

    We deliberately do NOT log the full system prompt (it is static and
    bloats every record) nor the full user prompt (it may contain market
    data whose long-term persistence requires compliance review).

    For the same compliance reasons we also do NOT log the raw LLM response
    verbatim: the model may echo or summarise market data supplied in the
    prompt, so the raw response carries the same sensitivity as the prompt
    itself.  Instead we store only:
      - system_prompt_sha256: allows offline verification that the prompt
        has not changed between runs without duplicating its text.
      - market_id: a stable, non-sensitive identifier that can be joined
        against the market database to reconstruct the prompt if needed.
      - parsed_output: the structured fields extracted from the response
        (recommendation, confidence, etc.) which do not reproduce raw
        market data.

    NOTE: Logging of the raw LLM response (including any chain-of-thought)
    is intentionally omitted pending a compliance review.  The model may
    echo or summarise market data from the user prompt, so storing the raw
    response verbatim raises the same concerns as storing the prompt itself.
    See the issue comment for context.
    """
    try:
        LLM_LOG_DIR.mkdir(parents=True, exist_ok=True)

        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "source": source,                        # "ollama" or "deepseek_api"
            "latency_ms": round(latency_ms, 1) if latency_ms is not None else None,
            "ollama_timed_out": ollama_timed_out,    # True = stuck runner detected
            # PR #29 fallback observability — counted in 24h audits to validate
            # the parse-failure-fallback fix actually moves the parsed_output
            # success rate. Two separate flags so we can distinguish "DeepSeek
            # was tried but didn't help" from "DeepSeek wasn't tried":
            #   ollama_returned_none           = transport failure (existed pre-PR)
            #   ollama_parse_failed            = Ollama returned text but couldn't
            #                                    be parsed (silent-failure mode pre-PR)
            #   deepseek_attempted             = we entered the fallback branch
            #                                    (Ollama failed for any reason)
            #   deepseek_returned_value        = _call_deepseek_api returned non-None
            #                                    (transport+API succeeded; says nothing
            #                                    about whether the response parsed)
            #   deepseek_saved_a_parse_failure = headline metric — DeepSeek produced
            #                                    parseable output specifically after
            #                                    Ollama's parse failure (the new path
            #                                    this PR added)
            "ollama_returned_none": ollama_returned_none,
            "ollama_parse_failed": ollama_parse_failed,
            "deepseek_attempted": deepseek_attempted,
            "deepseek_returned_value": deepseek_returned_value,
            "deepseek_saved_a_parse_failure": deepseek_saved_a_parse_failure,
            # Cost accounting (additive fields, 2026-06). `usage` is the raw
            # token-usage object DeepSeek reported (None for Ollama calls —
            # local inference has no billing meter); inference_cost_usd is the
            # computed USD spend (0.0 for Ollama, None when unknowable).
            # Token counts are billing metadata, not market data, so they are
            # safe to log under the compliance constraints described above.
            "usage": usage,
            "inference_cost_usd": inference_cost_usd,
            "system_prompt_sha256": system_prompt_hash,
            "market_id": market_id,
            # raw_response and chain_of_thought are intentionally excluded
            # pending compliance review.  See docstring above.
            "parsed_output": parsed_output,
        }

        log_path = LLM_LOG_DIR / "llm_calls.jsonl"
        _rotate_log_if_needed(log_path)

        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f'[llm_log] warning: {e}', file=sys.stderr)
        logger.warning('[llm_log] warning: %s', e)


def _call_ollama(prompt: str, meta: Optional[dict] = None) -> Optional[str]:
    """
    Call local Ollama instance using streaming.

    We stream (stream=True) instead of waiting for the full response in one shot
    (stream=False) for a critical reason: when the Ollama runner process is stuck
    in a spin loop (symptom: 700%+ CPU, no useful output), it accepts the TCP
    connection but never writes the first response byte. With stream=False the
    read_timeout is the only protection — it had to be set to 60s to allow normal
    80s generation, meaning a stuck runner blocks for 60s per market (180s for 3).

    With streaming, the connect timeout (5s) catches an unreachable Ollama, and a
    separate FIRST_TOKEN_TIMEOUT (20s) catches a stuck runner: healthy Ollama sends
    the first token within a few seconds of starting generation; a hung process
    never does. Once the first token arrives we read the rest with a generous
    per-chunk timeout since generation can legitimately take 80-90s on CPU.

    With llama3.2:3b at ~9 tok/s, first token arrives in <2s warm or ~40s cold
    (model load from disk). FIRST_TOKEN_TIMEOUT must exceed the cold-load time.

    meta: optional dict; if provided, sets meta['timed_out']=True on timeout
    so the caller can distinguish a stuck runner from other failures.
    """
    FIRST_TOKEN_TIMEOUT = 90   # seconds — cold model load on this hardware takes ~40s; warm is <2s
    PER_CHUNK_TIMEOUT   = 60   # seconds per subsequent chunk — 3b finishes in ~35s at 9 tok/s
    try:
        import requests
        # `format=ANALYSIS_SCHEMA` enables Ollama's FSM-based constrained
        # decoding: the model is forced to emit JSON conforming exactly to
        # the schema. Pre-PR-30 we relied on prompt wording + regex extraction
        # in _parse_response, which produced a 78% null-output rate on
        # llama3.2:3b. With format= the model literally cannot emit non-JSON.
        # Requires Ollama >= 0.5; sandbox runs 0.18.3.
        response = requests.post(
            f"{OLLAMA_BASE}/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "prompt": f"{SYSTEM_PROMPT}\n\n{prompt}",
                "format": ANALYSIS_SCHEMA,
                "stream": True,
                "keep_alive": "4h",  # keep model resident; 4h covers gaps between hourly cron runs
                "options": {"temperature": 0.3}
            },
            timeout=(5, FIRST_TOKEN_TIMEOUT),  # connect=5s, first-byte=90s
            stream=True,
        )
        if response.status_code != 200:
            return None

        full_text = []
        first_chunk = True
        for raw_line in response.iter_lines(chunk_size=None):
            if not raw_line:
                continue
            try:
                chunk = json.loads(raw_line)
            except (json.JSONDecodeError, ValueError):
                continue
            full_text.append(chunk.get("response", ""))
            if chunk.get("done"):
                break
            if first_chunk:
                # After the first token arrives, extend the socket timeout so
                # long CPU inference can finish without hitting the 20s deadline.
                response.raw._fp.fp.raw._sock.settimeout(PER_CHUNK_TIMEOUT)
                first_chunk = False

        return "".join(full_text) if full_text else None

    except Exception as e:
        import requests as _req
        if isinstance(e, (_req.exceptions.Timeout, _req.exceptions.ReadTimeout)):
            if meta is not None:
                meta['timed_out'] = True
    return None


def _call_deepseek_api(prompt: str, meta: Optional[dict] = None) -> Optional[str]:
    """Call DeepSeek API.

    meta: optional dict (same pattern as _call_ollama). When provided and the
    call succeeds, meta['usage'] is set to the API-reported ``usage`` object
    (prompt_cache_hit_tokens / prompt_cache_miss_tokens / completion_tokens…)
    so the caller can compute the real USD cost of the call via
    _compute_deepseek_cost_usd(). Return type is unchanged for existing callers.
    """
    api_key = _get_deepseek_api_key()
    if not api_key:
        return None
    try:
        import requests
        response = requests.post(
            f"{DEEPSEEK_API_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json={
                "model": DEEPSEEK_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.3,
                "max_tokens": 400,
                # DeepSeek json_object mode = guarantee valid JSON syntax.
                # NOT a strict-schema mode (DeepSeek doesn't expose one), so
                # _parse_response's manual per-field extraction-with-clamps is
                # what tolerates missing/wrong-typed fields here (e.g. unknown
                # recommendation → SKIP, confidence clamped to [0, 1]). The
                # ANALYSIS_SCHEMA constant is the source of truth for what we
                # ASK for, but it's not enforced on this reply.
                # Source: https://api-docs.deepseek.com/api/create-chat-completion
                "response_format": {"type": "json_object"},
            },
            timeout=30
        )
        if response.status_code == 200:
            body = response.json()
            if meta is not None:
                # Capture token usage for cost accounting. Guarded so a
                # missing/odd usage object can never break the analysis path.
                usage = body.get("usage")
                if isinstance(usage, dict):
                    meta["usage"] = usage
            return body["choices"][0]["message"]["content"]
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


def _build_news_note(news_context: list) -> str:
    """
    Format recent headlines for injection into the AI prompt.

    Returns a block like:
        Recent news (last 7 days):
          - "Bitcoin hits new ATH" (Reuters, 6h ago)
          - "SEC approves spot ETF" (CoinDesk, 18h ago)

    Returns "" when news_context is empty so the prompt placeholder collapses
    cleanly without leaving a blank section.
    """
    if not news_context:
        return ""
    lines = ["Recent news (last 7 days — use this to update your probability estimate):"]
    for title in news_context[:3]:
        lines.append(f'  - "{title}"')
    return "\n".join(lines) + "\n\n"


def analyze_market(market: Dict, news_context: Optional[list] = None) -> Optional[Dict]:
    """
    Run AI analysis on a single market.

    Args:
        market:       Market dict from Manifold API (must have 'question' and 'probability')
        news_context: Optional list of recent headline title strings to inject into
                      the prompt. Fetched by auto_research.py via news_fetcher and
                      passed through batch_analyze. Absent when NEWS_API_KEY is unset.

    Returns:
        Dict with keys: recommendation, confidence, estimated_true_probability,
        reasoning, risk_factors, source ("ollama" or "deepseek_api")
        Returns None if both backends are unavailable.
    """
    question = market.get("question", "")
    probability = market.get("probability", 0.5)
    volume = market.get("volume", 0)
    liquidity = market.get("totalLiquidity", market.get("liquidity", 0))
    market_id = market.get("id") or ""

    if not market_id:
        logger.warning("analyze_market called with market missing id field")
        market_id = "UNKNOWN"

    # Format close date
    close_time_ms = market.get("closeTime")
    if close_time_ms:
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
        news_note=_build_news_note(news_context or []),
        calibration_note=_build_query_calibration_note(question, probability),
    )

    # Try Ollama first (free), then DeepSeek API.
    # Two distinct Ollama failure modes both trigger the DeepSeek fallback:
    #   1. Transport-level: _call_ollama returned None (connection failed,
    #      timeout, HTTP non-200). Original code already handled this.
    #   2. Parse-level: Ollama returned text but _parse_response couldn't
    #      extract a valid JSON object. Original code returned None here
    #      WITHOUT trying DeepSeek — that's the bug PR #29 fixes.
    # 24h log audit pre-fix: 21/27 calls (~78%) had parsed_output=null,
    # all on Ollama, and DeepSeek never got a chance to save them.
    ollama_meta: dict = {}
    t0 = time.monotonic()

    raw = _call_ollama(prompt, meta=ollama_meta)
    source = "ollama"
    ollama_timed_out = ollama_meta.get('timed_out', False)
    ollama_returned_none = (raw is None)

    # Try parsing whatever Ollama returned (None gives None back trivially).
    result = _parse_response(raw) if raw is not None else None
    ollama_parse_failed = (raw is not None and result is None)

    # Fall back to DeepSeek on EITHER transport-failure (raw is None) or
    # parse-failure (result is None despite raw being non-empty).
    #
    # We log TWO separate flags so 24h audits can distinguish "DeepSeek
    # was tried but didn't help" from "DeepSeek wasn't tried":
    #   deepseek_attempted     — entered the fallback branch (Ollama failed)
    #   deepseek_returned_value — _call_deepseek_api returned non-None
    # The metric reviewer originally flagged (was named fallback_to_deepseek_used)
    # was conflating these — name said "tried", behavior was "returned a value".
    deepseek_attempted = False
    deepseek_returned_value = False
    deepseek_meta: dict = {}
    if result is None:
        deepseek_attempted = True
        ds_raw = _call_deepseek_api(prompt, meta=deepseek_meta)
        if ds_raw is not None:
            deepseek_returned_value = True
            source = "deepseek_api"
            result = _parse_response(ds_raw)

    deepseek_saved_a_parse_failure = (
        ollama_parse_failed and deepseek_returned_value and result is not None
    )

    # Per-call inference cost in USD, from API-reported token usage.
    # Semantics (consumed downstream by the scorekeeper's manifold_bot adapter,
    # which prefers an explicit per-trade inference_cost over its flat
    # --inference-cost-per-ai-call estimate):
    #   ollama       → 0.0 (local inference, zero marginal API spend)
    #   deepseek_api → real cost from usage x published per-token pricing
    #   unknown      → None (usage missing or unpriceable model). Callers must
    #                  then OMIT the field so the flat-estimate fallback applies.
    inference_cost: Optional[float] = None
    if result is not None:
        if source == "ollama":
            inference_cost = 0.0
        else:
            inference_cost = _compute_deepseek_cost_usd(
                deepseek_meta.get("usage"), DEEPSEEK_MODEL
            )

    latency_ms = (time.monotonic() - t0) * 1000

    # Log after parsing so parsed_output is available.
    # raw_response is intentionally not forwarded to _log_llm_call — see
    # that function's docstring for the compliance rationale.
    model = OLLAMA_MODEL if source == "ollama" else DEEPSEEK_MODEL
    _log_llm_call(
        model, SYSTEM_PROMPT_SHA256, market_id,
        parsed_output=result,
        source=source,
        latency_ms=latency_ms,
        ollama_timed_out=ollama_timed_out,
        ollama_returned_none=ollama_returned_none,
        ollama_parse_failed=ollama_parse_failed,
        deepseek_attempted=deepseek_attempted,
        deepseek_returned_value=deepseek_returned_value,
        deepseek_saved_a_parse_failure=deepseek_saved_a_parse_failure,
        usage=deepseek_meta.get("usage"),
        inference_cost_usd=inference_cost,
    )

    if result:
        result["source"] = source
        result["market_id"] = market_id
        # Only attach when known — absent field means "cost unknown", which
        # downstream consumers (scorekeeper adapter) treat as "use estimate".
        if inference_cost is not None:
            result["inference_cost"] = inference_cost
    return result


def batch_analyze(
    markets: list,
    max_markets: int = 20,
    delay: float = 1.0,
    per_market_timeout: float = 90.0,
    news_by_id: Optional[Dict] = None,
) -> Dict[str, Dict]:
    """
    Analyze a batch of markets.

    Args:
        markets:           List of market dicts
        max_markets:       Cap to avoid rate limits / long runtimes
        delay:             Seconds between calls (be polite to APIs)
        per_market_timeout: Max seconds to spend on a single market before skipping it.
        news_by_id:        Optional {market_id: [headline, ...]} from news_fetcher.
                           When present, the headlines are injected into the AI prompt
                           so the model has fresh context beyond its training cutoff.

    Returns:
        Dict keyed by market_id -> analysis result
    """
    results = {}
    count = 0
    # Wall-clock deadline for the entire batch. Each market gets per_market_timeout,
    # but we also enforce a total budget so a slow first market can't eat all slots.
    batch_deadline = time.monotonic() + per_market_timeout * max(len(markets[:max_markets]), 1) + 10

    for market in markets[:max_markets]:
        market_id = market.get("id", "")
        if not market_id:
            continue

        if time.monotonic() > batch_deadline:
            logger.warning("AI batch global timeout reached — stopping early after %d results.", len(results))
            break

        result = None
        news_ctx = (news_by_id or {}).get(market_id)
        # Do NOT use `with ThreadPoolExecutor` here. The context manager calls
        # shutdown(wait=True) on exit — even when a TimeoutError is raised — which
        # blocks the main thread until the background thread's requests.post() call
        # finishes. That means the actual hang is future_timeout + requests_timeout
        # (up to 150s per market, 450s for 3 markets), busting the 300s cron budget.
        # Instead, shut down with wait=False so the background thread is abandoned
        # immediately after timeout and the loop moves on.
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(analyze_market, market, news_ctx)
        try:
            result = future.result(timeout=per_market_timeout)
        except concurrent.futures.TimeoutError:
            logger.warning(
                "AI analysis timed out after %.1fs for market %s (%s) — skipping.",
                per_market_timeout,
                market_id,
                market.get("question", "")[:60],
            )
            result = None
        except Exception as exc:
            logger.warning(
                "AI analysis raised an unexpected error for market %s: %s — skipping.",
                market_id,
                exc,
            )
            result = None
        finally:
            # wait=False: do not block on the background thread. It will clean up
            # on its own once the underlying requests call completes or errors.
            executor.shutdown(wait=False)

        if result:
            results[market_id] = result
            rec = result["recommendation"]
            conf = result["confidence"]
            source = result["source"]
            print(f"  AI [{source}] {market.get('question', '')[:60]}...")
            print(f"       → {rec} ({conf:.0%} confidence): {result['reasoning'][:80]}")
            # DeepSeek API fallback always enforces 2s regardless of caller-supplied delay.
            # This is intentional: delay=0 is safe for Ollama (CPU-bound) but not for a paid API.
            effective_delay = 2.0 if source == "deepseek_api" else delay
        else:
            print(f"  AI analysis unavailable for {market_id}")
            effective_delay = delay

        count += 1
        if count < len(markets[:max_markets]):
            time.sleep(effective_delay)

    return results
