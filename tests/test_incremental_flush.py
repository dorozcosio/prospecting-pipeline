"""
Unit tests for the incremental-flush callback added to lookup_members().

Tests run without real API calls or a live sheet: both SheetsClient and
SerpAPIScholarBackend are replaced with lightweight fakes via unittest.mock.

Key behaviours verified:
  - on_batch_complete fires ceil(N / scholar_flush_interval) times
  - Each batch dict contains the right "scholar_results" and "pi_identifiers" keys
  - Batch sizes are flush_interval for all but the last; the final batch
    contains any remainder lookups
  - All results are still returned as a single flat dict from lookup_members()
"""
import math
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from src.pipeline2.step2_scholar_lookup import ScholarResult, lookup_members

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FLUSH_INTERVAL = 3
_N_MEMBERS = 10
_PI_NAME = "PI Smith"
_INSTITUTION = "MIT"
_CANDIDATE_PIS = [f"{_PI_NAME}|||{_INSTITUTION}"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(flush_interval: int = _FLUSH_INTERVAL) -> MagicMock:
    cfg = MagicMock()
    cfg.settings.scholar_cache_days = 90
    cfg.settings.scholar_delay_range = [0, 0]   # no sleep in tests
    cfg.settings.scholar_flush_interval = flush_interval
    return cfg


def _make_rows(
    n: int,
    pi_name: str = _PI_NAME,
    institution: str = _INSTITUTION,
) -> list[dict]:
    """Return n minimal member rows with no cached papers (all need a lookup)."""
    return [
        {
            "pi_name": pi_name,
            "institution": institution,
            "member_name": f"Member {i}",
            "recent_papers": "",
            "last_enriched": "",
        }
        for i in range(n)
    ]


@contextmanager
def _patch_lookup(rows: list[dict], flush_interval: int = _FLUSH_INTERVAL):
    """
    Context manager that patches SheetsClient and SerpAPIScholarBackend
    inside step2_scholar_lookup and yields (config, mock_backend).
    """
    config = _make_config(flush_interval)
    backend = MagicMock()
    backend.lookup.return_value = ScholarResult(status="found", papers=["Paper A"])

    with (
        patch("src.pipeline2.step2_scholar_lookup.SheetsClient") as MockClient,
        patch(
            "src.pipeline2.step2_scholar_lookup.SerpAPIScholarBackend",
            return_value=backend,
        ),
    ):
        MockClient.return_value.read_all_rows.return_value = rows
        yield config, backend


def _capture_callback():
    """Return (callback, captured_list).  Each call appends a copy of the batch."""
    captured: list[dict] = []

    def on_batch(batch: dict) -> None:
        captured.append({
            "scholar_results": dict(batch["scholar_results"]),
            "pi_identifiers": list(batch["pi_identifiers"]),
        })

    return on_batch, captured


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestIncrementalFlushCallback:
    """Verify callback firing cadence and payload structure."""

    def test_callback_fires_correct_number_of_times(self):
        """ceil(N / flush_interval) callbacks expected."""
        rows = _make_rows(_N_MEMBERS)
        on_batch, captured = _capture_callback()

        with _patch_lookup(rows) as (config, _):
            lookup_members(_CANDIDATE_PIS, config, on_batch_complete=on_batch)

        expected = math.ceil(_N_MEMBERS / _FLUSH_INTERVAL)
        assert len(captured) == expected, (
            f"Expected {expected} callback fires, got {len(captured)}"
        )

    def test_batch_sizes(self):
        """All but the last batch are flush_interval; last batch is the remainder."""
        rows = _make_rows(_N_MEMBERS)
        on_batch, captured = _capture_callback()

        with _patch_lookup(rows) as (config, _):
            lookup_members(_CANDIDATE_PIS, config, on_batch_complete=on_batch)

        for b in captured[:-1]:
            assert len(b["scholar_results"]) == _FLUSH_INTERVAL, (
                f"Expected full batch of {_FLUSH_INTERVAL}, got {len(b['scholar_results'])}"
            )
        remainder = _N_MEMBERS % _FLUSH_INTERVAL or _FLUSH_INTERVAL
        assert len(captured[-1]["scholar_results"]) == remainder

    def test_batch_pi_identifiers_format(self):
        """Every pi_identifier must be a non-empty string with '|||' separator."""
        rows = _make_rows(_N_MEMBERS)
        on_batch, captured = _capture_callback()

        with _patch_lookup(rows) as (config, _):
            lookup_members(_CANDIDATE_PIS, config, on_batch_complete=on_batch)

        for b in captured:
            assert b["pi_identifiers"], "pi_identifiers must be non-empty"
            for pid in b["pi_identifiers"]:
                assert "|||" in pid, f"Expected '|||' in pi_identifier, got {pid!r}"

    def test_all_results_returned(self):
        """lookup_members must return all N results regardless of batching."""
        rows = _make_rows(_N_MEMBERS)
        on_batch, _ = _capture_callback()

        with _patch_lookup(rows) as (config, _):
            results = lookup_members(_CANDIDATE_PIS, config, on_batch_complete=on_batch)

        assert len(results) == _N_MEMBERS

    def test_batches_cover_all_members(self):
        """Union of all batch scholar_results keys must equal the full result set."""
        rows = _make_rows(_N_MEMBERS)
        on_batch, captured = _capture_callback()

        with _patch_lookup(rows) as (config, _):
            results = lookup_members(_CANDIDATE_PIS, config, on_batch_complete=on_batch)

        batch_keys: set = set()
        for b in captured:
            batch_keys.update(b["scholar_results"].keys())

        assert batch_keys == set(results.keys()), (
            "Batch keys do not cover every result returned by lookup_members"
        )

    def test_no_callback_when_none(self):
        """Passing on_batch_complete=None must not raise and still returns all results."""
        rows = _make_rows(_N_MEMBERS)

        with _patch_lookup(rows) as (config, _):
            results = lookup_members(_CANDIDATE_PIS, config, on_batch_complete=None)

        assert len(results) == _N_MEMBERS

    def test_exact_multiple_of_interval(self):
        """When N is an exact multiple, all batches are full — no partial final batch."""
        n = _FLUSH_INTERVAL * 4  # 12 members → exactly 4 full batches
        rows = _make_rows(n)
        on_batch, captured = _capture_callback()

        with _patch_lookup(rows, flush_interval=_FLUSH_INTERVAL) as (config, _):
            lookup_members(_CANDIDATE_PIS, config, on_batch_complete=on_batch)

        assert len(captured) == 4
        for b in captured:
            assert len(b["scholar_results"]) == _FLUSH_INTERVAL

    def test_single_batch_when_n_less_than_interval(self):
        """Fewer members than flush_interval → exactly one callback with all results."""
        rows = _make_rows(2)
        on_batch, captured = _capture_callback()

        with _patch_lookup(rows) as (config, _):
            lookup_members(_CANDIDATE_PIS, config, on_batch_complete=on_batch)

        assert len(captured) == 1
        assert len(captured[0]["scholar_results"]) == 2

    def test_no_members_no_callback(self):
        """Zero eligible members → callback never fires; empty dict returned."""
        rows: list[dict] = []
        on_batch, captured = _capture_callback()

        with _patch_lookup(rows) as (config, _):
            results = lookup_members(_CANDIDATE_PIS, config, on_batch_complete=on_batch)

        assert results == {}
        assert captured == []

    def test_cached_members_skipped(self):
        """Members with recent_papers + a current last_enriched date are not looked up."""
        from datetime import date

        today_str = date.today().isoformat()
        rows = _make_rows(_N_MEMBERS)
        # Mark the first 3 rows as already-enriched today
        for row in rows[:3]:
            row["recent_papers"] = "Some paper"
            row["last_enriched"] = today_str

        on_batch, captured = _capture_callback()

        with _patch_lookup(rows) as (config, _):
            results = lookup_members(_CANDIDATE_PIS, config, on_batch_complete=on_batch)

        # Only 7 non-cached members should be looked up
        assert len(results) == 7
        total_in_batches = sum(len(b["scholar_results"]) for b in captured)
        assert total_in_batches == 7
