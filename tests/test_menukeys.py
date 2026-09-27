from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from beans_picker._proc import Completed
from beans_picker.errors import ProcessError
from beans_picker.menus.keyequiv import KeyEquivalent, english_title, key_equivalent, normalize_title, view_modes
from beans_picker.menus.menukeys import (
    LearnedKey,
    MenuKeys,
    MenuKeyTable,
    RawMenuKey,
    hotkey_for,
    raw_menu_keys,
    table_from,
)
from beans_picker.paths import Paths
from tests.fakes import FakeRunner

UP_ARROW = chr(0xF700)
APPLE_LOGO = chr(0xF8FF)


@dataclass(frozen=True)
class Item:
    path: list[str]


def raw(path: list[str], key: str, top: int, mods: list[str] | None = None) -> RawMenuKey:
    return RawMenuKey(path=path, key=key, mods=mods if mods is not None else ["cmd"], top=top)


LOCAL = [
    raw(["TextEdit", "テキストエディットを非表示"], "h", 0),
    raw(["フォーマット", "フォント", "大きく"], "+", 4),
    raw(["フォーマット", "テキスト", "中央揃え"], "|", 4),
]
ENGLISH = [
    raw(["TextEdit", "Hide TextEdit"], "h", 0),
    raw(["Format", "Font", "Bigger"], "+", 4),
    raw(["Format", "Text", "Center"], "|", 4),
]
TABLE = table_from(LOCAL, ENGLISH)


class TestHotkeyFor:
    def test_maps_nib_key_equivalents_to_cua_driver_hotkeys(self) -> None:
        assert hotkey_for("+", ["cmd"]) == ["cmd", "+"]
        assert hotkey_for("S", ["cmd"]) == ["cmd", "shift", "s"]
        assert hotkey_for("f", ["option", "cmd"]) == ["cmd", "option", "f"]
        assert hotkey_for(UP_ARROW, ["cmd"]) == ["cmd", "up"]
        assert hotkey_for("\r", ["cmd"]) == ["cmd", "return"]

    def test_rejects_keys_without_a_modifier_or_that_cannot_be_named(self) -> None:
        assert hotkey_for("a", []) is None
        assert hotkey_for(APPLE_LOGO, ["cmd"]) is None
        assert hotkey_for("", ["cmd"]) is None

    def test_function_keys_and_modifier_order(self) -> None:
        assert hotkey_for(chr(0xF704), ["cmd"]) == ["cmd", "f1"]
        assert hotkey_for(chr(0xF70F), ["cmd"]) == ["cmd", "f12"]
        assert hotkey_for(chr(0xF710), ["cmd"]) is None
        assert hotkey_for("x", ["shift", "option", "ctrl", "cmd"]) == ["cmd", "ctrl", "option", "shift", "x"]
        assert hotkey_for("\x7f", ["cmd"]) == ["cmd", "delete"]
        assert hotkey_for(" ", ["ctrl"]) == ["ctrl", "space"]

    def test_an_upper_case_letter_alone_is_shift(self) -> None:
        assert hotkey_for("A", []) == ["shift", "a"]

    def test_unknown_modifiers_count_but_are_not_sent(self) -> None:
        assert hotkey_for("k", ["fn"]) == ["k"]

    def test_more_than_one_character_is_refused(self) -> None:
        assert hotkey_for("ab", ["cmd"]) is None

    def test_a_character_outside_printable_ascii_cannot_be_named(self) -> None:
        assert hotkey_for("\N{GRINNING FACE}", ["cmd"]) is None
        assert hotkey_for("\N{LATIN SMALL LETTER E WITH ACUTE}", ["cmd"]) is None


class TestLearnedMenuKeys:
    def test_gives_items_outside_the_stock_list_a_shortcut_preferring_the_apps_own(self) -> None:
        assert key_equivalent(["フォーマット", "フォント", "大きく"], []) is None
        assert key_equivalent(["フォーマット", "フォント", "大きく"], [], TABLE) == KeyEquivalent(
            keys=["cmd", "+"], source="learned"
        )
        found = key_equivalent(["フォーマット", "テキスト", "中央揃え\N{HORIZONTAL ELLIPSIS}"], [], TABLE)
        assert found is not None
        assert found.keys == ["cmd", "|"]

    def test_matches_the_application_menu_whatever_its_run_time_title(self) -> None:
        found = key_equivalent(["テキストエディット", "テキストエディットを非表示"], [], TABLE)
        assert found is not None
        assert found.keys == ["cmd", "h"]

    def test_names_localized_items_in_english_from_the_english_load_of_the_same_nib(self) -> None:
        assert english_title("大きく", TABLE) == "Bigger"
        assert english_title("大きく") is None

    def test_drops_english_names_when_the_two_loads_do_not_line_up(self) -> None:
        assert table_from(LOCAL, ENGLISH[1:]).english_title("大きく") is None


class TestMenuKeyTable:
    def test_normalizes_paths_and_puts_the_application_menu_under_an_empty_top(self) -> None:
        assert TABLE.size == 3
        assert TABLE.items[0] == LearnedKey(
            path=["", "テキストエディットを非表示"], keys=["cmd", "h"], english=["TextEdit", "Hide TextEdit"]
        )
        assert TABLE.lookup(["Anything", "テキストエディットを非表示"]) == TABLE.items[0]
        assert TABLE.lookup(["テキストエディットを非表示"]) is None

    def test_skips_keys_that_cannot_be_sent(self) -> None:
        table = table_from([raw(["File", "Plain"], "a", 1, mods=[]), raw(["File", "Open..."], "o", 1)])
        assert [it.path for it in table.items] == [["file", "open"]]
        assert table.items[0].english is None

    def test_english_equal_to_the_leaf_is_not_a_translation(self) -> None:
        table = table_from([raw(["Format", "Bigger"], "+", 4)], [raw(["Format", "Bigger"], "+", 4)])
        assert table.english_title("Bigger") is None

    def test_a_later_item_with_the_same_path_wins(self) -> None:
        table = MenuKeyTable(
            [LearnedKey(path=["file", "save"], keys=["cmd", "s"]), LearnedKey(path=["file", "save"], keys=["cmd", "k"])]
        )
        found = table.lookup(["File", "Save"])
        assert found is not None
        assert found.keys == ["cmd", "k"]


class TestStandardKeys:
    def test_normalize_title(self) -> None:
        assert normalize_title("  Save   As\N{HORIZONTAL ELLIPSIS}  ") == "save as"
        assert normalize_title("Open...") == "open"
        assert normalize_title("a......") == "a..."
        assert normalize_title("Find\N{IDEOGRAPHIC SPACE}Next") == "find next"

    def test_stock_commands_in_english_and_japanese(self) -> None:
        assert key_equivalent(["Edit", "Paste and Match Style"], []) == KeyEquivalent(
            keys=["cmd", "option", "shift", "v"], source="standard"
        )
        found = key_equivalent(["フォーマット", "フォントパネルを非表示"], [])
        assert found is not None
        assert found.keys == ["cmd", "t"]
        assert key_equivalent([], []) is None
        assert key_equivalent(["File", ""], []) is None

    def test_english_names_pair_up_in_order(self) -> None:
        assert english_title("新規") == "New"
        assert english_title("新規書類") == "New Document"
        assert english_title("ルーラを非表示") == "Hide Ruler"
        assert english_title("ページサイズで表示") == "Wrap to Page"
        assert english_title("Copy") is None

    def test_duplicate_takes_shift_cmd_s_only_next_to_save_as(self) -> None:
        menu = [Item(["File", "Duplicate"]), Item(["File", "Save As\N{HORIZONTAL ELLIPSIS}"])]
        assert key_equivalent(["File", "Duplicate"], menu) == KeyEquivalent(
            keys=["cmd", "shift", "s"], source="standard"
        )
        assert key_equivalent(["File", "Duplicate"], [Item(["File", "Duplicate"])]) is None
        assert key_equivalent(["File", "Duplicate"], [Item(["Edit", "Save As"])]) is None

    def test_a_learned_shortcut_taken_by_a_runtime_item_moves_to_the_displaced_one(self) -> None:
        table = table_from([raw(["File", "Save As..."], "S", 1)])
        menu = [Item(["File", "Duplicate"]), Item(["File", "Save As\N{HORIZONTAL ELLIPSIS}"])]
        assert key_equivalent(["File", "Save As\N{HORIZONTAL ELLIPSIS}"], menu, table) == KeyEquivalent(
            keys=["cmd", "option", "shift", "s"], source="standard"
        )
        assert key_equivalent(["File", "Save As"], [Item(["File", "Save As"])], table) == KeyEquivalent(
            keys=["cmd", "shift", "s"], source="learned"
        )

    def test_a_taken_learned_shortcut_without_a_displaced_one_is_dropped(self) -> None:
        table = table_from([raw(["File", "Export"], "S", 1)])
        assert key_equivalent(["File", "Export"], [Item(["File", "Duplicate"])], table) is None
        both = table_from([raw(["File", "Export"], "S", 1), raw(["File", "Duplicate"], "d", 1)])
        found = key_equivalent(["File", "Export"], [Item(["File", "Duplicate"])], both)
        assert found is not None
        assert found.source == "learned"


class TestViewConvention:
    MENU = (
        Item(["View", "as Icons"]),
        Item(["View", "as List"]),
        Item(["View", "as Columns"]),
        Item(["View", "Show Toolbar"]),
        Item(["View", "Later"]),
        Item(["Edit", "as Icons"]),
    )

    def test_numbers_the_leading_view_items(self) -> None:
        menu = list(self.MENU)
        assert key_equivalent(["View", "as List"], menu) == KeyEquivalent(keys=["cmd", "2"], source="convention")
        assert key_equivalent(["view", "as Icons"], [Item(["view", "as Icons"])]) == KeyEquivalent(
            keys=["cmd", "1"], source="convention"
        )
        assert key_equivalent(["View", "Later"], menu) is None
        assert key_equivalent(["Edit", "as Icons"], menu) is None
        assert key_equivalent(["表示", "Sub", "as List"], menu) is None

    def test_the_run_stops_at_a_dialog_a_submenu_or_a_stock_command(self) -> None:
        dialog = [Item(["View", "One"]), Item(["View", "Customize..."]), Item(["View", "Two"])]
        assert view_modes(["View", "One"], dialog) is None
        submenu = [Item(["View", "One"]), Item(["View", "Two"]), Item(["View", "Two", "Deep"]), Item(["View", "Three"])]
        assert view_modes(["View", "One"], submenu) is None
        assert view_modes(["View", "One"], [Item(["View", "One"]), Item(["View", "Three"])]) == ["One", "Three"]

    def test_only_nine_items_are_numbered(self) -> None:
        menu = [Item(["View", f"Mode {i}"]) for i in range(1, 12)]
        found = key_equivalent(["View", "Mode 9"], menu)
        assert found is not None
        assert found.keys == ["cmd", "9"]
        assert key_equivalent(["View", "Mode 10"], menu) is None

    def test_view_modes_needs_two_modes_and_the_item_among_them(self) -> None:
        menu = list(self.MENU)
        assert view_modes(["View", "as Columns"], menu) == ["as Icons", "as List", "as Columns"]
        assert view_modes(["View", "Later"], menu) is None
        assert view_modes(["View", "One"], [Item(["View", "One"])]) is None
        assert view_modes(["Window", "as List"], menu) is None
        assert view_modes(["View"], menu) is None


class FakeHelpers:
    def __init__(self, binary: Path | None) -> None:
        self.binary = binary
        self.calls = 0

    async def menukeys_bin(self) -> Path | None:
        self.calls += 1
        return self.binary


PID = 42
PS = ("ps", "-o", "comm=", "-p", str(PID))
LANGS = ("defaults", "read", "-g", "AppleLanguages")


def helper_output(items: list[RawMenuKey]) -> str:
    rows = [{"path": it.path, "key": it.key, "mods": it.mods, "top": it.top} for it in items]
    return "menukeys: a warning line\n" + json.dumps(rows, ensure_ascii=False) + "\n"


def app_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "Apps" / "TextEdit.app"
    (bundle / "Contents").mkdir(parents=True)
    (bundle / "Contents" / "Info.plist").write_text("<plist/>")
    return bundle


def learning_runner(bundle: Path, helper: Path) -> FakeRunner:
    return FakeRunner(
        {
            PS: Completed(f"  {bundle}/Contents/MacOS/TextEdit\n", 0),
            LANGS: Completed('(\n    "ja-JP",\n    "en-JP"\n)\n', 0),
            (str(helper), str(bundle)): Completed(helper_output(LOCAL), 0),
            (str(helper), str(bundle), "-AppleLanguages", "(en)"): Completed(helper_output(ENGLISH), 0),
        }
    )


class TestMenuKeys:
    async def test_learns_a_table_once_per_pid_and_caches_it_on_disk(self, tmp_path: Path, paths: Paths) -> None:
        bundle = app_bundle(tmp_path)
        helper = tmp_path / "menukeys-bin"
        runner = learning_runner(bundle, helper)
        keys = MenuKeys(FakeHelpers(helper), paths, runner=runner)
        assert keys.table_for(PID) is None

        table = await keys.learn(PID)
        assert table is not None
        assert table.english_title("大きく") == "Bigger"
        assert keys.table_for(PID) is table
        assert await keys.learn(PID) is table
        assert len(runner.calls) == 4
        cached = list(paths.menukeys.glob("table-*.json"))
        assert len(cached) == 1
        assert len(cached[0].stem.removeprefix("table-")) == 16

        again = learning_runner(bundle, helper)
        fresh = await MenuKeys(FakeHelpers(helper), paths, runner=again).learn(PID)
        assert fresh is not None
        assert [it.path for it in fresh.items] == [it.path for it in table.items]
        assert again.calls == [PS, LANGS]

    async def test_a_damaged_cache_entry_is_rebuilt(self, tmp_path: Path, paths: Paths) -> None:
        bundle = app_bundle(tmp_path)
        helper = tmp_path / "menukeys-bin"
        await MenuKeys(FakeHelpers(helper), paths, runner=learning_runner(bundle, helper)).learn(PID)
        (cached,) = paths.menukeys.glob("table-*.json")
        cached.write_text("{not json")
        runner = learning_runner(bundle, helper)
        table = await MenuKeys(FakeHelpers(helper), paths, runner=runner).learn(PID)
        assert table is not None
        assert table.size == 3
        assert len(runner.calls) == 4
        assert json.loads(cached.read_text())["local"][0]["path"] == LOCAL[0].path

    async def test_a_failed_learn_is_not_retried(self, tmp_path: Path, paths: Paths) -> None:
        runner = FakeRunner({PS: Completed("/usr/bin/some-tool\n", 0)})
        helpers = FakeHelpers(tmp_path / "bin")
        keys = MenuKeys(helpers, paths, runner=runner)
        assert await keys.learn(PID) is None
        assert await keys.learn(PID) is None
        assert runner.calls == [PS]
        assert keys.table_for(PID) is None

    async def test_never_raises(self, paths: Paths) -> None:
        runner = FakeRunner({PS: ProcessError("ps exited with code 1")})
        assert await MenuKeys(FakeHelpers(None), paths, runner=runner).learn(PID) is None

    async def test_without_info_plist_or_helper_nothing_is_learned(self, tmp_path: Path, paths: Paths) -> None:
        bundle = tmp_path / "Bare.app"
        runner = FakeRunner({PS: Completed(f"{bundle}/Contents/MacOS/Bare\n", 0)})
        helpers = FakeHelpers(tmp_path / "bin")
        assert await MenuKeys(helpers, paths, runner=runner).learn(PID) is None
        assert helpers.calls == 0

        bundle = app_bundle(tmp_path)
        runner = FakeRunner({PS: Completed(f"{bundle}/Contents/MacOS/TextEdit\n", 0), LANGS: ProcessError("no")})
        helpers = FakeHelpers(None)
        assert await MenuKeys(helpers, paths, runner=runner).learn(PID) is None
        assert helpers.calls == 1

    async def test_an_empty_or_failing_helper_learns_nothing_and_caches_nothing(
        self, tmp_path: Path, paths: Paths
    ) -> None:
        bundle = app_bundle(tmp_path)
        helper = tmp_path / "menukeys-bin"
        for output in ("[]\n", "", "{}\n", "not json\n", ProcessError("menukeys exited with code 1")):
            runner = FakeRunner(
                {
                    PS: Completed(f"{bundle}/Contents/MacOS/TextEdit\n", 0),
                    LANGS: Completed("", 0),
                    (str(helper), str(bundle)): output if isinstance(output, Exception) else Completed(output, 0),
                }
            )
            assert await MenuKeys(FakeHelpers(helper), paths, runner=runner).learn(PID) is None
        assert not paths.menukeys.exists() or not list(paths.menukeys.glob("table-*.json"))

    async def test_a_malformed_helper_entry_learns_nothing(self, tmp_path: Path, paths: Paths) -> None:
        bundle = app_bundle(tmp_path)
        helper = tmp_path / "menukeys-bin"
        runner = learning_runner(bundle, helper)
        runner.outputs[(str(helper), str(bundle))] = Completed('[{"path": ["File"], "key": 1}]\n', 0)
        assert await MenuKeys(FakeHelpers(helper), paths, runner=runner).learn(PID) is None

    async def test_concurrent_callers_share_one_learn(self, tmp_path: Path, paths: Paths) -> None:
        bundle = app_bundle(tmp_path)
        helper = tmp_path / "menukeys-bin"
        runner = learning_runner(bundle, helper)
        keys = MenuKeys(FakeHelpers(helper), paths, runner=runner)
        a, b = await asyncio.gather(keys.learn(PID), keys.learn(PID))
        assert a is b
        assert a is not None
        assert runner.calls.count(PS) == 1


class TestRawMenuKeys:
    def test_decodes_helper_entries(self) -> None:
        assert raw_menu_keys([{"path": ["A", "B"], "key": "b", "mods": ["cmd"], "top": 1}]) == [
            RawMenuKey(path=["A", "B"], key="b", mods=["cmd"], top=1)
        ]

    @pytest.mark.parametrize(
        "value",
        [
            {},
            [1],
            [{"path": "A", "key": "b", "mods": [], "top": 1}],
            [{"path": ["A"], "key": "b", "mods": [], "top": True}],
            [{"path": ["A"], "key": "b", "mods": [1], "top": 0}],
        ],
    )
    def test_rejects_malformed_output(self, value: object) -> None:
        with pytest.raises(ValueError, match="menu keys"):
            raw_menu_keys(value)
