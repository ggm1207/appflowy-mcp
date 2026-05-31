"""Authentication: obtain and refresh an AppFlowy Cloud access token.

AppFlowy Cloud uses GoTrue. Token acquisition (password grant):

    POST {GOTRUE_URL}/token?grant_type=password
    body: {"email": ..., "password": ...}
    -> {"access_token", "refresh_token", "expires_in", ...}

Refresh:

    POST {GOTRUE_URL}/token?grant_type=refresh_token
    body: {"refresh_token": ...}

All subsequent /api/* calls send:  Authorization: Bearer <access_token>

(Verified against the Rust backend: libs/gotrue/src/api.rs, libs/gotrue/src/grant.rs.)

Frozen contract B (mcp-keen-haven.md):
  - get_access_token is ASYNC (refresh performs I/O) and single-flight via an
    asyncio.Lock so concurrent callers trigger at most one refresh.
  - Proactive refresh when within `refresh_skew_seconds` of expiry, plus a
    reactive path (the client retries once on a 401 after calling refresh()).
  - Secrets (password, access/refresh tokens) are NEVER placed in log or error
    strings.
"""

from __future__ import annotations

import asyncio
import time

import httpx


class AuthError(Exception):
    """Raised when sign-in or token refresh fails.

    The message is intentionally generic — it must never embed credentials or
    token material (no-secret-leak contract).
    """


class TokenStore:
    """Holds the current access/refresh token and refreshes transparently.

    Usage:
        store = TokenStore(gotrue_url, http=client)
        await store.sign_in(email, password)
        token = await store.get_access_token()   # refreshes if near expiry
    """

    def __init__(
        self,
        gotrue_url: str,
        *,
        http: httpx.AsyncClient,
        refresh_skew_seconds: int = 60,
    ) -> None:
        self.gotrue_url = gotrue_url.rstrip("/")
        self._http = http
        self._refresh_skew = refresh_skew_seconds
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def is_authenticated(self) -> bool:
        return self._access_token is not None

    async def sign_in(self, email: str, password: str) -> None:
        """Obtain tokens via the password grant.

        On any non-2xx response, transport error, or missing token, raise
        AuthError without leaking the password or response body.
        """
        data = await self._post_token(
            params={"grant_type": "password"},
            body={"email": email, "password": password},
            context="sign-in",
        )
        self._store_tokens(data, context="sign-in")

    async def refresh(self) -> None:
        """Refresh tokens via the refresh_token grant.

        Raises AuthError if there is no refresh token or the grant fails.
        """
        if not self._refresh_token:
            raise AuthError("cannot refresh: not signed in")
        data = await self._post_token(
            params={"grant_type": "refresh_token"},
            body={"refresh_token": self._refresh_token},
            context="token refresh",
        )
        self._store_tokens(data, context="token refresh")

    async def get_access_token(self) -> str:
        """Return a valid access token, refreshing proactively if near expiry.

        Single-flight: the asyncio.Lock serialises concurrent callers so that a
        burst of requests during an expiry window triggers exactly one refresh —
        callers after the first see the freshly stored token and skip refresh.
        """
        async with self._lock:
            if self._access_token is None:
                raise AuthError("not authenticated: call sign_in() first")
            if self._is_near_expiry():
                await self.refresh()
            return self._access_token  # type: ignore[return-value]

    # -- internals ---------------------------------------------------------

    def _is_near_expiry(self) -> bool:
        return time.time() >= (self._expires_at - self._refresh_skew)

    async def _post_token(
        self,
        *,
        params: dict[str, str],
        body: dict[str, str],
        context: str,
    ) -> dict:
        url = f"{self.gotrue_url}/token"
        try:
            resp = await self._http.post(url, params=params, json=body)
        except httpx.HTTPError:
            # Never echo the underlying exception text — request bodies/URLs may
            # carry credentials. Surface a generic, safe message instead.
            raise AuthError(f"{context} failed: network error contacting GoTrue")
        if resp.status_code < 200 or resp.status_code >= 300:
            raise AuthError(f"{context} failed: HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError:
            raise AuthError(f"{context} failed: invalid response from GoTrue")

    def _store_tokens(self, data: dict, *, context: str) -> None:
        access = data.get("access_token")
        refresh = data.get("refresh_token")
        if not access or not refresh:
            raise AuthError(f"{context} failed: token missing in response")
        expires_in = data.get("expires_in", 0)
        try:
            expires_in = float(expires_in)
        except (TypeError, ValueError):
            expires_in = 0.0
        self._access_token = access
        self._refresh_token = refresh
        self._expires_at = time.time() + expires_in
