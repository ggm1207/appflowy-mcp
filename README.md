# AppFlowy MCP

**English** | [한국어](./README.ko.md)

An [MCP](https://modelcontextprotocol.io) server for **self-hosted AppFlowy Cloud**.
It lets Claude (or any MCP client) create, read, update, organize and search
AppFlowy pages by talking directly to the AppFlowy Cloud REST API.

## Quick start — let your AI agent install it

Don't install it by hand. Paste the block below to your coding agent
(Claude Code, Cursor, Windsurf, …). It contains everything the agent needs to
clone, configure, and register the server — it will ask you for your AppFlowy
URL and credentials along the way.

````text
Set up the AppFlowy MCP server for me.

1. Clone https://github.com/ggm1207/appflowy-mcp and cd into it.
2. Run `uv sync`.
3. Copy `.env.example` to `.env`. Ask me for my AppFlowy Cloud base URL,
   GoTrue URL, email, and password, then fill them in. Never echo my
   password back to me.
4. Register the server with Claude Code:
   `claude mcp add appflowy -- uv --directory "$(pwd)" run appflowy-mcp`
5. Verify it works by calling the `list_workspaces` tool and showing me my
   workspaces.

Requirements & rules:
- Python 3.12+ and `uv` must be installed.
- A self-hosted AppFlowy Cloud must already be running and I must have an
  account on it.
- `.env` holds secrets — confirm it is gitignored and never commit it.
````

Prefer to do it yourself? See [Setup](#setup) and
[Register with Claude Code](#register-with-claude-code) below.

## Why

AppFlowy desktop stores notes as RocksDB/collab binaries that an AI can't read
directly, so the usual workaround is a markdown vault synced by hand. If you run
**AppFlowy Cloud** locally, you don't need that: this server speaks the Cloud
REST API directly, so the AI can operate on pages with no manual import/export.

## Tools

| Tool | What it does |
|---|---|
| `list_workspaces` | List your workspaces |
| `get_folder` | Read a workspace's page/folder tree (`depth?`) |
| `create_page` | Create a page, optionally from markdown (`parent_view_id`, `name?`, `markdown?`, `layout?`) |
| `get_page` | Read page **metadata only** (`view_id`) |
| `update_page` | Rename / set icon / lock (`view_id`, `name`, `icon?`, `is_locked?`, `extra?`) |
| `append_markdown` | Append markdown content to a page (`view_id`, `markdown`) |
| `move_page` | Move a page under a new parent (`view_id`, `new_parent_view_id`, `prev_view_id?`) |
| `trash_page` | Move a page to trash (`view_id`) |
| `restore_page` | Restore a page from trash (`view_id`) |
| `favorite_page` | Favorite / pin a page (`view_id`, `is_favorite`, `is_pinned?`) |
| `search_workspace` | Semantic search across a workspace (`query`, `limit?`) |

Every workspace-scoped tool also accepts an optional `workspace_id`; when omitted
the server uses `APPFLOWY_WORKSPACE_ID`, or the first workspace returned by
`GET /api/workspace`.

### Behavior notes (read these)

- **`get_page` returns metadata only.** The page body (collab document) is not
  decoded. To read a workspace's structure / page tree, use **`get_folder`**,
  which is the primary tool for reading layout.
- **`create_page` is create-then-append (2 requests).** It first creates an empty
  document to obtain a `view_id`, then — if `markdown` was supplied — calls
  `append-block`. This is non-atomic by design (the inline `page_data` path is the
  most fragile and is excluded from the MVP). If creation succeeds but the append
  fails, the error is **surfaced as a structured error that includes the created
  `view_id`** plus the append failure — it is never swallowed, so you can retry the
  append against that `view_id`.
- **`search_workspace` requires the embedding indexer.** Search is served by
  AppFlowy Cloud's **AI / embedding indexer service**, which must be running. Newly
  created pages are **not immediately searchable** — they only appear once the
  indexer has processed them. Search returns nothing useful for content you just
  wrote; query against already-indexed documents.

### Markdown conversion

Markdown is converted to AppFlowy document blocks via `markdown-it-py`:
headings (level clamped 1–3), bulleted/numbered lists, todo lists (`[ ]` / `[x]`),
code, quote, divider, and paragraphs, plus inline **bold** / *italic* / `code` /
[link](#) / ~~strikethrough~~.

Not supported (documented limitation): underline, tables, images, and nested lists
(nested list items are flattened).

## Usage examples

Once the server is registered, just talk to your agent in plain language — it
picks the right tool for you:

| You say | Tool used |
|---|---|
| "List my AppFlowy workspaces" | `list_workspaces` |
| "Show me the page tree in my workspace" | `get_folder` |
| "Create a page 'Meeting Notes' under General with this agenda: …" | `create_page` |
| "Append today's standup notes to the Meeting Notes page" | `append_markdown` |
| "Rename that page to 'Q3 Planning'" | `update_page` |
| "Move the Drafts page under Archive" | `move_page` |
| "Favorite the roadmap page" | `favorite_page` |
| "Trash the old scratch page" | `trash_page` |
| "Search my workspace for 'authentication design'" | `search_workspace` |

### Example — turn a conversation into a structured note

> "Summarize our conversation and save it as a new AppFlowy page under General
> titled **API design decisions**, with a heading per decision and a checklist
> of follow-ups."

The agent writes a markdown summary (headings, bullet/todo lists, bold, links),
the server converts it to AppFlowy blocks, and `create_page` produces the page
in one shot (create-then-append).

### Example — append to a running journal

> "Add a new section to my **Journal** page: today's date as a heading, then
> three bullet points of what I worked on."

The agent calls `append_markdown` against the existing page's `view_id`, so the
new section is added without touching what's already there.

### Example — tidy up the workspace

> "Find my old draft pages, move the finished ones under **Archive**, and trash
> the empty ones."

The agent reads the tree with `get_folder`, then orchestrates `move_page` and
`trash_page` per page — review the proposed actions before confirming.

## Requirements

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/)
- A running self-hosted AppFlowy Cloud and an account on it
- For `search_workspace`: the AppFlowy Cloud **AI / embedding indexer** service running

## Setup

```bash
uv sync
cp .env.example .env   # then fill in APPFLOWY_* values
```

| Variable | Description | Default |
|---|---|---|
| `APPFLOWY_BASE_URL` | Cloud API base | `http://localhost` (nginx) or `:8000` |
| `APPFLOWY_GOTRUE_URL` | Auth (GoTrue) base | `http://localhost/gotrue` or `:9999` |
| `APPFLOWY_EMAIL` / `APPFLOWY_PASSWORD` | login credentials | — |
| `APPFLOWY_WORKSPACE_ID` | default workspace (optional) | first workspace |

Authentication is lazy: on startup only the presence of config is validated
(fail-fast if missing). The actual GoTrue `sign_in` happens on the first tool call,
and tokens are refreshed automatically (proactively before expiry, and reactively
on a single 401 retry). Credentials and tokens are never written to logs or error
messages.

## Register with Claude Code

```bash
claude mcp add appflowy -- uv --directory /absolute/path/to/appflowy-mcp run appflowy-mcp
```

Or in Claude Desktop config:

```json
{
  "mcpServers": {
    "appflowy": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/appflowy-mcp", "run", "appflowy-mcp"]
    }
  }
}
```

## Development

```bash
uv run pytest        # unit tests (markdown → blocks conversion)
```

## Live smoke test (8 steps)

End-to-end check against a running local AppFlowy Cloud with a registered account.
Run via the MCP Inspector or a Claude client with the server registered:

1. `list_workspaces` — your workspace(s) are listed.
2. `get_folder` — read the tree and pick a parent `view_id`.
3. `create_page` — create a page under that parent; note the returned `view_id`.
4. `append_markdown` — append multi-block markdown to that page, e.g.:

   ```markdown
   # T

   **bold** [link](u) `code`

   - a
   - b
   ```
5. **`get_page` + open the page in the AppFlowy UI to confirm it renders
   (MANDATORY).** Verify the heading, inline bold/link/code, and the list render
   correctly in AppFlowy itself — `get_page` only returns metadata, so visual
   confirmation in the UI is required.
6. `update_page` — rename the page; confirm the new name.
7. `favorite_page` → `trash_page` → `restore_page` — exercise the lifecycle.
8. `search_workspace` — query an **already-indexed** document (remember: pages you
   just created are not immediately searchable).

## License

MIT — see [LICENSE](./LICENSE).
