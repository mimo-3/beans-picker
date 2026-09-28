"""A web page's list as cua-driver hands it over: one flat run of elements under the web area."""

from __future__ import annotations

from beans_picker._json import JsonObject, JsonValue


def web_list(tokens: str, rows: list[tuple[str, str]]) -> JsonObject:
    """Each row is a heading, a status text and an unnamed menu button, all siblings."""
    elements: list[JsonValue] = [
        {"element_index": 0, "element_token": f"{tokens}:0", "role": "AXWindow", "label": "Positions", "depth": 0},
        {"element_index": 1, "element_token": f"{tokens}:1", "role": "AXWebArea", "parent_index": 0, "depth": 1},
    ]
    md = ['- [0] AXWindow "Positions"', '  - [1] AXWebArea "Positions"']
    i = 2
    for title, status in rows:
        elements += [
            {
                "element_index": i,
                "element_token": f"{tokens}:{i}",
                "role": "AXHeading",
                "label": title,
                "parent_index": 1,
                "depth": 2,
            },
            {
                "element_index": i + 1,
                "element_token": f"{tokens}:{i + 1}",
                "role": "AXPopUpButton",
                "parent_index": 1,
                "depth": 2,
            },
        ]
        md += [
            f'    - [{i}] AXHeading "{title}"',
            f'      - AXStaticText = "{title}"',
            f'    - AXStaticText = "{status}"',
            f"    - [{i + 1}] AXPopUpButton [actions=[press]]",
        ]
        i += 2
    return {"elements": elements, "tree_markdown": "\n".join(md), "window_title": "Positions"}
