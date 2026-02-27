"""
Anthropic client wrapper.

Provides call_haiku() and call_sonnet() with:
  - Per-model pacing to respect Free Tier rate limits (5 RPM per model)
  - Retry on 429 with Retry-After header support
  - Token estimation and batch-building helpers
  - Full JSONL logging of every call
"""
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from src.config import get_config

logger = logging.getLogger(__name__)

HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 8192
MAX_RETRIES = 5

_LOG_FILE = Path(__file__).parent.parent / "logs" / "llm_calls.jsonl"
_client: anthropic.Anthropic | None = None

# Per-model timestamp of the last completed API call (monotonic clock).
# Haiku and Sonnet have SEPARATE rate limit pools on Free Tier, so we track
# them independently and only block calls to the SAME model.
_last_call_time: dict[str, float] = {}


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=get_config().anthropic_api_key)
    return _client


def _get_delay(model: str) -> float:
    """Return the configured minimum inter-call delay for this model (seconds)."""
    settings = get_config().settings
    if HAIKU_MODEL in model:
        return float(settings.haiku_call_delay)
    return float(settings.sonnet_call_delay)


def _pace(model: str) -> None:
    """
    Sleep if needed so that the gap between consecutive calls to the same
    model is at least the configured minimum.  Calls to different models
    are never blocked by each other.
    """
    delay = _get_delay(model)
    now = time.monotonic()
    last = _last_call_time.get(model, 0.0)
    elapsed = now - last
    if elapsed < delay:
        wait = delay - elapsed
        logger.info("Pacing: waiting %.1fs before next %s call", wait, model)
        time.sleep(wait)


def call_haiku(prompt: str, system: str = "") -> str:
    """Call claude-haiku with optional system prompt. Returns response text."""
    return _call(HAIKU_MODEL, prompt, system)


def call_sonnet(prompt: str, system: str = "") -> str:
    """Call claude-sonnet with optional system prompt. Returns response text."""
    return _call(SONNET_MODEL, prompt, system)


def _call(model: str, prompt: str, system: str, _attempt: int = 0) -> str:
    _pace(model)

    kwargs: dict = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system

    try:
        response = _get_client().messages.create(**kwargs)
        _last_call_time[model] = time.monotonic()
        text = response.content[0].text
        _log(model, prompt, system, text, response.usage)
        return text

    except anthropic.RateLimitError as exc:
        if _attempt >= MAX_RETRIES:
            raise
        # Respect the Retry-After header when present; otherwise use
        # exponential backoff starting at 60 s (one full TPM window).
        retry_after = None
        if hasattr(exc, "response") and exc.response is not None:
            retry_after = exc.response.headers.get("retry-after")
        wait = int(retry_after) if retry_after else 60 * (2 ** _attempt)
        logger.warning(
            "Rate limit on %s — retrying in %ds (attempt %d/%d)",
            model, wait, _attempt + 1, MAX_RETRIES,
        )
        time.sleep(wait)
        return _call(model, prompt, system, _attempt + 1)


def _log(model: str, prompt: str, system: str, response: str, usage) -> None:
    _LOG_FILE.parent.mkdir(exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "system": system,
        "prompt": prompt,
        "response": response,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
    }
    with _LOG_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English text."""
    return len(text) // 4


def build_batches(
    items: list[str],
    max_items: int,
    max_tokens: int = 8000,
) -> list[list[int]]:
    """
    Group item indices into batches that respect both item count and token budget.
    max_tokens default 8000 leaves headroom under the 10K input TPM limit.

    Args:
        items:      List of text strings (one per item to batch).
        max_items:  Maximum number of items per batch.
        max_tokens: Maximum total tokens per batch (estimated).

    Returns:
        List of index lists, e.g. [[0, 1, 2], [3, 4], [5, 6, 7]].
    """
    batches: list[list[int]] = []
    current_batch: list[int] = []
    current_tokens = 0

    for i, item in enumerate(items):
        item_tokens = estimate_tokens(item)
        if current_batch and (
            len(current_batch) >= max_items
            or current_tokens + item_tokens > max_tokens
        ):
            batches.append(current_batch)
            current_batch = []
            current_tokens = 0
        current_batch.append(i)
        current_tokens += item_tokens

    if current_batch:
        batches.append(current_batch)

    return batches
