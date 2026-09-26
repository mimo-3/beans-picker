from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from cua_jev._proc import DEFAULT_MAX_BYTES, Completed
from cua_jev.errors import ProcessError
from cua_jev.observe.exacttext import ExactText, apply_exact_text, prompt_for_access
from cua_jev.observe.types import UINode
from cua_jev.paths import Paths
from tests.fakes import FakeClock, fake_runner


class _App:
    """An `AxtextBuild` with a fixed answer."""

    def __init__(self, app: Path | None) -> None:
        self.app = app

    async def axtext_app(self) -> Path | None:
        return self.app


type _Answer = Completed | Exception | Callable[[tuple[str, ...]], Completed]


class _Runner:
    """Answers the direct run and `open` from lists (first entry first, the last one repeated);
    records argv and max_bytes."""

    def __init__(self, direct: Sequence[_Answer], via_app: Sequence[_Answer] = ()) -> None:
        self.direct = list(direct)
        self.via_app = list(via_app)
        self.calls: list[tuple[str, ...]] = []
        self.max_bytes: list[int] = []

    async def __call__(
        self, argv: Sequence[str], /, *, max_bytes: int = DEFAULT_MAX_BYTES, check: bool = True, cwd: Path | None = None
    ) -> Completed:
        key = tuple(argv)
        self.calls.append(key)
        self.max_bytes.append(max_bytes)
        answers = self.via_app if key[0] == "open" else self.direct
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, Completed):
            return answer
        return answer(key)


def _writes(text: str) -> Callable[[tuple[str, ...]], Completed]:
    def write(argv: tuple[str, ...]) -> Completed:
        Path(argv[-1]).write_text(text, encoding="utf-8")
        return Completed("", 0)

    return write


def _exists(path: Path) -> bool:
    return path.exists()


def _silent(argv: tuple[str, ...]) -> Completed:
    return Completed("", 0)


FIELDS = '{"fields":[{"window":"W","role":"AXTextField","value":" a "}]}'
FAILED = ProcessError("axtext exited with code 1", returncode=1)


@pytest.fixture
def app(paths: Paths) -> Path:
    app = paths.axtext / "CuaJevAXText-0123456789ab.app"
    app.mkdir(parents=True)
    return app


def _reader(app: Path | None, paths: Paths, runner: _Runner, clock: FakeClock | None = None) -> ExactText:
    return ExactText(_App(app), paths, runner=runner, clock=clock or FakeClock())


async def test_reads_the_fields_directly(app: Path, paths: Paths) -> None:
    runner = _Runner([Completed(FIELDS + "\n", 0)])
    reader = _reader(app, paths, runner)
    assert await reader.read_fields(42) == [{"window": "W", "role": "AXTextField", "value": " a "}]
    assert runner.calls == [(str(app / "Contents" / "MacOS" / "axtext"), "42")]
    assert runner.max_bytes == [32 * 1024 * 1024]
    assert await reader.read_fields(42) is not None
    assert len(runner.calls) == 2


async def test_an_empty_list_is_a_successful_read(app: Path, paths: Paths) -> None:
    runner = _Runner([Completed('{"fields":[]}', 0)])
    assert await _reader(app, paths, runner).read_fields(1) == []
    assert [c[0] for c in runner.calls] == [str(app / "Contents" / "MacOS" / "axtext")]


async def test_falls_back_to_the_helper_app_and_keeps_using_it(app: Path, paths: Paths) -> None:
    runner = _Runner([FAILED], [_writes(FIELDS)])
    reader = _reader(app, paths, runner)
    assert await reader.read_fields(5) == [{"window": "W", "role": "AXTextField", "value": " a "}]
    open_call = runner.calls[1]
    assert open_call[:7] == ("open", "-W", "-g", "-n", str(app), "--args", "5")
    out = Path(open_call[7])
    assert out.parent == paths.axtext
    assert out.name.startswith(f"out-{os.getpid()}-")
    assert out.name.endswith(".json")
    assert not _exists(out)
    assert runner.max_bytes[1] == DEFAULT_MAX_BYTES
    # The app worked, so the next read goes straight to it.
    await reader.read_fields(5)
    assert [c[0] for c in runner.calls] == [str(app / "Contents" / "MacOS" / "axtext"), "open", "open"]


@pytest.mark.parametrize(
    "stdout", ["", "not json", "null", "5", "[1]", '{"fields":null}', '{"fields":{"a":1}}', '{"other":[]}']
)
async def test_output_without_a_fields_list_is_not_a_read(app: Path, paths: Paths, stdout: str) -> None:
    runner = _Runner([Completed(stdout, 0)], [_writes(stdout)])
    reader = _reader(app, paths, runner)
    assert await reader.read_fields(1) is None
    assert [c[0] for c in runner.calls][-1] == "open"


async def test_backs_off_for_thirty_seconds_after_both_ways_fail(app: Path, paths: Paths) -> None:
    clock = FakeClock(100)
    runner = _Runner([FAILED], [_silent])
    reader = _reader(app, paths, runner, clock)
    assert await reader.read_fields(1) is None
    assert len(runner.calls) == 2
    clock.advance(29.9)
    assert await reader.read_fields(1) is None
    assert len(runner.calls) == 2
    clock.advance(0.1)
    assert await reader.read_fields(1) is None
    assert [c[0] for c in runner.calls[2:]] == [str(app / "Contents" / "MacOS" / "axtext"), "open"]


async def test_a_failing_open_counts_as_no_way(app: Path, paths: Paths) -> None:
    clock = FakeClock()
    runner = _Runner([FAILED], [ProcessError("open exited with code 1", returncode=1)])
    reader = _reader(app, paths, runner, clock)
    assert await reader.read_fields(1) is None
    assert await reader.read_fields(1) is None
    assert len(runner.calls) == 2
    assert list(paths.axtext.glob("out-*")) == []


async def test_a_direct_read_after_the_app_way_stopped_working(app: Path, paths: Paths) -> None:
    clock = FakeClock()
    runner = _Runner([FAILED, Completed(FIELDS, 0)], [_writes(FIELDS), _silent])
    reader = _reader(app, paths, runner, clock)
    assert await reader.read_fields(1) is not None  # direct fails, app works
    assert await reader.read_fields(1) is None  # app only: it now writes nothing
    clock.advance(30)
    assert await reader.read_fields(1) is not None  # direct is probed again
    assert [c[0] for c in runner.calls] == [
        str(app / "Contents" / "MacOS" / "axtext"),
        "open",
        "open",
        str(app / "Contents" / "MacOS" / "axtext"),
    ]


async def test_nothing_is_read_without_the_helper(paths: Paths) -> None:
    runner = _Runner([Completed(FIELDS, 0)])
    assert await _reader(None, paths, runner).read_fields(1) is None
    assert runner.calls == []


async def test_prompt_for_access(app: Path) -> None:
    runner = fake_runner({("open", "-W", "-n", str(app), "--args", "--prompt"): Completed("", 0)})
    message = await prompt_for_access(_App(app), runner=runner)
    assert message == f"asked macOS to list {app} under Privacy & Security > Accessibility; turn it on there"
    assert await prompt_for_access(_App(None), runner=runner) == "the helper could not be built (is clang installed?)"
    assert len(runner.calls) == 1
    with pytest.raises(ProcessError):
        await prompt_for_access(_App(app), runner=fake_runner({}))


# apply_exact_text


def _node(role: str, raw_value: str | None = None, raw_label: str | None = None) -> UINode:
    n = UINode(
        index=0,
        token="",
        role=role,
        label="",
        enabled=True,
        actions=[],
        depth=1,
        in_menu_bar=False,
        within=[],
        key=role,
    )
    n.raw_label = raw_label
    if raw_value is not None:
        n.raw_value = raw_value
        n.value = raw_value.strip()
    return n


def test_fields_of_the_window_are_preferred_over_others() -> None:
    field = _node("AXTextField", "b")
    apply_exact_text(
        [field],
        [
            {"window": "Other", "role": "AXTextField", "value": "a"},
            {"window": "Doc", "role": "AXTextField", "value": "b "},
        ],
        "Doc",
    )
    assert (field.raw_value, field.exact) == ("b ", True)
    # With no field of that window, every field is considered.
    other = _node("AXTextField", "a")
    apply_exact_text([other], [{"window": "Other", "role": "AXTextField", "value": " a"}], "Doc")
    assert other.raw_value == " a"


def test_single_line_roles_pair_with_each_other_but_not_with_text_areas() -> None:
    search = _node("AXSearchField", "q")
    apply_exact_text([search], [{"window": "W", "role": "AXComboBox", "value": "q"}], "W")
    assert search.exact is True
    area = _node("AXTextArea", "q")
    apply_exact_text([area], [{"window": "W", "role": "AXTextField", "value": "q"}], "W")
    assert area.exact is None


def test_a_value_matched_by_several_texts_is_left_alone() -> None:
    field = _node("AXTextField", "x")
    other = _node("AXTextField", "zzz")
    apply_exact_text(
        [field, other],
        [{"window": "W", "role": "AXTextField", "value": "x"}, {"window": "W", "role": "AXTextField", "value": " x"}],
        "W",
    )
    assert field.exact is None
    same = _node("AXTextField", "x")
    apply_exact_text(
        [same, other],
        [{"window": "W", "role": "AXTextField", "value": " x"}, {"window": "W", "role": "AXTextField", "value": " x"}],
        "W",
    )
    assert (same.raw_value, same.exact) == (" x", True)
    assert other.exact is None


def test_the_document_url_is_kept() -> None:
    field = _node("AXTextArea", "text")
    apply_exact_text(
        [field], [{"window": "W", "role": "AXTextArea", "value": "text", "document": "file:///tmp/a.txt"}], "W"
    )
    assert field.document == "file:///tmp/a.txt"
    no_doc = _node("AXTextArea", "text")
    apply_exact_text([no_doc], [{"window": "W", "role": "AXTextArea", "value": "text", "document": ""}], "W")
    assert no_doc.document is None


def test_toggles_pair_only_when_titles_agree() -> None:
    box = _node("AXCheckBox", raw_label="Bold")
    radio = _node("AXRadioButton", raw_label="Left")
    apply_exact_text(
        [box, radio],
        [
            {"window": "W", "role": "AXCheckBox", "title": " Bold", "value": "0"},
            {"window": "W", "role": "AXRadioButton", "value": "1"},
        ],
        "W",
    )
    assert (box.value, box.raw_value, radio.value) == ("0", "0", "1")
    other = _node("AXCheckBox", raw_label="Italic")
    apply_exact_text([other], [{"window": "W", "role": "AXCheckBox", "title": "Bold", "value": "1"}], "W")
    assert other.exact is None
    unnamed = _node("AXSwitch")
    apply_exact_text([unnamed], [{"window": "W", "role": "AXSwitch", "title": "On", "value": "1"}], "W")
    assert unnamed.exact is None


def test_entries_that_are_not_objects_match_nothing() -> None:
    field = _node("AXTextField", "a")
    apply_exact_text([field], [5, "x", [], {"window": "W", "role": "AXTextField", "value": "a"}], "W")
    assert field.exact is True
    with pytest.raises(TypeError):
        apply_exact_text([field], [None], "W")


def test_a_field_without_text_is_refused_when_its_text_is_compared() -> None:
    with pytest.raises(TypeError):
        apply_exact_text([_node("AXTextField", "a")], [{"window": "W", "role": "AXTextField"}], "W")
    # A node without a value pairs by role alone and takes the entry as it is.
    stepper = _node("AXIncrementor")
    apply_exact_text([stepper], [{"window": "W", "role": "AXIncrementor", "value": 3}], "W")
    assert (stepper.raw_value, stepper.value, stepper.exact) == ("3", "3", True)


def test_a_toggle_state_without_title_pairs_by_order() -> None:
    box = _node("AXCheckBox", raw_label="Bold")
    apply_exact_text(
        [box], [{"window": "W", "role": "AXCheckBox", "title": "", "value": "1", "document": ["file:///a"]}], "W"
    )
    assert (box.value, box.exact, box.document) == ("1", True, "file:///a")
    numbered = _node("AXCheckBox", raw_label="1")
    apply_exact_text([numbered], [{"window": "W", "role": "AXCheckBox", "title": 1, "value": "0"}], "W")
    assert numbered.exact is True
    zero = _node("AXCheckBox", raw_label="x")
    apply_exact_text([zero], [{"window": "W", "role": "AXCheckBox", "title": 0, "value": "0"}], "W")
    assert zero.exact is True
