# 현재 개발 중단 및 Discord_Codex에서 새로 진행 중
# Connect Codex to Discord

Discord에서 로컬 `codex` CLI를 불러 코딩 작업을 맡기는 브리지 봇입니다.

서버에 봇을 초대해 두면 Discord 메시지로 Codex에게 파일 수정, 코드 리뷰, 테스트 추가, 커밋, 푸시, 새 프로젝트 생성을 요청할 수 있습니다. Codex는 이 PC의 작업 폴더에서 실행되며, 결과를 다시 Discord 채널에 답합니다.

## 무엇을 할 수 있나요?

- Discord 채널에서 `!codex` 또는 자연어로 Codex 작업 실행
- 채널별 프로젝트 폴더 연결
- 새 프로젝트 폴더와 Discord 프로젝트 채널 자동 생성
- GitHub 저장소 생성 요청
- 현재 변경사항 리뷰
- 확인 이모지 후 커밋/푸시 실행
- 이미지 첨부를 Codex 입력으로 전달
- 별도 Gemini 봇과 Discord 안에서 diff 리뷰/승인 흐름 실행
- 실행 결과와 수정내역을 `ai-수정내역` 포럼에 기록
- 작업 중단, 상태 확인, 이전 Codex 세션 이어가기

## 기본 사용 흐름

1. Discord에서 `#ai` 같은 일반 AI 채널에 새 프로젝트를 요청합니다.
2. 봇이 `CODEX_PROJECTS_ROOT` 아래에 프로젝트 폴더를 만들고, `ai` 카테고리에 프로젝트 채널을 만듭니다.
3. 이후 그 프로젝트 채널에서 `!codex` 또는 자연어로 작업을 요청합니다.
4. Codex가 로컬 파일을 수정하고 Discord에 결과를 답합니다.
5. 필요하면 `!codex-review`, `!codex-commit`, `!codex-push`로 검토와 배포 흐름을 이어갑니다.

예시:

```text
코덱스야 새 프로젝트 Todo App 만들어줘
코덱스야 Todo App 프로젝트 만들고 GitHub에도 private로 만들어줘
```

그러면 `D:\Coding\Todo App` 같은 폴더와 `#todo-app` 프로젝트 채널이 만들어집니다. 이후 `#todo-app`에서 실행하는 Codex 작업은 그 폴더를 기준으로 동작합니다.

## 설치 전 준비

1. Discord Developer Portal에서 봇을 만들고 토큰을 발급합니다.
2. Bot 설정에서 **Privileged Gateway Intents** 아래의 **Message Content Intent**를 켭니다.
3. 봇 초대 URL에는 최소 권한으로 `Read Messages/View Channels`, `Send Messages`, `Read Message History`, `Attach Files`, `Add Reactions`를 넣습니다.
4. 프로젝트 채널 자동 생성을 쓰려면 `Manage Channels` 권한도 추가합니다.
5. 이 PC에서 `codex login`을 완료합니다.
6. GitHub 저장소 생성이나 푸시를 쓰려면 `gh auth login`도 완료합니다.
7. Gemini 리뷰 봇을 함께 쓰려면 별도 Discord 봇에도 **Message Content Intent**와 메시지 읽기/쓰기 권한을 켭니다.

## 설치

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

`.env`를 열고 `DISCORD_TOKEN`을 채웁니다. 필요하면 `CODEX_WORKSPACE`와 `CODEX_PROJECTS_ROOT`를 실제 사용할 경로로 바꿉니다.

## 실행

```powershell
.\.venv\Scripts\Activate.ps1
python run_bots.py
```

Codex 봇만 따로 실행하려면 다음 명령을 사용합니다.

```powershell
.\.venv\Scripts\Activate.ps1
python bot.py
```

봇이 정상 로그인하면 Discord에서 명령어를 사용할 수 있습니다.

## 명령어

### Codex 작업

| 명령어 | 설명 |
| --- | --- |
| `!codex <요청>` | 새 Codex 작업을 실행합니다. |
| `!codex-continue <요청>` | 이 채널의 마지막 Codex 세션을 이어서 요청합니다. |
| `!codex-resume <세션ID\|Discord 메시지 URL> <요청>` | 특정 세션을 이어서 요청합니다. |
| `!codex-review [지시문]` | 현재 작업 트리의 변경사항을 리뷰합니다. |
| `!codex-status` | 작업 폴더, 채널 모드, 실행 여부, 저장된 세션을 확인합니다. |
| `!codex-cancel` | 현재 채널에서 실행 중인 Codex 작업을 중단합니다. |

이미지 첨부가 있는 `!codex`/`!codex-continue` 메시지는 첨부 이미지를 `codex exec --image`로 함께 전달합니다.

`GEMINI_REVIEW_ENABLED=true`이고 `GEMINI_BOT_USER_IDS`가 설정되어 있으면 Codex 작업 성공 후 답변에 `GEMINI_REVIEW_EMOJI` 반응을 붙입니다. 요청한 사용자가 그 반응을 누르면 봇이 Codex 답변을 Gemini 봇에 재검토 요청하고, `git diff`가 있으면 함께 보냅니다. 채팅창이 길어지지 않도록 Gemini에게 보낼 상세 내용은 항상 첨부 파일로 보냅니다. 예전처럼 자동으로 Gemini 리뷰를 요청하려면 `GEMINI_REVIEW_AUTO_REQUEST=true`를 설정하세요. 자동 요청은 코드 변경으로 보이는 답변이나 실제 diff가 있는 답변에만 동작합니다. `!codex-review`는 Codex가 만든 리뷰 결과를 Gemini 봇에 다시 보내 독립 재검토를 요청합니다. Gemini 봇은 기본적으로 Flash 모델로 리뷰하고 실제 사용 모델을 답변에 표시합니다. 리뷰가 길면 전체 리뷰를 첨부 파일로 보내며, Codex는 첨부까지 읽어 Gemini 리뷰에 대한 자기 의견과 반영 시 바뀔 점을 함께 보여줍니다. 사용자가 `✅`를 누르면 리뷰를 반영하며 `❌`를 누르면 취소합니다.

### 프로젝트 관리

| 명령어 | 설명 |
| --- | --- |
| `!codex-new <프로젝트 이름>` | `CODEX_PROJECTS_ROOT` 아래에 프로젝트 폴더를 만들고 `ai` 카테고리에 채널을 생성합니다. |
| `!codex-new <프로젝트 이름> --github --private` | 프로젝트를 만든 뒤 GitHub 저장소 생성 확인 메시지를 보냅니다. |
| `!codex-delete` | 현재 프로젝트 채널과 로컬 프로젝트 폴더 삭제를 요청합니다. |

`!codex-delete`는 바로 삭제하지 않습니다. 같은 사용자가 5분 안에 `삭제 확인`이라고 보내야 실행됩니다. `ai-수정내역` 포럼의 프로젝트 수정내역 포스트는 유지됩니다.

### Git 작업

| 명령어 | 설명 |
| --- | --- |
| `!codex-commit <메시지>` | 확인 이모지를 누른 뒤 현재 채널의 작업 폴더를 커밋합니다. |
| `!codex-push <메시지>` | 확인 이모지를 누른 뒤 현재 채널의 작업 폴더를 커밋하고 푸시합니다. |

커밋/푸시는 바로 실행되지 않습니다. 봇이 확인 메시지에 `✅`/`❌` 이모지를 달고, 요청한 사용자가 `✅`를 누르면 실행합니다.

자연어로도 요청할 수 있습니다.

```text
코덱스야 커밋해줘
코덱스야 푸시해줘
```

### 채팅 모드

| 명령어 | 설명 |
| --- | --- |
| `!codex-chat [요청]` | 대화용 스레드를 열고 채팅 모드를 시작합니다. |
| `!codex-chat-off` | 현재 채널/스레드의 채팅 모드를 끕니다. |
| `@봇 요청` | 명령어 없이 봇 멘션으로 Codex에게 요청합니다. |
| `코덱스야 요청` | 설정된 호출어로 Codex에게 요청합니다. |

기본 설정에서는 `ai` 카테고리 안에서 명령어 없이도 바로 대화할 수 있습니다. 일반 `#ai` 채널은 범용 질문과 프로젝트 생성에 쓰고, 그 외 `ai` 카테고리 채널은 채널별 프로젝트 작업에 쓰는 구성을 권장합니다.

## 실행 중 다른 질문을 보내면?

같은 채널에서 실행 중인 작업이 있을 때는 `!codex-status`로 상태를 확인할 수 있고, 필요하면 `!codex-cancel`로 중단할 수 있습니다.

기존 작업의 맥락을 이어서 말하려면 `!codex-continue <요청>`을 사용하세요. 완전히 새 작업으로 요청하고 싶다면 `!codex <요청>`을 사용합니다. 동시에 실행 가능한 작업 수는 `MAX_PARALLEL_CODEX_JOBS` 설정을 따릅니다.

## 수정내역 기록

`ai` 카테고리 안에서 실행된 프로젝트 작업의 결과는 `ai-수정내역` 포럼에 프로젝트 이름의 포스트로 기록됩니다. 포스트가 없으면 봇이 새로 만들고, 이후 같은 프로젝트의 작업 로그를 이어서 남깁니다.

일반 `#ai` 채널에서도 파일 수정, 구현, 삭제, 오류 등 변경 가능성이 있는 결과는 수정내역 포스트에 기록됩니다.

## 설정

`.env.example`의 주요 값:

| 값 | 설명 |
| --- | --- |
| `DISCORD_TOKEN` | Discord 봇 토큰 |
| `COMMAND_PREFIX` | 명령어 접두사. 기본값은 `!` |
| `CODEX_WORKSPACE` | 기본 Codex 작업 폴더 |
| `CODEX_PROJECTS_ROOT` | 새 프로젝트 폴더를 만들 루트 경로 |
| `CODEX_COMMAND` | 실행할 Codex 명령어. 기본값은 `codex` |
| `CODEX_MODEL` | 비워두면 Codex CLI 기본 모델 사용 |
| `CODEX_ARGS` | Codex 실행 인자. 기본값은 `--full-auto` |
| `CODEX_TIMEOUT_SECONDS` | Codex 작업 제한 시간 |
| `MAX_PARALLEL_CODEX_JOBS` | 동시에 실행할 Codex 작업 수 |
| `DISCORD_CODEX_CATEGORY_NAME` | Codex 채널을 둘 Discord 카테고리 이름 |
| `DISCORD_CHANGELOG_FORUM_NAME` | 수정내역을 기록할 Discord 포럼 이름 |
| `CODEX_GENERAL_CHANNEL_NAMES` | 일반 AI 채널 이름 목록 |
| `CATEGORY_CHAT_ENABLED` | `ai` 카테고리에서 명령어 없는 대화 허용 |
| `MENTION_CHAT_ENABLED` | 봇 멘션으로 Codex 요청 허용 |
| `THREAD_CHAT_ENABLED` | 채팅 스레드 모드 허용 |
| `WAKE_WORDS` | 자연어 호출어 목록 |
| `INSTANT_REPLIES_ENABLED` | 짧은 인사/도움말을 하드코딩 즉답으로 처리할지 여부 |
| `SLOW_NOTICE_ENABLED` | 작업이 오래 걸릴 때 안내 메시지 표시 여부 |
| `GITHUB_REPO_OWNER` | GitHub 저장소를 만들 owner. 비워두면 `gh` 로그인 계정 사용 |
| `GITHUB_DEFAULT_VISIBILITY` | GitHub 저장소 기본 공개 범위. `private`, `public`, `internal` 중 하나 |
| `GEMINI_REVIEW_ENABLED` | Codex 작업 후 Gemini Discord 봇 리뷰 기능 사용 여부 |
| `GEMINI_BOT_USER_IDS` | Gemini 리뷰 봇 Discord 사용자 ID 목록 |
| `GEMINI_REVIEW_AUTO_REQUEST` | 코드 변경처럼 보이는 Codex 결과를 Gemini에 자동 리뷰 요청할지 여부 |
| `GEMINI_REVIEW_EMOJI` | Codex 답변에 붙일 Gemini 리뷰 요청 reaction |
| `GEMINI_REVIEW_MAX_DIFF_CHARS` | Codex 봇이 Gemini 봇에 보낼 diff 최대 글자 수 |
| `GEMINI_REVIEW_WAIT_SECONDS` | Gemini 리뷰 반영 확인 대기 시간 |
| `GEMINI_DISCORD_TOKEN` | 별도 Gemini Discord 봇 토큰 |
| `GEMINI_API_KEY` | Gemini API 키 |
| `GEMINI_MODEL` | 기본 리뷰 모델. 예: `gemini-2.5-flash` |
| `GEMINI_FALLBACK_MODEL` | 기본 모델 사용 제한 시 대체 리뷰 모델 |
| `GEMINI_REVIEW_MAX_OUTPUT_TOKENS` | Gemini 리뷰 출력 토큰 한도. 리뷰가 끊기면 늘립니다. 기본값 예: `8192` |
| `DISCORD_ALLOWED_CHANNEL_IDS` | 허용할 채널 ID 목록. 비워두면 모든 채널 허용 |
| `DISCORD_ALLOWED_ROLE_IDS` | 허용할 역할 ID 목록. 비워두면 모든 멤버 허용 |

## 안전 메모

이 봇은 Discord 메시지를 로컬 Codex 실행으로 연결합니다. 개인 서버나 제한된 채널에서 먼저 쓰는 것을 권장합니다.

`CODEX_ARGS`에 `--dangerously-bypass-approvals-and-sandbox`를 넣으면 훨씬 위험해집니다. 신뢰하는 폐쇄 환경이 아니라면 기본값을 유지하세요.
