"""Executor call traces: the ordered cua-driver calls (complete payloads) of every route, and what
must not be called."""

from __future__ import annotations

import pytest

from cua_jev.act.execute import ActResult, Executor, in_popup_menu, is_stale, path_state, to_act
from cua_jev.driver.types import ToolResult
from cua_jev.observe.types import Modal, Snapshot
from tests.act_support import (
    CallDriver,
    FakeMenuKeys,
    Reobserve,
    cand,
    frame,
    geometry_driver,
    menu_item,
    node,
    ok,
    refused,
    snap,
    with_signature,
)

AX_FAILED = refused("ax_action_failed", "AX action failed: kAXErrorCannotComplete")
STALE = refused("stale_element_token", "element token t:1 is stale")
BTN = node(1, "AXButton", "OK")
WIN = snap([BTN])


def executor(driver: CallDriver, *snaps: Snapshot, keys: FakeMenuKeys | None = None) -> tuple[Executor, Reobserve]:
    again = Reobserve(*snaps)
    return Executor(driver, again, menu_keys=keys if keys is not None else FakeMenuKeys()), again


def outcome(r: ActResult) -> tuple[bool, str | None, str | None, str | None]:
    return r.ok, r.code, r.effect, r.detail


# -- press / click -------------------------------------------------------------------------------


async def test_a_click_presses_the_element_by_token() -> None:
    driver = CallDriver()
    ex, again = executor(driver)
    r = await ex.execute(cand("click", BTN), WIN)
    assert outcome(r) == (True, None, "confirmed", None)
    assert r.route == ["click"]
    assert driver.calls == [("click", {"pid": 1, "element_token": "t:1"})]
    assert again.count == 0


async def test_an_ax_failure_after_the_window_changed_is_never_pressed_again() -> None:
    driver = CallDriver({"click": [AX_FAILED]})
    ex, again = executor(driver, with_signature(WIN, "changed"))
    r = await ex.execute(cand("click", BTN), WIN)
    assert outcome(r) == (True, None, "unverifiable", None)
    assert r.route == ["click", "ax_acted"]
    assert driver.tools == ["click"]
    assert again.count == 1


async def test_an_ax_failure_on_an_unchanged_window_clicks_the_visible_centre() -> None:
    target = node(1, "AXButton", "OK", frame=frame(150, 100))
    driver = geometry_driver(scripts={"click": [AX_FAILED, ok()]})
    ex, _ = executor(driver, WIN)
    r = await ex.execute(cand("toggle", target), WIN)
    assert outcome(r) == (True, None, "confirmed", None)
    assert r.route == ["click", "pixel"]
    assert driver.calls == [
        ("click", {"pid": 1, "element_token": "t:1"}),
        ("list_windows", {}),
        ("get_window_state", {"pid": 1, "window_id": 1, "max_elements": 1}),
        ("click", {"pid": 1, "window_id": 1, "x": 120, "y": 120}),
    ]
    assert list(driver.calls[-1][1]) == ["pid", "window_id", "x", "y"]


async def test_an_ax_failure_without_a_safe_pixel_reports_the_refusal() -> None:
    driver = CallDriver({"click": [AX_FAILED]})
    ex, _ = executor(driver, WIN)
    r = await ex.execute(cand("click", BTN), WIN)
    assert outcome(r) == (False, "ax_action_failed", None, "AX action failed: kAXErrorCannotComplete")
    assert r.route == ["click"]
    assert driver.tools == ["click"]


async def test_other_refusals_are_not_retried_by_pixel() -> None:
    driver = CallDriver({"click": [refused("disabled", "the button is disabled")]})
    ex, again = executor(driver, WIN)
    r = await ex.execute(cand("click", BTN), WIN)
    assert outcome(r) == (False, "disabled", None, "the button is disabled")
    assert again.count == 0


# -- stale tokens --------------------------------------------------------------------------------


async def test_a_stale_token_is_rebound_by_stable_key_once() -> None:
    driver = CallDriver({"click": [STALE, ok()]})
    ex, again = executor(driver, snap([node(1, "AXButton", "OK", tok="t:9")]))
    r = await ex.execute(cand("click", BTN), WIN)
    assert r.ok
    assert r.route == ["click", "rebind"]
    assert driver.calls == [
        ("click", {"pid": 1, "element_token": "t:1"}),
        ("click", {"pid": 1, "element_token": "t:9"}),
    ]
    assert again.count == 1


async def test_a_second_stale_answer_is_reported_not_retried() -> None:
    driver = CallDriver({"click": [STALE]})
    ex, again = executor(driver, snap([node(1, "AXButton", "OK", tok="t:9")]))
    r = await ex.execute(cand("click", BTN), WIN)
    assert outcome(r) == (False, "stale_element_token", None, "element token t:1 is stale")
    assert driver.tools == ["click", "click"]
    assert again.count == 1


async def test_a_stale_token_whose_element_is_gone_is_target_gone() -> None:
    driver = CallDriver({"click": [STALE]})
    ex, _ = executor(driver, snap([]))
    r = await ex.execute(cand("click", BTN), WIN)
    assert outcome(r) == (False, "target_gone", None, "could not rebind AXButton:OK")
    assert r.route == ["click"]
    assert driver.tools == ["click"]


def test_stale_is_recognized_by_code_or_message() -> None:
    assert is_stale(STALE)
    assert is_stale(refused("internal", "Element NOT FOUND IN CACHE"))
    assert is_stale(refused("internal", "unknown token; Call Get_Window_State First"))
    assert not is_stale(refused("internal", "element not found"))
    assert not is_stale(ok())


# -- element tools -------------------------------------------------------------------------------


async def test_context_menu_is_a_background_right_click() -> None:
    row = node(2, "AXRow", "Draft SOW.docx")
    driver = CallDriver()
    ex, _ = executor(driver)
    r = await ex.execute(cand("context_menu", row), snap([row]))
    assert r.ok
    assert r.route == ["right_click"]
    assert driver.calls == [("right_click", {"pid": 1, "window_id": 1, "element_token": "t:2"})]


async def test_set_value_writes_the_value_as_given() -> None:
    field = node(2, "AXTextField", "Name")
    driver = CallDriver()
    ex, _ = executor(driver)
    await ex.execute(cand("set_value", field, text=" x "), snap([field]))
    await ex.execute(cand("set_value", field), snap([field]))
    assert driver.calls == [
        ("set_value", {"pid": 1, "element_token": "t:2", "value": " x "}),
        ("set_value", {"pid": 1, "element_token": "t:2", "value": ""}),
    ]


async def test_type_into_enters_the_text_untouched() -> None:
    field = node(2, "AXTextField", "Name")
    driver = CallDriver()
    ex, _ = executor(driver)
    r = await ex.execute(cand("type_into", field, text=" a  b \n"), snap([field]))
    assert r.route == ["type_text"]
    assert driver.calls == [("type_text", {"pid": 1, "element_token": "t:2", "text": " a  b \n"})]


async def test_scroll_is_one_page_down_unless_told() -> None:
    area = node(2, "AXScrollArea", "List")
    driver = CallDriver()
    ex, _ = executor(driver)
    await ex.execute(cand("scroll", area), snap([area]))
    await ex.execute(cand("scroll", area, direction="up"), snap([area]))
    payload = {"pid": 1, "window_id": 1, "element_token": "t:2", "by": "page", "amount": 1}
    assert driver.calls == [
        ("scroll", {**payload, "direction": "down"}),
        ("scroll", {**payload, "direction": "up"}),
    ]
    assert list(driver.calls[0][1]) == ["pid", "window_id", "element_token", "direction", "by", "amount"]


async def test_keys_go_to_the_window_or_to_the_focused_control() -> None:
    row = node(2, "AXRow", "Draft")
    driver = CallDriver()
    ex, _ = executor(driver)
    r = await ex.execute(cand("key", keys=["return"]), WIN)
    assert r.route == ["press_key"]
    r = await ex.execute(cand("key", row, keys=["shift", "f10"]), snap([row]))
    assert r.route == ["press_key"]
    assert driver.calls == [
        ("press_key", {"pid": 1, "window_id": 1, "key": "return"}),
        ("press_key", {"pid": 1, "window_id": 1, "element_token": "t:2", "key": "f10", "modifiers": ["shift"]}),
    ]
    assert list(driver.calls[1][1]) == ["pid", "window_id", "element_token", "key", "modifiers"]


async def test_a_candidate_without_its_target_is_an_error() -> None:
    ex, _ = executor(CallDriver())
    with pytest.raises(ValueError, match="has no target"):
        await ex.execute(cand("click"), WIN)
    with pytest.raises(ValueError, match="has no keys"):
        await ex.execute(cand("key", keys=[]), WIN)
    with pytest.raises(ValueError, match="has no menu item"):
        await ex.execute(cand("menu"), WIN)


async def test_a_driver_exception_propagates() -> None:
    ex, _ = executor(CallDriver({"click": [RuntimeError("connection lost")]}))
    with pytest.raises(RuntimeError, match="connection lost"):
        await ex.execute(cand("click", BTN), WIN)


# -- retype --------------------------------------------------------------------------------------

WEB = node(1, "AXWebArea", "Order")
QTY = node(2, "AXIncrementor", "Quantity", parent=1, raw_value="2", value="2", exact=True)
ORDER = snap([WEB, QTY])


async def test_a_web_number_field_is_retyped_never_written() -> None:
    driver = CallDriver()
    ex, _ = executor(driver)
    r = await ex.execute(cand("set_value", QTY, text="1"), ORDER)
    assert r.ok
    assert r.route == ["press_key", "press_key", "type_text"]
    assert driver.calls == [
        ("press_key", {"pid": 1, "window_id": 1, "element_token": "t:2", "key": "end", "modifiers": []}),
        ("press_key", {"pid": 1, "window_id": 1, "element_token": "t:2", "key": "home", "modifiers": ["shift"]}),
        ("type_text", {"pid": 1, "element_token": "t:2", "text": "1"}),
    ]


async def test_a_number_field_outside_a_web_page_takes_the_value() -> None:
    qty = node(2, "AXIncrementor", "Quantity")
    driver = CallDriver()
    ex, _ = executor(driver)
    await ex.execute(cand("set_value", qty, text="1"), snap([qty]))
    assert driver.calls == [("set_value", {"pid": 1, "element_token": "t:2", "value": "1"})]


async def test_retyping_an_empty_text_deletes_the_selection() -> None:
    driver = CallDriver()
    ex, _ = executor(driver)
    r = await ex.retype_field(cand("set_value", QTY, text=""), ORDER)
    assert r.route == ["press_key", "press_key", "press_key"]
    assert driver.calls[-1] == (
        "press_key",
        {"pid": 1, "window_id": 1, "element_token": "t:2", "key": "delete", "modifiers": []},
    )


@pytest.mark.parametrize("fail_at", [0, 1, 2])
async def test_retyping_stops_at_the_first_refused_step(fail_at: int) -> None:
    answers: list[ToolResult] = [ok(), ok(), ok()]
    answers[fail_at] = refused("internal", "nope")
    driver = CallDriver({"press_key": [*answers[:2], ok()], "type_text": [answers[2]]})
    ex, _ = executor(driver)
    r = await ex.retype_field(cand("set_value", QTY, text="7"), ORDER)
    assert outcome(r) == (False, "internal", None, "nope")
    assert len(driver.calls) == fail_at + 1


# -- modifiers -----------------------------------------------------------------------------------


async def test_a_modifier_click_needs_a_visible_control() -> None:
    driver = CallDriver()
    ex, _ = executor(driver)
    r = await ex.execute(cand("click", BTN), WIN, ["shift"])
    assert outcome(r) == (
        False,
        "not_visible",
        None,
        "a click with modifier keys is a pixel click, and this control is not visible on the window "
        "(scroll it into view)",
    )
    assert r.route == []
    assert driver.calls == []


async def test_a_modifier_click_is_a_pixel_click_holding_the_keys() -> None:
    target = node(1, "AXRow", "Draft", frame=frame(150, 100))
    driver = geometry_driver()
    ex, _ = executor(driver)
    r = await ex.execute(cand("click", target), snap([target]), ["shift", "cmd"])
    assert r.ok
    assert r.route == ["pixel", "mod:shift+cmd"]
    assert driver.calls[-1] == ("click", {"pid": 1, "window_id": 1, "x": 120, "y": 120, "modifier": ["shift", "cmd"]})


# -- keypad --------------------------------------------------------------------------------------

SEVEN = node(7, "AXButton", "7", frame=frame(150, 100))
EIGHT = node(8, "AXButton", "8")
PAD = snap([SEVEN, EIGHT])
PLAIN_PAD = snap([node(7, "AXButton", "7"), EIGHT])  # no key visible: every press goes by token


async def test_keypad_presses_by_pixel_where_visible_and_by_token_elsewhere() -> None:
    driver = geometry_driver()
    ex, again = executor(driver)
    r = await ex.execute(cand("keypad", presses=[SEVEN, EIGHT]), PAD)
    assert outcome(r) == (True, None, "unverifiable", None)
    assert r.route == ["pixel", "ax"]
    assert driver.calls == [
        ("list_windows", {}),
        ("get_window_state", {"pid": 1, "window_id": 1, "max_elements": 1}),
        ("click", {"pid": 1, "window_id": 1, "x": 120, "y": 120}),
        ("click", {"pid": 1, "element_token": "t:8"}),
    ]
    assert again.count == 0


async def test_keypad_with_no_keys_does_nothing() -> None:
    driver = CallDriver()
    ex, _ = executor(driver)
    r = await ex.execute(cand("keypad", presses=[]), PAD)
    assert (r.ok, r.effect, r.route) == (True, "unverifiable", [])
    r = await ex.execute(cand("keypad"), PAD)
    assert (r.ok, r.effect, r.route) == (True, "unverifiable", [])
    assert driver.calls == []


async def test_keypad_rebinding_stops_at_its_budget() -> None:
    driver = CallDriver({"click": [STALE]})
    ex, again = executor(driver, snap([node(8, "AXButton", "8", tok="t:80")]))
    r = await ex.execute(cand("keypad", presses=[EIGHT]), PLAIN_PAD)
    assert outcome(r) == (False, "stale_element_token", None, "key 1/1: element token t:1 is stale")
    assert r.route == ["ax", "rebind", "ax", "rebind", "ax"]
    assert [p["element_token"] for _, p in driver.calls] == ["t:8", "t:80", "t:80"]
    assert again.count == 2


async def test_keypad_retries_the_same_key_after_a_rebind() -> None:
    driver = CallDriver({"click": [ok(), STALE, ok()]})
    ex, again = executor(driver, snap([node(7, "AXButton", "7", tok="t:70"), node(8, "AXButton", "8", tok="t:80")]))
    r = await ex.execute(cand("keypad", presses=[node(7, "AXButton", "7"), EIGHT]), PLAIN_PAD)
    assert r.ok
    assert r.route == ["ax", "ax", "rebind", "ax"]
    assert [p["element_token"] for _, p in driver.calls] == ["t:7", "t:8", "t:80"]
    assert again.count == 1


async def test_keypad_stops_at_a_refused_key() -> None:
    driver = CallDriver({"click": [ok(), refused("internal", "gone")]})
    ex, again = executor(driver)
    r = await ex.execute(cand("keypad", presses=[node(7, "AXButton", "7"), EIGHT, EIGHT]), PLAIN_PAD)
    assert outcome(r) == (False, "internal", None, "key 2/3: gone")
    assert driver.tools == ["click", "click"]
    assert again.count == 0


# -- pop-ups -------------------------------------------------------------------------------------

POP = node(1, "AXPopUpButton", "Small", key="pop", value="Small")


def popup(value: str, open_: bool) -> Snapshot:
    nodes = [node(1, "AXPopUpButton", value, key="pop", value=value)]
    if open_:
        nodes.append(node(2, "AXMenu", parent=1))
        nodes += [node(i, "AXMenuItem", t, parent=2) for i, t in ((3, "Small"), (4, "Medium"), (5, "Large"))]
        nodes.append(node(6, "AXMenuItem", "Large", in_menu_bar=True))
    return with_signature(snap(nodes), f"{value}:{open_}")


async def test_choose_opens_the_pop_up_and_presses_the_item_with_that_title() -> None:
    driver = CallDriver()
    ex, again = executor(driver, popup("Small", True), popup("Large", False))
    r = await ex.execute(cand("choose_option", POP, text=" Large "), popup("Small", False))
    assert r.ok
    assert r.route == ["click", "click"]
    assert driver.calls == [
        ("click", {"pid": 1, "element_token": "t:1"}),
        ("click", {"pid": 1, "element_token": "t:5"}),
    ]
    assert again.count == 2


async def test_choose_closes_a_menu_without_that_title_and_lists_its_items() -> None:
    driver = CallDriver()
    ex, _ = executor(driver, popup("Small", True))
    r = await ex.execute(cand("choose_option", POP, text="Huge"), popup("Small", False))
    assert outcome(r) == (
        False,
        "option_not_found",
        None,
        'the pop-up has no item "Huge"; it lists "Small", "Medium", "Large"',
    )
    assert r.route == ["click", "press_key"]
    assert driver.calls[-1] == ("press_key", {"pid": 1, "window_id": 1, "key": "escape"})


async def test_choose_reports_a_pop_up_that_opened_no_menu() -> None:
    driver = CallDriver()
    ex, _ = executor(driver, popup("Small", False))
    r = await ex.execute(cand("choose_option", POP, text="Large"), popup("Small", False))
    assert outcome(r) == (False, "option_not_found", None, "pressing the pop-up opened no menu")
    assert driver.tools == ["click"]


async def test_choose_reports_a_pop_up_that_could_not_be_pressed() -> None:
    driver = CallDriver({"click": [refused("disabled", "the pop-up is disabled")]})
    ex, again = executor(driver)
    r = await ex.execute(cand("choose_option", POP, text="Large"), popup("Small", False))
    assert outcome(r) == (False, "disabled", None, "the pop-up is disabled")
    assert again.count == 0


async def test_an_item_pressed_directly_closes_a_menu_left_open() -> None:
    item = popup("Small", True).nodes[4]
    assert item.label == "Large"
    left_open = snap(popup("Small", True).nodes, modal=Modal(role="AXMenu", label="", index=2))
    driver = CallDriver()
    ex, _ = executor(driver, left_open)
    r = await ex.execute(cand("click", item), popup("Small", True))
    assert r.route == ["click", "press_key"]
    assert driver.calls == [
        ("click", {"pid": 1, "element_token": "t:5"}),
        ("press_key", {"pid": 1, "window_id": 1, "key": "escape"}),
    ]


async def test_an_item_pressed_directly_leaves_a_closed_menu_alone() -> None:
    item = popup("Small", True).nodes[4]
    driver = CallDriver()
    ex, again = executor(driver, popup("Large", False))
    r = await ex.execute(cand("click", item), popup("Small", True))
    assert r.route == ["click"]
    assert again.count == 1


async def test_a_refused_item_press_does_not_reobserve() -> None:
    item = popup("Small", True).nodes[4]
    driver = CallDriver({"click": [refused("internal", "no")]})
    ex, again = executor(driver)
    r = await ex.execute(cand("click", item), popup("Small", True))
    assert not r.ok
    assert again.count == 0


def test_pop_up_menu_items_are_found_through_their_parents() -> None:
    s = popup("Small", True)
    assert in_popup_menu(s, s.nodes[2])
    assert not in_popup_menu(s, s.nodes[5])  # the menu bar's own item
    assert not in_popup_menu(s, s.nodes[0])
    orphan = node(9, "AXMenuItem", "Loose", parent=42)
    assert not in_popup_menu(snap([orphan]), orphan)
    looped = [node(1, "AXMenu", parent=2), node(2, "AXMenu", parent=1), node(3, "AXMenuItem", "X", parent=1)]
    assert not in_popup_menu(snap(looped), looped[2])


# -- menu commands -------------------------------------------------------------------------------

COPY = menu_item("Edit", "Copy")
EDIT_MENU = [menu_item("Edit"), COPY]


async def test_a_menu_command_is_sent_as_its_shortcut_in_the_background() -> None:
    keys = FakeMenuKeys()
    driver = CallDriver()
    ex, again = executor(driver, snap(menu=EDIT_MENU), keys=keys)
    r = await ex.execute(cand("menu", menu=COPY), snap(menu=EDIT_MENU))
    assert r.ok
    assert r.route == ["hotkey", "bg:cmd+c"]
    assert driver.calls == [("hotkey", {"pid": 1, "window_id": 1, "keys": ["cmd", "c"]})]
    assert keys.asked == [1]
    assert again.count == 1


async def test_a_menu_command_without_a_shortcut_needs_the_foreground() -> None:
    frob = menu_item("Edit", "Frobnicate")
    driver = CallDriver()
    ex, again = executor(driver, keys=FakeMenuKeys())
    r = await ex.execute(cand("menu", menu=frob), snap(menu=[frob]))
    assert outcome(r) == (
        False,
        "foreground_required",
        None,
        '"Edit > Frobnicate" has no keyboard shortcut, so it cannot run in the background',
    )
    assert driver.calls == []
    assert again.count == 0


async def test_a_menu_command_is_checked_on_a_fresh_menu_first() -> None:
    driver = CallDriver()
    ex, _ = executor(driver, snap(menu=[menu_item("Edit")]))
    r = await ex.execute(cand("menu", menu=COPY), snap(menu=EDIT_MENU))
    assert outcome(r) == (False, "menu_item_gone", None, "Edit > Copy not found")
    ex, _ = executor(driver, snap(menu=[menu_item("Edit"), menu_item("Edit", "Copy", enabled=False)]))
    r = await ex.execute(cand("menu", menu=COPY), snap(menu=EDIT_MENU))
    assert outcome(r) == (
        False,
        "menu_item_disabled",
        None,
        "Edit > Copy is disabled (the app has no key window for it in the background)",
    )
    assert driver.calls == []


def test_path_state_checks_every_level_below_the_menu_bar_title() -> None:
    menu = [
        menu_item("Format", enabled=False),
        menu_item("Format", "Font", enabled=False),
        menu_item("Format", "Font", "Bold"),
        menu_item("Edit", "Copy"),
        menu_item("Edit", "Copy", enabled=False),
    ]
    s = snap(menu=menu)
    assert path_state(s, ["Format"]) == "enabled"
    assert path_state(s, ["Gone"]) == "enabled"
    assert path_state(s, ["Format", "Font", "Bold"]) == "disabled"
    assert path_state(s, ["Format", "Size", "Bigger"]) == "gone"
    assert path_state(s, ["Edit", "Copy"]) == "enabled"  # the first item with that path counts


# -- append --------------------------------------------------------------------------------------

AREA = node(1, "AXTextArea", key="area", raw_value="alpha ", value="alpha", exact=True)


async def test_append_moves_the_caret_to_the_end_and_types_there() -> None:
    fresh = snap([node(1, "AXTextArea", key="area", tok="t:1b", raw_value="alpha ", exact=True)])
    driver = CallDriver()
    ex, again = executor(driver, fresh)
    r = await ex.execute(cand("append", AREA, text="beta"), snap([AREA]))
    assert r.ok
    assert r.route == ["press_key", "type_text"]
    assert driver.calls == [
        ("press_key", {"pid": 1, "window_id": 1, "element_token": "t:1b", "key": "down", "modifiers": ["cmd"]}),
        ("type_text", {"pid": 1, "element_token": "t:1b", "text": "beta"}),
    ]
    assert again.count == 1


async def test_append_sets_the_whole_exact_value_when_the_caret_is_refused() -> None:
    driver = CallDriver({"press_key": [refused("internal", "key refused")]})
    ex, _ = executor(driver, snap([AREA]))
    r = await ex.execute(cand("append", AREA, text="beta"), snap([AREA]))
    assert r.route == ["press_key", "set_value"]
    assert driver.calls[-1] == ("set_value", {"pid": 1, "element_token": "t:1", "value": "alpha beta"})


async def test_append_never_sets_a_field_whose_text_is_not_exact() -> None:
    loose = node(1, "AXTextArea", key="area", value="alpha")
    driver = CallDriver({"press_key": [refused("internal", "key refused")]})
    ex, _ = executor(driver, snap([loose]))
    r = await ex.execute(cand("append", loose, text="beta"), snap([loose]))
    assert outcome(r) == (
        False,
        "caret_refused",
        None,
        "key refused; the field's exact text is unknown, so it is not rewritten",
    )
    assert driver.tools == ["press_key"]


async def test_append_starts_from_an_empty_field_when_nothing_was_read() -> None:
    blank = node(1, "AXTextArea", key="area", exact=True)
    driver = CallDriver({"press_key": [refused("internal", "key refused")]})
    ex, _ = executor(driver, snap([blank]))
    await ex.execute(cand("append", blank, text="beta"), snap([blank]))
    assert driver.calls[-1] == ("set_value", {"pid": 1, "element_token": "t:1", "value": "beta"})


async def test_append_to_a_field_that_is_gone() -> None:
    driver = CallDriver()
    ex, _ = executor(driver, snap([]))
    r = await ex.execute(cand("append", AREA, text="x"), snap([AREA]))
    assert outcome(r) == (False, "target_gone", None, "could not rebind area")
    r = await ex.execute(cand("append", text="x"), snap([AREA]))
    assert outcome(r) == (False, "target_gone", None, "could not rebind undefined")
    assert driver.calls == []


# -- results -------------------------------------------------------------------------------------


def test_cua_driver_words_and_codes_are_forwarded_unchanged() -> None:
    failed = to_act(ok({"effect": "failed"}))
    assert (failed.ok, failed.code, failed.effect) == (False, None, "failed")
    novel = to_act(ok({"effect": "delivered_somehow"}))
    assert (novel.ok, novel.effect) == (True, "delivered_somehow")
    assert to_act(ok({})).effect is None
    assert to_act(ok({"effect": ""})).effect is None
    assert to_act(ok({"effect": 3})).effect is None
    unknown = to_act(refused("brand_new_code", "something new"))
    assert (unknown.ok, unknown.code, unknown.detail) == (False, "brand_new_code", "something new")


async def test_ms_is_the_elapsed_time_rounded_half_up() -> None:
    ticks = iter([0.0, 0.0125])
    ex = Executor(CallDriver(), Reobserve(), menu_keys=FakeMenuKeys(), clock=lambda: next(ticks))
    r = await ex.execute(cand("click", BTN), WIN)
    assert r.ms == 13
