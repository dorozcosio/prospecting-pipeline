"""
Unit and integration tests for pipeline_rescrape.py.

Tests:
  test_score_lab_url            — URL scoring heuristics
  test_diff_members             — new/missing member detection
  test_rescrape_integration_dry_run — full pipeline dry-run (no sheet writes)

All tests run without real API calls or a live sheet.
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.pipeline_rescrape import diff_members, score_lab_url, rescrape_and_enrich


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_config(
    boost_keywords=None,
    penalize_domains=None,
    max_candidates: int = 3,
) -> MagicMock:
    """Return a minimal Config mock suitable for rescrape tests."""
    cfg = MagicMock()
    cfg.settings.rescrape.lab_url_boost_keywords = boost_keywords or [
        "lab", "group", "research"
    ]
    cfg.settings.rescrape.lab_url_penalize_domains = penalize_domains or [
        "scholar.google.com",
        "pubmed.ncbi.nlm.nih.gov",
        "linkedin.com",
        "researchgate.net",
    ]
    cfg.settings.rescrape.max_lab_search_candidates = max_candidates
    cfg.settings.haiku_batch_size = 5
    cfg.settings.sonnet_batch_size = 5
    cfg.institutions = []
    return cfg


# ---------------------------------------------------------------------------
# 1. test_score_lab_url
# ---------------------------------------------------------------------------

def test_score_lab_url():
    config = _make_config()

    # High-score case: PI last name in URL + .edu + "lab" in path
    score_high = score_lab_url(
        "https://mit.edu/~smith/lab/index.html",
        pi_last_name="smith",
        institution_domain="mit.edu",
        config=config,
    )
    # Base 1.0 + name(+0.3) + edu(+0.3) + keyword(+0.3) = 1.9
    assert score_high == pytest.approx(1.9, abs=1e-6), f"Expected ~1.9, got {score_high}"

    # Penalized case: scholar.google.com domain
    score_penalized = score_lab_url(
        "https://scholar.google.com/citations?user=abc",
        pi_last_name="smith",
        institution_domain="mit.edu",
        config=config,
    )
    assert score_penalized < 1.0, f"Penalized URL should score < 1.0, got {score_penalized}"

    # PDF penalty
    score_pdf = score_lab_url(
        "https://mit.edu/papers/smith_cv.pdf",
        pi_last_name="smith",
        institution_domain="mit.edu",
        config=config,
    )
    assert score_pdf < 1.0, f"PDF URL should score < 1.0, got {score_pdf}"

    # Minimum score is 0.0 — two heavy penalties should not go negative
    score_min = score_lab_url(
        "https://scholar.google.com/citations?user=abc.pdf",
        pi_last_name="",
        institution_domain=None,
        config=config,
    )
    assert score_min >= 0.0, f"Score must not go negative, got {score_min}"

    # No boost, no penalty — plain .com URL → base 1.0
    score_base = score_lab_url(
        "https://example.com/smith",
        pi_last_name="",
        institution_domain=None,
        config=config,
    )
    assert score_base == pytest.approx(1.0, abs=1e-6), f"Expected 1.0 base, got {score_base}"


# ---------------------------------------------------------------------------
# 2. test_diff_members
# ---------------------------------------------------------------------------

def test_diff_members():
    existing_rows = [
        {"member_name": "Alice Johnson", "pi_name": "PI One", "institution": "MIT"},
        {"member_name": "Bob Smith",     "pi_name": "PI One", "institution": "MIT"},
    ]
    scraped_members = [
        {"member_name": "Alice Johnson", "pi_name": "PI One", "institution": "MIT"},
        {"member_name": "Carol White",   "pi_name": "PI One", "institution": "MIT"},
    ]

    new_members, missing_members = diff_members(
        existing_rows, scraped_members, "PI One", "MIT"
    )

    # Carol is new; Bob is missing
    new_names = [m["member_name"] for m in new_members]
    missing_names = [m["member_name"] for m in missing_members]

    assert "Carol White" in new_names, f"Carol should be new, got new={new_names}"
    assert "Alice Johnson" not in new_names, "Alice is existing — not new"
    assert "Bob Smith" in missing_names, f"Bob should be missing, got missing={missing_names}"
    assert "Alice Johnson" not in missing_names, "Alice is present in both — not missing"

    # Middle-initial normalization: "J. Doe" and "Jane Doe" share last name
    # but full names differ — no match expected
    existing_with_initial = [
        {"member_name": "Jane A. Doe", "pi_name": "PI One", "institution": "MIT"},
    ]
    scraped_no_initial = [
        {"member_name": "Jane Doe", "pi_name": "PI One", "institution": "MIT"},
    ]
    new2, missing2 = diff_members(
        existing_with_initial, scraped_no_initial, "PI One", "MIT"
    )
    # normalize_name("Jane A. Doe") == "jane doe"
    # normalize_name("Jane Doe") == "jane doe"
    # → they match → no new, no missing
    assert new2 == [], f"Middle-initial variant should match, got new={new2}"
    assert missing2 == [], f"Middle-initial variant should match, got missing={missing2}"


# ---------------------------------------------------------------------------
# 3. test_rescrape_integration_dry_run
# ---------------------------------------------------------------------------

def test_rescrape_integration_dry_run(tmp_path, monkeypatch):
    """
    Full dry-run through rescrape_and_enrich() with all external calls mocked.

    Verifies:
      - write_pipeline1_results is NOT called (dry_run=True)
      - write_pipeline2_results is NOT called (dry_run=True)
      - A JSON output file is saved to logs/
      - The returned summary dict has all expected keys
    """
    # Redirect logs/ dir to tmp_path to avoid polluting the real project
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir()

    config = _make_config()
    config.institutions = []

    pis = [{"pi_name": "Jane Smith", "institution": "MIT"}]
    situation = "machine learning for clinical outcomes"

    fake_member = {
        "member_name": "Alice Lee",
        "pi_name": "Jane Smith",
        "institution": "MIT",
        "member_role": "PhD Student",
        "lab_homepage_url": "https://mit.edu/~smith/lab",
        "lab_research_summary": "Studies ML for clinical outcomes.",
        "lab_members_url": "https://mit.edu/~smith/lab/people",
        "department_program": "",
        "source_url": "",
    }

    with (
        patch("src.pipeline_rescrape.search.web_search") as mock_search,
        patch("src.pipeline_rescrape._fetch_page") as mock_fetch,
        patch("src.pipeline_rescrape.summarize_lab_pages") as mock_summarize,
        patch("src.pipeline_rescrape.find_member_pages") as mock_find_members,
        patch("src.pipeline_rescrape.extract_members") as mock_extract,
        patch("src.pipeline_rescrape.SheetsClient") as mock_sheets_cls,
        patch("src.pipeline_rescrape.lookup_members") as mock_scholar,
        patch("src.pipeline_rescrape.score_relevance") as mock_score,
        patch("src.pipeline_rescrape.lookup_emails") as mock_emails,
        patch("src.pipeline_rescrape.write_pipeline1_results") as mock_write1,
        patch("src.pipeline_rescrape.write_pipeline2_results") as mock_write2,
        patch("src.pipeline_rescrape.init_run_cache"),
    ):
        # Web search returns one plausible result
        mock_search.return_value = [
            {"link": "https://mit.edu/~smith/lab/index.html", "snippet": "Smith Lab"}
        ]
        # Page fetch returns minimal HTML
        mock_fetch.return_value = "<html><body><h1>Smith Lab</h1></body></html>"
        # Summarize returns a valid lab summary
        mock_summarize.return_value = {
            "Jane Smith": "Studies machine learning methods for clinical outcome prediction."
        }
        # find_member_pages returns the pi_dict with members URL
        mock_find_members.return_value = [{
            "name": "Jane Smith",
            "institution": "MIT",
            "lab_homepage_url": "https://mit.edu/~smith/lab/index.html",
            "lab_research_summary": "Studies ML.",
            "lab_members_url": "https://mit.edu/~smith/lab/people",
            "department_program": "",
            "role": "PI",
            "source_url": "",
        }]
        # extract_members returns one member
        mock_extract.return_value = [fake_member]
        # Sheet has no existing rows for this PI
        mock_sheets_instance = MagicMock()
        mock_sheets_instance.read_all_rows.return_value = []
        mock_sheets_cls.return_value = mock_sheets_instance
        # P2 stubs — no papers, no scoring, no emails
        mock_scholar.return_value = {}
        mock_score.return_value = []
        mock_emails.return_value = {}

        summary = rescrape_and_enrich(
            pis=pis,
            situation=situation,
            config=config,
            dry_run=True,
            skip_scholar=False,
            tab_name="Master List",
        )

    # Sheet write functions must NOT be called in dry-run mode
    mock_write1.assert_not_called()
    mock_write2.assert_not_called()

    # Summary has all expected keys
    expected_keys = {
        "pis_processed",
        "lab_homepages_updated",
        "new_members_found",
        "scholar_lookups_run",
        "members_flagged_relevant",
        "emails_found",
    }
    assert expected_keys == set(summary.keys()), (
        f"Summary keys mismatch: {set(summary.keys())}"
    )
    assert summary["pis_processed"] == 1
    assert summary["lab_homepages_updated"] == 1
    assert summary["new_members_found"] == 1  # Alice Lee is new (sheet was empty)

    # A JSON dry-run file must have been created in logs/
    json_files = list(Path("logs").glob("rescrape_dry_run_*.json"))
    assert json_files, "Expected a dry-run JSON file in logs/"
    with json_files[0].open() as f:
        payload = json.load(f)
    assert "pis" in payload
    assert "discovery_results" in payload
    assert payload["pis"][0]["pi_name"] == "Jane Smith"
