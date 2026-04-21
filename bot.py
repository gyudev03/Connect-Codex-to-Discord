from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Iterable, TypeVar

import aiohttp
import discord
from discord.ext import commands


DISCORD_MESSAGE_LIMIT = 2000
MAX_REPLY_CHUNKS = 6
MAX_CHANGELOG_CHUNKS = 24
PROJECT_DELETE_CONFIRM_SECONDS = 300
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
    projects_root: Path
    codex_command: str
    codex_model: str | None
    codex_args: list[str]
    timeout_seconds: int
    max_parallel_jobs: int
    allowed_channel_ids: set[int]
    allowed_role_ids: set[int]
    codex_category_id: int | None
    codex_category_name: str
    changelog_forum_id: int | None
    changelog_forum_name: str
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
        projects_root = Path(os.environ.get("CODEX_PROJECTS_ROOT", r"D:\Coding")).expanduser().resolve()
        return cls(
            token=token,
            prefix=os.environ.get("COMMAND_PREFIX", "!").strip() or "!",
            workspace=workspace,
            projects_root=projects_root,
            codex_command=os.environ.get("CODEX_COMMAND", "codex").strip() or "codex",
            codex_model=os.environ.get("CODEX_MODEL", "").strip() or None,
            codex_args=shlex.split(os.environ.get("CODEX_ARGS", "--full-auto")),
            timeout_seconds=env_int("CODEX_TIMEOUT_SECONDS", 1800),
            max_parallel_jobs=max(1, env_int("MAX_PARALLEL_CODEX_JOBS", 1)),
            allowed_channel_ids=env_list("DISCORD_ALLOWED_CHANNEL_IDS"),
            allowed_role_ids=env_list("DISCORD_ALLOWED_ROLE_IDS"),
            codex_category_id=env_optional_int("DISCORD_CODEX_CATEGORY_ID"),
            codex_category_name=os.environ.get("DISCORD_CODEX_CATEGORY_NAME", "Codex").strip(),
            changelog_forum_id=env_optional_int("DISCORD_CHANGELOG_FORUM_ID"),
            changelog_forum_name=os.environ.get("DISCORD_CHANGELOG_FORUM_NAME", "codex-수정내역").strip(),
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

    def remove(self, channel_id: int) -> None:
        self._data.pop(str(channel_id), None)
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


class ChangelogThreadStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, int] = self._load()

    def _load(self) -> dict[str, int]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}

        thread_ids: dict[str, int] = {}
        for key, value in data.items():
            try:
                thread_ids[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
        return thread_ids

    def get(self, project_name: str) -> int | None:
        return self._data.get(normalize_discord_name(project_name))

    def set(self, project_name: str, thread_id: int) -> None:
        self._data[normalize_discord_name(project_name)] = thread_id
        self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")


@dataclass
class Project:
    channel_id: int
    name: str
    slug: str
    path: Path


@dataclass
class PendingProjectDelete:
    project: Project
    requested_by_id: int
    created_at: float


@dataclass
class PendingGitAction:
    action: str
    workspace: Path
    requested_by_id: int
    commit_message: str
    created_at: float


class ProjectStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, dict[str, str]] = self._load()

    def _load(self) -> dict[str, dict[str, str]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(key): value
            for key, value in data.items()
            if isinstance(value, dict) and "path" in value
        }

    def save(self) -> None:
        self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")

    def get(self, channel_id: int) -> Project | None:
        data = self._data.get(str(channel_id))
        if not data:
            return None
        return Project(
            channel_id=channel_id,
            name=data.get("name", ""),
            slug=data.get("slug", ""),
            path=Path(data["path"]).expanduser().resolve(),
        )

    def set(self, project: Project) -> None:
        self._data[str(project.channel_id)] = {
            "name": project.name,
            "slug": project.slug,
            "path": str(project.path),
        }
        self.save()

    def remove(self, channel_id: int) -> None:
        self._data.pop(str(channel_id), None)
        self.save()

    def find_by_slug(self, slug: str) -> Project | None:
        for channel_id, data in self._data.items():
            if data.get("slug") == slug:
                return Project(
                    channel_id=int(channel_id),
                    name=data.get("name", ""),
                    slug=data.get("slug", ""),
                    path=Path(data["path"]).expanduser().resolve(),
                )
        return None


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
        workspace: Path | None = None,
    ) -> tuple[int, str, str | None]:
        workspace = (workspace or self.settings.workspace).resolve()
        with tempfile.NamedTemporaryFile(prefix="codex-last-message-", suffix=".txt", delete=False) as output_file:
            output_path = Path(output_file.name)

        args = self.base_args()
        if resume_session_id:
            args.extend(["exec", "resume", "--json", "-o", str(output_path)])
            for image_path in image_paths:
                args.extend(["--image", str(image_path)])
            args.extend([resume_session_id, "-"])
        else:
            args.extend(["exec", "-C", str(workspace), "--json", "-o", str(output_path)])
            for image_path in image_paths:
                args.extend(["--image", str(image_path)])
            args.append("-")

        return await self._run_process(channel_id, args, prompt, output_path=output_path, workspace=workspace)

    async def run_review(self, channel_id: int, prompt: str, *, workspace: Path | None = None) -> tuple[int, str, str | None]:
        workspace = (workspace or self.settings.workspace).resolve()
        args = [self.settings.codex_command, "review", "--uncommitted"]
        if prompt:
            args.append("-")
            stdin = prompt
        else:
            stdin = ""

        return_code, text, _ = await self._run_process(channel_id, args, stdin, output_path=None, workspace=workspace)
        return return_code, text, None

    async def _run_process(
        self,
        channel_id: int,
        args: list[str],
        stdin: str,
        *,
        output_path: Path | None,
        workspace: Path,
    ) -> tuple[int, str, str | None]:
        if not workspace.exists():
            return (
                1,
                "Codex 작업 폴더를 찾지 못했어요.\n"
                f"현재 작업 폴더: {workspace}\n\n"
                "프로젝트 폴더가 존재하는지 확인해 주세요.",
                None,
            )

        async with self.semaphore:
            try:
                process = await asyncio.create_subprocess_exec(
                    *args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(workspace),
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
changelog_thread_store = ChangelogThreadStore(Path("data") / "changelog_threads.json")
project_store = ProjectStore(Path("data") / "projects.json")
pending_project_deletes: dict[int, PendingProjectDelete] = {}
pending_git_actions: dict[int, PendingGitAction] = {}
GIT_CONFIRM_EMOJI = "✅"
DEFAULT_COMMIT_MESSAGE = "update from Discord Codex"
bridge = CodexBridge(settings, session_store)

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
bot = commands.Bot(command_prefix=settings.prefix, intents=intents, help_command=None)


def normalize_discord_name(value: str) -> str:
    return value.strip().casefold()


def slugify_project_name(name: str) -> str:
    slug = name.strip().lower()
    slug = re.sub(r"[\\/:*?\"<>|]", "", slug)
    slug = re.sub(r"\s+", "-", slug)
    slug = re.sub(r"[^0-9a-z가-힣_-]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-_")
    return slug[:80] or "project"


def sanitize_project_folder_name(name: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]", "", name.strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or "New Project"


def project_path_for_name(name: str) -> Path:
    folder_name = sanitize_project_folder_name(name)
    path = (settings.projects_root / folder_name).resolve()
    if not path.is_relative_to(settings.projects_root):
        raise ValueError("프로젝트 경로는 CODEX_PROJECTS_ROOT 안에 있어야 해요.")
    return path


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


def project_for_channel(channel: discord.abc.Messageable) -> Project | None:
    root = root_channel(channel)
    channel_id = getattr(root, "id", None)
    if not isinstance(channel_id, int):
        return None
    return project_store.get(channel_id)


def workspace_for_channel(channel: discord.abc.Messageable) -> Path:
    project = project_for_channel(channel)
    if project:
        return project.path
    return settings.workspace


def extract_new_project_name(prompt: str) -> str | None:
    text = prompt.strip()

    if "프로젝트" not in text:
        return None

    create_words = ("만들어줘", "생성해줘", "만들자", "만들기", "생성", "만들어")
    if not any(word in text for word in create_words):
        return None

    name_match = re.match(
        r"^(.+?)(?:이라는|라는)?\s*이름으로\s*(?:새|새로운)?\s*프로젝트(?:를|을)?\s*(?:하나|1개)?\s*(?:만들어\s*줘|만들어줘|생성해\s*줘|생성해줘|만들자|만들기|생성|만들어)\s*$",
        text,
        re.IGNORECASE,
    )
    if name_match:
        name = name_match.group(1).strip(" \"'“”‘’.。!！?？")
        return name or None

    name_first_match = re.match(
        r"^(.+?)\s*프로젝트(?:를|을)?\s*(?:새로|새롭게|새|새로운)?\s*(?:하나|1개)?\s*(?:만들어\s*줘|만들어줘|생성해\s*줘|생성해줘|만들자|만들기|생성|만들어)\s*$",
        text,
        re.IGNORECASE,
    )
    if name_first_match:
        name = name_first_match.group(1).strip(" \"'“”‘’.。!！?？")
        if name in {"새", "새로운", "새로", "하나", "1개"}:
            return None
        return name or None

    patterns = [
        r"^(?:새|새로운)\s*프로젝트(?:를|을)?\s*(?:하나|1개)?\s*[\"'“”‘’]?(.+?)[\"'“”‘’]?\s*(?:만들어\s*줘|만들어줘|생성해\s*줘|생성해줘|만들자|만들기|생성|만들어)?$",
        r"^프로젝트(?:를|을)?\s*(?:하나|1개)?\s*[\"'“”‘’]?(.+?)[\"'“”‘’]?\s*(?:만들어\s*줘|만들어줘|생성해\s*줘|생성해줘|만들자|만들기|생성|만들어)$",
    ]
    for pattern in patterns:
        match = re.match(pattern, text, re.IGNORECASE)
        if match:
            name = match.group(1).strip(" \"'“”‘’.。!！?？")
            if name in {"하나", "1개", "만들어줘", "생성해줘", "만들자", "만들기", "생성", "만들어"}:
                return None
            return name or None
    return None


def find_codex_category(guild: discord.Guild, channel: discord.abc.Messageable) -> discord.CategoryChannel | None:
    category = channel_category(channel)
    if category and is_in_codex_category(channel):
        return category
    if settings.codex_category_id:
        found = guild.get_channel(settings.codex_category_id)
        if isinstance(found, discord.CategoryChannel):
            return found
    for candidate in guild.categories:
        if normalize_discord_name(candidate.name) == normalize_discord_name(settings.codex_category_name):
            return candidate
    return None


async def find_changelog_forum(
    guild: discord.Guild,
    channel: discord.abc.Messageable,
) -> discord.ForumChannel | None:
    if settings.changelog_forum_id:
        found = guild.get_channel(settings.changelog_forum_id)
        if found is None:
            with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                found = await guild.fetch_channel(settings.changelog_forum_id)
        if isinstance(found, discord.ForumChannel):
            return found

    forum_name = normalize_discord_name(settings.changelog_forum_name)
    category = find_codex_category(guild, channel)
    if category:
        for candidate in category.forums:
            if normalize_discord_name(candidate.name) == forum_name:
                return candidate

    for candidate in guild.forums:
        if normalize_discord_name(candidate.name) == forum_name:
            return candidate

    return None


async def find_changelog_thread(forum: discord.ForumChannel, project_name: str) -> discord.Thread | None:
    thread_id = changelog_thread_store.get(project_name)
    if thread_id:
        cached = bot.get_channel(thread_id)
        if isinstance(cached, discord.Thread):
            return cached
        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            fetched = await bot.fetch_channel(thread_id)
            if isinstance(fetched, discord.Thread):
                return fetched

    normalized_name = normalize_discord_name(project_name)
    for thread in forum.threads:
        if normalize_discord_name(thread.name) == normalized_name:
            changelog_thread_store.set(project_name, thread.id)
            return thread

    with contextlib.suppress(discord.Forbidden, discord.HTTPException):
        async for thread in forum.archived_threads(limit=100):
            if normalize_discord_name(thread.name) == normalized_name:
                changelog_thread_store.set(project_name, thread.id)
                return thread

    return None


async def get_or_create_changelog_thread(
    source_channel: discord.abc.Messageable,
    project_name: str,
) -> discord.Thread | None:
    guild = getattr(source_channel, "guild", None)
    if not isinstance(guild, discord.Guild):
        return None

    forum = await find_changelog_forum(guild, source_channel)
    if forum is None:
        return None

    thread = await find_changelog_thread(forum, project_name)
    if thread:
        if thread.archived:
            with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                await thread.edit(archived=False)
        return thread

    initial_content = f"`{project_name}` 프로젝트 수정내역 포스트입니다."
    try:
        created = await forum.create_thread(
            name=project_name[:100],
            content=initial_content,
            allowed_mentions=discord.AllowedMentions.none(),
            reason=f"Create Codex changelog post for {project_name}",
        )
    except (discord.Forbidden, discord.HTTPException):
        return None

    changelog_thread_store.set(project_name, created.thread.id)
    return created.thread


async def init_git_repo(path: Path) -> str | None:
    if (path / ".git").exists():
        return None
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            "init",
            cwd=str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return "git을 찾지 못해서 git init은 건너뛰었어요."

    stdout, stderr = await process.communicate()
    if process.returncode == 0:
        return None
    detail = (stderr or stdout).decode("utf-8", errors="replace").strip()
    return f"git init에 실패했어요: {detail or process.returncode}"


async def create_project_from_message(message: discord.Message, project_name: str) -> None:
    if not await is_allowed_message(message):
        return
    if not is_general_codex_channel(message.channel):
        await message.reply("새 프로젝트는 Codex 카테고리의 일반 `codex` 채널에서 만들어 주세요.", mention_author=False)
        return
    if not isinstance(message.channel, discord.TextChannel) or not message.guild:
        await message.reply("서버 텍스트 채널에서만 프로젝트 채널을 만들 수 있어요.", mention_author=False)
        return

    category = find_codex_category(message.guild, message.channel)
    if not category:
        await message.reply("Codex 카테고리를 찾지 못했어요.", mention_author=False)
        return

    project_name = sanitize_project_folder_name(project_name)
    slug = slugify_project_name(project_name)
    existing_channel = discord.utils.get(category.text_channels, name=slug)
    existing_project = project_store.find_by_slug(slug)
    if existing_project:
        await message.reply(
            f"이미 연결된 프로젝트가 있어요.\n채널: <#{existing_project.channel_id}>\n경로: `{existing_project.path}`",
            mention_author=False,
        )
        return

    try:
        project_path = project_path_for_name(project_name)
    except ValueError as exc:
        await message.reply(str(exc), mention_author=False)
        return

    member = message.guild.me
    if existing_channel is None and member and not category.permissions_for(member).manage_channels:
        await message.reply("채널을 만들 권한이 없어요. 봇에 Manage Channels 권한을 추가해 주세요.", mention_author=False)
        return

    try:
        project_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        await message.reply(f"프로젝트 폴더를 만들지 못했어요: `{project_path}`\n{exc}", mention_author=False)
        return

    git_warning = await init_git_repo(project_path)

    if existing_channel:
        project_channel = existing_channel
    else:
        try:
            project_channel = await category.create_text_channel(
                name=slug,
                topic=f"Codex project: {project_name} | {project_path}",
                reason=f"Create Codex project channel for {project_name}",
            )
        except discord.Forbidden:
            await message.reply("채널을 만들 권한이 없어요. 봇에 Manage Channels 권한을 추가해 주세요.", mention_author=False)
            return
        except discord.HTTPException as exc:
            await message.reply(f"Discord 채널 생성에 실패했어요: {exc}", mention_author=False)
            return

    project = Project(
        channel_id=project_channel.id,
        name=project_name,
        slug=slug,
        path=project_path,
    )
    project_store.set(project)
    chat_channel_store.add(project_channel.id)

    lines = [
        f"`{project_name}` 프로젝트를 만들었어요.",
        f"채널: {project_channel.mention}",
        f"경로: `{project_path}`",
    ]
    if git_warning:
        lines.append(git_warning)
    await message.reply("\n".join(lines), mention_author=False)
    await project_channel.send(
        f"`{project_name}` 프로젝트 채널이에요.\n"
        f"이 채널의 Codex 작업 폴더는 `{project_path}`입니다.\n"
        "이제 여기서 바로 말하면 이 프로젝트 기준으로 작업합니다."
    )


def channel_context_prompt(message: discord.Message, prompt: str) -> str:
    if not is_in_codex_category(message.channel):
        return prompt

    category = channel_category(message.channel)
    category_name = category.name if category else settings.codex_category_name
    parent_channel = root_channel(message.channel)
    channel_name = getattr(parent_channel, "name", str(getattr(parent_channel, "id", "unknown")))

    project = project_for_channel(message.channel)

    if project:
        context = (
            f"Discord context: This message is from project channel #{channel_name} "
            f"in the {category_name} category. This channel is linked to local project "
            f"{project.name} at {project.path}. Keep project-specific context, decisions, "
            "and follow-up work scoped to this project."
        )
    elif is_general_codex_channel(message.channel):
        context = (
            f"Discord context: This message is from #{channel_name} in the {category_name} category. "
            "Treat it as the general Codex channel where the user may ask about anything. "
            f"Do not include long examples, stderr/stdout dumps, or detailed change logs in #{channel_name}; "
            f"project-specific change details belong in the {settings.changelog_forum_name} forum post named after the project."
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


def is_project_delete_request(prompt: str) -> bool:
    text = prompt.strip().lower()
    normalized = normalized_prompt(prompt)
    project_words = ("프로젝트", "project")
    if not any(word in normalized for word in project_words):
        return False

    blockers = (
        "삭제하면안",
        "삭제하면안돼",
        "삭제하면안되",
        "삭제하지마",
        "삭제하지말",
        "삭제말고",
        "삭제금지",
        "지우지마",
        "지우면안",
        "제거하지마",
        "제거하면안",
        "don'tdelete",
        "donotdelete",
        "shouldn'tdelete",
        "shouldnotdelete",
        "nodelete",
    )
    if any(blocker in normalized for blocker in blockers):
        return False

    delete_command_patterns = [
        r"(?:이|현재|연결된)?\s*프로젝트(?:를|을)?\s*(?:삭제|제거)\s*(?:해\s*줘|해줘|해주세요|해|하자|해라|요청|진행)",
        r"(?:이|현재|연결된)?\s*프로젝트(?:를|을)?\s*지워\s*(?:줘|라|주세요|버려)",
        r"(?:delete|remove)\s+(?:this\s+)?project",
        r"project\s+(?:delete|remove)",
    ]
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in delete_command_patterns)


def is_project_delete_confirmation(prompt: str) -> bool:
    normalized = normalized_prompt(prompt)
    return normalized in {
        "삭제확인",
        "프로젝트삭제확인",
        "확인",
        "yes",
        "y",
        "confirm",
        "deleteconfirm",
        "confirmdelete",
    }


def is_explicit_project_delete_confirmation(prompt: str) -> bool:
    normalized = normalized_prompt(prompt)
    return normalized in {
        "삭제확인",
        "프로젝트삭제확인",
        "deleteconfirm",
        "confirmdelete",
    }


def is_safe_project_delete_path(path: Path) -> bool:
    try:
        resolved = path.expanduser().resolve()
        projects_root = settings.projects_root.expanduser().resolve()
        resolved.relative_to(projects_root)
    except (OSError, ValueError):
        return False
    return resolved != projects_root


def current_project_text_channel(channel: discord.abc.Messageable) -> discord.TextChannel | None:
    root = root_channel(channel)
    if isinstance(root, discord.TextChannel):
        return root
    return None


def pending_delete_for_channel(channel: discord.abc.Messageable) -> PendingProjectDelete | None:
    root = current_project_text_channel(channel)
    if root is None:
        return None
    pending = pending_project_deletes.get(root.id)
    if pending is None:
        return None
    if time.monotonic() - pending.created_at > PROJECT_DELETE_CONFIRM_SECONDS:
        pending_project_deletes.pop(root.id, None)
        return None
    return pending


async def request_project_delete(message: discord.Message) -> None:
    if not await is_allowed_message(message):
        return

    project = project_for_channel(message.channel)
    project_channel = current_project_text_channel(message.channel)
    if not project or not project_channel or is_general_codex_channel(message.channel):
        await message.reply("프로젝트 삭제는 연결된 프로젝트 채널 안에서만 요청할 수 있어요.", mention_author=False)
        return

    if project.channel_id in bridge.active_processes:
        await message.reply(
            f"이 프로젝트 채널에서 실행 중인 Codex 작업이 있어요. 먼저 `{settings.prefix}codex-cancel`로 중단한 뒤 다시 요청해 주세요.",
            mention_author=False,
        )
        return

    if not is_safe_project_delete_path(project.path):
        await message.reply(
            f"안전하지 않은 프로젝트 경로라서 삭제하지 않을게요.\n경로: `{project.path}`\n"
            f"삭제 가능한 경로는 `{settings.projects_root}` 아래여야 합니다.",
            mention_author=False,
        )
        return

    pending_project_deletes[project_channel.id] = PendingProjectDelete(
        project=project,
        requested_by_id=message.author.id,
        created_at=time.monotonic(),
    )
    await message.reply(
        "\n".join(
            [
                "프로젝트 삭제 확인이 필요합니다.",
                f"프로젝트: `{project.name}`",
                f"채널: {project_channel.mention}",
                f"경로: `{project.path}`",
                "5분 안에 같은 사용자가 `삭제 확인`이라고 보내면 Discord 채널과 로컬 프로젝트 폴더를 삭제합니다.",
            ]
        ),
        mention_author=False,
    )


async def confirm_project_delete(message: discord.Message) -> None:
    if not await is_allowed_message(message):
        return

    pending = pending_delete_for_channel(message.channel)
    if pending is None:
        await message.reply("확인 대기 중인 프로젝트 삭제 요청이 없어요.", mention_author=False)
        return

    if pending.requested_by_id != message.author.id:
        await message.reply("삭제를 요청한 같은 사용자만 확인할 수 있어요.", mention_author=False)
        return

    project = pending.project
    project_channel = current_project_text_channel(message.channel)
    if not project_channel or project_channel.id != project.channel_id:
        await message.reply("프로젝트 채널을 확인하지 못해서 삭제를 중단했어요.", mention_author=False)
        return

    if project.channel_id in bridge.active_processes:
        await message.reply(
            f"이 프로젝트 채널에서 실행 중인 Codex 작업이 있어요. 먼저 `{settings.prefix}codex-cancel`로 중단한 뒤 다시 요청해 주세요.",
            mention_author=False,
        )
        return

    if not is_safe_project_delete_path(project.path):
        await message.reply(
            f"안전하지 않은 프로젝트 경로라서 삭제하지 않을게요.\n경로: `{project.path}`",
            mention_author=False,
        )
        return

    member = message.guild.me if message.guild else None
    if member and not project_channel.permissions_for(member).manage_channels:
        await message.reply("채널을 삭제할 권한이 없어요. 봇에 Manage Channels 권한을 추가해 주세요.", mention_author=False)
        return

    await message.reply(
        f"`{project.name}` 프로젝트 삭제를 시작합니다. 완료되면 이 채널도 삭제됩니다.",
        mention_author=False,
    )

    try:
        if project.path.exists():
            await asyncio.to_thread(shutil.rmtree, project.path)
    except OSError as exc:
        await message.channel.send(f"프로젝트 폴더 삭제에 실패해서 채널 삭제를 중단했어요.\n경로: `{project.path}`\n{exc}")
        return

    project_store.remove(project.channel_id)
    chat_channel_store.remove(project.channel_id)
    session_store.remove(project.channel_id)
    pending_project_deletes.pop(project.channel_id, None)

    try:
        await project_channel.delete(reason=f"Delete Codex project {project.name}")
    except discord.Forbidden:
        await message.channel.send("프로젝트 폴더는 삭제했지만 Discord 채널 삭제 권한이 없어요.")
    except discord.HTTPException as exc:
        await message.channel.send(f"프로젝트 폴더는 삭제했지만 Discord 채널 삭제에 실패했어요: {exc}")


async def handle_project_delete_message(message: discord.Message, prompt: str) -> bool:
    if is_project_delete_confirmation(prompt):
        pending = pending_delete_for_channel(message.channel)
        if pending is not None or is_explicit_project_delete_confirmation(prompt):
            await confirm_project_delete(message)
            return True

    if is_project_delete_request(prompt):
        await request_project_delete(message)
        return True

    return False


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
        workspace = workspace_for_channel(message.channel)
        return "\n".join(
            [
                f"작업 폴더: `{workspace}`",
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


async def run_git(workspace: Path, *args: str) -> tuple[int, str, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return 127, "", "git 실행 파일을 찾지 못했어요."
    except OSError as exc:
        return 1, "", str(exc)

    stdout, stderr = await process.communicate()
    return (
        process.returncode or 0,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


async def is_git_repo(workspace: Path) -> bool:
    code, stdout, _ = await run_git(workspace, "rev-parse", "--is-inside-work-tree")
    return code == 0 and stdout.strip() == "true"


async def git_status_short(workspace: Path) -> tuple[int, str, str]:
    return await run_git(workspace, "status", "--short")


async def git_current_branch(workspace: Path) -> str:
    code, stdout, _ = await run_git(workspace, "branch", "--show-current")
    if code == 0 and stdout:
        return stdout
    return "unknown"


def truncate_lines(text: str, max_lines: int = 12) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[:max_lines] + [f"... 외 {len(lines) - max_lines}줄"])


def git_request_from_prompt(prompt: str) -> tuple[str, str] | None:
    normalized = re.sub(r"\s+", "", prompt.strip().lower())
    if not normalized:
        return None
    if any(word in normalized for word in {"차이", "설명", "뭐야", "무엇", "뜻"}):
        return None

    wants_push = any(word in normalized for word in {"푸시해", "푸쉬해", "push해", "push", "깃허브에올려", "github에올려"})
    wants_commit = any(word in normalized for word in {"커밋해", "commit해", "commit"})
    if not wants_push and not wants_commit:
        return None

    message_match = re.search(r"(?:메시지|message|msg)\s*[:：]?\s*[\"'“”‘’]?(.+?)[\"'“”‘’]?\s*$", prompt, re.IGNORECASE)
    quote_match = re.search(r"[\"'“”‘’](.+?)[\"'“”‘’]", prompt)
    commit_message = ""
    if message_match:
        commit_message = message_match.group(1).strip()
    elif quote_match:
        commit_message = quote_match.group(1).strip()

    if not commit_message:
        commit_message = DEFAULT_COMMIT_MESSAGE

    return ("push" if wants_push else "commit", commit_message)


async def request_git_action(message: discord.Message, action: str, commit_message: str) -> None:
    if not await is_allowed_message(message):
        return

    workspace = workspace_for_channel(message.channel)
    if not workspace.exists():
        await message.reply(f"작업 폴더를 찾지 못했어요: `{workspace}`", mention_author=False)
        return

    if not await is_git_repo(workspace):
        await message.reply(f"이 작업 폴더는 Git 저장소가 아니에요: `{workspace}`", mention_author=False)
        return

    status_code, status, status_error = await git_status_short(workspace)
    if status_code != 0:
        await message.reply(f"`git status` 확인에 실패했어요.\n{status_error or status}", mention_author=False)
        return

    if action == "commit" and not status:
        await message.reply("커밋할 변경사항이 없어요.", mention_author=False)
        return

    branch = await git_current_branch(workspace)
    action_label = "커밋 후 푸시" if action == "push" else "커밋"
    status_preview = truncate_lines(status) if status else "변경사항 없음. 그래도 푸시는 진행할 수 있어요."
    confirm_message = await message.reply(
        "\n".join(
            [
                f"{action_label}을 진행할까요?",
                f"작업 폴더: `{workspace}`",
                f"브랜치: `{branch}`",
                f"커밋 메시지: `{commit_message}`",
                "",
                "변경사항:",
                f"```text\n{status_preview}\n```",
                f"{GIT_CONFIRM_EMOJI} 반응을 누르면 진행합니다.",
            ]
        ),
        mention_author=False,
    )
    try:
        await confirm_message.add_reaction(GIT_CONFIRM_EMOJI)
    except discord.HTTPException:
        await confirm_message.reply("확인 이모지를 달지 못했어요. 봇의 반응 추가 권한을 확인해 주세요.", mention_author=False)
        return

    pending_git_actions[confirm_message.id] = PendingGitAction(
        action=action,
        workspace=workspace,
        requested_by_id=message.author.id,
        commit_message=commit_message,
        created_at=time.time(),
    )


async def git_commit(workspace: Path, commit_message: str) -> tuple[bool, str]:
    if not await is_git_repo(workspace):
        return False, f"Git 저장소가 아니에요: `{workspace}`"

    status_code, status, status_error = await git_status_short(workspace)
    if status_code != 0:
        return False, f"`git status` 실패:\n{status_error or status}"
    if not status:
        return True, "커밋할 변경사항이 없어서 커밋은 건너뛰었어요."

    add_code, add_stdout, add_stderr = await run_git(workspace, "add", "-A")
    if add_code != 0:
        return False, f"`git add -A` 실패:\n{add_stderr or add_stdout}"

    commit_code, commit_stdout, commit_stderr = await run_git(workspace, "commit", "-m", commit_message)
    if commit_code != 0:
        return False, f"`git commit` 실패:\n{commit_stderr or commit_stdout}"

    hash_code, short_hash, _ = await run_git(workspace, "rev-parse", "--short", "HEAD")
    suffix = f"\n커밋: `{short_hash}`" if hash_code == 0 and short_hash else ""
    return True, f"커밋 완료: `{commit_message}`{suffix}"


async def git_push(workspace: Path, commit_message: str) -> tuple[bool, str]:
    commit_ok, commit_text = await git_commit(workspace, commit_message)
    if not commit_ok:
        return False, commit_text

    push_code, push_stdout, push_stderr = await run_git(workspace, "push")
    if push_code != 0:
        return False, f"{commit_text}\n\n`git push` 실패:\n{push_stderr or push_stdout}"

    detail = push_stdout or push_stderr or "push completed"
    return True, f"{commit_text}\n\n푸시 완료.\n```text\n{detail}\n```"


async def execute_pending_git_action(reaction: discord.Reaction, user: discord.abc.User) -> None:
    pending = pending_git_actions.pop(reaction.message.id, None)
    if not pending:
        return
    if user.id != pending.requested_by_id:
        pending_git_actions[reaction.message.id] = pending
        return
    if time.time() - pending.created_at > 600:
        await reaction.message.reply("확인 시간이 지나서 요청을 취소했어요. 다시 요청해 주세요.", mention_author=False)
        return

    async with reaction.message.channel.typing():
        if pending.action == "push":
            ok, text = await git_push(pending.workspace, pending.commit_message)
        else:
            ok, text = await git_commit(pending.workspace, pending.commit_message)

    prefix = "완료했어요." if ok else "실패했어요."
    await reaction.message.reply(f"{prefix}\n{text}", mention_author=False)


def changelog_project_name(channel: discord.abc.Messageable) -> str:
    project = project_for_channel(channel)
    if project:
        return project.name
    if is_general_codex_channel(channel):
        return settings.workspace.name
    return getattr(root_channel(channel), "name", settings.workspace.name)


def result_looks_like_change(
    message: discord.Message,
    return_code: int,
    output: str,
) -> bool:
    if return_code != 0 or "[stderr]" in output:
        return True

    prompt = message.content.casefold()
    output_text = output.casefold()
    change_words = (
        "수정",
        "변경",
        "반영",
        "추가",
        "삭제",
        "구현",
        "고쳐",
        "업데이트",
        "fix",
        "change",
        "update",
        "implement",
        "add",
        "remove",
        "delete",
        "refactor",
    )
    result_words = ("modified", "updated", "changed", "added", "removed", "수정했", "변경했", "추가했", "반영했")
    return any(word in prompt for word in change_words) or any(word in output_text for word in result_words)


def should_record_result_to_changelog(
    message: discord.Message,
    return_code: int,
    output: str,
) -> bool:
    if not is_in_codex_category(message.channel):
        return False
    if project_for_channel(message.channel):
        return True
    return result_looks_like_change(message, return_code, output)


def should_route_general_result_to_changelog(
    message: discord.Message,
    return_code: int,
    output: str,
) -> bool:
    if not is_general_codex_channel(message.channel):
        return False
    return should_record_result_to_changelog(message, return_code, output)


def general_result_notice(thread: discord.Thread | None, return_code: int) -> str:
    status = "오류 상세" if return_code != 0 else "작업 상세"
    if thread:
        return f"{status}는 {thread.mention}에 올렸어요."
    return f"{status}는 일반 `codex` 채널에 길게 올리지 않았어요. `{settings.changelog_forum_name}` 포럼을 찾거나 쓸 수 있는지 확인해 주세요."


async def post_codex_result_to_changelog(
    message: discord.Message,
    title: str,
    return_code: int,
    output: str,
) -> discord.Thread | None:
    project_name = changelog_project_name(message.channel)
    thread = await get_or_create_changelog_thread(message.channel, project_name)
    if thread is None:
        return None

    source = root_channel(message.channel)
    source_id = getattr(source, "id", None)
    source_text = f"<#{source_id}>" if isinstance(source_id, int) else getattr(source, "name", "unknown")
    status = "success" if return_code == 0 else f"exit {return_code}"
    header = "\n".join(
        [
            f"### {title}",
            f"- 채널: {source_text}",
            f"- 요청자: {message.author.display_name}",
            f"- 결과: `{status}`",
        ]
    )
    chunks = as_discord_messages(f"{header}\n\n{output}")
    for chunk in chunks[:MAX_CHANGELOG_CHUNKS]:
        await thread.send(chunk, allowed_mentions=discord.AllowedMentions.none())
    if len(chunks) > MAX_CHANGELOG_CHUNKS:
        await thread.send(
            "출력이 길어서 이 포스트에는 일부만 올렸어요. 전체 결과는 Codex 세션/로컬 로그에서 이어 확인해 주세요.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
    return thread


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

    if should_route_general_result_to_changelog(message, return_code, output):
        thread = await post_codex_result_to_changelog(message, title, return_code, output)
        await message.reply(general_result_notice(thread, return_code), mention_author=False)
        return

    if should_record_result_to_changelog(message, return_code, output):
        await post_codex_result_to_changelog(message, title, return_code, output)

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
    source_message: discord.Message | None = None,
) -> None:
    channel_id = getattr(channel, "id", None)
    if session_id and channel_id is not None:
        session_store.set(channel_id, session_id)

    if return_code != 0:
        output = f"작업 중 오류가 났어요. 종료 코드: {return_code}\n\n{output}"

    if source_message and should_record_result_to_changelog(source_message, return_code, output):
        await post_codex_result_to_changelog(source_message, title, return_code, output)

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
    workspace = workspace_for_channel(message.channel)

    async with message.channel.typing():
        if session_id:
            return_code, output, new_session_id = await run_with_slow_notice(
                message,
                bridge.run_exec(
                    message.channel.id,
                    codex_prompt,
                    resume_session_id=session_id,
                    image_paths=image_paths,
                    workspace=workspace,
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
                    workspace=workspace,
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
async def on_reaction_add(reaction: discord.Reaction, user: discord.abc.User) -> None:
    if user.bot or str(reaction.emoji) != GIT_CONFIRM_EMOJI:
        return
    await execute_pending_git_action(reaction, user)


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    ctx = await bot.get_context(message)
    if ctx.valid:
        await bot.process_commands(message)
        return

    if settings.mention_chat_enabled and bot.user and bot.user in message.mentions:
        prompt = strip_bot_mention(message.content)
        git_request = git_request_from_prompt(prompt)
        if git_request:
            await request_git_action(message, *git_request)
            return
        if await handle_project_delete_message(message, prompt):
            return
        project_name = extract_new_project_name(prompt)
        if project_name:
            await create_project_from_message(message, project_name)
            return
        await run_chat_turn(message, prompt)
        return

    wake_prompt = strip_wake_word(message.content)
    if wake_prompt is not None:
        git_request = git_request_from_prompt(wake_prompt)
        if git_request:
            await request_git_action(message, *git_request)
            return
        if await handle_project_delete_message(message, wake_prompt):
            return
        project_name = extract_new_project_name(wake_prompt)
        if project_name:
            await create_project_from_message(message, project_name)
            return
        await run_chat_turn(message, wake_prompt)
        return

    if settings.category_chat_enabled and is_in_codex_category(message.channel):
        git_request = git_request_from_prompt(message.content)
        if git_request:
            await request_git_action(message, *git_request)
            return
        if await handle_project_delete_message(message, message.content):
            return
        project_name = extract_new_project_name(message.content)
        if project_name:
            await create_project_from_message(message, project_name)
            return
        await run_chat_turn(message, message.content)
        return

    if settings.thread_chat_enabled and chat_channel_store.contains(message.channel.id):
        if await handle_project_delete_message(message, message.content):
            return
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
    workspace = workspace_for_channel(ctx.channel)
    async with ctx.channel.typing():
        return_code, output, session_id = await run_with_slow_notice(
            ctx.message,
            bridge.run_exec(ctx.channel.id, codex_prompt, image_paths=image_paths, workspace=workspace),
        )
    await send_codex_result(ctx, "Codex 작업", return_code, output, session_id)


@bot.command(name="codex-new", aliases=["codex-project"])
async def codex_new(ctx: commands.Context, *, project_name: str = "") -> None:
    project_name = project_name.strip()
    if not project_name:
        await ctx.reply(f"사용법: `{settings.prefix}codex-new <프로젝트 이름>`", mention_author=False)
        return
    await create_project_from_message(ctx.message, project_name)


@bot.command(name="codex-delete", aliases=["codex-remove"])
async def codex_delete(ctx: commands.Context, *, confirmation: str = "") -> None:
    if confirmation.strip() and is_project_delete_confirmation(confirmation):
        await confirm_project_delete(ctx.message)
        return
    await request_project_delete(ctx.message)


@bot.command(name="codex-commit")
async def codex_commit(ctx: commands.Context, *, commit_message: str = "") -> None:
    commit_message = commit_message.strip() or DEFAULT_COMMIT_MESSAGE
    await request_git_action(ctx.message, "commit", commit_message)


@bot.command(name="codex-push")
async def codex_push(ctx: commands.Context, *, commit_message: str = "") -> None:
    commit_message = commit_message.strip() or DEFAULT_COMMIT_MESSAGE
    await request_git_action(ctx.message, "push", commit_message)


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
        workspace = workspace_for_channel(thread)
        async with thread.typing():
            return_code, output, session_id = await run_with_slow_notice(
                thread,
                bridge.run_exec(thread.id, codex_prompt, image_paths=image_paths, workspace=workspace),
            )
        await send_codex_result_to_channel(thread, "Codex", return_code, output, session_id, source_message=ctx.message)


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
    workspace = workspace_for_channel(ctx.channel)
    async with ctx.channel.typing():
        return_code, output, new_session_id = await run_with_slow_notice(
            ctx.message,
            bridge.run_exec(
                ctx.channel.id,
                codex_prompt,
                resume_session_id=session_id,
                image_paths=image_paths,
                workspace=workspace,
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
    workspace = workspace_for_channel(ctx.channel)
    async with ctx.channel.typing():
        return_code, output, new_session_id = await run_with_slow_notice(
            ctx.message,
            bridge.run_exec(
                ctx.channel.id,
                codex_prompt,
                resume_session_id=session_id,
                image_paths=image_paths,
                workspace=workspace,
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
            bridge.run_review(ctx.channel.id, prompt.strip(), workspace=workspace_for_channel(ctx.channel)),
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
    workspace = workspace_for_channel(ctx.channel)
    project = project_for_channel(ctx.channel)
    if is_in_codex_category(ctx.channel) and is_general_codex_channel(ctx.channel):
        channel_mode = "general"
    elif project:
        channel_mode = f"project:{project.name}"
    elif is_in_codex_category(ctx.channel):
        channel_mode = "project"
    else:
        channel_mode = "outside-codex-category"
    lines = [
        f"작업 폴더: `{workspace}`",
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
            f"`{prefix}codex-new <프로젝트 이름>`: 프로젝트 폴더와 Discord 채널 생성",
            f"`{prefix}codex-delete`: 현재 프로젝트 채널과 로컬 프로젝트 폴더 삭제 요청",
            f"`{prefix}codex-commit <메시지>`: 확인 이모지 후 현재 채널 작업 폴더 커밋",
            f"`{prefix}codex-push <메시지>`: 확인 이모지 후 커밋하고 GitHub에 푸시",
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
    text += "\n`코덱스야 커밋해줘`, `코덱스야 푸시해줘`: 확인 이모지 후 Git 작업"
    text += "\n`코덱스야 새 프로젝트 Todo App 만들어줘`: 프로젝트 채널과 로컬 폴더 생성"
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
