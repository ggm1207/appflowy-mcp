"""Markdown -> AppFlowy document blocks converter (the core "translator").

AppFlowy's append-block / create-page page_data expect document blocks, NOT raw
markdown. Block schema (Rust SerdeBlock, libs/workspace-template/src/document/parser.rs):

    {"type": <str>, "data": {<...>}, "children": [<block>, ...]}

Block types and their `data` (examples from
libs/workspace-template/assets/initial_document.json and
tests/workspace/page_view.rs):

    paragraph      {"delta": [...]}
    heading        {"delta": [...], "level": 1|2|3}
    bulleted_list  {"delta": [...]}
    numbered_list  {"delta": [...]}
    todo_list      {"delta": [...], "checked": bool}
    code           {"language": <str>, "delta": [...]}
    quote          {"delta": [...]}
    divider        (no data key at all)

Inline "delta" is Quill-compatible:

    {"insert": "text", "attributes": {"bold": true, "italic": true, "code": true,
                                       "href": "...", "strikethrough": true,
                                       "underline": true}}

This module returns a FLAT list of blocks (frozen contract A). Every block carries
``"children": []`` — nested structures (nested lists, tables, images) are flattened
or dropped, not represented as children.

Limitations (frozen contract A — intentionally unsupported in the MVP):
  * Nested lists are flattened to one block per item (depth is lost).
  * Tables and images are not converted.
  * ``underline`` has no CommonMark syntax, so it is never emitted (the key is
    documented for completeness only).
"""

from __future__ import annotations

import re
from typing import Any

from markdown_it import MarkdownIt

# CommonMark + strikethrough (~~s~~). Task lists are detected manually below
# rather than via a plugin, so the [ ] / [x] handling stays under our control.
_MD = MarkdownIt("commonmark").enable("strikethrough")

# Leading task-list marker on a list item, e.g. "[ ] " or "[x] ".
_TODO_RE = re.compile(r"^\[([ xX])\]\s+")


def _make_op(text: str, attributes: dict[str, Any]) -> dict[str, Any]:
    """Build a single Quill delta op, omitting an empty ``attributes`` key."""
    op: dict[str, Any] = {"insert": text}
    if attributes:
        op["attributes"] = dict(attributes)
    return op


def _inline_to_delta(children: list[Any] | None) -> list[dict[str, Any]]:
    """Convert an inline token's children into a list of Quill delta ops.

    Attribute keys are exactly: bold / italic / code / href / strikethrough.
    (underline has no markdown syntax and is never produced.)
    """
    if not children:
        return []

    ops: list[dict[str, Any]] = []
    active: dict[str, Any] = {}

    for child in children:
        ctype = child.type
        if ctype == "text":
            if child.content == "":
                continue
            ops.append(_make_op(child.content, active))
        elif ctype in ("softbreak", "hardbreak"):
            ops.append({"insert": "\n"})
        elif ctype == "code_inline":
            attrs = dict(active)
            attrs["code"] = True
            ops.append(_make_op(child.content, attrs))
        elif ctype == "strong_open":
            active["bold"] = True
        elif ctype == "strong_close":
            active.pop("bold", None)
        elif ctype == "em_open":
            active["italic"] = True
        elif ctype == "em_close":
            active.pop("italic", None)
        elif ctype == "s_open":
            active["strikethrough"] = True
        elif ctype == "s_close":
            active.pop("strikethrough", None)
        elif ctype == "link_open":
            active["href"] = child.attrGet("href")
        elif ctype == "link_close":
            active.pop("href", None)
        # image / other inline tokens: intentionally ignored (MVP).

    return ops


def _block(btype: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"type": btype, "data": data, "children": []}


def _list_item_block(
    list_type: str, inline: Any
) -> dict[str, Any]:
    """Build a list block, detecting ``[ ]`` / ``[x]`` task-list items.

    A task item becomes a ``todo_list`` block with ``checked``; the checkbox
    marker is stripped from the resulting delta.
    """
    delta = _inline_to_delta(inline.children if inline is not None else None)
    content = inline.content if inline is not None else ""
    match = _TODO_RE.match(content)
    if match:
        checked = match.group(1).lower() == "x"
        if delta and "insert" in delta[0]:
            delta[0]["insert"] = _TODO_RE.sub("", delta[0]["insert"], count=1)
            first = delta[0]
            if first["insert"] == "" and "attributes" not in first:
                delta.pop(0)
        return _block("todo_list", {"delta": delta, "checked": checked})
    return _block(list_type, {"delta": delta})


def markdown_to_blocks(text: str) -> list[dict[str, Any]]:
    """Convert a markdown string into a flat list of AppFlowy block dicts.

    Returns ``[]`` for empty / whitespace-only input.
    """
    if not text:
        return []

    tokens = _MD.parse(text)
    blocks: list[dict[str, Any]] = []
    # Container context stack: "bulleted_list" | "numbered_list" | "quote".
    container_stack: list[str] = []

    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        ttype = tok.type

        if ttype == "heading_open":
            level = int(tok.tag[1:])
            level = max(1, min(3, level))  # clamp h4-h6 -> 3
            inline = tokens[i + 1]
            blocks.append(
                _block("heading", {"delta": _inline_to_delta(inline.children), "level": level})
            )
            i += 3  # heading_open, inline, heading_close
            continue

        if ttype == "paragraph_open":
            inline = tokens[i + 1]
            top = container_stack[-1] if container_stack else None
            if top in ("bulleted_list", "numbered_list"):
                blocks.append(_list_item_block(top, inline))
            elif top == "quote":
                blocks.append(_block("quote", {"delta": _inline_to_delta(inline.children)}))
            else:
                blocks.append(_block("paragraph", {"delta": _inline_to_delta(inline.children)}))
            i += 3  # paragraph_open, inline, paragraph_close
            continue

        if ttype in ("fence", "code_block"):
            content = tok.content
            if content.endswith("\n"):
                content = content[:-1]
            language = tok.info.strip() if tok.info else ""
            blocks.append(
                _block("code", {"language": language, "delta": [{"insert": content}]})
            )
            i += 1
            continue

        if ttype == "hr":
            # divider has NO data key and NO delta.
            blocks.append({"type": "divider", "children": []})
            i += 1
            continue

        if ttype == "bullet_list_open":
            container_stack.append("bulleted_list")
        elif ttype == "ordered_list_open":
            container_stack.append("numbered_list")
        elif ttype == "blockquote_open":
            container_stack.append("quote")
        elif ttype in ("bullet_list_close", "ordered_list_close", "blockquote_close"):
            if container_stack:
                container_stack.pop()

        i += 1

    return blocks
