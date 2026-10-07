"""
Static analysis and process inspection tools for AgentBot.
Pure stdlib + psutil. No external ML dependencies.
"""

import os
import re
import math
import hashlib
import datetime
from collections import Counter

try:
    import psutil
    PSUTIL_OK = True
except ImportError:
    PSUTIL_OK = False

QUARANTINE_DIR = os.path.join(
    os.environ.get("USERPROFILE", ""), "Downloads", "Quarantine"
)

# ---------------------------------------------------------------
# File scanning
# ---------------------------------------------------------------

SUSPICIOUS_STRINGS = [
    "powershell -enc", "powershell -encodedcommand", "-windowstyle hidden",
    "cmd.exe /c", "wscript.shell", 'createobject("wscript',
    "base64", "downloadstring", "invoke-expression", "iex(",
    "reg add hkcu", "schtasks /create", "net user", "net localgroup",
    "vssadmin delete", "bcdedit /set", "wbadmin delete",
    "cipher /w", "taskkill /f", "bitcoin", "wallet", "ransom",
    "onion", ".onion", "tor2web", "pastebin.com/raw",
]

SUSPICIOUS_EXTENSIONS = {
    ".exe", ".dll", ".scr", ".bat", ".cmd", ".ps1", ".vbs", ".js",
    ".jse", ".wsf", ".wsh", ".hta", ".jar", ".msi", ".com", ".pif",
}

KNOWN_GOOD_PREFIXES = (
    "C:\\Windows\\System32\\", "C:\\Windows\\SysWOW64\\",
    "C:\\Program Files\\", "C:\\Program Files (x86)\\",
)


def _shannon_entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = Counter(data)
    length = len(data)
    entropy = 0.0
    for c in counts.values():
        p = c / length
        entropy -= p * math.log2(p)
    return round(entropy, 4)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_strings(data: bytes, min_len: int = 4, limit: int = 200):
    # ASCII strings
    ascii_re = re.compile(rb"[\x20-\x7e]{%d,}" % min_len)
    strings = [m.decode("ascii", errors="ignore") for m in ascii_re.findall(data)]
    # Unicode strings (UTF-16LE)
    utf16_re = re.compile(rb"(?:[\x20-\x7e]\x00){%d,}" % min_len)
    for m in utf16_re.findall(data):
        strings.append(m.decode("utf-16-le", errors="ignore"))
    return strings[:limit]


def scan_file(path: str) -> str:
    """Static analysis of a single file. Returns a text report."""
    path = (path or "").strip().strip('"').strip("'")
    if not path:
        return "ERROR: no path given"
    if not os.path.isfile(path):
        return f"ERROR: not a file: {path}"

    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            data = f.read(min(size, 2_000_000))  # cap at 2MB for analysis

        sha = _sha256(path)
        entropy = _shannon_entropy(data)
        ext = os.path.splitext(path)[1].lower()
        strings = _extract_strings(data, min_len=4, limit=500)

        # Pattern hits
        joined = "\n".join(strings).lower()
        hits = [s for s in SUSPICIOUS_STRINGS if s.lower() in joined]

        # Heuristic score
        score = 0
        reasons = []

        if entropy > 7.2:
            score += 3
            reasons.append(f"very high entropy ({entropy}) — likely packed/encrypted")
        elif entropy > 6.8:
            score += 1
            reasons.append(f"elevated entropy ({entropy})")

        if ext in SUSPICIOUS_EXTENSIONS:
            score += 1
            reasons.append(f"executable extension '{ext}'")

        if len(hits) >= 3:
            score += 3
            reasons.append(f"{len(hits)} suspicious string patterns")
        elif hits:
            score += 1
            reasons.append(f"{len(hits)} suspicious string pattern(s)")

        if not path.startswith(KNOWN_GOOD_PREFIXES) and ext in (".dll", ".exe"):
            score += 1
            reasons.append("executable outside standard Program Files")

        verdict = (
            "LIKELY MALICIOUS" if score >= 5 else
            "SUSPICIOUS" if score >= 2 else
            "LIKELY CLEAN"
        )

        lines = [
            f"=== Scan report: {os.path.basename(path)} ===",
            f"Path:    {path}",
            f"Size:    {size:,} bytes",
            f"SHA256:  {sha}",
            f"Entropy: {entropy}",
            f"Ext:     {ext or '(none)'}",
            f"Score:   {score}/8",
            f"Verdict: {verdict}",
            "",
        ]
        if reasons:
            lines.append("Reasons:")
            lines.extend(f"  - {r}" for r in reasons)
        else:
            lines.append("No heuristic triggers.")
        if hits:
            lines.append("")
            lines.append("Suspicious string hits:")
            lines.extend(f"  - {h}" for h in hits[:10])
        # Include a small sample of interesting strings for the LLM
        interesting = [s for s in strings if len(s) > 8 and not s.startswith("C:\\")]
        if interesting:
            lines.append("")
            lines.append("Sample strings (first 15):")
            lines.extend(f"  {s[:120]}" for s in interesting[:15])

        return "\n".join(lines)

    except Exception as e:
        return f"ERROR scanning {path}: {e}"


# ---------------------------------------------------------------
# Process inspection
# ---------------------------------------------------------------

def scan_process(name_or_pid: str) -> str:
    """Inspect a running process. Input: process name (partial) or PID."""
    if not PSUTIL_OK:
        return "ERROR: psutil not installed"

    target = (name_or_pid or "").strip()
    if not target:
        return "ERROR: give a process name or PID"

    try:
        pid = int(target)
        matches = [psutil.Process(pid)]
    except ValueError:
        matches = []
        for p in psutil.process_iter(["pid", "name"]):
            try:
                if target.lower() in (p.info["name"] or "").lower():
                    matches.append(p)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    if not matches:
        return f"No running process matches '{target}'"

    lines = [f"=== Process scan: {target} ===", ""]
    for p in matches[:5]:  # cap at 5 matches
        try:
            info = p.as_dict(attrs=[
                "pid", "name", "exe", "cmdline", "username",
                "create_time", "cpu_percent", "memory_percent",
            ])
            lines.append(f"PID {info['pid']}: {info['name']}")
            if info.get("exe"):
                lines.append(f"  path: {info['exe']}")
            if info.get("cmdline"):
                lines.append(f"  cmd:  {' '.join(info['cmdline'])[:200]}")
            if info.get("username"):
                lines.append(f"  user: {info['username']}")
            if info.get("create_time"):
                ts = datetime.datetime.fromtimestamp(info["create_time"])
                lines.append(f"  started: {ts.strftime('%Y-%m-%d %H:%M:%S')}")
            try:
                conns = p.net_connections(kind="inet")
                if conns:
                    lines.append(f"  network: {len(conns)} connection(s)")
                    for c in conns[:5]:
                        lines.append(f"    {c.laddr} -> {c.raddr} [{c.status}]")
            except (psutil.AccessDenied, Exception):
                pass
            lines.append("")
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            lines.append(f"PID {p.pid}: access denied ({e})")
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------
# Quarantine
# ---------------------------------------------------------------

def quarantine_file(path: str) -> str:
    """Move a suspicious file to Downloads\\Quarantine\\. Reversible."""
    import shutil
    path = (path or "").strip().strip('"').strip("'")
    if not os.path.isfile(path):
        return f"ERROR: not a file: {path}"
    try:
        os.makedirs(QUARANTINE_DIR, exist_ok=True)
        name = os.path.basename(path)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = os.path.join(QUARANTINE_DIR, f"{ts}_{name}")
        shutil.move(path, dest)
        return f"Quarantined: {path}\nMoved to: {dest}\nRestore by moving it back."
    except Exception as e:
        return f"ERROR quarantining: {e}"


def list_quarantine() -> str:
    """List quarantined files."""
    if not os.path.isdir(QUARANTINE_DIR):
        return "Quarantine folder is empty (does not exist yet)."
    files = os.listdir(QUARANTINE_DIR)
    if not files:
        return "Quarantine folder is empty."
    lines = [f"Quarantined files ({len(files)}):"]
    for f in sorted(files):
        full = os.path.join(QUARANTINE_DIR, f)
        size = os.path.getsize(full)
        lines.append(f"  {f}  ({size:,} bytes)")
    return "\n".join(lines)
