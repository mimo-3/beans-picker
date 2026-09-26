from __future__ import annotations

import copy
from dataclasses import replace

from cua_jev._json import quote
from cua_jev._text import trim
from cua_jev.candidates.build import BuildOptions, build_candidates
from cua_jev.candidates.types import ActionCandidate
from cua_jev.observe.types import Snapshot, TextNode
from cua_jev.verify.effect import expected_text, verify_effect
from tests.helpers import snap_fixture


class TestExpectedText:
    def test_set_value_is_exact_equality_no_trimming_no_containment(self) -> None:
        ok = expected_text("set_value", "old", "hello ")
        assert ok is not None
        assert ok("hello ") is True
        assert ok("hello") is False
        assert ok("hello world") is False

    def test_append_keeps_everything_before_it_and_adds_exactly_the_text(self) -> None:
        ok = expected_text("append", "line one\n", "two ")
        assert ok is not None
        assert ok("line one\ntwo ") is True
        assert ok("line one\ntwo") is False
        assert ok("line onetwo ") is False

    def test_type_into_inserts_the_whole_text_at_one_position_and_changes_nothing_else(self) -> None:
        ok = expected_text("type_into", "ac", "b")
        assert ok is not None
        assert ok("abc") is True
        assert ok("bac") is True
        assert ok("acb") is True
        assert ok("ab") is False
        assert ok("abcb") is False


def with_area(s: Snapshot, value: str, exact: bool = True) -> Snapshot:
    c = copy.deepcopy(s)
    c.nodes = [
        replace(n, raw_value=value, value=trim(value), exact=exact) if n.role == "AXTextArea" else n for n in c.nodes
    ]
    c.signature = f"{s.signature}:{quote(value)}:{'true' if exact else 'false'}"
    return c


def te_cand(kind: str, text: str) -> ActionCandidate:
    te = snap_fixture("textedit")
    return next(c for c in build_candidates(te, BuildOptions(text=text)) if c.kind == kind)


class TestVerifyEffectOnText:
    def test_confirms_an_append_only_when_the_field_reads_exactly_the_old_text_plus_the_new(self) -> None:
        te = snap_fixture("textedit")
        before = with_area(te, "alpha")
        assert verify_effect(te_cand("append", " beta "), before, with_area(te, "alpha beta ")).effect == "ok"
        assert verify_effect(te_cand("append", " beta "), before, with_area(te, "alpha beta")).effect == "wrong"

    def test_does_not_confirm_text_it_could_not_read_exactly(self) -> None:
        te = snap_fixture("textedit")
        v = verify_effect(te_cand("set_value", "x"), with_area(te, "alpha", False), with_area(te, "x", False))
        assert (v.effect, v.exact) == ("unverified", False)
        same = verify_effect(te_cand("set_value", "x"), with_area(te, "alpha", False), with_area(te, "alpha", False))
        assert same.effect == "none"

    def test_judges_only_the_targeted_field_text_that_landed_elsewhere_is_not_success(self) -> None:
        te = snap_fixture("textedit")
        before = with_area(te, "alpha")
        after = copy.deepcopy(before)
        after.texts = [*after.texts, TextNode(role="AXStaticText", value="beta", raw="beta", depth=3)]
        after.signature = "changed"
        assert verify_effect(te_cand("append", "beta"), before, after).effect == "wrong"

    def test_reports_an_unchanged_window_as_no_effect(self) -> None:
        te = snap_fixture("textedit")
        before = with_area(te, "alpha")
        assert verify_effect(te_cand("append", "beta"), before, before).effect == "none"


class TestVerifyEffectOnClicks:
    def test_needs_a_change_in_the_window_not_the_drivers_word(self) -> None:
        calc = snap_fixture("calculator")
        seven = next(c for c in build_candidates(calc) if c.target is not None and c.target.identifier == "Seven")
        assert verify_effect(seven, calc, calc).effect == "none"
        after = copy.deepcopy(calc)
        after.texts = [TextNode(role="AXStaticText", value="7", raw="\u200e7", depth=5)]
        after.signature = "changed"
        assert verify_effect(seven, calc, after).effect == "ok"
