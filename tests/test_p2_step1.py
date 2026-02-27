"""
Integration test for Pipeline 2, Step 1: coarse PI relevance filter.

Inserts 5 synthetic PI rows into a "Test_P2" tab — three clearly relevant
to CRISPR gene editing for cancer therapy, two clearly irrelevant — then
runs coarse_filter_pis and verifies the classifications.
"""
import pytest

from src.config import load_config
from src.pipeline2.step1_coarse_filter import coarse_filter_pis
from src.sheets import SheetsClient

TAB_NAME = "Test_P2"
SITUATION = "CRISPR gene editing for cancer therapy"

# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------
# Rows written directly to the sheet (only fields coarse_filter cares about)
TEST_ROWS = [
    # --- Clearly relevant ---
    {
        "institution": "MIT",
        "pi_name": "Alice Chen",
        "lab_research_summary": (
            "Develops CRISPR-Cas9 and base-editing tools for precision correction of "
            "oncogenic mutations, with applications in T-cell engineering and solid tumour therapy."
        ),
        "status": "active",
    },
    {
        "institution": "Stanford University",
        "pi_name": "Bob Rivera",
        "lab_research_summary": (
            "Focuses on lentiviral and AAV gene delivery vectors for ex-vivo CAR-T cell "
            "therapy and in-vivo somatic genome editing in cancer models."
        ),
        "status": "active",
    },
    {
        "institution": "Harvard University",
        "pi_name": "Carol Kim",
        "lab_research_summary": (
            "Studies the molecular mechanisms of DNA double-strand break repair and "
            "applies this knowledge to improve HDR efficiency for therapeutic genome editing "
            "in haematological malignancies."
        ),
        "status": "active",
    },
    # --- Clearly irrelevant ---
    {
        "institution": "MIT",
        "pi_name": "Dave Nguyen",
        "lab_research_summary": (
            "Designs distributed routing algorithms and congestion-control protocols "
            "for large-scale software-defined networks and data-centre fabrics."
        ),
        "status": "active",
    },
    {
        "institution": "Stanford University",
        "pi_name": "Eva Schulz",
        "lab_research_summary": (
            "Models atmospheric aerosol dynamics and cloud microphysics to improve "
            "century-scale climate projections under high-emission scenarios."
        ),
        "status": "active",
    },
]

RELEVANT_PIS = {"Alice Chen", "Bob Rivera", "Carol Kim"}
IRRELEVANT_PIS = {"Dave Nguyen", "Eva Schulz"}


@pytest.fixture(scope="module")
def client():
    return SheetsClient(load_config())


@pytest.fixture(autouse=True, scope="module")
def setup_and_teardown(client):
    """Create Test_P2 tab with test rows before module; delete after."""
    client.delete_tab(TAB_NAME)
    client.create_tab(TAB_NAME)
    client.append_rows(TAB_NAME, TEST_ROWS)
    yield
    client.delete_tab(TAB_NAME)


def test_coarse_filter_pis():
    config = load_config()
    passing = coarse_filter_pis(SITUATION, config, tab_name=TAB_NAME)

    assert passing, "coarse_filter_pis returned no passing PIs"

    passing_names = {ident.split("|||")[0] for ident in passing}

    # All clearly relevant PIs should pass
    for name in RELEVANT_PIS:
        assert name in passing_names, (
            f"Expected relevant PI '{name}' to pass but it was filtered out. "
            f"Passing: {passing_names}"
        )

    # Clearly irrelevant PIs should not pass
    for name in IRRELEVANT_PIS:
        assert name not in passing_names, (
            f"Expected irrelevant PI '{name}' to be filtered out but it passed. "
            f"Passing: {passing_names}"
        )

    total = len(TEST_ROWS)
    n_passing = len(passing)
    n_filtered = total - n_passing

    print(f"\n{'='*60}")
    print(f"Situation: {SITUATION}")
    print(f"{'='*60}")
    print(f"  Total PIs screened : {total}")
    print(f"  Passing            : {n_passing}")
    print(f"  Filtered out       : {n_filtered}")
    print(f"  Pass rate          : {n_passing / total * 100:.0f}%")
    print(f"\n  Passing PIs:")
    for ident in passing:
        name, inst = ident.split("|||")
        print(f"    [YES] {name:<30} ({inst})")
    filtered_names = {r["pi_name"] for r in TEST_ROWS} - passing_names
    for name in filtered_names:
        inst = next(r["institution"] for r in TEST_ROWS if r["pi_name"] == name)
        print(f"    [NO ] {name:<30} ({inst})")
