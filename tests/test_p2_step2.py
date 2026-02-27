"""
Integration test for Pipeline 2, Step 2: Google Scholar lookup.

Calls SerpAPIScholarBackend directly (bypasses the sheet-reading orchestrator)
to keep the test focused and avoid fixture overhead.

Delay is shortened to [1, 2]s for the test run (restored afterwards).
"""
import pytest

from src.config import load_config
from src.pipeline2.step2_scholar_lookup import SerpAPIScholarBackend, ScholarlyBackend

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def backend():
    """SerpAPIScholarBackend with a shortened delay for test speed."""
    config = load_config()
    original = config.settings.scholar_delay_range
    config.settings.scholar_delay_range = [1, 2]
    yield SerpAPIScholarBackend(config)
    config.settings.scholar_delay_range = original


# ---------------------------------------------------------------------------
# Tests — real researchers
# ---------------------------------------------------------------------------

REAL_RESEARCHERS = [
    ("Fei-Fei Li", "Stanford University"),
    ("Regina Barzilay", "MIT"),
    ("Andrew Ng", "Stanford University"),
]


@pytest.mark.parametrize("name,institution", REAL_RESEARCHERS)
def test_known_researcher(backend, name, institution):
    result = backend.lookup(name, institution)

    assert result.status in ("found", "ambiguous"), (
        f"{name}: expected found/ambiguous, got '{result.status}'. "
        f"error_message={result.error_message!r}"
    )
    assert result.papers, f"{name}: status={result.status} but no papers returned"

    print(f"\n  [{result.status:9}] {name} ({institution})")
    print(f"  profile : {result.profile_url or '(none)'}")
    print(f"  papers  : {len(result.papers)}")
    for title in result.papers:
        print(f"    - {title}")


# ---------------------------------------------------------------------------
# Test — clearly fake name
# ---------------------------------------------------------------------------

def test_fake_name(backend):
    result = backend.lookup("Zxqwerty Aaabbb Nonexistent 99999")

    assert result.status == "not_found", (
        f"Expected not_found for fake name, got '{result.status}'. "
        f"papers={result.papers!r}"
    )
    print(f"\n  [not_found] fake name — correct")


# ---------------------------------------------------------------------------
# Test — scholarly stub raises NotImplementedError
# ---------------------------------------------------------------------------

def test_scholarly_stub_raises():
    stub = ScholarlyBackend()
    with pytest.raises(NotImplementedError, match="SerpAPIScholarBackend"):
        stub.lookup("Anyone")
