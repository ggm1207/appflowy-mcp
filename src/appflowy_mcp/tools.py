"""MCP tool definitions and handlers.

Maps the 11 MVP tools onto AppFlowyClient calls using the low-level
``mcp.server.Server``. Each tool declares a JSON input schema (``@server.list_tools``)
and is dispatched through a single ``@server.call_tool`` handler that returns a
``TextContent`` JSON payload.

Error policy (frozen contract):
  * ``AppFlowyError`` / ``AuthError`` are converted into structured MCP errors
    (``McpError``) whose message reads ``"AppFlowy error (code N): message"`` and
    NEVER contains token or password material.
  * ``create_page`` follows the *create-then-append* ADR: the page is created
    first (``page_data`` omitted → guaranteed empty document), then — if markdown
    was supplied — the converted blocks are appended. If the append step fails the
    created ``view_id`` and the append error are returned together as a structured
    result; the failure is never swallowed and the created page is never lost.

``TOOL_NAMES`` is the single source of truth for the tool list (shared with
server.py and the tests).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

import mcp.types as types
from mcp.server import Server
from mcp.shared.exceptions import McpError

from .auth import AuthError
from .client import AppFlowyClient, AppFlowyError
from .markdown_blocks import markdown_to_blocks

# Tool names (kept here so server.py and tests share one source of truth).
TOOL_NAMES = [
    "list_workspaces",
    "get_folder",
    "create_page",
    "get_page",
    "update_page",
    "append_markdown",
    "move_page",
    "trash_page",
    "restore_page",
    "favorite_page",
    "search_workspace",
]

# --- shared JSON-schema fragments -------------------------------------------

_WORKSPACE_ID_PROP = {
    "workspace_id": {
        "type": "string",
        "description": (
            "Optional workspace id override. Defaults to APPFLOWY_WORKSPACE_ID, "
            "or the first workspace on the account if unset."
        ),
    }
}

_ICON_PROP = {
    "type": "object",
    "description": "ViewIcon: {\"ty\": 0|1|2 (emoji/url/icon), \"value\": str}.",
    "properties": {
        "ty": {"type": "integer", "enum": [0, 1, 2]},
        "value": {"type": "string"},
    },
    "required": ["ty", "value"],
}


def _object_schema(
    properties: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {**properties, **_WORKSPACE_ID_PROP},
    }
    if required:
        schema["required"] = required
    return schema


# --- tool definitions -------------------------------------------------------

TOOLS: list[types.Tool] = [
    types.Tool(
        name="list_workspaces",
        description="List all workspaces available to the authenticated account.",
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="get_folder",
        description=(
            "Get the folder (view tree) of a workspace. This is the primary tool "
            "for reading page structure and discovering view ids."
        ),
        inputSchema=_object_schema(
            {
                "depth": {
                    "type": "integer",
                    "description": "Optional traversal depth of the returned view tree.",
                }
            }
        ),
    ),
    types.Tool(
        name="create_page",
        description=(
            "Create a new page under a parent view, then optionally append markdown "
            "content. The page is always created as an empty document first; if "
            "`markdown` is given it is converted to blocks and appended. If the "
            "append step fails, the created view_id is returned together with the "
            "append error (the page is not lost)."
        ),
        inputSchema=_object_schema(
            {
                "parent_view_id": {
                    "type": "string",
                    "description": "View id of the parent under which to create the page.",
                },
                "name": {"type": "string", "description": "Optional page title."},
                "markdown": {
                    "type": "string",
                    "description": "Optional markdown body appended after creation.",
                },
                "layout": {
                    "type": "integer",
                    "description": "ViewLayout: 0=Document (default), 1=Grid, 2=Board, 3=Calendar, 4=Chat.",
                    "default": 0,
                },
            },
            required=["parent_view_id"],
        ),
    ),
    types.Tool(
        name="get_page",
        description=(
            "Get metadata for a page view by id. Note: the document body is returned "
            "as encoded collab and is not decoded here — use get_folder to read structure."
        ),
        inputSchema=_object_schema(
            {"view_id": {"type": "string", "description": "View id of the page."}},
            required=["view_id"],
        ),
    ),
    types.Tool(
        name="update_page",
        description="Update a page's name and optional icon / lock / extra metadata.",
        inputSchema=_object_schema(
            {
                "view_id": {"type": "string", "description": "View id of the page."},
                "name": {"type": "string", "description": "New page title (required)."},
                "icon": _ICON_PROP,
                "is_locked": {"type": "boolean"},
                "extra": {
                    "type": "object",
                    "description": "Optional extra metadata object.",
                    "additionalProperties": True,
                },
            },
            required=["view_id", "name"],
        ),
    ),
    types.Tool(
        name="append_markdown",
        description=(
            "Convert markdown to AppFlowy blocks and append them to an existing page."
        ),
        inputSchema=_object_schema(
            {
                "view_id": {"type": "string", "description": "View id of the page."},
                "markdown": {
                    "type": "string",
                    "description": "Markdown content to convert and append.",
                },
            },
            required=["view_id", "markdown"],
        ),
    ),
    types.Tool(
        name="move_page",
        description="Move a page under a new parent, optionally after a sibling view.",
        inputSchema=_object_schema(
            {
                "view_id": {"type": "string", "description": "View id to move."},
                "new_parent_view_id": {
                    "type": "string",
                    "description": "View id of the new parent.",
                },
                "prev_view_id": {
                    "type": "string",
                    "description": "Optional sibling view id to position after.",
                },
            },
            required=["view_id", "new_parent_view_id"],
        ),
    ),
    types.Tool(
        name="trash_page",
        description="Move a page to the trash.",
        inputSchema=_object_schema(
            {"view_id": {"type": "string", "description": "View id to trash."}},
            required=["view_id"],
        ),
    ),
    types.Tool(
        name="restore_page",
        description="Restore a page from the trash.",
        inputSchema=_object_schema(
            {"view_id": {"type": "string", "description": "View id to restore."}},
            required=["view_id"],
        ),
    ),
    types.Tool(
        name="favorite_page",
        description="Favorite or unfavorite a page (optionally pin it).",
        inputSchema=_object_schema(
            {
                "view_id": {"type": "string", "description": "View id of the page."},
                "is_favorite": {
                    "type": "boolean",
                    "description": "True to favorite, False to unfavorite.",
                },
                "is_pinned": {
                    "type": "boolean",
                    "description": "Optional pin flag (default False).",
                    "default": False,
                },
            },
            required=["view_id", "is_favorite"],
        ),
    ),
    types.Tool(
        name="search_workspace",
        description=(
            "Full-text search within a workspace. Note: results depend on the "
            "embedding indexer; freshly created documents may not be searchable "
            "immediately."
        ),
        inputSchema=_object_schema(
            {
                "query": {"type": "string", "description": "Search query."},
                "limit": {
                    "type": "integer",
                    "description": "Optional maximum number of results.",
                },
            },
            required=["query"],
        ),
    ),
]

# Sanity: TOOLS and TOOL_NAMES must stay in lock-step.
assert [t.name for t in TOOLS] == TOOL_NAMES, "TOOLS and TOOL_NAMES are out of sync"


# --- result / error helpers -------------------------------------------------


def _text(obj: Any) -> list[types.TextContent]:
    """Serialise a result object to a single JSON TextContent block."""
    return [
        types.TextContent(
            type="text",
            text=json.dumps(obj, ensure_ascii=False, indent=2),
        )
    ]


def _appflowy_error_message(exc: AppFlowyError) -> str:
    """Render an AppFlowyError as a safe, structured message (no secrets)."""
    if exc.code is not None:
        return f"AppFlowy error (code {exc.code}): {exc.message}"
    if exc.status is not None:
        return f"AppFlowy error (HTTP {exc.status}): {exc.message}"
    return f"AppFlowy error: {exc.message}"


def _extract_view_id(data: Any) -> str | None:
    """Best-effort extraction of a created page's view id from the response."""
    if isinstance(data, dict):
        for key in ("view_id", "id"):
            value = data.get(key)
            if value:
                return str(value)
        view = data.get("view")
        if isinstance(view, dict):
            for key in ("view_id", "id"):
                value = view.get(key)
                if value:
                    return str(value)
    return None


# --- per-tool handlers ------------------------------------------------------
#
# Each handler resolves the workspace id up-front (so create-then-append targets
# one workspace) and returns a TextContent JSON payload. AppFlowyError / AuthError
# propagate to the central dispatcher which converts them to McpError.


async def _list_workspaces(client: AppFlowyClient, args: dict) -> list[types.TextContent]:
    return _text(await client.list_workspaces())


async def _get_folder(client: AppFlowyClient, args: dict) -> list[types.TextContent]:
    ws = await client.resolve_workspace_id(args.get("workspace_id"))
    return _text(await client.get_folder(workspace_id=ws, depth=args.get("depth")))


async def _create_page(client: AppFlowyClient, args: dict) -> list[types.TextContent]:
    ws = await client.resolve_workspace_id(args.get("workspace_id"))
    # page_data omitted -> backend guarantees an empty document (safe path).
    created = await client.create_page(
        parent_view_id=args["parent_view_id"],
        name=args.get("name"),
        layout=args.get("layout", 0),
        workspace_id=ws,
    )
    view_id = _extract_view_id(created)
    result: dict[str, Any] = {"view_id": view_id, "created": created}

    markdown = args.get("markdown")
    if markdown:
        if not view_id:
            # Created but could not determine where to append: surface, don't swallow.
            result["appended"] = False
            result["append_error"] = (
                "page was created but no view_id was returned; cannot append markdown"
            )
            return _text(result)
        blocks = markdown_to_blocks(markdown)
        if not blocks:
            result["appended"] = False
            result["blocks"] = 0
            result["note"] = "markdown produced no blocks; nothing appended"
            return _text(result)
        try:
            append_result = await client.append_blocks(
                view_id=view_id, blocks=blocks, workspace_id=ws
            )
            result["appended"] = True
            result["blocks"] = len(blocks)
            result["append_result"] = append_result
        except AppFlowyError as exc:
            # Critical: do NOT swallow and do NOT lose the created page. Return a
            # structured result carrying the view_id alongside the append error.
            result["appended"] = False
            result["blocks"] = len(blocks)
            result["append_error"] = _appflowy_error_message(exc)
    return _text(result)


async def _get_page(client: AppFlowyClient, args: dict) -> list[types.TextContent]:
    ws = await client.resolve_workspace_id(args.get("workspace_id"))
    return _text(await client.get_page(view_id=args["view_id"], workspace_id=ws))


async def _update_page(client: AppFlowyClient, args: dict) -> list[types.TextContent]:
    ws = await client.resolve_workspace_id(args.get("workspace_id"))
    data = await client.update_page(
        view_id=args["view_id"],
        name=args["name"],
        icon=args.get("icon"),
        is_locked=args.get("is_locked"),
        extra=args.get("extra"),
        workspace_id=ws,
    )
    return _text({"ok": True, "view_id": args["view_id"], "result": data})


async def _append_markdown(client: AppFlowyClient, args: dict) -> list[types.TextContent]:
    ws = await client.resolve_workspace_id(args.get("workspace_id"))
    blocks = markdown_to_blocks(args["markdown"])
    if not blocks:
        return _text(
            {
                "view_id": args["view_id"],
                "appended": False,
                "blocks": 0,
                "note": "markdown produced no blocks; nothing appended",
            }
        )
    data = await client.append_blocks(
        view_id=args["view_id"], blocks=blocks, workspace_id=ws
    )
    return _text(
        {
            "view_id": args["view_id"],
            "appended": True,
            "blocks": len(blocks),
            "result": data,
        }
    )


async def _move_page(client: AppFlowyClient, args: dict) -> list[types.TextContent]:
    ws = await client.resolve_workspace_id(args.get("workspace_id"))
    data = await client.move_page(
        view_id=args["view_id"],
        new_parent_view_id=args["new_parent_view_id"],
        prev_view_id=args.get("prev_view_id"),
        workspace_id=ws,
    )
    return _text({"ok": True, "view_id": args["view_id"], "result": data})


async def _trash_page(client: AppFlowyClient, args: dict) -> list[types.TextContent]:
    ws = await client.resolve_workspace_id(args.get("workspace_id"))
    data = await client.trash_page(view_id=args["view_id"], workspace_id=ws)
    return _text({"ok": True, "view_id": args["view_id"], "trashed": True, "result": data})


async def _restore_page(client: AppFlowyClient, args: dict) -> list[types.TextContent]:
    ws = await client.resolve_workspace_id(args.get("workspace_id"))
    data = await client.restore_page(view_id=args["view_id"], workspace_id=ws)
    return _text(
        {"ok": True, "view_id": args["view_id"], "restored": True, "result": data}
    )


async def _favorite_page(client: AppFlowyClient, args: dict) -> list[types.TextContent]:
    ws = await client.resolve_workspace_id(args.get("workspace_id"))
    data = await client.favorite_page(
        view_id=args["view_id"],
        is_favorite=args["is_favorite"],
        is_pinned=args.get("is_pinned", False),
        workspace_id=ws,
    )
    return _text({"ok": True, "view_id": args["view_id"], "result": data})


async def _search_workspace(client: AppFlowyClient, args: dict) -> list[types.TextContent]:
    ws = await client.resolve_workspace_id(args.get("workspace_id"))
    return _text(
        await client.search_workspace(
            query=args["query"], limit=args.get("limit"), workspace_id=ws
        )
    )


_Handler = Callable[[AppFlowyClient, dict], Awaitable[list[types.TextContent]]]

_HANDLERS: dict[str, _Handler] = {
    "list_workspaces": _list_workspaces,
    "get_folder": _get_folder,
    "create_page": _create_page,
    "get_page": _get_page,
    "update_page": _update_page,
    "append_markdown": _append_markdown,
    "move_page": _move_page,
    "trash_page": _trash_page,
    "restore_page": _restore_page,
    "favorite_page": _favorite_page,
    "search_workspace": _search_workspace,
}

# Sanity: every declared tool has a handler and vice versa.
assert set(_HANDLERS) == set(TOOL_NAMES), "handlers and TOOL_NAMES are out of sync"


# --- server construction ----------------------------------------------------


def build_server(
    client: AppFlowyClient,
    *,
    ensure_signed_in: Callable[[], Awaitable[None]] | None = None,
) -> Server:
    """Build a low-level MCP Server wired to the given AppFlowyClient.

    ``ensure_signed_in`` (optional) is awaited before each tool call to support
    lazy authentication: no network sign-in happens at startup; the first tool
    call triggers it, and later calls are cheap no-ops.
    """
    server: Server = Server("appflowy-mcp")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return TOOLS

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
        args = arguments or {}
        handler = _HANDLERS.get(name)
        if handler is None:
            raise McpError(
                types.ErrorData(
                    code=types.METHOD_NOT_FOUND, message=f"unknown tool: {name}"
                )
            )
        try:
            if ensure_signed_in is not None:
                await ensure_signed_in()
            return await handler(client, args)
        except AppFlowyError as exc:
            raise McpError(
                types.ErrorData(
                    code=types.INTERNAL_ERROR, message=_appflowy_error_message(exc)
                )
            ) from None
        except AuthError as exc:
            # AuthError messages are already secret-safe (see auth.py).
            raise McpError(
                types.ErrorData(
                    code=types.INTERNAL_ERROR, message=f"Authentication error: {exc}"
                )
            ) from None
        except KeyError as exc:
            # A required argument was missing despite schema validation.
            raise McpError(
                types.ErrorData(
                    code=types.INVALID_PARAMS,
                    message=f"missing required argument: {exc}",
                )
            ) from None

    return server
