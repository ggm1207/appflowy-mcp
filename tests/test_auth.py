"""Offline tests for auth.TokenStore (httpx.MockTransport, no network).

Covers frozen contract B: token storage on sign-in, proactive refresh near
expiry, single-flight refresh under concurrency, and AuthError on failure +
no-secret-leak.

Tests are plain sync functions driving coroutines via asyncio.run() so no
pytest-asyncio plugin is required (offline-friendly).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from appflowy_mcp.auth import AuthError, TokenStore

GOTRUE = "http://gotrue.test/gotrue"


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_sign_in_stores_tokens():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("grant_type") == "password"
        return httpx.Response(
            200,
            json={
                "access_token": "acc-1",
                "refresh_token": "ref-1",
                "expires_in": 3600,
            },
        )

    async def run():
        async with _client(handler) as http:
            store = TokenStore(GOTRUE, http=http)
            assert store.is_authenticated is False
            await store.sign_in("a@b.com", "secret-pw")
            assert store.is_authenticated is True
            assert await store.get_access_token() == "acc-1"

    asyncio.run(run())


def test_proactive_refresh_near_expiry():
    calls = {"password": 0, "refresh_token": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        grant = request.url.params.get("grant_type")
        calls[grant] += 1
        if grant == "password":
            return httpx.Response(
                200,
                json={
                    "access_token": "acc-old",
                    "refresh_token": "ref-old",
                    "expires_in": 10,  # within default 60s skew -> near expiry
                },
            )
        return httpx.Response(
            200,
            json={
                "access_token": "acc-new",
                "refresh_token": "ref-new",
                "expires_in": 3600,
            },
        )

    async def run():
        async with _client(handler) as http:
            store = TokenStore(GOTRUE, http=http, refresh_skew_seconds=60)
            await store.sign_in("a@b.com", "pw")
            token = await store.get_access_token()
            assert token == "acc-new"
            assert calls["refresh_token"] == 1

    asyncio.run(run())


def test_single_flight_refresh_under_concurrency():
    calls = {"refresh_token": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        grant = request.url.params.get("grant_type")
        if grant == "password":
            return httpx.Response(
                200,
                json={
                    "access_token": "acc-old",
                    "refresh_token": "ref-old",
                    "expires_in": 1,  # near expiry -> triggers refresh
                },
            )
        calls["refresh_token"] += 1
        # Simulate refresh I/O so concurrent callers overlap on the lock.
        await asyncio.sleep(0.05)
        return httpx.Response(
            200,
            json={
                "access_token": "acc-new",
                "refresh_token": "ref-new",
                "expires_in": 3600,
            },
        )

    async def run():
        async with _client(handler) as http:
            store = TokenStore(GOTRUE, http=http, refresh_skew_seconds=60)
            await store.sign_in("a@b.com", "pw")
            results = await asyncio.gather(
                *(store.get_access_token() for _ in range(10))
            )
            assert results == ["acc-new"] * 10
            assert calls["refresh_token"] == 1  # single-flight

    asyncio.run(run())


def test_get_access_token_without_sign_in_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    async def run():
        async with _client(handler) as http:
            store = TokenStore(GOTRUE, http=http)
            with pytest.raises(AuthError):
                await store.get_access_token()

    asyncio.run(run())


def test_sign_in_failure_raises_and_no_secret_leak():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    password = "super-secret-pw"

    async def run():
        async with _client(handler) as http:
            store = TokenStore(GOTRUE, http=http)
            with pytest.raises(AuthError) as exc:
                await store.sign_in("a@b.com", password)
            assert password not in str(exc.value)
            assert store.is_authenticated is False

    asyncio.run(run())


def test_sign_in_missing_token_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        # 200 but no access_token in body.
        return httpx.Response(200, json={"refresh_token": "r"})

    async def run():
        async with _client(handler) as http:
            store = TokenStore(GOTRUE, http=http)
            with pytest.raises(AuthError):
                await store.sign_in("a@b.com", "pw")

    asyncio.run(run())
