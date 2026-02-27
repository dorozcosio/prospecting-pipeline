"""
Integration test for Pipeline 1, Step 1: URL discovery.

Runs discover_urls for MIT only and prints results for manual inspection.
"""
from src.config import Config, load_config
from src.pipeline1.step1_url_discovery import discover_urls

INSTITUTION = "MIT"


def _mit_only_config() -> Config:
    config = load_config()
    return Config(
        institutions=[i for i in config.institutions if i.name == INSTITUTION],
        settings=config.settings,
        google_service_account_key_path=config.google_service_account_key_path,
        google_sheet_id=config.google_sheet_id,
        anthropic_api_key=config.anthropic_api_key,
        serpapi_api_key=config.serpapi_api_key,
    )


def test_discover_urls_mit():
    result = discover_urls(_mit_only_config())

    assert result, "discover_urls returned an empty dict"
    assert INSTITUTION in result, f"Expected '{INSTITUTION}' key, got: {list(result.keys())}"

    urls = result[INSTITUTION]
    assert urls, f"No URLs discovered for {INSTITUTION}"

    for entry in urls:
        assert "url" in entry, f"Missing 'url' field: {entry}"
        assert "snippet" in entry, f"Missing 'snippet' field: {entry}"
        assert "source_query" in entry, f"Missing 'source_query' field: {entry}"

    # Print for manual quality inspection
    print(f"\n{'='*60}")
    print(f"Discovered {len(urls)} URL(s) for {INSTITUTION}:")
    print(f"{'='*60}")
    for i, entry in enumerate(urls, 1):
        print(f"\n[{i}] {entry['url']}")
        print(f"     query  : {entry['source_query']}")
        print(f"     snippet: {entry['snippet'][:120]}")
