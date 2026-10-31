"""State-aware loop detection for autonomous tool execution."""
from __future__ import annotations

import hashlib
import json
from collections import deque
from enum import Enum
from typing import Any, Collection

from tool_registry import ToolRegistry


class GuardVerdict(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    BLOCK = "BLOCK"


class LoopGuard:
    """Track recent (tool, input hash, outcome hash) triples.

    Three rules stop the loops agents actually fall into:

    * The same (tool, input) three times in the window blocks the next call,
      even when the output text changes slightly.
    * Five completed actions plus the proposed action forming an alternating
      A B A B A B pattern block the proposed call.
    * A polling tool called five times in a row without another action
      between calls blocks the sixth call.

    Polling tools are safe to call repeatedly while waiting for a state
    change, so short runs pass. Control tools are exempt entirely.
    """

    MAX_CONSECUTIVE_POLLS = 5

    def __init__(
        self,
        *,
        registry: ToolRegistry | None = None,
        polling_tools: Collection[str] = (),
        exempt_tools: Collection[str] = (),
        window_size: int = 10,
        threshold: int = 3,
    ):
        if type(window_size) is not int or window_size < 1:
            raise ValueError("window_size must be a positive integer")
        if type(threshold) is not int or not 1 <= threshold <= window_size:
            raise ValueError(
                "threshold must be an integer from 1 through window_size")
        for label, names in (
                ("polling_tools", polling_tools),
                ("exempt_tools", exempt_tools)):
            if any(not isinstance(name, str) or not name.strip()
                   for name in names):
                raise ValueError(f"{label} must contain tool names")

        self._registry = registry
        self._polling_tools = frozenset(polling_tools)
        self._exempt_tools = frozenset(exempt_tools)
        self._threshold = threshold
        self._window: deque[tuple[str, str, str]] = deque(maxlen=window_size)
        self._poll_streak_tool = None
        self._poll_streak = 0

    @property
    def window(self) -> tuple[tuple[str, str, str], ...]:
        return tuple(self._window)

    def reset(self) -> None:
        self._window.clear()
        self._clear_poll_streak()

    def _clear_poll_streak(self) -> None:
        self._poll_streak_tool = None
        self._poll_streak = 0

    def _is_polling(self, tool_name: str) -> bool:
        if tool_name in self._polling_tools:
            return True
        if self._registry is None:
            return False
        spec = self._registry.get(tool_name)
        return bool(spec is not None and spec.is_polling)

    @staticmethod
    def _hash(value: Any) -> str:
        if isinstance(value, bytes):
            payload = value
        else:
            try:
                payload = json.dumps(
                    value, sort_keys=True, ensure_ascii=False,
                    default=repr).encode("utf-8")
            except (TypeError, ValueError, RecursionError):
                payload = repr(value).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:16]

    def _forms_ping_pong(self, tool_name: str, input_hash: str) -> bool:
        pairs = [
            (row[0], row[1]) for row in list(self._window)[-5:]
        ]
        pairs.append((tool_name, input_hash))
        return (
            len(pairs) == 6
            and pairs[0] == pairs[2] == pairs[4]
            and pairs[1] == pairs[3] == pairs[5]
            and pairs[0] != pairs[1]
        )

    def check(
        self, tool_name: str, input_data: Any, outcome_data: Any = None,
    ) -> GuardVerdict:
        """Return PASS, WARN, or BLOCK for the next call.

        The loop calls this before execution with no outcome, so the verdict
        comes from completed actions. Outcomes are deliberately ignored for
        block detection: noisy output must not hide the same repeated call.
        """
        if tool_name in self._exempt_tools:
            return GuardVerdict.PASS
        if self._is_polling(tool_name):
            if (tool_name == self._poll_streak_tool
                    and self._poll_streak >= self.MAX_CONSECUTIVE_POLLS):
                return GuardVerdict.BLOCK
            return GuardVerdict.PASS

        input_hash = self._hash(input_data)
        if self._forms_ping_pong(tool_name, input_hash):
            return GuardVerdict.BLOCK

        repeats = sum(
            1 for row in self._window
            if row[0] == tool_name and row[1] == input_hash
        )
        if repeats >= self._threshold:
            return GuardVerdict.BLOCK
        return GuardVerdict.PASS

    def record(self, tool_name: str, input_data: Any, outcome_data: Any) -> None:
        if tool_name in self._exempt_tools:
            self._clear_poll_streak()
            return
        if self._is_polling(tool_name):
            if tool_name == self._poll_streak_tool:
                self._poll_streak += 1
            else:
                self._poll_streak_tool = tool_name
                self._poll_streak = 1
            return
        self._clear_poll_streak()
        self._window.append((
            tool_name,
            self._hash(input_data),
            self._hash(outcome_data),
        ))
