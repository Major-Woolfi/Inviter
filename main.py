import asyncio
import html
import io
import json
import logging
import os
import random
import re
import signal
import sys
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any, ClassVar, Self

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramConflictError,
    TelegramNetworkError,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.callback_answer import CallbackAnswerMiddleware
from dotenv import load_dotenv
from telethon import TelegramClient, functions
from telethon import types as telethon_types
from telethon.errors import (
    AuthKeyUnregisteredError,
    BadRequestError,
    ChannelInvalidError,
    ChannelPrivateError,
    ChatAdminRequiredError,
    ChatWriteForbiddenError,
    FloodError,
    FloodWaitError,
    InputUserDeactivatedError,
    InviteRequestSentError,
    MessageDeleteForbiddenError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
    SlowModeWaitError,
    UserAlreadyParticipantError,
    UserBannedInChannelError,
    UserIsBlockedError,
    UsernameNotOccupiedError,
    UserNotParticipantError,
    UserPrivacyRestrictedError,
)
from telethon.sessions import StringSession

# --- Настройка окружения ---
BASE_DIR: Path = Path(__file__).parent
ENV_FILE: Path = BASE_DIR / ".env"
load_dotenv(ENV_FILE)

# --- Настройка логирования ---
LOGS_DIR: Path = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)
LOG_FILE: Path = LOGS_DIR / f"bot_{datetime.now(UTC).strftime('%Y-%m-%d')}.log"

logger = logging.getLogger("bot")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    file_handler: logging.FileHandler | None = None
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8", mode="a")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except (PermissionError, OSError) as e:
        logger.warning(f"Не удалось инициализировать файловый логгер: {e}")
    logger.info("=== Логгер инициализирован ===")

# --- Константы ---
ACTIVE_TASK_STATUSES: frozenset[str] = frozenset({"pending", "running", "paused"})
FINAL_TASK_STATUSES: frozenset[str] = frozenset({"completed", "cancelled", "failed"})
PLACEHOLDER_PATTERN: re.Pattern[str] = re.compile(r"\{[a-z_][a-z0-9_]*\}")


# --- Кастомные исключения ---
class BotError(Exception):
    def __init__(self, message: str, code: int = 500):
        self.message = message
        self.code = code
        super().__init__(self.message)


class ConfigError(BotError):
    def __init__(self, message: str):
        super().__init__(message, code=400)


class DatabaseError(BotError):
    def __init__(self, message: str, original_error: Exception | None = None):
        self.original_error = original_error
        super().__init__(message, code=500)


class ChatUnreachableError(BotError):
    def __init__(self, message: str):
        super().__init__(message, code=404)


class NoAvailableAccountError(BotError):
    def __init__(self, message: str):
        super().__init__(message, code=503)


class AccountWaitCancelled(BotError):
    def __init__(self, message: str = "Ожидание аккаунта отменено"):
        super().__init__(message, code=499)


# --- Утилиты маскирования секретов ---
_SENSITIVE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("BOT_TOKEN", re.compile(r"(\d{6,12}:)[A-Za-z0-9_-]{30,}")),
    ("API_HASH", re.compile(r"\b[0-9a-fA-F]{32}\b")),
    ("SESSION_STRING", re.compile(r"\b[1-9][A-Za-z0-9_-]{45,}\b")),
    ("PHONE", re.compile(r"(\+\d{2,4})\d{5,12}")),
    ("CODE", re.compile(r"(\b(?:code|phone_code|password)\s*[=:]\s*)\S+", re.IGNORECASE)),
]

_SENSITIVE_KEYS: set[str] = {
    "API_HASH",
    "BOT_TOKEN",
    "PHONE",
    "SESSION_STRING",
    "code",
    "password",
    "phone",
    "phone_code",
}


def _mask_sensitive(data: Any) -> Any:
    if data is None:
        return "None"
    if isinstance(data, (int, float, bool)):
        return str(data)
    if isinstance(data, (dict, list, tuple, set)):
        if isinstance(data, dict):
            return {
                k: "***REDACTED***" if k in _SENSITIVE_KEYS else _mask_sensitive(v)
                for k, v in data.items()
            }
        return [_mask_sensitive(item) for item in data]
    text = str(data)
    for _name, pattern in _SENSITIVE_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.re.groups else "") + "***", text)
    if len(text) > 500:
        text = text[:500] + "... [truncated]"
    return text


class _MaskingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _mask_sensitive(record.msg)
        record.args = ()
        return True


logger.addFilter(_MaskingFilter())


# --- Утилиты для работы с событиями ---
_HTML_TAG_RE: re.Pattern[str] = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    return _HTML_TAG_RE.sub("", text or "").strip()


def truncate(text: str, limit: int = 4096) -> str:
    return text if len(text) <= limit else f"{text[: limit - 1].rstrip()}…"


def log_error(func: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await func(*args, **kwargs)
        except Exception:
            logger.exception(f"Ошибка в {func.__name__}")
            raise

    return wrapper


async def safe_send_message(
    bot: Bot,
    user_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    if user_id <= 0:
        return False
    try:
        await bot.send_message(
            user_id, truncate(text), parse_mode=ParseMode.HTML, reply_markup=reply_markup
        )
        return True
    except TelegramBadRequest as e:
        error_msg = str(e).lower()
        if "blocked" in error_msg or "chat not found" in error_msg or "deactivated" in error_msg:
            return False
        try:
            await bot.send_message(
                user_id,
                truncate(strip_html(text)),
                reply_markup=reply_markup,
            )
            logger.warning(f"HTML не прошёл, отправлен plain-текст для {user_id}")
            return True
        except (TelegramBadRequest, TelegramNetworkError) as fallback_error:
            logger.error(f"safe_send_message {user_id}: {fallback_error}")
            return False
    except TelegramNetworkError as e:
        logger.error(f"safe_send_message {user_id}: {e}")
        return False


async def smart_answer(
    event: Message | CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    delete_origin: bool = False,
) -> bool:
    try:
        if isinstance(event, Message):
            await event.answer(truncate(text), reply_markup=reply_markup)
            return True
        if event.message is None:
            return False
        await event.message.answer(truncate(text), reply_markup=reply_markup)
        if delete_origin:
            try:
                await event.message.delete()
            except (TelegramBadRequest, TelegramNetworkError):
                logger.debug("Не удалось удалить исходное сообщение")
        return True
    except (TelegramBadRequest, TelegramNetworkError) as e:
        logger.error(f"smart_answer: {e}")
        return False


async def notify_owners(bot: Bot, key: str, **kwargs: Any) -> None:
    for owner_id in Config.OWNER_USER_IDS:
        language = await user_db.get_language(owner_id)
        await safe_send_message(bot, owner_id, translate(language, key, **kwargs))


# --- Retry декоратор для самоисправления ---
async def retry_async(
    func: Callable[..., Any],
    max_retries: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> Any:
    last_exception: BaseException | None = None
    for attempt in range(max_retries):
        try:
            return await func()
        except exceptions as e:
            last_exception = e
            if attempt < max_retries - 1:
                wait_time = delay * (backoff**attempt)
                logger.warning(
                    f"Повтор {attempt + 1}/{max_retries} через {wait_time:.1f}с: "
                    f"{type(e).__name__}: {e}"
                )
                await asyncio.sleep(wait_time)
            else:
                logger.exception(f"Все {max_retries} попытки исчерпаны")
    if last_exception is not None:
        raise last_exception
    raise BotError(err("retry_unknown"))


# --- Валидация данных ---
def str_to_bool(val: str) -> bool:
    return str(val).strip().lower() in ("1", "true", "yes", "y", "on")


def env_int(name: str, default: int = 0) -> int:
    raw = str(os.getenv(name, default)).strip()
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"{name}='{raw}' не число, используется {default}")
        return default


def env_float(name: str, default: float = 0.0) -> float:
    raw = str(os.getenv(name, default)).strip()
    try:
        return float(raw)
    except ValueError:
        logger.warning(f"{name}='{raw}' не число, используется {default}")
        return default


def env_int_list(name: str) -> list[int]:
    values: list[int] = []
    for raw in str(os.getenv(name, "")).replace(";", ",").split(","):
        item = raw.strip()
        if item:
            try:
                values.append(int(item))
            except ValueError:
                logger.warning(f"Некорректное значение в {name}: {item}")
    return values


def resolve_local_path(value: Any, default: str = "") -> str:
    raw = str(value if value not in (None, "") else default).strip()
    if not raw:
        return ""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    resolved = path.resolve()
    try:
        resolved.relative_to(BASE_DIR.resolve())
    except ValueError:
        return ""
    return str(resolved)


def to_int(value: Any, *, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def to_float(value: Any, *, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def parse_iso(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def is_valid_bot_token_format(token: str) -> bool:
    return re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{30,}", token.strip()) is not None


_TELEGRAM_LINK_RE: re.Pattern[str] = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me/|telegram\.me/|telegram\.dog/)"
    r"(\+?[A-Za-z0-9_/-]{2,64})",
    re.IGNORECASE,
)
_TELEGRAM_USERNAME_RE: re.Pattern[str] = re.compile(r"@([A-Za-z0-9_]{3,32})\b")
_INVITE_LINK_RE: re.Pattern[str] = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me/|telegram\.me/|telegram\.dog/)?"
    r"\+(?P<hash>[A-Za-z0-9_-]{5,64})"
)
_NON_TARGET_WORDS: frozenset[str] = frozenset(
    {"all", "channel", "bot", "support", "help", "admin", "joinchat"}
)


def split_raw_links(text: str) -> list[str]:
    return [chunk for chunk in re.split(r"[\s,;\n]+", text or "") if chunk]


def extract_telegram_links(text: str) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()
    for match in _TELEGRAM_LINK_RE.finditer(text or ""):
        link = f"https://t.me/{match.group(1)}"
        if link not in seen:
            seen.add(link)
            links.append(link)
    for match in _TELEGRAM_USERNAME_RE.finditer(text or ""):
        username = match.group(1)
        if username.lower() in _NON_TARGET_WORDS:
            continue
        link = f"https://t.me/{username}"
        if link not in seen:
            seen.add(link)
            links.append(link)
    return links


def parse_link_to_identifier(link: str) -> str | None:
    raw = str(link or "").strip()
    if not raw:
        return None
    raw = re.sub(r"^https?://", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"^(www\.)", "", raw, flags=re.IGNORECASE)
    for prefix in ("t.me/", "telegram.me/", "telegram.dog/"):
        if raw.lower().startswith(prefix):
            raw = raw[len(prefix) :]
            break
    raw = raw.split("?", 1)[0].split("#", 1)[0].strip("/")
    raw = raw.removeprefix("@")
    if not raw:
        return None
    if raw.startswith("+"):
        invite = f"https://t.me/{raw}"
        return invite if is_invite_link(invite) else None
    if raw.startswith("joinchat/"):
        return None
    if re.fullmatch(r"[A-Za-z0-9_]{3,32}", raw):
        return raw
    return None


def collect_identifier_variants(identifier: str) -> list[str]:
    raw = str(identifier or "").strip()
    variants: list[str] = []
    for candidate in (raw, parse_link_to_identifier(raw) if raw else None):
        if not candidate or candidate in variants:
            continue
        variants.append(candidate)
        bare = candidate.removeprefix("@")
        for value in (bare, f"https://t.me/{bare}"):
            if value not in variants:
                variants.append(value)
    return variants


def is_invite_link(identifier: str) -> bool:
    return bool(_INVITE_LINK_RE.fullmatch(str(identifier or "").strip()))


def build_chat_reference(username: str | None, identifier: str) -> str:
    if username:
        return f"https://t.me/{username}"
    raw = str(identifier or "").strip()
    if is_invite_link(raw) or raw.lower().startswith(("http://", "https://")):
        return raw
    bare = raw.removeprefix("@")
    if re.fullmatch(r"[A-Za-z0-9_]{3,32}", bare):
        return f"https://t.me/{bare}"
    return ""


def parse_invite_hash(identifier: str) -> str:
    match = _INVITE_LINK_RE.fullmatch(str(identifier or "").strip())
    if match is None:
        return ""
    return match.group("hash")


def format_progress_bar(percentage: float, length: int = 12) -> str:
    percent = max(0.0, min(100.0, percentage))
    filled = int(length * percent / 100)
    return f"{'🟩' * filled}{'⬜' * (length - filled)} {percent:.1f}%"


def format_accounts_summary(sessions: list[str], limit: int = 5) -> str:
    unique = list(dict.fromkeys(session for session in sessions if session))
    if not unique:
        return "-"
    shown = ", ".join(unique[:limit])
    if len(unique) > limit:
        shown += f" +{len(unique) - limit}"
    return shown


# --- Конфигурация ---
class Config:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
    API_ID: int = env_int("API_ID", 0)
    API_HASH: str = os.getenv("API_HASH", "").strip()
    OWNER_USER_IDS: list[int] = env_int_list("OWNER_USER_IDS")

    DEFAULT_LANGUAGE: str = os.getenv("DEFAULT_LANGUAGE", "ru").strip().lower() or "ru"
    LANGS_DIR: str = resolve_local_path(os.getenv("LANGS_DIR"), "langs")

    SESSIONS_DIR: str = resolve_local_path(os.getenv("SESSIONS_DIR"), "sessions")
    TASKS_DB_PATH: str = resolve_local_path(os.getenv("TASKS_DB_PATH"), "data/tasks.db")
    CHATS_DB_PATH: str = resolve_local_path(os.getenv("CHATS_DB_PATH"), "data/chats.db")
    CACHE_DB_PATH: str = resolve_local_path(os.getenv("CACHE_DB_PATH"), "data/cache.db")
    USERS_DB_PATH: str = resolve_local_path(os.getenv("USERS_DB_PATH"), "data/users.db")

    MAX_CONCURRENT_TASKS: int = env_int("MAX_CONCURRENT_TASKS", 5)
    MAX_TASKS_PER_USER: int = env_int("MAX_TASKS_PER_USER", 5)
    TASK_TIMEOUT: int = env_int("TASK_TIMEOUT", 7200)
    TASK_HISTORY_DAYS: int = env_int("TASK_HISTORY_DAYS", 7)

    MIN_WORKERS: int = env_int("MIN_WORKERS", 2)
    MAX_WORKERS: int = env_int("MAX_WORKERS", 8)
    WORKER_CHECK_INTERVAL: int = env_int("WORKER_CHECK_INTERVAL", 10)
    WORKER_IDLE_TIMEOUT: int = env_int("WORKER_IDLE_TIMEOUT", 300)

    MIN_INVITE_DELAY: int = env_int("MIN_INVITE_DELAY", 40)
    MAX_INVITE_DELAY: int = env_int("MAX_INVITE_DELAY", 70)
    HUMAN_DELAY_EVERY: int = env_int("HUMAN_DELAY_EVERY", 5)
    HUMAN_BREAK_CHANCE: float = env_float("HUMAN_BREAK_CHANCE", 0.15)
    HUMAN_BREAK_MIN: int = env_int("HUMAN_BREAK_MIN", 60)
    HUMAN_BREAK_MAX: int = env_int("HUMAN_BREAK_MAX", 300)
    SIMULATE_SKIP_RATE: float = env_float("SIMULATE_SKIP_RATE", 0.05)
    HUMAN_SKIP_DELAY_MIN: int = env_int("HUMAN_SKIP_DELAY_MIN", 10)
    HUMAN_SKIP_DELAY_MAX: int = env_int("HUMAN_SKIP_DELAY_MAX", 30)
    INVITE_BUFFER_SIZE: int = env_int("INVITE_BUFFER_SIZE", 10)
    INVITE_JITTER_MIN: float = env_float("INVITE_JITTER_MIN", 0.8)
    INVITE_JITTER_MAX: float = env_float("INVITE_JITTER_MAX", 1.2)
    POST_BUFFER_DELAY_MIN: float = env_float("POST_BUFFER_DELAY_MIN", 30)
    POST_BUFFER_DELAY_MAX: float = env_float("POST_BUFFER_DELAY_MAX", 60)
    MAX_INVITES_PER_ACCOUNT: int = env_int("MAX_INVITES_PER_ACCOUNT", 500)

    ACCOUNT_ROLLER_WAIT_TIMEOUT: float = env_float("ACCOUNT_ROLLER_WAIT_TIMEOUT", 15.0)
    INVITE_STEP_USERS: int = env_int("INVITE_STEP_USERS", 1)
    MAILING_STEP_MESSAGES: int = env_int("MAILING_STEP_MESSAGES", 1)

    ADAPTIVE_DELAY_BASE: float = env_float("ADAPTIVE_DELAY_BASE", 5.0)
    ADAPTIVE_DELAY_MAX: float = env_float("ADAPTIVE_DELAY_MAX", 120.0)
    MAX_ACCOUNT_CONSECUTIVE_ERRORS: int = env_int("MAX_ACCOUNT_CONSECUTIVE_ERRORS", 5)
    ACCOUNT_ERROR_COOLDOWN: int = env_int("ACCOUNT_ERROR_COOLDOWN", 300)

    FLOOD_WAIT_MULTIPLIER: float = env_float("FLOOD_WAIT_MULTIPLIER", 1.5)
    FLOOD_WAIT_PADDING: int = env_int("FLOOD_WAIT_PADDING", 5)
    MAX_RETRIES: int = env_int("MAX_RETRIES", 3)
    RETRY_BACKOFF_BASE: float = env_float("RETRY_BACKOFF_BASE", 2.0)
    RETRY_DEFAULT_DELAY: float = env_float("RETRY_DEFAULT_DELAY", 1.0)

    ACCOUNT_HEALTH_CHECK_INTERVAL: int = env_int("ACCOUNT_HEALTH_CHECK_INTERVAL", 300)
    ACCOUNT_CONNECT_TIMEOUT: int = env_int("ACCOUNT_CONNECT_TIMEOUT", 30)

    WORM_SCAN_LIMIT: int = env_int("WORM_SCAN_LIMIT", 500)
    WORM_MIN_DELAY: int = env_int("WORM_MIN_DELAY", 3)
    WORM_MAX_DELAY: int = env_int("WORM_MAX_DELAY", 7)
    WORM_MAX_SOURCES: int = env_int("WORM_MAX_SOURCES", 10)
    WORM_CHECK_INTERVAL: int = env_int("WORM_CHECK_INTERVAL", 25)
    WORM_SEEN_LIMIT: int = env_int("WORM_SEEN_LIMIT", 20000)

    MAILING_MIN_DELAY: int = env_int("MAILING_MIN_DELAY", 45)
    MAILING_MAX_DELAY: int = env_int("MAILING_MAX_DELAY", 80)
    MAILING_CHECK_INTERVAL: int = env_int("MAILING_CHECK_INTERVAL", 10)
    MAILING_FLOOD_WAIT_PADDING: int = env_int("MAILING_FLOOD_WAIT_PADDING", 5)
    MAILING_MAX_CONSECUTIVE_ERRORS: int = env_int("MAILING_MAX_CONSECUTIVE_ERRORS", 10)
    MAILING_CONSECUTIVE_ERROR_LONG_DELAY: int = env_int("MAILING_CONSECUTIVE_ERROR_LONG_DELAY", 30)
    MAILING_CONSECUTIVE_ERROR_SHORT_DELAY: int = env_int("MAILING_CONSECUTIVE_ERROR_SHORT_DELAY", 2)
    MAILING_MAX_TOTAL_SENDS: int = env_int("MAILING_MAX_TOTAL_SENDS", 5000)

    VALIDATOR_TEST_DELETE_WAIT: int = env_int("VALIDATOR_TEST_DELETE_WAIT", 5)
    MAX_CHATS_PER_IMPORT: int = env_int("MAX_CHATS_PER_IMPORT", 50)
    VALIDATOR_ANTI_FLOOD_DELAY_MIN: int = env_int("VALIDATOR_ANTI_FLOOD_DELAY_MIN", 3)
    VALIDATOR_ANTI_FLOOD_DELAY_MAX: int = env_int("VALIDATOR_ANTI_FLOOD_DELAY_MAX", 7)
    CHAT_ADD_THROTTLE_DELAY_MIN: float = env_float("CHAT_ADD_THROTTLE_DELAY_MIN", 1.0)
    CHAT_ADD_THROTTLE_DELAY_MAX: float = env_float("CHAT_ADD_THROTTLE_DELAY_MAX", 2.0)

    SCRAPE_MIN_MESSAGE_LIMIT: int = env_int("SCRAPE_MIN_MESSAGE_LIMIT", 50)
    SCRAPE_MAX_MESSAGE_LIMIT: int = env_int("SCRAPE_MAX_MESSAGE_LIMIT", 5000)
    SCRAPE_MIN_USER_COUNT: int = env_int("SCRAPE_MIN_USER_COUNT", 10)
    SCRAPE_MAX_USER_COUNT: int = env_int("SCRAPE_MAX_USER_COUNT", 1000)

    AUTO_LEAVE_AFTER_INVITE: bool = str_to_bool(os.getenv("AUTO_LEAVE_AFTER_INVITE", "true"))

    FEATURE_WORM_MODE: bool = str_to_bool(os.getenv("FEATURE_WORM_MODE", "true"))
    FEATURE_MAILING: bool = str_to_bool(os.getenv("FEATURE_MAILING", "true"))
    FEATURE_MAILING_TO_USERS: bool = str_to_bool(os.getenv("FEATURE_MAILING_TO_USERS", "true"))

    ENTITY_CACHE_TTL: int = env_int("ENTITY_CACHE_TTL", 300)
    FULL_CHAT_CACHE_TTL: int = env_int("FULL_CHAT_CACHE_TTL", 600)
    PARTICIPANTS_CACHE_TTL: int = env_int("PARTICIPANTS_CACHE_TTL", 600)
    INVITED_CACHE_TTL: int = env_int("INVITED_CACHE_TTL", 3600)
    ENTITY_CACHE_CLEANUP_INTERVAL: int = env_int("ENTITY_CACHE_CLEANUP_INTERVAL", 1800)
    TASK_HEALTH_CHECK_INTERVAL: int = env_int("TASK_HEALTH_CHECK_INTERVAL", 60)
    OLD_TASK_CLEANUP_INTERVAL: int = env_int("OLD_TASK_CLEANUP_INTERVAL", 86400)
    ERROR_BACKOFF_MIN: float = env_float("ERROR_BACKOFF_MIN", 5.0)
    ERROR_BACKOFF_MAX: float = env_float("ERROR_BACKOFF_MAX", 15.0)

    @classmethod
    def validate(cls) -> None:
        errors: list[str] = []
        if not cls.BOT_TOKEN:
            errors.append("BOT_TOKEN не установлен")
        elif not is_valid_bot_token_format(cls.BOT_TOKEN):
            errors.append("BOT_TOKEN имеет неверный формат (ожидается <id>:<токен>)")
        if cls.API_ID <= 0:
            errors.append("API_ID должен быть положительным числом")
        if not cls.API_HASH:
            errors.append("API_HASH не установлен")
        elif not re.fullmatch(r"[0-9a-fA-F]{32}", cls.API_HASH):
            errors.append("API_HASH имеет неверный формат (ожидается 32 hex-символа)")
        if not cls.OWNER_USER_IDS:
            errors.append("OWNER_USER_IDS не установлен")
        if cls.DEFAULT_LANGUAGE not in get_available_languages():
            errors.append(f"DEFAULT_LANGUAGE='{cls.DEFAULT_LANGUAGE}' не найден среди языков")
        if not cls.LANGS_DIR:
            errors.append("LANGS_DIR не может выходить за пределы каталога проекта")
        if not cls.SESSIONS_DIR:
            errors.append("SESSIONS_DIR не может выходить за пределы каталога проекта")
        if cls.MIN_INVITE_DELAY > cls.MAX_INVITE_DELAY:
            errors.append("MIN_INVITE_DELAY не может быть больше MAX_INVITE_DELAY")
        if cls.ACCOUNT_ROLLER_WAIT_TIMEOUT < 1:
            errors.append("ACCOUNT_ROLLER_WAIT_TIMEOUT должен быть не меньше 1 секунды")
        if cls.INVITE_STEP_USERS < 1:
            errors.append("INVITE_STEP_USERS должен быть не меньше 1")
        if cls.MAILING_STEP_MESSAGES < 1:
            errors.append("MAILING_STEP_MESSAGES должен быть не меньше 1")
        if cls.MIN_WORKERS > cls.MAX_WORKERS:
            errors.append("MIN_WORKERS не может быть больше MAX_WORKERS")
        if cls.MAX_CONCURRENT_TASKS < 1:
            errors.append("MAX_CONCURRENT_TASKS должен быть больше 0")
        if cls.MAX_TASKS_PER_USER < 1:
            errors.append("MAX_TASKS_PER_USER должен быть больше 0")
        if cls.TASK_TIMEOUT < 60:
            errors.append("TASK_TIMEOUT должен быть не меньше 60 секунд")
        if cls.HUMAN_BREAK_MIN > cls.HUMAN_BREAK_MAX:
            errors.append("HUMAN_BREAK_MIN не может быть больше HUMAN_BREAK_MAX")
        if cls.HUMAN_SKIP_DELAY_MIN > cls.HUMAN_SKIP_DELAY_MAX:
            errors.append("HUMAN_SKIP_DELAY_MIN не может быть больше HUMAN_SKIP_DELAY_MAX")
        if cls.INVITE_JITTER_MIN > cls.INVITE_JITTER_MAX:
            errors.append("INVITE_JITTER_MIN не может быть больше INVITE_JITTER_MAX")
        if cls.POST_BUFFER_DELAY_MIN > cls.POST_BUFFER_DELAY_MAX:
            errors.append("POST_BUFFER_DELAY_MIN не может быть больше POST_BUFFER_DELAY_MAX")
        if cls.WORM_MIN_DELAY > cls.WORM_MAX_DELAY:
            errors.append("WORM_MIN_DELAY не может быть больше WORM_MAX_DELAY")
        if cls.MAILING_MIN_DELAY > cls.MAILING_MAX_DELAY:
            errors.append("MAILING_MIN_DELAY не может быть больше MAILING_MAX_DELAY")
        if cls.SCRAPE_MIN_USER_COUNT > cls.SCRAPE_MAX_USER_COUNT:
            errors.append("SCRAPE_MIN_USER_COUNT не может быть больше SCRAPE_MAX_USER_COUNT")
        if cls.SCRAPE_MIN_MESSAGE_LIMIT > cls.SCRAPE_MAX_MESSAGE_LIMIT:
            errors.append("SCRAPE_MIN_MESSAGE_LIMIT не может быть больше SCRAPE_MAX_MESSAGE_LIMIT")
        if cls.ADAPTIVE_DELAY_BASE > cls.ADAPTIVE_DELAY_MAX:
            errors.append("ADAPTIVE_DELAY_BASE не может быть больше ADAPTIVE_DELAY_MAX")
        if cls.ERROR_BACKOFF_MIN > cls.ERROR_BACKOFF_MAX:
            errors.append("ERROR_BACKOFF_MIN не может быть больше ERROR_BACKOFF_MAX")
        if cls.FLOOD_WAIT_MULTIPLIER < 1.0:
            errors.append("FLOOD_WAIT_MULTIPLIER должен быть не меньше 1.0")
        if cls.VALIDATOR_ANTI_FLOOD_DELAY_MIN > cls.VALIDATOR_ANTI_FLOOD_DELAY_MAX:
            errors.append("VALIDATOR_ANTI_FLOOD_DELAY_MIN не может быть больше MAX")
        if cls.CHAT_ADD_THROTTLE_DELAY_MIN > cls.CHAT_ADD_THROTTLE_DELAY_MAX:
            errors.append("CHAT_ADD_THROTTLE_DELAY_MIN не может быть больше MAX")
        if errors:
            error_msg = "Ошибка конфигурации:\n" + "\n".join(f"  • {e}" for e in errors)
            logger.critical(error_msg)
            raise ConfigError(error_msg)


# --- Features ---
class Features:
    @staticmethod
    def auto_leave() -> bool:
        return bool(Config.AUTO_LEAVE_AFTER_INVITE)

    @staticmethod
    def human_simulation() -> bool:
        return Config.SIMULATE_SKIP_RATE > 0 or Config.HUMAN_BREAK_CHANCE > 0

    @staticmethod
    def mailing() -> bool:
        return bool(Config.FEATURE_MAILING)

    @staticmethod
    def mailing_to_users() -> bool:
        return bool(Config.FEATURE_MAILING_TO_USERS)

    @staticmethod
    def worm_mode() -> bool:
        return bool(Config.FEATURE_WORM_MODE)

    @staticmethod
    def features_dict() -> dict[str, bool]:
        return {
            "auto_leave": Features.auto_leave(),
            "human_simulation": Features.human_simulation(),
            "mailing": Features.mailing(),
            "mailing_to_users": Features.mailing_to_users(),
            "worm_mode": Features.worm_mode(),
        }

    @staticmethod
    def validate() -> list[str]:
        errors: list[str] = []
        if not Features.mailing():
            errors.append("FEATURE_MAILING=false - массовая рассылка недоступна")
        if not Features.worm_mode():
            errors.append("FEATURE_WORM_MODE=false - режим червя недоступна")
        if not Features.mailing_to_users():
            errors.append("FEATURE_MAILING_TO_USERS=false - рассылка в личные сообщения отключена")
        return errors


OWNER_USER_ID_SET: set[int] = set(Config.OWNER_USER_IDS)


def rand_range(min_value: float, max_value: float) -> float:
    low = float(min_value)
    high = float(max_value)
    if high < low:
        low, high = high, low
    return random.uniform(low, high)


def backoff_delay() -> float:
    return rand_range(Config.ERROR_BACKOFF_MIN, Config.ERROR_BACKOFF_MAX)


def is_owner_user(user_id: int) -> bool:
    return to_int(user_id) in OWNER_USER_ID_SET


# --- Языки и перевод ---
LANGS_PATH: Path = Path(Config.LANGS_DIR)
DEFAULT_LANGUAGE: str = Config.DEFAULT_LANGUAGE
_LANG_CACHE: OrderedDict[str, dict[str, Any]] = OrderedDict()
_LANG_CACHE_MAX_SIZE: int = 100
LANGUAGES: dict[str, dict[str, Any]] = {}


def _strip_bom(raw: bytes) -> bytes:
    return raw[3:] if raw[:3] == b"\xef\xbb\xbf" else raw


def load_language_file(code: str) -> dict[str, Any]:
    path = LANGS_PATH / f"{code}.json"
    try:
        return dict(json.loads(_strip_bom(path.read_bytes()).decode("utf-8")))
    except (OSError, ValueError) as e:
        logger.warning(f"Не удалось загрузить язык {code}: {e}")
        return {}


def load_languages() -> None:
    LANGUAGES.clear()
    _LANG_CACHE.clear()
    if not LANGS_PATH.exists():
        logger.error(f"Папка языков не найдена: {LANGS_PATH}")
        return
    for path in sorted(LANGS_PATH.glob("*.json")):
        data = load_language_file(path.stem)
        code = str(data.get("meta", {}).get("code", path.stem)).strip().lower() or path.stem
        LANGUAGES[code] = data
        _LANG_CACHE[code] = data
    logger.info(f"Языковая система инициализирована: {sorted(LANGUAGES)}")


def validate_languages() -> list[str]:
    errors: list[str] = []
    if DEFAULT_LANGUAGE not in LANGUAGES:
        errors.append(f"Язык по умолчанию '{DEFAULT_LANGUAGE}' не загружен")
    for code, data in LANGUAGES.items():
        meta = data.get("meta")
        if not isinstance(meta, dict):
            errors.append(f"{code}: отсутствует секция meta")
            continue
        if str(meta.get("code", "")).strip().lower() != code:
            errors.append(f"{code}: meta.code не совпадает с именем файла")
        if not str(meta.get("name", "")).strip():
            errors.append(f"{code}: пустое meta.name")
        for section in ("buttons", "texts"):
            if not isinstance(data.get(section), dict):
                errors.append(f"{code}: отсутствует секция {section}")
    return errors


def get_available_languages() -> list[str]:
    return sorted(LANGUAGES)


def get_language_display_name(code: str) -> str:
    data = LANGUAGES.get(code, {})
    name = str(data.get("meta", {}).get("name", "")).strip()
    return name or code


LANGUAGE_CONTEXT: ContextVar[str] = ContextVar("language_context", default=DEFAULT_LANGUAGE)


def _resolve_key(data: dict[str, Any], key: str) -> Any:
    node: Any = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _cached_language(code: str) -> dict[str, Any]:
    if code in _LANG_CACHE:
        _LANG_CACHE.move_to_end(code)
        return _LANG_CACHE[code]
    data = load_language_file(code)
    _LANG_CACHE[code] = data
    while len(_LANG_CACHE) > _LANG_CACHE_MAX_SIZE:
        _LANG_CACHE.popitem(last=False)
    return data


def translate(language_code: Any, key: str, **kwargs: Any) -> str:
    code = str(language_code or "").strip().lower() or DEFAULT_LANGUAGE
    data = _cached_language(code) or _cached_language(DEFAULT_LANGUAGE)
    text = _resolve_key(data, key)
    if text is None and code != DEFAULT_LANGUAGE:
        text = _resolve_key(_cached_language(DEFAULT_LANGUAGE), key)
    if text is None:
        logger.warning(f"Перевод '{key}' не найден для языка '{code}'")
        return key
    text = str(text)
    if not kwargs:
        return text
    safe_kwargs = {name: html.escape(str(value)) for name, value in kwargs.items()}
    try:
        return text.format(**safe_kwargs)
    except (KeyError, IndexError, ValueError) as e:
        logger.error(f"Ошибка подстановки в ключе '{key}': {e}")
        return text


def err(key: str, **kwargs: Any) -> str:
    return translate(LANGUAGE_CONTEXT.get(), f"texts.error_{key}", **kwargs)


# --- Модели данных ---
class Task:
    ACTIVE_STATUSES: ClassVar[frozenset[str]] = ACTIVE_TASK_STATUSES
    FINAL_STATUSES: ClassVar[frozenset[str]] = FINAL_TASK_STATUSES

    __slots__ = (
        "cancelled_at",
        "cancelled_by",
        "completed_at",
        "created_at",
        "data",
        "error",
        "paused_at",
        "progress",
        "progress_text",
        "results",
        "resumed_at",
        "sent",
        "status",
        "task_id",
        "type",
        "user_id",
    )

    def __init__(
        self,
        task_id: str,
        type: str,
        status: str,
        user_id: int,
        data: dict[str, Any] | None = None,
        results: dict[str, Any] | None = None,
        error: str = "",
        progress: float = 0.0,
        progress_text: str = "",
        sent: int = 0,
        created_at: str = "",
        completed_at: str | None = None,
        cancelled_at: str | None = None,
        cancelled_by: int | None = None,
        paused_at: str | None = None,
        resumed_at: str | None = None,
    ):
        self.task_id = task_id
        self.type = type
        self.status = status
        self.user_id = user_id
        self.data = data or {}
        self.results = results
        self.error = error
        self.progress = progress
        self.progress_text = progress_text
        self.sent = sent
        self.created_at = created_at
        self.completed_at = completed_at
        self.cancelled_at = cancelled_at
        self.cancelled_by = cancelled_by
        self.paused_at = paused_at
        self.resumed_at = resumed_at

    @property
    def is_active(self) -> bool:
        return self.status in self.ACTIVE_STATUSES

    @classmethod
    def from_row(cls, row: Any) -> Self:
        data = dict(row)
        return cls(
            task_id=str(data.get("task_id") or ""),
            type=str(data.get("type") or ""),
            status=str(data.get("status") or "pending"),
            user_id=to_int(data.get("user_id")),
            data=decode_json_object(data.get("data")),
            results=decode_json_object(data.get("results")),
            error=str(data.get("error") or ""),
            progress=to_float(data.get("progress")),
            progress_text=str(data.get("progress_text") or ""),
            sent=to_int(data.get("sent")),
            created_at=str(data.get("created_at") or ""),
            completed_at=data.get("completed_at"),
            cancelled_at=data.get("cancelled_at"),
            cancelled_by=data.get("cancelled_by"),
            paused_at=data.get("paused_at"),
            resumed_at=data.get("resumed_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "type": self.type,
            "status": self.status,
            "user_id": self.user_id,
            "data": self.data,
            "results": self.results,
            "error": self.error,
            "progress": self.progress,
            "progress_text": self.progress_text,
            "sent": self.sent,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "cancelled_at": self.cancelled_at,
            "cancelled_by": self.cancelled_by,
            "paused_at": self.paused_at,
            "resumed_at": self.resumed_at,
        }


def decode_json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


@dataclass(eq=False)
class Account:
    session_file: str
    session_string: str
    in_use: bool = False
    is_valid: bool = True
    client: TelegramClient | None = None
    last_used: datetime | None = None
    invite_count: int = 0
    consecutive_errors: int = 0
    flood_wait_until: datetime | None = None
    joined_chats: list[tuple[Any, str]] = field(default_factory=list)


@dataclass
class ChatInfo:
    chat_id: str
    chat_name: str
    chat_url: str
    chat_type: str
    user_count: int
    verified: int
    last_check: str
    status: str


@dataclass
class ValidatedChat:
    chat_id: str
    chat_name: str
    chat_url: str
    chat_type: str
    user_count: int


@dataclass
class CheckAndCleanResult:
    total: int = 0
    checked: int = 0
    added: int = 0
    removed: int = 0
    updated: int = 0
    errors: int = 0
    flood_seconds: float = 0.0
    flood_events: int = 0
    accounts_used: list[str] = field(default_factory=list)
    stop_reason: str = ""


@dataclass
class InviteResult:
    success: int = 0
    failed: int = 0
    privacy_errors: int = 0
    already_members: int = 0
    remaining: list[int] = field(default_factory=list)


@dataclass
class WormSourceStats:
    messages: int = 0
    links: int = 0
    added: int = 0
    errors: int = 0


# --- База данных SQLite ---
class SQLiteDatabase:
    def __init__(self, db_path: str):
        self.db_path: str = db_path
        self.conn: aiosqlite.Connection | None = None
        self.lock: asyncio.Lock = asyncio.Lock()

    async def connect(self) -> None:
        parent = os.path.dirname(self.db_path)
        if parent:
            Path(parent).mkdir(parents=True, exist_ok=True)

        async def _connect() -> aiosqlite.Connection:
            return await aiosqlite.connect(self.db_path, timeout=30.0)

        try:
            self.conn = await retry_async(
                _connect,
                max_retries=Config.MAX_RETRIES,
                delay=Config.RETRY_DEFAULT_DELAY,
                exceptions=(aiosqlite.Error, OSError),
            )
        except Exception as e:
            logger.critical(f"Не удалось подключиться к БД {self.db_path}: {e}")
            raise DatabaseError(err("db_connect", path=self.db_path, error=e), e) from e
        self.conn.row_factory = aiosqlite.Row
        await self._apply_pragmas()
        await self._init_db()
        logger.info(f"БД подключена: {self.db_path}")

    async def _apply_pragmas(self) -> None:
        if self.conn is None:
            return
        async with self.lock:
            for pragma in (
                "PRAGMA journal_mode = WAL",
                "PRAGMA foreign_keys = ON",
                "PRAGMA busy_timeout = 5000",
            ):
                await self.conn.execute(pragma)
            await self.conn.commit()

    async def _init_db(self) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        if self.conn is None:
            return
        try:
            await self.conn.close()
            logger.info(f"БД закрыта: {self.db_path}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Ошибка закрытия БД {self.db_path}: {e}")
        finally:
            self.conn = None


class TasksDB(SQLiteDatabase):
    _COLUMNS: ClassVar[dict[str, str]] = {
        "task_id": "TEXT PRIMARY KEY",
        "type": "TEXT NOT NULL",
        "status": "TEXT NOT NULL DEFAULT 'pending'",
        "user_id": "INTEGER NOT NULL",
        "data": "TEXT NOT NULL DEFAULT '{}'",
        "results": "TEXT",
        "error": "TEXT NOT NULL DEFAULT ''",
        "progress": "REAL NOT NULL DEFAULT 0",
        "progress_text": "TEXT NOT NULL DEFAULT ''",
        "sent": "INTEGER NOT NULL DEFAULT 0",
        "created_at": "TEXT NOT NULL DEFAULT ''",
        "completed_at": "TEXT",
        "cancelled_at": "TEXT",
        "cancelled_by": "INTEGER",
        "paused_at": "TEXT",
        "resumed_at": "TEXT",
    }
    _IMMUTABLE: ClassVar[frozenset[str]] = frozenset({"task_id", "type", "user_id", "created_at"})
    _LEGACY_DROP: ClassVar[frozenset[str]] = frozenset({"checkpoints"})

    async def _init_db(self) -> None:
        if self.conn is None:
            return
        columns_sql = ", ".join(f"{name} {spec}" for name, spec in self._COLUMNS.items())
        async with self.lock:
            try:
                await self.conn.execute(f"CREATE TABLE IF NOT EXISTS tasks ({columns_sql})")
                for name, spec in self._COLUMNS.items():
                    if name == "task_id":
                        continue
                    try:
                        await self.conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {spec}")
                    except (aiosqlite.OperationalError, aiosqlite.DatabaseError):
                        continue
                for legacy in self._LEGACY_DROP:
                    try:
                        await self.conn.execute(f"ALTER TABLE tasks DROP COLUMN {legacy}")
                    except (aiosqlite.OperationalError, aiosqlite.DatabaseError):
                        continue
                for statement in (
                    "CREATE INDEX IF NOT EXISTS idx_tasks_user_id ON tasks(user_id)",
                    "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
                    "CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at)",
                ):
                    await self.conn.execute(statement)
                await self.conn.commit()
            except Exception as e:
                logger.error(f"Ошибка инициализации БД задач: {e}")
                raise DatabaseError(err("db_init_tasks", error=e), e) from e

    async def add_task(self, task: Task) -> bool:
        if self.conn is None:
            return False
        values = task.to_dict()
        values["created_at"] = values["created_at"] or utc_now_iso()
        async with self.lock:
            try:
                await self.conn.execute(
                    "INSERT OR REPLACE INTO tasks ("
                    "task_id, type, status, user_id, data, results, error, progress, "
                    "progress_text, sent, created_at, completed_at, cancelled_at, "
                    "cancelled_by, paused_at, resumed_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task.task_id,
                        task.type,
                        task.status,
                        task.user_id,
                        json.dumps(values["data"], ensure_ascii=False),
                        json.dumps(values["results"], ensure_ascii=False)
                        if values["results"] is not None
                        else None,
                        task.error,
                        task.progress,
                        task.progress_text,
                        task.sent,
                        values["created_at"],
                        task.completed_at,
                        task.cancelled_at,
                        task.cancelled_by,
                        task.paused_at,
                        task.resumed_at,
                    ),
                )
                await self.conn.commit()
            except Exception as e:  # noqa: BLE001
                logger.error(f"add_task {task.task_id}: {e}")
                return False
        return True

    async def get_task(self, task_id: str) -> Task | None:
        if self.conn is None:
            return None
        async with self.lock:
            try:
                cursor = await self.conn.execute(
                    "SELECT * FROM tasks WHERE task_id = ?", (str(task_id),)
                )
                row = await cursor.fetchone()
            except Exception as e:  # noqa: BLE001
                logger.error(f"get_task {task_id}: {e}")
                return None
        return Task.from_row(row) if row else None

    async def update_task(self, task_id: str, updates: dict[str, Any]) -> bool:
        if self.conn is None or not updates:
            return False
        unknown = set(updates) - (set(self._COLUMNS) - self._IMMUTABLE)
        if unknown:
            logger.error(f"update_task {task_id}: неизвестные поля {sorted(unknown)}")
            return False
        async with self.lock:
            try:
                if isinstance(updates.get("data"), dict):
                    cursor = await self.conn.execute(
                        "SELECT data FROM tasks WHERE task_id = ?", (str(task_id),)
                    )
                    row = await cursor.fetchone()
                    merged = decode_json_object(row["data"]) if row else {}
                    merged.update(updates["data"])
                    updates = {**updates, "data": json.dumps(merged, ensure_ascii=False)}
                elif "data" in updates:
                    updates = {
                        **updates,
                        "data": json.dumps(updates["data"], ensure_ascii=False),
                    }
                if "results" in updates and isinstance(updates["results"], dict):
                    updates = {
                        **updates,
                        "results": json.dumps(updates["results"], ensure_ascii=False),
                    }
                set_clause = ", ".join(f"{name} = ?" for name in updates)
                values = [*updates.values(), str(task_id)]
                await self.conn.execute(f"UPDATE tasks SET {set_clause} WHERE task_id = ?", values)
                await self.conn.commit()
            except Exception as e:  # noqa: BLE001
                logger.error(f"update_task {task_id}: {e}")
                return False
        return True

    async def _fetch_tasks(self, query: str, params: tuple[Any, ...] = ()) -> list[Task]:
        if self.conn is None:
            return []
        async with self.lock:
            try:
                cursor = await self.conn.execute(query, params)
                rows = await cursor.fetchall()
            except Exception as e:  # noqa: BLE001
                logger.error(f"TasksDB fetch: {e}")
                return []
        return [Task.from_row(row) for row in rows]

    async def get_all_tasks(self) -> list[Task]:
        return await self._fetch_tasks("SELECT * FROM tasks ORDER BY created_at DESC")

    async def get_user_tasks(self, user_id: int) -> list[Task]:
        return await self._fetch_tasks(
            "SELECT * FROM tasks WHERE user_id = ? ORDER BY created_at DESC", (to_int(user_id),)
        )

    async def get_active_tasks(self) -> list[Task]:
        return await self._fetch_tasks(
            "SELECT * FROM tasks WHERE status IN ('pending', 'running', 'paused') "
            "ORDER BY created_at DESC"
        )

    async def get_stats(self) -> dict[str, int]:
        tasks = await self.get_all_tasks()
        stats: dict[str, int] = {}
        for task in tasks:
            stats[task.status] = stats.get(task.status, 0) + 1
        stats["total"] = len(tasks)
        return stats

    async def cancel_task(self, task_id: str, cancelled_by: int | None = None) -> bool:
        updates: dict[str, Any] = {
            "status": "cancelled",
            "cancelled_at": utc_now_iso(),
        }
        if cancelled_by is not None:
            updates["cancelled_by"] = to_int(cancelled_by)
        return await self.update_task(task_id, updates)

    async def cancel_all_active(self, user_id: int) -> int:
        if self.conn is None:
            return 0
        now = utc_now_iso()
        async with self.lock:
            try:
                cursor = await self.conn.execute(
                    "UPDATE tasks SET status = 'cancelled', completed_at = ?, cancelled_at = ? "
                    "WHERE user_id = ? AND status IN ('pending', 'running', 'paused')",
                    (now, now, to_int(user_id)),
                )
                await self.conn.commit()
                return max(0, cursor.rowcount)
            except Exception as e:  # noqa: BLE001
                logger.error(f"cancel_all_active {user_id}: {e}")
                return 0

    async def delete_finished_before(self, cutoff_iso: str) -> int:
        if self.conn is None:
            return 0
        async with self.lock:
            try:
                cursor = await self.conn.execute(
                    "DELETE FROM tasks WHERE status IN ('completed', 'cancelled', 'failed') "
                    "AND COALESCE(completed_at, created_at) < ?",
                    (cutoff_iso,),
                )
                await self.conn.commit()
                return max(0, cursor.rowcount)
            except Exception as e:  # noqa: BLE001
                logger.error(f"delete_finished_before: {e}")
                return 0

    async def mark_running_as_paused(self) -> int:
        if self.conn is None:
            return 0
        async with self.lock:
            try:
                cursor = await self.conn.execute(
                    "UPDATE tasks SET status = 'paused', paused_at = ? "
                    "WHERE status IN ('pending', 'running')",
                    (utc_now_iso(),),
                )
                await self.conn.commit()
                return max(0, cursor.rowcount)
            except Exception as e:  # noqa: BLE001
                logger.error(f"mark_running_as_paused: {e}")
                return 0


class ChatDB(SQLiteDatabase):
    _COLUMNS: ClassVar[tuple[str, ...]] = (
        "chat_id",
        "chat_name",
        "chat_url",
        "chat_type",
        "user_count",
        "verified",
        "last_check",
        "status",
    )
    _UPDATABLE: ClassVar[frozenset[str]] = frozenset(
        {"chat_name", "chat_url", "chat_type", "user_count", "verified", "last_check", "status"}
    )

    async def _init_db(self) -> None:
        if self.conn is None:
            return
        async with self.lock:
            try:
                await self.conn.execute(
                    "CREATE TABLE IF NOT EXISTS collected_chats ("
                    "chat_id TEXT PRIMARY KEY, chat_name TEXT NOT NULL DEFAULT '', "
                    "chat_url TEXT NOT NULL DEFAULT '', chat_type TEXT NOT NULL DEFAULT 'group', "
                    "user_count INTEGER NOT NULL DEFAULT 0, verified INTEGER NOT NULL DEFAULT 0, "
                    "last_check TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active')"
                )
                for column, spec in (
                    ("chat_name", "TEXT NOT NULL DEFAULT ''"),
                    ("chat_url", "TEXT NOT NULL DEFAULT ''"),
                    ("chat_type", "TEXT NOT NULL DEFAULT 'group'"),
                    ("user_count", "INTEGER NOT NULL DEFAULT 0"),
                    ("verified", "INTEGER NOT NULL DEFAULT 0"),
                    ("last_check", "TEXT NOT NULL DEFAULT ''"),
                    ("status", "TEXT NOT NULL DEFAULT 'active'"),
                ):
                    try:
                        await self.conn.execute(
                            f"ALTER TABLE collected_chats ADD COLUMN {column} {spec}"
                        )
                    except (aiosqlite.OperationalError, aiosqlite.DatabaseError):
                        continue
                await self.conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_chats_status "
                    "ON collected_chats(status, verified)"
                )
                await self.conn.commit()
            except Exception as e:
                logger.error(f"Ошибка инициализации БД чатов: {e}")
                raise DatabaseError(err("db_init_chats", error=e), e) from e

    async def upsert_chat(
        self,
        chat_id: str,
        chat_name: str,
        chat_url: str,
        chat_type: str,
        user_count: int,
        verified: int,
    ) -> bool:
        if self.conn is None:
            return False
        async with self.lock:
            try:
                await self.conn.execute(
                    "INSERT INTO collected_chats "
                    "(chat_id, chat_name, chat_url, chat_type, user_count, verified, "
                    "last_check, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'active') "
                    "ON CONFLICT(chat_id) DO UPDATE SET chat_name = excluded.chat_name, "
                    "chat_url = excluded.chat_url, chat_type = excluded.chat_type, "
                    "user_count = excluded.user_count, verified = excluded.verified, "
                    "last_check = excluded.last_check, status = 'active'",
                    (
                        str(chat_id),
                        str(chat_name),
                        str(chat_url),
                        str(chat_type),
                        to_int(user_count),
                        to_int(verified),
                        utc_now_iso(),
                    ),
                )
                await self.conn.commit()
            except Exception as e:  # noqa: BLE001
                logger.error(f"upsert_chat {chat_id}: {e}")
                return False
        return True

    async def update_chat(self, chat_id: str, fields: dict[str, Any]) -> bool:
        if self.conn is None or not fields:
            return False
        unknown = set(fields) - self._UPDATABLE
        if unknown:
            logger.error(f"update_chat {chat_id}: неизвестные поля {sorted(unknown)}")
            return False
        async with self.lock:
            try:
                set_clause = ", ".join(f"{name} = ?" for name in fields)
                values = [*fields.values(), str(chat_id)]
                await self.conn.execute(
                    f"UPDATE collected_chats SET {set_clause} WHERE chat_id = ?", values
                )
                await self.conn.commit()
            except Exception as e:  # noqa: BLE001
                logger.error(f"update_chat {chat_id}: {e}")
                return False
        return True

    async def delete_chat(self, chat_id: str) -> bool:
        return await self._delete("DELETE FROM collected_chats WHERE chat_id = ?", (str(chat_id),))

    async def delete_chat_by_identifier(self, identifier: str) -> bool:
        variants = collect_identifier_variants(identifier)
        if not variants:
            return False
        placeholders = ",".join(["?"] * len(variants))
        return await self._delete(
            f"DELETE FROM collected_chats "
            f"WHERE chat_id IN ({placeholders}) OR chat_url IN ({placeholders})",
            (*variants, *variants),
        )

    async def _delete(self, query: str, params: tuple[Any, ...]) -> bool:
        if self.conn is None:
            return False
        async with self.lock:
            try:
                cursor = await self.conn.execute(query, params)
                await self.conn.commit()
                return cursor.rowcount > 0
            except Exception as e:  # noqa: BLE001
                logger.error(f"ChatDB delete: {e}")
                return False

    async def get_chat(self, chat_id: str) -> ChatInfo | None:
        if self.conn is None:
            return None
        async with self.lock:
            try:
                cursor = await self.conn.execute(
                    "SELECT * FROM collected_chats WHERE chat_id = ?", (str(chat_id),)
                )
                row = await cursor.fetchone()
            except Exception as e:  # noqa: BLE001
                logger.error(f"get_chat {chat_id}: {e}")
                return None
        return self._to_info(row) if row else None

    async def get_all_chats(self, verified_only: bool = False) -> list[ChatInfo]:
        if self.conn is None:
            return []
        query = "SELECT * FROM collected_chats WHERE status = 'active'"
        if verified_only:
            query += " AND verified = 1"
        query += " ORDER BY user_count DESC"
        async with self.lock:
            try:
                cursor = await self.conn.execute(query)
                rows = await cursor.fetchall()
            except Exception as e:  # noqa: BLE001
                logger.error(f"get_all_chats: {e}")
                return []
        return [self._to_info(row) for row in rows]

    async def get_total_users(self, verified_only: bool = False) -> int:
        if self.conn is None:
            return 0
        query = (
            "SELECT COALESCE(SUM(user_count), 0) AS total FROM collected_chats "
            "WHERE status = 'active'"
        )
        if verified_only:
            query += " AND verified = 1"
        async with self.lock:
            try:
                cursor = await self.conn.execute(query)
                row = await cursor.fetchone()
            except Exception as e:  # noqa: BLE001
                logger.error(f"get_total_users: {e}")
                return 0
        return to_int(row["total"]) if row else 0

    async def get_active_chats_count(self, verified_only: bool = False) -> int:
        return len(await self.get_all_chats(verified_only=verified_only))

    async def clear(self) -> int:
        return await self._delete("DELETE FROM collected_chats", ())

    @staticmethod
    def _to_info(row: Any) -> ChatInfo:
        return ChatInfo(
            chat_id=str(row["chat_id"] or ""),
            chat_name=str(row["chat_name"] or ""),
            chat_url=str(row["chat_url"] or ""),
            chat_type=str(row["chat_type"] or "group"),
            user_count=to_int(row["user_count"]),
            verified=to_int(row["verified"]),
            last_check=str(row["last_check"] or ""),
            status=str(row["status"] or "active"),
        )


class CacheManager(SQLiteDatabase):
    def __init__(self, db_path: str):
        super().__init__(db_path)
        self._participants: dict[str, tuple[list[int], float]] = {}
        self._invited: dict[str, tuple[bool, float]] = {}

    async def _init_db(self) -> None:
        if self.conn is None:
            return
        async with self.lock:
            try:
                await self.conn.execute(
                    "CREATE TABLE IF NOT EXISTS chat_participants ("
                    "chat_id TEXT NOT NULL, user_id INTEGER NOT NULL, "
                    "cached_at TEXT NOT NULL DEFAULT '', PRIMARY KEY (chat_id, user_id))"
                )
                await self.conn.execute(
                    "CREATE TABLE IF NOT EXISTS invited_users ("
                    "chat_id TEXT NOT NULL, user_id INTEGER NOT NULL, task_id TEXT, "
                    "invited_at TEXT NOT NULL DEFAULT '', PRIMARY KEY (chat_id, user_id))"
                )
                await self.conn.commit()
            except Exception as e:
                logger.error(f"Ошибка инициализации БД кэша: {e}")
                raise DatabaseError(err("db_init_cache", error=e), e) from e

    async def cache_participants(self, chat_id: str, user_ids: list[int]) -> None:
        if self.conn is None:
            return
        now = utc_now_iso()
        async with self.lock:
            try:
                await self.conn.execute(
                    "DELETE FROM chat_participants WHERE chat_id = ?", (str(chat_id),)
                )
                await self.conn.executemany(
                    "INSERT OR IGNORE INTO chat_participants (chat_id, user_id, cached_at) "
                    "VALUES (?, ?, ?)",
                    [(str(chat_id), int(uid), now) for uid in user_ids],
                )
                await self.conn.commit()
            except Exception as e:  # noqa: BLE001
                logger.error(f"cache_participants {chat_id}: {e}")
                return
        self._participants[str(chat_id)] = (
            list(user_ids),
            datetime.now(UTC).timestamp() + Config.PARTICIPANTS_CACHE_TTL,
        )

    async def get_cached_participants(self, chat_id: str) -> list[int]:
        key = str(chat_id)
        now = datetime.now(UTC).timestamp()
        cached = self._participants.get(key)
        if cached and cached[1] > now:
            return list(cached[0])
        if self.conn is None:
            return []
        async with self.lock:
            try:
                cursor = await self.conn.execute(
                    "SELECT user_id FROM chat_participants WHERE chat_id = ?", (key,)
                )
                rows = await cursor.fetchall()
            except Exception as e:  # noqa: BLE001
                logger.error(f"get_cached_participants {chat_id}: {e}")
                return []
        result = [to_int(row["user_id"]) for row in rows]
        self._participants[key] = (result, now + Config.PARTICIPANTS_CACHE_TTL)
        return result

    async def clear_participants(self, chat_id: str | None = None) -> int:
        self._participants.clear()
        if self.conn is None:
            return 0
        query = "DELETE FROM chat_participants"
        params: tuple[Any, ...] = ()
        if chat_id is not None:
            query += " WHERE chat_id = ?"
            params = (str(chat_id),)
        async with self.lock:
            try:
                cursor = await self.conn.execute(query, params)
                await self.conn.commit()
                return max(0, cursor.rowcount)
            except Exception as e:  # noqa: BLE001
                logger.error(f"clear_participants: {e}")
                return 0

    async def mark_invited(self, chat_id: str, user_id: int, task_id: str | None = None) -> None:
        key = f"{chat_id}:{user_id}"
        if self.conn is None:
            self._invited[key] = (
                True,
                datetime.now(UTC).timestamp() + Config.INVITED_CACHE_TTL,
            )
            return
        async with self.lock:
            try:
                await self.conn.execute(
                    "INSERT OR IGNORE INTO invited_users (chat_id, user_id, task_id, invited_at) "
                    "VALUES (?, ?, ?, ?)",
                    (str(chat_id), to_int(user_id), task_id, utc_now_iso()),
                )
                await self.conn.commit()
            except Exception as e:  # noqa: BLE001
                logger.error(f"mark_invited {key}: {e}")
                return
        self._invited[key] = (
            True,
            datetime.now(UTC).timestamp() + Config.INVITED_CACHE_TTL,
        )

    async def is_invited(self, chat_id: str, user_id: int) -> bool:
        key = f"{chat_id}:{user_id}"
        now = datetime.now(UTC).timestamp()
        cached = self._invited.get(key)
        if cached and cached[1] > now:
            return cached[0]
        if self.conn is None:
            return False
        async with self.lock:
            try:
                cursor = await self.conn.execute(
                    "SELECT 1 FROM invited_users WHERE chat_id = ? AND user_id = ?",
                    (str(chat_id), to_int(user_id)),
                )
                row = await cursor.fetchone()
            except Exception as e:  # noqa: BLE001
                logger.error(f"is_invited {key}: {e}")
                return False
        result = row is not None
        self._invited[key] = (result, now + Config.INVITED_CACHE_TTL)
        return result

    async def clear(self) -> int:
        self._invited.clear()
        removed = await self.clear_participants()
        if self.conn is None:
            return removed
        async with self.lock:
            try:
                cursor = await self.conn.execute("DELETE FROM invited_users")
                await self.conn.commit()
                return removed + max(0, cursor.rowcount)
            except Exception as e:  # noqa: BLE001
                logger.error(f"clear invited cache: {e}")
                return removed


class UserDB(SQLiteDatabase):
    async def _init_db(self) -> None:
        if self.conn is None:
            return
        async with self.lock:
            try:
                await self.conn.execute(
                    "CREATE TABLE IF NOT EXISTS users ("
                    "user_id INTEGER PRIMARY KEY, join_date TEXT NOT NULL DEFAULT '', "
                    "language TEXT NOT NULL DEFAULT '')"
                )
                await self.conn.execute(
                    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)"
                )
                await self.conn.commit()
            except Exception as e:
                logger.error(f"Ошибка инициализации БД пользователей: {e}")
                raise DatabaseError(err("db_init_users", error=e), e) from e

    async def ensure_user(self, user_id: int) -> bool:
        if self.conn is None:
            return False
        async with self.lock:
            try:
                await self.conn.execute(
                    "INSERT OR IGNORE INTO users (user_id, join_date, language) VALUES (?, ?, ?)",
                    (to_int(user_id), utc_now_iso(), DEFAULT_LANGUAGE),
                )
                await self.conn.commit()
            except Exception as e:  # noqa: BLE001
                logger.error(f"ensure_user {user_id}: {e}")
                return False
        return True

    async def get_language(self, user_id: int) -> str:
        if self.conn is None:
            return DEFAULT_LANGUAGE
        async with self.lock:
            try:
                cursor = await self.conn.execute(
                    "SELECT language FROM users WHERE user_id = ?", (to_int(user_id),)
                )
                row = await cursor.fetchone()
            except Exception as e:  # noqa: BLE001
                logger.error(f"get_language {user_id}: {e}")
                return DEFAULT_LANGUAGE
        code = str(row["language"] or "").strip().lower() if row else ""
        return code if code in LANGUAGES else DEFAULT_LANGUAGE

    async def set_language(self, user_id: int, language: str) -> bool:
        code = str(language).strip().lower()
        if code not in LANGUAGES:
            return False
        if await self.ensure_user(user_id) is False:
            return False
        if self.conn is None:
            return False
        async with self.lock:
            try:
                await self.conn.execute(
                    "UPDATE users SET language = ? WHERE user_id = ?",
                    (code, to_int(user_id)),
                )
                await self.conn.commit()
            except Exception as e:  # noqa: BLE001
                logger.error(f"set_language {user_id}: {e}")
                return False
        return True


# --- Telegram: клиент, кэш сущностей, вход и выход ---
DEVICE_MODEL: str = "Inviter"
SYSTEM_VERSION: str = "Linux"
APP_VERSION: str = "4.16.8"


def create_telegram_client(session_string: str | None = None) -> TelegramClient:
    session = StringSession(session_string) if session_string else StringSession()
    return TelegramClient(
        session,
        Config.API_ID,
        Config.API_HASH,
        device_model=DEVICE_MODEL,
        system_version=SYSTEM_VERSION,
        app_version=APP_VERSION,
        system_lang_code=Config.DEFAULT_LANGUAGE,
        lang_code=Config.DEFAULT_LANGUAGE,
        catch_up=False,
        connection_retries=3,
        retry_delay=2,
    )


class EntityCache:
    def __init__(self, ttl: int):
        self._ttl: int = ttl
        self._cache: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._inflight: dict[str, asyncio.Future[Any | None]] = {}
        self._lock: asyncio.Lock = asyncio.Lock()

    def _key(self, owner: str, identifier: int | str) -> str:
        return f"{owner}:{identifier}"

    async def get(self, owner: str, client: TelegramClient, identifier: int | str) -> Any | None:
        key = self._key(owner, identifier)
        now = datetime.now(UTC).timestamp()
        async with self._lock:
            entry = self._cache.get(key)
            if entry and entry[1] > now:
                self._cache.move_to_end(key)
                return entry[0]
            future = self._inflight.get(key)
            if future is None:
                future = asyncio.ensure_future(self._fetch(client, identifier, key))
                self._inflight[key] = future
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            raise
        except FloodWaitError:
            async with self._lock:
                self._inflight.pop(key, None)
            raise
        except (ChatUnreachableError, ValueError, AuthKeyUnregisteredError):
            async with self._lock:
                self._inflight.pop(key, None)
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Не удалось получить сущность {identifier}: {type(e).__name__}: {e}")
            return None

    async def _fetch(self, client: TelegramClient, identifier: int | str, key: str) -> Any | None:
        try:
            entity = await client.get_entity(identifier)
        except BaseException:
            async with self._lock:
                self._inflight.pop(key, None)
            raise
        async with self._lock:
            self._cache[key] = (entity, datetime.now(UTC).timestamp() + self._ttl)
            self._cache.move_to_end(key)
            self._inflight.pop(key, None)
        return entity

    async def invalidate(self, owner: str, identifier: int | str) -> None:
        async with self._lock:
            self._cache.pop(self._key(owner, identifier), None)

    async def prune_expired(self) -> int:
        now = datetime.now(UTC).timestamp()
        async with self._lock:
            expired = [key for key, (_, expire_at) in self._cache.items() if expire_at <= now]
            for key in expired:
                self._cache.pop(key, None)
            return len(expired)

    async def clear(self) -> int:
        async with self._lock:
            count = len(self._cache)
            self._cache.clear()
            return count


class FullChatCache:
    def __init__(self, ttl: int):
        self._ttl: int = ttl
        self._cache: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._lock: asyncio.Lock = asyncio.Lock()

    async def get(self, owner: str, identifier: int | str) -> Any | None:
        key = f"{owner}:{identifier}"
        async with self._lock:
            entry = self._cache.get(key)
            if entry and entry[1] > datetime.now(UTC).timestamp():
                return entry[0]
        return None

    async def set(self, owner: str, identifier: int | str, value: Any) -> None:
        key = f"{owner}:{identifier}"
        async with self._lock:
            self._cache[key] = (value, datetime.now(UTC).timestamp() + self._ttl)

    async def invalidate(self, owner: str, identifier: int | str) -> None:
        async with self._lock:
            self._cache.pop(f"{owner}:{identifier}", None)

    async def prune_expired(self) -> int:
        now = datetime.now(UTC).timestamp()
        async with self._lock:
            expired = [key for key, (_, expire_at) in self._cache.items() if expire_at <= now]
            for key in expired:
                self._cache.pop(key, None)
            return len(expired)

    async def clear(self) -> int:
        async with self._lock:
            count = len(self._cache)
            self._cache.clear()
            return count


entity_cache: EntityCache = EntityCache(Config.ENTITY_CACHE_TTL)
full_chat_cache: FullChatCache = FullChatCache(Config.FULL_CHAT_CACHE_TTL)


def require_client(account: Account) -> TelegramClient:
    client = account.client
    if client is None:
        raise NoAvailableAccountError(err("account_not_connected", session=account.session_file))
    return client


async def get_cached_entity(account: Account, identifier: int | str) -> Any | None:
    client = account.client
    if client is None:
        return None
    try:
        return await entity_cache.get(account.session_file, client, identifier)
    except FloodWaitError:
        raise
    except AuthKeyUnregisteredError:
        account.is_valid = False
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Не удалось получить сущность {identifier}: {type(e).__name__}: {e}")
        return None


async def get_full_chat(account: Account, identifier: int | str) -> Any | None:
    cached = await full_chat_cache.get(account.session_file, identifier)
    if cached is not None:
        return cached
    entity = await get_cached_entity(account, identifier)
    if entity is None:
        return None
    client = account.client
    if client is None:
        return None
    try:
        if isinstance(entity, telethon_types.Channel):
            full = await client(functions.channels.GetFullChannelRequest(entity))
        elif isinstance(entity, telethon_types.Chat):
            full = await client(functions.messages.GetFullChatRequest(entity))
        else:
            return None
    except AuthKeyUnregisteredError:
        account.is_valid = False
        raise
    except FloodWaitError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Не удалось получить full chat для {identifier}: {e}")
        return None
    await full_chat_cache.set(account.session_file, identifier, full)
    return full


async def extract_participants_count(full: Any) -> int | None:
    if full is None:
        return None
    full_chat = getattr(full, "full_chat", None)
    if full_chat is not None and hasattr(full_chat, "participants_count"):
        return to_int(full_chat.participants_count)
    if hasattr(full, "participants_count"):
        return to_int(full.participants_count)
    return None


def is_broadcast_channel(entity: Any) -> bool:
    """Канал-вещание: вступать в него не нужно, группы для выборки не подходят."""
    return (
        isinstance(entity, telethon_types.Channel)
        and bool(getattr(entity, "broadcast", False))
        and not bool(getattr(entity, "megagroup", False))
    )


async def join_chat(account: Account, identifier: str) -> tuple[Any, bool]:
    client = account.client
    if client is None:
        raise NoAvailableAccountError(err("account_no_client"))
    if is_invite_link(identifier):
        invite_hash = parse_invite_hash(identifier)
        if not invite_hash:
            raise ChatUnreachableError(err("chat_unreachable", identifier=identifier))
        try:
            result = await client(functions.messages.ImportChatInviteRequest(invite_hash))
        except UserAlreadyParticipantError:
            entity = await _resolve_invited_entity(client, invite_hash)
            if entity is None:
                raise ChatUnreachableError(err("chat_unreachable", identifier=identifier))
            return entity, False
        chats = getattr(result, "chats", None)
        entity = chats[0] if chats else await get_cached_entity(account, invite_hash)
        if entity is None:
            raise ChatUnreachableError(err("chat_unreachable", identifier=identifier))
        track_joined_chat(account, entity, identifier)
        return entity, True
    entity = await get_cached_entity(account, identifier)
    if entity is None:
        raise ChatUnreachableError(err("entity_not_found", identifier=identifier))
    if is_broadcast_channel(entity):
        logger.info(f"Канал (не группа), вход пропущен: {identifier}")
        return entity, False
    try:
        await client(functions.channels.JoinChannelRequest(entity))
    except UserAlreadyParticipantError:
        return entity, False
    except InviteRequestSentError:
        logger.warning(
            f"{identifier}: чат требует подтверждения входа, бот не сможет писать и уйдёт из проверки"
        )
        raise
    track_joined_chat(account, entity, identifier)
    return entity, True


def entity_key(entity: Any) -> tuple[str, int] | None:
    if entity is None:
        return None
    kind = "channel" if isinstance(entity, telethon_types.Channel) else "chat"
    entity_id = to_int(getattr(entity, "id", 0), default=0)
    if entity_id == 0:
        return None
    return kind, entity_id


def _same_entity(left: Any, right: Any) -> bool:
    if left is right:
        return True
    left_key = entity_key(left)
    return left_key is not None and left_key == entity_key(right)


def track_joined_chat(account: Account, entity: Any, label: str) -> None:
    account.joined_chats = [
        item for item in account.joined_chats if not _same_entity(item[0], entity)
    ]
    account.joined_chats.append((entity, label))


def hold_task_chat(
    joined_targets: dict[str, tuple[Any, str]], account: Account, entity: Any, label: str
) -> None:
    """Оставляет чат в аккаунте до конца задачи: авто-выход отменяется."""
    account.joined_chats = [
        item for item in account.joined_chats if not _same_entity(item[0], entity)
    ]
    joined_targets[account.session_file] = (entity, label)


async def leave_task_chats(joined_targets: dict[str, tuple[Any, str]]) -> None:
    for session_file, (entity, label) in list(joined_targets.items()):
        joined_targets.pop(session_file, None)
        account = account_manager.get_account(session_file)
        if account is None:
            continue
        if not Features.auto_leave():
            logger.info("AUTO_LEAVE выключен, чаты задачи оставляем")
            return
        if account.client is None or not account.client.is_connected():
            with suppress(Exception):
                await account_manager.ensure_client(account)
        await leave_chat(account, entity, label)


async def _resolve_invited_entity(client: TelegramClient, invite_hash: str) -> Any | None:
    try:
        result = await client(functions.messages.CheckChatInviteRequest(invite_hash))
    except (FloodWaitError, AuthKeyUnregisteredError):
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Инвайт {invite_hash} не проверен: {type(e).__name__}: {e}")
        return None
    chat = getattr(result, "chat", None)
    if chat is None:
        logger.warning(f"Инвайт {invite_hash}: чат не возвращён проверкой")
        return None
    if isinstance(chat, telethon_types.Channel):
        return chat
    chat_id = to_int(getattr(chat, "id", 0))
    if chat_id <= 0:
        return None
    return chat


async def _send_probe(client: TelegramClient, entity: Any, language: str) -> Any:
    return await client.send_message(entity, pick_probe_text(language))


PARSE_ERROR_MARKERS = ("MESSAGE_PARSE_ENTITY", "MESSAGE_PARSE_MODE", "MESSAGE_PARSE_MARKDOWN")


def is_parse_error(error: Exception) -> bool:
    if isinstance(error, ValueError):
        return True
    if isinstance(error, BadRequestError):
        message = str(getattr(error, "message", "") or "")
        return any(marker in message for marker in PARSE_ERROR_MARKERS)
    return False


async def send_formatted(client: TelegramClient, destination: Any, text: str) -> Any:
    try:
        return await client.send_message(destination, text, parse_mode="html")
    except (BadRequestError, ValueError) as e:
        if not is_parse_error(e):
            raise
        logger.debug(f"Разбор HTML не удался ({type(e).__name__}), отправляем без разметки")
        return await client.send_message(destination, strip_html(text))


async def leave_chat(account: Account, entity: Any, label: str) -> bool:
    client = account.client
    account.joined_chats = [
        item for item in account.joined_chats if not _same_entity(item[0], entity)
    ]
    if client is None:
        return False
    try:
        if isinstance(entity, telethon_types.Channel):
            await client(functions.channels.LeaveChannelRequest(entity))
        else:
            await client(functions.messages.DeleteChatUserRequest(chat_id=entity.id, user_id="me"))
    except AuthKeyUnregisteredError:
        account.is_valid = False
        logger.warning(
            f"Аккаунт {account.session_file} не авторизован, выход из {label} не выполнен"
        )
        return False
    except UserNotParticipantError:
        logger.debug(f"Аккаунт {account.session_file} и так не состоит в {label}")
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Не удалось выйти из {label}: {type(e).__name__}: {e}")
        return False
    logger.info(f"Аккаунт {account.session_file} вышел из {label}")
    return True


async def validate_and_test_chat(
    account: Account,
    identifier: str,
    language: str,
    update_existing: bool = False,
) -> ValidatedChat | None:
    try:
        entity, joined_by_bot = await join_chat(account, identifier)
    except FloodWaitError:
        raise
    except AuthKeyUnregisteredError:
        account.is_valid = False
        raise
    except (ChannelPrivateError, ChatAdminRequiredError, UserNotParticipantError) as e:
        logger.info(f"Чат {identifier} недоступен: {type(e).__name__}")
        if update_existing:
            await chat_db.delete_chat_by_identifier(identifier)
        return None
    except (ChatUnreachableError, ValueError) as e:
        logger.info(f"Чат {identifier} не распознан: {e}")
        if update_existing:
            await chat_db.delete_chat_by_identifier(identifier)
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Ошибка входа в {identifier}: {type(e).__name__}: {e}")
        if update_existing:
            await chat_db.delete_chat_by_identifier(identifier)
        return None

    if isinstance(entity, telethon_types.Channel) and not getattr(entity, "megagroup", False):
        logger.info(f"Пропуск канала (не группа): {identifier}")
        if joined_by_bot:
            await leave_chat(account, entity, identifier)
        if update_existing:
            await chat_db.delete_chat_by_identifier(identifier)
        return None
    if not isinstance(entity, (telethon_types.Channel, telethon_types.Chat)):
        logger.info(f"Пропуск неизвестного типа сущности {identifier}: {type(entity).__name__}")
        if joined_by_bot:
            await leave_chat(account, entity, identifier)
        if update_existing:
            await chat_db.delete_chat_by_identifier(identifier)
        return None

    chat_id = str(entity.id)
    chat_name = str(getattr(entity, "title", "") or identifier)
    chat_url = build_chat_reference(getattr(entity, "username", None), identifier)

    user_count = to_int(getattr(entity, "participants_count", None))
    if not user_count:
        try:
            user_count = to_int(
                await extract_participants_count(await get_full_chat(account, identifier))
            )
        except FloodWaitError:
            user_count = 0
        except AuthKeyUnregisteredError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Не удалось получить participant_count для {identifier}: {e}")

    can_write = False
    try:
        sent = await _send_probe(require_client(account), entity, language)
        can_write = True
    except SlowModeWaitError as e:
        logger.info(f"Slow mode в {chat_name}: {getattr(e, 'seconds', 5)}с, повтор пробы")
        try:
            await asyncio.sleep(min(float(getattr(e, "seconds", 5)), 30.0))
            sent = await _send_probe(require_client(account), entity, language)
            can_write = True
        except FloodWaitError:
            raise
        except AuthKeyUnregisteredError:
            account.is_valid = False
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"Slow mode в {chat_name} не позволил повторить пробу: {type(e).__name__}"
            )
            can_write = False
    except FloodWaitError:
        raise
    except AuthKeyUnregisteredError:
        account.is_valid = False
        raise
    except (
        ChatAdminRequiredError,
        ChannelPrivateError,
        ChatWriteForbiddenError,
        UserPrivacyRestrictedError,
    ) as e:
        logger.info(f"Нет прав на запись в {chat_name}: {type(e).__name__}")
        can_write = False
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Ошибка проверки записи в {chat_name}: {type(e).__name__}: {e}")
        can_write = False

    if can_write:
        try:
            await asyncio.sleep(Config.VALIDATOR_TEST_DELETE_WAIT)
            await require_client(account).delete_messages(entity, sent.id)
        except MessageDeleteForbiddenError:
            logger.warning(f"Нет прав на удаление тестового сообщения в {chat_name}")
            can_write = False
        except FloodWaitError:
            raise
        except AuthKeyUnregisteredError:
            account.is_valid = False
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Не удалось удалить тестовое сообщение в {chat_name}: {e}")

    if joined_by_bot:
        await leave_chat(account, entity, chat_url or chat_name)

    if not can_write:
        if update_existing:
            await chat_db.delete_chat_by_identifier(identifier)
        return None

    await chat_db.upsert_chat(
        chat_id=chat_id,
        chat_name=chat_name,
        chat_url=chat_url,
        chat_type="group",
        user_count=user_count,
        verified=1,
    )
    logger.info(f"Чат {chat_name} ({chat_id}) добавлен в БД")
    return ValidatedChat(
        chat_id=chat_id,
        chat_name=chat_name,
        chat_url=chat_url,
        chat_type="group",
        user_count=user_count,
    )


async def check_one_chat(
    account: Account, chat: Any, language: str, result: CheckAndCleanResult
) -> None:
    """Проверяет один чат из БД. FloodWait и AuthKeyUnregisteredError пробрасываются."""
    identifier = chat.chat_url or chat.chat_id
    try:
        entity, _ = await join_chat(account, identifier)
    except FloodWaitError:
        raise
    except AuthKeyUnregisteredError:
        account.is_valid = False
        raise
    except Exception as e:  # noqa: BLE001
        logger.info(f"Чат {chat.chat_name} недоступен: {type(e).__name__}")
        result.errors += 1
        return

    if entity is None or not isinstance(entity, (telethon_types.Channel, telethon_types.Chat)):
        if await chat_db.delete_chat(chat.chat_id):
            result.removed += 1
            logger.info(f"Чат {chat.chat_name} удалён: больше не группа")
        return
    if isinstance(entity, telethon_types.Channel) and not getattr(entity, "megagroup", False):
        if await chat_db.delete_chat(chat.chat_id):
            result.removed += 1
            logger.info(f"Чат {chat.chat_name} удалён: это канал, а не группа")
        return

    reachable = True
    try:
        probe = await _send_probe(require_client(account), entity, language)
        try:
            await require_client(account).delete_messages(entity, probe.id)
        except FloodWaitError:
            raise
        except AuthKeyUnregisteredError:
            account.is_valid = False
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"Чат {chat.chat_name}: не удалось удалить проверочное сообщение: {type(e).__name__}"
            )
    except (ChatAdminRequiredError, ChannelPrivateError, ChatWriteForbiddenError):
        reachable = False
    except SlowModeWaitError as e:
        logger.info(f"Чат {chat.chat_name}: slow mode {getattr(e, 'seconds', 5)}с")
        result.errors += 1
        return
    except FloodWaitError:
        raise
    except AuthKeyUnregisteredError:
        account.is_valid = False
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Проба записи в {chat.chat_name} не удалась: {type(e).__name__}: {e}")
        result.errors += 1
        return

    if not reachable:
        if await chat_db.delete_chat(chat.chat_id):
            result.removed += 1
            logger.info(f"Чат {chat.chat_name} удалён: нет доступа на запись")
        return

    try:
        new_count = to_int(getattr(entity, "participants_count", None))
        if not new_count:
            new_count = to_int(
                await extract_participants_count(await get_full_chat(account, identifier))
            )
        fields: dict[str, Any] = {"last_check": utc_now_iso()}
        if new_count and new_count != chat.user_count:
            fields["user_count"] = new_count
            result.updated += 1
            logger.info(f"Чат {chat.chat_name}: участников {chat.user_count} -> {new_count}")
        if not await chat_db.update_chat(chat.chat_id, fields):
            logger.error(f"Не удалось обновить чат {chat.chat_name} в БД")
            result.errors += 1
    except FloodWaitError:
        raise
    except AuthKeyUnregisteredError:
        account.is_valid = False
        raise
    except Exception as e:  # noqa: BLE001
        result.errors += 1
        logger.error(f"Ошибка обновления чата {chat.chat_name}: {e}")


async def check_and_clean_chats(
    language: str, control: "TaskControl | None" = None
) -> CheckAndCleanResult:
    """Проверяет чаты из БД, ротируя аккаунт после каждого чата."""
    result = CheckAndCleanResult()
    chats = await chat_db.get_all_chats(verified_only=False)
    result.total = len(chats)
    for chat in chats:
        try:
            async with account_manager.acquire_step(None, control) as account:
                if account.session_file not in result.accounts_used:
                    result.accounts_used.append(account.session_file)
                result.checked += 1
                try:
                    await check_one_chat(account, chat, language, result)
                except FloodWaitError as e:
                    seconds = float(getattr(e, "seconds", 60))
                    extended = account_manager.handle_flood_wait(account, seconds)
                    result.flood_seconds = max(result.flood_seconds, seconds)
                    result.flood_events += 1
                    result.errors += 1
                    logger.warning(
                        f"FloodWait на чате {chat.chat_name}: {seconds:.0f}с "
                        f"(пауза {extended:.0f}с), продолжаю на другом аккаунте"
                    )
                    continue
                except AuthKeyUnregisteredError:
                    account.is_valid = False
                    result.errors += 1
                    result.stop_reason = translate(language, "texts.invalid_account_continue")
                    logger.warning(f"Аккаунт {account.session_file} сброшен, продолжаю на другом")
                    continue
        except AccountWaitCancelled:
            result.stop_reason = translate(language, "texts.roller_stopped")
            break
        except NoAvailableAccountError as e:
            result.stop_reason = str(e.message)
            logger.warning(f"Проверка чатов остановлена: {e.message}")
            break
    return result


# --- Пул аккаунтов ---
class AccountPoolManager:
    def __init__(self):
        self.accounts: list[Account] = []
        self.lock: asyncio.Lock = asyncio.Lock()
        self._success_rate: dict[str, float] = {}
        self._rr_cursor: int = 0
        self._free_event: asyncio.Event = asyncio.Event()
        self._health_task: asyncio.Task[None] | None = None
        self.load_accounts()

    def load_accounts(self) -> None:
        self.accounts.clear()
        self._success_rate.clear()
        self._rr_cursor = 0
        directory = Path(Config.SESSIONS_DIR)
        if not directory.exists():
            logger.warning(f"Папка сессий не найдена: {directory}")
            return
        for path in sorted(directory.glob("*.session")):
            try:
                session_string = path.read_text(encoding="utf-8").strip()
            except OSError as e:
                logger.error(f"Не удалось прочитать сессию {path.name}: {e}")
                continue
            if not session_string:
                logger.warning(f"Пустая сессия {path.name}, пропущена")
                continue
            self.accounts.append(Account(session_file=path.name, session_string=session_string))
            self._success_rate[path.name] = 1.0
        logger.info(f"Загружено сессий: {len(self.accounts)}")

    def add_account(self, session_string: str, session_name: str) -> bool:
        directory = Path(Config.SESSIONS_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        session_path = directory / f"{session_name}.session"
        try:
            session_path.write_text(session_string, encoding="utf-8")
        except OSError as e:
            logger.error(f"Не удалось сохранить сессию {session_name}: {e}")
            return False
        self.accounts.append(Account(session_file=session_path.name, session_string=session_string))
        self._success_rate[session_path.name] = 1.0
        logger.info(f"Добавлена сессия {session_path.name}")
        return True

    def get_account(self, session_file: str) -> Account | None:
        for account in self.accounts:
            if account.session_file == session_file:
                return account
        return None

    def update_success_rate(self, account: Account, success: bool) -> None:
        current = self._success_rate.setdefault(account.session_file, 1.0)
        self._success_rate[account.session_file] = 0.1 * (1.0 if success else 0.0) + (0.9 * current)
        if success:
            account.consecutive_errors = 0
            return
        account.consecutive_errors += 1
        if account.consecutive_errors < Config.MAX_ACCOUNT_CONSECUTIVE_ERRORS:
            return
        account.consecutive_errors = 0
        cooldown_until = datetime.now(UTC) + timedelta(seconds=Config.ACCOUNT_ERROR_COOLDOWN)
        if account.flood_wait_until is None or account.flood_wait_until < cooldown_until:
            account.flood_wait_until = cooldown_until
        logger.warning(
            f"Аккаунт {account.session_file}: {Config.MAX_ACCOUNT_CONSECUTIVE_ERRORS} ошибок подряд, "
            f"пауза {Config.ACCOUNT_ERROR_COOLDOWN}с"
        )

    def _adaptive_delay(self, account: Account) -> float:
        rate = max(self._success_rate.get(account.session_file, 1.0), 0.1)
        delay = min(Config.ADAPTIVE_DELAY_BASE / rate, Config.ADAPTIVE_DELAY_MAX)
        return delay * rand_range(Config.INVITE_JITTER_MIN, Config.INVITE_JITTER_MAX)

    def _flood_active(self, account: Account) -> bool:
        if account.flood_wait_until is None:
            return False
        if account.flood_wait_until > datetime.now(UTC):
            return True
        account.flood_wait_until = None
        return False

    def _rate_limited(self, account: Account) -> bool:
        if account.last_used is None:
            return False
        last_used = account.last_used
        if last_used.tzinfo is None:
            last_used = last_used.replace(tzinfo=UTC)
        elapsed = (datetime.now(UTC) - last_used).total_seconds()
        return elapsed < self._adaptive_delay(account)

    def is_available(self, account: Account) -> bool:
        return (
            not account.in_use
            and account.is_valid
            and not self._flood_active(account)
            and not self._rate_limited(account)
        )

    def has_available_accounts(self) -> bool:
        return any(self.is_available(account) for account in self.accounts)

    def handle_flood_wait(self, account: Account, wait_seconds: float) -> float:
        extended = float(wait_seconds) * Config.FLOOD_WAIT_MULTIPLIER + Config.FLOOD_WAIT_PADDING
        account.flood_wait_until = datetime.now(UTC) + timedelta(seconds=extended)
        self.update_success_rate(account, False)
        logger.warning(
            f"FloodWait для {account.session_file}: пауза {extended:.0f}с "
            f"(запрошено {wait_seconds:.0f}с)"
        )
        return extended

    def should_simulate_skip(self) -> bool:
        return Features.human_simulation() and rand_range(0, 1) < Config.SIMULATE_SKIP_RATE

    async def _connect(self, account: Account) -> TelegramClient:
        async def _do_connect() -> TelegramClient:
            client = create_telegram_client(account.session_string)
            await asyncio.wait_for(client.connect(), timeout=Config.ACCOUNT_CONNECT_TIMEOUT)
            return client

        client = await retry_async(
            _do_connect,
            max_retries=Config.MAX_RETRIES,
            delay=Config.RETRY_DEFAULT_DELAY,
            backoff=Config.RETRY_BACKOFF_BASE,
        )
        if not await client.is_user_authorized():
            await client.disconnect()
            raise AuthKeyUnregisteredError(request=None)
        return client

    async def ensure_client(self, account: Account) -> TelegramClient:
        if account.client is not None and account.client.is_connected():
            return account.client
        if account.client is not None:
            try:
                await account.client.disconnect()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"disconnect {account.session_file}: {e}")
            account.client = None
        try:
            account.client = await self._connect(account)
        except Exception as e:
            logger.error(f"Не удалось подключить {account.session_file}: {e}")
            account.is_valid = False
            raise NoAvailableAccountError(
                err("account_unavailable", session=account.session_file, error=e)
            ) from e
        self.update_success_rate(account, True)
        return account.client

    @asynccontextmanager
    async def acquire(
        self, session_file: str | None = None, control: "TaskControl | None" = None
    ) -> AsyncIterator[Account]:
        account = await self._take_account(session_file, control)
        try:
            yield account
        finally:
            await self.release(account)

    def acquire_step(
        self, session_file: str | None = None, control: "TaskControl | None" = None
    ) -> AbstractAsyncContextManager[Account]:
        """Один шаг задачи: аккаунт берётся по кругу, используется и сразу освобождается."""
        return self.acquire(session_file, control)

    async def release(self, account: Account) -> None:
        try:
            await self.release_joined_chats(account)
        finally:
            async with self.lock:
                account.in_use = False
            self._free_event.set()

    async def release_joined_chats(self, account: Account) -> None:
        pending: list[tuple[Any, str]] = list(account.joined_chats)
        account.joined_chats.clear()
        if not pending:
            return
        if not Features.auto_leave():
            logger.info("AUTO_LEAVE выключен, чаты оставляем")
            return
        for entity, label in reversed(pending):
            with suppress(Exception):
                await leave_chat(account, entity, label)

    def _budget_exhausted(self, account: Account) -> bool:
        return account.invite_count >= Config.MAX_INVITES_PER_ACCOUNT

    def _account_index(self, account: Account) -> int:
        for index, item in enumerate(self.accounts):
            if item is account:
                return index
        return -1

    def _pick_auto_locked(self) -> Account | None:
        """Следующий свободный аккаунт по кругу: 1, 2, 3, снова 1."""
        total = len(self.accounts)
        if total == 0:
            return None
        start = self._rr_cursor % total
        for offset in range(total):
            account = self.accounts[(start + offset) % total]
            if self.is_available(account):
                return account
        return None

    def _pick_locked(self, session_file: str | None) -> Account | None:
        if not session_file:
            return self._pick_auto_locked()
        account = self.get_account(session_file)
        if account is None:
            raise NoAvailableAccountError(err("session_not_found", session=session_file))
        if not account.is_valid:
            raise NoAvailableAccountError(err("session_invalid", session=session_file))
        return account if self.is_available(account) else None

    def _mark_acquired_locked(self, account: Account) -> None:
        account.in_use = True
        account.last_used = datetime.now(UTC)
        index = self._account_index(account)
        if index >= 0 and self.accounts:
            self._rr_cursor = (index + 1) % len(self.accounts)
        if self._budget_exhausted(account):
            logger.info(f"Сброс счётчика инвайтов для {account.session_file}")
            account.invite_count = 0

    def _available_delay(self, account: Account) -> float | None:
        """0 - аккаунт свободен, секунды до освобождения, None - аккаунт непригоден."""
        if not account.is_valid:
            return None
        if self._flood_active(account):
            until = account.flood_wait_until or datetime.now(UTC)
            return max(0.0, (until - datetime.now(UTC)).total_seconds())
        if account.last_used is not None and self._rate_limited(account):
            last_used = account.last_used
            if last_used.tzinfo is None:
                last_used = last_used.replace(tzinfo=UTC)
            remaining = (
                self._adaptive_delay(account) - (datetime.now(UTC) - last_used).total_seconds()
            )
            return max(0.0, remaining)
        return 0.0

    def _next_ready_delay_locked(self, session_file: str | None) -> float | None:
        delays: list[float] = []
        for account in self.accounts:
            if session_file and account.session_file != session_file:
                continue
            if account.in_use:
                continue
            delay = self._available_delay(account)
            if delay is None or delay <= 0:
                continue
            delays.append(delay)
        return min(delays) if delays else None

    def pool_state_locked(self) -> tuple[int, int, int]:
        busy = sum(1 for account in self.accounts if account.in_use)
        flood = sum(
            1 for account in self.accounts if not account.in_use and self._flood_active(account)
        )
        invalid = sum(1 for account in self.accounts if not account.is_valid)
        return busy, flood, invalid

    async def _wait_for_account(
        self, session_file: str | None, control: "TaskControl | None"
    ) -> None:
        async with self.lock:
            if self._pick_locked(session_file) is not None:
                return
        with suppress(TimeoutError):
            await asyncio.wait_for(
                self._free_event.wait(), timeout=Config.ACCOUNT_ROLLER_WAIT_TIMEOUT
            )
        async with self.lock:
            if self._pick_locked(session_file) is not None:
                return
            if not self.accounts:
                raise NoAvailableAccountError(err("no_free_accounts"))
            if not any(account.is_valid for account in self.accounts):
                raise NoAvailableAccountError(err("no_valid_accounts"))
            delay = self._next_ready_delay_locked(session_file)
            busy, flood, invalid = self.pool_state_locked()
        if delay is not None and delay > 0:
            wait_time = max(0.1, delay)
            logger.info(
                f"Роллер: свободных аккаунтов нет, ждём {wait_time:.0f}с "
                f"(в работе {busy}, пауза {flood}, недействительных {invalid})"
            )
            with suppress(TimeoutError):
                await asyncio.wait_for(self._free_event.wait(), timeout=wait_time)
        else:
            logger.info(
                f"Роллер: свободных аккаунтов нет, ждём (в работе {busy}, "
                f"пауза {flood}, недействительных {invalid})"
            )
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self._free_event.wait(), timeout=Config.ACCOUNT_ROLLER_WAIT_TIMEOUT
                )
        if control is not None and not await control.checkpoint():
            raise AccountWaitCancelled

    async def _take_account(
        self, session_file: str | None, control: "TaskControl | None"
    ) -> Account:
        failures = 0
        while True:
            account: Account | None = None
            async with self.lock:
                account = self._pick_locked(session_file)
                if account is not None:
                    self._mark_acquired_locked(account)
            if account is None:
                await self._wait_for_account(session_file, control)
                continue
            try:
                await self.ensure_client(account)
            except asyncio.CancelledError:
                async with self.lock:
                    account.in_use = False
                self._free_event.set()
                raise
            except Exception:
                async with self.lock:
                    account.in_use = False
                self._free_event.set()
                failures += 1
                if session_file or failures >= max(1, len(self.accounts)):
                    raise
                logger.warning(
                    f"Аккаунт {account.session_file} не подключился, роутер берёт следующий"
                )
                continue
            logger.info(f"Аккаунт {account.session_file} взят в работу")
            return account

    async def health_check_once(self) -> None:
        for account in list(self.accounts):
            if account.in_use:
                continue
            try:
                await self.ensure_client(account)
                logger.info(f"Проверка аккаунта {account.session_file}: ОК")
            except Exception as e:  # noqa: BLE001
                logger.error(f"Проверка аккаунта {account.session_file}: {e}")

    async def start_health_check(self) -> None:
        self._health_task = asyncio.create_task(self._health_check_loop())
        logger.info("Health check аккаунтов запущен")

    async def stop_health_check(self) -> None:
        if self._health_task is None:
            return
        self._health_task.cancel()
        try:
            await self._health_task
        except asyncio.CancelledError:
            logger.info("Health check аккаунтов остановлен")

    async def _health_check_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(Config.ACCOUNT_HEALTH_CHECK_INTERVAL)
                await self.health_check_once()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.error(f"Ошибка health check: {e}")
                await asyncio.sleep(backoff_delay())

    async def release_all(self) -> None:
        for account in self.accounts:
            if account.client is None:
                continue
            try:
                if account.client.is_connected():
                    await account.client.disconnect()
            except Exception as e:  # noqa: BLE001
                logger.error(f"Ошибка отключения {account.session_file}: {e}")
            account.client = None
            account.in_use = False
        self._free_event.set()


# --- Глобальные объекты ---
_bot_token: str = Config.BOT_TOKEN if is_valid_bot_token_format(Config.BOT_TOKEN) else ""
if not _bot_token:
    logger.critical("BOT_TOKEN не настроен или некорректен. Бот не будет запущен.")
    sys.exit(1)

bot: Bot = Bot(
    token=_bot_token,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router: Router = Router(name="inviter")
dp.include_router(router)
dp.callback_query.middleware(CallbackAnswerMiddleware())

tasks_db: TasksDB = TasksDB(Config.TASKS_DB_PATH)
chat_db: ChatDB = ChatDB(Config.CHATS_DB_PATH)
cache_db: CacheManager = CacheManager(Config.CACHE_DB_PATH)
user_db: UserDB = UserDB(Config.USERS_DB_PATH)
account_manager: AccountPoolManager = AccountPoolManager()

LOGIN_CLIENTS: dict[int, TelegramClient] = {}
BACKGROUND_TASKS: set[asyncio.Task[None]] = set()

queue_manager: "TaskQueueManager | None" = None

TASK_TYPES: dict[str, str] = {
    "scrape_invite": "texts.task_type_scrape_invite",
    "bulkmail": "texts.task_type_bulkmail",
    "worm": "texts.task_type_worm",
}

PROBE_MESSAGE_KEYS: tuple[str, ...] = (
    "texts.probe_message_1",
    "texts.probe_message_2",
    "texts.probe_message_3",
    "texts.probe_message_4",
    "texts.probe_message_5",
    "texts.probe_message_6",
)


def new_task_id() -> str:
    return uuid.uuid4().hex[:12]


def pick_probe_text(language: str) -> str:
    return translate(language, random.choice(PROBE_MESSAGE_KEYS))


def format_task_status(status: str, language: str) -> str:
    return translate(language, f"texts.status_{status}")


def format_task_type(task_type: str, language: str) -> str:
    key = TASK_TYPES.get(task_type, "")
    return translate(language, key) if key else task_type


def format_task_card(task: Task, language: str = DEFAULT_LANGUAGE) -> str:
    data = task.data
    source = str(data.get("source") or data.get("chats_label") or "-")
    target = str(data.get("target") or "-")
    account = str(data.get("account") or "-")
    lines = [
        f"{task_status_emoji(task.status)} <b>{translate(language, 'texts.task_card_title')}</b>",
        "",
        (f"{translate(language, 'texts.task_id_label')} <code>{html.escape(task.task_id)}</code>"),
        f"{translate(language, 'texts.task_type_label')} {format_task_type(task.type, language)}",
        f"{translate(language, 'texts.task_status_label')} {format_task_status(task.status, language)}",
        f"{translate(language, 'texts.task_source_label')} <code>{html.escape(source)}</code>",
    ]
    if target != "-":
        lines.append(
            f"{translate(language, 'texts.task_target_label')} <code>{html.escape(target)}</code>"
        )
    if account not in ("", "-"):
        account_value = (
            translate(language, "buttons.auto_account")
            if account == "auto"
            else html.escape(account)
        )
        lines.append(f"{translate(language, 'texts.task_account_label')} {account_value}")
    used_accounts = task.results.get("accounts") if isinstance(task.results, dict) else None
    if isinstance(used_accounts, list) and used_accounts:
        lines.append(
            f"{translate(language, 'texts.task_accounts_label', count=len(used_accounts))} "
            f"<code>{html.escape(format_accounts_summary([str(item) for item in used_accounts]))}</code>"
        )
    for label_key, data_key in (
        ("texts.task_messages_label", "message_limit"),
        ("texts.task_users_label", "user_limit"),
        ("texts.task_chats_label", "chat_count"),
        ("texts.task_total_label", "total"),
    ):
        value = to_int(data.get(data_key))
        if value:
            lines.append(f"{translate(language, label_key)} <b>{value}</b>")
    lines.append(f"{translate(language, 'texts.task_sent_label')} <b>{task.sent}</b>")
    lines.append("")
    lines.append(format_progress_bar(task.progress))
    if task.progress_text:
        lines.append(html.escape(task.progress_text))
    if task.error:
        lines.append(translate(language, "texts.task_error_line", error=task.error))
    return "\n".join(lines)


def build_task_keyboard(task: Task, language: str) -> InlineKeyboardMarkup:
    rows: list[list[dict[str, str]]] = []
    if task.status == "running":
        action = (
            translate(language, "buttons.task_pause"),
            f"task:pause:{task.task_id}",
        )
    elif task.status in ("pending", "paused"):
        action = (
            translate(language, "buttons.task_resume"),
            f"task:resume:{task.task_id}",
        )
    else:
        action = None
    if action is not None:
        rows.append(
            [
                {"text": action[0], "callback_data": action[1]},
                {
                    "text": translate(language, "buttons.task_cancel"),
                    "callback_data": f"task:cancel:{task.task_id}",
                },
            ]
        )
    rows.append(
        [
            {
                "text": translate(language, "buttons.task_refresh"),
                "callback_data": f"task:view:{task.task_id}",
            },
            {
                "text": translate(language, "buttons.task_view_all"),
                "callback_data": "task:list:active",
            },
        ]
    )
    rows.append([{"text": translate(language, "buttons.main"), "callback_data": "start"}])
    return kb(rows)


# --- Управление жизненным циклом задачи ---
class TaskControl:
    def __init__(self):
        self.task: Task | None = None
        self._cancelled: bool = False
        self._paused: bool = False
        self._resume_event: asyncio.Event = asyncio.Event()
        self._resume_event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    @property
    def is_paused(self) -> bool:
        return self._paused

    def cancel(self) -> None:
        self._cancelled = True
        self._resume_event.set()

    def pause(self) -> None:
        self._paused = True
        self._resume_event.clear()

    def resume(self) -> None:
        self._paused = False
        self._resume_event.set()

    async def checkpoint(self) -> bool:
        if self._cancelled:
            return False
        if self._paused:
            logger.info(f"Задача {self.task_id} на паузе, ждём возобновления")
            await self._resume_event.wait()
        return not self._cancelled

    async def wait(self, seconds: float) -> bool:
        deadline = asyncio.get_running_loop().time() + max(0.0, seconds)
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return not self._cancelled
            if self._cancelled:
                return False
            if self._paused:
                logger.info(
                    f"Задача {self.task_id} на паузе, "
                    f"ждём возобновления (осталось {remaining:.1f}с)"
                )
                try:
                    await asyncio.wait_for(self._resume_event.wait(), timeout=remaining)
                except TimeoutError:
                    return not self._cancelled
                continue
            try:
                await asyncio.wait_for(self._resume_event.wait(), timeout=remaining)
            except TimeoutError:
                return not self._cancelled

    @property
    def task_id(self) -> str:
        return self.task.task_id if self.task else "-"


def resolve_final_status(control: TaskControl, fatal_error: str = "") -> str:
    if control.is_cancelled:
        return "cancelled"
    if fatal_error:
        return "failed"
    if control.is_paused:
        return "paused"
    return "completed"


def build_final_updates(
    control: TaskControl, results: dict[str, Any], fatal_error: str = ""
) -> dict[str, Any]:
    status = resolve_final_status(control, fatal_error)
    updates: dict[str, Any] = {
        "status": status,
        "results": json.dumps(results, ensure_ascii=False),
    }
    if status == "completed":
        updates["progress"] = 100.0
        updates["completed_at"] = utc_now_iso()
    elif status == "failed":
        updates["completed_at"] = utc_now_iso()
        updates["error"] = fatal_error[:900]
        updates["progress_text"] = fatal_error[:200]
    elif status == "cancelled":
        updates["completed_at"] = utc_now_iso()
        updates["cancelled_at"] = utc_now_iso()
    else:
        updates["paused_at"] = utc_now_iso()
    return updates


class TaskQueueManager:
    def __init__(self, bot: Bot):
        self.bot: Bot = bot
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.controls: dict[str, TaskControl] = {}
        self.runners: dict[str, asyncio.Task[None]] = {}
        self._workers: dict[int, asyncio.Task[None]] = {}
        self._retiring: set[int] = set()
        self._last_activity: float = 0.0
        self._lock: asyncio.Lock = asyncio.Lock()
        self._monitors: list[asyncio.Task[None]] = []
        self._stopping: bool = False

    def _touch(self) -> None:
        self._last_activity = asyncio.get_running_loop().time()

    async def start(self) -> None:
        await self._recover_tasks()
        self._touch()
        for index in range(max(1, Config.MIN_WORKERS)):
            await self._spawn_worker(index)
        self._monitors = [
            asyncio.create_task(self._scale_loop(), name="task-scaler"),
            asyncio.create_task(self._health_loop(), name="task-health"),
            asyncio.create_task(self._cleanup_loop(), name="task-cleanup"),
        ]
        logger.info(
            f"Планировщик задач запущен: воркеров {len(self._workers)}, "
            f"лимит одновременных {Config.MAX_CONCURRENT_TASKS}"
        )

    async def stop(self) -> None:
        self._stopping = True
        for monitor in self._monitors:
            monitor.cancel()
        for monitor in self._monitors:
            try:
                await monitor
            except asyncio.CancelledError:
                continue
        for control in list(self.controls.values()):
            control.cancel()
        workers = list(self._workers.values())
        for worker in workers:
            worker.cancel()
        for worker in workers:
            try:
                await worker
            except asyncio.CancelledError:
                continue
        for runner in list(self.runners.values()):
            runner.cancel()
        for runner in list(self.runners.values()):
            try:
                await runner
            except asyncio.CancelledError:
                continue
        self._workers.clear()
        self._monitors.clear()
        logger.info("Планировщик задач остановлен")

    async def _recover_tasks(self) -> None:
        restored = await tasks_db.mark_running_as_paused()
        if restored:
            logger.info(f"Задач переведено в паузу после перезапуска: {restored}")
        for task in await tasks_db.get_active_tasks():
            if task.status == "pending":
                self.queue.put_nowait(task.task_id)

    async def user_task_count(self, user_id: int) -> int:
        return len([task for task in await tasks_db.get_user_tasks(user_id) if task.is_active])

    async def global_running_count(self) -> int:
        return len([task for task in await tasks_db.get_active_tasks() if task.status == "running"])

    async def submit(self, task: Task) -> bool:
        async with self._lock:
            user_tasks = await self.user_task_count(task.user_id)
            if user_tasks >= Config.MAX_TASKS_PER_USER:
                logger.warning(f"Лимит активных задач для {task.user_id}: {user_tasks}")
                return False
            if len(self.runners) + self.queue.qsize() >= Config.MAX_CONCURRENT_TASKS:
                logger.warning("Достигнут лимит одновременных задач")
                return False
            if not await tasks_db.add_task(task):
                return False
            self.queue.put_nowait(task.task_id)
            self._touch()
        logger.info(f"Задача {task.task_id} ({task.type}) поставлена в очередь")
        return True

    def get_control(self, task_id: str) -> TaskControl | None:
        return self.controls.get(task_id)

    async def request_pause(self, task_id: str) -> bool:
        control = self.controls.get(task_id)
        if control is None:
            return False
        control.pause()
        return await tasks_db.update_task(task_id, {"status": "paused", "paused_at": utc_now_iso()})

    async def request_resume(self, task_id: str) -> bool:
        control = self.controls.get(task_id)
        task = await tasks_db.get_task(task_id)
        if task is None:
            return False
        if control is not None:
            control.resume()
            return await tasks_db.update_task(
                task_id, {"status": "running", "resumed_at": utc_now_iso()}
            )
        if task.status != "paused":
            return False
        if not await tasks_db.update_task(
            task_id, {"status": "pending", "resumed_at": utc_now_iso()}
        ):
            return False
        self.queue.put_nowait(task_id)
        return True

    async def request_cancel(self, task_id: str, cancelled_by: int | None = None) -> bool:
        control = self.controls.get(task_id)
        if control is not None:
            control.cancel()
        return await tasks_db.cancel_task(task_id, cancelled_by=cancelled_by)

    async def _spawn_worker(self, index: int) -> None:
        worker = asyncio.create_task(self._worker_loop(index), name=f"invite-worker-{index}")
        self._workers[index] = worker

    async def _retire_worker(self, index: int) -> None:
        worker = self._workers.pop(index, None)
        self._retiring.discard(index)
        if worker is None:
            return
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

    async def _worker_loop(self, index: int) -> None:
        logger.info(f"Воркер #{index} запущен")
        try:
            while not self._stopping:
                if index in self._retiring:
                    break
                try:
                    task_id = await asyncio.wait_for(self.queue.get(), timeout=5.0)
                except TimeoutError:
                    continue
                try:
                    await self._execute(task_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(f"Воркер #{index}: сбой выполнения {task_id}")
                finally:
                    self.queue.task_done()
        except asyncio.CancelledError:
            logger.info(f"Воркер #{index} остановлен")
        except Exception:
            logger.exception(f"Воркер #{index}: критическая ошибка")

    async def _execute(self, task_id: str) -> None:
        task = await tasks_db.get_task(task_id)
        if task is None:
            logger.error(f"Задача {task_id} не найдена в БД")
            return
        if task.status != "pending":
            logger.info(f"Задача {task_id} пропущена: статус {task.status}")
            return
        if not account_manager.accounts:
            await self._finish_failed(
                task,
                translate(await user_db.get_language(task.user_id), "texts.no_available_accounts"),
            )
            return

        control = TaskControl()
        control.task = task
        runner = asyncio.create_task(self._runner(task, control), name=f"invite-task-{task_id}")
        async with self._lock:
            self.controls[task_id] = control
            self.runners[task_id] = runner
            self._touch()
        await tasks_db.update_task(
            task_id, {"status": "running", "error": "", "resumed_at": utc_now_iso()}
        )
        try:
            await asyncio.wait_for(asyncio.shield(runner), timeout=Config.TASK_TIMEOUT)
        except TimeoutError:
            control.cancel()
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                logger.debug(f"Задача {task_id}: выполнение отменено по таймауту")
            except Exception as e:  # noqa: BLE001
                logger.debug(f"Задача {task_id}: runner остановлен по таймауту: {e}")
            await self._finish_failed(
                task,
                translate(
                    await user_db.get_language(task.user_id),
                    "texts.task_failed_timeout",
                    seconds=Config.TASK_TIMEOUT,
                ),
            )
        except asyncio.CancelledError:
            control.cancel()
            await self._finish_cancelled(task)
            raise
        except Exception as e:
            logger.exception(f"Задача {task_id} завершилась ошибкой")
            await self._finish_failed(task, f"{type(e).__name__}: {e}")
        finally:
            async with self._lock:
                self.controls.pop(task_id, None)
                self.runners.pop(task_id, None)

    async def _runner(self, task: Task, control: TaskControl) -> None:
        handlers: dict[str, Callable[[Task, TaskControl], Awaitable[None]]] = {
            "scrape_invite": run_scrape_invite_task,
            "bulkmail": run_mailing_task,
            "worm": run_worm_task,
        }
        handler = handlers.get(task.type)
        if handler is None:
            raise BotError(
                translate(
                    await user_db.get_language(task.user_id),
                    "texts.task_failed_unknown_type",
                    task_type=task.type,
                )
            )
        token = LANGUAGE_CONTEXT.set(await user_db.get_language(task.user_id))
        try:
            await handler(task, control)
        finally:
            LANGUAGE_CONTEXT.reset(token)

    async def _finish_failed(self, task: Task, error: str) -> None:
        await tasks_db.update_task(
            task.task_id,
            {
                "status": "failed",
                "error": error[:900],
                "completed_at": utc_now_iso(),
                "progress_text": error[:200],
            },
        )
        language = await user_db.get_language(task.user_id)
        await safe_send_message(
            self.bot,
            task.user_id,
            translate(language, "texts.task_failed_report", task_id=task.task_id, error=error),
            reply_markup=main_menu_keyboard(language),
        )
        logger.error(f"Задача {task.task_id} провалена: {error}")

    async def _finish_cancelled(self, task: Task) -> None:
        await tasks_db.update_task(
            task.task_id, {"status": "cancelled", "completed_at": utc_now_iso()}
        )
        language = await user_db.get_language(task.user_id)
        key = (
            "texts.mailing_cancelled_report"
            if task.type == "bulkmail"
            else "texts.task_cancelled_report"
        )
        await safe_send_message(
            self.bot,
            task.user_id,
            translate(language, key, task_id=task.task_id),
            reply_markup=main_menu_keyboard(language),
        )
        logger.info(f"Задача {task.task_id} отменена")

    async def _scale_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(Config.WORKER_CHECK_INTERVAL)
                loop = asyncio.get_running_loop()
                pending = self.queue.qsize()
                if pending and len(self._workers) < Config.MAX_WORKERS:
                    if not account_manager.has_available_accounts():
                        continue
                    index = max(self._workers, default=0) + 1
                    await self._spawn_worker(index)
                    logger.info(f"Воркер #{index} добавлен (очередь {pending})")
                elif not pending and not self.runners:
                    idle = loop.time() - self._last_activity
                    if (
                        idle >= Config.WORKER_IDLE_TIMEOUT
                        and len(self._workers) > Config.MIN_WORKERS
                    ):
                        candidates = [key for key in self._workers if key not in self._retiring]
                        if candidates:
                            self._retiring.add(candidates[0])
                            await self._retire_worker(candidates[0])
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Ошибка масштабирования воркеров")
                await asyncio.sleep(backoff_delay())

    async def _health_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(Config.TASK_HEALTH_CHECK_INTERVAL)
                for task in await tasks_db.get_active_tasks():
                    if task.status != "running":
                        continue
                    if task.task_id in self.runners:
                        continue
                    logger.warning(f"Задача {task.task_id} потеряна, помечаю как failed")
                    await self._finish_failed(
                        task,
                        translate(
                            await user_db.get_language(task.user_id),
                            "texts.task_failed_lost",
                        ),
                    )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Ошибка health check задач")
                await asyncio.sleep(backoff_delay())

    async def _cleanup_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(Config.OLD_TASK_CLEANUP_INTERVAL)
                cutoff = (
                    datetime.now(UTC) - timedelta(days=max(1, Config.TASK_HISTORY_DAYS))
                ).isoformat()
                removed = await tasks_db.delete_finished_before(cutoff)
                if removed:
                    logger.info(f"Удалено старых задач: {removed}")
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Ошибка очистки задач")
                await asyncio.sleep(backoff_delay())


def get_queue_manager() -> TaskQueueManager:
    if queue_manager is None:
        raise BotError(err("queue_not_started"), code=503)
    return queue_manager


# --- Прогресс задач ---
class TaskProgressReporter:
    UPDATE_INTERVAL: ClassVar[float] = 12.0

    def __init__(self, task: Task, control: TaskControl, interval: float | None = None):
        self.task: Task = task
        self.control: TaskControl = control
        self.bot: Bot = get_queue_manager().bot
        self.language: str = DEFAULT_LANGUAGE
        self.interval: float = float(interval) if interval else TaskProgressReporter.UPDATE_INTERVAL
        self._chat_id: int = to_int(task.user_id)
        self._message_id: int = 0
        self._updated_at: float = 0.0
        self._lock: asyncio.Lock = asyncio.Lock()

    async def start(self) -> None:
        self.language = await user_db.get_language(self.task.user_id)
        card = format_task_card(self.task, self.language)
        markup = build_task_keyboard(self.task, self.language)
        try:
            message = await self.bot.send_message(
                self._chat_id, card, parse_mode=ParseMode.HTML, reply_markup=markup
            )
            self._message_id = to_int(message.message_id)
        except (TelegramBadRequest, TelegramNetworkError) as e:
            logger.error(f"Не удалось отправить карточку задачи {self.task.task_id}: {e}")
            self._message_id = 0
        self._updated_at = asyncio.get_running_loop().time()

    async def update(self, progress: float, text: str, force: bool = False) -> None:
        loop = asyncio.get_running_loop()
        if not force and loop.time() - self._updated_at < self.interval:
            return
        await self._flush(progress, text)

    async def _flush(self, progress: float, text: str) -> None:
        async with self._lock:
            await tasks_db.update_task(
                self.task.task_id,
                {
                    "progress": max(0.0, min(100.0, progress)),
                    "progress_text": text[:200],
                },
            )
            self.task.progress = max(0.0, min(100.0, progress))
            self.task.progress_text = text
            self._updated_at = asyncio.get_running_loop().time()
            if not self._message_id:
                return
            card = format_task_card(self.task, self.language)
            markup = build_task_keyboard(self.task, self.language)
            try:
                await self.bot.edit_message_text(
                    self._chat_id,
                    self._message_id,
                    card,
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                )
            except TelegramBadRequest as e:
                message = str(e).lower()
                if "not modified" in message:
                    return
                logger.debug(f"Не удалось обновить карточку задачи: {e}")
                self._message_id = 0
            except TelegramNetworkError as e:
                logger.debug(f"Сеть недоступна при обновлении карточки: {e}")

    async def finalize(self, extra: str = "") -> None:
        async with self._lock:
            card = format_task_card(self.task, self.language)
            if extra:
                card = f"{card}\n\n{extra}"
            markup = build_task_keyboard(self.task, self.language)
            if not self._message_id:
                try:
                    message = await self.bot.send_message(
                        self._chat_id, card, parse_mode=ParseMode.HTML, reply_markup=markup
                    )
                    self._message_id = to_int(message.message_id)
                except (TelegramBadRequest, TelegramNetworkError) as e:
                    logger.error(f"Не удалось отправить итог задачи: {e}")
                return
            try:
                await self.bot.edit_message_text(
                    self._chat_id,
                    self._message_id,
                    card,
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                )
            except (TelegramBadRequest, TelegramNetworkError) as e:
                logger.debug(f"Не удалось обновить итог задачи: {e}")


async def _apply_progress(
    control: TaskControl,
    reporter: TaskProgressReporter,
    processed: int,
    total: int,
    text: str,
) -> bool:
    if not await control.checkpoint():
        return False
    percentage = (processed / total * 100) if total else 0.0
    await reporter.update(percentage, text)
    return True


async def _collect_users_from_messages(
    account: Account,
    entity: Any,
    limit: int,
    control: TaskControl,
) -> list[int]:
    client = account.client
    if client is None:
        return []
    own_id = 0
    with suppress(Exception):
        me = await client.get_me()
        own_id = to_int(me.id)
    users: list[int] = []
    seen: set[int] = set()
    async for message in client.iter_messages(entity, limit=limit):
        if not await control.checkpoint():
            break
        candidates: list[int] = [to_int(message.sender_id)]
        from_id = getattr(message, "from_id", None)
        candidates.append(to_int(getattr(from_id, "user_id", 0)))
        for user_id in candidates:
            if user_id <= 0 or user_id == own_id or user_id in seen:
                continue
            seen.add(user_id)
            users.append(user_id)
    return users


async def _collect_users_from_participants(
    account: Account,
    entity: Any,
    limit: int,
    control: TaskControl,
) -> list[int]:
    client = account.client
    if client is None:
        return []
    users: list[int] = []
    seen: set[int] = set()
    async for user in client.iter_participants(entity, limit=limit):
        if not await control.checkpoint():
            break
        if user is None or getattr(user, "bot", False) or getattr(user, "deleted", False):
            continue
        user_id = to_int(getattr(user, "id", 0))
        if user_id <= 0 or user_id in seen:
            continue
        seen.add(user_id)
        users.append(user_id)
    return users


async def _invite_user(account: Account, target: Any, user_id: int) -> str:
    client = account.client
    if client is None:
        return "failed"
    is_channel = isinstance(target, telethon_types.Channel)
    is_megagroup = bool(getattr(target, "megagroup", False))
    try:
        if is_channel and not is_megagroup:
            input_user = await client.get_input_entity(user_id)
            await client(
                functions.channels.InviteToChannelRequest(
                    channel=target, users=[input_user], random_id=random.randint(1, 2**31)
                )
            )
        else:
            await client(
                functions.messages.AddChatUserRequest(chat=target, user_id=user_id, fwd_limit=3)
            )
    except UserAlreadyParticipantError:
        return "already"
    except UserPrivacyRestrictedError as e:
        logger.debug(f"Приватность {user_id}: {e}")
        return "privacy"
    except (InviteRequestSentError, UserBannedInChannelError) as e:
        logger.debug(f"Инвайт-статус {user_id}: {type(e).__name__}")
        return "request"
    except (
        ChannelInvalidError,
        UsernameNotOccupiedError,
        ChannelPrivateError,
        ChatAdminRequiredError,
        ValueError,
    ) as e:
        logger.warning(f"Инвайт {user_id} невозможен: {type(e).__name__}: {e}")
        return "failed"
    except AuthKeyUnregisteredError:
        account.is_valid = False
        raise
    return "success"


async def _release_chats(
    account: Account,
    target: Any | None,
    target_joined: bool,
    source: Any | None,
    source_joined: bool,
) -> None:
    if not Features.auto_leave():
        logger.info("AUTO_LEAVE выключен, чаты оставляем")
        return
    if source is not None and source_joined:
        await leave_chat(account, source, "источник")
    if target is not None and target_joined:
        await leave_chat(account, target, "цель")


async def _collect_invite_batch(
    account: Account,
    source_identifier: str,
    target_identifier: str,
    mode: str,
    message_limit: int,
    user_limit: int,
    control: TaskControl,
    language: str,
    joined_targets: dict[str, tuple[Any, str]],
) -> tuple[list[int], str, str]:
    """Собирает пользователей и проверяет цель. Возвращает (пользователи, ключ цели, ошибка)."""
    source_entity = None
    source_joined = False
    try:
        source_entity, source_joined = await join_chat(account, source_identifier)
        if mode == "users":
            collected = await _collect_users_from_participants(
                account, source_entity, user_limit or Config.SCRAPE_MAX_USER_COUNT, control
            )
        else:
            collected = await _collect_users_from_messages(
                account, source_entity, message_limit, control
            )
        if user_limit:
            collected = collected[:user_limit]
        if not collected:
            return [], "", translate(language, "texts.no_users_found", source=source_identifier)
        target_entity, target_joined = await join_chat(account, target_identifier)
        target_key = str(getattr(target_entity, "id", target_identifier))
        if target_joined:
            hold_task_chat(joined_targets, account, target_entity, target_identifier)
        return collected, target_key, ""
    except FloodWaitError:
        raise
    except AuthKeyUnregisteredError:
        account.is_valid = False
        raise
    except ChatUnreachableError as e:
        return [], "", str(e)[:900]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Сбор не удался: {type(e).__name__}: {e}")
        return [], "", f"{type(e).__name__}: {e}"[:900]
    finally:
        await _release_chats(account, None, False, source_entity, source_joined)


async def _invite_one_with_account(
    account: Account,
    target_identifier: str,
    user_id: int,
    joined_targets: dict[str, tuple[Any, str]],
) -> str:
    """Входит в цель этим аккаунтом и приглашает одного пользователя."""
    target_entity, target_joined = await join_chat(account, target_identifier)
    if target_joined:
        hold_task_chat(joined_targets, account, target_entity, target_identifier)
    return await _invite_user(account, target_entity, user_id)


async def run_scrape_invite_task(task: Task, control: TaskControl) -> None:
    data = task.data
    language = await user_db.get_language(task.user_id)
    source_identifier = str(data.get("source") or "").strip()
    target_identifier = str(data.get("target") or "").strip()
    mode = str(data.get("mode") or "messages")
    message_limit = to_int(data.get("message_limit"), default=Config.SCRAPE_MAX_MESSAGE_LIMIT)
    user_limit = to_int(data.get("user_limit"))
    chosen = str(data.get("account") or "auto")
    pinned = "" if chosen == "auto" else chosen

    reporter = TaskProgressReporter(task, control)
    await reporter.start()

    if not source_identifier or not target_identifier:
        await tasks_db.update_task(
            task.task_id,
            {
                "status": "failed",
                "error": translate(language, "texts.task_failed_no_source_target"),
                "completed_at": utc_now_iso(),
            },
        )
        await reporter.finalize()
        return

    result = InviteResult()
    joined_targets: dict[str, tuple[Any, str]] = {}
    accounts_used: list[str] = []
    collected: list[int] = []
    target_key = ""
    total = 0
    processed = 0
    stop_reason = ""
    fatal_error = ""
    flood_events = 0
    flood_seconds = 0.0

    try:
        await reporter.update(
            5.0,
            translate(
                language,
                "texts.progress_scraping",
                progress_text=format_progress_bar(5),
                processed=0,
                limit=message_limit or user_limit,
            ),
            force=True,
        )
        attempts = max(1, len(account_manager.accounts))
        for attempt in range(1, attempts + 1):
            if not await control.checkpoint():
                stop_reason = "cancelled"
                break
            try:
                async with account_manager.acquire(pinned or None, control) as account:
                    if account.session_file not in accounts_used:
                        accounts_used.append(account.session_file)
                    collected, target_key, batch_error = await _collect_invite_batch(
                        account,
                        source_identifier,
                        target_identifier,
                        mode,
                        message_limit,
                        user_limit,
                        control,
                        language,
                        joined_targets,
                    )
                    if batch_error:
                        fatal_error = batch_error
                    break
            except FloodWaitError as e:
                seconds = float(getattr(e, "seconds", 60))
                extended = account_manager.handle_flood_wait(account, seconds)
                flood_events += 1
                flood_seconds = max(flood_seconds, seconds)
                logger.warning(
                    f"Сбор: FloodWait {seconds:.0f}с (пауза {extended:.0f}с), "
                    f"пробую другой аккаунт ({attempt}/{attempts})"
                )
                continue
            except AuthKeyUnregisteredError:
                fatal_error = translate(language, "texts.task_failed_auth_reset")
                break
            except AccountWaitCancelled:
                stop_reason = "cancelled"
                break

        if not fatal_error and not collected and not stop_reason:
            fatal_error = translate(language, "texts.no_users_found", source=source_identifier)
        if fatal_error or (stop_reason and not collected):
            status_text = fatal_error or translate(language, "texts.task_cancelled_report")
            await tasks_db.update_task(
                task.task_id,
                {
                    "status": "failed" if fatal_error else "cancelled",
                    "error": status_text[:900],
                    "completed_at": utc_now_iso(),
                },
            )
            if flood_events:
                await reporter.finalize(
                    translate(
                        language,
                        "texts.roller_flood_summary",
                        count=flood_events,
                        seconds=int(flood_seconds),
                    )
                )
            else:
                await reporter.finalize()
            return

        logger.info(
            f"Задача {task.task_id}: собрано {len(collected)} пользователей, "
            f"ротация по {max(1, Config.INVITE_STEP_USERS)} на аккаунт"
        )
        total = len(collected)
        await reporter.update(
            10.0,
            translate(
                language,
                "texts.progress_inviting",
                progress_text=format_progress_bar(10),
                processed=0,
                total=total,
            ),
            force=True,
        )

        step_users = max(1, Config.INVITE_STEP_USERS)
        while processed < total:
            if not await control.checkpoint():
                stop_reason = "cancelled"
                break
            batch = collected[processed : processed + step_users]
            try:
                async with account_manager.acquire_step(pinned or None, control) as account:
                    if account.session_file not in accounts_used:
                        accounts_used.append(account.session_file)
                    for user_id in batch:
                        if not await control.checkpoint():
                            stop_reason = "cancelled"
                            break
                        if await cache_db.is_invited(target_key, user_id):
                            processed += 1
                            result.already_members += 1
                            continue
                        if account_manager.should_simulate_skip():
                            logger.debug(
                                f"Задача {task.task_id}: пользователь {user_id} пропущен "
                                f"аккаунтом {account.session_file}, беру другой"
                            )
                            break
                        processed += 1
                        try:
                            outcome = await _invite_one_with_account(
                                account, target_identifier, user_id, joined_targets
                            )
                        except FloodWaitError as e:
                            seconds = float(getattr(e, "seconds", 60))
                            extended = account_manager.handle_flood_wait(account, seconds)
                            flood_events += 1
                            flood_seconds = max(flood_seconds, seconds)
                            processed -= 1
                            logger.warning(
                                f"FloodWait на инвайте {seconds:.0f}с -> {extended:.0f}с, "
                                f"перехожу на другой аккаунт"
                            )
                            break
                        except AuthKeyUnregisteredError:
                            account.is_valid = False
                            fatal_error = translate(language, "texts.task_failed_auth_reset")
                            processed -= 1
                            break
                        except Exception as e:  # noqa: BLE001
                            logger.warning(f"Ошибка инвайта {user_id}: {type(e).__name__}: {e}")
                            outcome = "failed"

                        if outcome == "success":
                            result.success += 1
                            await cache_db.mark_invited(target_key, user_id, task.task_id)
                            account.invite_count += 1
                        elif outcome == "already":
                            result.already_members += 1
                            await cache_db.mark_invited(target_key, user_id, task.task_id)
                        elif outcome == "privacy":
                            result.privacy_errors += 1
                        elif outcome == "request":
                            result.success += 1
                            await cache_db.mark_invited(target_key, user_id, task.task_id)
                        else:
                            result.failed += 1
                        account_manager.update_success_rate(account, outcome == "success")
                        if control.is_paused or control.is_cancelled:
                            stop_reason = "cancelled"
                            break
                    if fatal_error or stop_reason:
                        break
            except AccountWaitCancelled:
                stop_reason = "cancelled"
                break
            except NoAvailableAccountError as e:
                fatal_error = str(e.message)
                break

            task.sent = result.success
            if processed % max(1, Config.INVITE_BUFFER_SIZE) == 0 or processed >= total:
                await _apply_progress(
                    control,
                    reporter,
                    processed,
                    total,
                    translate(
                        language,
                        "texts.progress_inviting",
                        progress_text=format_progress_bar(processed / total * 100),
                        processed=processed,
                        total=total,
                    ),
                )
            if stop_reason or fatal_error:
                break
            delay = rand_range(Config.MIN_INVITE_DELAY, Config.MAX_INVITE_DELAY)
            if processed % max(1, Config.HUMAN_DELAY_EVERY) == 0:
                delay = rand_range(Config.HUMAN_BREAK_MIN, Config.HUMAN_BREAK_MAX)
            if processed % max(1, Config.INVITE_BUFFER_SIZE) == 0:
                delay += rand_range(Config.POST_BUFFER_DELAY_MIN, Config.POST_BUFFER_DELAY_MAX)
            if not await control.wait(delay):
                stop_reason = "cancelled"
                break
    finally:
        await leave_task_chats(joined_targets)

    if processed < total:
        result.remaining = collected[processed:]

    task.sent = result.success
    status = resolve_final_status(control, fatal_error)
    if status == "completed" and stop_reason == "cancelled":
        status = "cancelled"
    await tasks_db.update_task(
        task.task_id,
        {
            **build_final_updates(
                control,
                {
                    "success": result.success,
                    "failed": result.failed,
                    "privacy": result.privacy_errors,
                    "already": result.already_members,
                    "processed": processed,
                    "total": total,
                    "remaining": len(result.remaining),
                    "accounts": accounts_used,
                    "flood_events": flood_events,
                },
                fatal_error,
            ),
            "sent": result.success,
        },
    )
    if status != "completed":
        await safe_send_message(
            get_queue_manager().bot,
            task.user_id,
            translate(
                language,
                "texts.task_failed_report"
                if status == "failed"
                else (
                    "texts.task_paused_report"
                    if status == "paused"
                    else "texts.task_cancelled_report"
                ),
                task_id=task.task_id,
                processed=processed,
                total=total,
                error=fatal_error,
            ),
            reply_markup=main_menu_keyboard(language),
        )
        await reporter.finalize()
        return
    accounts_label = format_accounts_summary(accounts_used)
    await safe_send_message(
        get_queue_manager().bot,
        task.user_id,
        translate(
            language,
            "texts.task_completed_report_multi"
            if len(accounts_used) > 1
            else "texts.task_completed_report",
            task_id=task.task_id,
            source=source_identifier,
            target=target_identifier,
            invited=processed,
            success=result.success,
            already=result.already_members,
            failed=result.failed,
            privacy=result.privacy_errors,
            account=accounts_label,
        ),
        reply_markup=main_menu_keyboard(language),
    )
    if flood_events:
        await reporter.finalize(
            translate(
                language,
                "texts.roller_flood_summary",
                count=flood_events,
                seconds=int(flood_seconds),
            )
        )
        return
    await reporter.finalize()


async def _collect_mailing_users(
    account: Account,
    chats: list[str],
    limit: int,
    control: TaskControl,
) -> list[int]:
    require_client(account)
    seen: set[int] = set()
    for identifier in chats:
        if not await control.checkpoint():
            break
        cached = await cache_db.get_cached_participants(identifier)
        candidates = cached or []
        if not candidates:
            entity, joined = await join_chat(account, identifier)
            participants = await _collect_users_from_participants(
                account, entity, Config.SCRAPE_MAX_USER_COUNT, control
            )
            if participants:
                await cache_db.cache_participants(identifier, participants)
            if Features.auto_leave() and joined:
                await leave_chat(account, entity, identifier)
            candidates = participants
        for user_id in candidates:
            if user_id not in seen:
                seen.add(user_id)
            if len(seen) >= limit:
                return list(seen)
    logger.info(f"Рассылка в ЛС: собрано пользователей {len(seen)}")
    return list(seen)


async def run_mailing_task(task: Task, control: TaskControl) -> None:
    data = task.data
    language = await user_db.get_language(task.user_id)
    chats: list[str] = [str(item) for item in data.get("chats", []) if str(item).strip()]
    texts: list[str] = [str(item) for item in data.get("texts", []) if str(item).strip()]
    min_delay = to_int(data.get("min_delay"), default=Config.MAILING_MIN_DELAY)
    max_delay = to_int(data.get("max_delay"), default=Config.MAILING_MAX_DELAY)
    max_sends = to_int(data.get("total"), default=Config.MAILING_MAX_TOTAL_SENDS)
    sender = str(data.get("account") or "auto")
    pinned = "" if sender == "auto" else sender
    target = str(data.get("target") or "chats")

    reporter = TaskProgressReporter(task, control, interval=float(Config.MAILING_CHECK_INTERVAL))
    await reporter.start()

    if not chats or not texts:
        await tasks_db.update_task(
            task.task_id,
            {
                "status": "failed",
                "error": translate(language, "texts.task_failed_no_chats_or_texts"),
                "completed_at": utc_now_iso(),
            },
        )
        await reporter.finalize()
        return

    sent = 0
    per_chat: dict[str, int] = {}
    errors = 0
    privacy = 0
    fatal_error = ""
    flood_events = 0
    flood_seconds = 0.0
    accounts_used: list[str] = []
    recipients: list[Any] = []
    total = 0
    processed = 0

    if target == "users":
        try:
            async with account_manager.acquire(pinned or None, control) as account:
                if account.session_file not in accounts_used:
                    accounts_used.append(account.session_file)
                recipients = await _collect_mailing_users(
                    account, chats, Config.SCRAPE_MAX_USER_COUNT, control
                )
        except FloodWaitError as e:
            seconds = float(getattr(e, "seconds", 60))
            account_manager.handle_flood_wait(account, seconds)
            flood_events += 1
            flood_seconds = max(flood_seconds, seconds)
            fatal_error = translate(
                language, "texts.task_failed_flood_scrape", seconds=int(seconds)
            )
        except AuthKeyUnregisteredError:
            fatal_error = translate(language, "texts.task_failed_auth_reset")
        except AccountWaitCancelled:
            pass
        except NoAvailableAccountError as e:
            fatal_error = str(e.message)
    else:
        recipients = list(chats)
        total_planned = min(
            max_sends if max_sends > 0 else Config.MAILING_MAX_TOTAL_SENDS,
            Config.MAILING_MAX_TOTAL_SENDS,
        )
        if total_planned > len(recipients):
            recipients = [recipients[index % len(recipients)] for index in range(total_planned)]

    if fatal_error:
        await tasks_db.update_task(
            task.task_id,
            {
                "status": "failed",
                "error": fatal_error,
                "completed_at": utc_now_iso(),
            },
        )
        await reporter.finalize()
        return

    if not recipients:
        await tasks_db.update_task(
            task.task_id,
            {
                "status": "failed",
                "error": translate(language, "texts.bulkmail_no_users"),
                "completed_at": utc_now_iso(),
            },
        )
        await reporter.finalize()
        return

    total = len(recipients)
    step_messages = max(1, Config.MAILING_STEP_MESSAGES)
    consecutive_errors = 0
    while processed < total:
        if not await control.checkpoint():
            break
        batch = recipients[processed : processed + step_messages]
        try:
            async with account_manager.acquire_step(pinned or None, control) as account:
                if account.session_file not in accounts_used:
                    accounts_used.append(account.session_file)
                for recipient in batch:
                    key = str(recipient)
                    entity: Any = None
                    joined = False
                    if target != "users":
                        try:
                            entity, joined = await join_chat(account, key)
                        except FloodWaitError as e:
                            seconds = float(getattr(e, "seconds", 60))
                            account_manager.handle_flood_wait(
                                account,
                                seconds + Config.MAILING_FLOOD_WAIT_PADDING,
                            )
                            flood_events += 1
                            flood_seconds = max(flood_seconds, seconds)
                            errors += 1
                            processed += 1
                            logger.warning(
                                f"Рассылка: FloodWait на {key} {seconds:.0f}с, "
                                f"перехожу на другой аккаунт"
                            )
                            continue
                        except ChatUnreachableError as e:
                            logger.warning(f"Рассылка: чат {key} недоступен: {e}")
                            errors += 1
                            processed += 1
                            continue
                        except AuthKeyUnregisteredError:
                            account.is_valid = False
                            fatal_error = translate(language, "texts.task_failed_auth_reset")
                            break
                        destination: Any = entity
                    else:
                        destination = key

                    text = texts[random.randrange(len(texts))]
                    try:
                        await send_formatted(require_client(account), destination, text)
                        sent += 1
                        per_chat[key] = per_chat.get(key, 0) + 1
                        consecutive_errors = 0
                    except FloodWaitError as e:
                        seconds = float(getattr(e, "seconds", 60))
                        account_manager.handle_flood_wait(
                            account, seconds + Config.MAILING_FLOOD_WAIT_PADDING
                        )
                        flood_events += 1
                        flood_seconds = max(flood_seconds, seconds)
                        errors += 1
                        consecutive_errors += 1
                    except AuthKeyUnregisteredError:
                        account.is_valid = False
                        fatal_error = translate(language, "texts.task_failed_auth_reset")
                        break
                    except UserPrivacyRestrictedError:
                        privacy += 1
                        consecutive_errors += 1
                    except (UserIsBlockedError, InputUserDeactivatedError) as e:
                        logger.debug(f"Рассылка в ЛС {key} невозможна: {type(e).__name__}")
                        errors += 1
                        consecutive_errors += 1
                    except (ChatWriteForbiddenError, ChatAdminRequiredError) as e:
                        logger.warning(f"Рассылка в {key} запрещена: {e}")
                        errors += 1
                        consecutive_errors += 1
                    finally:
                        processed += 1
                        if target != "users" and Features.auto_leave() and joined:
                            await leave_chat(account, entity, key)
                    if fatal_error or control.is_cancelled:
                        break
                if fatal_error:
                    break
        except AccountWaitCancelled:
            break
        except NoAvailableAccountError as e:
            fatal_error = str(e.message)
            break

        task.sent = sent
        await _apply_progress(
            control,
            reporter,
            processed,
            total,
            translate(
                language,
                "texts.progress_mailing",
                progress_text=format_progress_bar(processed / total * 100),
                processed=processed,
                total=total,
            ),
        )
        if fatal_error:
            break
        if consecutive_errors >= Config.MAILING_MAX_CONSECUTIVE_ERRORS:
            logger.warning(
                f"Рассылка: {consecutive_errors} ошибок подряд, пауза "
                f"{Config.MAILING_CONSECUTIVE_ERROR_LONG_DELAY}с"
            )
            if not await control.wait(Config.MAILING_CONSECUTIVE_ERROR_LONG_DELAY):
                break
            consecutive_errors = 0
        elif consecutive_errors:
            if not await control.wait(Config.MAILING_CONSECUTIVE_ERROR_SHORT_DELAY):
                break
        elif not await control.wait(rand_range(min_delay, max_delay)):
            break

    task.sent = sent
    status = resolve_final_status(control, fatal_error)
    report_lines = "\n".join(
        translate(language, "texts.mailing_chat_line", chat=chat, count=count)
        for chat, count in list(per_chat.items())[:20]
    )
    await tasks_db.update_task(
        task.task_id,
        {
            **build_final_updates(
                control,
                {
                    "sent": sent,
                    "chats": per_chat,
                    "errors": errors,
                    "privacy": privacy,
                    "target": target,
                    "processed": processed,
                    "total": total,
                    "accounts": accounts_used,
                    "flood_events": flood_events,
                },
                fatal_error,
            ),
            "sent": sent,
        },
    )
    if status != "completed":
        await safe_send_message(
            get_queue_manager().bot,
            task.user_id,
            translate(
                language,
                "texts.task_failed_report"
                if status == "failed"
                else (
                    "texts.mailing_cancelled_report"
                    if status == "cancelled"
                    else "texts.task_paused_report"
                ),
                task_id=task.task_id,
                sent=sent,
                processed=processed,
                total=total,
                error=fatal_error,
            ),
            reply_markup=main_menu_keyboard(language),
        )
        await reporter.finalize()
        return
    if target == "users":
        summary = translate(
            language,
            "texts.mailing_completed_users_report",
            task_id=task.task_id,
            sent=sent,
            texts_count=len(texts),
            users_count=len(per_chat),
        )
    else:
        summary = translate(
            language,
            "texts.mailing_completed_report",
            task_id=task.task_id,
            sent=sent,
            texts_count=len(texts),
            chats_report=report_lines or "-",
        )
    if accounts_used:
        summary += "\n\n" + translate(
            language,
            "texts.roller_accounts_used",
            count=len(accounts_used),
            accounts=format_accounts_summary(accounts_used),
        )
    if flood_events:
        summary += "\n\n" + translate(
            language,
            "texts.roller_flood_summary",
            count=flood_events,
            seconds=int(flood_seconds),
        )
    await safe_send_message(
        get_queue_manager().bot,
        task.user_id,
        summary,
        reply_markup=main_menu_keyboard(language),
    )
    await reporter.finalize()


async def _scan_worm_source(
    account: Account,
    identifier: str,
    language: str,
    seen: set[str],
    control: TaskControl,
) -> WormSourceStats:
    """Сканирует один источник. FloodWait и AuthKeyUnregisteredError пробрасываются."""
    stats = WormSourceStats()
    entity = None
    joined = False
    try:
        entity, joined = await join_chat(account, identifier)
    except FloodWaitError:
        raise
    except AuthKeyUnregisteredError:
        account.is_valid = False
        raise
    except (ChatUnreachableError, ValueError) as e:
        stats.errors += 1
        logger.warning(f"Червь: источник {identifier} недоступен: {e}")
        return stats

    client = require_client(account)
    try:
        async for message in client.iter_messages(entity, limit=Config.WORM_SCAN_LIMIT):
            if not await control.checkpoint():
                break
            stats.messages += 1
            text = getattr(message, "message", None) or getattr(message, "text", None)
            if not text:
                continue
            for link in extract_telegram_links(text):
                stats.links += 1
                target = parse_link_to_identifier(link)
                if not target or target in seen:
                    continue
                seen.add(target)
                if len(seen) > Config.WORM_SEEN_LIMIT:
                    logger.info(f"Червь: достигнут лимит {Config.WORM_SEEN_LIMIT} ссылок")
                    break
                if await chat_db.get_chat(target) or await chat_db.get_chat(
                    f"https://t.me/{target}"
                ):
                    continue
                if stats.added >= Config.WORM_MAX_SOURCES:
                    break
                validated = await validate_and_test_chat(account, target, language)
                if validated is not None:
                    stats.added += 1
                if not await control.wait(rand_range(Config.WORM_MIN_DELAY, Config.WORM_MAX_DELAY)):
                    break
    except FloodWaitError:
        raise
    except AuthKeyUnregisteredError:
        account.is_valid = False
        raise
    except Exception as e:  # noqa: BLE001
        stats.errors += 1
        logger.error(f"Червь: ошибка обработки {identifier}: {e}")
    finally:
        if joined:
            await leave_chat(account, entity, identifier)
    return stats


async def run_worm_task(task: Task, control: TaskControl) -> None:
    data = task.data
    language = await user_db.get_language(task.user_id)
    sources: list[str] = [str(item) for item in data.get("sources", []) if str(item).strip()]
    reporter = TaskProgressReporter(task, control, interval=float(Config.WORM_CHECK_INTERVAL))
    await reporter.start()

    if not sources:
        await tasks_db.update_task(
            task.task_id,
            {
                "status": "failed",
                "error": translate(language, "texts.task_failed_no_sources"),
                "completed_at": utc_now_iso(),
            },
        )
        await reporter.finalize()
        return

    totals = WormSourceStats()
    processed_sources: list[str] = []
    seen: set[str] = set(sources)
    fatal_error = ""
    flood_events = 0
    flood_seconds = 0.0
    accounts_used: list[str] = []

    for identifier in sources:
        if not await control.checkpoint():
            break
        try:
            async with account_manager.acquire_step(None, control) as account:
                if account.session_file not in accounts_used:
                    accounts_used.append(account.session_file)
                stats = await _scan_worm_source(account, identifier, language, seen, control)
        except FloodWaitError as e:
            seconds = float(getattr(e, "seconds", 60))
            account_manager.handle_flood_wait(account, seconds)
            flood_events += 1
            flood_seconds = max(flood_seconds, seconds)
            logger.warning(f"Червь: FloodWait на {identifier} {seconds:.0f}с, беру другой аккаунт")
            continue
        except AuthKeyUnregisteredError:
            fatal_error = translate(language, "texts.task_failed_auth_reset")
            break
        except AccountWaitCancelled:
            break
        except NoAvailableAccountError as e:
            fatal_error = str(e.message)
            break

        processed_sources.append(identifier)
        totals.messages += stats.messages
        totals.links += stats.links
        totals.added += stats.added
        totals.errors += stats.errors
        await _apply_progress(
            control,
            reporter,
            len(processed_sources),
            len(sources),
            translate(
                language,
                "texts.progress_worm",
                added=stats.added,
                processed=len(processed_sources),
                total=len(sources),
            ),
        )
        if processed_sources[-1] != sources[-1] and not await control.wait(
            rand_range(Config.WORM_MIN_DELAY, Config.WORM_MAX_DELAY)
        ):
            break

    status = resolve_final_status(control, fatal_error)
    await tasks_db.update_task(
        task.task_id,
        build_final_updates(
            control,
            {
                "messages": totals.messages,
                "links": totals.links,
                "added": totals.added,
                "errors": totals.errors,
                "accounts": accounts_used,
                "flood_events": flood_events,
            },
            fatal_error,
        ),
    )
    if accounts_used:
        await safe_send_message(
            get_queue_manager().bot,
            task.user_id,
            translate(
                language,
                "texts.roller_accounts_used",
                count=len(accounts_used),
                accounts=format_accounts_summary(accounts_used),
            ),
            reply_markup=main_menu_keyboard(language),
        )
    await reporter.finalize(
        translate(
            language,
            "texts.worm_stopped_multi"
            if status == "completed"
            else (
                "texts.task_failed_report"
                if status == "failed"
                else (
                    "texts.task_paused_report"
                    if status == "paused"
                    else "texts.task_cancelled_report"
                )
            ),
            messages=totals.messages,
            links=totals.links,
            added=totals.added,
            errors=totals.errors,
            task_id=task.task_id,
            processed=len(processed_sources),
            total=len(sources),
            error=fatal_error,
        )
    )


# --- FSM ---
class InviterStates(StatesGroup):
    waiting_phone: State = State()
    waiting_code: State = State()
    waiting_password: State = State()
    waiting_links: State = State()
    waiting_worm_chats: State = State()
    waiting_scrape_source: State = State()
    waiting_scrape_target: State = State()
    waiting_scrape_limit: State = State()
    waiting_mail_chats: State = State()
    waiting_mail_delay: State = State()
    waiting_mail_text: State = State()
    waiting_mail_total: State = State()


async def _drop_login_client(user_id: int) -> None:
    client = LOGIN_CLIENTS.pop(user_id, None)
    if client is None:
        return
    with suppress(Exception):
        if client.is_connected():
            await client.disconnect()


def is_phone(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    return 10 <= len(digits) <= 15


def parse_identifier(value: str) -> str | None:
    return parse_link_to_identifier(value)


# --- Клавиатуры ---
def kb(rows: list[list[dict[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(**btn) for btn in row] for row in rows]
    )


CALLBACK_DATA_MAX_BYTES: int = 64


def parse_account_callback(data: str, prefix: str) -> str:
    raw = str(data or "")
    if raw == f"{prefix}auto":
        return "auto"
    marker = f"{prefix}session:"
    return raw[len(marker) :] if raw.startswith(marker) else "auto"


# Принцип построения клавиатур - как в эталоне VPN_service_for_3X-UI:
# у каждого экрана своя функция и только свои кнопки. Экран, который ждёт ввода,
# несёт кнопку отмены и ничего больше - кнопки главного меню на нём быть не должно.


def main_row(language: str) -> list[dict[str, str]]:
    return [{"text": translate(language, "buttons.main"), "callback_data": "start"}]


def cancel_row(language: str) -> list[dict[str, str]]:
    return [{"text": translate(language, "buttons.cancel"), "callback_data": "cancel"}]


def main_menu_keyboard(language: str, *, worm_active: bool = False) -> InlineKeyboardMarkup:
    """Главное меню. Последняя кнопка - имя текущего языка, как в эталоне."""
    if not account_manager.accounts:
        return kb(
            [
                [
                    {
                        "text": translate(language, "buttons.add_account"),
                        "callback_data": "add_account",
                    }
                ],
                [
                    {
                        "text": get_language_display_name(language),
                        "callback_data": "change_language",
                    }
                ],
            ]
        )
    rows: list[list[dict[str, str]]] = [
        [
            {
                "text": translate(language, "buttons.start_scraping"),
                "callback_data": "scrape",
            },
            {
                "text": translate(language, "buttons.bulk_mailing"),
                "callback_data": "mailing",
            },
        ],
        [
            {
                "text": translate(language, "buttons.my_tasks"),
                "callback_data": "task:list:mine",
            },
            {
                "text": translate(language, "buttons.task_list"),
                "callback_data": "task:list:active",
            },
        ],
        [
            {
                "text": translate(language, "buttons.add_chats_to_db"),
                "callback_data": "add_chats",
            },
            {
                "text": translate(language, "buttons.update_chats_db"),
                "callback_data": "update_chats",
            },
        ],
        [
            {
                "text": translate(language, "buttons.add_account"),
                "callback_data": "add_account",
            },
            {
                "text": translate(language, "buttons.list_accounts"),
                "callback_data": "accounts",
            },
        ],
        [
            {
                "text": translate(language, "buttons.clear_cache"),
                "callback_data": "clear_cache",
            },
            {
                "text": translate(language, "buttons.cancel_all_tasks"),
                "callback_data": "cancel_all",
            },
        ],
    ]
    if Features.worm_mode():
        rows.append([{"text": translate(language, "buttons.worm_mode"), "callback_data": "worm"}])
    if worm_active:
        rows.append(
            [{"text": translate(language, "buttons.stop_worm"), "callback_data": "stop_worm"}]
        )
    rows.append(
        [
            {
                "text": get_language_display_name(language),
                "callback_data": "change_language",
            }
        ]
    )
    return kb(rows)


def build_language_keyboard(language: str) -> InlineKeyboardMarkup:
    """Экран выбора языка: по кнопке на язык, снизу возврат на главную."""
    rows: list[list[dict[str, str]]] = [
        [{"text": get_language_display_name(code), "callback_data": f"lang:{code}"}]
        for code in get_available_languages()
    ]
    rows.append(main_row(language))
    return kb(rows)


def cancel_keyboard(language: str) -> InlineKeyboardMarkup:
    """Шаг мастера, ожидающий свободный ввод: только отмена."""
    return kb([cancel_row(language)])


def accounts_keyboard(language: str) -> InlineKeyboardMarkup:
    """Список аккаунтов: обновить, добавить, вернуться."""
    return kb(
        [
            [
                {
                    "text": translate(language, "buttons.add_account"),
                    "callback_data": "add_account",
                },
                {
                    "text": translate(language, "buttons.task_refresh"),
                    "callback_data": "accounts",
                },
            ],
            main_row(language),
        ]
    )


def no_accounts_keyboard(language: str) -> InlineKeyboardMarkup:
    return kb(
        [
            [
                {
                    "text": translate(language, "buttons.add_account"),
                    "callback_data": "add_account",
                }
            ],
            main_row(language),
        ]
    )


def scrape_mode_keyboard(language: str) -> InlineKeyboardMarkup:
    return kb(
        [
            [
                {
                    "text": translate(language, "buttons.mode_messages"),
                    "callback_data": "scrape:mode:messages",
                },
                {
                    "text": translate(language, "buttons.mode_users"),
                    "callback_data": "scrape:mode:users",
                },
            ],
            cancel_row(language),
        ]
    )


def source_keyboard(language: str, prefix: str) -> InlineKeyboardMarkup:
    return kb(
        [
            [
                {
                    "text": translate(language, "buttons.source_from_db"),
                    "callback_data": f"{prefix}:db",
                }
            ],
            [
                {
                    "text": translate(language, "buttons.source_manual"),
                    "callback_data": f"{prefix}:manual",
                }
            ],
            cancel_row(language),
        ]
    )


def account_keyboard(language: str, prefix: str) -> InlineKeyboardMarkup:
    rows: list[list[dict[str, str]]] = [
        [
            {
                "text": translate(language, "buttons.auto_account"),
                "callback_data": f"{prefix}:auto",
            }
        ]
    ]
    rows.extend(
        [
            [
                {
                    "text": html.escape(account.session_file),
                    "callback_data": f"{prefix}:session:{account.session_file}",
                }
            ]
            for account in account_manager.accounts[:10]
            if len(f"{prefix}:session:{account.session_file}".encode()) <= CALLBACK_DATA_MAX_BYTES
        ]
    )
    rows.append(cancel_row(language))
    return kb(rows)


def mail_target_keyboard(language: str) -> InlineKeyboardMarkup:
    return kb(
        [
            [
                {
                    "text": translate(language, "buttons.mail_target_chats"),
                    "callback_data": "mail:target:chats",
                }
            ],
            [
                {
                    "text": translate(language, "buttons.mail_target_users"),
                    "callback_data": "mail:target:users",
                }
            ],
            cancel_row(language),
        ]
    )


def mail_texts_done_keyboard(language: str) -> InlineKeyboardMarkup:
    return kb(
        [
            [
                {
                    "text": translate(language, "buttons.mail_texts_done"),
                    "callback_data": "mail:texts_done",
                }
            ],
            cancel_row(language),
        ]
    )


def confirm_keyboard(language: str, accept_data: str) -> InlineKeyboardMarkup:
    """Подтверждение: ровно две кнопки - да и нет."""
    return kb(
        [
            [
                {
                    "text": translate(language, "buttons.confirm_yes"),
                    "callback_data": accept_data,
                },
                {
                    "text": translate(language, "buttons.confirm_no"),
                    "callback_data": "start",
                },
            ]
        ]
    )


def task_list_keyboard(language: str, scope: str, tasks: list[Task]) -> InlineKeyboardMarkup:
    """Список задач: сами задачи, обновление, возврат на главную."""
    rows: list[list[dict[str, str]]] = [
        [
            {
                "text": f"{task_status_emoji(task.status)} {html.escape(task.task_id)}",
                "callback_data": f"task:view:{task.task_id}",
            }
        ]
        for task in tasks[:8]
    ]
    rows.append(
        [
            {
                "text": translate(language, "buttons.task_refresh"),
                "callback_data": f"task:list:{scope}",
            }
        ]
    )
    rows.append(main_row(language))
    return kb(rows)


# --- Middleware ---
class OwnerMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if isinstance(event, CallbackQuery) and event.from_user is not None:
            user = event.from_user
        user_id = to_int(getattr(user, "id", 0))
        if not is_owner_user(user_id):
            logger.warning(f"Отклонён доступ для {user_id}")
            client_code = str(getattr(user, "language_code", "") or "").strip().lower()
            client_language = client_code if client_code in LANGUAGES else DEFAULT_LANGUAGE
            if isinstance(event, Message):
                await event.answer(translate(client_language, "texts.only_admin"))
            elif isinstance(event, CallbackQuery):
                await event.answer(translate(client_language, "texts.only_admin"), show_alert=True)
            return None
        data["user_id"] = user_id
        data["language"] = await user_db.get_language(user_id)
        token = LANGUAGE_CONTEXT.set(data["language"])
        try:
            return await handler(event, data)
        finally:
            LANGUAGE_CONTEXT.reset(token)


class ErrorLogMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except (TelegramBadRequest, TelegramNetworkError) as e:
            logger.warning(f"Сетевая ошибка Telegram: {e}")
        except BotError as e:
            logger.error(f"Ошибка бота: {e}")
            error_text = translate(
                data.get("language", DEFAULT_LANGUAGE),
                "texts.error_prefix",
                error=e.message,
            )
            if isinstance(event, Message):
                with suppress(TelegramBadRequest, TelegramNetworkError):
                    await event.answer(error_text)
            elif isinstance(event, CallbackQuery):
                with suppress(TelegramBadRequest, TelegramNetworkError):
                    await event.answer(error_text, show_alert=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Необработанная ошибка")
            if isinstance(event, Message):
                with suppress(TelegramBadRequest, TelegramNetworkError):
                    await event.answer(
                        translate(data.get("language", DEFAULT_LANGUAGE), "texts.invalid_format")
                    )
        return None


# --- Экраны: сборка текста и клавиатуры в одном месте ---
@dataclass(frozen=True, slots=True)
class Screen:
    """Единица отображения.

    Экран - это текст плюс клавиатура. Хэндлеры не собирают сообщения, они
    выбирают экран и показывают его, поэтому правка разметки или формулировки
    не требует правки бизнес-логики.
    """

    text: str
    markup: InlineKeyboardMarkup | None = None
    delete_origin: bool = False

    async def show(self, event: Message | CallbackQuery) -> bool:
        return await smart_answer(
            event, self.text, self.markup, self.delete_origin or isinstance(event, CallbackQuery)
        )


SCRAPE_WIZARD_STEPS: int = 5
MAILING_WIZARD_STEPS: int = 6


def screen_text(
    language: str,
    title_key: str,
    body_key: str = "",
    *,
    step: int = 0,
    total: int = 0,
    **kwargs: Any,
) -> str:
    """Собирает текст экрана по эталону: заголовок, пустая строка, тело.

    `step`/`total` добавляют счётчик шага мастера. Заголовок и счётчик
    повторяются на каждом шаге, чтобы пользователь не зависел от истории
    переписки.
    """
    counter = (
        translate(language, "texts.step_counter", current=step, total=total)
        if step and total
        else ""
    )
    body = translate(language, body_key, **kwargs) if body_key else ""
    if counter and body:
        text = f"{translate(language, title_key)}\n\n{counter} {body}"
    else:
        text = f"{translate(language, title_key)}\n\n{counter or body}"
    unresolved = PLACEHOLDER_PATTERN.search(text)
    if unresolved is not None:
        raise BotError(f"{title_key}: не подставлено {unresolved.group(0)}")
    return text


def screen_notice(language: str, key: str, **kwargs: Any) -> str:
    return translate(language, key, **kwargs)


def task_status_emoji(status: str) -> str:
    return {
        "pending": "⏳",
        "running": "🔄",
        "paused": "⏸",
        "completed": "✅",
        "cancelled": "🚫",
        "failed": "🔥",
    }.get(status, "•")


def format_task_row(task: Task, language: str) -> str:
    row = (
        f"{task_status_emoji(task.status)} <code>{html.escape(task.task_id)}</code> - "
        f"{format_task_type(task.type, language)} "
        f"{format_progress_bar(task.progress)}\n"
    )
    if task.progress_text:
        row += f"   {html.escape(task.progress_text)}\n"
    return row


async def download_links_from_document(bot: Bot, message: Message) -> list[str]:
    buffer = io.BytesIO()
    await bot.download(message.document, destination=buffer)
    buffer.seek(0)
    raw = buffer.read().decode("utf-8", errors="ignore")
    return extract_telegram_links(raw)


async def parse_links_from_message(message: Message) -> list[str]:
    if message.document is not None:
        return await download_links_from_document(message.bot, message)
    return extract_telegram_links(message.text or message.caption or "")


async def render_main_menu(
    event: Message | CallbackQuery, language: str, delete_origin: bool = False
) -> None:
    active = await tasks_db.get_active_tasks()
    await Screen(
        text=truncate(
            translate(
                language,
                "texts.welcome_admin",
                accounts=len(account_manager.accounts),
                chats=await chat_db.get_active_chats_count(),
                users=await chat_db.get_total_users(),
                tasks=len(active),
            )
        ),
        markup=main_menu_keyboard(
            language, worm_active=any(item.type == "worm" for item in active)
        ),
        delete_origin=delete_origin,
    ).show(event)


async def render_task_list(
    event: Message | CallbackQuery, language: str, scope: str, delete_origin: bool = False
) -> None:
    title_key = "texts.task_list_mine" if scope == "mine" else "texts.task_list_active"
    if scope == "mine":
        tasks = [
            task for task in await tasks_db.get_user_tasks(event.from_user.id) if task.is_active
        ]
    else:
        tasks = list(await tasks_db.get_active_tasks())
    if not tasks:
        await Screen(
            text=translate(language, "texts.no_active_tasks"),
            markup=main_menu_keyboard(language),
            delete_origin=delete_origin,
        ).show(event)
        return
    lines = [f"{translate(language, title_key)}\n"]
    lines.extend(format_task_row(task, language) for task in tasks[:20])
    await Screen(
        text=truncate("".join(lines)),
        markup=task_list_keyboard(language, scope, tasks),
        delete_origin=delete_origin,
    ).show(event)


async def render_prompt(
    event: Message | CallbackQuery,
    language: str,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    delete_origin: bool = False,
) -> None:
    """Шаг мастера: текст и кнопки этого шага. Без кнопок главного меню."""
    await Screen(
        text=text,
        markup=cancel_keyboard(language) if markup is None else markup,
        delete_origin=delete_origin,
    ).show(event)


async def render_notice(
    event: Message | CallbackQuery, language: str, key: str, **kwargs: Any
) -> None:
    """Короткое уведомление после действия: главное меню тут уместно."""
    await Screen(
        text=screen_notice(language, key, **kwargs),
        markup=main_menu_keyboard(language),
        delete_origin=isinstance(event, CallbackQuery),
    ).show(event)


# --- Обработчики: старт и меню ---
@router.message(Command("start"))
@router.callback_query(F.data == "start")
@log_error
async def cmd_start(event: Message | CallbackQuery, state: FSMContext, language: str) -> None:
    await state.clear()
    await render_main_menu(event, language, delete_origin=isinstance(event, CallbackQuery))


@router.callback_query(F.data == "cancel")
@router.message(Command("cancel"))
@log_error
async def cmd_cancel(event: Message | CallbackQuery, state: FSMContext, language: str) -> None:
    had_state = await state.get_state() is not None
    await state.clear()
    if not had_state:
        await render_main_menu(event, language, delete_origin=isinstance(event, CallbackQuery))
        return
    await _drop_login_client(to_int(event.from_user.id))
    await render_notice(event, language, "texts.action_cancelled")


async def prompt_language_selection(event: Message | CallbackQuery, language: str) -> None:
    """Экран выбора языка - как в эталоне: список языков и возврат на главную."""
    await Screen(
        text=translate(language, "texts.language_prompt"),
        markup=build_language_keyboard(language),
        delete_origin=isinstance(event, CallbackQuery),
    ).show(event)


@router.message(Command("language"))
@router.callback_query(F.data == "change_language")
@log_error
async def cb_change_language(event: Message | CallbackQuery, language: str) -> None:
    await prompt_language_selection(event, language)


@router.callback_query(F.data.startswith("lang:"))
@log_error
async def cb_language_set(call: CallbackQuery, state: FSMContext, language: str) -> None:
    code = str(call.data or "").split(":", 1)[1].strip().lower()
    if code not in LANGUAGES:
        await call.answer(translate(language, "texts.language_unknown"), show_alert=True)
        return
    if not await user_db.set_language(call.from_user.id, code):
        await call.answer(translate(language, "texts.language_save_failed"), show_alert=True)
        return
    await state.clear()
    await call.answer(
        translate(code, "texts.language_selected", name=get_language_display_name(code)),
        show_alert=True,
    )
    await render_main_menu(call, code, delete_origin=True)


@router.callback_query(F.data == "stop_worm")
async def cb_stop_worm(call: CallbackQuery, state: FSMContext, language: str) -> None:
    await state.clear()
    await cmd_stop_worm(call, language)


# --- Аккаунты ---
@router.callback_query(F.data == "add_account")
async def cb_add_account(call: CallbackQuery, state: FSMContext, language: str) -> None:
    await state.clear()
    await call.answer()
    await render_prompt(call, language, translate(language, "texts.waiting_phone"))
    await state.set_state(InviterStates.waiting_phone)


@router.message(InviterStates.waiting_phone)
async def on_phone(message: Message, state: FSMContext, language: str) -> None:
    phone = (message.text or "").strip()
    if not is_phone(phone):
        await smart_answer(
            message,
            translate(language, "texts.auth_failed"),
            reply_markup=cancel_keyboard(language),
        )
        return
    await _drop_login_client(message.from_user.id)
    client = create_telegram_client()
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
    except (PhoneNumberInvalidError, FloodError) as e:
        await client.disconnect()
        await smart_answer(
            message,
            translate(language, "texts.auth_failed"),
            reply_markup=cancel_keyboard(language),
        )
        logger.warning(f"Ошибка отправки кода: {e}")
        return
    except Exception as e:  # noqa: BLE001
        await client.disconnect()
        logger.error(f"Ошибка подключения при добавлении аккаунта: {e}")
        await smart_answer(
            message,
            translate(language, "texts.auth_failed"),
            reply_markup=cancel_keyboard(language),
        )
        return
    LOGIN_CLIENTS[message.from_user.id] = client
    await state.update_data(
        phone=phone,
        phone_code_hash=sent.phone_code_hash,
        session_name=re.sub(r"\D", "", phone),
    )
    await smart_answer(
        message,
        translate(language, "texts.waiting_code", phone=phone),
        reply_markup=cancel_keyboard(language),
    )
    await state.set_state(InviterStates.waiting_code)


@router.message(InviterStates.waiting_code)
async def on_code(message: Message, state: FSMContext, language: str) -> None:
    code = re.sub(r"\D", "", message.text or "")
    data = await state.get_data()
    client = LOGIN_CLIENTS.get(message.from_user.id)
    if client is None or not code:
        await state.clear()
        await smart_answer(
            message,
            translate(language, "texts.auth_error_state"),
            reply_markup=cancel_keyboard(language),
        )
        return
    try:
        await client.sign_in(
            phone=data.get("phone"), code=code, phone_code_hash=data.get("phone_code_hash")
        )
    except SessionPasswordNeededError:
        await smart_answer(
            message,
            translate(language, "texts.waiting_password"),
            reply_markup=cancel_keyboard(language),
        )
        await state.set_state(InviterStates.waiting_password)
        return
    except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
        logger.warning(f"Неверный код: {e}")
        await smart_answer(
            message,
            translate(language, "texts.auth_failed"),
            reply_markup=cancel_keyboard(language),
        )
        return
    except Exception as e:  # noqa: BLE001
        logger.error(f"Ошибка входа: {e}")
        await _drop_login_client(message.from_user.id)
        await state.clear()
        await smart_answer(
            message,
            translate(language, "texts.auth_failed"),
            reply_markup=cancel_keyboard(language),
        )
        return
    await _finish_account_login(message, state, client, language)


@router.message(InviterStates.waiting_password)
async def on_password(message: Message, state: FSMContext, language: str) -> None:
    client = LOGIN_CLIENTS.get(message.from_user.id)
    if client is None:
        await state.clear()
        await smart_answer(
            message,
            translate(language, "texts.auth_error_state"),
            reply_markup=cancel_keyboard(language),
        )
        return
    try:
        await client.sign_in(password=message.text or "")
    except PasswordHashInvalidError:
        await smart_answer(
            message,
            translate(language, "texts.auth_failed"),
            reply_markup=cancel_keyboard(language),
        )
        return
    except Exception as e:  # noqa: BLE001
        logger.error(f"Ошибка ввода пароля: {e}")
        await _drop_login_client(message.from_user.id)
        await state.clear()
        await smart_answer(
            message,
            translate(language, "texts.auth_failed"),
            reply_markup=cancel_keyboard(language),
        )
        return
    await _finish_account_login(message, state, client, language)


async def _finish_account_login(
    message: Message, state: FSMContext, client: TelegramClient, language: str
) -> None:
    data = await state.get_data()
    session_name = str(data.get("session_name") or "account")
    try:
        me = await client.get_me()
        session_string = client.session.save()
    except Exception as e:  # noqa: BLE001
        logger.error(f"Не удалось сохранить сессию: {e}")
        await _drop_login_client(message.from_user.id)
        await state.clear()
        await smart_answer(
            message,
            translate(language, "texts.auth_failed"),
            reply_markup=cancel_keyboard(language),
        )
        return
    await _drop_login_client(message.from_user.id)
    await state.clear()
    if not account_manager.add_account(session_string, session_name):
        await smart_answer(
            message,
            translate(language, "texts.auth_failed"),
            reply_markup=cancel_keyboard(language),
        )
        return
    await smart_answer(
        message,
        translate(
            language,
            "texts.account_added",
            name=session_name,
            username=getattr(me, "username", "") or "-",
            phone=getattr(me, "phone", "") or "-",
        ),
        reply_markup=main_menu_keyboard(language),
    )


@router.callback_query(F.data == "accounts")
async def cb_accounts(call: CallbackQuery, language: str) -> None:
    await call.answer()
    if not account_manager.accounts:
        await Screen(
            text=translate(language, "texts.no_available_accounts"),
            markup=no_accounts_keyboard(language),
            delete_origin=True,
        ).show(call)
        return
    lines = [f"{translate(language, 'texts.accounts_list')}\n"]
    now = datetime.now(UTC)
    for account in account_manager.accounts:
        status = (
            translate(language, "texts.account_status_busy")
            if account.in_use
            else translate(language, "texts.account_status_free")
        )
        validity = (
            translate(language, "texts.account_status_valid")
            if account.is_valid
            else translate(language, "texts.account_status_invalid")
        )
        line = f"• <code>{html.escape(account.session_file)}</code>: {status}, {validity}"
        flood_until = account.flood_wait_until
        if flood_until and flood_until > now:
            line += ", " + translate(
                language, "texts.account_flood_wait", until=flood_until.strftime("%H:%M:%S")
            )
        lines.append(line)
    await Screen(
        text=truncate("\n".join(lines)),
        markup=accounts_keyboard(language),
        delete_origin=True,
    ).show(call)


# --- Чаты ---
@router.callback_query(F.data == "add_chats")
async def cb_add_chats(call: CallbackQuery, state: FSMContext, language: str) -> None:
    await state.clear()
    await call.answer()
    await render_prompt(
        call, language, translate(language, "texts.worm_waiting_links"), delete_origin=True
    )
    await state.set_state(InviterStates.waiting_links)


@router.message(InviterStates.waiting_links)
async def on_links(message: Message, state: FSMContext, language: str) -> None:
    links = await parse_links_from_message(message)
    if not links:
        await smart_answer(
            message,
            translate(language, "texts.worm_wait_links_input"),
            reply_markup=cancel_keyboard(language),
        )
        return
    added: list[str] = []
    failed = 0
    rejected = 0
    processed = 0
    flood_events = 0
    flood_seconds = 0.0
    accounts_used: list[str] = []
    notes: list[str] = []
    limit = max(1, Config.MAX_CHATS_PER_IMPORT)
    if not account_manager.accounts:
        await render_prompt(message, language, translate(language, "texts.no_available_accounts"))
        await state.clear()
        return
    for link in links:
        if processed >= limit:
            break
        identifier = parse_identifier(link)
        if not identifier:
            rejected += 1
            failed += 1
            continue
        try:
            async with account_manager.acquire_step(None) as account:
                if account.session_file not in accounts_used:
                    accounts_used.append(account.session_file)
                try:
                    validated = await validate_and_test_chat(account, identifier, language)
                except FloodWaitError as e:
                    seconds = float(getattr(e, "seconds", 60))
                    account_manager.handle_flood_wait(account, seconds)
                    flood_events += 1
                    flood_seconds = max(flood_seconds, seconds)
                    logger.warning(
                        f"FloodWait на {identifier}: {seconds:.0f}с, беру другой аккаунт"
                    )
                    continue
                except AuthKeyUnregisteredError:
                    account.is_valid = False
                    notes.append(translate(language, "texts.invalid_account_continue"))
                    continue
                processed += 1
                if validated is None:
                    failed += 1
                else:
                    added.append(
                        translate(
                            language,
                            "texts.chat_added_result",
                            chat_name=validated.chat_name,
                            user_count=validated.user_count,
                        )
                    )
        except AccountWaitCancelled:
            notes.append(translate(language, "texts.roller_stopped"))
            break
        except NoAvailableAccountError as e:
            notes.append(translate(language, "texts.import_stopped_no_accounts", error=e.message))
            break
    await state.clear()
    text = translate(
        language,
        "texts.chats_added_title",
        count=processed,
    )
    if added:
        text += "\n\n" + "\n".join(added)
    if failed:
        text += f"\n\n{translate(language, 'texts.more_results', count=failed)}"
    skipped = len(links) - processed - rejected
    if skipped > 0:
        text += f"\n\n{translate(language, 'texts.links_skipped', count=skipped)}"
    if flood_events:
        text += "\n\n" + translate(
            language,
            "texts.roller_flood_summary",
            count=flood_events,
            seconds=int(flood_seconds),
        )
    if accounts_used:
        text += "\n\n" + translate(
            language,
            "texts.roller_accounts_used",
            count=len(accounts_used),
            accounts=format_accounts_summary(accounts_used),
        )
    if notes:
        text += "\n\n" + "\n".join(notes)
    await smart_answer(message, text, reply_markup=main_menu_keyboard(language))


@router.callback_query(F.data == "update_chats")
async def cb_update_chats(call: CallbackQuery, language: str) -> None:
    await call.answer()
    total = await chat_db.get_active_chats_count()
    await render_notice(call, language, "texts.update_db_start", total=total)
    if not account_manager.accounts:
        await render_notice(call, language, "texts.no_available_accounts")
        return
    result = await check_and_clean_chats(language)
    text = translate(
        language,
        "texts.update_db_done",
        checked=result.checked,
        added=result.added,
        removed=result.removed,
        errors=result.errors,
    )
    notes: list[str] = []
    if result.flood_events:
        notes.append(
            translate(
                language,
                "texts.roller_flood_summary",
                count=result.flood_events,
                seconds=int(result.flood_seconds),
            )
        )
    if result.accounts_used:
        notes.append(
            translate(
                language,
                "texts.roller_accounts_used",
                count=len(result.accounts_used),
                accounts=format_accounts_summary(result.accounts_used),
            )
        )
    if result.stop_reason:
        notes.append(result.stop_reason)
    if notes:
        text += "\n\n" + "\n".join(notes)
    await safe_send_message(
        call.bot,
        call.from_user.id,
        text,
        reply_markup=main_menu_keyboard(language),
    )


@router.callback_query(F.data == "clear_cache")
async def cb_clear_cache(call: CallbackQuery, language: str) -> None:
    await call.answer()
    removed = await cache_db.clear()
    await entity_cache.clear()
    await full_chat_cache.clear()
    await smart_answer(
        call,
        translate(language, "texts.cache_cleared", count=removed),
        reply_markup=main_menu_keyboard(language),
        delete_origin=True,
    )


# --- Задачи ---
@router.callback_query(F.data == "task:list:active")
async def cb_task_list_active(call: CallbackQuery, language: str) -> None:
    await call.answer()
    await render_task_list(call, language, "active", delete_origin=True)


@router.callback_query(F.data == "task:list:mine")
async def cb_task_list_mine(call: CallbackQuery, language: str) -> None:
    await call.answer()
    await render_task_list(call, language, "mine", delete_origin=True)


@router.callback_query(F.data.startswith("task:view:"))
async def cb_task_view(call: CallbackQuery, language: str) -> None:
    task_id = call.data.split(":", 2)[2]
    task = await tasks_db.get_task(task_id)
    if task is None:
        await call.answer(translate(language, "texts.task_not_found"), show_alert=True)
        return
    await call.answer()
    await smart_answer(
        call,
        format_task_card(task, language),
        reply_markup=build_task_keyboard(task, language),
        delete_origin=True,
    )


@router.callback_query(F.data.startswith("task:pause:"))
async def cb_task_pause(call: CallbackQuery, language: str) -> None:
    task_id = call.data.split(":", 2)[2]
    if not await get_queue_manager().request_pause(task_id):
        await call.answer(translate(language, "texts.task_not_found"), show_alert=True)
        return
    await call.answer()
    await smart_answer(
        call,
        translate(language, "texts.task_paused_action", task_id=task_id),
        reply_markup=main_menu_keyboard(language),
        delete_origin=True,
    )


@router.callback_query(F.data.startswith("task:resume:"))
async def cb_task_resume(call: CallbackQuery, language: str) -> None:
    task_id = call.data.split(":", 2)[2]
    if not await get_queue_manager().request_resume(task_id):
        await call.answer(translate(language, "texts.task_not_found"), show_alert=True)
        return
    await call.answer()
    await smart_answer(
        call,
        translate(language, "texts.task_resumed", task_id=task_id),
        reply_markup=main_menu_keyboard(language),
        delete_origin=True,
    )


@router.callback_query(F.data.startswith("task:cancel:"))
async def cb_task_cancel(call: CallbackQuery, language: str) -> None:
    task_id = call.data.split(":", 2)[2]
    if not await get_queue_manager().request_cancel(task_id, cancelled_by=call.from_user.id):
        await call.answer(translate(language, "texts.task_not_found"), show_alert=True)
        return
    await call.answer()
    await smart_answer(
        call,
        translate(language, "texts.task_cancelled_confirm", task_id=task_id),
        reply_markup=main_menu_keyboard(language),
        delete_origin=True,
    )


@router.callback_query(F.data == "cancel_all")
async def cb_cancel_all(call: CallbackQuery, language: str) -> None:
    await call.answer()
    await smart_answer(
        call,
        translate(language, "texts.task_cancel_confirm"),
        reply_markup=confirm_keyboard(language, "cancel_all:yes"),
        delete_origin=True,
    )


@router.callback_query(F.data == "cancel_all:yes")
async def cb_cancel_all_yes(call: CallbackQuery, language: str) -> None:
    await call.answer()
    manager = get_queue_manager()
    stopped = 0
    for task in await tasks_db.get_user_tasks(call.from_user.id):
        control = manager.get_control(task.task_id)
        if control is None:
            continue
        control.cancel()
        stopped += 1
    count = await tasks_db.cancel_all_active(call.from_user.id)
    await smart_answer(
        call,
        translate(language, "texts.tasks_cancelled", count=count, stopped=stopped),
        reply_markup=main_menu_keyboard(language),
        delete_origin=True,
    )


async def _submit_task(
    bot: Bot,
    user_id: int,
    language: str,
    task_type: str,
    data: dict[str, Any],
) -> Task | None:
    if not account_manager.accounts:
        await safe_send_message(
            bot,
            user_id,
            translate(language, "texts.no_available_accounts"),
            reply_markup=main_menu_keyboard(language),
        )
        return None
    task = Task(
        task_id=new_task_id(),
        type=task_type,
        status="pending",
        user_id=user_id,
        data=data,
        created_at=utc_now_iso(),
    )
    if not await get_queue_manager().submit(task):
        await safe_send_message(
            bot,
            user_id,
            translate(language, "texts.max_tasks", max_tasks=Config.MAX_TASKS_PER_USER),
            reply_markup=main_menu_keyboard(language),
        )
        return None
    return task


# --- Сбор пользователей и инвайты ---
@router.callback_query(F.data == "scrape")
async def cb_scrape(call: CallbackQuery, state: FSMContext, language: str) -> None:
    await state.clear()
    await call.answer()
    await render_prompt(
        call,
        language,
        screen_text(
            language,
            "texts.waiting_source",
            "texts.chat_link_hint",
            step=1,
            total=SCRAPE_WIZARD_STEPS,
        ),
        delete_origin=True,
    )
    await state.set_state(InviterStates.waiting_scrape_source)


@router.message(InviterStates.waiting_scrape_source)
async def on_scrape_source(message: Message, state: FSMContext, language: str) -> None:
    identifier = parse_identifier(message.text or "")
    if not identifier:
        await render_prompt(message, language, translate(language, "texts.invalid_format"))
        return
    await state.update_data(source=identifier)
    await render_prompt(
        message,
        language,
        screen_text(
            language,
            "texts.waiting_target",
            "texts.chat_link_hint",
            step=2,
            total=SCRAPE_WIZARD_STEPS,
        ),
    )
    await state.set_state(InviterStates.waiting_scrape_target)


@router.message(InviterStates.waiting_scrape_target)
async def on_scrape_target(message: Message, state: FSMContext, language: str) -> None:
    identifier = parse_identifier(message.text or "")
    if not identifier:
        await render_prompt(message, language, translate(language, "texts.invalid_format"))
        return
    data = await state.get_data()
    if identifier == data.get("source"):
        await render_prompt(message, language, translate(language, "texts.invalid_format"))
        return
    await state.update_data(target=identifier)
    await render_prompt(
        message,
        language,
        screen_text(
            language,
            "texts.waiting_mode",
            "texts.scrape_mode_select",
            step=3,
            total=SCRAPE_WIZARD_STEPS,
        ),
        markup=scrape_mode_keyboard(language),
    )
    await state.set_state(None)


@router.callback_query(F.data.startswith("scrape:mode:"))
async def cb_scrape_mode(call: CallbackQuery, state: FSMContext, language: str) -> None:
    mode = call.data.split(":")[-1]
    if mode not in ("messages", "users"):
        await call.answer(translate(language, "texts.invalid_format"), show_alert=True)
        return
    await state.update_data(mode=mode)
    await call.answer()
    if mode == "messages":
        text = screen_text(
            language,
            "texts.waiting_message_limit",
            "texts.scrape_limit_prompt",
            step=4,
            total=SCRAPE_WIZARD_STEPS,
            min_limit=Config.SCRAPE_MIN_MESSAGE_LIMIT,
            max_limit=Config.SCRAPE_MAX_MESSAGE_LIMIT,
        )
    else:
        text = screen_text(
            language,
            "texts.waiting_user_count",
            "texts.scrape_user_count_prompt",
            step=4,
            total=SCRAPE_WIZARD_STEPS,
            min_count=Config.SCRAPE_MIN_USER_COUNT,
            max_count=Config.SCRAPE_MAX_USER_COUNT,
        )
    await render_prompt(call, language, text, delete_origin=True)
    await state.set_state(InviterStates.waiting_scrape_limit)


@router.message(InviterStates.waiting_scrape_limit)
async def on_scrape_limit(message: Message, state: FSMContext, language: str) -> None:
    data = await state.get_data()
    mode = str(data.get("mode") or "messages")
    value = to_int(message.text or "", default=-1)
    if mode == "messages":
        if not Config.SCRAPE_MIN_MESSAGE_LIMIT <= value <= Config.SCRAPE_MAX_MESSAGE_LIMIT:
            await render_prompt(
                message,
                language,
                translate(
                    language,
                    "texts.invalid_number",
                    min_val=Config.SCRAPE_MIN_MESSAGE_LIMIT,
                    max_val=Config.SCRAPE_MAX_MESSAGE_LIMIT,
                ),
            )
            return
        await state.update_data(message_limit=value)
    else:
        if not Config.SCRAPE_MIN_USER_COUNT <= value <= Config.SCRAPE_MAX_USER_COUNT:
            await render_prompt(
                message,
                language,
                translate(
                    language,
                    "texts.invalid_number",
                    min_val=Config.SCRAPE_MIN_USER_COUNT,
                    max_val=Config.SCRAPE_MAX_USER_COUNT,
                ),
            )
            return
        await state.update_data(user_limit=value)
    await render_prompt(
        message,
        language,
        screen_text(
            language,
            "texts.scrape_select_account",
            "texts.scrape_account_prompt",
            step=5,
            total=SCRAPE_WIZARD_STEPS,
        ),
        markup=account_keyboard(language, "scrape:account"),
    )
    await state.set_state(None)


@router.callback_query(F.data.startswith("scrape:account:"))
async def cb_scrape_account(call: CallbackQuery, state: FSMContext, language: str) -> None:
    session = parse_account_callback(call.data, "scrape:account:")
    data = await state.get_data()
    task_data = {
        "source": data.get("source", ""),
        "target": data.get("target", ""),
        "mode": data.get("mode", "messages"),
        "message_limit": to_int(data.get("message_limit")),
        "user_limit": to_int(data.get("user_limit")),
        "account": session,
    }
    await state.clear()
    await call.answer()
    task = await _submit_task(
        call.bot,
        call.from_user.id,
        language,
        "scrape_invite",
        task_data,
    )
    if call.message is not None:
        with suppress(TelegramBadRequest, TelegramNetworkError):
            await call.message.delete()
    if task is None:
        return
    await safe_send_message(
        call.bot,
        call.from_user.id,
        translate(
            language,
            "texts.task_launched",
            task_id=task.task_id,
            source=str(task_data["source"]),
            target=str(task_data["target"]),
            mode=translate(language, "buttons.mode_messages")
            if task_data["mode"] == "messages"
            else translate(language, "buttons.mode_users"),
            account=session,
        ),
        reply_markup=build_task_keyboard(task, language),
    )
    await safe_send_message(call.bot, call.from_user.id, format_task_card(task, language))


# --- Режим червя ---
@router.callback_query(F.data == "worm")
async def cb_worm(call: CallbackQuery, state: FSMContext, language: str) -> None:
    if not Features.worm_mode():
        await call.answer(translate(language, "texts.feature_disabled_worm"), show_alert=True)
        return
    await state.clear()
    await call.answer()
    await render_prompt(
        call,
        language,
        screen_text(language, "texts.worm_waiting_chat", "texts.chat_link_hint"),
        delete_origin=True,
    )
    await state.set_state(InviterStates.waiting_worm_chats)


@router.message(InviterStates.waiting_worm_chats, ~F.text.startswith("/"))
async def on_worm_chats(message: Message, state: FSMContext, language: str) -> None:
    links = await parse_links_from_message(message)
    sources: list[str] = []
    for link in links:
        identifier = parse_identifier(link)
        if identifier and identifier not in sources:
            sources.append(identifier)
    if not sources:
        await render_prompt(message, language, translate(language, "texts.invalid_format"))
        return
    if len(sources) > Config.WORM_MAX_SOURCES:
        await render_prompt(
            message,
            language,
            translate(language, "texts.worm_max_sources", count=Config.WORM_MAX_SOURCES),
        )
        sources = sources[: Config.WORM_MAX_SOURCES]
    await state.clear()
    task = await _submit_task(
        message.bot,
        message.from_user.id,
        language,
        "worm",
        {"sources": sources, "chats_label": f"{len(sources)}"},
    )
    if task is None:
        return
    await safe_send_message(
        message.bot,
        message.from_user.id,
        translate(
            language,
            "texts.worm_started_multi",
            task_id=task.task_id,
            chats=len(sources),
            sources="\n".join(f"• {item}" for item in sources),
        ),
        reply_markup=main_menu_keyboard(language),
    )


@router.message(Command("stop_worm"))
async def cmd_stop_worm(event: Message | CallbackQuery, language: str) -> None:
    manager = get_queue_manager()
    cancelled = 0
    messages = links = added = errors = 0
    for task in await tasks_db.get_user_tasks(event.from_user.id):
        if task.type != "worm" or not task.is_active:
            continue
        if not await manager.request_cancel(task.task_id, event.from_user.id):
            continue
        cancelled += 1
        stored = decode_json_object(task.results)
        messages += to_int(stored.get("messages"))
        links += to_int(stored.get("links"))
        added += to_int(stored.get("added"))
        errors += to_int(stored.get("errors"))
    await smart_answer(
        event,
        translate(
            language,
            "texts.worm_stopped_multi",
            messages=messages,
            links=links,
            added=added,
            errors=errors,
        )
        if cancelled
        else translate(language, "texts.no_active_tasks"),
        reply_markup=main_menu_keyboard(language),
        delete_origin=isinstance(event, CallbackQuery),
    )


# --- Массовая рассылка ---
@router.callback_query(F.data == "mailing")
async def cb_mailing(call: CallbackQuery, state: FSMContext, language: str) -> None:
    if not Features.mailing():
        await call.answer(translate(language, "texts.feature_disabled_mailing"), show_alert=True)
        return
    await state.clear()
    await call.answer()
    await render_prompt(
        call,
        language,
        screen_text(
            language,
            "texts.bulkmail_target_select",
            "texts.bulkmail_target_prompt",
            step=1,
            total=MAILING_WIZARD_STEPS,
        ),
        markup=mail_target_keyboard(language),
        delete_origin=True,
    )


@router.callback_query(F.data.startswith("mail:target:"))
async def cb_mail_target(call: CallbackQuery, state: FSMContext, language: str) -> None:
    target = call.data.split(":")[-1]
    if target not in ("chats", "users"):
        await call.answer(translate(language, "texts.invalid_format"), show_alert=True)
        return
    if target == "users" and not Features.mailing_to_users():
        await call.answer(
            translate(language, "texts.feature_disabled_mailing_users"), show_alert=True
        )
        return
    await state.update_data(target=target)
    chat_count = await chat_db.get_active_chats_count()
    if chat_count == 0:
        await call.answer(translate(language, "texts.bulkmail_db_empty"), show_alert=True)
        return
    await call.answer()
    await render_prompt(
        call,
        language,
        screen_text(
            language,
            "texts.bulkmail_chats_db",
            "texts.bulkmail_chats_db_prompt",
            step=2,
            total=MAILING_WIZARD_STEPS,
            count=chat_count,
        ),
        markup=source_keyboard(language, "mail:source"),
        delete_origin=True,
    )


@router.callback_query(F.data.startswith("mail:source:"))
async def cb_mail_source(call: CallbackQuery, state: FSMContext, language: str) -> None:
    mode = call.data.split(":")[-1]
    if mode == "db":
        chats = await chat_db.get_all_chats()
        identifiers = [chat.chat_url or chat.chat_id for chat in chats]
        if not identifiers:
            await call.answer(translate(language, "texts.bulkmail_db_empty"), show_alert=True)
            return
        await state.update_data(chats=identifiers)
        await call.answer()
        await render_prompt(
            call,
            language,
            screen_text(
                language,
                "texts.bulkmail_delay",
                "texts.bulkmail_delay_prompt",
                step=3,
                total=MAILING_WIZARD_STEPS,
            ),
            delete_origin=True,
        )
        await state.set_state(InviterStates.waiting_mail_delay)
        return
    if mode != "manual":
        await call.answer(translate(language, "texts.invalid_format"), show_alert=True)
        return
    await call.answer()
    await render_prompt(
        call,
        language,
        screen_text(
            language,
            "texts.bulkmail_chats_manual",
            "texts.bulkmail_chats_manual_prompt",
            step=2,
            total=MAILING_WIZARD_STEPS,
        ),
        delete_origin=True,
    )
    await state.set_state(InviterStates.waiting_mail_chats)


@router.message(InviterStates.waiting_mail_chats)
async def on_mail_chats(message: Message, state: FSMContext, language: str) -> None:
    identifiers: list[str] = []
    for link in await parse_links_from_message(message):
        identifier = parse_identifier(link)
        if identifier and identifier not in identifiers:
            identifiers.append(identifier)
    if not identifiers:
        await render_prompt(message, language, translate(language, "texts.bulkmail_empty_chats"))
        return
    await state.update_data(chats=identifiers)
    await render_prompt(
        message,
        language,
        screen_text(
            language,
            "texts.bulkmail_delay",
            "texts.bulkmail_delay_prompt",
            step=3,
            total=MAILING_WIZARD_STEPS,
        ),
    )
    await state.set_state(InviterStates.waiting_mail_delay)


@router.message(InviterStates.waiting_mail_delay)
async def on_mail_delay(message: Message, state: FSMContext, language: str) -> None:
    parts = re.split(r"[\s,]+", (message.text or "").strip())
    if len(parts) != 2:
        await render_prompt(message, language, translate(language, "texts.bulkmail_invalid_delay"))
        return
    min_delay, max_delay = to_int(parts[0], default=-1), to_int(parts[1], default=-1)
    if min_delay < 0 or max_delay < 0 or min_delay > max_delay:
        await render_prompt(message, language, translate(language, "texts.bulkmail_invalid_delay"))
        return
    await state.update_data(min_delay=min_delay, max_delay=max_delay)
    await render_prompt(
        message,
        language,
        screen_text(
            language,
            "texts.bulkmail_texts_first",
            "texts.bulkmail_texts_first_prompt",
            step=4,
            total=MAILING_WIZARD_STEPS,
        ),
    )
    await state.set_state(InviterStates.waiting_mail_text)


@router.message(InviterStates.waiting_mail_text)
async def on_mail_text(message: Message, state: FSMContext, language: str) -> None:
    text_value = (message.text or "").strip()
    if not text_value:
        await render_prompt(message, language, translate(language, "texts.empty_input"))
        return
    data = await state.get_data()
    texts: list[str] = list(data.get("texts", []))
    texts.append(text_value)
    await state.update_data(texts=texts)
    await render_prompt(
        message,
        language,
        translate(language, "texts.bulkmail_texts_received", count=len(texts)),
        markup=mail_texts_done_keyboard(language),
    )


@router.callback_query(F.data == "mail:texts_done")
async def cb_mail_texts_done(call: CallbackQuery, state: FSMContext, language: str) -> None:
    data = await state.get_data()
    if not data.get("texts"):
        await call.answer(translate(language, "texts.bulkmail_no_texts"), show_alert=True)
        return
    await call.answer()
    await render_prompt(
        call,
        language,
        screen_text(
            language,
            "texts.bulkmail_account",
            "texts.bulkmail_account_prompt",
            step=5,
            total=MAILING_WIZARD_STEPS,
        ),
        markup=account_keyboard(language, "mail:account"),
        delete_origin=True,
    )
    await state.set_state(None)


@router.callback_query(F.data.startswith("mail:account:"))
async def cb_mail_account(call: CallbackQuery, state: FSMContext, language: str) -> None:
    session = parse_account_callback(call.data, "mail:account:")
    await state.update_data(account=session)
    await call.answer()
    await render_prompt(
        call,
        language,
        screen_text(
            language,
            "texts.bulkmail_total",
            "texts.bulkmail_total_prompt",
            step=6,
            total=MAILING_WIZARD_STEPS,
            max_total=Config.MAILING_MAX_TOTAL_SENDS,
        ),
        delete_origin=True,
    )
    await state.set_state(InviterStates.waiting_mail_total)


@router.message(InviterStates.waiting_mail_total)
async def on_mail_total(message: Message, state: FSMContext, language: str) -> None:
    total = to_int(message.text or "")
    if total <= 0:
        await smart_answer(
            message,
            translate(language, "texts.bulkmail_total_error"),
            reply_markup=cancel_keyboard(language),
        )
        return
    data = await state.get_data()
    chats: list[str] = list(data.get("chats", []))
    task_data = {
        "chats": chats,
        "chat_count": len(chats),
        "chats_label": f"{len(chats)}",
        "texts": list(data.get("texts", [])),
        "min_delay": to_int(data.get("min_delay"), default=Config.MAILING_MIN_DELAY),
        "max_delay": to_int(data.get("max_delay"), default=Config.MAILING_MAX_DELAY),
        "account": str(data.get("account") or "auto"),
        "target": str(data.get("target") or "chats"),
        "total": total,
    }
    await state.clear()
    task = await _submit_task(
        message.bot,
        message.from_user.id,
        language,
        "bulkmail",
        task_data,
    )
    if task is None:
        return
    await safe_send_message(
        message.bot,
        message.from_user.id,
        translate(language, "texts.bulkmail_sent", task_id=task.task_id, sent=total),
        reply_markup=main_menu_keyboard(language),
    )


@router.message(F.text)
@log_error
async def on_unhandled_text(message: Message, language: str) -> None:
    await render_main_menu(message, language)


async def cache_cleanup_loop() -> None:
    interval = max(60, Config.ENTITY_CACHE_CLEANUP_INTERVAL)
    while True:
        await asyncio.sleep(interval)
        try:
            entities = await entity_cache.prune_expired()
            chats = await full_chat_cache.prune_expired()
            if entities or chats:
                logger.info(f"Кэш очищен: сущностей {entities}, полных чатов {chats}")
        except asyncio.CancelledError:
            break
        except Exception as e:  # noqa: BLE001
            logger.error(f"Ошибка очистки кэша: {e}")


# --- Запуск ---
async def release_webhook() -> None:
    try:
        webhook_info = await bot.get_webhook_info()
    except TelegramAPIError as e:
        logger.warning(f"Не удалось проверить webhook: {type(e).__name__}: {e}")
        return
    url = str(getattr(webhook_info, "url", "") or "").strip()
    if not url:
        return
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.warning(f"Удалён активный webhook '{url}', включён long polling")
    except TelegramAPIError as e:
        logger.error(f"Не удалось удалить webhook '{url}': {type(e).__name__}: {e}")
        raise BotError(err("webhook_release_failed", url=url, error=e)) from e


async def on_startup() -> None:
    await release_webhook()

    for error in validate_languages():
        logger.error(f"Языки: {error}")

    await tasks_db.connect()
    await chat_db.connect()
    await cache_db.connect()
    await user_db.connect()
    global queue_manager
    queue_manager = TaskQueueManager(bot=bot)
    await queue_manager.start()
    await account_manager.start_health_check()
    BACKGROUND_TASKS.add(asyncio.create_task(cache_cleanup_loop(), name="cache-cleanup"))

    me = await bot.get_me()
    logger.info(f"Бот запущен: @{me.username}")
    await notify_owners(
        bot,
        "texts.admin_startup",
        accounts=len(account_manager.accounts),
        chats=await chat_db.get_active_chats_count(),
        users=await chat_db.get_total_users(),
        max_tasks=Config.MAX_CONCURRENT_TASKS,
        max_per_user=Config.MAX_TASKS_PER_USER,
        min_delay=Config.MIN_INVITE_DELAY,
        max_delay=Config.MAX_INVITE_DELAY,
        flood_multiplier=Config.FLOOD_WAIT_MULTIPLIER,
        retries=Config.MAX_RETRIES,
    )


async def on_shutdown() -> None:
    logger.info("🛑 Запуск graceful shutdown...")
    logger.info("Остановка фоновых задач...")
    for task in list(BACKGROUND_TASKS):
        if not task.done():
            task.cancel()
    if BACKGROUND_TASKS:
        try:
            await asyncio.gather(*BACKGROUND_TASKS, return_exceptions=True)
        except asyncio.CancelledError:
            pass
    BACKGROUND_TASKS.clear()

    if queue_manager is not None:
        await queue_manager.stop()
    await account_manager.stop_health_check()
    await account_manager.release_all()

    await notify_owners(bot, "texts.admin_shutdown")
    await tasks_db.close()
    await chat_db.close()
    await cache_db.close()
    await user_db.close()

    if bot.session:
        await bot.session.close()
    logger.info("✅ Бот остановлен")


async def main() -> None:
    background_tasks: list[asyncio.Task[Any]] = []
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    async def stop_polling_safely() -> None:
        try:
            await dp.stop_polling()
        except RuntimeError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Ошибка остановки polling: {type(e).__name__}: {e}")

    def signal_handler(sig: int, frame: Any) -> None:
        logger.info(f"Получен сигнал {sig}. Запуск graceful shutdown...")
        shutdown_event.set()
        for task in background_tasks:
            if not task.done():
                task.cancel()
        background_tasks.append(loop.create_task(stop_polling_safely()))

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, signal_handler)
        except (AttributeError, ValueError):
            pass

    load_languages()
    if not LANGUAGES:
        logger.critical(f"Не найдены языковые файлы в {LANGS_PATH}")
        sys.exit(1)

    try:
        Config.validate()
        logger.info("Конфигурация валидна")
    except ConfigError as e:
        logger.critical(f"Ошибка конфигурации:\n{e}")
        sys.exit(1)

    feature_errors = Features.validate()
    if feature_errors:
        error_msg = "Ошибка конфигурации функций:\n" + "\n".join(f"  • {e}" for e in feature_errors)
        logger.critical(error_msg)
        for owner_id in OWNER_USER_ID_SET:
            try:
                await safe_send_message(bot, owner_id, html.escape(error_msg))
            except Exception:  # noqa: BLE001, S110
                pass
        if bot.session:
            await bot.session.close()
        sys.exit(1)
    logger.info("Функции валидированы")

    dp.update.outer_middleware(ErrorLogMiddleware())
    dp.update.middleware(OwnerMiddleware())

    try:
        await on_startup()
        logger.info("Запуск polling...")
        background_tasks.append(asyncio.create_task(shutdown_event.wait()))
        await dp.start_polling(bot)
        logger.info("Polling завершен")
    except ConfigError as e:
        logger.critical(f"Ошибка конфигурации: {e}")
        sys.exit(1)
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Остановка бота")
    except TelegramConflictError:
        logger.critical(
            "Конфликт опроса: бот уже запущен в другом процессе или на нём висит webhook.\n"
            "  • Проверь, что не запущен второй экземпляр main.py\n"
            "  • Проверь webhook: https://api.telegram.org/bot<TOKEN>/getWebhookInfo\n"
            "  • Удали webhook: https://api.telegram.org/bot<TOKEN>/deleteWebhook"
        )
        sys.exit(1)
    except Exception as e:
        logger.critical(f"Неожиданная ошибка: {type(e).__name__}: {e}", exc_info=True)
        sys.exit(1)
    finally:
        for task in background_tasks:
            if not task.done():
                task.cancel()
        if background_tasks:
            try:
                await asyncio.gather(*background_tasks, return_exceptions=True)
            except asyncio.CancelledError:
                pass
        await on_shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Принудительная остановка")
    except Exception as e:
        logger.critical(f"Фатальная ошибка: {e}", exc_info=True)
        sys.exit(1)
