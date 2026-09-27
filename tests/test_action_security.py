from __future__ import annotations

import pytest

from beans_picker.act.execute import Executor
from beans_picker.candidates.build import BuildOptions, build_candidates
from beans_picker.candidates.safety import is_destructive_label
from beans_picker.candidates.types import ActionKind
from beans_picker.observe.identity import key_of, menu_key
from beans_picker.observe.snapshot import assign_keys
from tests.act_support import CallDriver, FakeMenuKeys, Reobserve, cand, node, ok, refused, snap


def test_identity_delimiters_cannot_merge_controls_or_menu_paths() -> None:
    assert key_of("AXTextField", "a|b", "c", []) != key_of("AXTextField", "a", "b|c", [])
    assert key_of("AXButton", None, "OK", ["a>b", "c"]) != key_of("AXButton", None, "OK", ["a", "b>c"])
    assert menu_key(["A > B", "C"]) != menu_key(["A", "B > C"])


@pytest.mark.parametrize("label", ["送信", "購入", "決済", "支払う", "消去", "公開", "Publish", "Pay now"])
def test_irreversible_labels_are_guarded(label: str) -> None:
    assert is_destructive_label(label)


def test_global_activation_keys_and_requested_destructive_options_require_confirmation() -> None:
    candidates = build_candidates(snap([node(1, "AXPopUpButton", "Actions")]), BuildOptions(text="Delete"))
    assert all(c.destructive for c in candidates if c.kind == "key" and c.keys in [["return"], ["space"]])
    assert next(c for c in candidates if c.kind == "choose_option").destructive


def test_duplicate_identity_never_inherits_a_unique_identity() -> None:
    twins = [node(1, "AXTextField", "Account", tok="a"), node(2, "AXTextField", "Account", tok="b")]
    assign_keys(twins)
    unique = node(2, "AXTextField", "Account", tok="b")
    assign_keys([unique])
    assert unique.key not in {n.key for n in twins}
    reordered = [node(2, "AXTextField", "Account", tok="b"), node(1, "AXTextField", "Account", tok="a")]
    assign_keys(reordered)
    assert [n.key for n in twins] == [n.key for n in reversed(reordered)]


async def test_stale_duplicate_never_retypes_into_a_remaining_twin() -> None:
    twins = [node(1, "AXTextField", "Account", tok="a"), node(2, "AXTextField", "Account", tok="b")]
    assign_keys(twins)
    remaining = node(2, "AXTextField", "Account", tok="new-b")
    assign_keys([remaining])
    driver = CallDriver({"type_text": [refused("stale_element_token", "old token")]})
    executor = Executor(driver, Reobserve(snap([remaining])), menu_keys=FakeMenuKeys())
    result = await executor.execute(cand("type_into", twins[0], text="private"), snap(twins))
    assert not result.ok
    assert len(driver.calls) == 1


async def test_newly_dangerous_popup_item_is_not_pressed() -> None:
    popup = node(1, "AXPopUpButton", "Actions")
    item = node(2, "AXMenuItem", "Proceed", parent=1)
    item.help = "Delete all files permanently"
    driver = CallDriver()
    executor = Executor(driver, Reobserve(snap([popup, item])), menu_keys=FakeMenuKeys())
    result = await executor.execute(cand("choose_option", popup, text="Proceed"), snap([popup]))
    assert not result.ok
    assert result.code == "needs_confirmation"
    assert driver.calls == [("click", {"pid": 1, "element_token": popup.token})]


@pytest.mark.parametrize("keys", [["return"], ["space"]])
async def test_unfocused_activation_needs_explicit_permission(keys: list[str]) -> None:
    driver = CallDriver()
    executor = Executor(driver, Reobserve(), menu_keys=FakeMenuKeys())
    candidate = cand("key", keys=keys)
    denied = await executor.execute(candidate, snap())
    assert denied.code == "needs_confirmation"
    assert driver.calls == []
    allowed = await executor.execute(candidate, snap(), allow_destructive=True)
    assert allowed.ok
    assert driver.tools == ["press_key"]


async def test_explicit_permission_allows_a_destructive_popup_selection() -> None:
    popup = node(1, "AXPopUpButton", "Actions")
    item = node(2, "AXMenuItem", "Delete", parent=1)
    driver = CallDriver()
    executor = Executor(driver, Reobserve(snap([popup, item]), snap([popup])), menu_keys=FakeMenuKeys())
    result = await executor.execute(cand("choose_option", popup, text="Delete"), snap([popup]), allow_destructive=True)
    assert result.ok
    assert [args.get("element_token") for _, args in driver.calls] == [popup.token, item.token]


async def test_stale_menu_item_is_rechecked_before_a_second_press() -> None:
    popup = node(1, "AXPopUpButton", "Actions")
    item = node(2, "AXMenuItem", "Proceed", parent=1, tok="old")
    changed = node(2, "AXMenuItem", "Proceed", parent=1, tok="new")
    changed.help = "Purchase immediately"
    driver = CallDriver({"click": [ok(), refused("stale_element_token", "expired")]})
    executor = Executor(driver, Reobserve(snap([popup, item]), snap([popup, changed])), menu_keys=FakeMenuKeys())
    result = await executor.execute(cand("choose_option", popup, text="Proceed"), snap([popup]))
    assert result.code == "needs_confirmation"
    assert [args.get("element_token") for _, args in driver.calls] == [popup.token, "old"]


async def test_duplicate_popup_option_titles_are_refused() -> None:
    popup = node(1, "AXPopUpButton", "Actions")
    items = [node(2, "AXMenuItem", "Proceed", parent=1), node(3, "AXMenuItem", "Proceed", parent=1)]
    driver = CallDriver()
    executor = Executor(driver, Reobserve(snap([popup, *items])), menu_keys=FakeMenuKeys())
    result = await executor.execute(cand("choose_option", popup, text="Proceed"), snap([popup]))
    assert result.code == "ambiguous_option"
    assert driver.tools == ["click"]


@pytest.mark.parametrize("kind", ["append", "keypad"])
async def test_other_rebind_paths_refuse_a_disappeared_duplicate(kind: ActionKind) -> None:
    twins = [node(1, "AXTextField", "Account", tok="a"), node(2, "AXTextField", "Account", tok="b")]
    assign_keys(twins)
    remaining = node(2, "AXTextField", "Account", tok="new-b")
    assign_keys([remaining])
    driver = CallDriver({"click": [refused("stale_element_token", "expired")]})
    executor = Executor(driver, Reobserve(snap([remaining])), menu_keys=FakeMenuKeys())
    result = await executor.execute(cand(kind, twins[0], text="private", presses=[twins[0]]), snap(twins))
    assert not result.ok
    assert all(args.get("element_token") != "new-b" for _, args in driver.calls)
