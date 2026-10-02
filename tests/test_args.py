from __future__ import annotations

import pytest

from beans_picker._json import JsonObject, JsonValue
from beans_picker.tools.args import (
    INPUT_SCHEMAS,
    Issue,
    act_args,
    check_arguments,
    extract_args,
    observe_args,
    validation_text,
)


def issues(tool: str, arguments: JsonObject | None) -> list[str]:
    return [f"{i.message} at {i.path}" if i.path else i.message for i in check_arguments(tool, arguments)[1]]


def test_valid_arguments_pass_with_unknown_keys_dropped() -> None:
    checked, found = check_arguments("observe", {"pid": 3, "limit": 5, "allowDestructive": True, "extra": [1]})
    assert found == []
    assert checked == {"pid": 3, "limit": 5}


def test_integral_floats_become_ints() -> None:
    checked, found = check_arguments("observe", {"pid": 5.0, "windowId": 2.0, "limit": 1e3})
    assert found == []
    assert checked == {"pid": 5, "windowId": 2, "limit": 1000}
    assert all(type(v) is int for v in checked.values())


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("app", None, "Invalid input: expected string, received null at app"),
        ("pid", None, "Invalid input: expected number, received null at pid"),
        ("pid", True, "Invalid input: expected number, received boolean at pid"),
        ("windowId", [], "Invalid input: expected number, received array at windowId"),
        ("instruction", 1, "Invalid input: expected string, received number at instruction"),
        ("limit", True, "Invalid input: expected number, received boolean at limit"),
        ("limit", 0.5, "Invalid input: expected int, received number at limit"),
        ("limit", 0, "Too small: expected number to be >0 at limit"),
        ("pid", 0, "Too small: expected int to be >=1 at pid"),
        ("windowId", -1, "Too small: expected int to be >=1 at windowId"),
        ("limit", float("nan"), "Invalid input: expected number, received NaN at limit"),
        ("pid", float("-inf"), "Invalid input: expected number, received -Infinity at pid"),
    ],
)
def test_one_wrong_value_gives_exactly_one_issue_at_its_path(key: str, value: JsonValue, message: str) -> None:
    assert issues("observe", {key: value}) == [message]


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("text", None, "Invalid input: expected string, received null at text"),
        ("candidateId", {}, "Invalid input: expected string, received object at candidateId"),
        ("allowDestructive", "yes", "Invalid input: expected boolean, received string at allowDestructive"),
        ("modifiers", "shift", "Invalid input: expected array, received string at modifiers"),
        ("then", {}, "Invalid input: expected array, received object at then"),
    ],
)
def test_act_step_fields_are_checked_one_by_one(key: str, value: JsonValue, message: str) -> None:
    assert issues("act", {"instruction": "x", key: value}) == [message]


def test_nested_steps_are_checked_in_place_and_the_length_last() -> None:
    step: JsonValue = {"instruction": "c"}
    steps: list[JsonValue] = [{"instruction": "a"}, {"text": "b"}, *([step] * 11)]
    assert issues("act", {"instruction": "x", "then": steps}) == [
        "Invalid input: expected string, received nothing at then[1].instruction",
        "Too big: expected array to have <=12 items at then",
    ]


def test_integers_beyond_the_exact_range_are_refused() -> None:
    assert issues("observe", {"pid": 2**53}) == ["Too big: expected int to be <=9007199254740991 at pid"]
    assert issues("observe", {"limit": -(2**53)}) == [
        "Too small: expected int to be >=-9007199254740991 at limit",
        "Too small: expected number to be >0 at limit",
    ]


def test_no_arguments_at_all() -> None:
    assert issues("extract", None) == ["Invalid input: expected object, received nothing"]
    assert issues("extract", {}) == ["Invalid input: expected string, received nothing at instruction"]
    assert issues("observe", {}) == []


def test_act_needs_an_instruction_even_with_a_candidate_id() -> None:
    missing = issues("act", {"app": "Calculator", "candidateId": "c0000001"})
    assert missing == ["Invalid input: expected string, received nothing at instruction"]
    assert issues("act", {"app": "Calculator", "instruction": "press 7", "candidateId": "c0000001"}) == []


def test_validation_text() -> None:
    text = validation_text("observe", [Issue("A", "pid"), Issue("B", "")])
    assert text == "MCP error -32602: Input validation error: Invalid arguments for tool observe: A at pid\nB"


def test_schemas_include_the_opt_in_driver_after_the_three_tools() -> None:
    assert list(INPUT_SCHEMAS) == ["observe", "act", "extract", "driver"]
    assert INPUT_SCHEMAS["act"]["required"] == ["instruction"]
    assert "required" not in INPUT_SCHEMAS["observe"]


def test_typed_arguments_keep_only_what_was_given() -> None:
    assert observe_args({"app": "Calculator", "limit": 3}) == {"app": "Calculator", "limit": 3}
    assert observe_args({}) == {}
    assert extract_args({"instruction": "total", "windowId": 4}) == {"instruction": "total", "windowId": 4}
    checked, _ = check_arguments(
        "act",
        {
            "pid": 1,
            "instruction": "go",
            "text": "",
            "candidateId": "c1",
            "allowDestructive": False,
            "modifiers": ["shift", "cmd"],
            "then": [{"instruction": "next", "text": "t"}],
        },
    )
    assert act_args(checked) == {
        "instruction": "go",
        "pid": 1,
        "text": "",
        "candidateId": "c1",
        "allowDestructive": False,
        "modifiers": ["shift", "cmd"],
        "then": [{"instruction": "next", "text": "t"}],
    }
