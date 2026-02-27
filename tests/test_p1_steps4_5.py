"""
Integration test for Pipeline 1, Steps 4 and 5.

Uses hardcoded PI dicts with confirmed lab homepage URLs.
Prints all results for manual quality inspection.
"""
from src.config import load_config
from src.pipeline1.step4_member_pages import find_member_pages
from src.pipeline1.step5_member_extraction import extract_members

# Hardcoded PI dicts with confirmed lab homepage URLs (from Step 3 test run)
TEST_PIS = [
    {
        "name": "Regina Barzilay",
        "role": "School of Engineering Distinguished Professor of AI and Health, [AI+D]",
        "institution": "MIT",
        "department_program": "EECS",
        "source_url": "https://www.eecs.mit.edu/role/faculty-aid/?fwp_research=ai-for-healthcare-and-life-sciences",
        "lab_homepage_url": "https://www.rbg.mit.edu/",
        "lab_research_summary": (
            "Develops machine learning methods for molecular docking, biomolecule "
            "generation, and clinical decision-making, with applications in drug "
            "discovery, protein design, and cancer risk prediction."
        ),
    },
    {
        "name": "Marzyeh Ghassemi",
        "role": "The Germeshausen Career Development Professor; Associate Professor, [AI+D]",
        "institution": "MIT",
        "department_program": "EECS",
        "source_url": "https://www.eecs.mit.edu/role/faculty-aid/?fwp_research=ai-for-healthcare-and-life-sciences",
        "lab_homepage_url": "https://healthyml.org/",
        "lab_research_summary": (
            "Builds machine learning models that are healthy — fair, robust, "
            "and generalizable — for clinical applications."
        ),
    },
]


def test_find_member_pages():
    config = load_config()
    enriched = find_member_pages(TEST_PIS, config)

    assert len(enriched) == len(TEST_PIS), "PI count should not change"
    for pi in enriched:
        assert "lab_members_url" in pi, f"Missing lab_members_url: {pi['name']}"

    assert any(pi["lab_members_url"] for pi in enriched), (
        "Expected at least one PI to have a lab_members_url"
    )

    print(f"\n{'='*60}")
    print("Member page discovery:")
    print(f"{'='*60}")
    for pi in enriched:
        url = pi["lab_members_url"] or "(not found)"
        print(f"  {pi['name']:<35} → {url}")



def test_extract_members():
    config = load_config()
    enriched_pis = find_member_pages(TEST_PIS, config)

    rows = extract_members(enriched_pis, config)

    assert rows, "extract_members returned an empty list"

    required_keys = {
        "institution", "department_program", "pi_name",
        "lab_research_summary", "lab_homepage_url",
        "lab_members_url", "member_name", "member_role",
    }
    for row in rows:
        missing = required_keys - set(row.keys())
        assert not missing, f"Row missing keys {missing}: {row}"

    pi_rows = [r for r in rows if r["member_role"] == "PI"]
    member_rows = [r for r in rows if r["member_role"] != "PI"]

    assert pi_rows, "No PI rows found"
    assert member_rows, "No member rows found — extraction returned no lab members"

    print(f"\n{'='*60}")
    print(f"Extracted {len(rows)} total rows ({len(pi_rows)} PI, {len(member_rows)} members):")
    print(f"{'='*60}")
    for row in rows:
        tag = "[PI]" if row["member_role"] == "PI" else "    "
        print(f"  {tag} {row['member_name']:<40} | {row['member_role']:<25} | {row['pi_name']}")
