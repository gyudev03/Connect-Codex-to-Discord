# Connect Codex to Discord

Discord 채널에서 로컬 `codex` CLI를 호출해 코딩 작업을 맡기는 작은 브리지 봇입니다.  
예: `!codex README 정리해줘`, `!codex-continue 방금 변경한 파일 테스트도 추가해줘`

## 준비

1. Discord Developer Portal에서 봇을 만들고 토큰을 발급합니다.
2. Bot 설정에서 **Privileged Gateway Intents** 아래의 **Message Content Intent**를 켭니다.
3. 봇 초대 URL에는 최소 권한으로 `Read Messages/View Channels`, `Send Messages`, `Read Message History`, `Attach Files`를 넣습니다.
4. 이 PC에서 `codex login`이 완료되어 있어야 합니다.

## 설치

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

`.env`를 열고 `DISCORD_TOKEN`을 채운 뒤, 필요하면 `CODEX_WORKSPACE`를 실제 코딩할 저장소 경로로 바꿉니다.

## 실행

```powershell
.\.venv\Scripts\Activate.ps1
python bot.py
```

## 명령어

- `!codex <요청>`: 새 Codex 작업을 실행합니다.
- `!codex-continue <요청>`: 이 채널에 저장된 마지막 Codex 세션을 이어갑니다.
- `!codex-resume <세션ID|Discord 메시지 URL> <요청>`: 특정 세션을 이어갑니다.
- `!codex-review [지시문]`: 현재 작업 트리의 변경사항을 리뷰합니다.
- `!codex-status`: 작업 폴더, 실행 여부, 저장된 세션을 확인합니다.
- `!codex-cancel`: 현재 채널에서 실행 중인 Codex 작업을 중단합니다.

이미지 첨부가 있는 `!codex`/`!codex-continue` 메시지는 첨부 이미지를 `codex exec --image`로 함께 전달합니다.

## 설정

`.env.example`의 주요 값:

- `CODEX_WORKSPACE`: Codex가 실제로 수정할 저장소 경로
- `CODEX_ARGS`: 기본값 `--full-auto`
- `CODEX_MODEL`: 비워두면 Codex CLI 기본 모델 사용
- `MAX_PARALLEL_CODEX_JOBS`: 동시에 실행할 Codex 작업 수
- `DISCORD_ALLOWED_CHANNEL_IDS`: 특정 채널에서만 허용하고 싶을 때 사용
- `DISCORD_ALLOWED_ROLE_IDS`: 특정 역할만 허용하고 싶을 때 사용

## 안전 메모

이 봇은 Discord 메시지를 로컬 Codex 실행으로 연결합니다. 개인 서버나 제한된 채널에서 먼저 쓰는 것을 권장합니다.  
`CODEX_ARGS`에 `--dangerously-bypass-approvals-and-sandbox`를 넣으면 훨씬 위험해지므로, 신뢰하는 폐쇄 환경이 아니라면 기본값을 유지하세요.
