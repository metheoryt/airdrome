import re
import unicodedata


_whitespace_re = re.compile(r"\s+")


def normalize_name(value: str) -> str:
    """
    Normalize artist / release / track names for identity comparison.

    Rules:
    - Unicode normalize (NFKC)
    - strip leading/trailing whitespace
    - collapse internal whitespace
    - lowercase

    Does NOT:
    - remove punctuation
    - remove 'feat.' / 'remaster' / etc
    - transliterate accents

    Those are display/semantic decisions, not identity decisions.
    """
    if not value:
        return ""

    value = unicodedata.normalize("NFKC", value)
    value = value.strip()
    value = _whitespace_re.sub(" ", value)
    value = value.lower()

    return value
