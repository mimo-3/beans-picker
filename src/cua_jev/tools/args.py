"""Tool input schemas (served verbatim in `tools/list`) and the validator that checks calls against them.

Issues are reported in schema property order, nested issues at their parent's position, with the
wording MCP clients of this server already see.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, NotRequired, TypedDict

from cua_jev._json import JsonObject, JsonValue

type Modifier = Literal["shift", "cmd", "option", "ctrl"]

MODIFIERS: Final[tuple[Modifier, ...]] = ("shift", "cmd", "option", "ctrl")
MAX_STEPS: Final = 12
MAX_SAFE_INTEGER: Final = 9007199254740991
"""The largest integer a JSON number carries exactly (2**53 - 1)."""

_DRAFT_07: Final = "http://json-schema.org/draft-07/schema#"


def _integer(description: str) -> JsonObject:
    return {"description": description, "type": "integer", "minimum": -MAX_SAFE_INTEGER, "maximum": MAX_SAFE_INTEGER}


def _target() -> JsonObject:
    return {
        "app": {
            "description": 'App name ("Calculator") or bundle id. Launched in the background if not running.',
            "type": "string",
        },
        "pid": _integer("Process id, instead of app."),
        "windowId": _integer("Window id. Default: the app's front-most titled window."),
    }


def _step() -> JsonObject:
    return {
        "instruction": {
            "type": "string",
            "description": "What this one action should do, e.g. 'press the 7 key' or 'type into the search field'.",
        },
        "text": {
            "description": "Exact text to enter (fields, pop-ups, on-screen keypads). Never trimmed.",
            "type": "string",
        },
        "candidateId": {"description": "Run this candidate instead of asking Jev.", "type": "string"},
        "allowDestructive": {
            "description": "Permit an action that may not be undoable (delete, close, send \u2026).",
            "type": "boolean",
        },
        "modifiers": {
            "description": "Keys held during a click or toggle: shift-click extends a selection, cmd-click adds "
            "one item to it. The control must be visible on the window.",
            "type": "array",
            "items": {"type": "string", "enum": list[JsonValue](MODIFIERS)},
        },
    }


def _object(properties: JsonObject, required: Sequence[str] = ()) -> JsonObject:
    schema: JsonObject = {"$schema": _DRAFT_07, "type": "object", "properties": properties}
    if required:
        schema["required"] = list[JsonValue](required)
    return schema


INPUT_SCHEMAS: Final[Mapping[str, JsonObject]] = MappingProxyType(
    {
        "observe": _object(
            {
                **_target(),
                "instruction": {"type": "string"},
                "limit": {
                    "description": "Candidates to return (default 80, or 10 with an instruction).",
                    "type": "integer",
                    "exclusiveMinimum": 0,
                    "maximum": MAX_SAFE_INTEGER,
                },
            }
        ),
        "act": _object(
            {
                **_target(),
                **_step(),
                "then": {
                    "description": "More steps on the same window, run in order after this one. The run stops at the "
                    "first step that is not done or unverified; each step's result is in `steps`.",
                    "maxItems": MAX_STEPS,
                    "type": "array",
                    "items": {"type": "object", "properties": _step(), "required": ["instruction"]},
                },
            },
            ["instruction"],
        ),
        "extract": _object({**_target(), "instruction": {"type": "string"}}, ["instruction"]),
    }
)


class TargetArgs(TypedDict, total=False):
    app: str
    pid: int
    windowId: int


class Step(TypedDict):
    instruction: str
    text: NotRequired[str]
    candidateId: NotRequired[str]
    allowDestructive: NotRequired[bool]
    modifiers: NotRequired[list[Modifier]]


class ObserveArgs(TargetArgs, total=False):
    instruction: str
    limit: int


class ActArgs(TargetArgs, Step, total=False):
    then: list[Step]


class ExtractArgs(TargetArgs):
    instruction: str


@dataclass(frozen=True, slots=True)
class Issue:
    """One validation failure; `path` is dotted with `[i]` for list items, empty for the whole input."""

    message: str
    path: str


def _received(v: JsonValue | None) -> str:
    """The received-type word of a validation message."""
    match v:
        case None:
            return "null"
        case bool():
            return "boolean"
        case int() | float():
            return "number"
        case str():
            return "string"
        case list():
            return "array"
        case _:
            return "object"


_EXPECTED: Final[Mapping[str, str]] = MappingProxyType(
    {"string": "string", "boolean": "boolean", "integer": "number", "array": "array", "object": "object"}
)


def _at(path: str, key: str | int) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    return f"{path}.{key}" if path else key


def _invalid_type(schema: JsonObject, v: JsonValue | None, path: str, *, missing: bool = False) -> Issue:
    expected = _EXPECTED[str(schema["type"])]
    return Issue(f"Invalid input: expected {expected}, received {'undefined' if missing else _received(v)}", path)


def _integer_value(schema: JsonObject, v: JsonValue, path: str, issues: list[Issue]) -> JsonValue:
    if isinstance(v, bool) or not isinstance(v, int | float):
        issues.append(_invalid_type(schema, v, path))
        return v
    if isinstance(v, float) and not math.isfinite(v):
        shown = "NaN" if math.isnan(v) else ("Infinity" if v > 0 else "-Infinity")
        issues.append(Issue(f"Invalid input: expected number, received {shown}", path))
        return v
    if isinstance(v, float) and not v.is_integer():
        issues.append(Issue("Invalid input: expected int, received number", path))
        return v
    n = int(v)
    if n > MAX_SAFE_INTEGER:
        issues.append(Issue(f"Too big: expected int to be <={MAX_SAFE_INTEGER}", path))
    elif n < -MAX_SAFE_INTEGER:
        issues.append(Issue(f"Too small: expected int to be >={-MAX_SAFE_INTEGER}", path))
    low = schema.get("exclusiveMinimum")
    if isinstance(low, int) and n <= low:
        issues.append(Issue(f"Too small: expected number to be >{low}", path))
    return n


def _schema_object(part: JsonValue) -> JsonObject:
    """A part of one of the schemas above that is an object by construction."""
    if not isinstance(part, dict):
        raise TypeError(f"malformed input schema: expected an object, got {type(part).__name__}")
    return part


def _schema_list(part: JsonValue) -> list[JsonValue]:
    """A part of one of the schemas above that is a list by construction."""
    if not isinstance(part, list):
        raise TypeError(f"malformed input schema: expected a list, got {type(part).__name__}")
    return part


def _array_value(schema: JsonObject, v: JsonValue, path: str, issues: list[Issue]) -> JsonValue:
    if not isinstance(v, list):
        issues.append(_invalid_type(schema, v, path))
        return v
    items = _schema_object(schema["items"])
    out = [_value(items, x, _at(path, i), issues) for i, x in enumerate(v)]
    most = schema.get("maxItems")
    if isinstance(most, int) and len(v) > most:
        issues.append(Issue(f"Too big: expected array to have <={most} items", path))
    return out


def _enum_value(schema: JsonObject, v: JsonValue, path: str, issues: list[Issue]) -> JsonValue:
    options = _schema_list(schema["enum"])
    if not isinstance(v, str) or v not in options:
        shown = "|".join(f'"{o}"' for o in options)
        issues.append(Issue(f"Invalid option: expected one of {shown}", path))
    return v


def _object_value(schema: JsonObject, v: JsonValue | None, path: str, issues: list[Issue]) -> JsonObject:
    if not isinstance(v, dict):
        issues.append(_invalid_type(schema, v, path))
        return {}
    properties = _schema_object(schema["properties"])
    required = _schema_list(schema.get("required", []))
    out: JsonObject = {}
    for key, raw in properties.items():
        prop = _schema_object(raw)
        if key in v:
            out[key] = _value(prop, v[key], _at(path, key), issues)
        elif key in required:
            issues.append(_invalid_type(prop, None, _at(path, key), missing=True))
    return out


def _value(schema: JsonObject, v: JsonValue, path: str, issues: list[Issue]) -> JsonValue:
    if "enum" in schema:
        return _enum_value(schema, v, path, issues)
    match schema["type"]:
        case "integer":
            return _integer_value(schema, v, path, issues)
        case "array":
            return _array_value(schema, v, path, issues)
        case "object":
            return _object_value(schema, v, path, issues)
        case "string":
            ok = isinstance(v, str)
        case _:
            ok = isinstance(v, bool)
    if not ok:
        issues.append(_invalid_type(schema, v, path))
    return v


def check_arguments(tool: str, arguments: Mapping[str, JsonValue] | None) -> tuple[JsonObject, list[Issue]]:
    """The arguments with unknown keys dropped and integral numbers as `int`, and every issue found."""
    schema = INPUT_SCHEMAS[tool]
    issues: list[Issue] = []
    if arguments is None:
        return {}, [Issue("Invalid input: expected object, received undefined", "")]
    return _object_value(schema, dict(arguments), "", issues), issues


def validation_text(tool: str, issues: Sequence[Issue]) -> str:
    """The error text an invalid call gets back."""
    lines = "\n".join(f"{i.message} at {i.path}" if i.path else i.message for i in issues)
    return f"MCP error -32602: Input validation error: Invalid arguments for tool {tool}: {lines}"


# The builders below turn checked arguments into their typed form; a value of the wrong type
# cannot reach them, so they only narrow.


def _str(a: JsonObject, key: str) -> str | None:
    v = a.get(key)
    return v if isinstance(v, str) else None


def _int(a: JsonObject, key: str) -> int | None:
    v = a.get(key)
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _put_target(a: JsonObject, out: TargetArgs) -> None:
    if (app := _str(a, "app")) is not None:
        out["app"] = app
    if (pid := _int(a, "pid")) is not None:
        out["pid"] = pid
    if (window_id := _int(a, "windowId")) is not None:
        out["windowId"] = window_id


def _modifier(v: JsonValue) -> Modifier | None:
    return next((m for m in MODIFIERS if v == m), None)


def _put_step(a: JsonObject, out: Step) -> None:
    if (text := _str(a, "text")) is not None:
        out["text"] = text
    if (cid := _str(a, "candidateId")) is not None:
        out["candidateId"] = cid
    allow = a.get("allowDestructive")
    if isinstance(allow, bool):
        out["allowDestructive"] = allow
    mods = a.get("modifiers")
    if isinstance(mods, list):
        out["modifiers"] = [m for m in map(_modifier, mods) if m is not None]


def _step_args(a: JsonObject) -> Step:
    step: Step = {"instruction": _str(a, "instruction") or ""}
    _put_step(a, step)
    return step


def observe_args(a: JsonObject) -> ObserveArgs:
    out: ObserveArgs = {}
    _put_target(a, out)
    if (instruction := _str(a, "instruction")) is not None:
        out["instruction"] = instruction
    if (limit := _int(a, "limit")) is not None:
        out["limit"] = limit
    return out


def act_args(a: JsonObject) -> ActArgs:
    out: ActArgs = {"instruction": _str(a, "instruction") or ""}
    _put_target(a, out)
    _put_step(a, out)
    then = a.get("then")
    if isinstance(then, list):
        out["then"] = [_step_args(s) for s in then if isinstance(s, dict)]
    return out


def extract_args(a: JsonObject) -> ExtractArgs:
    out: ExtractArgs = {"instruction": _str(a, "instruction") or ""}
    _put_target(a, out)
    return out
