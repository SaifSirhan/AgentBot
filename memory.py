"""
Persistent long-term memory - facts about the user that survive restarts.

Stores facts as a list of dicts:
    {"fact": str, "category": str, "added": iso-timestamp}

Categories are optional; defaults to "general". Common categories:
identity, preferences, work, relationships, schedule, health, general

Interface used by agent.py:
  remember(fact)         -> add a fact (no-op if duplicate)
  forget(fact)           -> remove a matching fact, returns True if removed
  get_facts_block()      -> formatted string for the prompt, grouped by category

Facts can be prefixed with "category: text" and the category will be
extracted. Example: remember("work: I work at Petronas")
"""

import json
import os
import re
import difflib
import datetime

MEMORY_FILE = os.path.join(
    os.environ.get('LOCALAPPDATA', ''), 'AgentMemory', 'facts.json'
)

VALID_CATEGORIES = {
    "identity", "preferences", "work", "relationships",
    "schedule", "health", "general",
}

CATEGORY_ORDER = [
    "identity", "preferences", "work", "relationships",
    "schedule", "health", "general",
]


# ---------- Normalization & dedup ----------

def _normalize(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s


def _is_duplicate(new_fact: str, existing_texts: list, threshold: float = 0.85) -> bool:
    new_norm = _normalize(new_fact)
    if not new_norm:
        return True
    for old in existing_texts:
        old_norm = _normalize(old)
        if not old_norm:
            continue
        if new_norm == old_norm:
            return True
        if new_norm in old_norm or old_norm in new_norm:
            return True
        ratio = difflib.SequenceMatcher(None, new_norm, old_norm).ratio()
        if ratio >= threshold:
            return True
    return False


# ---------- Load / save / migrate ----------

def _load():
    """Return list of dicts. Migrates old list-of-strings format."""
    if not os.path.exists(MEMORY_FILE):
        return []
    try:
        with open(MEMORY_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    facts = data.get('facts', []) if isinstance(data, dict) else data

    migrated = []
    for item in facts:
        if isinstance(item, str):
            migrated.append({
                "fact": item,
                "category": "general",
                "added": None,
            })
        elif isinstance(item, dict) and "fact" in item:
            migrated.append({
                "fact": item.get("fact", ""),
                "category": item.get("category", "general"),
                "added": item.get("added"),
            })
    return migrated


def _save(facts):
    os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
    with open(MEMORY_FILE, 'w', encoding='utf-8') as f:
        json.dump({'facts': facts}, f, indent=2)


# ---------- Category parsing ----------

def _parse_category(fact: str):
    """If fact looks like 'category: text', extract the category."""
    m = re.match(r"^([a-zA-Z]+)\s*:\s*(.+)$", fact.strip())
    if m:
        cat = m.group(1).lower()
        text = m.group(2).strip()
        if cat in VALID_CATEGORIES and text:
            return cat, text
    return "general", fact.strip()


# ---------- Public API ----------

def remember(fact):
    if not fact or not fact.strip():
        return
    category, text = _parse_category(fact)
    if not text:
        return

    facts = _load()
    existing_texts = [f["fact"] for f in facts]

    if _is_duplicate(text, existing_texts):
        return

    facts.append({
        "fact": text,
        "category": category,
        "added": datetime.datetime.now().isoformat(timespec="seconds"),
    })
    _save(facts)


def forget(fact):
    if not fact or not fact.strip():
        return False
    facts = _load()
    _, target = _parse_category(fact)
    target_norm = _normalize(target)

    for i, f in enumerate(facts):
        if _normalize(f["fact"]) == target_norm:
            facts.pop(i)
            _save(facts)
            return True

    for i, f in enumerate(facts):
        if target_norm and target_norm in _normalize(f["fact"]):
            facts.pop(i)
            _save(facts)
            return True

    existing_texts = [f["fact"] for f in facts]
    close = difflib.get_close_matches(target, existing_texts, n=1, cutoff=0.6)
    if close:
        for i, f in enumerate(facts):
            if f["fact"] == close[0]:
                facts.pop(i)
                _save(facts)
                return True
    return False


def get_facts_block():
    """Return facts as a formatted string, grouped by category."""
    facts = _load()
    if not facts:
        return ""

    grouped = {}
    for f in facts:
        cat = f.get("category", "general") or "general"
        grouped.setdefault(cat, []).append(f["fact"])

    lines = []
    seen = set()
    for cat in CATEGORY_ORDER:
        if cat in grouped:
            seen.add(cat)
            lines.append(f"{cat.capitalize()}:")
            for f in grouped[cat]:
                lines.append(f"  - {f}")
            lines.append("")
    for cat, items in grouped.items():
        if cat in seen:
            continue
        lines.append(f"{cat.capitalize()}:")
        for f in items:
            lines.append(f"  - {f}")
        lines.append("")

    return "\n".join(lines).rstrip()


def list_facts():
    """Return all facts (for debugging/UI)."""
    return _load()


if __name__ == "__main__":
    print(f"Memory file: {MEMORY_FILE}")
    for i, f in enumerate(_load()):
        print(f"  [{i}] ({f['category']}) {f['fact']}")