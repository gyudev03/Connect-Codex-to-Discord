from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import aiohttp
import discord


DISCORD_MESSAGE_LIMIT = 2000
GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_REVIEW_REQUEST_MARKER = "[[GEMINI_REVIEW_REQUEST]]"
GEMINI_REVIEW_RESULT_MARKER = "[[GEMINI_REVIEW_RESULT]]"
GEMINI_REVIEW_SKIPPED_MARKER = "[[GEMINI_REVIEW_SKIPPED]]"
COMPACT_RETRY_CHARS = 12000


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    return int(value)


def split_text(text: str, limit: int = DISCORD_MESSAGE_LIMIT - 80) -> list[str]:
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


def decode_gemini_text(data: dict[str, Any]) -> str:
    texts: list[str] = []
    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        return ""

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
    return "\n".join(texts).strip()


def is_rate_limited(status: int, data: dict[str, Any] | None) -> bool:
    if status == 429:
        return True
    if not data:
        return False
    error = data.get("error")
    if not isinstance(error, dict):
        return False
    error_text = json.dumps(error, ensure_ascii=False).lower()
    return "resource_exhausted" in error_text or "quota" in error_text or "rate" in error_text


def api_failure_summary(model: str, status: int, data: dict[str, Any] | None) -> str:
    if status == 0:
        return f"{model}: 네트워크 오류 또는 시간 초과"
    if not isinstance(data, dict):
        return f"{model}: HTTP {status}, 응답 본문을 읽지 못함"

    error = data.get("error")
    if isinstance(error, dict):
        api_status = error.get("status") or "UNKNOWN"
        message = str(error.get("message") or "").replace("\n", " ")
        if len(message) > 260:
            message = message[:260].rstrip() + "..."
        return f"{model}: HTTP {status} {api_status} - {message}"

    candidates = data.get("candidates")
    if isinstance(candidates, list) and candidates:
        reasons = []
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("finishReason"):
                reasons.append(str(candidate["finishReason"]))
        if reasons:
            return f"{model}: HTTP {status}, finishReason={', '.join(reasons)}"

    return f"{model}: HTTP {status}, 텍스트 없는 응답"


def compact_review_input(review_input: str, limit: int = COMPACT_RETRY_CHARS) -> str:
    if len(review_input) <= limit:
        return review_input
    head_limit = limit * 2 // 3
    tail_limit = limit - head_limit
    return "\n\n".join(
        [
            review_input[:head_limit].rstrip(),
            f"... 중간 내용 {len(review_input) - limit}자가 생략됨 ...",
            review_input[-tail_limit:].lstrip(),
        ]
    )


class GeminiReviewBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        load_dotenv(Path(".env"))

        self.token = os.environ.get("GEMINI_DISCORD_TOKEN", "").strip()
        self.api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        self.model = os.environ.get("GEMINI_MODEL", "gemini-2.5-pro").strip() or "gemini-2.5-pro"
        self.fallback_model = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash").strip()
        self.max_input_chars = max(1000, env_int("GEMINI_REVIEW_MAX_INPUT_CHARS", 60000))
        self.max_output_tokens = max(256, env_int("GEMINI_REVIEW_MAX_OUTPUT_TOKENS", 1200))
        self.attachment_max_bytes = max(1024, env_int("GEMINI_REVIEW_ATTACHMENT_MAX_BYTES", 1024 * 1024))

        if not self.token:
            raise RuntimeError("GEMINI_DISCORD_TOKEN is missing in .env.")
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is missing in .env.")

    async def on_ready(self) -> None:
        print(f"Gemini review bot logged in as {self.user} | model={self.model}")

    async def on_message(self, message: discord.Message) -> None:
        if self.user and message.author.id == self.user.id:
            return
        if not self.should_review(message):
            return

        async with message.channel.typing():
            review_input = await self.review_input_from_message(message)
            if not review_input:
                await message.reply(
                    f"{GEMINI_REVIEW_SKIPPED_MARKER}\n리뷰할 diff 내용을 찾지 못했어요.",
                    mention_author=False,
                )
                return

            review_text, model_used, fallback_reason, failure_reason = await self.review_with_fallback(review_input)

        if not review_text:
            await message.reply(
                f"{GEMINI_REVIEW_SKIPPED_MARKER}\n"
                "Gemini 리뷰를 생성하지 못했어요.\n"
                f"원인: {failure_reason or '알 수 없는 응답 오류'}",
                mention_author=False,
            )
            return

        header = "\n".join(
            [
                GEMINI_REVIEW_RESULT_MARKER,
                f"모델: `{model_used}`",
                f"대체 사용: `{fallback_reason}`" if fallback_reason else "대체 사용: `없음`",
                "",
            ]
        )
        full_review = header + review_text
        if len(full_review) <= DISCORD_MESSAGE_LIMIT - 80:
            await message.reply(full_review, mention_author=False)
            return

        result_dir = Path("data") / "gemini_review_results"
        result_dir.mkdir(parents=True, exist_ok=True)
        result_path = result_dir / f"{message.channel.id}-{message.id}.md"
        result_path.write_text(full_review, encoding="utf-8")
        summary = "\n".join(
            [
                header,
                "리뷰가 길어서 전체 내용을 첨부 파일로 보냅니다.",
                "Codex 봇은 이 첨부 파일을 읽어 승인 메시지와 반영 프롬프트에 사용합니다.",
            ]
        )
        await message.reply(
            summary,
            mention_author=False,
            file=discord.File(result_path),
        )

    def should_review(self, message: discord.Message) -> bool:
        return GEMINI_REVIEW_REQUEST_MARKER in message.content

    async def review_input_from_message(self, message: discord.Message) -> str:
        parts = [message.content]
        for attachment in message.attachments:
            if attachment.size > self.attachment_max_bytes:
                parts.append(f"\n[첨부 {attachment.filename}은 너무 커서 건너뜀]")
                continue
            suffix = Path(attachment.filename).suffix.lower()
            if suffix not in {".diff", ".patch", ".txt", ".md"}:
                continue
            try:
                content = await attachment.read()
            except discord.HTTPException:
                parts.append(f"\n[첨부 {attachment.filename} 다운로드 실패]")
                continue
            if b"\x00" in content:
                continue
            text = content.decode("utf-8", errors="replace")
            parts.append(f"\n\n# Attachment: {attachment.filename}\n{text}")

        review_input = "\n".join(parts).strip()
        if len(review_input) > self.max_input_chars:
            review_input = review_input[: self.max_input_chars] + "\n\n... input truncated for Gemini review ..."
        return review_input

    async def review_with_fallback(self, review_input: str) -> tuple[str, str, str | None, str | None]:
        text, status, data = await self.call_gemini(self.model, review_input)
        if text:
            return text, self.model, None, None

        failures = [api_failure_summary(self.model, status, data)]

        if self.fallback_model and self.fallback_model != self.model and is_rate_limited(status, data):
            fallback_text, fallback_status, fallback_data = await self.call_gemini(self.fallback_model, review_input)
            if fallback_text:
                return fallback_text, self.fallback_model, f"{self.model} 사용 제한", None
            failures.append(api_failure_summary(self.fallback_model, fallback_status, fallback_data))

            compact_input = compact_review_input(review_input)
            if compact_input != review_input:
                compact_text, compact_status, compact_data = await self.call_gemini(self.fallback_model, compact_input)
                if compact_text:
                    return (
                        compact_text,
                        self.fallback_model,
                        f"{self.model} 사용 제한, 긴 입력 압축 후 재시도",
                        None,
                    )
                failures.append(
                    api_failure_summary(f"{self.fallback_model} 압축 재시도", compact_status, compact_data)
                )

        return "", self.model, None, " / ".join(failures)

    async def call_gemini(self, model: str, review_input: str) -> tuple[str, int, dict[str, Any] | None]:
        prompt = "\n".join(
            [
                "You are a code reviewer in a Discord multi-bot workflow.",
                "Review only the supplied Codex change request and git diff.",
                "Focus on real bugs, behavioral regressions, security/privacy risks, and missing tests.",
                "Do not praise the code. Do not rewrite the whole diff.",
                "Respond in Korean.",
                "If there are actionable findings, use numbered items.",
                "If there are no actionable findings, say: 발견된 주요 문제 없음.",
                "",
                review_input,
            ]
        )
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": self.max_output_tokens,
            },
        }
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }
        url = f"{GEMINI_API_BASE_URL}/{model}:generateContent"

        try:
            timeout = aiohttp.ClientTimeout(total=120)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=payload) as response:
                    data = await response.json(content_type=None)
                    if response.status >= 400:
                        return "", response.status, data if isinstance(data, dict) else None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return "", 0, None

        if not isinstance(data, dict):
            return "", 200, None
        return decode_gemini_text(data), 200, data


def main() -> None:
    client = GeminiReviewBot()
    client.run(client.token)


if __name__ == "__main__":
    main()
