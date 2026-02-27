"""
Integration test for Pipeline 2, Step 4: email lookup.

Tests:
  1. DNS MX check — valid and invalid domains
  2. Email regex extraction from a synthetic HTML snippet
  3. Method 1 live test: find a publicly listed email from a real lab members page
     (Weian Mao, Barzilay Lab, MIT — email weian@mit.edu is on rbg.mit.edu/people/)
"""
import pytest

from src.config import load_config
from src.pipeline2.step4_email_lookup import (
    _domain_has_mx,
    _from_members_page,
    _find_email_near_name_parts,
    lookup_emails,
)


# ---------------------------------------------------------------------------
# Test 1: DNS MX record check
# ---------------------------------------------------------------------------

def test_mx_valid_domain():
    """mit.edu should have MX records."""
    assert _domain_has_mx("mit.edu"), "Expected mit.edu to have MX records"


def test_mx_invalid_domain():
    """A made-up domain should not have MX records."""
    assert not _domain_has_mx("this-domain-does-not-exist-xyzabc123.com"), (
        "Expected non-existent domain to have no MX records"
    )


# ---------------------------------------------------------------------------
# Test 2: Email regex extraction on synthetic HTML
# ---------------------------------------------------------------------------

SAMPLE_HTML = """
<html><body>
<h2>Lab Members</h2>
<div class="person">
  <h3>Jane Smith</h3>
  <p>PhD Student in Computational Biology</p>
  <p>Contact: <a href="mailto:jsmith@university.edu">jsmith@university.edu</a></p>
</div>
<div class="person">
  <h3>Bob Jones</h3>
  <p>Postdoctoral Researcher</p>
  <p>Email: bjones@university.edu</p>
</div>
</body></html>
"""


def test_regex_email_extraction_by_name():
    """_find_email_near_name_parts should find an email close to the person's name."""
    from src.pipeline1.step2_pi_extraction import clean_html
    text = clean_html(SAMPLE_HTML)

    email = _find_email_near_name_parts(text, "Jane Smith")
    assert email == "jsmith@university.edu", (
        f"Expected jsmith@university.edu, got {email!r}"
    )

    email2 = _find_email_near_name_parts(text, "Bob Jones")
    assert email2 == "bjones@university.edu", (
        f"Expected bjones@university.edu, got {email2!r}"
    )


def test_regex_no_false_positive():
    """Should not return an email when the name is absent from the page."""
    from src.pipeline1.step2_pi_extraction import clean_html
    text = clean_html(SAMPLE_HTML)
    email = _find_email_near_name_parts(text, "Nonexistent Person")
    assert email is None, f"Expected None for unknown person, got {email!r}"


# ---------------------------------------------------------------------------
# Test 3: Method 1 live test — real publicly listed email
# ---------------------------------------------------------------------------

def test_members_page_live():
    """
    Weian Mao's email (weian@mit.edu) is publicly listed on the Barzilay Lab
    members page (https://www.rbg.mit.edu/people/).
    """
    email = _from_members_page(
        name="Weian Mao",
        members_url="https://www.rbg.mit.edu/people/",
    )
    assert email, "Expected an email to be found for Weian Mao"
    assert "@" in email, f"Found value doesn't look like an email: {email!r}"
    print(f"\n  [Method 1] Weian Mao → {email}")


# ---------------------------------------------------------------------------
# Test 4: Full lookup_emails with known result
# ---------------------------------------------------------------------------

def test_lookup_emails_integration():
    """
    Run lookup_emails on a small flagged_members list that includes
    Weian Mao (should find email) and a clearly fake person (should not).
    """
    config = load_config()

    flagged = [
        {
            "pi_name": "Regina Barzilay",
            "member_name": "Weian Mao",
            "institution": "MIT",
            "lab_members_url": "https://www.rbg.mit.edu/people/",
            "email": "",
            "relevance_flag": True,
        },
        {
            "pi_name": "Regina Barzilay",
            "member_name": "Xyzabc Nonexistent 99999",
            "institution": "MIT",
            "lab_members_url": "https://www.rbg.mit.edu/people/",
            "email": "",
            "relevance_flag": True,
        },
        {
            # relevance_flag=False — must be skipped
            "pi_name": "Regina Barzilay",
            "member_name": "Should Be Skipped",
            "institution": "MIT",
            "lab_members_url": "https://www.rbg.mit.edu/people/",
            "email": "",
            "relevance_flag": False,
        },
    ]

    results = lookup_emails(flagged, config)

    # Skipped member should not appear in results
    assert ("Regina Barzilay", "Should Be Skipped") not in results, (
        "Non-flagged member should be skipped"
    )

    # Weian Mao should have an email
    weian_email = results.get(("Regina Barzilay", "Weian Mao"), None)
    assert weian_email, f"Expected email for Weian Mao, got {weian_email!r}"
    assert "@" in weian_email

    # Fake person should have an empty string (not found, method 3 may produce a guess)
    # Accept either empty or a guessed pattern (method 3 validates domain only)
    fake_result = results.get(("Regina Barzilay", "Xyzabc Nonexistent 99999"), "MISSING")
    assert fake_result != "MISSING", "Fake member key should be present in results"

    print(f"\n{'='*60}")
    print("lookup_emails results:")
    print(f"{'='*60}")
    for (pi, member), email in results.items():
        status = email if email else "(not found)"
        print(f"  {member:<35} → {status}")
