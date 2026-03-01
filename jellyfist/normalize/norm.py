import re
import unicodedata


_whitespace_re = re.compile(r"\s+")

DECORATION_PATTERNS = [
    r"\(feat\..*?\)",  # (feat. xxx)
    r"\[feat\..*?\]",  # [feat. xxx]
    r"feat\. .*?$",
    r"\(explicit\)$",
    r"\(deluxe box set\)$",
    r"\(super deluxe\)$",
    r"\(deluxe\)$",
    r"\[deluxe\]$",
    r"\[re-recorded\]$",
    r"\(re-recorded\)$",
    r"\(переиздание\)$",
    r"\(золотое издание\)$",
    r"\(target deluxe edition\)$",
    r"\(new edition\)$",
    r"\(limited edition\)$",
    r"\(deluxe edition\)$",
    r"\(expanded edition\)$",
    r"\(remastered.*?\)$",
    r"\- remaster$",
    r"\- re-recorded$",
    r"\(.*?remaster\)$",
    r"\(.*?version.*?\)$",
    # track numbers
    r"^\d+\.\s+",
    r"^\d+\s+\-\s+",
    r"^\|\d+\|\s+",
    # a year at the end of the track
    r"\[\d{4}\]$",
    # ep at the end of the album
    r"\s+\-\s+EP$",
]


def normalize_name(value: str | None) -> str:
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

    for pattern in DECORATION_PATTERNS:
        value = re.sub(pattern, "", value, flags=re.IGNORECASE)

    # exclude special characters first, replace them with spaces to avoid word fusing
    for char in "/\\":
        value = value.replace(char, " ")

    for char in "-,.:\"*[]()'’‘…►":  # some chars, though, need to be replaced with empty string
        value = value.replace(char, "")

    value = unicodedata.normalize("NFKC", value)
    value = value.strip()
    value = _whitespace_re.sub(" ", value)
    value = value.lower()

    for k, v in {"ё": "e", "é": "e"}.items():
        value = value.replace(k, v)

    return value
