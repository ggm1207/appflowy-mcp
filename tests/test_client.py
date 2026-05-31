"""Offline tests for client.AppFlowyClient (httpx.MockTransport, no network).

Covers frozen contract B: AppResponse envelope parsing (code != 0 ->
AppFlowyError), 401 -> refresh -> single retry, workspace-id resolution
fallback, optional-None body omission, and no-secret-leak.

Tests drive coroutines via asyncio.run() (no pytest-asyncio dependency).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from appflowy_mcp.client import AppFlowyClient, AppFlowyError

BASE = "http://appflowy.test"


class FakeTokenStore:
    """Minimal TokenStore double honoring the async get_access_token/refresh API."""

    def __init__(self, token="tok-secret-123"):
        self.token = token
        self.refresh_calls = 0

    async def get_access_token(self) -> str:
        return self.token

    async def refresh(self) -> None:
        self.refresh_calls += 1
        self.token = "tok-refreshed-456"


def _client(handler, token_store=None, **kwargs) -> AppFlowyClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return AppFlowyClient(
        BASE, token_store or FakeTokenStore(), http=http, **kwargs
    )


def _ok(data):
    return httpx.Response(200, json={"data": data, "code": 0, "message": ""})


def test_envelope_success_returns_data():
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok({"hello": "world"})

    async def run():
        client = _client(handler, default_workspace_id="ws-1")
        data = await client.get_page(view_id="v1")
        assert data == {"hello": "world"}

    asyncio.run(run())


def test_nonzero_code_raises_appflowy_error():
    def handler(request: httpx.Request) -> httpx.Response:
        # HTTP 200 but envelope code != 0 -> failure with message surfaced.
        return httpx.Response(
            200, json={"data": None, "code": 1004, "message": "record not found"}
        )

    async def run():
        client = _client(handler, default_workspace_id="ws-1")
        with pytest.raises(AppFlowyError) as exc:
            await client.get_page(view_id="missing")
        assert exc.value.code == 1004
        assert "record not found" in str(exc.value)

    asyncio.run(run())


def test_401_triggers_refresh_then_single_retry():
    state = {"hits": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["hits"] += 1
        if state["hits"] == 1:
            assert request.headers["Authorization"] == "Bearer tok-secret-123"
            return httpx.Response(401, json={"code": 401, "message": "unauthorized"})
        # Retry should carry the refreshed token.
        assert request.headers["Authorization"] == "Bearer tok-refreshed-456"
        return _ok({"ok": True})

    async def run():
        ts = FakeTokenStore()
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = AppFlowyClient(BASE, ts, http=http, default_workspace_id="ws-1")
        data = await client.get_page(view_id="v1")
        assert data == {"ok": True}
        assert ts.refresh_calls == 1
        assert state["hits"] == 2  # exactly one retry

    asyncio.run(run())


def test_resolve_workspace_id_override_and_default_and_fallback():
    def handler(request: httpx.Request) -> httpx.Response:
        # GET /api/workspace fallback list.
        if request.url.path == "/api/workspace":
            return _ok([{"workspace_id": "ws-from-list", "workspace_name": "W"}])
        return _ok({})

    async def run():
        # override wins
        c1 = _client(handler)
        assert await c1.resolve_workspace_id("ws-override") == "ws-override"

        # default used when no override
        c2 = _client(handler, default_workspace_id="ws-default")
        assert await c2.resolve_workspace_id() == "ws-default"

        # fallback to GET /api/workspace first id, then cached
        c3 = _client(handler)
        assert await c3.resolve_workspace_id() == "ws-from-list"
        assert c3._resolved_workspace_id == "ws-from-list"

    asyncio.run(run())


def test_optional_none_fields_omitted_from_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen["body"] = _json.loads(request.content)
        return _ok({"view_id": "new-1"})

    async def run():
        client = _client(handler, default_workspace_id="ws-1")
        # name/page_data omitted -> should not appear in body; layout default 0.
        await client.create_page(parent_view_id="parent-1")
        body = seen["body"]
        assert body == {"parent_view_id": "parent-1", "layout": 0}
        assert "name" not in body and "page_data" not in body

    asyncio.run(run())


def test_append_blocks_wraps_in_blocks_key():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen["body"] = _json.loads(request.content)
        assert request.url.path.endswith("/append-block")
        return _ok({"ok": True})

    async def run():
        client = _client(handler, default_workspace_id="ws-1")
        blocks = [{"type": "paragraph", "data": {"delta": []}, "children": []}]
        await client.append_blocks(view_id="v1", blocks=blocks)
        assert seen["body"] == {"blocks": blocks}

    asyncio.run(run())


def test_trash_and_restore_send_no_body():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.content))
        return _ok({"ok": True})

    async def run():
        client = _client(handler, default_workspace_id="ws-1")
        await client.trash_page(view_id="v1")
        await client.restore_page(view_id="v1")
        for path, content in seen:
            assert content in (b"", b"null") or content == b""

    asyncio.run(run())


def test_wrap_as_page_shape():
    blocks = [{"type": "heading", "data": {"delta": [], "level": 1}, "children": []}]
    page = AppFlowyClient.wrap_as_page(blocks)
    assert page == {"type": "page", "data": {}, "children": blocks}


def test_network_error_no_secret_leak():
    secret = "tok-secret-123"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed connecting with header Bearer {secret}")

    async def run():
        client = _client(handler, FakeTokenStore(secret), default_workspace_id="ws-1")
        with pytest.raises(AppFlowyError) as exc:
            await client.get_page(view_id="v1")
        assert secret not in str(exc.value)

    asyncio.run(run())


def test_search_workspace_query_params():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        seen["path"] = request.url.path
        return _ok([])

    async def run():
        client = _client(handler, default_workspace_id="ws-1")
        await client.search_workspace(query="hello", limit=5)
        assert seen["path"] == "/api/search/ws-1"
        assert seen["params"]["query"] == "hello"
        assert seen["params"]["limit"] == "5"
        # limit None should be omitted
        await client.search_workspace(query="x")
        assert "limit" not in seen["params"]

    asyncio.run(run())
