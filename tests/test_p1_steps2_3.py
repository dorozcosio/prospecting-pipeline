"""
Integration test for Pipeline 1, Steps 2 and 3.

Uses hardcoded MIT faculty page URLs (known to be stable from Step 1 runs).
Prints all results for manual quality inspection.
"""
from src.config import load_config
from src.pipeline1.step2_pi_extraction import extract_pis
from src.pipeline1.step3_lab_homepages import find_lab_homepages

INSTITUTION = "MIT"

# Stable MIT faculty listing pages with medical AI / healthcare ML content
TEST_URLS = [
    {
        "url": "https://www.eecs.mit.edu/role/faculty-aid/?fwp_research=ai-for-healthcare-and-life-sciences",
        "snippet": "MIT EECS faculty — AI for Healthcare and Life Sciences filter",
        "source_query": "test_hardcoded",
    },
    {
        "url": "https://imes.mit.edu/people/faculty",
        "snippet": "MIT Institute for Medical Engineering & Science faculty listing",
        "source_query": "test_hardcoded",
    },
]


def test_extract_pis():
    config = load_config()
    pis = extract_pis(INSTITUTION, TEST_URLS, config)

    assert pis, "extract_pis returned an empty list"
    for pi in pis:
        assert "name" in pi and pi["name"], f"PI missing name: {pi}"
        assert "role" in pi, f"PI missing role key: {pi}"
        assert "institution" in pi, f"PI missing institution: {pi}"
        assert "department_program" in pi, f"PI missing department_program: {pi}"
        assert "source_url" in pi, f"PI missing source_url: {pi}"

    print(f"\n{'='*60}")
    print(f"Extracted {len(pis)} PI(s) from {len(TEST_URLS)} pages:")
    print(f"{'='*60}")
    for i, pi in enumerate(pis, 1):
        print(f"[{i:>3}] {pi['name']:<35} | {pi['role']:<30} | {pi['department_program']}")



def test_find_lab_homepages():
    config = load_config()
    pis = extract_pis(INSTITUTION, TEST_URLS, config)
    assert pis, "No PIs to work with"

    # Limit to first 2 to keep the test fast and cheap
    sample = pis[:2]
    enriched = find_lab_homepages(sample, config)

    assert len(enriched) == len(sample)
    for pi in enriched:
        assert "lab_homepage_url" in pi, f"Missing lab_homepage_url: {pi}"
        assert "lab_research_summary" in pi, f"Missing lab_research_summary: {pi}"

    assert any(pi["lab_homepage_url"] for pi in enriched), (
        "Expected at least one PI to have a lab homepage URL"
    )
    assert any(pi["lab_research_summary"] for pi in enriched), (
        "Expected at least one PI to have a research summary"
    )

    print(f"\n{'='*60}")
    print(f"Lab homepages for {len(enriched)} PI(s):")
    print(f"{'='*60}")
    for pi in enriched:
        print(f"\n  Name   : {pi['name']}")
        print(f"  Role   : {pi['role']}")
        print(f"  URL    : {pi['lab_homepage_url'] or '(not found)'}")
        print(f"  Summary: {pi['lab_research_summary'] or '(none)'}")
