from __future__ import annotations

import pytest

from beans_picker.observe.exacttext import apply_exact_text
from beans_picker.observe.types import UINode


def _field(*, role: str = "AXTextField", subrole: str | None = None, value: str | None = None) -> UINode:
    return UINode(
        index=1,
        token="",
        role=role,
        subrole=subrole,
        label="Account",
        raw_label="Account",
        raw_value=value,
        enabled=True,
        actions=[],
        depth=1,
        in_menu_bar=False,
        within=[],
        key="field",
    )


def test_unmatched_window_never_imports_another_windows_exact_text() -> None:
    node = _field(value="shared")
    apply_exact_text([node], [{"window": "Private", "role": "AXTextField", "value": " shared "}], "Public")
    assert (node.raw_value, node.exact) == ("shared", None)


def test_duplicate_window_titles_with_extra_fields_do_not_claim_exactness() -> None:
    node = _field(value="shared")
    entries = [
        {"window": "Untitled", "role": "AXTextField", "value": " shared "},
        {"window": "Untitled", "role": "AXTextField", "value": "another windows value"},
    ]
    apply_exact_text([node], entries, "Untitled")
    assert (node.raw_value, node.exact) == ("shared", None)


@pytest.mark.parametrize(("role", "subrole"), [("AXSecureTextField", None), ("AXTextField", "AXSecureTextField")])
def test_secure_fields_never_receive_exact_plaintext(role: str, subrole: str | None) -> None:
    node = _field(role=role, subrole=subrole)
    apply_exact_text(
        [node], [{"window": "Login", "role": role, "title": "Account", "value": "SENTINEL-private"}], "Login"
    )
    assert (node.raw_value, node.exact) == (None, None)


def test_matching_role_without_a_value_does_not_override_a_different_label() -> None:
    node = _field()
    apply_exact_text(
        [node], [{"window": "Doc", "role": "AXTextField", "title": "Other", "value": "SENTINEL-private"}], "Doc"
    )
    assert (node.raw_value, node.exact) == (None, None)


def test_matching_role_and_label_can_recover_an_unreported_value() -> None:
    node = _field()
    apply_exact_text(
        [node], [{"window": "Doc", "role": "AXTextField", "title": "Account", "value": " supplied "}], "Doc"
    )
    assert (node.raw_value, node.exact) == (" supplied ", True)


@pytest.mark.parametrize(
    "windows",
    [
        ["Untitled", "Untitled"],
        ["Untitled ", "Untitled"],
        ["Untitled\u200b", "Untitled"],
        ["Untitled\u00a0", "Untitled"],
        [],
        None,
        "Untitled",
    ],
)
def test_helper_requires_unique_window_metadata_even_if_only_one_field_has_a_value(windows: object) -> None:
    import json

    from beans_picker.observe.exacttext import _parse

    payload = {
        "windows": windows,
        "fields": [{"window": "Untitled", "role": "AXTextField", "title": "Account", "value": "SENTINEL-private"}],
    }
    assert not _parse(json.dumps(payload))


def test_helper_response_without_window_metadata_is_untrusted() -> None:
    from beans_picker.observe.exacttext import _parse

    assert _parse('{"fields":[{"window":"Untitled","role":"AXTextField","value":"private"}]}') is None


def test_helper_unique_window_metadata_preserves_exact_values() -> None:
    import json

    from beans_picker.observe.exacttext import _parse

    fields = [{"window": "Unique", "role": "AXTextField", "value": "  exact  "}]
    assert _parse(json.dumps({"windows": ["Other", "Unique"], "fields": fields})) == fields
