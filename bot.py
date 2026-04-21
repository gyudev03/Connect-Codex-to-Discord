from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import aiohttp
import discord
from discord.ext import commands


DISCORD_MESSAGE_LIMIT = 2000
CODE_BLOCK_OVERHEAD = len("```text\n\n```")
MAX_REPLY_CHUNKS = 6


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def env_list(name: str) -> set[int]:
    value = os.environ.get(name, "").strip()
    if not value:
        return set()

    ids: set[int] = set()
    for item in value.split(","):
        item = item.strip()
        if item:
            ids.add(int(item))
    return ids


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    return int(value)


def split_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        boundary = remaining.rfind("\n", 0, limit)
        if boundary < limit // 2:
            boundary = remaining.rfind(" ", 0, limit)
        if boundary < limit // 2:
            boundary = limit

        chunks.append(remaining[:boundary].rstrip())
        remaining = remaining[boundary:].lstrip()

    return chunks


def as_code_blocks(text: str) -> list[str]:
    safe = text.replace("```", "`\u200b``")
    return [f"```text\n{chunk}\n```" for chunk in split_text(safe, DISCORD_MESSAGE_LIMIT - CODE_BLOCK_OVERHEAD)]


def parse_session_id(line: str) -> str | None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None

    candidates = [
        event.get("session_id"),
        event.get("sessionId"),
        event.get("conversation_id"),
        event.get("conversationId"),
    ]

    if isinstance(event.get("session"), dict):
        candidates.extend(
            [
                event["session"].get("id"),
                event["session"].get("session_id"),
                event["session"].get("sessionId"),
            ]
        )

    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def extract_session_id(output: str) -> str | None:
    for line in output.splitlines():
        session_id = parse_session_id(line)
        if session_id:
            return session_id
    return None


def parse_discord_id_from_url(url: str) -> int | None:
    match = re.search(r"/channels/\d+/(\d+)(?:/|$)", url)
    if match:
        return int(match.group(1))
    return None


@dataclass
class Settings:
    token: str
    prefix: str
    workspace: Path
    codex_command: str
    codex_model: str | None
    codex_args: list[str]
    timeout_seconds: int
    max_parallel_jobs: int
    allowed_channel_ids: set[int]
    allowed_role_ids: set[int]
    mention_chat_enabled: bool
    thread_chat_enabled: bool

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(Path(".env"))
        token = os.environ.get("DISCORD_TOKEN", "").strip()
        if not token:
            raise RuntimeError("DISCORD_TOKEN is missing. Copy .env.example to .env and fill it in.")

        workspace = Path(os.environ.get("CODEX_WORKSPACE", os.getcwd())).expanduser().resolve()
        return cls(
            token=token,
            prefix=os.environ.get("COMMAND_PREFIX", "!").strip() or "!",
            workspace=workspace,
            codex_command=os.environ.get("CODEX_COMMAND", "codex").strip() or "codex",
            codex_model=os.environ.get("CODEX_MODEL", "").strip() or None,
            codex_args=shlex.split(os.environ.get("CODEX_ARGS", "--full-auto")),
            timeout_seconds=env_int("CODEX_TIMEOUT_SECONDS", 1800),
            max_parallel_jobs=max(1, env_int("MAX_PARALLEL_CODEX_JOBS", 1)),
            allowed_channel_ids=env_list("DISCORD_ALLOWED_CHANNEL_IDS"),
            allowed_role_ids=env_list("DISCORD_ALLOWED_ROLE_IDS"),
            mention_chat_enabled=os.environ.get("MENTION_CHAT_ENABLED", "true").strip().lower() != "false",
            thread_chat_enabled=os.environ.get("THREAD_CHAT_ENABLED", "true").strip().lower() != "false",
        )


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {str(key): str(value) for key, value in data.items()}

    def get(self, channel_id: int) -> str | None:
        return self._data.get(str(channel_id))

    def set(self, channel_id: int, session_id: str) -> None:
        self._data[str(channel_id)] = session_id
        self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")


class ChatChannelStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._channel_ids: set[str] = self._load()

    def _load(self) -> set[str]:
        if not self.path.exists():
            return set()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return set()
        if not isinstance(data, list):
            return set()
        return {str(item) for item in data}

    def add(self, channel_id: int) -> None:
        self._channel_ids.add(str(channel_id))
        self.path.write_text(json.dumps(sorted(self._channel_ids), indent=2), encoding="utf-8")

    def remove(self, channel_id: int) -> None:
        self._channel_ids.discard(str(channel_id))
        self.path.write_text(json.dumps(sorted(self._channel_ids), indent=2), encoding="utf-8")

    def contains(self, channel_id: int) -> bool:
        return str(channel_id) in self._channel_ids


class CodexBridge:
    def __init__(self, settings: Settings, session_store: SessionStore) -> None:
        self.settings = settings
        self.session_store = session_store
        self.semaphore = asyncio.Semaphore(settings.max_parallel_jobs)
        self.active_processes: dict[int, asyncio.subprocess.Process] = {}

    def base_args(self) -> list[str]:
        args = [self.settings.codex_command]
        args.extend(self.settings.codex_args)
        if self.settings.codex_model:
            args.extend(["--model", self.settings.codex_model])
        return args

    async def save_attachments(self, message: discord.Message) -> list[Path]:
        image_paths: list[Path] = []
        attachment_dir = Path("data") / "attachments" / str(message.id)
        attachment_dir.mkdir(parents=True, exist_ok=True)

        for attachment in message.attachments:
            content_type = attachment.content_type or ""
            suffix = Path(attachment.filename).suffix.lower()
            if not content_type.startswith("image/") and suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
                continue
            target = attachment_dir / Path(attachment.filename).name
            await attachment.save(target)
            image_paths.append(target.resolve())

        return image_paths

    async def run_exec(
        self,
        channel_id: int,
        prompt: str,
        *,
        resume_session_id: str | None = None,
        image_paths: Iterable[Path] = (),
    ) -> tuple[int, str, str | None]:
        with tempfile.NamedTemporaryFile(prefix="codex-last-message-", suffix=".txt", delete=False) as output_file:
            output_path = Path(output_file.name)

        args = self.base_args()
        if resume_session_id:
            args.extend(["exec", "resume", "--json", "-o", str(output_path)])
            for image_path in image_paths:
                args.extend(["--image", str(image_path)])
            args.extend([resume_session_id, "-"])
        else:
            args.extend(["exec", "-C", str(self.settings.workspace), "--json", "-o", str(output_path)])
            for image_path in image_paths:
                args.extend(["--image", str(image_path)])
            args.append("-")

        return await self._run_process(channel_id, args, prompt, output_path=output_path)

    async def run_review(self, channel_id: int, prompt: str) -> tuple[int, str, str | None]:
        args = [self.settings.codex_command, "review", "--uncommitted"]
        if prompt:
            args.append("-")
            stdin = prompt
        else:
            stdin = ""

        return_code, text, _ = await self._run_process(channel_id, args, stdin, output_path=None)
        return return_code, text, None

    async def _run_process(
        self,
        channel_id: int,
        args: list[str],
        stdin: str,
        *,
        output_path: Path | None,
    ) -> tuple[int, str, str | None]:
        if not self.settings.workspace.exists():
            return (
                1,
                "Codex 작업 폴더를 찾지 못했어요.\n"
                f"현재 CODEX_WORKSPACE: {self.settings.workspace}\n\n"
                ".env의 CODEX_WORKSPACE를 실제로 존재하는 프로젝트 폴더로 바꿔 주세요.",
                None,
            )

        async with self.semaphore:
            try:
                process = await asyncio.create_subprocess_exec(
                    *args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(self.settings.workspace),
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
            except FileNotFoundError:
                command = args[0]
                return (
                    127,
                    "Codex 실행 파일을 찾지 못했어요.\n"
                    f"현재 CODEX_COMMAND: {command}\n\n"
                    ".env의 CODEX_COMMAND를 codex.exe 절대경로로 바꿔 주세요. 예:\n"
                    "CODEX_COMMAND=d:\\Coding\\extensions\\openai.chatgpt-26.417.40842-win32-x64\\bin\\windows-x86_64\\codex.exe",
                    None,
                )
            self.active_processes[channel_id] = process

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(stdin.encode("utf-8")),
                    timeout=self.settings.timeout_seconds,
                )
            except asyncio.TimeoutError:
                self.terminate(channel_id)
                return 124, f"Codex timed out after {self.settings.timeout_seconds} seconds.", None
            finally:
                self.active_processes.pop(channel_id, None)

            raw_stdout = stdout.decode("utf-8", errors="replace").strip()
            raw_stderr = stderr.decode("utf-8", errors="replace").strip()
            session_id = extract_session_id(raw_stdout)

            final_text = ""
            if output_path and output_path.exists():
                final_text = output_path.read_text(encoding="utf-8", errors="replace").strip()
                output_path.unlink(missing_ok=True)

            if not final_text:
                final_text = raw_stdout
            if raw_stderr:
                final_text = f"{final_text}\n\n[stderr]\n{raw_stderr}".strip()

            return process.returncode or 0, final_text or "(no output)", session_id

    def terminate(self, channel_id: int) -> bool:
        process = self.active_processes.get(channel_id)
        if not process or process.returncode is not None:
            return False

        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.terminate()
        return True


settings = Settings.from_env()
session_store = SessionStore(Path("data") / "sessions.json")
chat_channel_store = ChatChannelStore(Path("data") / "chat_channels.json")
bridge = CodexBridge(settings, session_store)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=settings.prefix, intents=intents, help_command=None)


async def is_allowed(ctx: commands.Context) -> bool:
    if settings.allowed_channel_ids and ctx.channel.id not in settings.allowed_channel_ids:
        await ctx.reply("이 채널에서는 Codex 봇을 사용할 수 없어요.", mention_author=False)
        return False

    if settings.allowed_role_ids and isinstance(ctx.author, discord.Member):
        member_role_ids = {role.id for role in ctx.author.roles}
        if not member_role_ids.intersection(settings.allowed_role_ids):
            await ctx.reply("이 명령을 실행할 역할 권한이 없어요.", mention_author=False)
            return False

    return True


async def send_codex_result(ctx: commands.Context, title: str, return_code: int, output: str, session_id: str | None) -> None:
    status = "완료" if return_code == 0 else f"종료 코드 {return_code}"
    header = f"{title} {status}"
    if session_id:
        session_store.set(ctx.channel.id, session_id)
        header += f"\n세션: `{session_id}`"

    await ctx.reply(header, mention_author=False)

    blocks = as_code_blocks(output)
    truncated = len(blocks) > MAX_REPLY_CHUNKS
    for block in blocks[:MAX_REPLY_CHUNKS]:
        await ctx.send(block)
    if truncated:
        await ctx.send("출력이 길어서 Discord에는 일부만 올렸어요. 전체 결과는 Codex 세션/로컬 로그에서 이어 확인해 주세요.")


def author_has_allowed_role(author: discord.abc.User) -> bool:
    if not settings.allowed_role_ids:
        return True
    if not isinstance(author, discord.Member):
        return False
    member_role_ids = {role.id for role in author.roles}
    return bool(member_role_ids.intersection(settings.allowed_role_ids))


async def is_allowed_message(message: discord.Message) -> bool:
    if settings.allowed_channel_ids and message.channel.id not in settings.allowed_channel_ids:
        await message.reply("이 채널에서는 Codex 봇을 사용할 수 없어요.", mention_author=False)
        return False

    if not author_has_allowed_role(message.author):
        await message.reply("이 명령을 실행할 역할 권한이 없어요.", mention_author=False)
        return False

    return True


async def is_allowed(ctx: commands.Context) -> bool:
    return await is_allowed_message(ctx.message)


async def send_codex_result(
    anchor: commands.Context | discord.Message,
    title: str,
    return_code: int,
    output: str,
    session_id: str | None,
) -> None:
    message = anchor.message if isinstance(anchor, commands.Context) else anchor
    status = "완료" if return_code == 0 else f"종료 코드 {return_code}"
    header = f"{title} {status}"
    if session_id:
        session_store.set(message.channel.id, session_id)
        header += f"\n세션: `{session_id}`"

    await message.reply(header, mention_author=False)

    blocks = as_code_blocks(output)
    truncated = len(blocks) > MAX_REPLY_CHUNKS
    for block in blocks[:MAX_REPLY_CHUNKS]:
        await message.channel.send(block)
    if truncated:
        await message.channel.send("출력이 길어서 Discord에는 일부만 올렸어요. 전체 결과는 Codex 세션/로컬 로그에서 이어 확인해 주세요.")


async def send_codex_result_to_channel(
    channel: discord.abc.Messageable,
    title: str,
    return_code: int,
    output: str,
    session_id: str | None,
) -> None:
    status = "완료" if return_code == 0 else f"종료 코드 {return_code}"
    header = f"{title} {status}"
    channel_id = getattr(channel, "id", None)
    if session_id and channel_id is not None:
        session_store.set(channel_id, session_id)
        header += f"\n세션: `{session_id}`"

    await channel.send(header)

    blocks = as_code_blocks(output)
    truncated = len(blocks) > MAX_REPLY_CHUNKS
    for block in blocks[:MAX_REPLY_CHUNKS]:
        await channel.send(block)
    if truncated:
        await channel.send("출력이 길어서 Discord에는 일부만 올렸어요. 전체 결과는 Codex 세션/로컬 로그에서 이어 확인해 주세요.")


async def run_chat_turn(message: discord.Message, prompt: str, *, force_new_session: bool = False) -> None:
    prompt = prompt.strip()
    if not prompt:
        await message.reply("무엇을 도와줄지 메시지로 말해 주세요.", mention_author=False)
        return
    if not await is_allowed_message(message):
        return

    session_id = None if force_new_session else session_store.get(message.channel.id)
    image_paths = await bridge.save_attachments(message)

    async with message.channel.typing():
        if session_id:
            return_code, output, new_session_id = await bridge.run_exec(
                message.channel.id,
                prompt,
                resume_session_id=session_id,
                image_paths=image_paths,
            )
            await send_codex_result(message, "Codex", return_code, output, new_session_id or session_id)
        else:
            return_code, output, new_session_id = await bridge.run_exec(
                message.channel.id,
                prompt,
                image_paths=image_paths,
            )
            await send_codex_result(message, "Codex", return_code, output, new_session_id)


def strip_bot_mention(content: str) -> str:
    if not bot.user:
        return content.strip()
    return re.sub(rf"<@!?{bot.user.id}>", "", content).strip()


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} | workspace={settings.workspace}")


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    ctx = await bot.get_context(message)
    if ctx.valid:
        await bot.process_commands(message)
        return

    if settings.mention_chat_enabled and bot.user and bot.user in message.mentions:
        await run_chat_turn(message, strip_bot_mention(message.content))
        return

    if settings.thread_chat_enabled and chat_channel_store.contains(message.channel.id):
        await run_chat_turn(message, message.content)


@bot.command(name="codex")
async def codex_command(ctx: commands.Context, *, prompt: str = "") -> None:
    if not prompt.strip():
        await ctx.reply(f"사용법: `{settings.prefix}codex <요청 내용>`", mention_author=False)
        return
    if not await is_allowed(ctx):
        return

    image_paths = await bridge.save_attachments(ctx.message)
    await ctx.reply("Codex 작업을 시작했어요. 끝나면 이 채널에 결과를 올릴게요.", mention_author=False)
    return_code, output, session_id = await bridge.run_exec(ctx.channel.id, prompt, image_paths=image_paths)
    await send_codex_result(ctx, "Codex 작업", return_code, output, session_id)


@bot.command(name="codex-chat")
async def codex_chat(ctx: commands.Context, *, prompt: str = "") -> None:
    if not await is_allowed(ctx):
        return

    if isinstance(ctx.channel, discord.Thread):
        chat_channel_store.add(ctx.channel.id)
        await ctx.reply("이 스레드에서 채팅 모드를 켰어요. 이제 명령어 없이 바로 말하면 됩니다.", mention_author=False)
        if prompt.strip():
            await run_chat_turn(ctx.message, prompt, force_new_session=True)
        return

    thread_name = "codex-chat"
    if prompt.strip():
        thread_name = f"codex-{prompt.strip()[:40]}"

    try:
        thread = await ctx.message.create_thread(name=thread_name, auto_archive_duration=1440)
    except (discord.Forbidden, discord.HTTPException):
        chat_channel_store.add(ctx.channel.id)
        await ctx.reply(
            "스레드를 만들 권한이 없어서 이 채널에서 채팅 모드를 켰어요. 이제 명령어 없이 바로 말하면 됩니다.",
            mention_author=False,
        )
        if prompt.strip():
            await run_chat_turn(ctx.message, prompt, force_new_session=True)
        return

    chat_channel_store.add(thread.id)
    await thread.send("채팅 모드를 시작했어요. 여기서는 명령어 없이 바로 말하면 Codex가 이어서 답합니다.")
    if prompt.strip():
        image_paths = await bridge.save_attachments(ctx.message)
        async with thread.typing():
            return_code, output, session_id = await bridge.run_exec(thread.id, prompt, image_paths=image_paths)
            await send_codex_result_to_channel(thread, "Codex", return_code, output, session_id)


@bot.command(name="codex-chat-off")
async def codex_chat_off(ctx: commands.Context) -> None:
    if not await is_allowed(ctx):
        return
    chat_channel_store.remove(ctx.channel.id)
    await ctx.reply("이 채널의 채팅 모드를 껐어요.", mention_author=False)


@bot.command(name="codex-continue", aliases=["codex-c"])
async def codex_continue(ctx: commands.Context, *, prompt: str = "") -> None:
    if not prompt.strip():
        await ctx.reply(f"사용법: `{settings.prefix}codex-continue <이어 요청할 내용>`", mention_author=False)
        return
    if not await is_allowed(ctx):
        return

    session_id = session_store.get(ctx.channel.id)
    if not session_id:
        await ctx.reply("이 채널에 저장된 Codex 세션이 없어요. 먼저 `!codex`로 새 작업을 시작해 주세요.", mention_author=False)
        return

    image_paths = await bridge.save_attachments(ctx.message)
    await ctx.reply(f"저장된 세션 `{session_id}`에 이어서 요청할게요.", mention_author=False)
    return_code, output, new_session_id = await bridge.run_exec(
        ctx.channel.id,
        prompt,
        resume_session_id=session_id,
        image_paths=image_paths,
    )
    await send_codex_result(ctx, "Codex 이어하기", return_code, output, new_session_id or session_id)


@bot.command(name="codex-resume")
async def codex_resume(ctx: commands.Context, session_id_or_url: str = "", *, prompt: str = "") -> None:
    if not session_id_or_url or not prompt.strip():
        await ctx.reply(f"사용법: `{settings.prefix}codex-resume <세션ID|Discord 메시지 URL> <요청 내용>`", mention_author=False)
        return
    if not await is_allowed(ctx):
        return

    target_channel_id = parse_discord_id_from_url(session_id_or_url)
    session_id = session_store.get(target_channel_id) if target_channel_id else session_id_or_url
    if not session_id:
        await ctx.reply("해당 Discord 채널에 저장된 Codex 세션을 찾지 못했어요.", mention_author=False)
        return

    image_paths = await bridge.save_attachments(ctx.message)
    await ctx.reply(f"세션 `{session_id}`를 이어서 실행할게요.", mention_author=False)
    return_code, output, new_session_id = await bridge.run_exec(
        ctx.channel.id,
        prompt,
        resume_session_id=session_id,
        image_paths=image_paths,
    )
    await send_codex_result(ctx, "Codex 세션 재개", return_code, output, new_session_id or session_id)


@bot.command(name="codex-review")
async def codex_review(ctx: commands.Context, *, prompt: str = "") -> None:
    if not await is_allowed(ctx):
        return

    await ctx.reply("현재 작업 트리 기준으로 Codex 리뷰를 시작할게요.", mention_author=False)
    return_code, output, _ = await bridge.run_review(ctx.channel.id, prompt.strip())
    await send_codex_result(ctx, "Codex 리뷰", return_code, output, None)


@bot.command(name="codex-cancel")
async def codex_cancel(ctx: commands.Context) -> None:
    if not await is_allowed(ctx):
        return

    if bridge.terminate(ctx.channel.id):
        await ctx.reply("이 채널에서 실행 중인 Codex 작업을 중단했어요.", mention_author=False)
    else:
        await ctx.reply("이 채널에는 실행 중인 Codex 작업이 없어요.", mention_author=False)


@bot.command(name="codex-status")
async def codex_status(ctx: commands.Context) -> None:
    active = ctx.channel.id in bridge.active_processes
    session_id = session_store.get(ctx.channel.id)
    lines = [
        f"작업 폴더: `{settings.workspace}`",
        f"실행 중: `{'yes' if active else 'no'}`",
        f"저장된 세션: `{session_id or 'none'}`",
    ]
    await ctx.reply("\n".join(lines), mention_author=False)


@bot.command(name="codex-help")
async def codex_help(ctx: commands.Context) -> None:
    prefix = settings.prefix
    text = "\n".join(
        [
            f"`{prefix}codex <요청>`: 새 Codex 작업 실행",
            f"`{prefix}codex-continue <요청>`: 이 채널의 마지막 Codex 세션에 이어 요청",
            f"`{prefix}codex-resume <세션ID|메시지URL> <요청>`: 특정 세션 재개",
            f"`{prefix}codex-review [지시문]`: 현재 변경사항 리뷰",
            f"`{prefix}codex-status`: 현재 채널 상태 확인",
            f"`{prefix}codex-cancel`: 현재 채널의 실행 중인 작업 중단",
        ]
    )
    text += "\n" + "\n".join(
        [
            f"`{prefix}codex-chat [요청]`: 대화용 스레드를 열고 채팅 모드 시작",
            f"`{prefix}codex-chat-off`: 현재 채널/스레드의 채팅 모드 끄기",
            "`@봇 요청`: 명령어 없이 바로 Codex에게 요청",
        ]
    )
    await ctx.reply(text, mention_author=False)


def main() -> None:
    try:
        bot.run(settings.token)
    except discord.LoginFailure:
        raise SystemExit(
            "Discord login failed. Check DISCORD_TOKEN in .env and reset the token in the "
            "Discord Developer Portal if needed."
        )
    except discord.PrivilegedIntentsRequired:
        raise SystemExit(
            "Discord Message Content Intent is disabled for this bot.\n"
            "Open Discord Developer Portal -> Applications -> your app -> Bot, then enable "
            "'Message Content Intent' under Privileged Gateway Intents.\n"
            "After saving the change, run python bot.py again."
        )
    except aiohttp.ClientConnectorError as exc:
        raise SystemExit(
            "Could not connect to Discord API at discord.com:443.\n"
            "On Windows, allow python.exe through your firewall/security tool, then run this "
            "from a normal PowerShell window instead of a restricted sandbox.\n"
            f"Original error: {exc}"
        )


if __name__ == "__main__":
    main()
