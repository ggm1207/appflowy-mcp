# AppFlowy MCP

[English](./README.md) | **한국어**

**self-hosted AppFlowy Cloud**를 위한 [MCP](https://modelcontextprotocol.io) 서버입니다.
Claude(또는 임의의 MCP 클라이언트)가 AppFlowy Cloud REST API와 직접 통신하여
AppFlowy 페이지를 만들고, 읽고, 수정하고, 정리하고, 검색할 수 있게 해줍니다.

## 왜 만들었나

AppFlowy 데스크톱은 노트를 RocksDB/collab 바이너리로 저장하기 때문에 AI가 직접
읽을 수 없습니다. 그래서 보통은 마크다운 보관함(vault)을 수동으로 동기화하는
우회책을 씁니다. 하지만 **AppFlowy Cloud**를 로컬에서 운영한다면 그럴 필요가
없습니다. 이 서버는 Cloud REST API와 직접 통신하므로, 수동 import/export 없이
AI가 페이지를 바로 다룰 수 있습니다.

## 도구 (Tools)

| 도구 | 설명 |
|---|---|
| `list_workspaces` | 워크스페이스 목록 조회 |
| `get_folder` | 워크스페이스의 페이지/폴더 트리 읽기 (`depth?`) |
| `create_page` | 페이지 생성, 선택적으로 마크다운으로 채움 (`parent_view_id`, `name?`, `markdown?`, `layout?`) |
| `get_page` | 페이지 **메타데이터만** 읽기 (`view_id`) |
| `update_page` | 이름 변경 / 아이콘 설정 / 잠금 (`view_id`, `name`, `icon?`, `is_locked?`, `extra?`) |
| `append_markdown` | 페이지에 마크다운 내용 추가 (`view_id`, `markdown`) |
| `move_page` | 페이지를 새 부모 아래로 이동 (`view_id`, `new_parent_view_id`, `prev_view_id?`) |
| `trash_page` | 페이지를 휴지통으로 이동 (`view_id`) |
| `restore_page` | 휴지통에서 페이지 복원 (`view_id`) |
| `favorite_page` | 페이지 즐겨찾기 / 고정 (`view_id`, `is_favorite`, `is_pinned?`) |
| `search_workspace` | 워크스페이스 내 시맨틱 검색 (`query`, `limit?`) |

워크스페이스 단위로 동작하는 모든 도구는 선택적 `workspace_id`도 받습니다. 생략하면
`APPFLOWY_WORKSPACE_ID`를 쓰고, 그것도 없으면 `GET /api/workspace`가 반환하는
첫 번째 워크스페이스를 사용합니다.

### 동작 참고 사항 (꼭 읽어보세요)

- **`get_page`는 메타데이터만 반환합니다.** 페이지 본문(collab 문서)은 디코딩하지
  않습니다. 워크스페이스의 구조 / 페이지 트리를 읽으려면 **`get_folder`**를
  사용하세요. 구조 읽기의 1차 도구입니다.
- **`create_page`는 create-then-append 방식입니다 (요청 2번).** 먼저 빈 문서를
  생성해 `view_id`를 얻은 뒤, `markdown`이 주어졌으면 `append-block`을 호출합니다.
  이는 설계상 비원자적(non-atomic)입니다 — 인라인 `page_data` 경로가 가장 취약하여
  MVP에서 제외했습니다. 생성은 성공했으나 append가 실패하면, 그 오류는 **생성된
  `view_id`를 포함한 구조화된 에러로 노출**되며 절대 삼켜지지 않습니다. 따라서 그
  `view_id`에 대해 append를 다시 시도할 수 있습니다.
- **`search_workspace`는 임베딩 인덱서가 필요합니다.** 검색은 AppFlowy Cloud의
  **AI / 임베딩 인덱서 서비스**가 처리하며, 이 서비스가 실행 중이어야 합니다. 방금
  생성한 페이지는 **즉시 검색되지 않습니다** — 인덱서가 처리한 뒤에야 나타납니다.
  방금 작성한 내용은 검색해도 결과가 안 나오니, 이미 인덱싱된 문서를 대상으로
  검색하세요.

### 마크다운 변환

마크다운은 `markdown-it-py`를 통해 AppFlowy 문서 블록으로 변환됩니다:
헤딩(레벨 1–3으로 제한), 불릿/번호 목록, 할 일 목록(`[ ]` / `[x]`), 코드, 인용,
구분선, 문단, 그리고 인라인 **굵게** / *기울임* / `코드` / [링크](#) / ~~취소선~~.

미지원(알려진 제약): 밑줄, 표, 이미지, 중첩 목록(중첩된 목록 항목은 평탄화됩니다).

## 요구 사항

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/)
- 실행 중인 self-hosted AppFlowy Cloud와 해당 계정
- `search_workspace`용: AppFlowy Cloud **AI / 임베딩 인덱서** 서비스 실행

## 설정

```bash
uv sync
cp .env.example .env   # 이후 APPFLOWY_* 값 채우기
```

| 환경 변수 | 설명 | 기본값 |
|---|---|---|
| `APPFLOWY_BASE_URL` | Cloud API 베이스 | `http://localhost` (nginx) 또는 `:8000` |
| `APPFLOWY_GOTRUE_URL` | 인증(GoTrue) 베이스 | `http://localhost/gotrue` 또는 `:9999` |
| `APPFLOWY_EMAIL` / `APPFLOWY_PASSWORD` | 로그인 자격 증명 | — |
| `APPFLOWY_WORKSPACE_ID` | 기본 워크스페이스 (선택) | 첫 번째 워크스페이스 |

인증은 지연(lazy) 방식입니다: 시작 시에는 설정 값의 존재만 검증하고(없으면
즉시 실패), 실제 GoTrue `sign_in`은 첫 도구 호출 때 일어납니다. 토큰은 자동으로
갱신됩니다(만료 전 선제적 갱신, 그리고 401 발생 시 1회 재시도). 자격 증명과 토큰은
로그나 에러 메시지에 절대 기록되지 않습니다.

## Claude Code에 등록

```bash
claude mcp add appflowy -- uv --directory /absolute/path/to/appflowy-mcp run appflowy-mcp
```

또는 Claude Desktop 설정에서:

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

## 개발

```bash
uv run pytest        # 단위 테스트 (마크다운 → 블록 변환)
```

## 라이브 스모크 테스트 (8단계)

실행 중인 로컬 AppFlowy Cloud와 등록된 계정에 대한 end-to-end 점검입니다.
MCP Inspector나 서버가 등록된 Claude 클라이언트로 실행하세요:

1. `list_workspaces` — 워크스페이스 목록이 보임.
2. `get_folder` — 트리를 읽고 부모 `view_id`를 선택.
3. `create_page` — 그 부모 아래 페이지 생성; 반환된 `view_id` 기록.
4. `append_markdown` — 그 페이지에 여러 블록의 마크다운 추가, 예:

   ```markdown
   # T

   **bold** [link](u) `code`

   - a
   - b
   ```
5. **`get_page` + AppFlowy UI에서 페이지를 열어 렌더링 확인 (필수).**
   헤딩, 인라인 굵게/링크/코드, 목록이 AppFlowy에서 의도대로 렌더링되는지
   확인하세요 — `get_page`는 메타데이터만 반환하므로, UI에서의 시각적 확인이
   반드시 필요합니다.
6. `update_page` — 페이지 이름 변경; 새 이름 확인.
7. `favorite_page` → `trash_page` → `restore_page` — 생명주기 점검.
8. `search_workspace` — **이미 인덱싱된** 문서로 검색(방금 생성한 페이지는 즉시
   검색되지 않음을 기억하세요).

## 라이선스

MIT — [LICENSE](./LICENSE) 참조.
