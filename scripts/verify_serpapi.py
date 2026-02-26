"""
Verify SerpAPI connectivity.
Runs a test Google Scholar search and prints the first result.
"""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

API_KEY = os.getenv("SERPAPI_API_KEY")

if not API_KEY:
    print("ERROR: SERPAPI_API_KEY must be set in .env")
    sys.exit(1)

import json
import urllib.request
import urllib.parse

QUERY = "Stanford computer science professor machine learning"

def main():
    params = urllib.parse.urlencode({
        "engine": "google_scholar",
        "q": QUERY,
        "api_key": API_KEY,
        "num": 3,
    })
    url = f"https://serpapi.com/search?{params}"

    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.loads(resp.read())

    results = data.get("organic_results", [])
    if not results:
        print("✗ SerpAPI returned no results — check your key or quota")
        sys.exit(1)

    first = results[0]
    print(f"✓ SerpAPI (Google Scholar) responded with {len(results)} results")
    print(f"  First result: {first.get('title', 'N/A')}")

if __name__ == "__main__":
    main()
