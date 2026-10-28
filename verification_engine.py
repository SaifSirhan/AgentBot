from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
from uuid import uuid4


ERROR_REPLY = (
    "ERROR: I claimed to do something but the tool execution logs show no "
    "evidence of it. Please check the logs."
)

_ACTION_FORMS = {
    "translate": (r"translate", r"translated"),
    "save": (r"save|write|store|persist", r"saved|wrote|written|stored|persisted"),
    "create": (r"create|generate|produce", r"created|generated|produced"),
    "edit": (r"edit|modify|update|change|rewrite", r"edited|modified|updated|changed|rewrote|rewritten"),
    "delete": (r"delete|remove|erase", r"deleted|removed|erased"),
    "run": (
        r"run|execute|rerun",
        r"ran|executed|reran|(?:has|have|had|was|were|been)\s+"
        r"(?:(?:been|already|successfully)\s+)*run",
    ),
    "install": (r"install|reinstall", r"installed|reinstalled"),
    "download": (r"download", r"downloaded"),
    "upload": (r"upload", r"uploaded"),
    "read": (r"read|open|load|retrieve|fetch", r"read|opened|loaded|retrieved|fetched"),
    "search": (r"search|find|locate", r"searched|found|located|looked\s+up"),
    "send": (r"send|email|notify", r"sent|emailed|notified"),
    "publish": (r"publish|post", r"published|posted"),
    "deploy": (r"deploy|redeploy", r"deployed|redeployed"),
    "convert": (r"convert|transform", r"converted|transformed"),
    "rename": (r"rename", r"renamed"),
    "move": (r"move", r"moved"),
    "copy": (r"copy|duplicate", r"copied|duplicated"),
    "extract": (r"extract|unpack", r"extracted|unpacked"),
    "compress": (r"compress|archive|zip", r"compressed|archived|zipped"),
    "test": (r"test", r"tested"),
    "verify": (r"verify|validate|check|confirm", r"verified|validated|checked|confirmed"),
    "fix": (r"fix|repair|resolve", r"fixed|repaired|resolved"),
    "build": (r"build|rebuild|compile", r"built|rebuilt|compiled"),
    "commit": (r"commit", r"committed"),
    "push": (r"push", r"pushed"),
    "merge": (r"merge", r"merged"),
    "start": (r"start|launch", r"started|launched"),
    "stop": (r"stop|terminate", r"stopped|terminated"),
    "restart": (r"restart|reboot", r"restarted|rebooted"),
    "configure": (r"configure|set\s+up", r"configured|set\s+up"),
    "connect": (r"connect", r"connected"),
    "schedule": (r"schedule|book", r"scheduled|booked"),
    "cancel": (r"cancel", r"canceled|cancelled"),
    "export": (r"export", r"exported"),
    "import": (r"import", r"imported"),
    "analyze": (r"analyze|analyse", r"analyzed|analysed"),
    "process": (r"process", r"processed"),
    "summarize": (r"summarize|summarise", r"summarized|summarised"),
    "organize": (r"organize|organise", r"organized|organised"),
    "enable": (r"enable|activate", r"enabled|activated"),
    "disable": (r"disable|deactivate", r"disabled|deactivated"),
    "apply": (r"apply", r"applied"),
    "format": (r"format", r"formatted"),
    "clean": (r"clean", r"cleaned"),
    "restore": (r"restore|recover", r"restored|recovered"),
    "backup": (r"back\s+up", r"backed\s+up"),
}

_COMPLETION_FORMS = {
    "translate": r"translation|translating",
    "save": r"saving|writing|storing|persistence",
    "create": r"creation|creating|generation|generating|producing",
    "edit": r"editing|modification|modifying|updating|changing|rewriting",
    "delete": r"deletion|deleting|removal|removing|erasing",
    "run": r"execution|executing|running|rerunning",
    "install": r"installation|installing|reinstallation|reinstalling",
    "download": r"downloading",
    "upload": r"uploading",
    "read": r"reading|opening|loading|retrieval|retrieving|fetching",
    "search": r"searching|finding|locating",
    "send": r"sending|emailing|notification|notifying",
    "publish": r"publication|publishing|posting",
    "deploy": r"deployment|deploying|redeployment|redeploying",
    "convert": r"conversion|converting|transformation|transforming",
    "rename": r"renaming",
    "move": r"moving",
    "copy": r"copying|duplication|duplicating",
    "extract": r"extraction|extracting|unpacking",
    "compress": r"compression|compressing|archiving|zipping",
    "test": r"testing",
    "verify": r"verification|verifying|validation|validating|checking|confirmation|confirming",
    "fix": r"fixing|repairing|resolution|resolving",
    "build": r"building|rebuilding|compilation|compiling",
    "commit": r"committing",
    "push": r"pushing",
    "merge": r"merging",
    "start": r"starting|launching",
    "stop": r"stopping|termination|terminating",
    "restart": r"restarting|rebooting",
    "configure": r"configuration|configuring|setup|setting\s+up",
    "connect": r"connection|connecting",
    "schedule": r"scheduling|booking",
    "cancel": r"cancellation|canceling|cancelling",
    "export": r"exporting",
    "import": r"importing",
    "analyze": r"analysis|analyzing|analysing",
    "process": r"processing",
    "summarize": r"summary|summarization|summarisation|summarizing|summarising",
    "organize": r"organization|organisation|organizing|organising",
    "enable": r"enabling|activation|activating",
    "disable": r"disabling|deactivation|deactivating",
    "apply": r"applying",
    "format": r"formatting",
    "clean": r"cleaning",
    "restore": r"restoration|restoring|recovery|recovering",
    "backup": r"backup|backing\s+up",
}
_COMPLETION_ACTION_PATTERNS = {
    action: re.compile(rf"\b(?:{_ACTION_FORMS[action][0]}|{forms})\b", re.IGNORECASE)
    for action, forms in _COMPLETION_FORMS.items()
}

_FIRST_PERSON = (
    r"\b(?:i|we)(?:['\u2019]ve)?\s+"
    r"(?:(?:have|had|just|already|successfully|now|also)\s+)*"
)
_ACTION_PATTERNS = {
    action: re.compile(rf"\b(?:{past})\b|{_FIRST_PERSON}(?:{base})\b", re.IGNORECASE)
    for action, (base, past) in _ACTION_FORMS.items()
}
_COMPLETION = re.compile(
    r"\b(?:done|completed|finished|accomplished|successfully|all\s+set|"
    r"taken\s+care\s+of|good\s+to\s+go)\b|\b(?:is|are)\s+(?:now\s+)?ready\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ToolResult:
    success: bool
    output: Any
    error: str | None = None


Verifier = Callable[[Mapping[str, Any], ToolResult], bool]


@dataclass(frozen=True)
class ToolSpec:
    run: Callable[[dict[str, Any]], ToolResult]
    verifiers: Mapping[str, Verifier]


@dataclass(frozen=True)
class ToolExecution:
    turn_id: str
    call_id: str
    tool_name: str
    arguments_json: str
    success: bool
    output_json: str
    has_output: bool
    verified_actions: frozenset[str]
    error: str | None
    verification_errors: tuple[str, ...]


def _has_output(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_has_output(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_output(item) for item in value)
    return True


def _json_snapshot(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _check_actions(actions: Iterable[str]) -> frozenset[str]:
    selected = frozenset(actions)
    unknown = selected.difference(_ACTION_PATTERNS)
    if unknown:
        raise ValueError(f"Unknown action names: {sorted(unknown)}")
    return selected


class VerificationEngine:
    """One instance per turn; keep this object outside model-controlled code."""

    def __init__(
        self,
        tools: Mapping[str, ToolSpec],
        *,
        expected_actions: Iterable[str] = (),
    ) -> None:
        self._turn_id = uuid4().hex
        self._expected_actions = _check_actions(expected_actions)
        copied_tools = {}
        for name, spec in tools.items():
            _check_actions(spec.verifiers)
            copied_tools[name] = ToolSpec(spec.run, MappingProxyType(dict(spec.verifiers)))
        self._tools = MappingProxyType(copied_tools)
        self._executions: list[ToolExecution] = []

    @property
    def turn_id(self) -> str:
        return self._turn_id

    @property
    def executions(self) -> tuple[ToolExecution, ...]:
        return tuple(self._executions)

    def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        arguments_json = "null"
        output_json = "null"
        verified_actions: set[str] = set()
        verification_errors: list[str] = []
        success = False
        has_output = False
        try:
            if not isinstance(arguments, dict):
                raise TypeError("Tool arguments must be a JSON object")
            arguments_json = _json_snapshot(arguments)
            spec = self._tools[tool_name]
            result = spec.run(json.loads(arguments_json))
            if not isinstance(result, ToolResult):
                raise TypeError("Tool adapter must return ToolResult")
            if type(result.success) is not bool:
                raise TypeError("ToolResult.success must be an explicit boolean")
            output_json = _json_snapshot(result.output)
            result = ToolResult(result.success, json.loads(output_json), result.error)
            has_output = _has_output(result.output)
            success = result.success is True and result.error is None
            if success and has_output:
                for action, verify in spec.verifiers.items():
                    try:
                        snapshot = ToolResult(True, json.loads(output_json))
                        if verify(json.loads(arguments_json), snapshot) is True:
                            verified_actions.add(action)
                        else:
                            verification_errors.append(f"{action}: postcondition not verified")
                    except Exception as exc:
                        verification_errors.append(f"{action}: {type(exc).__name__}: {exc}")
        except Exception as exc:
            result = ToolResult(False, None, f"{type(exc).__name__}: {exc}")
            success = False
            has_output = False
            verified_actions.clear()

        self._executions.append(ToolExecution(
            turn_id=self._turn_id,
            call_id=uuid4().hex,
            tool_name=tool_name,
            arguments_json=arguments_json,
            success=success,
            output_json=output_json,
            has_output=has_output,
            verified_actions=frozenset(verified_actions),
            error=result.error,
            verification_errors=tuple(verification_errors),
        ))
        return result

    def claimed_actions(self, reply: str) -> frozenset[str]:
        text = self._normalize(reply)
        actions = {
            action for action, pattern in _ACTION_PATTERNS.items() if pattern.search(text)
        }
        if _COMPLETION.search(text):
            actions.update(
                action for action, pattern in _COMPLETION_ACTION_PATTERNS.items()
                if pattern.search(text)
            )
        return frozenset(actions)

    def filter_reply(self, reply: str) -> str:
        if not isinstance(reply, str):
            return ERROR_REPLY
        actions = self.claimed_actions(reply)
        completion_claim = bool(_COMPLETION.search(self._normalize(reply)))
        if not actions and not completion_claim:
            return reply

        required = actions
        if completion_claim:
            required = required | self._expected_actions
        if not required:
            return ERROR_REPLY

        evidenced = frozenset(
            action
            for execution in self._executions
            if execution.turn_id == self._turn_id
            and execution.success
            and execution.has_output
            for action in execution.verified_actions
        )
        return reply if required.issubset(evidenced) else ERROR_REPLY

    @staticmethod
    def _normalize(text: str) -> str:
        normalized = unicodedata.normalize("NFKC", text)
        return "".join(
            char for char in normalized
            if unicodedata.category(char) != "Cf" and char not in "*_`~"
        )
