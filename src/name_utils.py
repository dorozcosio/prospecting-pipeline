"""
Shared name normalization for identity deduplication across pipelines.

Used by:
  - pipeline1/step2_pi_extraction.py  — PI deduplication
  - pipeline1/step6_sheet_writer.py   — composite key building
  - pipeline_rescrape.py              — diff_members comparison
  - pipeline2/step2_scholar_lookup.py — cross-PI scholar dedup
  - pipeline2/step3_fine_scoring.py   — cross-PI scoring dedup
  - pipeline2/step4_email_lookup.py   — cross-PI email dedup
"""
import re
import unicodedata

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """
    Normalize a person's name for dedup comparison.

    Operations applied in order:
    1. Normalize unicode to ASCII equivalents (e.g., "José" -> "Jose").
    2. Lowercase and strip leading/trailing whitespace.
    3. Remove periods (e.g., "J." -> "J", "St." -> "St").
    4. Remove single-letter middle initials — single-letter words that are
       not the first or last token (e.g., "Jane A Smith" -> "Jane Smith").
    5. Collapse multiple spaces to one.
    """
    # 1. Unicode -> ASCII (covers accents, ligatures, etc.)
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    # 2. Lowercase + strip
    name = name.lower().strip()
    # 3. Remove periods
    name = name.replace(".", "")
    # 4. Remove single-letter middle initials
    words = name.split()
    if len(words) > 2:
        words = [words[0]] + [w for w in words[1:-1] if len(w) > 1] + [words[-1]]
    # 5. Collapse whitespace
    return _WHITESPACE_RE.sub(" ", " ".join(words)).strip()
