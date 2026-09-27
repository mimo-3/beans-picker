from __future__ import annotations

from dataclasses import dataclass, field

from beans_picker.observe.identity import menu_key, stable_key


@dataclass
class _Parts:
    role: str
    label: str
    identifier: str | None = None
    within: list[str] = field(default_factory=list)


def test_stable_key_joins_role_identifier_label_and_containers() -> None:
    ok = _Parts("AXButton", "OK", within=["AXSheet: Save", "AXWindow: Doc"])
    assert stable_key(ok) == '["AXButton",null,"OK",["AXSheet: Save","AXWindow: Doc"]]'
    assert stable_key(_Parts("AXButton", "", identifier="")) == '["AXButton","","",[]]'
    assert stable_key(_Parts("AXButton", "7", identifier="Seven")) == '["AXButton","Seven","7",[]]'


def test_stable_key_uses_three_containers_at_most() -> None:
    n = _Parts("AXButton", "Go", within=["a", "b", "c", "d"])
    assert stable_key(n) == '["AXButton",null,"Go",["a","b","c"]]'


def test_menu_key() -> None:
    assert menu_key(["File", "Save"]) == '["menu",["File","Save"]]'
    assert menu_key([]) == '["menu",[]]'
