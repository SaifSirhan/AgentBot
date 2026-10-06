"""
Autocorrect for outgoing Telegram messages — English + Malay/Manglish.

Design:
  - Conservative. Only fixes high-confidence typos.
  - Never touches: URLs, emails, numbers, ALL-CAPS, mixed-case,
    words in the whitelist, or very short words.
  - Built-in whitelist of ~400 common Malay + Manglish words so they
    don't get "corrected" into unrelated English.
  - User-extensible via autocorrect_whitelist.txt (one word per line,
    add your contact names there).

Requires: pip install pyspellchecker
"""

import os
import re

# ---------- English spell checker ----------
try:
    from spellchecker import SpellChecker
    _EN = SpellChecker(language="en")
    _EN_AVAILABLE = True
except ImportError:
    _EN = None
    _EN_AVAILABLE = False


# ---------- Built-in whitelist (Malay + Manglish + common chat) ----------
_BUILTIN_RAW = """
aku saya kamu awak anda dia kami kita mereka kalian hang korang
apa siapa mana bila kenapa mengapa macam bagaimana berapa
ini itu sini situ sana
ya tak tidak bukan belum sudah dah jangan takkan
ada pergi datang balik makan minum tidur bangun kerja belajar
main tengok lihat dengar cakap kata tanya jawab buat bagi ambil
hantar terima suka benci rasa fikir tahu faham ingat lupa
nak mahu hendak boleh mesti perlu tolong minta sila jemput
rumah sekolah pejabat kedai pasar jalan kereta motor bas
air nasi roti ayam ikan daging sayur buah buku telefon komputer
wang duit nama orang budak anak mak ayah emak bapa ibu abang
kakak adik atuk nenek pak cik sepupu keluarga kawan sahabat
cikgu doktor polis
baik buruk besar kecil panjang pendek tinggi rendah baru lama
banyak sikit semua lain sama cantik comel kotor bersih
panas sejuk sedap pahit masam manis
hari malam pagi petang tengahari semalam esok lusa sekarang
nanti tadi kelmarin minggu bulan tahun jam minit saat
satu dua tiga empat lima enam tujuh lapan sembilan sepuluh
sebelas dua belas puluh seratus seribu
atas bawah depan belakang kiri kanan tepi dalam luar dekat jauh
dengan untuk dari ke pada di dan atau tapi kalau sebab jadi
masih lagi juga pun lah kot je ja la
lah lor leh mah meh kan bah sia sial gila
walao walaoweh aiyo aiyah alamak aduh cis
bodoh best syok power gempak cun steady tahan geram kacau
sibuk tension stress ok oke okay on off can cannot
dun dowan wanna gonna kinda sorta anyhow whatever
chialat chialak paiseh shiok tapau dabao tapao kenduri
dih eh ah oh uh hmm hmmm lo
hi hey bye hello thanks thank
""".split()

_BUILTIN_WHITELIST = {w.lower() for w in _BUILTIN_RAW if w}


# ---------- User whitelist file ----------
WHITELIST_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "autocorrect_whitelist.txt",
)


def _load_user_whitelist():
    if not os.path.exists(WHITELIST_FILE):
        # Create a template on first run
        try:
            with open(WHITELIST_FILE, "w", encoding="utf-8") as f:
                f.write(
                    "# Autocorrect whitelist — one word per line.\n"
                    "# Words here are NEVER changed. Add your contact names,\n"
                    "# unusual slang, or anything you don't want touched.\n"
                    "# Lines starting with # are ignored.\n\n"
                    "# Examples (uncomment or add your own):\n"
                    "# JEE\n"
                    "# holocaustmuseum\n"
                )
        except Exception:
            pass
        return set()
    try:
        with open(WHITELIST_FILE, "r", encoding="utf-8") as f:
            return {
                line.strip().lower()
                for line in f
                if line.strip() and not line.startswith("#")
            }
    except Exception:
        return set()


_USER_WHITELIST = _load_user_whitelist()


def reload_whitelist():
    """Re-read the whitelist file. Call after editing it."""
    global _USER_WHITELIST
    _USER_WHITELIST = _load_user_whitelist()
    return len(_USER_WHITELIST)


# ---------- Word filtering ----------
def _is_protected(word):
    """True if the word must be left alone."""
    if not word:
        return True

    w = word.lower()

    if w in _BUILTIN_WHITELIST:
        return True
    if w in _USER_WHITELIST:
        return True

    # URLs / emails / handles
    if any(c in word for c in "@:/\\"):
        return True

    # Contains a digit
    if any(c.isdigit() for c in word):
        return True

    # Too short — high false-positive risk
    if len(w) < 3:
        return True

    # ALL CAPS (acronym, emphasis, name like JEE)
    if word.isupper() and len(word) > 1:
        return True

    # Mixed case (camelCase, names like "McDonald")
    if word[0].isupper() and any(c.isupper() for c in word[1:]):
        return True

    return False


def _correct_word(word):
    """Return (corrected_word, changed:bool). Preserves case for Title-case input."""
    if not _EN_AVAILABLE or _is_protected(word):
        return word, False

    was_title = word[0].isupper() and word[1:].islower()
    probe = word.lower()

    suggestion = _EN.correction(probe)
    if not suggestion or suggestion == probe:
        return word, False

    # Confidence: don't let the correction change length by more than 1,
    # and require the first letter to match (avoids wild guesses).
    if abs(len(suggestion) - len(probe)) > 1:
        return word, False
    if suggestion[0] != probe[0]:
        return word, False

    result = suggestion.capitalize() if was_title else suggestion
    if result.lower() == word.lower():
        return word, False
    return result, True


# Match letters + apostrophes (so "don't" stays intact)
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z']*")


def correct_text(text, enabled=True):
    """
    Autocorrect English text. Returns (corrected_text, changes_list).
    changes_list is a list of (original, corrected) tuples.
    """
    if not enabled or not text or not _EN_AVAILABLE:
        return text, []

    changes = []

    def replace(m):
        original = m.group(0)
        corrected, changed = _correct_word(original)
        if changed:
            changes.append((original, corrected))
            return corrected
        return original

    result = _WORD_RE.sub(replace, text)
    return result, changes


def diff_summary(changes, max_shown=5):
    """Short human-readable summary of changes."""
    if not changes:
        return ""
    parts = [f"'{o}'→'{c}'" for o, c in changes[:max_shown]]
    if len(changes) > max_shown:
        parts.append(f"+{len(changes) - max_shown} more")
    return ", ".join(parts)


def is_available():
    return _EN_AVAILABLE
    
    # ------------------------------------------------------------------
# Whitelist management (add / remove / list)
# ------------------------------------------------------------------
def add_word(word):
    """Add a word to the user whitelist. Returns a status string."""
    if not word or not word.strip():
        return "ERROR: no word given."
    w = word.strip()
    if " " in w:
        return "ERROR: whitelist words must be a single word (no spaces)."
    if any(c in w for c in "|/\\@:"):
        return "ERROR: whitelist words can't contain special characters."

    wl = w.lower()
    if wl in _USER_WHITELIST:
        return f"'{w}' is already in the whitelist."

    try:
        # Ensure file exists (with header)
        if not os.path.exists(WHITELIST_FILE):
            _load_user_whitelist()

        with open(WHITELIST_FILE, "a", encoding="utf-8") as f:
            f.write(w + "\n")
        reload_whitelist()
        return f"Added '{w}' to the autocorrect whitelist."
    except Exception as e:
        return f"ERROR: could not write whitelist: {e}"


def remove_word(word):
    """Remove a word from the user whitelist. Returns a status string."""
    if not word or not word.strip():
        return "ERROR: no word given."
    wl = word.strip().lower()
    if wl not in _USER_WHITELIST:
        return f"'{word.strip()}' isn't in the whitelist."

    try:
        with open(WHITELIST_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()

        new_lines = []
        removed = 0
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and stripped.lower() == wl:
                removed += 1
                continue
            new_lines.append(line)

        with open(WHITELIST_FILE, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        reload_whitelist()
        return f"Removed '{word.strip()}' from the whitelist."
    except Exception as e:
        return f"ERROR: could not update whitelist: {e}"


def list_words():
    """Return a printable list of the user's custom whitelist entries."""
    if not _USER_WHITELIST:
        return "The custom whitelist is empty."
    words = sorted(_USER_WHITELIST)
    lines = [f"Custom whitelist ({len(words)} word{'s' if len(words) != 1 else ''}):"]
    # 4 per line for compactness
    for i in range(0, len(words), 4):
        lines.append("  " + "  ".join(f"{w:<18}" for w in words[i:i+4]).rstrip())
    return "\n".join(lines)


def whitelist_tool(input_str):
    """
    Combined tool entry for the agent.
    Format:
      'add <word>'      → add a word
      'remove <word>'   → remove a word
      'list'            → list custom words
    """
    if not input_str or not input_str.strip():
        return "ERROR: format is 'add <word>', 'remove <word>', or 'list'."

    s = input_str.strip()
    low = s.lower()

    if low in ("list", "show", "words"):
        return list_words()

    for prefix in ("add ", "add:", "add_word ", "add_word:"):
        if low.startswith(prefix):
            return add_word(s[len(prefix):].strip())

    for prefix in ("remove ", "remove:", "delete ", "delete:", "del ", "del:"):
        if low.startswith(prefix):
            return remove_word(s[len(prefix):].strip())

    # Bare word with no prefix — assume add
    if " " not in s:
        return add_word(s)

    return "ERROR: format is 'add <word>', 'remove <word>', or 'list'."