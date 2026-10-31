"""Structured tool specs with basic JSON-schema input validation.

The registry is a migration bridge: tools registered here are validated
before execution, while every other tool still uses agent.py's legacy
dispatcher until it is migrated.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Callable

EFFECT_CLASSES = frozenset({"read_only", "write", "system_control"})

MAX_BATCH_ACTIONS = 3
BATCH_EFFECT_ERROR = (
    "ERROR: Cannot batch write/system_control tools. "
    "Execute them sequentially.")
BATCH_SIZE_ERROR = (
    f"ERROR: Cannot batch more than {MAX_BATCH_ACTIONS} actions. "
    "Execute at most three read-only tools per batch.")

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class InputValidationError(ValueError):
    """Raised when a tool input does not match its ToolSpec schema."""


def _type_label(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return (isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value))
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "null":
        return value is None
    raise InputValidationError(f"unknown schema type '{expected}'")


def _validate_node(schema: dict, value: Any, path: str) -> None:
    if not isinstance(schema, dict):
        raise InputValidationError("input schema must be an object")

    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try:
                _validate_node(option, value, path)
                return
            except InputValidationError:
                continue
        raise InputValidationError(f"{path} does not match any allowed shape")

    if "enum" in schema and value not in schema["enum"]:
        choices = ", ".join(repr(choice) for choice in schema["enum"])
        raise InputValidationError(f"{path} must be one of: {choices}")

    expected = schema.get("type")
    if expected is not None and not _matches_type(value, expected):
        raise InputValidationError(
            f"{path} must be {expected} (got {_type_label(value)})")

    if expected == "string":
        length = len(value)
        min_length = schema.get("minLength")
        if min_length is not None and length < min_length:
            raise InputValidationError(
                f"{path} must be at least {min_length} character(s)")
        max_length = schema.get("maxLength")
        if max_length is not None and length > max_length:
            raise InputValidationError(
                f"{path} must be at most {max_length} character(s)")
        pattern = schema.get("pattern")
        if pattern and re.search(pattern, value) is None:
            raise InputValidationError(
                f"{path} does not match pattern {pattern!r}")
    elif expected == "integer":
        if "minimum" in schema and value < schema["minimum"]:
            raise InputValidationError(
                f"{path} must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise InputValidationError(
                f"{path} must be <= {schema['maximum']}")
    elif expected == "number":
        if "minimum" in schema and value < schema["minimum"]:
            raise InputValidationError(
                f"{path} must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise InputValidationError(
                f"{path} must be <= {schema['maximum']}")
    elif expected == "object":
        for key in schema.get("required", ()):
            if key not in value:
                raise InputValidationError(
                    f"{path} is missing required property '{key}'")
        properties = schema.get("properties", {})
        for key, sub_schema in properties.items():
            if key in value:
                _validate_node(sub_schema, value[key], f"{path}.{key}")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                raise InputValidationError(
                    f"{path} has unexpected properties: "
                    f"{', '.join(repr(name) for name in extra)}")
    elif expected == "array":
        min_items = schema.get("minItems")
        if min_items is not None and len(value) < min_items:
            raise InputValidationError(
                f"{path} must contain at least {min_items} item(s)")
        max_items = schema.get("maxItems")
        if max_items is not None and len(value) > max_items:
            raise InputValidationError(
                f"{path} must contain at most {max_items} item(s)")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                _validate_node(item_schema, item, f"{path}[{index}]")


def validate_input(schema: dict, value: Any) -> None:
    """Raise InputValidationError when value does not match schema.

    Supports the subset used by ToolSpec: type, enum, anyOf, string
    length/pattern, numeric bounds, object properties/required, and array
    item/count constraints.
    """
    _validate_node(schema, value, "input")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    effect_class: str
    timeout: int
    func: Callable[..., Any]
    is_polling: bool = False

    def __post_init__(self):
        if not _NAME_RE.fullmatch(self.name or ""):
            raise ValueError(
                f"tool name must be snake_case, got {self.name!r}")
        if not str(self.description or "").strip():
            raise ValueError(f"tool '{self.name}' needs a description")
        if not isinstance(self.input_schema, dict):
            raise TypeError(f"tool '{self.name}' input_schema must be a dict")
        if self.effect_class not in EFFECT_CLASSES:
            raise ValueError(
                f"tool '{self.name}' effect_class must be one of "
                f"{sorted(EFFECT_CLASSES)}")
        if (isinstance(self.timeout, bool)
                or not isinstance(self.timeout, int)
                or self.timeout <= 0):
            raise ValueError(
                f"tool '{self.name}' timeout must be a positive integer")
        if not isinstance(self.is_polling, bool):
            raise TypeError(
                f"tool '{self.name}' is_polling must be a boolean")
        if not callable(self.func):
            raise TypeError(f"tool '{self.name}' func must be callable")

    def validate(self, value: Any) -> None:
        validate_input(self.input_schema, value)

    def catalog_description(self) -> str:
        """Schema-bearing description shared by the prompt and catalog."""
        schema = json.dumps(self.input_schema, sort_keys=True)
        return (f"[{self.effect_class}, {self.timeout}s] "
                f"{self.description} input: {schema}")


class ToolRegistry:
    """Name-keyed ToolSpec collection with prompt-catalog generation."""

    def __init__(self):
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        if not isinstance(spec, ToolSpec):
            raise TypeError("registry only accepts ToolSpec instances")
        if spec.name in self._specs:
            raise ValueError(f"tool '{spec.name}' is already registered")
        self._specs[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def describe(self, name: str) -> str | None:
        spec = self.get(name)
        return spec.catalog_description() if spec is not None else None

    def __contains__(self, name: str) -> bool:
        return name in self._specs

    def __len__(self) -> int:
        return len(self._specs)

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs[name] for name in sorted(self._specs))

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.specs)

    def catalog_text(self) -> str:
        """Prompt-ready catalog, one bullet per migrated tool."""
        lines = []
        for spec in self.specs:
            lines.append(f"- {spec.name} {spec.catalog_description()}")
        return "\n".join(lines)


def batch_rejection_error(names, registry: ToolRegistry) -> str | None:
    """Return the rejection for a batch, or None when it is safe to run concurrently.

    Unregistered tools fail closed: without a spec the effect class cannot be
    verified, and a legacy write tool must never join a parallel batch.
    """
    names = list(names)
    if len(names) > MAX_BATCH_ACTIONS:
        return BATCH_SIZE_ERROR
    for name in names:
        spec = (
            registry.get(name)
            if registry is not None and isinstance(name, str)
            else None
        )
        if spec is None or spec.effect_class != "read_only":
            return BATCH_EFFECT_ERROR
    return None
