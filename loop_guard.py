"""State-aware loop detection for autonomous tool execution."""
from __future__ import annotations

import hashlib
import json
from collections import Counter, deque
from enum import Enum
from typing import Any, Collection

from tool_registry import ToolRegistry


class GuardVerdict(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    BLOCK = "BLOCK"


class LoopGuard:
    """Track recent (tool, input hash, outcome hash) triples.

    Polling tools are exempt: they are safe to call repeatedly while the
    agent waits for a state change. The same exact triple three times in the
    window blocks the next attempt; the same input with changing outcomes
    only warns.
    """

    def __init__(
        self,
        *,
        registry: ToolRegistry | None = None,
        polling_tools: Collection[str] = (),
        window_size: int = 10,
        threshold: int = 3,
    ):
        if type(window_size) is not int or window_size < 1:
            raise ValueError("window_size must be a positive integer")
        if type(threshold) is not int or not 1 <= threshold <= window_size:
            raise ValueError(
                "threshold must be an integer from 1 through window_size")
        if any(not isinstance(name, str) or not name.strip()
               for name in polling_tools):
            raise ValueError("polling_tools must contain tool names")

        self._registry = registry
        self._polling_tools = frozenset(polling_tools)
        self._threshold = threshold
        self._window: deque[tuple[str, str, str]] = deque(maxlen=window_size)

    @property
    def window(self) -> tuple[tuple[str, str, str], ...]:
        return tuple(self._window)

    def reset(self) -> None:
        self._window.clear()

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

    def check(
        self, tool_name: str, input_data: Any, outcome_data: Any = None,
    ) -> GuardVerdict:
        """Return PASS, WARN, or BLOCK for the next call.

        The loop calls this before execution with no outcome, so the verdict
        comes from completed actions in the window. Passing outcome_data
        evaluates a candidate outcome as if it had completed.
        """
        if self._is_polling(tool_name):
            return GuardVerdict.PASS

        input_hash = self._hash(input_data)
        records = [
            row for row in self._window
            if row[0] == tool_name and row[1] == input_hash
        ]
        if not records:
            return GuardVerdict.PASS

        outcomes = [row[2] for row in records]
        if outcome_data is not None:
            outcomes.append(self._hash(outcome_data))
        if max(Counter(outcomes).values()) >= self._threshold:
            return GuardVerdict.BLOCK
        if len(records) >= self._threshold:
            return GuardVerdict.WARN
        return GuardVerdict.PASS

    def record(self, tool_name: str, input_data: Any, outcome_data: Any) -> None:
        if self._is_polling(tool_name):
            return
        self._window.append((
            tool_name,
            self._hash(input_data),
            self._hash(outcome_data),
        ))
