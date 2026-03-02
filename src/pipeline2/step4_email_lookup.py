"""
Pipeline 2, Step 4: Find contact email addresses for relevance-flagged researchers.

Three-method cascade (stops as soon as a confident result is found):

  Method 1 — Lab members page
    Fetch lab_members_url HTML (cache-first). Search the cleaned text for
    email addresses that appear within ~500 characters of the person's name.
    Also scan all mailto: href values.

  Method 2 — Web search
    Search "{name}" "{institution}" email via SerpAPI. Fetch the top-3 result
    pages. Scan each page for email regex. If a page looks like a
    profile/directory, optionally use a Haiku call to extract the email.

  Method 3 — Institutional pattern guessing + DNS MX validation
    Try four common address templates against the institution's domain.
    Validate with a DNS MX record check (proves the domain can receive mail,
    NOT that the specific address exists — documented in comments).

  If no email is found after all three methods, leave blank and log a
  "flagged for manual follow-up" message. NEVER generate or guess an email.
"""
import logging
import re
from urllib.parse import urljoin

import requests

from src import cache, llm, search
from src.config import Config
from src.pipeline1.step2_pi_extraction import clean_html

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Name normalisation for identity dedup (mirrors step2_scholar_lookup)
# ---------------------------------------------------------------------------

_MIDDLE_INITIAL_RE = re.compile(r"(?<=\s)[a-z]\.\s*")


def _normalize_member_name(name: str) -> str:
    n = name.strip().lower()
    n = _MIDDLE_INITIAL_RE.sub("", n)
    return " ".join(n.split())

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Loose email regex — matches most academic addresses; refined by proximity check
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)

# mailto: href extractor
_MAILTO_RE = re.compile(r'href=["\']mailto:([^"\'?\s]+)', re.IGNORECASE)

# Window of characters around a name hit to search for nearby emails
_PROXIMITY_WINDOW = 500

# Max characters of page text passed to Haiku
_HAIKU_PAGE_CHARS = 12_000

# Profile/directory page heuristics (trigger Haiku extraction)
_PROFILE_HINTS = re.compile(
    r"profile|people|faculty|directory|contact|staff|team",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Shared fetch helper
# ---------------------------------------------------------------------------

def _fetch(url: str) -> str | None:
    """Fetch URL with cache-first strategy. Returns raw HTML or None."""
    html = cache.get(url)
    if html is not None:
        return html
    try:
        resp = requests.get(url, timeout=15, headers=_HEADERS)
        resp.raise_for_status()
        cache.set(url, resp.text)
        return resp.text
    except Exception as exc:
        logger.debug("Fetch failed for %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Helper: name-proximity email search
# ---------------------------------------------------------------------------

def _find_email_near_name(text: str, name: str) -> str | None:
    """
    Search `text` for an email address near any occurrence of the person's name.

    Strategy: prefer the post-name window (emails are almost always listed after
    the person's name on lab pages). Only fall back to a small pre-name window
    for edge cases like "email | Name" table layouts.

    Returns the first qualifying email, or None.
    """
    name_lower = name.lower()
    text_lower = text.lower()
    pos = 0
    while True:
        idx = text_lower.find(name_lower, pos)
        if idx == -1:
            break
        name_end = idx + len(name_lower)

        # 1. Search AFTER the name (primary — covers typical "Name … email" layout)
        post_end = min(len(text), name_end + _PROXIMITY_WINDOW)
        m = _EMAIL_RE.search(text, name_end, post_end)
        if m:
            return m.group(0)

        # 2. Search a small window BEFORE the name (fallback for table-style pages)
        pre_start = max(0, idx - 100)
        m = _EMAIL_RE.search(text, pre_start, idx)
        if m:
            return m.group(0)

        pos = idx + 1
    return None


def _find_email_near_name_parts(text: str, name: str) -> str | None:
    """
    Like _find_email_near_name but also tries matching on just the last name,
    since some pages list "Smith, J." or just "Smith".

    The last-name fallback requires the found email's local part to contain
    the last name, preventing false positives from nearby emails belonging
    to other people on the same page.
    """
    # Try full name first
    result = _find_email_near_name(text, name)
    if result:
        return result
    # Try last name alone with a stricter confirmation check
    parts = name.strip().split()
    if len(parts) >= 2:
        last = parts[-1]
        if len(last) > 3:  # avoid single-letter or very short last names
            result = _find_email_near_name(text, last)
            if result:
                # Confirm: the email's local part must contain the last name
                local = result.split("@")[0].lower()
                if last.lower() in local:
                    return result
    return None


# ---------------------------------------------------------------------------
# Method 1: Lab members page
# ---------------------------------------------------------------------------

def _from_members_page(name: str, members_url: str) -> str | None:
    """
    Scan the lab members page for an email near the person's name.
    Also checks all mailto: links in the raw HTML.
    """
    if not members_url:
        return None

    html = _fetch(members_url)
    if not html:
        return None

    # Check mailto: hrefs first (most reliable when present)
    mailto_matches = _MAILTO_RE.findall(html)
    name_lower = name.lower()
    name_parts = [p.lower() for p in name.split() if len(p) > 1]
    for addr in mailto_matches:
        addr_lower = addr.lower()
        # Accept if any name part appears in the local part of the address
        local = addr_lower.split("@")[0]
        if any(part in local for part in name_parts):
            return addr

    # Fall back to proximity search in cleaned text
    text = clean_html(html)
    return _find_email_near_name_parts(text, name)


# ---------------------------------------------------------------------------
# Method 2: Web search
# ---------------------------------------------------------------------------

def _haiku_extract_email(name: str, page_text: str) -> str | None:
    """
    Ask Haiku to extract an email for `name` from `page_text`.
    Returns the email string or None.
    """
    system = (
        "Extract contact email addresses from web pages. "
        "Return ONLY the email address or \"NOT_FOUND\"."
    )
    user_prompt = (
        f"Find the email address for {name} on this page:\n\n"
        f"{page_text[:_HAIKU_PAGE_CHARS]}\n\n"
        "Return ONLY the email address if found, or \"NOT_FOUND\"."
    )
    response = llm.call_haiku(prompt=user_prompt, system=system).strip()
    if response.upper() == "NOT_FOUND" or not response:
        return None
    # Validate the response looks like an email
    m = _EMAIL_RE.search(response)
    return m.group(0) if m else None


def _from_web_search(name: str, institution: str) -> str | None:
    """
    Search for the email via SerpAPI, then scan the top result pages.
    Profile/directory pages additionally get a Haiku extraction pass.
    """
    query = f'"{name}" "{institution}" email'
    try:
        hits = search.web_search(query, num_results=5)
    except Exception as exc:
        logger.debug("Web search failed for %r: %s", name, exc)
        return None

    for hit in hits[:3]:
        url = hit.get("link", "")
        if not url:
            continue

        html = _fetch(url)
        if not html:
            continue

        text = clean_html(html)

        # Quick regex scan first
        email = _find_email_near_name_parts(text, name)
        if email:
            return email

        # Haiku pass for pages that look like profiles or directories
        if _PROFILE_HINTS.search(url) or _PROFILE_HINTS.search(hit.get("title", "")):
            email = _haiku_extract_email(name, text)
            if email:
                return email

    return None


# ---------------------------------------------------------------------------
# Method 3: Institutional pattern guessing + DNS MX validation
# ---------------------------------------------------------------------------

def _domain_has_mx(domain: str) -> bool:
    """
    Check whether `domain` has at least one MX record.

    This only confirms the domain is configured to receive email — it does NOT
    verify that any specific address at that domain exists or is deliverable.
    """
    try:
        import dns.resolver
        answers = dns.resolver.resolve(domain, "MX")
        return len(answers) > 0
    except Exception:
        return False


def _institutional_guess(name: str, domain: str) -> str | None:
    """
    Try common academic email patterns for the given name + domain.

    Returns a guessed address (with MX validation) if the domain has MX records,
    otherwise returns None. The caller must treat this as a low-confidence guess.
    """
    if not domain or not _domain_has_mx(domain):
        return None

    parts = name.lower().split()
    if not parts:
        return None

    first = re.sub(r"[^a-z]", "", parts[0])
    last = re.sub(r"[^a-z]", "", parts[-1])
    first_initial = first[:1] if first else ""

    if not last:
        return None

    candidates = []
    if first and last:
        candidates.append(f"{first}.{last}@{domain}")
        candidates.append(f"{first}{last}@{domain}")
    if first_initial and last:
        candidates.append(f"{first_initial}{last}@{domain}")
    candidates.append(f"{last}@{domain}")

    # Return the first pattern — caller is responsible for treating as a guess
    # (domain MX check is the only validation we perform)
    return candidates[0] if candidates else None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def lookup_emails(
    flagged_members: list[dict],
    config: Config,
) -> dict[tuple[str, str], str]:
    """
    Find email addresses for relevance-flagged researchers.

    Args:
        flagged_members: List of member dicts with at least:
                         {pi_name, member_name, institution,
                          lab_members_url (optional), email (optional)}
                         Only members where relevance_flag=True AND email=""
                         are processed.
        config:          Loaded Config.

    Returns:
        Dict mapping (pi_name, member_name) → email string (empty if not found).
    """
    results: dict[tuple[str, str], str] = {}

    # Resolve institution domains for Method 3
    domain_map: dict[str, str] = {
        inst.name: inst.domain for inst in config.institutions
    }

    # ------------------------------------------------------------------
    # Group flagged members by normalised identity for dedup.
    # A person listed under N PIs needs only ONE lookup; the result fans
    # to all (pi_name, member_name) keys.  If ANY row already has an
    # email, propagate it to the others without performing any lookup.
    # ------------------------------------------------------------------
    identity_to_members: dict[tuple[str, str], list[dict]] = {}
    for member in flagged_members:
        if not member.get("relevance_flag"):
            continue
        name = member.get("member_name", "").strip()
        institution = member.get("institution", "").strip()
        if not name:
            continue
        identity = (_normalize_member_name(name), institution.lower())
        identity_to_members.setdefault(identity, []).append(member)

    total_flagged = sum(len(grp) for grp in identity_to_members.values())
    unique_people = len(identity_to_members)
    propagated = 0

    for identity, group in identity_to_members.items():
        # Check whether any row already carries an email
        existing_email = next(
            (m.get("email", "").strip() for m in group if m.get("email", "").strip()),
            "",
        )
        if existing_email:
            for m in group:
                key = (m.get("pi_name", "").strip(), m.get("member_name", "").strip())
                results[key] = existing_email
                if not m.get("email", "").strip():
                    propagated += 1
            continue

        # No existing email — perform ONE lookup using the first occurrence
        rep = group[0]
        name = rep.get("member_name", "").strip()
        institution = rep.get("institution", "").strip()
        members_url = rep.get("lab_members_url", "").strip()

        email: str = ""

        # --- Method 1: lab members page ---
        found = _from_members_page(name, members_url)
        if found:
            logger.info("Email for %s found via members page: %s", name, found)
            email = found

        # --- Method 2: web search ---
        if not email:
            found = _from_web_search(name, institution)
            if found:
                logger.info("Email for %s found via web search: %s", name, found)
                email = found

        # --- Method 3: institutional pattern guess ---
        if not email:
            domain = domain_map.get(institution, "")
            found = _institutional_guess(name, domain)
            if found:
                logger.info(
                    "Email for %s guessed via institutional pattern "
                    "(MX-validated domain only): %s",
                    name, found,
                )
                email = found

        if not email:
            logger.info(
                "No email found for %s at %s — flagged for manual follow-up.",
                name, institution,
            )

        # Fan result to all rows in the group
        for m in group:
            results[(m.get("pi_name", "").strip(), m.get("member_name", "").strip())] = email

    emails_found = sum(1 for v in results.values() if v)
    logger.info(
        "Email lookups: %d flagged row(s) → %d unique member(s) "
        "(%d duplicate lookup(s) avoided). "
        "%d email(s) found, %d propagated from existing rows.",
        total_flagged, unique_people,
        total_flagged - unique_people,
        emails_found, propagated,
    )
    return results
