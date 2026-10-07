"""Redact sensitive strings from a Telegram export before it reaches RAG.

Everything here is deliberately conservative in one direction only: this runs on
a private group export that is about to be indexed, so a miss costs privacy and
a false positive costs a line of chat. When the two trade off, redact.

The one exception is the address rule, which is a *drop* rather than a
redaction and therefore cannot be undone by a reader. A bare keyword match
there is destructive: "rumah" means "home" in Malay and appears in ordinary
lines like "lari dari rumah" (ran away from home). Matching it alone dropped
216 of 326 lines on a real export while catching almost no addresses, so the
rule now requires an actual address *shape* — a street/block/section token, or
a keyword with a house/lot number attached — rather than a keyword on its own.
"""

import re

# Malaysian IC: 123456-78-9012, or a bare 12-digit run.
IC_PATTERNS = [
    (re.compile(r"\b\d{6}-\d{2}-\d{4}\b"), "[REDACTED-IC]"),
    (re.compile(r"\b\d{12}\b"), "[REDACTED-IC]"),
]

PHONE_PATTERNS = [
    (re.compile(r"\+?60\d{9,11}"), "[REDACTED-PHONE]"),
    (re.compile(r"\b0\d{1,2}-?\d{7,8}\b"), "[REDACTED-PHONE]"),
    # "017 723 8999" / "017-605 4649" — space-grouped mobile numbers, which
    # the pattern above misses because it expects the digits to be contiguous.
    (re.compile(r"\b01\d[- ]?\d{3,4}[- ]?\d{4}\b"), "[REDACTED-PHONE]"),
]

EMAIL_PATTERN = (re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
), "[REDACTED-EMAIL]")

# Card numbers MUST run before the generic long-number rule, or the 16-digit
# card is partially eaten by the 10-16 rule and never gets the specific label.
BANK_PATTERNS = [
    (re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"), "[REDACTED-CARD]"),
    (re.compile(r"\b\d{10,16}\b"), "[REDACTED-NUMBER]"),
]

ALL_RULES = IC_PATTERNS + PHONE_PATTERNS + [EMAIL_PATTERN] + BANK_PATTERNS

# A line containing one of these is dropped outright. Every entry must imply a
# real address on its own — see the module docstring for why "Rumah" and
# "Kampung" as bare nouns are NOT in this list.
_STRONG_ADDRESS = re.compile(
    r"\b(Jalan|Lorong|Persiaran|Lebuhraya|Lebuh|Simpang|Seksyen|"
    r"Kondominium|Apartment|Blok\s+[A-Z0-9]|Block\s+[A-Z0-9]|"
    r"Lot\s+\d+|No\.\s*\d+|SS\d+|USJ\s?\d+)\b",
    re.IGNORECASE,
)

# Weak tokens: common words that only indicate an address when a house/lot
# number sits next to them, as in "Taman Sri Muda 12" or "Kampung Baru 5".
_WEAK_ADDRESS = re.compile(
    r"\b(Taman|Kampung|Bandar|Flat|Kondo|Rumah|Puchong|Subang|"
    r"Shah Alam|Petaling)\b\s*[A-Za-z]*\s*\d+",
    re.IGNORECASE,
)

ADDRESS_KEYWORDS = re.compile(
    _STRONG_ADDRESS.pattern + r"|" + _WEAK_ADDRESS.pattern, re.IGNORECASE)


def looks_like_address(line):
    """True when a line should be dropped as address-bearing.

    Strong token anywhere, or a weak token that has a number attached.
    """
    if not line:
        return False
    return bool(_STRONG_ADDRESS.search(line) or _WEAK_ADDRESS.search(line))


def scrub_line(line, custom_blocklist=None):
    """Scrub one line.

    Returns (cleaned_line, was_modified, reasons). cleaned_line is None when
    the line should be dropped entirely.
    """
    reasons = []

    if looks_like_address(line):
        return None, True, ["address_keyword"]

    modified = False
    for pattern, replacement in ALL_RULES:
        if pattern.search(line):
            line = pattern.sub(replacement, line)
            modified = True
            reasons.append(replacement.strip("[]"))

    if custom_blocklist:
        low = line.lower()
        for word in custom_blocklist:
            if word and word.lower() in low:
                # Report the reason without echoing the word: the log is a
                # count of what was removed, not a copy of it.
                return None, True, ["blocklist"]

    return line, modified, reasons


def scrub_stats():
    """Fresh counters for one parse run."""
    return {
        "total_lines": 0,
        "dropped_lines": 0,
        "modified_lines": 0,
        "reasons": {},
    }


def record_stats(stats, dropped, modified, reasons):
    stats["total_lines"] += 1
    if dropped:
        stats["dropped_lines"] += 1
    elif modified:
        stats["modified_lines"] += 1
    for r in reasons:
        stats["reasons"][r] = stats["reasons"].get(r, 0) + 1
