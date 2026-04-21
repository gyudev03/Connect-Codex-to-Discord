from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Iterable, TypeVar

import aiohttp
import discord
from discord.ext import commands


DISCORD_MESSAGE_LIMIT = 2000
MAX_REPLY_CHUNKS = 6
T = TypeVar("T")
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


def env_text_list(name: str, default: str = "") -> list[str]:
    value = os.environ.get(name, default).strip()
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    return int(value)


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value not in {"0", "false", "no", "off"}


def env_optional_int(name: str) -> int | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
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


def as_discord_messages(text: str) -> list[str]:
    clean = text.strip()
    if not clean:
        clean = "(응답이 비어 있어요.)"
    return split_text(clean, DISCORD_MESSAGE_LIMIT - 50)


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
    codex_category_id: int | None
    codex_category_name: str
    general_channel_names: list[str]
    category_chat_enabled: bool
    mention_chat_enabled: bool
    thread_chat_enabled: bool
    wake_words: list[str]
    instant_replies_enabled: bool
    slow_notice_enabled: bool
    slow_notice_seconds: int

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
            codex_category_id=env_optional_int("DISCORD_CODEX_CATEGORY_ID"),
            codex_category_name=os.environ.get("DISCORD_CODEX_CATEGORY_NAME", "Codex").strip(),
            general_channel_names=env_text_list("CODEX_GENERAL_CHANNEL_NAMES", "codex"),
            category_chat_enabled=env_bool("CATEGORY_CHAT_ENABLED", True),
            mention_chat_enabled=env_bool("MENTION_CHAT_ENABLED", True),
            thread_chat_enabled=env_bool("THREAD_CHAT_ENABLED", True),
            wake_words=env_text_list("WAKE_WORDS", "코덱스야,코덱스"),
            instant_replies_enabled=env_bool("INSTANT_REPLIES_ENABLED", True),
            slow_notice_enabled=env_bool("SLOW_NOTICE_ENABLED", True),
            slow_notice_seconds=max(1, env_int("SLOW_NOTICE_SECONDS", 12)),
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


def normalize_discord_name(value: str) -> str:
    return value.strip().casefold()


def root_channel(channel: discord.abc.Messageable) -> discord.abc.Messageable:
    parent = getattr(channel, "parent", None)
    if isinstance(channel, discord.Thread) and parent is not None:
        return parent
    return channel


def channel_category(channel: discord.abc.Messageable) -> discord.CategoryChannel | None:
    category = getattr(root_channel(channel), "category", None)
    if isinstance(category, discord.CategoryChannel):
        return category
    return None


def channel_acl_ids(channel: discord.abc.Messageable) -> set[int]:
    ids: set[int] = set()
    channel_id = getattr(channel, "id", None)
    if isinstance(channel_id, int):
        ids.add(channel_id)

    parent = getattr(channel, "parent", None)
    parent_id = getattr(parent, "id", None)
    if isinstance(parent_id, int):
        ids.add(parent_id)

    category = channel_category(channel)
    if category:
        ids.add(category.id)

    return ids


def is_in_codex_category(channel: discord.abc.Messageable) -> bool:
    if not settings.codex_category_id and not settings.codex_category_name:
        return True

    category = channel_category(channel)
    if not category:
        return False

    if settings.codex_category_id:
        return category.id == settings.codex_category_id

    return normalize_discord_name(category.name) == normalize_discord_name(settings.codex_category_name)


def is_general_codex_channel(channel: discord.abc.Messageable) -> bool:
    names = {normalize_discord_name(name) for name in settings.general_channel_names}
    channel_name = getattr(root_channel(channel), "name", "")
    return bool(channel_name) and normalize_discord_name(channel_name) in names


def channel_context_prompt(message: discord.Message, prompt: str) -> str:
    if not is_in_codex_category(message.channel):
        return prompt

    category = channel_category(message.channel)
    category_name = category.name if category else settings.codex_category_name
    parent_channel = root_channel(message.channel)
    channel_name = getattr(parent_channel, "name", str(getattr(parent_channel, "id", "unknown")))

    if is_general_codex_channel(message.channel):
        context = (
            f"Discord context: This message is from #{channel_name} in the {category_name} category. "
            "Treat it as the general Codex channel where the user may ask about anything."
        )
    else:
        context = (
            f"Discord context: This message is from project channel #{channel_name} "
            f"in the {category_name} category. Treat this channel as a persistent project named "
            f"{channel_name}; keep project-specific context, decisions, and follow-up work scoped to it."
        )

    if isinstance(message.channel, discord.Thread):
        context += f" Thread: {message.channel.name}."

    return f"{context}\n\nUser message:\n{prompt}"


def normalized_prompt(prompt: str) -> str:
    stripped = prompt.strip().lower()
    stripped = stripped.strip(" \t\r\n,，.。!！?？:：;；~")
    return re.sub(r"\s+", "", stripped)


def instant_reply(message: discord.Message, prompt: str) -> str | None:
    if not settings.instant_replies_enabled or message.attachments:
        return None

    normalized = normalized_prompt(prompt)
    if normalized in {"안녕", "안녕하세요", "하이", "ㅎㅇ", "hello", "hi"}:
        return "안녕하세요. 어떤 작업부터 같이 볼까요?"

    if normalized in {"도움", "도움말", "사용법", "명령어", "뭐할수있어", "뭘할수있어"}:
        prefix = settings.prefix
        return "\n".join(
            [
                "이렇게 말하면 돼요.",
                f"`코덱스야 <요청>`: 바로 대화하기",
                f"`{prefix}codex <요청>`: 새 작업 맡기기",
                f"`{prefix}codex-chat <요청>`: 대화용 스레드 열기",
                f"`{prefix}codex-status`: 현재 상태 보기",
            ]
        )

    if normalized in {"상태", "상태확인", "status"}:
        active = message.channel.id in bridge.active_processes
        session_id = session_store.get(message.channel.id)
        return "\n".join(
            [
                f"작업 폴더: `{settings.workspace}`",
                f"실행 중: `{'yes' if active else 'no'}`",
                f"저장된 세션: `{session_id or 'none'}`",
            ]
        )

    return None


def author_has_allowed_role(author: discord.abc.User) -> bool:
    if not settings.allowed_role_ids:
        return True
    if not isinstance(author, discord.Member):
        return False
    member_role_ids = {role.id for role in author.roles}
    return bool(member_role_ids.intersection(settings.allowed_role_ids))


async def is_allowed_message(message: discord.Message) -> bool:
    if not is_in_codex_category(message.channel):
        await message.reply("Codex 카테고리 안의 채널에서만 사용할 수 있어요.", mention_author=False)
        return False

    if settings.allowed_channel_ids and not channel_acl_ids(message.channel).intersection(settings.allowed_channel_ids):
        await message.reply("이 채널에서는 Codex 봇을 사용할 수 없어요.", mention_author=False)
        return False

    if not author_has_allowed_role(message.author):
        await message.reply("이 명령을 실행할 역할 권한이 없어요.", mention_author=False)
        return False

    return True


async def is_allowed(ctx: commands.Context) -> bool:
    return await is_allowed_message(ctx.message)


async def send_slow_notice(anchor: discord.Message | discord.abc.Messageable) -> None:
    if not settings.slow_notice_enabled:
        return

    await asyncio.sleep(settings.slow_notice_seconds)
    text = "확인할 내용이 많아서 조금 더 걸릴 수 있어요."
    if isinstance(anchor, discord.Message):
        await anchor.reply(text, mention_author=False)
    else:
        await anchor.send(text)


async def run_with_slow_notice(
    anchor: discord.Message | discord.abc.Messageable,
    awaitable: Awaitable[T],
) -> T:
    notice_task = asyncio.create_task(send_slow_notice(anchor))
    try:
        return await awaitable
    finally:
        notice_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await notice_task


async def send_codex_result(
    anchor: commands.Context | discord.Message,
    title: str,
    return_code: int,
    output: str,
    session_id: str | None,
) -> None:
    message = anchor.message if isinstance(anchor, commands.Context) else anchor
    if session_id:
        session_store.set(message.channel.id, session_id)

    if return_code != 0:
        output = f"작업 중 오류가 났어요. 종료 코드: {return_code}\n\n{output}"

    chunks = as_discord_messages(output)
    truncated = len(chunks) > MAX_REPLY_CHUNKS
    for index, chunk in enumerate(chunks[:MAX_REPLY_CHUNKS]):
        if index == 0:
            await message.reply(chunk, mention_author=False)
        else:
            await message.channel.send(chunk)
    if truncated:
        await message.channel.send("출력이 길어서 Discord에는 일부만 올렸어요. 전체 결과는 Codex 세션/로컬 로그에서 이어 확인해 주세요.")


async def send_codex_result_to_channel(
    channel: discord.abc.Messageable,
    title: str,
    return_code: int,
    output: str,
    session_id: str | None,
) -> None:
    channel_id = getattr(channel, "id", None)
    if session_id and channel_id is not None:
        session_store.set(channel_id, session_id)

    if return_code != 0:
        output = f"작업 중 오류가 났어요. 종료 코드: {return_code}\n\n{output}"

    chunks = as_discord_messages(output)
    truncated = len(chunks) > MAX_REPLY_CHUNKS
    for chunk in chunks[:MAX_REPLY_CHUNKS]:
        await channel.send(chunk)
    if truncated:
        await channel.send("출력이 길어서 Discord에는 일부만 올렸어요. 전체 결과는 Codex 세션/로컬 로그에서 이어 확인해 주세요.")


async def run_chat_turn(message: discord.Message, prompt: str, *, force_new_session: bool = False) -> None:
    prompt = prompt.strip()
    if not prompt:
        await message.reply("무엇을 도와줄지 메시지로 말해 주세요.", mention_author=False)
        return
    if not await is_allowed_message(message):
        return

    quick = instant_reply(message, prompt)
    if quick:
        await message.reply(quick, mention_author=False)
        return

    session_id = None if force_new_session else session_store.get(message.channel.id)
    image_paths = await bridge.save_attachments(message)
    codex_prompt = channel_context_prompt(message, prompt)

    async with message.channel.typing():
        if session_id:
            return_code, output, new_session_id = await run_with_slow_notice(
                message,
                bridge.run_exec(
                    message.channel.id,
                    codex_prompt,
                    resume_session_id=session_id,
                    image_paths=image_paths,
                ),
            )
            await send_codex_result(message, "Codex", return_code, output, new_session_id or session_id)
        else:
            return_code, output, new_session_id = await run_with_slow_notice(
                message,
                bridge.run_exec(
                    message.channel.id,
                    codex_prompt,
                    image_paths=image_paths,
                ),
            )
            await send_codex_result(message, "Codex", return_code, output, new_session_id)


def strip_bot_mention(content: str) -> str:
    if not bot.user:
        return content.strip()
    return re.sub(rf"<@!?{bot.user.id}>", "", content).strip()


def strip_wake_word(content: str) -> str | None:
    text = content.strip()
    text_lower = text.lower()

    for wake_word in settings.wake_words:
        wake_word_lower = wake_word.lower()
        if text_lower == wake_word_lower:
            return ""
        if not text_lower.startswith(wake_word_lower):
            continue

        rest = text[len(wake_word) :]
        if rest and rest[0] not in " \t\r\n,，.。!！?？:：;；~":
            continue
        return rest.lstrip(" \t\r\n,，.。!！?？:：;；~")

    return None


@bot.event
async def on_ready() -> None:
    category = settings.codex_category_id or settings.codex_category_name or "all"
    print(f"Logged in as {bot.user} | workspace={settings.workspace} | category={category}")


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

    wake_prompt = strip_wake_word(message.content)
    if wake_prompt is not None:
        await run_chat_turn(message, wake_prompt)
        return

    if settings.category_chat_enabled and is_in_codex_category(message.channel):
        await run_chat_turn(message, message.content)
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
    codex_prompt = channel_context_prompt(ctx.message, prompt)
    async with ctx.channel.typing():
        return_code, output, session_id = await run_with_slow_notice(
            ctx.message,
            bridge.run_exec(ctx.channel.id, codex_prompt, image_paths=image_paths),
        )
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
        codex_prompt = channel_context_prompt(ctx.message, prompt)
        async with thread.typing():
            return_code, output, session_id = await run_with_slow_notice(
                thread,
                bridge.run_exec(thread.id, codex_prompt, image_paths=image_paths),
            )
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
    codex_prompt = channel_context_prompt(ctx.message, prompt)
    async with ctx.channel.typing():
        return_code, output, new_session_id = await run_with_slow_notice(
            ctx.message,
            bridge.run_exec(
                ctx.channel.id,
                codex_prompt,
                resume_session_id=session_id,
                image_paths=image_paths,
            ),
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
    codex_prompt = channel_context_prompt(ctx.message, prompt)
    async with ctx.channel.typing():
        return_code, output, new_session_id = await run_with_slow_notice(
            ctx.message,
            bridge.run_exec(
                ctx.channel.id,
                codex_prompt,
                resume_session_id=session_id,
                image_paths=image_paths,
            ),
        )
    await send_codex_result(ctx, "Codex 세션 재개", return_code, output, new_session_id or session_id)


@bot.command(name="codex-review")
async def codex_review(ctx: commands.Context, *, prompt: str = "") -> None:
    if not await is_allowed(ctx):
        return

    async with ctx.channel.typing():
        return_code, output, _ = await run_with_slow_notice(
            ctx.message,
            bridge.run_review(ctx.channel.id, prompt.strip()),
        )
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
    category = channel_category(ctx.channel)
    if is_in_codex_category(ctx.channel) and is_general_codex_channel(ctx.channel):
        channel_mode = "general"
    elif is_in_codex_category(ctx.channel):
        channel_mode = "project"
    else:
        channel_mode = "outside-codex-category"
    lines = [
        f"작업 폴더: `{settings.workspace}`",
        f"Codex 카테고리: `{category.name if category else 'none'}`",
        f"채널 모드: `{channel_mode}`",
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
    text += "\n`코덱스야 요청`: 멘션 없이 Codex에게 요청"
    text += "\n\nCodex 카테고리 안에서는 명령어 없이 바로 대화할 수 있어요."
    text += "\n`#codex`: 일반 질문 채널"
    text += "\n그 외 Codex 카테고리 채널: 채널별 프로젝트"
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
