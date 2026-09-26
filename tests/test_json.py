from __future__ import annotations

import json
import math

import pytest

from cua_jev._json import dumps, quote


def test_compact_with_insertion_order() -> None:
    assert dumps({"b": 1, "a": [1, 2.5, None, True, False], "c": {}}) == '{"b":1,"a":[1,2.5,null,true,false],"c":{}}'


def test_integral_floats_have_no_fraction() -> None:
    assert dumps([1.0, -0.0, 1e21, 1e-7, 100.0]) == "[1,0,1e+21,1e-7,100]"


def test_non_finite_numbers_are_null() -> None:
    assert dumps([math.nan, math.inf, -math.inf, 10**400]) == "[null,null,null,null]"


def test_tuples_are_arrays() -> None:
    assert dumps((1, "a")) == '[1,"a"]'


def test_escapes() -> None:
    assert quote('a"b\\c') == '"a\\"b\\\\c"'
    assert quote("\b\f\n\r\t") == '"\\b\\f\\n\\r\\t"'
    assert quote("\x00\x1f\x7f") == '"\\u0000\\u001f\x7f"'
    assert quote("\u00e9\u2028/") == '"\u00e9\u2028/"'


def test_lone_surrogates_are_escaped_and_output_encodes() -> None:
    out = dumps({"k": "\ud800x\udfff"})
    assert out == '{"k":"\\ud800x\\udfff"}'
    out.encode("utf-8")


def test_pair_held_as_two_code_points_is_one_character() -> None:
    assert quote("\ud83d\ude00") == '"\U0001f600"'
    assert quote("\U0001f600") == '"\U0001f600"'
    assert quote("\ud83d") == '"\\ud83d"'


def test_round_trips_through_a_json_parser() -> None:
    value = {"s": 'a\x01"\\\n\u00e9\U0001f600', "n": [0, -1, 0.5, 123456789012], "o": {"x": None}}
    assert json.loads(dumps(value)) == value


def test_rejects_other_types() -> None:
    with pytest.raises(TypeError):
        dumps({"a": {1, 2}})
    with pytest.raises(TypeError):
        dumps({1: "a"})
