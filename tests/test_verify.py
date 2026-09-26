"""verify.effect rules on hand-built windows: toggles, menu retitles, text exactness, messages,
and UTF-16 positions."""

from __future__ import annotations

import pytest

from cua_jev.candidates.types import ActionKind
from cua_jev.jev.state import Change
from cua_jev.verify.effect import EffectVerdict, clip, expected_text, summarize, verify_effect
from tests.act_support import cand, menu_item, node, snap, with_signature

ASTRAL = "\U0001f600"  # two UTF-16 units


class TestExpectedTextPositions:
    def test_type_into_counts_positions_in_utf16_units(self) -> None:
        ok = expected_text("type_into", "a" + ASTRAL, "b")
        assert ok is not None
        assert ok("ba" + ASTRAL)
        assert ok("ab" + ASTRAL)
        assert ok("a" + ASTRAL + "b")
        # Inside the pair is a position too, as the accessibility API counts it.
        assert ok("a\ud83db\ude00")
        assert not ok("a" + ASTRAL)
        assert not ok("a" + ASTRAL + "bb")

    def test_type_into_with_empty_values(self) -> None:
        ok = expected_text("type_into", "", "x")
        assert ok is not None
        assert ok("x")
        assert not ok("")
        keep = expected_text("type_into", "abc", "")
        assert keep is not None
        assert keep("abc")
        assert not keep("acb")

    @pytest.mark.parametrize("kind", ["click", "toggle", "menu", "keypad", "key", "scroll", "context_menu"])
    def test_kinds_that_enter_no_text_have_no_expectation(self, kind: ActionKind) -> None:
        assert expected_text(kind, "a", "b") is None

    def test_choose_option_expects_the_title_exactly(self) -> None:
        ok = expected_text("choose_option", "Small", "Large")
        assert ok is not None
        assert ok("Large")
        assert not ok("Large ")


class TestClip:
    def test_keeps_120_units_and_cuts_longer_text_with_an_ellipsis(self) -> None:
        assert clip("a" * 120) == "a" * 120
        assert clip("a" * 121) == "a" * 119 + "\u2026"

    def test_counts_utf16_units_and_may_cut_a_pair(self) -> None:
        assert clip("a" * 59 + ASTRAL * 31) == "a" * 59 + ASTRAL * 30 + "\u2026"
        assert clip("a" * 118 + ASTRAL + "b") == "a" * 118 + "\ud83d\u2026"


class TestSummarize:
    def test_lists_changes_in_a_fixed_order(self) -> None:
        d: Change = {
            "appeared": ["Button A", "Button B", "Button C", "Button D", "Button E", "Button F"],
            "disappeared": ["Button Z"],
            "fields_changed": ['TextField "Name": "a" \u2192 "b"'],
            "texts_changed": ["one", "two"],
            "window_title": "Doc 2",
            "menu_items_retitled": ['Format > Font: "Show Fonts" is now "Hide Fonts"'],
        }
        assert summarize(d) == (
            'window title now "Doc 2"; TextField "Name": "a" \u2192 "b"; new text: one | two; '
            'Format > Font: "Show Fonts" is now "Hide Fonts"; '
            "appeared: Button A, Button B, Button C, Button D, Button E; disappeared: Button Z"
        )

    def test_an_empty_title_is_a_change_and_no_change_has_a_default_line(self) -> None:
        assert summarize({"appeared": [], "disappeared": [], "window_title": ""}) == 'window title now ""'
        assert summarize({"appeared": [], "disappeared": []}) == "the window's state changed"
        assert summarize({"appeared": [], "disappeared": [], "texts_changed": []}) == "the window's state changed"

    def test_is_at_most_400_utf16_units(self) -> None:
        line = summarize({"appeared": [], "disappeared": [], "texts_changed": [ASTRAL * 300]})
        assert line == "new text: " + ASTRAL * 195
        cut = summarize({"appeared": [], "disappeared": [], "texts_changed": ["x" + ASTRAL * 300]})
        assert cut.endswith("\ud83d")


TOGGLE_OFF = node(1, "AXCheckBox", "Wrap", value="0")


class TestToggles:
    def test_a_flipped_value_is_ok(self) -> None:
        after = snap([node(1, "AXCheckBox", "Wrap", value="1")])
        got = verify_effect(cand("toggle", TOGGLE_OFF), snap([TOGGLE_OFF]), after)
        assert got == EffectVerdict(effect="ok", detail="value 0 \u2192 1")

    def test_an_absent_value_reads_undefined(self) -> None:
        before = node(1, "AXCheckBox", "Wrap")
        after = snap([node(1, "AXCheckBox", "Wrap", value="1")])
        got = verify_effect(cand("toggle", before), snap([before]), after)
        assert got == EffectVerdict(effect="ok", detail="value undefined \u2192 1")
        gone = verify_effect(cand("toggle", TOGGLE_OFF), snap([TOGGLE_OFF]), snap([node(1, "AXCheckBox", "Wrap")]))
        assert gone.detail == "value 0 \u2192 undefined"

    def test_a_changed_screen_without_the_flip_is_wrong(self) -> None:
        before = snap([TOGGLE_OFF])
        got = verify_effect(cand("toggle", TOGGLE_OFF), before, with_signature(before, "other"))
        assert got == EffectVerdict(effect="wrong", detail="the screen changed but the control's state did not flip")
        assert verify_effect(cand("toggle", TOGGLE_OFF), before, before) == EffectVerdict(
            effect="none", detail="unchanged"
        )


class TestMenus:
    def test_a_retitled_item_is_the_effect_of_its_command(self) -> None:
        show = menu_item("Format", "Font", "Show Fonts")
        before = snap(menu=[menu_item("Format"), menu_item("Format", "Font"), show])
        after = snap(menu=[menu_item("Format"), menu_item("Format", "Font"), menu_item("Format", "Font", "Hide Fonts")])
        got = verify_effect(cand("menu", menu=show), before, after)
        assert got == EffectVerdict(effect="ok", detail='menu item now reads "Hide Fonts"')

    def test_without_a_retitle_the_window_decides(self) -> None:
        copy_item = menu_item("Edit", "Copy")
        before = snap(menu=[copy_item])
        assert verify_effect(cand("menu", menu=copy_item), before, before).effect == "none"
        assert verify_effect(cand("menu", menu=copy_item), before, with_signature(before, "x")).effect == "ok"


FIELD = node(1, "AXTextField", "Name", key="name", raw_value="old", value="old", exact=True)


class TestText:
    def test_an_exact_match_names_the_role_and_the_text(self) -> None:
        before = snap([FIELD])
        after = snap([node(1, "AXTextField", "Name", key="name", raw_value="x ", value="x", exact=True)])
        got = verify_effect(cand("set_value", FIELD, text="x "), before, after)
        assert got == EffectVerdict(effect="ok", detail='TextField reads exactly "x "', exact=True)

    def test_an_exact_mismatch_quotes_what_the_field_reads(self) -> None:
        before = snap([FIELD])
        after = snap([node(1, "AXTextField", "Name", key="name", raw_value='say "hi"\n', exact=True)])
        got = verify_effect(cand("set_value", FIELD, text="x"), before, after)
        assert got == EffectVerdict(
            effect="wrong", detail='the field reads "say \\"hi\\"\\n", not what was asked', exact=True
        )

    def test_an_exact_unchanged_field(self) -> None:
        before = snap([FIELD])
        moved = verify_effect(cand("set_value", FIELD, text="x"), before, with_signature(before, "other"))
        assert moved == EffectVerdict(effect="wrong", detail="the screen changed but the field did not", exact=True)
        still = verify_effect(cand("set_value", FIELD, text="x"), before, before)
        assert still == EffectVerdict(effect="none", detail="unchanged", exact=True)

    def test_a_change_that_could_not_be_read_exactly_is_unverified(self) -> None:
        loose = node(1, "AXTextField", "Name", key="name", value="old")
        after = snap([node(1, "AXTextField", "Name", key="name", value="x")])
        got = verify_effect(cand("set_value", loose, text="x "), snap([loose]), after)
        assert got == EffectVerdict(
            effect="unverified",
            detail='the field changed; cua-driver reads "x" (whitespace at the ends trimmed), '
            "and the exact text could not be read",
            exact=False,
        )

    def test_a_field_that_is_gone(self) -> None:
        before = snap([FIELD])
        moved = verify_effect(cand("type_into", FIELD, text="x"), before, snap([], signature="other"))
        assert moved == EffectVerdict(effect="wrong", detail="the target field is gone")
        still = verify_effect(cand("type_into", FIELD, text="x"), before, snap([]))
        assert still.effect == "none"
        untargeted = verify_effect(cand("type_into", text="x"), before, before)
        assert untargeted == EffectVerdict(effect="none", detail="the target field is gone")

    def test_a_field_that_appeared_is_judged_from_empty(self) -> None:
        after = snap([node(1, "AXTextField", "Name", key="name", raw_value="x", exact=True)])
        got = verify_effect(cand("set_value", FIELD, text="x"), snap([]), after)
        assert got == EffectVerdict(effect="ok", detail='TextField reads exactly "x"', exact=True)

    def test_set_value_needs_only_the_new_text_read_exactly(self) -> None:
        loose = node(1, "AXTextField", "Name", key="name", value="old")
        after = snap([node(1, "AXTextField", "Name", key="name", raw_value="x", exact=True)])
        assert verify_effect(cand("set_value", loose, text="x"), snap([loose]), after).exact is True
        assert verify_effect(cand("type_into", loose, text="x"), snap([loose]), after).exact is False
        assert verify_effect(cand("append", loose, text="x"), snap([loose]), after).exact is False

    def test_the_value_read_is_the_raw_value_even_when_empty(self) -> None:
        before = snap([node(1, "AXTextField", "Name", key="name", raw_value="", value="placeholder", exact=True)])
        after = snap([node(1, "AXTextField", "Name", key="name", raw_value="x", value="x", exact=True)])
        got = verify_effect(cand("append", before.nodes[0], text="x"), before, after)
        assert got.effect == "ok"

    def test_a_long_reading_is_clipped_in_the_message(self) -> None:
        before = snap([FIELD])
        after = snap([node(1, "AXTextField", "Name", key="name", raw_value="y" * 130, exact=True)])
        got = verify_effect(cand("set_value", FIELD, text="x"), before, after)
        assert got.detail == f'the field reads "{"y" * 119}\u2026", not what was asked'


POP = node(1, "AXPopUpButton", "Size", key="pop", value="Small")


class TestPopUps:
    def test_a_title_without_outer_whitespace_is_exact(self) -> None:
        after = snap([node(1, "AXPopUpButton", "Size", key="pop", value="Large")])
        got = verify_effect(cand("choose_option", POP, text="Large"), snap([POP]), after)
        assert got == EffectVerdict(effect="ok", detail='PopUpButton reads exactly "Large"', exact=True)

    def test_a_title_with_whitespace_cannot_be_confirmed(self) -> None:
        already = snap([node(1, "AXPopUpButton", "Size", key="pop", value="Large")])
        got = verify_effect(cand("choose_option", already.nodes[0], text=" Large"), already, already)
        assert (got.effect, got.exact) == ("unverified", False)
        other = verify_effect(cand("choose_option", POP, text=" Huge"), snap([POP]), snap([POP]))
        assert other == EffectVerdict(effect="none", detail="unchanged", exact=False)
