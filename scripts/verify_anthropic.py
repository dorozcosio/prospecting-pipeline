"""
Verify Anthropic API connectivity.
Makes a minimal test call to claude-haiku-4-5-20250929.
"""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

API_KEY = os.getenv("ANTHROPIC_API_KEY")

if not API_KEY:
    print("ERROR: ANTHROPIC_API_KEY must be set in .env")
    sys.exit(1)

import anthropic

MODEL = "claude-haiku-4-5-20250929"

def main():
    client = anthropic.Anthropic(api_key=API_KEY)

    message = client.messages.create(
        model=MODEL,
        max_tokens=64,
        messages=[{"role": "user", "content": "Reply with exactly: connectivity_ok"}],
    )

    reply = message.content[0].text.strip()
    print(f"✓ Anthropic API ({MODEL}) responded: '{reply}'")

if __name__ == "__main__":
    main()
