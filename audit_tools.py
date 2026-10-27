#!/usr/bin/env python3
"""Audit every tool in agent.VALID_TOOLS.

Prints one line per tool:
  [OK] name       implementation exists and is callable
  [MISSING] name  no dispatch target or target is not callable
  [ERROR] name    import/probe failure

Most tools map to a same-name function in agent.py. The exceptions are
listed in SPECIAL_TARGETS. set_volume and media_control also get a safe,
read-only probe so a pycaw/pyautogui API break is caught even when the
function still exists; stream_media probes that yt-dlp imports, and the
office tools probe that pywin32 imports.
No API keys are used and no tool is executed.

Run: python audit_tools.py
Exit code: 0 when every tool is OK, 1 when anything is broken.
"""
from __future__ import annotations

import contextlib
import importlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Tools whose implementation is not simply the same-named agent attribute.
SPECIAL_TARGETS = {
    "chat": ("agent._execute_tool_impl",),
    "done": ("agent._run_agent_turn_impl",),
    "crawl_managed": ("crawl_managed.crawl_managed",),
    "edit_document": (
        "document_tools.edit_docx",
        "document_tools.edit_xlsx",
        "document_tools.edit_pptx",
        "document_tools.edit_pdf",
    ),
    "forget": ("agent.forget_fact",),
    "inspect_docx": ("file_tools.inspect_docx",),
    "list_quarantine": ("security_tools.list_quarantine",),
    "load_skill": ("agent.load_skill_tool",),
    "post_to_social": ("typefully_tool.post_to_social",),
    "quarantine_file": ("security_tools.quarantine_file",),
    "rag_search": ("rag_tool.search_documents",),
    "read_word_doc": ("office_control.read_word_doc",),
    "remember": ("agent.remember_fact",),
    "run_recipe": ("recipes.run_recipe",),
    "scan_file": ("security_tools.scan_file",),
    "scan_process": ("security_tools.scan_process",),
    "serp_search": ("serpapi_tool.serp_search",),
    "set_word_language": ("office_control.set_word_language",),
}


def resolve_target(target: str, agent_module):
    """Resolve 'agent.foo' or 'module.foo' to a Python object."""
    if target.startswith("agent."):
        return getattr(agent_module, target.split(".", 1)[1])
    module_name, _, attribute = target.partition(".")
    module = importlib.import_module(module_name)
    return getattr(module, attribute)


def _probe_set_volume(agent_module):
    if not getattr(agent_module, "PYCAW_AVAILABLE", False):
        return "pycaw is not importable"
    interface = agent_module._get_volume_interface()
    level = float(interface.GetMasterVolumeLevelScalar())
    if not 0.0 <= level <= 1.0:
        return f"invalid master volume scalar: {level}"
    interface.GetMute()
    return None


def _probe_media_control(agent_module):
    pyautogui = getattr(agent_module, "pyautogui", None)
    if pyautogui is None:
        return "pyautogui is not imported"
    supported = set(getattr(pyautogui, "KEYBOARD_KEYS", ()))
    required = {"playpause", "nexttrack", "prevtrack", "stop"}
    missing = sorted(required - supported)
    if missing:
        return f"pyautogui lacks media keys: {', '.join(missing)}"
    return None


def _probe_stream_media(agent_module):
    if not getattr(agent_module, "YTDLP_AVAILABLE", False):
        return "yt-dlp is not importable"
    return None


def _probe_office_com(agent_module):
    office_control = importlib.import_module("office_control")
    if not getattr(office_control, "PYWIN32_AVAILABLE", False):
        return "pywin32 is not importable"
    return None


PROBES = {
    "set_volume": _probe_set_volume,
    "media_control": _probe_media_control,
    "stream_media": _probe_stream_media,
    "read_word_doc": _probe_office_com,
    "set_word_language": _probe_office_com,
}


def audit_tool(name: str, agent_module):
    """Return (status, detail) for one tool."""
    for target in SPECIAL_TARGETS.get(name, (f"agent.{name}",)):
        try:
            implementation = resolve_target(target, agent_module)
        except AttributeError as e:
            return "MISSING", f"{target}: {e}"
        except Exception as e:
            return "ERROR", f"{target}: {type(e).__name__}: {e}"
        if not callable(implementation):
            return "MISSING", f"{target} is not callable"

    probe = PROBES.get(name)
    if probe is not None:
        try:
            problem = probe(agent_module)
        except Exception as e:
            return "ERROR", f"{type(e).__name__}: {e}"
        if problem:
            return "ERROR", problem
    return "OK", ""


def audit(agent_module):
    """Return [(tool_name, status, detail)] sorted by tool name."""
    return [
        (name, *audit_tool(name, agent_module))
        for name in sorted(agent_module.VALID_TOOLS)
    ]


def main():
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured), \
                contextlib.redirect_stderr(captured):
            import agent as agent_module
    except Exception as e:
        print(captured.getvalue(), end="")
        print(f"[ERROR] agent import failed: {type(e).__name__}: {e}")
        return 2

    results = audit(agent_module)
    counts = {"OK": 0, "MISSING": 0, "ERROR": 0}
    for name, status, detail in results:
        counts[status] += 1
        suffix = f" - {detail}" if detail else ""
        print(f"[{status}] {name}{suffix}")

    print()
    print(
        f"Audited {len(results)} tools: {counts['OK']} OK, "
        f"{counts['MISSING']} MISSING, {counts['ERROR']} ERROR")
    print("No API/HTTP calls were made; only wiring and read-only probes.")
    return 0 if counts["MISSING"] == 0 and counts["ERROR"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
