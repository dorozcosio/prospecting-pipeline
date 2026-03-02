"""
Unit tests for cross-PI identity deduplication in P2 Steps 2, 3, and 4.

A researcher listed under multiple PI labs in the Master List must trigger
only ONE external lookup (Scholar / email search / Sonnet scoring), with the
result fanned back to all (pi_name, member_name) keys for that person.

All tests run without real API calls or a live sheet.
"""
import json
from contextlib import contextmanager
from unittest.mock import MagicMock, call, patch

import pytest

from src.pipeline2.step2_scholar_lookup import ScholarResult, lookup_members
from src.pipeline2.step3_fine_scoring import score_relevance
from src.pipeline2.step4_email_lookup import lookup_emails

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

_INSTITUTION = "MIT"
_MEMBER = "John A. Smith"          # canonical spelling (has middle initial)
_MEMBER_ALT = "John Smith"         # same person, middle initial stripped
_PI_1 = "Dr. Alpha"
_PI_2 = "Dr. Beta"
_CANDIDATE_PIS = [f"{_PI_1}|||{_INSTITUTION}", f"{_PI_2}|||{_INSTITUTION}"]


def _make_config(flush_interval: int = 25) -> MagicMock:
    cfg = MagicMock()
    cfg.settings.scholar_cache_days = 90
    cfg.settings.scholar_delay_range = [0, 0]
    cfg.settings.scholar_flush_interval = flush_interval
    cfg.settings.sonnet_batch_size = 15
    cfg.institutions = []
    return cfg


def _rows_two_pis(member_name: str = _MEMBER, email: str = "") -> list[dict]:
    """Two sheet rows — same member under PI-1 and PI-2."""
    base = {
        "member_name": member_name,
        "institution": _INSTITUTION,
        "recent_papers": "",
        "last_enriched": "",
        "lab_research_summary": "AI for healthcare",
        "email": email,
        "lab_members_url": "",
        "status": "active",
        "scholar_lookup_status": "",
    }
    return [
        {**base, "pi_name": _PI_1, "relevance_flag": True},
        {**base, "pi_name": _PI_2, "relevance_flag": True},
    ]


# ---------------------------------------------------------------------------
# Step 2 — Scholar lookup dedup
# ---------------------------------------------------------------------------


@contextmanager
def _patch_scholar(rows: list[dict], flush_interval: int = 3):
    config = _make_config(flush_interval)
    backend = MagicMock()
    backend.lookup.return_value = ScholarResult(status="found", papers=["Paper X"])
    with (
        patch("src.pipeline2.step2_scholar_lookup.SheetsClient") as MockClient,
        patch(
            "src.pipeline2.step2_scholar_lookup.SerpAPIScholarBackend",
            return_value=backend,
        ),
    ):
        MockClient.return_value.read_all_rows.return_value = rows
        yield config, backend


class TestScholarDedup:
    def test_lookup_called_once_for_two_pi_rows(self):
        """backend.lookup() must be called exactly once for a two-PI member."""
        rows = _rows_two_pis()
        with _patch_scholar(rows) as (config, backend):
            results = lookup_members(_CANDIDATE_PIS, config)

        backend.lookup.assert_called_once()
        assert len(results) == 2
        assert results[(_PI_1, _MEMBER)].status == "found"
        assert results[(_PI_2, _MEMBER)].status == "found"

    def test_both_keys_get_same_result_object(self):
        """Both (pi_1, member) and (pi_2, member) must map to the same ScholarResult."""
        rows = _rows_two_pis()
        with _patch_scholar(rows) as (config, backend):
            results = lookup_members(_CANDIDATE_PIS, config)

        assert results[(_PI_1, _MEMBER)] is results[(_PI_2, _MEMBER)]

    def test_middle_initial_normalisation(self):
        """Rows with 'John A. Smith' and 'John Smith' are treated as the same person."""
        rows = [
            {**_rows_two_pis()[0], "member_name": "John A. Smith"},
            {**_rows_two_pis()[1], "member_name": "John Smith"},
        ]
        with _patch_scholar(rows) as (config, backend):
            results = lookup_members(_CANDIDATE_PIS, config)

        backend.lookup.assert_called_once()
        # Result fanned to both name spellings
        assert results[(_PI_1, "John A. Smith")].status == "found"
        assert results[(_PI_2, "John Smith")].status == "found"

    def test_cached_row_fans_without_lookup(self):
        """If one row is cache-valid, no lookup is performed and data fans to both rows."""
        from datetime import date
        rows = _rows_two_pis()
        rows[0]["recent_papers"] = "Cached paper"
        rows[0]["last_enriched"] = date.today().isoformat()
        rows[0]["scholar_lookup_status"] = "found"

        with _patch_scholar(rows) as (config, backend):
            results = lookup_members(_CANDIDATE_PIS, config)

        backend.lookup.assert_not_called()
        assert len(results) == 2
        assert "Cached paper" in results[(_PI_1, _MEMBER)].papers
        assert "Cached paper" in results[(_PI_2, _MEMBER)].papers

    def test_callback_receives_all_pi_rows_in_batch(self):
        """The on_batch_complete callback must include both PI rows for a deduped person."""
        rows = _rows_two_pis()
        captured: list[dict] = []

        with _patch_scholar(rows, flush_interval=1) as (config, backend):
            lookup_members(
                _CANDIDATE_PIS, config,
                on_batch_complete=lambda b: captured.append(dict(b["scholar_results"])),
            )

        # Only 1 unique lookup → 1 callback fire; but both (pi_name, member) entries present
        assert len(captured) == 1
        batch_keys = set(captured[0].keys())
        assert (_PI_1, _MEMBER) in batch_keys
        assert (_PI_2, _MEMBER) in batch_keys


# ---------------------------------------------------------------------------
# Step 3 — Relevance scoring dedup
# ---------------------------------------------------------------------------


@contextmanager
def _patch_scoring(rows: list[dict]):
    config = _make_config()
    sonnet_response = json.dumps({
        "1": {"relevant": True, "reasoning": "Relevant because AI healthcare."}
    })
    with (
        patch("src.pipeline2.step3_fine_scoring.SheetsClient") as MockClient,
        patch("src.pipeline2.step3_fine_scoring.llm") as mock_llm,
    ):
        MockClient.return_value.read_all_rows.return_value = rows
        mock_llm.call_sonnet.return_value = sonnet_response
        yield config, mock_llm


class TestScoringDedup:
    def test_sonnet_called_once_for_two_pi_rows(self):
        """call_sonnet must be invoked once even when the same member appears under 2 PIs."""
        rows = _rows_two_pis()
        with _patch_scoring(rows) as (config, mock_llm):
            scores = score_relevance(_CANDIDATE_PIS, {}, "AI in healthcare", config)

        mock_llm.call_sonnet.assert_called_once()
        assert len(scores) == 2

    def test_both_pi_rows_get_same_flag(self):
        """Both output rows must have the same relevance_flag and reasoning."""
        rows = _rows_two_pis()
        with _patch_scoring(rows) as (config, mock_llm):
            scores = score_relevance(_CANDIDATE_PIS, {}, "AI in healthcare", config)

        flags = {(s["pi_name"], s["member_name"]): s["relevance_flag"] for s in scores}
        assert flags[(_PI_1, _MEMBER)] is True
        assert flags[(_PI_2, _MEMBER)] is True

        reasonings = {(s["pi_name"], s["member_name"]): s["relevance_reasoning"] for s in scores}
        assert reasonings[(_PI_1, _MEMBER)] == reasonings[(_PI_2, _MEMBER)]

    def test_combined_lab_context_in_prompt(self):
        """For a multi-PI member, the Sonnet prompt must mention both PIs' summaries."""
        rows = _rows_two_pis()
        rows[0]["lab_research_summary"] = "EHR prediction"
        rows[1]["lab_research_summary"] = "CT scan analysis"

        with _patch_scoring(rows) as (config, mock_llm):
            score_relevance(_CANDIDATE_PIS, {}, "clinical AI", config)

        prompt_used = mock_llm.call_sonnet.call_args[1]["prompt"]
        assert "EHR prediction" in prompt_used
        assert "CT scan analysis" in prompt_used

    def test_middle_initial_dedup_in_scoring(self):
        """Scoring must dedup 'John A. Smith' and 'John Smith' to a single LLM call."""
        rows = [
            {**_rows_two_pis()[0], "member_name": "John A. Smith"},
            {**_rows_two_pis()[1], "member_name": "John Smith"},
        ]
        with _patch_scoring(rows) as (config, mock_llm):
            scores = score_relevance(_CANDIDATE_PIS, {}, "AI in healthcare", config)

        mock_llm.call_sonnet.assert_called_once()
        assert len(scores) == 2


# ---------------------------------------------------------------------------
# Step 4 — Email lookup dedup
# ---------------------------------------------------------------------------


def _flagged(pi_name: str, member_name: str = _MEMBER, email: str = "") -> dict:
    return {
        "pi_name": pi_name,
        "member_name": member_name,
        "institution": _INSTITUTION,
        "relevance_flag": True,
        "email": email,
        "lab_members_url": "",
    }


class TestEmailDedup:
    def test_web_search_called_once_for_two_pi_rows(self):
        """_from_web_search must run only once for a person with two PI rows."""
        members = [_flagged(_PI_1), _flagged(_PI_2)]
        config = _make_config()

        with (
            patch("src.pipeline2.step4_email_lookup._from_members_page", return_value=None),
            patch(
                "src.pipeline2.step4_email_lookup._from_web_search",
                return_value="person@mit.edu",
            ) as mock_ws,
            patch("src.pipeline2.step4_email_lookup._institutional_guess", return_value=None),
        ):
            results = lookup_emails(members, config)

        mock_ws.assert_called_once()
        assert results[(_PI_1, _MEMBER)] == "person@mit.edu"
        assert results[(_PI_2, _MEMBER)] == "person@mit.edu"

    def test_existing_email_propagated_without_lookup(self):
        """If one row has an email, it must propagate to the other without any lookup."""
        members = [
            _flagged(_PI_1, email="existing@mit.edu"),
            _flagged(_PI_2, email=""),
        ]
        config = _make_config()

        with (
            patch("src.pipeline2.step4_email_lookup._from_members_page") as mock_m1,
            patch("src.pipeline2.step4_email_lookup._from_web_search") as mock_m2,
            patch("src.pipeline2.step4_email_lookup._institutional_guess") as mock_m3,
        ):
            results = lookup_emails(members, config)

        mock_m1.assert_not_called()
        mock_m2.assert_not_called()
        mock_m3.assert_not_called()
        assert results[(_PI_1, _MEMBER)] == "existing@mit.edu"
        assert results[(_PI_2, _MEMBER)] == "existing@mit.edu"

    def test_middle_initial_dedup_in_email(self):
        """'John A. Smith' and 'John Smith' map to the same identity → one lookup."""
        members = [
            _flagged(_PI_1, member_name="John A. Smith"),
            _flagged(_PI_2, member_name="John Smith"),
        ]
        config = _make_config()

        with (
            patch("src.pipeline2.step4_email_lookup._from_members_page", return_value=None),
            patch(
                "src.pipeline2.step4_email_lookup._from_web_search",
                return_value="jsmith@mit.edu",
            ) as mock_ws,
            patch("src.pipeline2.step4_email_lookup._institutional_guess", return_value=None),
        ):
            results = lookup_emails(members, config)

        mock_ws.assert_called_once()
        assert results[(_PI_1, "John A. Smith")] == "jsmith@mit.edu"
        assert results[(_PI_2, "John Smith")] == "jsmith@mit.edu"

    def test_not_flagged_members_skipped(self):
        """Members with relevance_flag=False or missing must be ignored."""
        members = [
            {**_flagged(_PI_1), "relevance_flag": False},
            _flagged(_PI_2),
        ]
        config = _make_config()

        with (
            patch("src.pipeline2.step4_email_lookup._from_members_page", return_value=None),
            patch("src.pipeline2.step4_email_lookup._from_web_search", return_value="x@mit.edu"),
            patch("src.pipeline2.step4_email_lookup._institutional_guess", return_value=None),
        ):
            results = lookup_emails(members, config)

        # PI-1's row is not flagged → not in results
        assert (_PI_1, _MEMBER) not in results
        # PI-2's row is flagged → looked up
        assert results[(_PI_2, _MEMBER)] == "x@mit.edu"

    def test_different_institutions_not_deduped(self):
        """Two members with the same name at different institutions are distinct."""
        harvard_member = {
            "pi_name": _PI_1,
            "member_name": _MEMBER,
            "institution": "Harvard",
            "relevance_flag": True,
            "email": "",
            "lab_members_url": "",
        }
        mit_member = _flagged(_PI_2)
        config = _make_config()

        with (
            patch("src.pipeline2.step4_email_lookup._from_members_page", return_value=None),
            patch(
                "src.pipeline2.step4_email_lookup._from_web_search",
                side_effect=["h@harvard.edu", "m@mit.edu"],
            ) as mock_ws,
            patch("src.pipeline2.step4_email_lookup._institutional_guess", return_value=None),
        ):
            results = lookup_emails([harvard_member, mit_member], config)

        # Two distinct identities → two lookups
        assert mock_ws.call_count == 2
        assert results[(_PI_1, _MEMBER)] == "h@harvard.edu"
        assert results[(_PI_2, _MEMBER)] == "m@mit.edu"
