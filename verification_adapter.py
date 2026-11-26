"""Host adapter that connects AgentBot's turn log to VerificationEngine.

The engine verifies recorded executions, so this adapter replays the turn's
successful tool outputs as evidence. Mutating tools evidence their own action;
read-only tools evidence `READ_ACTION`, which is what a reply like "I searched
the web" actually claims. A tool that only looked at something still proves
nothing about a mutation.
"""
from __future__ import annotations

import ctypes
import hashlib
import hmac
import json
import os
import re
import stat
import traceback
from collections.abc import Iterable
from threading import Event

from verification_engine import (
    _ACTION_FORMS,
    _COMPLETION,
    _FIRST_PERSON,
    ERROR_REPLY,  # noqa: F401  re-export: agent.py, tests/test_provider_stop.py, tests/test_verification_turn.py read verification_adapter.ERROR_REPLY
    ToolResult,
    ToolSpec,
    VerificationEngine,
    _check_cancelled,
)

RETRY_MESSAGE = (
    "ERROR: Your claim was rejected by the verification engine because you "
    "did not actually run the tool. Try again."
)
MAX_RETRIES = 1

_PYTHON_RECEIPT_KEY = os.urandom(32)
_PYTHON_RECEIPT_MARKER = "[KESTREL_PYTHON_RECEIPT] "
_PYTHON_MAX_TARGETS = 64
_PYTHON_MAX_HASH_BYTES = 32 * 1024 * 1024
_PYTHON_MAX_RECEIPT_BYTES = 512 * 1024
_PYTHON_STATE_KEYS = frozenset({
    "kind", "size", "mtime_ns", "dev", "ino", "sha256", "link_target",
})
_PYTHON_FILE_ACTIONS = frozenset({"save", "create", "edit", "delete"})
_PYTHON_PATH_PATTERN = re.compile(
    r"(?P<quoted>[\"'`](?P<quoted_path>(?:[A-Za-z]:[\\/]|\\\\|\.{1,2}[\\/])"
    r"[^\"'\r\n`]+)[\"'`])"
    r"|(?P<absolute>(?:[A-Za-z]:[\\/]|\\\\)[^\s\"'`<>|]+)"
    r"|(?P<relative>(?:\.{1,2}[\\/])?(?:[A-Za-z0-9_.-]+[\\/])+"
    r"[A-Za-z0-9_.-]+)"
    r"|(?P<filename>(?<![A-Za-z0-9_.-])[A-Za-z0-9_-]+"
    r"(?:\.[A-Za-z0-9_-]+)+)(?![A-Za-z0-9_-])"
)
_PYTHON_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)

ENFORCED_ACTIONS = frozenset({
    "translate",
    "save", "create", "edit", "delete", "move", "copy", "send", "publish",
    "run", "schedule", "install", "download", "upload", "deploy", "convert",
    "rename", "compress", "extract", "restart", "stop", "backup", "restore",
    "connect", "configure", "enable", "disable", "apply", "format", "clean",
})

# The action a successful observation evidences. Deliberately absent from
# ENFORCED_ACTIONS: it satisfies "I searched / I read / I checked" and can
# never satisfy a mutation claim.
READ_ACTION = "read"

# Names the registry has not migrated yet. Registered tools report their own
# effect_class, so this list only covers the legacy dispatcher.
LEGACY_READ_ONLY_TOOLS = frozenset({
    "rag_search", "web_fetch", "map_site", "crawl_site", "crawl_managed",
    "describe_image", "describe_images", "analyze_screenshot", "ocr_image",
    "extract_text", "scrutinize_text", "read_screen", "describe_screen",
    "get_pixel_color", "clipboard_read", "show_last_result", "inspect_docx",
    "list_symbols", "repo_map", "list_dir", "system_status",
    "check_battery_health", "weather", "air_quality", "news", "search_web",
    "serp_search", "read_file", "find_files", "current_time", "read_artifact",
    "uia_dump_tree", "uia_get_text", "watch_folder_list",
})


def tool_is_read_only(tool: str) -> bool:
    """True when the tool only observes. The registry answers for itself."""
    try:
        import agent
        spec = agent.TOOL_REGISTRY.get(tool)
    except Exception:
        spec = None
    if spec is not None:
        return getattr(spec, "effect_class", None) == "read_only"
    return tool in LEGACY_READ_ONLY_TOOLS


def _first_person_claim(action: str, reply: str) -> bool:
    """Whether the reply says the *agent* performed this action.

    The engine matches bare past tense ("published") with no subject check, so
    a sentence reporting someone else's action reads as the agent's claim.
    Mutation verbs keep their own guard; this only tells "DeepSeek published a
    paper" apart from "I published the site".
    """
    forms = _ACTION_FORMS.get(action)
    if not forms:
        return True
    base, past = forms
    pattern = re.compile(
        rf"{_FIRST_PERSON}[^.!?]{{0,40}}\b(?:{base}|{past})\b", re.IGNORECASE)
    return bool(pattern.search(reply))


def _reported_actions(reply: str, enforced, sources) -> set:
    """Enforced verbs that a successful observation's own text reports.

    Exempting these cannot weaken a real claim: it requires a read-only tool
    that ran, returned the verb in its output, and a reply that never says the
    agent did it.
    """
    if not sources:
        return set()
    reported = set()
    for action in enforced:
        forms = _ACTION_FORMS.get(action)
        if not forms:
            continue
        base, past = forms
        if _first_person_claim(action, reply):
            continue
        verb = re.compile(rf"\b(?:{base}|{past})\b", re.IGNORECASE)
        if any(verb.search(source) for source in sources):
            reported.add(action)
    return reported


def _post_to_social(tool_input):
    action = tool_input.split("|", 1)[0].strip().lower()
    return {"publish"} if action == "post" else set()


def _telegram_delete(tool_input):
    parts = tool_input.split("|")
    confirm = parts[-1].strip().lower() if len(parts) > 1 else ""
    return {"delete"} if confirm in ("confirm", "all") else set()


def _schedule_task(tool_input):
    action = tool_input.split("|", 1)[0].strip().lower()
    return set() if action in ("list", "remove") else {"schedule"}


def _whitelist(tool_input):
    action = tool_input.split("|", 1)[0].strip().lower()
    return {"delete"} if action == "remove" else set()


def _skill_manage(tool_input):
    action = tool_input.split("|", 1)[0].strip().lower()
    return {"create": {"create"}, "update": {"edit"}, "delete": {"delete"}}.get(action, set())


def _todo(tool_input):
    action = tool_input.split(":", 1)[0].strip().lower()
    return {"delete"} if action in ("remove", "clear") else set()


def _media_control(tool_input):
    return {"stop"} if tool_input.strip().lower() == "stop" else set()


TOOL_EVIDENCE = {
    "write_file": frozenset({"save", "create"}),
    "patch_file": frozenset({"edit"}),
    "make_folder": frozenset({"create"}),
    "move_files": frozenset({"move"}),
    "edit_document": frozenset({"edit", "save"}),
    "edit_docx_safe": frozenset({"edit", "save"}),
    "post_to_social": _post_to_social,
    "send_telegram_message": frozenset({"send"}),
    "telegram_user_send": frozenset({"send"}),
    "telegram_user_edit": frozenset({"edit"}),
    "telegram_user_delete": _telegram_delete,
    "send_image_telegram": frozenset({"send"}),
    "send_gif": frozenset({"send"}),
    "reply_with_gif": frozenset({"send"}),
    "send_maps_url": frozenset({"send"}),
    "send_maps_list": frozenset({"send"}),
    "generate_image": frozenset({"create"}),
    "save_screenshot": frozenset({"save", "create"}),
    "save_macro": frozenset({"save"}),
    "play_macro": frozenset({"run"}),
    "run_recipe": frozenset({"run"}),
    "run_command": frozenset({"run"}),
    "execute_python": frozenset({"run"}),
    "set_reminder": frozenset({"schedule"}),
    "set_reminder_at": frozenset({"schedule"}),
    "schedule_task": _schedule_task,
    "remember": frozenset({"save"}),
    "forget": frozenset({"delete"}),
    "whitelist": _whitelist,
    "skill_manage": _skill_manage,
    "todo": _todo,
    "set_name": frozenset({"edit", "rename"}),
    "media_control": _media_control,
    "watch_folder": frozenset({"start"}),
    "stop_watching": frozenset({"stop"}),
    "play_on_youtube": frozenset({"start"}),
    "stream_media": frozenset({"start"}),
    "quarantine_file": frozenset({"move"}),
    "restart_pc": frozenset({"restart"}),
    "shutdown_pc": frozenset({"stop"}),
    "sleep_pc": frozenset({"stop"}),
    "lock_pc": frozenset({"stop"}),
    "deep_research": frozenset({"save", "create"}),
}


def _actions_for(tool, tool_input):
    entry = TOOL_EVIDENCE.get(tool)
    if entry is None:
        if tool_is_read_only(tool):
            return frozenset({READ_ACTION})
        return frozenset()
    if callable(entry):
        return frozenset(entry(tool_input))
    return entry


def _text(output):
    if output is None:
        return ""
    return output if isinstance(output, str) else str(output)


def _source_text(execution) -> str:
    """The recorded output as plain text, for verb matching.

    The engine stores it JSON-encoded, so a bare `execution.output` does not
    exist — reading the wrong attribute here would raise inside
    reply_is_supported's catch-all and silently block every reply.
    """
    raw = getattr(execution, "output_json", None)
    if raw is None:
        return ""
    try:
        return _text(json.loads(raw))
    except (TypeError, ValueError):
        return _text(raw)


def _verified(_arguments, result):
    return (
        result.success is True
        and isinstance(result.output, str)
        and bool(result.output.strip())
    )


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_identity(saved_stat):
    return (
        saved_stat.st_dev,
        saved_stat.st_ino,
        saved_stat.st_size,
        saved_stat.st_mtime_ns,
    )


def _open_python_file_no_follow(path):
    if os.name == "nt":
        import msvcrt

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
            ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
        ]
        kernel.CreateFileW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int
        handle = kernel.CreateFileW(
            os.fspath(path), 0x80000000, 0x00000007, None, 3,
            0x00200000 | 0x02000000, None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)
        except Exception:
            kernel.CloseHandle(handle)
            raise
        try:
            return os.fdopen(descriptor, "rb")
        except Exception:
            os.close(descriptor)
            raise
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(path, flags), "rb")


def _python_state(saved_stat, kind, *, sha256=None, link_target=None):
    return {
        "kind": kind,
        "size": saved_stat.st_size,
        "mtime_ns": saved_stat.st_mtime_ns,
        "dev": saved_stat.st_dev,
        "ino": saved_stat.st_ino,
        "sha256": sha256,
        "link_target": link_target,
    }


def _python_file_state(path):
    try:
        saved_stat = os.lstat(path)
    except FileNotFoundError:
        return None
    if getattr(saved_stat, "st_file_attributes", 0) & 0x400:
        link_target = os.readlink(path)
        if _file_identity(os.lstat(path)) != _file_identity(saved_stat):
            raise OSError("reparse point changed while it was being receipted")
        return _python_state(saved_stat, "reparse", link_target=link_target)
    if stat.S_ISDIR(saved_stat.st_mode):
        return _python_state(saved_stat, "directory")
    if not stat.S_ISREG(saved_stat.st_mode):
        raise OSError("not a regular file or directory")
    if saved_stat.st_size > _PYTHON_MAX_HASH_BYTES:
        raise OSError("file too large to receipt")
    digest = hashlib.sha256()
    with _open_python_file_no_follow(path) as source:
        opened_stat = os.fstat(source.fileno())
        if not stat.S_ISREG(opened_stat.st_mode) \
                or _file_identity(opened_stat) != _file_identity(saved_stat):
            raise OSError("file identity changed while it was being receipted")
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
        if _file_identity(os.fstat(source.fileno())) != _file_identity(opened_stat):
            raise OSError("file changed while it was being receipted")
    return _python_state(saved_stat, "file", sha256=digest.hexdigest())


def snapshot_python_file(path):
    return _python_file_state(path)


def _python_receipt_signature(payload):
    canonical = json.dumps(
        payload, ensure_ascii=False, allow_nan=False,
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(_PYTHON_RECEIPT_KEY, canonical, hashlib.sha256).hexdigest()


def append_python_receipts(output, before):
    output = output.replace(_PYTHON_RECEIPT_MARKER, "[untrusted receipt marker removed]")
    if len(before) > _PYTHON_MAX_TARGETS:
        return "ERROR: Python mutation receipt target limit exceeded."
    receipts = []
    try:
        for key, previous in before.items():
            current = _python_file_state(key)
            if previous is None and current is None:
                continue
            if previous is not None and previous == current:
                continue
            receipts.append({"path": key, "before": previous, "after": current})
    except Exception as exc:  # noqa: BLE001 - incomplete snapshots cannot prove mutations
        return f"ERROR: Python mutation receipt snapshot failed: {type(exc).__name__}."
    if not receipts:
        return output
    payload = {"receipts": receipts}
    envelope = {
        "payload": payload,
        "signature": _python_receipt_signature(payload),
    }
    marker = _PYTHON_RECEIPT_MARKER + json.dumps(
        envelope, ensure_ascii=False, allow_nan=False,
        sort_keys=True, separators=(",", ":"),
    )
    if len(marker.encode("utf-8")) > _PYTHON_MAX_RECEIPT_BYTES:
        return "ERROR: Python mutation receipt size limit exceeded."
    return f"{output}\n\n{marker}"


def _valid_python_state(state):
    if state is None:
        return True
    if not isinstance(state, dict) or set(state) != _PYTHON_STATE_KEYS:
        return False
    if state["kind"] not in ("file", "directory", "reparse"):
        return False
    if any(type(state[key]) is not int or state[key] < 0
           for key in ("size", "mtime_ns", "dev", "ino")):
        return False
    if state["kind"] == "file":
        return (
            isinstance(state["sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", state["sha256"]) is not None
            and state["link_target"] is None
        )
    if state["sha256"] is not None:
        return False
    if state["kind"] == "directory":
        return state["link_target"] is None
    return isinstance(state["link_target"], str) and bool(state["link_target"])


def split_python_receipt(output):
    if not isinstance(output, str) or output.count(_PYTHON_RECEIPT_MARKER) != 1:
        return output, ""
    body, receipt = output.split(_PYTHON_RECEIPT_MARKER, 1)
    return body, _PYTHON_RECEIPT_MARKER + receipt


def _verified_python_receipts(output):
    if not isinstance(output, str):
        return ()
    if output.count(_PYTHON_RECEIPT_MARKER) != 1:
        return ()
    marker_at = output.find(_PYTHON_RECEIPT_MARKER)
    receipt_text = output[marker_at + len(_PYTHON_RECEIPT_MARKER):]
    if len(receipt_text.encode("utf-8")) > _PYTHON_MAX_RECEIPT_BYTES:
        return ()
    try:
        envelope = json.loads(receipt_text)
    except (TypeError, ValueError):
        return ()
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "signature"}:
        return ()
    payload = envelope["payload"]
    signature = envelope["signature"]
    if not isinstance(payload, dict) or set(payload) != {"receipts"}:
        return ()
    if not isinstance(signature, str) \
            or re.fullmatch(r"[0-9a-f]{64}", signature) is None:
        return ()
    try:
        expected = _python_receipt_signature(payload)
    except (TypeError, ValueError):
        return ()
    if hmac.compare_digest(signature, expected) is not True:
        return ()
    receipts = payload["receipts"]
    if not isinstance(receipts, list) or not 1 <= len(receipts) <= _PYTHON_MAX_TARGETS:
        return ()
    verified = []
    seen = set()
    for receipt in receipts:
        if not isinstance(receipt, dict) \
                or set(receipt) != {"path", "before", "after"}:
            return ()
        path = receipt["path"]
        before = receipt["before"]
        after = receipt["after"]
        if not isinstance(path, str) or not path or "\x00" in path \
                or not os.path.isabs(path):
            return ()
        normalized = os.path.normcase(path)
        if normalized in seen:
            return ()
        seen.add(normalized)
        if not _valid_python_state(before) or not _valid_python_state(after):
            return ()
        if before == after:
            return ()
        try:
            if _python_file_state(path) != after:
                return ()
        except Exception:  # noqa: BLE001 - unavailable state invalidates the receipt
            return ()
        if after is None:
            actions = frozenset({"delete"})
        else:
            actions = frozenset({
                "save", "create" if before is None else "edit",
            })
        verified.append((path, actions))
    return tuple(verified)


def verified_python_actions(output):
    return frozenset().union(*(
        actions for _path, actions in _verified_python_receipts(output)
    ))


def _verified_python(_arguments, result):
    return _verified(_arguments, result) and bool(verified_python_actions(result.output))


def _verified_docx(_arguments, result):
    """Require a receipt that still matches the file on disk."""
    if not _verified(_arguments, result):
        return False
    try:
        payload = json.loads(result.output)
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return False
    changes = payload.get("changes")
    if type(changes) is not int or changes <= 0:
        return False
    receipt = payload.get("receipt")
    if not isinstance(receipt, dict):
        return False
    path = receipt.get("path")
    size = receipt.get("size")
    mtime_ns = receipt.get("mtime_ns")
    sha256 = receipt.get("sha256")
    if not isinstance(path, str) or not path:
        return False
    if type(size) is not int or size < 0:
        return False
    if type(mtime_ns) is not int or mtime_ns < 0:
        return False
    if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
        return False
    try:
        saved_stat = os.stat(path)
        if not stat.S_ISREG(saved_stat.st_mode):
            return False
        if saved_stat.st_size != size or saved_stat.st_mtime_ns != mtime_ns:
            return False
        return _sha256_file(path) == sha256
    except OSError:
        return False


def _tool_succeeded(tool, text):
    if tool == "edit_docx_safe":
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            return False
        return isinstance(payload, dict) and payload.get("ok") is True
    return bool(text.strip()) and not text.lstrip().lower().startswith("error:")


def build_engine(executions: Iterable[tuple[str, object, object]],
                 stop_event: Event | None = None) -> VerificationEngine:
    _check_cancelled(stop_event)
    tools = {}
    pending = []
    for index, (tool, tool_input, output) in enumerate(executions):
        _check_cancelled(stop_event)
        actions = _actions_for(tool, _text(tool_input))
        text = _text(output)
        python_actions = verified_python_actions(text) if tool == "execute_python" else frozenset()
        actions = actions | python_actions
        if not actions:
            continue
        success = _tool_succeeded(tool, text)
        if tool == "edit_docx_safe":
            verifier = _verified_docx
        elif tool == "execute_python" and python_actions:
            verifier = _verified_python
        else:
            verifier = _verified
        key = f"{tool}:{index}"
        tools[key] = ToolSpec(
            run=(lambda _arguments, _text=text, _success=success: ToolResult(
                _success,
                _text if _success else None,
                None if _success else "tool reported an error",
            )),
            verifiers={action: verifier for action in actions},
        )
        pending.append((key, _text(tool_input)))
    engine = VerificationEngine(tools, stop_event=stop_event)
    for key, tool_input in pending:
        _check_cancelled(stop_event)
        engine.execute_tool(key, {"tool_input": tool_input})
    _check_cancelled(stop_event)
    return engine


def _extract_python_path_targets(text):
    url_spans = [match.span() for match in _PYTHON_URL_PATTERN.finditer(text)]
    targets = []
    for match in _PYTHON_PATH_PATTERN.finditer(text):
        if any(start <= match.start() < end for start, end in url_spans):
            continue
        value = match.group("quoted_path") or match.group("absolute") \
            or match.group("relative") or match.group("filename")
        value = value.strip().rstrip(".,;:!?)]}")
        if value:
            targets.append(value)
    return targets


def _python_path_matches(target, receipt_path):
    if re.match(r"^[A-Za-z]:[\\/]", target) or target.startswith("\\\\"):
        return os.path.normcase(os.path.normpath(target)) \
            == os.path.normcase(os.path.normpath(receipt_path))
    normalized = os.path.normcase(os.path.normpath(target))
    receipt = os.path.normcase(os.path.normpath(receipt_path))
    if "/" not in target and "\\" not in target:
        return os.path.basename(receipt) == normalized
    return receipt.endswith(os.sep + normalized)


def _claimed_python_file_targets(reply, actions):
    matches = []
    for action in actions:
        base, past = _ACTION_FORMS[action]
        pattern = re.compile(
            rf"\b(?:{past})\b|{_FIRST_PERSON}(?:{base})\b", re.IGNORECASE)
        matches.extend(
            (match.start(), match.end(), action) for match in pattern.finditer(reply))
    matches.sort(key=lambda match: match[0])
    targets = {action: set() for action in actions}
    for index, (start, end, action) in enumerate(matches):
        following = re.search(r"(?:[.!?](?=\s|$)|\n)", reply[end:])
        sentence_end = end + following.start() if following else len(reply)
        next_start = matches[index + 1][0] if index + 1 < len(matches) else sentence_end
        segment = reply[end:min(next_start, sentence_end)]
        found = _extract_python_path_targets(segment)
        if not found:
            preceding = list(re.finditer(
                r"(?:[.!?](?=\s|$)|\n)", reply[:start]))
            sentence_start = preceding[-1].end() if preceding else 0
            found = _extract_python_path_targets(reply[sentence_start:start])
        targets[action].update(found)
    return targets


def _python_actions_for_reply(reply, actions, receipts):
    if not receipts:
        return frozenset()
    available = frozenset().union(*(actions for _path, actions in receipts))
    targets = _claimed_python_file_targets(reply, actions)
    supported = set()
    for action in actions & available:
        required = targets[action]
        if all(any(action in receipt_actions and _python_path_matches(target, path)
                   for path, receipt_actions in receipts)
               for target in required):
            supported.add(action)
    return frozenset(supported)


def reply_is_supported(engine: VerificationEngine, reply: str,
                       stop_event: Event | None = None) -> bool:
    """True when the reply may be sent; faults fail closed, cancellation propagates."""
    def check():
        _check_cancelled(stop_event)
        engine.check_cancelled()

    try:
        check()
        if not isinstance(reply, str):
            return False
        engine.filter_reply(reply)
        check()
        claimed = engine.claimed_actions(reply)
        evidenced = set()
        non_python_evidenced = set()
        python_receipts = []
        sources = []
        for execution in engine.executions:
            check()
            if execution.success and execution.has_output:
                evidenced.update(execution.verified_actions)
                if execution.tool_name.split(":", 1)[0] == "execute_python":
                    python_receipts.extend(_verified_python_receipts(
                        _source_text(execution)))
                    non_python_evidenced.update(
                        execution.verified_actions - _PYTHON_FILE_ACTIONS)
                else:
                    non_python_evidenced.update(execution.verified_actions)
                if READ_ACTION in execution.verified_actions:
                    sources.append(_source_text(execution))
        enforced = claimed & ENFORCED_ACTIONS
        python_supported = _python_actions_for_reply(
            reply, claimed & _PYTHON_FILE_ACTIONS, python_receipts)
        check()
        # A verb that the observed source itself reports, in a sentence that
        # never says the agent did it, is quotation — not a claim to prove.
        unmet = enforced - non_python_evidenced - python_supported \
            - _reported_actions(reply, enforced, sources)
        check()
        if _COMPLETION.search(VerificationEngine._normalize(reply)):
            return bool(evidenced) and not unmet
        return not unmet
    except InterruptedError:
        raise
    except Exception:
        # Cancellation wins even when a callback reports a different failure.
        try:
            check()
        except InterruptedError:
            raise
        except Exception:
            pass
        traceback.print_exc()
        return False
