"""
Anthropic client wrapper.
Provides call_haiku() and call_sonnet() with logging and retry on rate limits.
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
MAX_TOKENS = 4096
MAX_RETRIES = 3

_LOG_FILE = Path(__file__).parent.parent / "logs" / "llm_calls.jsonl"
_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=get_config().anthropic_api_key)
    return _client


def call_haiku(prompt: str, system: str = "") -> str:
    """Call claude-haiku with optional system prompt. Returns response text."""
    return _call(HAIKU_MODEL, prompt, system)


def call_sonnet(prompt: str, system: str = "") -> str:
    """Call claude-sonnet with optional system prompt. Returns response text."""
    return _call(SONNET_MODEL, prompt, system)


def _call(model: str, prompt: str, system: str, _attempt: int = 0) -> str:
    kwargs: dict = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system

    try:
        response = _get_client().messages.create(**kwargs)
        text = response.content[0].text
        _log(model, prompt, system, text, response.usage)
        return text

    except anthropic.RateLimitError:
        if _attempt >= MAX_RETRIES:
            raise
        wait = 5 * (2 ** _attempt)  # 5 → 10 → 20 s
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
