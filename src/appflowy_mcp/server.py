"""MCP server entrypoint (stdio transport).

Wires configuration from the environment, constructs the auth ``TokenStore`` and
``AppFlowyClient`` over one shared ``httpx.AsyncClient``, registers the tools from
tools.py, and serves over stdio.

Authentication is LAZY (frozen contract B): ``load_config`` only fails fast when a
required environment variable is MISSING; no network sign-in happens at startup.
The first tool call signs in (single-flight); later calls reuse the token. This
keeps the server importable and initializable without credentials being valid —
only an actual tool invocation needs the network.

Required env: APPFLOWY_BASE_URL, APPFLOWY_GOTRUE_URL, APPFLOWY_EMAIL, APPFLOWY_PASSWORD.
Optional env: APPFLOWY_WORKSPACE_ID.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

import httpx
from dotenv import load_dotenv
from mcp.server.stdio import stdio_server

from .auth import TokenStore
from .client import AppFlowyClient
from .tools import build_server

_REQUIRED_ENV = (
    "APPFLOWY_BASE_URL",
    "APPFLOWY_GOTRUE_URL",
    "APPFLOWY_EMAIL",
    "APPFLOWY_PASSWORD",
)


@dataclass
class Config:
    """Resolved server configuration. Holds credentials but never logs them."""

    base_url: str
    gotrue_url: str
    email: str
    password: str
    workspace_id: str | None = None


def load_config() -> Config:
    """Load configuration from the environment, failing fast on MISSING vars.

    Only the *presence* of required variables is validated here — no network
    call is made — so a misconfigured environment is reported immediately while
    a fully-configured one defers actual sign-in until the first tool call.

    A ``.env`` file in the working directory is loaded if present; real
    environment variables take precedence (``load_dotenv`` does not override).
    """
    load_dotenv()
    missing = [name for name in _REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". See .env.example for the full list."
        )
    return Config(
        base_url=os.environ["APPFLOWY_BASE_URL"],
        gotrue_url=os.environ["APPFLOWY_GOTRUE_URL"],
        email=os.environ["APPFLOWY_EMAIL"],
        password=os.environ["APPFLOWY_PASSWORD"],
        workspace_id=os.environ.get("APPFLOWY_WORKSPACE_ID") or None,
    )


async def serve() -> None:
    """Construct the client stack and serve the MCP server over stdio."""
    config = load_config()

    async with httpx.AsyncClient(timeout=30.0) as http:
        token_store = TokenStore(config.gotrue_url, http=http)
        client = AppFlowyClient(
            config.base_url,
            token_store,
            http=http,
            default_workspace_id=config.workspace_id,
        )

        # Lazy authentication: sign in once, on the first tool call.
        _signin_lock = asyncio.Lock()

        async def ensure_signed_in() -> None:
            if token_store.is_authenticated:
                return
            async with _signin_lock:
                if not token_store.is_authenticated:
                    await token_store.sign_in(config.email, config.password)
                    # Provision/sync the cloud user (af_user + default workspace).
                    # Required for first-time accounts; idempotent otherwise.
                    await client.verify_user()

        server = build_server(client, ensure_signed_in=ensure_signed_in)
        init_options = server.create_initialization_options()

        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, init_options)


def main() -> None:
    """Console-script entrypoint (`appflowy-mcp`)."""
    asyncio.run(serve())


if __name__ == "__main__":
    main()
