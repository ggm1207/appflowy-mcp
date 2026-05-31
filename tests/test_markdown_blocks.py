"""Golden tests for markdown_to_blocks (frozen contract A).

These 16 parametrized cases ARE the contract: each asserts the full block-dict
output of ``markdown_to_blocks`` so any drift in shape, keys, delta attributes,
or block ordering is caught.

Invariants exercised:
  * every block carries ``"children": []``
  * text blocks carry ``data.delta``; ``divider`` has NO ``data`` key and NO delta
  * lists flatten to one block per item; ``[ ]`` / ``[x]`` -> todo_list + checked
  * delta attribute keys are exactly bold / italic / code / href / strikethrough
  * empty attribute set -> the ``attributes`` key is omitted
  * "" (and whitespace-only) -> []
"""

import pytest

from appflowy_mcp.markdown_blocks import markdown_to_blocks


# (id, markdown_input, expected_blocks)
CASES = [
    # 1 — plain paragraph
    (
        "paragraph",
        "hello world",
        [
            {
                "type": "paragraph",
                "data": {"delta": [{"insert": "hello world"}]},
                "children": [],
            }
        ],
    ),
    # 2a — heading h1
    (
        "heading_h1",
        "# Title",
        [
            {
                "type": "heading",
                "data": {"delta": [{"insert": "Title"}], "level": 1},
                "children": [],
            }
        ],
    ),
    # 2b — heading h2
    (
        "heading_h2",
        "## Title",
        [
            {
                "type": "heading",
                "data": {"delta": [{"insert": "Title"}], "level": 2},
                "children": [],
            }
        ],
    ),
    # 2c — heading h3 (and h5 clamps down to 3)
    (
        "heading_h5_clamped",
        "##### Deep",
        [
            {
                "type": "heading",
                "data": {"delta": [{"insert": "Deep"}], "level": 3},
                "children": [],
            }
        ],
    ),
    # 3 — bulleted list: one block per item
    (
        "bulleted_list",
        "- a\n- b",
        [
            {"type": "bulleted_list", "data": {"delta": [{"insert": "a"}]}, "children": []},
            {"type": "bulleted_list", "data": {"delta": [{"insert": "b"}]}, "children": []},
        ],
    ),
    # 4 — numbered list: one block per item
    (
        "numbered_list",
        "1. x\n2. y",
        [
            {"type": "numbered_list", "data": {"delta": [{"insert": "x"}]}, "children": []},
            {"type": "numbered_list", "data": {"delta": [{"insert": "y"}]}, "children": []},
        ],
    ),
    # 5 — todo list: [ ] unchecked / [x] checked, marker stripped
    (
        "todo_list",
        "- [ ] todo\n- [x] done",
        [
            {
                "type": "todo_list",
                "data": {"delta": [{"insert": "todo"}], "checked": False},
                "children": [],
            },
            {
                "type": "todo_list",
                "data": {"delta": [{"insert": "done"}], "checked": True},
                "children": [],
            },
        ],
    ),
    # 6 — fenced code with language; trailing newline trimmed
    (
        "code_with_language",
        "```python\nprint(1)\n```",
        [
            {
                "type": "code",
                "data": {"language": "python", "delta": [{"insert": "print(1)"}]},
                "children": [],
            }
        ],
    ),
    # 7 — blockquote
    (
        "quote",
        "> hi there",
        [
            {
                "type": "quote",
                "data": {"delta": [{"insert": "hi there"}]},
                "children": [],
            }
        ],
    ),
    # 8 — divider: NO data key, NO delta
    (
        "divider",
        "---",
        [{"type": "divider", "children": []}],
    ),
    # 9 — bold inline
    (
        "bold",
        "**b**",
        [
            {
                "type": "paragraph",
                "data": {"delta": [{"insert": "b", "attributes": {"bold": True}}]},
                "children": [],
            }
        ],
    ),
    # 10 — italic inline
    (
        "italic",
        "*i*",
        [
            {
                "type": "paragraph",
                "data": {"delta": [{"insert": "i", "attributes": {"italic": True}}]},
                "children": [],
            }
        ],
    ),
    # 11 — inline code
    (
        "code_inline",
        "`c`",
        [
            {
                "type": "paragraph",
                "data": {"delta": [{"insert": "c", "attributes": {"code": True}}]},
                "children": [],
            }
        ],
    ),
    # 12 — link -> href attribute
    (
        "link_href",
        "[t](u)",
        [
            {
                "type": "paragraph",
                "data": {"delta": [{"insert": "t", "attributes": {"href": "u"}}]},
                "children": [],
            }
        ],
    ),
    # 13 — strikethrough
    (
        "strikethrough",
        "~~s~~",
        [
            {
                "type": "paragraph",
                "data": {"delta": [{"insert": "s", "attributes": {"strikethrough": True}}]},
                "children": [],
            }
        ],
    ),
    # 14 — mixed inline within one paragraph (order + separate ops preserved)
    (
        "mixed_inline",
        "**b** *i* `c`",
        [
            {
                "type": "paragraph",
                "data": {
                    "delta": [
                        {"insert": "b", "attributes": {"bold": True}},
                        {"insert": " "},
                        {"insert": "i", "attributes": {"italic": True}},
                        {"insert": " "},
                        {"insert": "c", "attributes": {"code": True}},
                    ]
                },
                "children": [],
            }
        ],
    ),
    # 15 — empty input -> []
    (
        "empty",
        "",
        [],
    ),
    # 16 — multi-block document: ordering and length
    (
        "multi_block",
        "# Title\n\nbody\n\n- item\n\n---",
        [
            {
                "type": "heading",
                "data": {"delta": [{"insert": "Title"}], "level": 1},
                "children": [],
            },
            {
                "type": "paragraph",
                "data": {"delta": [{"insert": "body"}]},
                "children": [],
            },
            {
                "type": "bulleted_list",
                "data": {"delta": [{"insert": "item"}]},
                "children": [],
            },
            {"type": "divider", "children": []},
        ],
    ),
]


@pytest.mark.parametrize(
    "markdown, expected",
    [pytest.param(md, expected, id=case_id) for case_id, md, expected in CASES],
)
def test_markdown_to_blocks(markdown, expected):
    assert markdown_to_blocks(markdown) == expected


def test_divider_has_no_data_key():
    """divider must not carry a data/delta key (backend removes delta for it)."""
    block = markdown_to_blocks("---")[0]
    assert "data" not in block
    assert block == {"type": "divider", "children": []}


def test_every_block_has_empty_children():
    blocks = markdown_to_blocks("# H\n\ntext\n\n- a\n- b\n\n> q\n\n---")
    assert blocks, "expected some blocks"
    assert all(b["children"] == [] for b in blocks)
