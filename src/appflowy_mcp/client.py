"""HTTP client: thin wrappers over the AppFlowy Cloud REST API.

Base scope: /api/workspace   (auth: Authorization: Bearer <access_token>)

Verified endpoint map (Rust backend src/api/workspace.rs, search.rs;
client reference libs/client-api/src/http_view.rs):

  list_workspaces   GET    /api/workspace
  get_folder        GET    /api/workspace/{ws}/folder
  create_page       POST   /api/workspace/{ws}/page-view
                           body CreatePageParams: parent_view_id, layout, name?, page_data?
  get_page          GET    /api/workspace/{ws}/page-view/{view_id}   (returns encoded collab)
  update_page       PATCH  /api/workspace/{ws}/page-view/{view_id}
                           body UpdatePageParams: name, icon?, is_locked?, extra?
  append_blocks     POST   /api/workspace/{ws}/page-view/{view_id}/append-block
                           body AppendBlockToPageParams: blocks: [<block json>]
  move_page         POST   /api/workspace/{ws}/page-view/{view_id}/move
                           body MovePageParams: new_parent_view_id, prev_view_id?
  trash_page        POST   /api/workspace/{ws}/page-view/{view_id}/move-to-trash   (no body)
  restore_page      POST   /api/workspace/{ws}/page-view/{view_id}/restore-from-trash (no body)
  favorite_page     POST   /api/workspace/{ws}/page-view/{view_id}/favorite
                           body FavoritePageParams: is_favorite, is_pinned
  search_workspace  GET    /api/search/{ws}?query=...

Frozen contract B (mcp-keen-haven.md, "백엔드 검증으로 확정된 사실"):
  - AppResponse envelope is {data, code, message}; success == (code == 0). A
    non-zero code is a failure even on HTTP 200 -> AppFlowyError(message, code).
  - On HTTP 401, refresh the token once and retry the request a single time.
  - page_data for create_page is a single root SerdeBlock
    {"type":"page","data":{},"children":[...]} (see wrap_as_page).
  - append-block body is {"blocks":[<SerdeBlock>,...]} (flat list).
  - trash/restore take no body; favorite takes {is_favorite, is_pinned}.
  - Secrets (tokens) are never embedded in error strings.
"""

from __future__ import annotations

from typing import Any

import httpx


class AppFlowyError(Exception):
    """Raised when the AppFlowy Cloud API reports a failure.

    Carries the backend `message` plus the envelope `code` and/or HTTP `status`
    where available. Never embeds the bearer token.
    """

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


class AppFlowyClient:
    """Async httpx-based client. Attaches bearer token from auth.TokenStore."""

    def __init__(
        self,
        base_url: str,
        token_store: Any,
        *,
        http: httpx.AsyncClient,
        default_workspace_id: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token_store = token_store
        self._http = http
        self._default_workspace_id = default_workspace_id
        self._resolved_workspace_id: str | None = None

    # -- core request ------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        """Perform an authenticated request and return the envelope `data`.

        Parses the {data, code, message} envelope: code != 0 raises
        AppFlowyError. A 401 triggers one token_store.refresh() + single retry.
        Transport errors become AppFlowyError (no token in the message).
        """
        body = _drop_none(json) if json is not None else None
        query = _drop_none(params) if params is not None else None

        resp = await self._send(method, path, json=body, params=query)
        if resp.status_code == 401:
            # Reactive refresh path: token may have expired between proactive
            # checks. Refresh once and retry exactly once.
            await self.token_store.refresh()
            resp = await self._send(method, path, json=body, params=query)

        return self._parse(resp)

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json: dict | None,
        params: dict | None,
    ) -> httpx.Response:
        token = await self.token_store.get_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{self.base_url}{path}"
        try:
            return await self._http.request(
                method, url, json=json, params=params, headers=headers
            )
        except httpx.HTTPError:
            # Do not echo exception text — request metadata may carry the token.
            raise AppFlowyError(f"network error calling {method} {path}")

    def _parse(self, resp: httpx.Response) -> Any:
        try:
            payload = resp.json()
        except ValueError:
            if resp.status_code < 200 or resp.status_code >= 300:
                raise AppFlowyError(
                    f"HTTP {resp.status_code} from AppFlowy", status=resp.status_code
                )
            raise AppFlowyError("invalid (non-JSON) response from AppFlowy")

        # Standard AppResponse envelope.
        if isinstance(payload, dict) and "code" in payload:
            code = payload.get("code")
            if code != 0:
                message = payload.get("message") or f"AppFlowy error (code {code})"
                raise AppFlowyError(message, code=code, status=resp.status_code)
            return payload.get("data")

        # Non-enveloped error (e.g. gateway/HTTP-level failure with JSON body).
        if resp.status_code < 200 or resp.status_code >= 300:
            raise AppFlowyError(
                f"HTTP {resp.status_code} from AppFlowy", status=resp.status_code
            )
        return payload

    # -- workspace resolution ---------------------------------------------

    async def resolve_workspace_id(self, override: str | None = None) -> str:
        """Resolve the workspace id: override -> default -> first workspace.

        The discovered id (from GET /api/workspace) is cached for reuse.
        """
        if override:
            return override
        if self._default_workspace_id:
            return self._default_workspace_id
        if self._resolved_workspace_id:
            return self._resolved_workspace_id

        workspaces = await self.list_workspaces()
        first = _first_workspace_id(workspaces)
        if not first:
            raise AppFlowyError("no workspace available for this account")
        self._resolved_workspace_id = first
        return first

    # -- endpoint wrappers -------------------------------------------------

    async def verify_user(self) -> Any:
        """Provision / sync the cloud user record after a GoTrue sign-in.

        A freshly signed-up GoTrue user has no `af_user` row or default
        workspace until this is called, so `GET /api/workspace` fails with
        "user ... not found in af_user". This mirrors the official client's
        `verify_token_cloud` (GET /api/user/verify/{access_token}); it is
        idempotent for existing users. The access token sits in the path (a
        JWT is path-safe — base64url has no '/').
        """
        token = await self.token_store.get_access_token()
        return await self._request("GET", f"/api/user/verify/{token}")

    async def list_workspaces(self) -> Any:
        return await self._request("GET", "/api/workspace")

    async def get_folder(
        self, *, workspace_id: str | None = None, depth: int | None = None
    ) -> Any:
        ws = await self.resolve_workspace_id(workspace_id)
        return await self._request(
            "GET", f"/api/workspace/{ws}/folder", params={"depth": depth}
        )

    async def create_page(
        self,
        *,
        parent_view_id: str,
        name: str | None = None,
        layout: int = 0,
        page_data: dict | None = None,
        workspace_id: str | None = None,
    ) -> Any:
        ws = await self.resolve_workspace_id(workspace_id)
        body = {
            "parent_view_id": parent_view_id,
            "layout": layout,
            "name": name,
            "page_data": page_data,
        }
        return await self._request(
            "POST", f"/api/workspace/{ws}/page-view", json=body
        )

    async def get_page(
        self, *, view_id: str, workspace_id: str | None = None
    ) -> Any:
        ws = await self.resolve_workspace_id(workspace_id)
        return await self._request(
            "GET", f"/api/workspace/{ws}/page-view/{view_id}"
        )

    async def update_page(
        self,
        *,
        view_id: str,
        name: str,
        icon: dict | None = None,
        is_locked: bool | None = None,
        extra: dict | None = None,
        workspace_id: str | None = None,
    ) -> Any:
        ws = await self.resolve_workspace_id(workspace_id)
        body = {
            "name": name,
            "icon": icon,
            "is_locked": is_locked,
            "extra": extra,
        }
        return await self._request(
            "PATCH", f"/api/workspace/{ws}/page-view/{view_id}", json=body
        )

    async def append_blocks(
        self,
        *,
        view_id: str,
        blocks: list[dict],
        workspace_id: str | None = None,
    ) -> Any:
        ws = await self.resolve_workspace_id(workspace_id)
        return await self._request(
            "POST",
            f"/api/workspace/{ws}/page-view/{view_id}/append-block",
            json={"blocks": blocks},
        )

    async def move_page(
        self,
        *,
        view_id: str,
        new_parent_view_id: str,
        prev_view_id: str | None = None,
        workspace_id: str | None = None,
    ) -> Any:
        ws = await self.resolve_workspace_id(workspace_id)
        body = {
            "new_parent_view_id": new_parent_view_id,
            "prev_view_id": prev_view_id,
        }
        return await self._request(
            "POST", f"/api/workspace/{ws}/page-view/{view_id}/move", json=body
        )

    async def trash_page(
        self, *, view_id: str, workspace_id: str | None = None
    ) -> Any:
        ws = await self.resolve_workspace_id(workspace_id)
        return await self._request(
            "POST", f"/api/workspace/{ws}/page-view/{view_id}/move-to-trash"
        )

    async def restore_page(
        self, *, view_id: str, workspace_id: str | None = None
    ) -> Any:
        ws = await self.resolve_workspace_id(workspace_id)
        return await self._request(
            "POST", f"/api/workspace/{ws}/page-view/{view_id}/restore-from-trash"
        )

    async def favorite_page(
        self,
        *,
        view_id: str,
        is_favorite: bool,
        is_pinned: bool = False,
        workspace_id: str | None = None,
    ) -> Any:
        ws = await self.resolve_workspace_id(workspace_id)
        return await self._request(
            "POST",
            f"/api/workspace/{ws}/page-view/{view_id}/favorite",
            json={"is_favorite": is_favorite, "is_pinned": is_pinned},
        )

    async def search_workspace(
        self,
        *,
        query: str,
        limit: int | None = None,
        workspace_id: str | None = None,
    ) -> Any:
        ws = await self.resolve_workspace_id(workspace_id)
        return await self._request(
            "GET", f"/api/search/{ws}", params={"query": query, "limit": limit}
        )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def wrap_as_page(blocks: list[dict]) -> dict:
        """Wrap a flat block list in the single root page SerdeBlock."""
        return {"type": "page", "data": {}, "children": blocks}


def _drop_none(d: dict) -> dict:
    """Remove keys whose value is None (optional fields omitted from the body)."""
    return {k: v for k, v in d.items() if v is not None}


def _first_workspace_id(workspaces: Any) -> str | None:
    """Extract the first workspace_id from a GET /api/workspace payload."""
    if isinstance(workspaces, list):
        items = workspaces
    elif isinstance(workspaces, dict):
        # Some envelopes nest under a key; be tolerant.
        items = workspaces.get("workspaces") or workspaces.get("items") or []
    else:
        items = []
    for item in items:
        if isinstance(item, dict) and item.get("workspace_id"):
            return item["workspace_id"]
    return None
