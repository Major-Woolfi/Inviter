from __future__ import annotations

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
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv
from telethon import TelegramClient, functions
from telethon import types as telethon_types
from telethon.errors import (
    AuthKeyUnregisteredError,
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
    RPCError,
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
load_dotenv(BASE_DIR / ".env")

# --- Настройка логирования ---
LOGS_DIR: Path = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
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
    try:
        file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8", mode="a")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except (PermissionError, OSError) as e:
        logger.warning(f"Не удалось инициализировать файловый логгер: {e}")
logger.info("=== Логгер инициализирован ===")


# --- Кастомные исключения ---
class BotError(Exception):
    def __init__(self, message: str, code: int = 500) -> None:
        self.message = message
        self.code = code
        super().__init__(self.message)


class ConfigError(BotError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code=400)


class DatabaseError(BotError):
    def __init__(self, message: str, original_error: Exception | None = None) -> None:
        self.original_error = original_error
        super().__init__(message, code=500)


class ChatUnreachableError(BotError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code=404)


# --- Наборы исключений для обработки ---
DB_ERRORS: tuple[type[BaseException], ...] = (
    aiosqlite.Error,
    ValueError,
    TypeError,
    RuntimeError,
)

TELEGRAM_ERRORS: tuple[type[BaseException], ...] = (
    RPCError,
    TelegramNetworkError,
    OSError,
    ValueError,
    TypeError,
    RuntimeError,
)

RUNTIME_ERRORS: tuple[type[BaseException], ...] = (
    BotError,
    TelegramBadRequest,
    TelegramNetworkError,
    OSError,
    ValueError,
    TypeError,
    RuntimeError,
)


# --- Утилиты маскирования секретов ---
_SENSITIVE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), "<bot_token>"),
    (re.compile(r"\b[1-9][A-Za-z0-9_-]{45,}\b"), "<session_string>"),
    (
        re.compile(
            r"(?i)\b(api_hash|api_id|bot_token|session|password|phone_code)\b(\s*[=:]\s*)\S+"
        ),
        r"\1\2<masked>",
    ),
)


def mask_sensitive(text: str) -> str:
    result = str(text)
    for pattern, replacement in _SENSITIVE_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


class _MaskingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mask_sensitive(str(record.msg))
        record.args = ()
        return True


logger.addFilter(_MaskingFilter())


# --- Утилиты для работы с событиями ---
async def safe_send_message(
    bot: Bot,
    user_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    if user_id <= 0:
        return False
    try:
        await bot.send_message(user_id, text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        return True
    except TelegramBadRequest as e:
        error_msg = str(e).lower()
        if "blocked" in error_msg or "deactivated" in error_msg:
            return False
        try:
            await bot.send_message(
                user_id,
                html.escape(text),
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
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
            await event.answer(text, reply_markup=reply_markup)
            return True
        if event.message is None:
            return False
        await event.message.answer(text, reply_markup=reply_markup)
        if delete_origin:
            try:
                await event.message.delete()
            except (TelegramBadRequest, TelegramNetworkError):
                logger.debug("Не удалось удалить исходное сообщение")
        return True
    except (TelegramBadRequest, TelegramNetworkError) as e:
        logger.error(f"smart_answer: {e}")
        return False


async def notify_user(
    bot: Bot,
    user_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    return await safe_send_message(bot, user_id, text, reply_markup=reply_markup)


async def notify_owners(
    bot: Bot,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    for owner_id in Config.OWNER_USER_IDS:
        await safe_send_message(bot, owner_id, text, reply_markup=reply_markup)


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
                logger.exception(f"Все {max_retries} попытки исчерпаны: {type(e).__name__}: {e}")  # noqa: TRY401
    if last_exception is not None:
        raise last_exception
    raise BotError("retry_async: неизвестная ошибка")


# --- Валидация данных ---
def str_to_bool(value: str | None, default: bool = False) -> bool:
    raw = str(value if value is not None else default).strip().lower()
    return raw in ("1", "true", "yes", "y", "on")


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"{name}='{raw}' не число, используется {default}")
        return default


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(f"{name}='{raw}' не число, используется {default}")
        return default


def env_int_list(name: str) -> list[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    result: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        item = chunk.strip()
        if not item:
            continue
        try:
            result.append(int(item))
        except ValueError:
            logger.warning(f"{name}: '{item}' пропущено, ожидалось целое число")
    return result


def resolve_local_path(raw: str | None, default_name: str) -> str:
    name = (raw or "").strip() or default_name
    candidate = Path(name)
    if candidate.is_absolute():
        return str(candidate)
    return str((BASE_DIR / candidate).resolve())


def to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def to_float(value: Any, default: float = 0.0) -> float:
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
        return f"https://t.me/{raw}" if len(raw) > 1 else None
    if raw.startswith("joinchat/"):
        return None
    if re.fullmatch(r"c/\d+", raw):
        return raw
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


def format_progress_bar(percentage: float, length: int = 12) -> str:
    percent = max(0.0, min(100.0, percentage))
    filled = int(length * percent / 100)
    return f"{'🟩' * filled}{'⬜' * (length - filled)} {percent:.1f}%"


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
    POST_BUFFER_DELAY_MIN: int = env_int("POST_BUFFER_DELAY_MIN", 30)
    POST_BUFFER_DELAY_MAX: int = env_int("POST_BUFFER_DELAY_MAX", 60)
    MAX_INVITES_PER_ACCOUNT: int = env_int("MAX_INVITES_PER_ACCOUNT", 500)

    ADAPTIVE_DELAY_BASE: float = env_float("ADAPTIVE_DELAY_BASE", 5.0)
    ADAPTIVE_DELAY_MAX: float = env_float("ADAPTIVE_DELAY_MAX", 120.0)
    MAX_ACCOUNT_CONSECUTIVE_ERRORS: int = env_int("MAX_ACCOUNT_CONSECUTIVE_ERRORS", 5)

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
    VALIDATOR_ANTI_FLOOD_DELAY_MIN: int = env_int("VALIDATOR_ANTI_FLOOD_DELAY_MIN", 3)
    VALIDATOR_ANTI_FLOOD_DELAY_MAX: int = env_int("VALIDATOR_ANTI_FLOOD_DELAY_MAX", 7)
    CHAT_ADD_THROTTLE_DELAY_MIN: float = env_float("CHAT_ADD_THROTTLE_DELAY_MIN", 1.0)
    CHAT_ADD_THROTTLE_DELAY_MAX: float = env_float("CHAT_ADD_THROTTLE_DELAY_MAX", 2.0)

    SCRAPE_MIN_MESSAGE_LIMIT: int = env_int("SCRAPE_MIN_MESSAGE_LIMIT", 50)
    SCRAPE_MAX_MESSAGE_LIMIT: int = env_int("SCRAPE_MAX_MESSAGE_LIMIT", 5000)
    SCRAPE_MIN_USER_COUNT: int = env_int("SCRAPE_MIN_USER_COUNT", 10)
    SCRAPE_MAX_USER_COUNT: int = env_int("SCRAPE_MAX_USER_COUNT", 1000)

    AUTO_LEAVE_AFTER_INVITE: bool = str_to_bool(os.getenv("AUTO_LEAVE_AFTER_INVITE"), True)

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
    def owner_ids(cls) -> set[int]:
        return set(cls.OWNER_USER_IDS)

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
            errors.append("OWNER_USER_IDS пуст - бот будет недоступен")
        if cls.MIN_INVITE_DELAY > cls.MAX_INVITE_DELAY:
            errors.append("MIN_INVITE_DELAY больше MAX_INVITE_DELAY")
        if cls.MIN_WORKERS > cls.MAX_WORKERS:
            errors.append("MIN_WORKERS больше MAX_WORKERS")
        if cls.HUMAN_BREAK_MIN > cls.HUMAN_BREAK_MAX:
            errors.append("HUMAN_BREAK_MIN больше HUMAN_BREAK_MAX")
        if errors:
            message = "Ошибка конфигурации:\n" + "\n".join(f"  - {e}" for e in errors)
            raise ConfigError(message)


class Features:
    AUTO_LEAVE: bool = Config.AUTO_LEAVE_AFTER_INVITE
    HUMAN_SIMULATION: bool = Config.SIMULATE_SKIP_RATE > 0 or Config.HUMAN_BREAK_CHANCE > 0
    WORM_MODE: bool = str_to_bool(os.getenv("FEATURE_WORM_MODE"), True)
    MAILING: bool = str_to_bool(os.getenv("FEATURE_MAILING"), True)
    MAILING_TO_USERS: bool = str_to_bool(os.getenv("FEATURE_MAILING_TO_USERS"), True)

    @classmethod
    def validate(cls) -> list[str]:
        errors: list[str] = []
        if not cls.MAILING:
            errors.append("FEATURE_MAILING=false - массовая рассылка недоступна")
        if not cls.WORM_MODE:
            errors.append("FEATURE_WORM_MODE=false - режим червя недоступен")
        return errors


def rand_range(min_value: float, max_value: float) -> float:
    low = float(min_value)
    high = float(max_value)
    if high < low:
        low, high = high, low
    return random.uniform(low, high)


def backoff_delay() -> float:
    return rand_range(Config.ERROR_BACKOFF_MIN, Config.ERROR_BACKOFF_MAX)


def is_owner_user(user_id: int) -> bool:
    return to_int(user_id) in set(Config.OWNER_USER_IDS)


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


# --- Утилиты логирования ---
def log_error(func: Callable[..., Any]) -> Callable[..., Any]:
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            logger.exception(f"Ошибка в {func.__name__}: {e}")  # noqa: TRY401
            raise

    return wrapper


# --- Модели данных ---
class Task:
    ACTIVE_STATUSES: ClassVar[frozenset[str]] = frozenset({"pending", "running", "paused"})
    FINAL_STATUSES: ClassVar[frozenset[str]] = frozenset({"completed", "cancelled", "failed"})

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
    ) -> None:
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
    def from_row(cls, row: Any) -> Task:
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


@dataclass
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
    checked: int = 0
    removed: int = 0
    updated: int = 0
    errors: int = 0


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
    def __init__(self, db_path: str) -> None:
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
            raise DatabaseError(f"Ошибка подключения к БД {self.db_path}: {e}", e) from e
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
        except DB_ERRORS as e:
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
                for index, statement in enumerate(
                    (
                        "CREATE INDEX IF NOT EXISTS idx_tasks_user_id ON tasks(user_id)",
                        "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
                        "CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at)",
                    ),
                    start=1,
                ):
                    await self.conn.execute(statement)
                await self.conn.commit()
            except Exception as e:
                logger.error(f"Ошибка инициализации БД задач: {e}")
                raise DatabaseError(f"Ошибка init_db задач: {e}", e) from e

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
            except DB_ERRORS as e:
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
            except DB_ERRORS as e:
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
            except DB_ERRORS as e:
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
            except DB_ERRORS as e:
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

    async def cancel_all_active(self) -> int:
        if self.conn is None:
            return 0
        now = utc_now_iso()
        async with self.lock:
            try:
                cursor = await self.conn.execute(
                    "UPDATE tasks SET status = 'cancelled', completed_at = ?, cancelled_at = ? "
                    "WHERE status IN ('pending', 'running', 'paused')",
                    (now, now),
                )
                await self.conn.commit()
                return max(0, cursor.rowcount)
            except DB_ERRORS as e:
                logger.error(f"cancel_all_active: {e}")
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
            except DB_ERRORS as e:
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
            except DB_ERRORS as e:
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
                raise DatabaseError(f"Ошибка init_db чатов: {e}", e) from e

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
            except DB_ERRORS as e:
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
            except DB_ERRORS as e:
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
            except DB_ERRORS as e:
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
            except DB_ERRORS as e:
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
            except DB_ERRORS as e:
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
            except DB_ERRORS as e:
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
    def __init__(self, db_path: str) -> None:
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
                raise DatabaseError(f"Ошибка init_db кэша: {e}", e) from e

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
            except DB_ERRORS as e:
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
            except DB_ERRORS as e:
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
            except DB_ERRORS as e:
                logger.error(f"clear_participants: {e}")
                return 0

    async def mark_invited(self, chat_id: str, user_id: int, task_id: str | None = None) -> None:
        key = f"{chat_id}:{user_id}"
        if self.conn is None:
            self._invited[key] = (True, datetime.now(UTC).timestamp() + Config.INVITED_CACHE_TTL)
            return
        async with self.lock:
            try:
                await self.conn.execute(
                    "INSERT OR IGNORE INTO invited_users (chat_id, user_id, task_id, invited_at) "
                    "VALUES (?, ?, ?, ?)",
                    (str(chat_id), to_int(user_id), task_id, utc_now_iso()),
                )
                await self.conn.commit()
            except DB_ERRORS as e:
                logger.error(f"mark_invited {key}: {e}")
                return
        self._invited[key] = (True, datetime.now(UTC).timestamp() + Config.INVITED_CACHE_TTL)

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
            except DB_ERRORS as e:
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
            except DB_ERRORS as e:
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
                raise DatabaseError(f"Ошибка init_db пользователей: {e}", e) from e

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
            except DB_ERRORS as e:
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
            except DB_ERRORS as e:
                logger.error(f"get_language {user_id}: {e}")
                return DEFAULT_LANGUAGE
        code = str(row["language"] or "").strip().lower() if row else ""
        return code if code in LANGUAGES else DEFAULT_LANGUAGE

    async def set_language(self, user_id: int, language: str) -> bool:
        if await self.ensure_user(user_id) is False:
            return False
        if self.conn is None:
            return False
        async with self.lock:
            try:
                await self.conn.execute(
                    "UPDATE users SET language = ? WHERE user_id = ?",
                    (str(language).strip().lower(), to_int(user_id)),
                )
                await self.conn.commit()
            except DB_ERRORS as e:
                logger.error(f"set_language {user_id}: {e}")
                return False
        return True


# --- Telegram: клиент, кэш сущностей, вход и выход ---
DEVICE_MODEL: str = "Inviter"
SYSTEM_VERSION: str = "Linux"
APP_VERSION: str = "4.16.8"


class NoAvailableAccountError(BotError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code=409)


def create_telegram_client(session_string: str | None = None) -> TelegramClient:
    session = StringSession(session_string) if session_string else StringSession()
    return TelegramClient(
        session,
        Config.API_ID,
        Config.API_HASH,
        device_model=DEVICE_MODEL,
        system_version=SYSTEM_VERSION,
        app_version=APP_VERSION,
        system_lang_code="en",
        lang_code="en",
        catch_up=False,
        connection_retries=3,
        retry_delay=2,
    )


class EntityCache:
    def __init__(self, ttl: int) -> None:
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
        except TELEGRAM_ERRORS:
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
    def __init__(self, ttl: int) -> None:
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
        raise NoAvailableAccountError(f"Аккаунт {account.session_file} не подключён")
    return client


async def get_cached_entity(account: Account, identifier: int | str) -> Any | None:
    client = account.client
    if client is None:
        return None
    for attempt in range(1, 4):
        try:
            return await entity_cache.get(account.session_file, client, identifier)
        except FloodWaitError as e:
            wait_time = float(getattr(e, "seconds", 60))
            logger.warning(
                f"FloodWait при получении сущности {identifier}: {wait_time:.0f}с "
                f"(попытка {attempt}/3)"
            )
            await asyncio.sleep(wait_time)
        except AuthKeyUnregisteredError:
            account.is_valid = False
            raise
        except TELEGRAM_ERRORS as e:
            logger.warning(f"Не удалось получить сущность {identifier}: {type(e).__name__}: {e}")
            return None
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
    except TELEGRAM_ERRORS as e:
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


async def join_chat(account: Account, identifier: str) -> tuple[Any, bool]:
    client = account.client
    if client is None:
        raise NoAvailableAccountError("Аккаунт не подключён")
    if identifier.startswith("https://t.me/+", "+"):
        invite_hash = identifier.split("+", 1)[1]
        result = await client(functions.messages.ImportChatInviteRequest(invite_hash))
        chats = getattr(result, "chats", None)
        entity = chats[0] if chats else await get_cached_entity(account, identifier)
        if entity is None:
            raise ChatUnreachableError(f"Не удалось получить чат по инвайт-ссылке {identifier}")
        return entity, True
    entity = await get_cached_entity(account, identifier)
    if entity is None:
        raise ChatUnreachableError(f"Сущность не найдена: {identifier}")
    try:
        await client(functions.channels.JoinChannelRequest(entity))
    except UserAlreadyParticipantError:
        return entity, False
    return entity, True


async def leave_chat(account: Account, entity: Any, label: str) -> bool:
    client = account.client
    if client is None:
        return False
    try:
        if isinstance(entity, telethon_types.Channel):
            await client(functions.channels.LeaveChannelRequest(entity))
        else:
            await client(functions.messages.DeleteChatUserRequest(chat_id=entity.id, user_id="me"))
    except AuthKeyUnregisteredError:
        account.is_valid = False
        raise
    except UserNotParticipantError:
        logger.debug(f"Аккаунт {account.session_file} и так не состоит в {label}")
        return False
    except TELEGRAM_ERRORS as e:
        logger.warning(f"Не удалось выйти из {label}: {type(e).__name__}: {e}")
        return False
    logger.info(f"Аккаунт {account.session_file} вышел из {label}")
    return True


async def validate_and_test_chat(
    account: Account,
    identifier: str,
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
    except TELEGRAM_ERRORS as e:
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
    username = getattr(entity, "username", None)
    chat_url = f"https://t.me/{username}" if username else f"https://t.me/{identifier}"

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
        except TELEGRAM_ERRORS as e:
            logger.warning(f"Не удалось получить participant_count для {identifier}: {e}")

    can_write = False
    try:
        sent = await require_client(account).send_message(
            entity, translate(DEFAULT_LANGUAGE, "texts.validator_probe")
        )
        can_write = True
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
    except SlowModeWaitError as e:
        logger.info(f"Slow mode в {chat_name}: {getattr(e, 'seconds', 5)}с")
        can_write = False
    except TELEGRAM_ERRORS as e:
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
        except TELEGRAM_ERRORS as e:
            logger.warning(f"Не удалось удалить тестовое сообщение в {chat_name}: {e}")

    if joined_by_bot:
        await leave_chat(account, entity, chat_url)

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


async def check_and_clean_chats(account: Account) -> CheckAndCleanResult:
    result = CheckAndCleanResult()
    chats = await chat_db.get_all_chats(verified_only=False)
    result.checked = len(chats)
    for chat in chats:
        identifier = chat.chat_url or chat.chat_id
        try:
            entity = await get_cached_entity(account, identifier)
        except AuthKeyUnregisteredError:
            account.is_valid = False
            raise
        except FloodWaitError:
            result.errors += 1
            continue
        except TELEGRAM_ERRORS:
            if await chat_db.delete_chat(chat.chat_id):
                result.removed += 1
                logger.info(f"Чат {chat.chat_name} удалён: недоступен")
            continue

        if entity is None or not isinstance(entity, (telethon_types.Channel, telethon_types.Chat)):
            if await chat_db.delete_chat(chat.chat_id):
                result.removed += 1
                logger.info(f"Чат {chat.chat_name} удалён: больше не группа")
            continue

        reachable = True
        try:
            probe = await require_client(account).send_message(
                entity, translate(DEFAULT_LANGUAGE, "texts.validator_probe")
            )
            with suppress(Exception):
                await require_client(account).delete_messages(entity, probe.id)
        except (ChatAdminRequiredError, ChannelPrivateError, ChatWriteForbiddenError):
            reachable = False
        except FloodWaitError:
            result.errors += 1
            continue
        except AuthKeyUnregisteredError:
            account.is_valid = False
            raise
        except TELEGRAM_ERRORS as e:
            logger.debug(f"Проба записи в {chat.chat_name} не удалась: {type(e).__name__}: {e}")
            reachable = False

        if not reachable:
            if await chat_db.delete_chat(chat.chat_id):
                result.removed += 1
                logger.info(f"Чат {chat.chat_name} удалён: нет доступа на запись")
            continue

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
            result.errors += 1
        except AuthKeyUnregisteredError:
            account.is_valid = False
            raise
        except TELEGRAM_ERRORS as e:
            result.errors += 1
            logger.error(f"Ошибка обновления чата {chat.chat_name}: {e}")
    return result


# --- Пул аккаунтов ---
class AccountPoolManager:
    def __init__(self) -> None:
        self.accounts: list[Account] = []
        self.lock: asyncio.Lock = asyncio.Lock()
        self._success_rate: dict[str, float] = {}
        self._health_task: asyncio.Task[None] | None = None
        self.load_accounts()

    def load_accounts(self) -> None:
        self.accounts.clear()
        self._success_rate.clear()
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

    def _update_success_rate(self, account: Account, success: bool) -> None:
        current = self._success_rate.setdefault(account.session_file, 1.0)
        self._success_rate[account.session_file] = 0.1 * (1.0 if success else 0.0) + (0.9 * current)
        if not success:
            account.consecutive_errors += 1
        else:
            account.consecutive_errors = 0

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
        extended = float(wait_seconds) * Config.FLOOD_WAIT_MULTIPLIER
        account.flood_wait_until = datetime.now(UTC) + timedelta(seconds=extended)
        self._update_success_rate(account, False)
        logger.warning(
            f"FloodWait для {account.session_file}: пауза {extended:.0f}с "
            f"(запрошено {wait_seconds:.0f}с)"
        )
        return extended

    def should_simulate_skip(self) -> bool:
        return Features.HUMAN_SIMULATION and rand_range(0, 1) < Config.SIMULATE_SKIP_RATE

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
            except TELEGRAM_ERRORS as e:
                logger.debug(f"disconnect {account.session_file}: {e}")
            account.client = None
        try:
            account.client = await self._connect(account)
        except Exception as e:
            logger.error(f"Не удалось подключить {account.session_file}: {e}")
            account.is_valid = False
            raise NoAvailableAccountError(f"Аккаунт {account.session_file} недоступен: {e}") from e
        self._update_success_rate(account, True)
        return account.client

    @asynccontextmanager
    async def acquire(self, session_file: str | None = None) -> AsyncIterator[Account]:
        account = await self._acquire_locked(session_file)
        try:
            yield account
        finally:
            async with self.lock:
                account.in_use = False

    async def _acquire_locked(self, session_file: str | None) -> Account:
        async with self.lock:
            if session_file:
                account = self.get_account(session_file)
                if account is None:
                    raise NoAvailableAccountError(f"Сессия {session_file} не найдена")
                if not account.is_valid:
                    raise NoAvailableAccountError(f"Сессия {session_file} недействительна")
                if account.in_use:
                    raise NoAvailableAccountError(f"Сессия {session_file} сейчас занята")
                if self._flood_active(account):
                    raise NoAvailableAccountError(
                        f"Сессия {session_file} в режиме ожидания FloodWait"
                    )
            else:
                candidates = [account for account in self.accounts if self.is_available(account)]
                if not candidates:
                    raise NoAvailableAccountError("Нет свободных аккаунтов")
                candidates.sort(
                    key=lambda item: (
                        -self._success_rate.get(item.session_file, 1.0),
                        item.last_used or datetime.min.replace(tzinfo=UTC),
                    )
                )
                account = candidates[0]
            account.in_use = True
            account.last_used = datetime.now(UTC)
            if account.invite_count >= Config.MAX_INVITES_PER_ACCOUNT:
                logger.info(f"Сброс счётчика инвайтов для {account.session_file}")
                account.invite_count = 0
        try:
            await self.ensure_client(account)
        except Exception:
            async with self.lock:
                account.in_use = False
            raise
        logger.info(f"Аккаунт {account.session_file} взят в работу")
        return account

    async def health_check_once(self) -> None:
        for account in list(self.accounts):
            if account.in_use:
                continue
            try:
                await self.ensure_client(account)
                logger.info(f"Проверка аккаунта {account.session_file}: ОК")
            except RUNTIME_ERRORS as e:
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
            except RUNTIME_ERRORS as e:
                logger.error(f"Ошибка health check: {e}")
                await asyncio.sleep(backoff_delay())

    async def release_all(self) -> None:
        for account in self.accounts:
            if account.client is None:
                continue
            try:
                if account.client.is_connected():
                    await account.client.disconnect()
            except RUNTIME_ERRORS as e:
                logger.error(f"Ошибка отключения {account.session_file}: {e}")
            account.client = None
            account.in_use = False


# --- Глобальные компоненты ---
tasks_db: TasksDB = TasksDB(Config.TASKS_DB_PATH)
chat_db: ChatDB = ChatDB(Config.CHATS_DB_PATH)
cache_db: CacheManager = CacheManager(Config.CACHE_DB_PATH)
user_db: UserDB = UserDB(Config.USERS_DB_PATH)
account_manager: AccountPoolManager = AccountPoolManager()

TASK_TYPES: dict[str, str] = {
    "scrape_invite": "task_types.scrape_invite",
    "bulkmail": "task_types.bulkmail",
    "worm": "task_types.worm",
}


def new_task_id() -> str:
    return uuid.uuid4().hex[:12]


def format_task_status(status: str, language: str) -> str:
    return translate(language, f"statuses.{status}")


def format_task_type(task_type: str, language: str) -> str:
    key = TASK_TYPES.get(task_type, "")
    return translate(language, key) if key else task_type


def format_chat_label(chat_url: str, chat_name: str = "") -> str:
    if chat_name and chat_url:
        return f"{chat_name} ({chat_url})"
    return chat_url or chat_name or "?"


def format_task_card(task: Task, language: str = DEFAULT_LANGUAGE) -> str:
    data = task.data
    source = str(data.get("source") or data.get("chats_label") or "-")
    target = str(data.get("target") or "-")
    lines = [
        translate(language, "texts.task_id_label") + f" <code>{html.escape(task.task_id)}</code>",
        translate(language, "texts.task_type_label") + f" {format_task_type(task.type, language)}",
        translate(language, "texts.task_status_label")
        + f" {format_task_status(task.status, language)}",
        translate(language, "texts.task_created_label") + f" {html.escape(task.created_at or '-')}",
        translate(language, "texts.task_source_label") + f" {html.escape(source)}",
    ]
    if target != "-":
        lines.append(translate(language, "texts.task_target_label") + f" {html.escape(target)}")
    if to_int(data.get("message_limit")):
        lines.append(
            translate(language, "texts.task_messages_label")
            + f" {to_int(data.get('message_limit'))}"
        )
    if to_int(data.get("user_limit")):
        lines.append(
            translate(language, "texts.task_users_label") + f" {to_int(data.get('user_limit'))}"
        )
    if to_int(data.get("chat_count")):
        lines.append(
            translate(language, "texts.task_chats_label") + f" {to_int(data.get('chat_count'))}"
        )
    lines.append(translate(language, "texts.task_sent_label") + f" {task.sent}")
    lines.append(
        translate(language, "texts.task_progress_label") + f" {format_progress_bar(task.progress)}"
    )
    if task.progress_text:
        lines.append(
            translate(language, "texts.task_current_status_label")
            + f" {html.escape(task.progress_text)}"
        )
    return "\n".join(lines)


def build_task_keyboard(task: Task, language: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if task.status == "running":
        rows.append(
            [
                InlineKeyboardButton(
                    text=translate(language, "texts.task_pause"),
                    callback_data=f"task:pause:{task.task_id}",
                ),
                InlineKeyboardButton(
                    text=translate(language, "texts.task_cancel_btn"),
                    callback_data=f"task:cancel:{task.task_id}",
                ),
            ]
        )
    elif task.status in ("pending", "paused"):
        rows.append(
            [
                InlineKeyboardButton(
                    text=translate(language, "texts.task_resume"),
                    callback_data=f"task:resume:{task.task_id}",
                ),
                InlineKeyboardButton(
                    text=translate(language, "texts.task_cancel_btn"),
                    callback_data=f"task:cancel:{task.task_id}",
                ),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=translate(language, "texts.task_refresh"),
                callback_data=f"task:view:{task.task_id}",
            ),
            InlineKeyboardButton(
                text=translate(language, "texts.task_all_tasks"),
                callback_data="task:list:active",
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --- Управление жизненным циклом задачи ---
class TaskControl:
    def __init__(self) -> None:
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
            if not await self.checkpoint():
                return False
            try:
                await asyncio.wait_for(self._resume_event.wait(), timeout=remaining)
            except TimeoutError:
                return not self._cancelled

    @property
    def task_id(self) -> str:
        return self.task.task_id if self.task else "-"


class TaskQueueManager:
    def __init__(self, bot: Bot) -> None:
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
                except Exception as e:
                    logger.exception(f"Воркер #{index}: сбой выполнения {task_id}: {e}")  # noqa: TRY401
                finally:
                    self.queue.task_done()
        except asyncio.CancelledError:
            logger.info(f"Воркер #{index} остановлен")
        except Exception as e:
            logger.exception(f"Воркер #{index}: критическая ошибка {e}")  # noqa: TRY401

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
            except RUNTIME_ERRORS as e:
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
            logger.exception(f"Задача {task_id} завершилась ошибкой: {e}")  # noqa: TRY401
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
        await handler(task, control)

    async def _finish_failed(self, task: Task, error: str) -> None:
        await tasks_db.update_task(
            task.task_id,
            {
                "status": "failed",
                "error": html.escape(error)[:900],
                "completed_at": utc_now_iso(),
                "progress_text": html.escape(error)[:200],
            },
        )
        language = await user_db.get_language(task.user_id)
        await safe_send_message(
            self.bot,
            task.user_id,
            translate(language, "texts.task_failed_report", task_id=task.task_id, error=error),
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
            self.bot, task.user_id, translate(language, key, task_id=task.task_id)
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
            except Exception as e:
                logger.exception(f"Ошибка масштабирования воркеров: {e}")  # noqa: TRY401
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
            except Exception as e:
                logger.exception(f"Ошибка health check задач: {e}")  # noqa: TRY401
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
            except Exception as e:
                logger.exception(f"Ошибка очистки задач: {e}")  # noqa: TRY401
                await asyncio.sleep(backoff_delay())


queue_manager: TaskQueueManager | None = None


def get_queue_manager() -> TaskQueueManager:
    if queue_manager is None:
        raise BotError("Планировщик задач не инициализирован", code=503)
    return queue_manager


# --- Прогресс задач ---
class TaskProgressReporter:
    UPDATE_INTERVAL: ClassVar[float] = 12.0

    def __init__(self, task: Task, control: TaskControl, interval: float | None = None) -> None:
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
                    "progress_text": html.escape(text)[:200],
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
    if not Features.AUTO_LEAVE:
        logger.info("AUTO_LEAVE выключен, чаты оставляем")
        return
    if source is not None and source_joined:
        await leave_chat(account, source, "источник")
    if target is not None and target_joined:
        await leave_chat(account, target, "цель")


async def run_scrape_invite_task(task: Task, control: TaskControl) -> None:
    data = task.data
    language = await user_db.get_language(task.user_id)
    source_identifier = str(data.get("source") or "").strip()
    target_identifier = str(data.get("target") or "").strip()
    mode = str(data.get("mode") or "messages")
    message_limit = to_int(data.get("message_limit"), Config.SCRAPE_MAX_MESSAGE_LIMIT)
    user_limit = to_int(data.get("user_limit"), 0)
    session_file = str(data.get("account") or "auto")

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

    async with account_manager.acquire(None if session_file == "auto" else session_file) as account:
        account_name = account.session_file
        task.data["account"] = account_name
        await tasks_db.update_task(task.task_id, {"data": {"account": account_name}})
        result = InviteResult()

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
            source_entity, source_joined = await join_chat(account, source_identifier)
        except FloodWaitError as e:
            seconds = float(getattr(e, "seconds", 60))
            extended = account_manager.handle_flood_wait(account, seconds)
            await tasks_db.update_task(
                task.task_id, {"status": "paused", "paused_at": utc_now_iso()}
            )
            await reporter.finalize(
                translate(
                    language,
                    "texts.floodwait_extended_pause",
                    task_id=task.task_id,
                    seconds=seconds,
                    extended=extended,
                )
            )
            return
        except (AuthKeyUnregisteredError, ChatUnreachableError) as e:
            await tasks_db.update_task(
                task.task_id,
                {
                    "status": "failed",
                    "error": html.escape(str(e))[:900],
                    "completed_at": utc_now_iso(),
                },
            )
            await reporter.finalize()
            return

        try:
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
        except FloodWaitError as e:
            seconds = float(getattr(e, "seconds", 60))
            account_manager.handle_flood_wait(account, seconds)
            await _release_chats(account, None, False, source_entity, source_joined)
            await tasks_db.update_task(
                task.task_id,
                {
                    "status": "failed",
                    "error": translate(
                        language, "texts.task_failed_flood_scrape", seconds=int(seconds)
                    ),
                    "completed_at": utc_now_iso(),
                },
            )
            await reporter.finalize()
            return
        except AuthKeyUnregisteredError:
            account.is_valid = False
            await tasks_db.update_task(
                task.task_id,
                {
                    "status": "failed",
                    "error": translate(language, "texts.task_failed_auth_reset"),
                    "completed_at": utc_now_iso(),
                },
            )
            await reporter.finalize()
            return

        if not collected:
            await _release_chats(account, None, False, source_entity, source_joined)
            await tasks_db.update_task(
                task.task_id,
                {
                    "status": "failed",
                    "error": translate(language, "texts.no_users_found", source=source_identifier),
                    "completed_at": utc_now_iso(),
                },
            )
            await reporter.finalize()
            return

        logger.info(f"Задача {task.task_id}: собрано {len(collected)} пользователей")

        target_entity = None
        target_joined = False
        try:
            target_entity, target_joined = await join_chat(account, target_identifier)
        except FloodWaitError as e:
            seconds = float(getattr(e, "seconds", 60))
            account_manager.handle_flood_wait(account, seconds)
            await _release_chats(account, None, False, source_entity, source_joined)
            await tasks_db.update_task(
                task.task_id,
                {
                    "status": "failed",
                    "error": translate(
                        language, "texts.task_failed_flood_target", seconds=int(seconds)
                    ),
                    "completed_at": utc_now_iso(),
                },
            )
            await reporter.finalize()
            return
        except (ChatUnreachableError, AuthKeyUnregisteredError) as e:
            await _release_chats(account, None, False, source_entity, source_joined)
            await tasks_db.update_task(
                task.task_id,
                {
                    "status": "failed",
                    "error": html.escape(str(e))[:900],
                    "completed_at": utc_now_iso(),
                },
            )
            await reporter.finalize()
            return

        target_key = str(getattr(target_entity, "id", target_identifier))
        total = len(collected)
        processed = 0
        for user_id in collected:
            if not await control.checkpoint():
                result.remaining = collected[processed:]
                break
            processed += 1
            if await cache_db.is_invited(target_key, user_id):
                result.already_members += 1
                continue
            if account_manager.should_simulate_skip():
                delay = rand_range(Config.HUMAN_SKIP_DELAY_MIN, Config.HUMAN_SKIP_DELAY_MAX)
                await control.wait(delay)
                continue
            try:
                outcome = await _invite_user(account, target_entity, user_id)
            except FloodWaitError as e:
                seconds = float(getattr(e, "seconds", 60))
                extended = account_manager.handle_flood_wait(account, seconds)
                logger.warning(f"FloodWait на инвайте: {seconds:.0f}с -> {extended:.0f}с")
                await control.wait(min(extended, 300.0))
                result.remaining = collected[processed - 1 :]
                break
            except AuthKeyUnregisteredError:
                account.is_valid = False
                result.remaining = collected[processed - 1 :]
                break
            except TELEGRAM_ERRORS as e:
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

            if processed % max(1, Config.INVITE_BUFFER_SIZE) == 0 or processed == total:
                task.sent = result.success
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
            account_manager._update_success_rate(account, outcome == "success")
            if control.is_paused or control.is_cancelled:
                break
            delay = rand_range(Config.MIN_INVITE_DELAY, Config.MAX_INVITE_DELAY)
            if processed % max(1, Config.HUMAN_DELAY_EVERY) == 0:
                delay = rand_range(Config.HUMAN_BREAK_MIN, Config.HUMAN_BREAK_MAX)
            if processed % max(1, Config.INVITE_BUFFER_SIZE) == 0:
                delay += rand_range(Config.POST_BUFFER_DELAY_MIN, Config.POST_BUFFER_DELAY_MAX)
            if not await control.wait(delay):
                break

        await _release_chats(account, target_entity, target_joined, source_entity, source_joined)

    task.sent = result.success
    status = "cancelled" if control.is_cancelled else "completed"
    await tasks_db.update_task(
        task.task_id,
        {
            "status": status,
            "progress": 100.0,
            "sent": result.success,
            "results": json.dumps(
                {
                    "success": result.success,
                    "failed": result.failed,
                    "privacy": result.privacy_errors,
                    "already": result.already_members,
                    "processed": processed,
                    "total": total,
                },
                ensure_ascii=False,
            ),
            "completed_at": utc_now_iso(),
        },
    )
    if status == "cancelled":
        await safe_send_message(
            get_queue_manager().bot,
            task.user_id,
            translate(language, "texts.task_cancelled_report", task_id=task.task_id),
        )
        await reporter.finalize()
        return
    await safe_send_message(
        get_queue_manager().bot,
        task.user_id,
        translate(
            language,
            "texts.task_completed_report",
            task_id=task.task_id,
            source=html.escape(source_identifier),
            target=html.escape(target_identifier),
            invited=processed,
            success=result.success,
            failed=result.failed,
            privacy=result.privacy_errors,
            account=html.escape(account_name),
        ),
    )
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
            if Features.AUTO_LEAVE and joined:
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
    min_delay = to_int(data.get("min_delay"), Config.MAILING_MIN_DELAY)
    max_delay = to_int(data.get("max_delay"), Config.MAILING_MAX_DELAY)
    max_sends = to_int(data.get("total"), Config.MAILING_MAX_TOTAL_SENDS)
    sender = str(data.get("account") or "auto")
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
    session_file = "" if sender == "auto" else sender

    async with account_manager.acquire(session_file or None) as account:
        session_file = account.session_file
        await tasks_db.update_task(task.task_id, {"data": {"account": session_file}})

        if target == "users":
            recipients: list[Any] = await _collect_mailing_users(
                account, chats, Config.SCRAPE_MAX_USER_COUNT, control
            )
        else:
            recipients = list(chats)
            total_planned = min(
                max_sends if max_sends > 0 else Config.MAILING_MAX_TOTAL_SENDS,
                Config.MAILING_MAX_TOTAL_SENDS,
            )
            if total_planned > len(recipients):
                recipients = [recipients[index % len(recipients)] for index in range(total_planned)]

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

        consecutive_errors = 0
        for index, recipient in enumerate(recipients, start=1):
            if not await control.checkpoint():
                break
            key = str(recipient)
            entity: Any = None
            joined = False
            failed = False

            if target != "users":
                try:
                    entity, joined = await join_chat(account, key)
                except FloodWaitError as e:
                    seconds = float(getattr(e, "seconds", 60)) + Config.MAILING_FLOOD_WAIT_PADDING
                    account_manager.handle_flood_wait(account, seconds)
                    await control.wait(min(seconds, 300.0))
                    errors += 1
                    continue
                except ChatUnreachableError as e:
                    logger.warning(f"Рассылка: чат {key} недоступен: {e}")
                    errors += 1
                    continue
                except AuthKeyUnregisteredError:
                    account.is_valid = False
                    break
                destination = entity
            else:
                destination = key

            text = texts[random.randrange(len(texts))]
            try:
                await require_client(account).send_message(destination, text)
                sent += 1
                per_chat[key] = per_chat.get(key, 0) + 1
                consecutive_errors = 0
            except FloodWaitError as e:
                seconds = float(getattr(e, "seconds", 60)) + Config.MAILING_FLOOD_WAIT_PADDING
                account_manager.handle_flood_wait(account, seconds)
                await control.wait(min(seconds, 300.0))
                errors += 1
                consecutive_errors += 1
                failed = True
            except AuthKeyUnregisteredError:
                account.is_valid = False
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
                failed = True
            finally:
                if target != "users" and Features.AUTO_LEAVE and joined:
                    await leave_chat(account, entity, key)

            task.sent = sent
            await _apply_progress(
                control,
                reporter,
                index,
                len(recipients),
                f"{target}: {index}/{len(recipients)}",
            )
            if failed and target != "users":
                continue
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
    status = "cancelled" if control.is_cancelled else "completed"
    report_lines = "\n".join(
        f"• {html.escape(chat)}: {count}" for chat, count in list(per_chat.items())[:20]
    )
    await tasks_db.update_task(
        task.task_id,
        {
            "status": status,
            "progress": 100.0,
            "sent": sent,
            "results": json.dumps(
                {
                    "sent": sent,
                    "chats": per_chat,
                    "errors": errors,
                    "privacy": privacy,
                    "target": target,
                },
                ensure_ascii=False,
            ),
            "completed_at": utc_now_iso(),
        },
    )
    if status == "cancelled":
        await safe_send_message(
            get_queue_manager().bot,
            task.user_id,
            translate(language, "texts.mailing_cancelled_report", task_id=task.task_id),
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
    await safe_send_message(
        get_queue_manager().bot,
        task.user_id,
        summary,
    )
    await reporter.finalize()


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

    async with account_manager.acquire(None) as account:
        session_file = account.session_file
        await tasks_db.update_task(task.task_id, {"data": {"account": session_file}})
        for identifier in sources:
            if not await control.checkpoint():
                break
            stats = WormSourceStats()
            entity = None
            joined = False
            try:
                entity, joined = await join_chat(account, identifier)
            except FloodWaitError as e:
                seconds = float(getattr(e, "seconds", 60))
                account_manager.handle_flood_wait(account, seconds)
                await control.wait(min(seconds, 300.0))
                continue
            except (ChatUnreachableError, AuthKeyUnregisteredError) as e:
                stats.errors += 1
                logger.warning(f"Червь: источник {identifier} недоступен: {e}")
                continue

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
                        validated = await validate_and_test_chat(account, target)
                        if validated is not None:
                            stats.added += 1
                        if not await control.wait(
                            rand_range(Config.WORM_MIN_DELAY, Config.WORM_MAX_DELAY)
                        ):
                            break
            except FloodWaitError as e:
                seconds = float(getattr(e, "seconds", 60))
                account_manager.handle_flood_wait(account, seconds)
                await control.wait(min(seconds, 300.0))
            except AuthKeyUnregisteredError:
                account.is_valid = False
                break
            except TELEGRAM_ERRORS as e:
                stats.errors += 1
                logger.error(f"Червь: ошибка обработки {identifier}: {e}")
            finally:
                if Features.AUTO_LEAVE and joined:
                    await leave_chat(account, entity, identifier)

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
                f"{identifier}: +{stats.added} чатов",
            )
            if processed_sources[-1] != sources[-1] and not await control.wait(
                rand_range(Config.WORM_MIN_DELAY, Config.WORM_MAX_DELAY)
            ):
                break

    status = "cancelled" if control.is_cancelled else "completed"
    await tasks_db.update_task(
        task.task_id,
        {
            "status": status,
            "progress": 100.0,
            "results": json.dumps(
                {
                    "messages": totals.messages,
                    "links": totals.links,
                    "added": totals.added,
                    "errors": totals.errors,
                },
                ensure_ascii=False,
            ),
            "completed_at": utc_now_iso(),
        },
    )
    await reporter.finalize(
        translate(
            language,
            "texts.worm_stopped_multi",
            messages=totals.messages,
            links=totals.links,
            added=totals.added,
            errors=totals.errors,
        )
        if status == "completed"
        else ""
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
    waiting_scrape_mode: State = State()
    waiting_scrape_limit: State = State()
    waiting_scrape_account: State = State()
    waiting_mail_chats: State = State()
    waiting_mail_delay: State = State()
    waiting_mail_text: State = State()
    waiting_mail_account: State = State()
    waiting_mail_total: State = State()


LOGIN_CLIENTS: dict[int, TelegramClient] = {}
BACKGROUND_TASKS: set[asyncio.Task[None]] = set()


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
def kb_main(language: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=translate(language, "buttons.add_chats_to_db"),
                callback_data="menu:add_chats",
            ),
            InlineKeyboardButton(
                text=translate(language, "buttons.update_chats_db"),
                callback_data="menu:update_chats",
            ),
        ],
        [
            InlineKeyboardButton(
                text=translate(language, "buttons.start_scraping"),
                callback_data="menu:scrape",
            ),
            InlineKeyboardButton(
                text=translate(language, "buttons.bulk_mailing"),
                callback_data="menu:mailing",
            ),
        ],
    ]
    if Features.WORM_MODE:
        rows.append(
            [
                InlineKeyboardButton(
                    text=translate(language, "buttons.worm_mode"),
                    callback_data="menu:worm",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=translate(language, "buttons.my_tasks"),
                callback_data="task:list:mine",
            ),
            InlineKeyboardButton(
                text=translate(language, "buttons.task_list"),
                callback_data="task:list:active",
            ),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text=translate(language, "buttons.add_account"),
                callback_data="menu:add_account",
            ),
            InlineKeyboardButton(
                text=translate(language, "buttons.list_accounts"),
                callback_data="menu:accounts",
            ),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text=translate(language, "buttons.clear_cache"),
                callback_data="menu:clear_cache",
            ),
            InlineKeyboardButton(
                text=translate(language, "buttons.cancel_all_tasks"),
                callback_data="menu:cancel_all",
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_cancel(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=translate(language, "buttons.cancel"), callback_data="menu:main"
                )
            ]
        ]
    )


def kb_source_choice(language: str, prefix: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=translate(language, "texts.bulkmail_source_db_btn"),
                callback_data=f"{prefix}:db",
            )
        ],
        [
            InlineKeyboardButton(
                text=translate(language, "texts.bulkmail_source_manual"),
                callback_data=f"{prefix}:manual",
            )
        ],
        [
            InlineKeyboardButton(
                text=translate(language, "buttons.cancel"), callback_data="menu:main"
            )
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_account_choice(
    language: str, prefix: str, auto_key: str = "texts.scrape_auto"
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=translate(language, auto_key), callback_data=f"{prefix}:auto")]
    ]
    for account in account_manager.accounts[:10]:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🧾 {account.session_file}",
                    callback_data=f"{prefix}:session:{account.session_file}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=translate(language, "buttons.cancel"), callback_data="menu:main"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_mail_target(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=translate(language, "texts.bulkmail_target_chats"),
                    callback_data="mail:target:chats",
                )
            ],
            [
                InlineKeyboardButton(
                    text=translate(language, "texts.bulkmail_target_users"),
                    callback_data="mail:target:users",
                )
            ],
            [
                InlineKeyboardButton(
                    text=translate(language, "buttons.cancel"), callback_data="menu:main"
                )
            ],
        ]
    )


def kb_mail_more_text(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=translate(language, "texts.bulkmail_texts_done"),
                    callback_data="mail:texts_done",
                )
            ],
            [
                InlineKeyboardButton(
                    text=translate(language, "buttons.cancel"), callback_data="menu:main"
                )
            ],
        ]
    )


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
            if isinstance(event, Message):
                await event.answer(translate(DEFAULT_LANGUAGE, "texts.only_admin"))
            elif isinstance(event, CallbackQuery):
                await event.answer(translate(DEFAULT_LANGUAGE, "texts.only_admin"), show_alert=True)
            return None
        data["user_id"] = user_id
        data["language"] = await user_db.get_language(user_id)
        return await handler(event, data)


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
            if isinstance(event, Message):
                with suppress(TelegramBadRequest, TelegramNetworkError):
                    await event.answer(f"⚠️ {html.escape(e.message)}")
            elif isinstance(event, CallbackQuery):
                with suppress(TelegramBadRequest, TelegramNetworkError):
                    await event.answer(f"⚠️ {html.escape(e.message)}", show_alert=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"Необработанная ошибка: {e}")  # noqa: TRY401
            if isinstance(event, Message):
                with suppress(TelegramBadRequest, TelegramNetworkError):
                    await event.answer(
                        translate(data.get("language", DEFAULT_LANGUAGE), "texts.invalid_format")
                    )
        return None


router: Router = Router(name="inviter")


# --- Хелперы для сообщений ---
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
    return (
        f"{task_status_emoji(task.status)} <code>{html.escape(task.task_id)}</code> "
        f"[{format_task_type(task.type, language)}] "
        f"{format_progress_bar(task.progress)} {html.escape(task.progress_text or '')}".strip()
    )


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


# --- Обработчики: старт и меню ---
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, language: str) -> None:
    await state.clear()
    await user_db.ensure_user(message.from_user.id)
    chats = await chat_db.get_active_chats_count()
    users = await chat_db.get_total_users()
    text = translate(
        language,
        "texts.welcome_admin",
        accounts=len(account_manager.accounts),
        chats=chats,
        users=users,
    )
    await smart_answer(message, text, reply_markup=kb_main(language))


@router.callback_query(F.data == "menu:main")
async def cb_main(call: CallbackQuery, state: FSMContext, language: str) -> None:
    await state.clear()
    await call.answer()
    await smart_answer(
        call,
        translate(
            language,
            "texts.welcome_admin",
            accounts=len(account_manager.accounts),
            chats=await chat_db.get_active_chats_count(),
            users=await chat_db.get_total_users(),
        ),
        reply_markup=kb_main(language),
        delete_origin=True,
    )


# --- Аккаунты ---
@router.callback_query(F.data == "menu:add_account")
async def cb_add_account(call: CallbackQuery, state: FSMContext, language: str) -> None:
    await state.clear()
    await call.answer()
    await smart_answer(
        call,
        translate(language, "texts.waiting_phone"),
        reply_markup=kb_cancel(language),
        delete_origin=True,
    )
    await state.set_state(InviterStates.waiting_phone)


@router.message(InviterStates.waiting_phone)
async def on_phone(message: Message, state: FSMContext, language: str) -> None:
    phone = (message.text or "").strip()
    if not is_phone(phone):
        await smart_answer(message, translate(language, "texts.invalid_format"))
        return
    await _drop_login_client(message.from_user.id)
    client = create_telegram_client()
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
    except (PhoneNumberInvalidError, FloodError) as e:
        await client.disconnect()
        await smart_answer(message, translate(language, "texts.invalid_format"))
        logger.warning(f"Ошибка отправки кода: {e}")
        return
    except TELEGRAM_ERRORS as e:
        await client.disconnect()
        logger.error(f"Ошибка подключения при добавлении аккаунта: {e}")
        await smart_answer(message, translate(language, "texts.invalid_format"))
        return
    LOGIN_CLIENTS[message.from_user.id] = client
    await state.update_data(
        phone=phone, phone_code_hash=sent.phone_code_hash, session_name=phone.replace("+", "")
    )
    await smart_answer(
        message,
        translate(language, "texts.waiting_code", phone=html.escape(phone)),
        reply_markup=kb_cancel(language),
    )
    await state.set_state(InviterStates.waiting_code)


@router.message(InviterStates.waiting_code)
async def on_code(message: Message, state: FSMContext, language: str) -> None:
    code = re.sub(r"\D", "", message.text or "")
    data = await state.get_data()
    client = LOGIN_CLIENTS.get(message.from_user.id)
    if client is None or not code:
        await smart_answer(message, translate(language, "texts.auth_error_state"))
        return
    try:
        await client.sign_in(
            phone=data.get("phone"), code=code, phone_code_hash=data.get("phone_code_hash")
        )
    except SessionPasswordNeededError:
        await smart_answer(
            message,
            translate(language, "texts.waiting_password"),
            reply_markup=kb_cancel(language),
        )
        await state.set_state(InviterStates.waiting_password)
        return
    except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
        logger.warning(f"Неверный код: {e}")
        await smart_answer(message, translate(language, "texts.invalid_format"))
        return
    except TELEGRAM_ERRORS as e:
        logger.error(f"Ошибка входа: {e}")
        await _drop_login_client(message.from_user.id)
        await state.clear()
        await smart_answer(message, translate(language, "texts.invalid_format"))
        return
    await _finish_account_login(message, state, client, language)


@router.message(InviterStates.waiting_password)
async def on_password(message: Message, state: FSMContext, language: str) -> None:
    client = LOGIN_CLIENTS.get(message.from_user.id)
    if client is None:
        await smart_answer(message, translate(language, "texts.auth_error_state"))
        return
    try:
        await client.sign_in(password=message.text or "")
    except PasswordHashInvalidError:
        await smart_answer(message, translate(language, "texts.invalid_format"))
        return
    except TELEGRAM_ERRORS as e:
        logger.error(f"Ошибка ввода пароля: {e}")
        await _drop_login_client(message.from_user.id)
        await state.clear()
        await smart_answer(message, translate(language, "texts.invalid_format"))
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
    except TELEGRAM_ERRORS as e:
        logger.error(f"Не удалось сохранить сессию: {e}")
        await _drop_login_client(message.from_user.id)
        await state.clear()
        await smart_answer(message, translate(language, "texts.invalid_format"))
        return
    await _drop_login_client(message.from_user.id)
    await state.clear()
    if not account_manager.add_account(session_string, session_name):
        await smart_answer(message, translate(language, "texts.invalid_format"))
        return
    await smart_answer(
        message,
        translate(
            language,
            "texts.account_added",
            name=html.escape(session_name),
            username=html.escape(getattr(me, "username", "") or "-"),
            phone=html.escape(getattr(me, "phone", "") or "-"),
        ),
        reply_markup=kb_main(language),
    )


@router.callback_query(F.data == "menu:accounts")
async def cb_accounts(call: CallbackQuery, language: str) -> None:
    await call.answer()
    if not account_manager.accounts:
        await smart_answer(call, translate(language, "texts.no_accounts"), delete_origin=True)
        return
    lines = [translate(language, "texts.accounts_list")]
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
    await smart_answer(
        call,
        "\n".join(lines),
        reply_markup=kb_main(language),
        delete_origin=True,
    )


# --- Чаты ---
@router.callback_query(F.data == "menu:add_chats")
async def cb_add_chats(call: CallbackQuery, state: FSMContext, language: str) -> None:
    await state.clear()
    await call.answer()
    await smart_answer(
        call,
        translate(language, "texts.worm_waiting_links"),
        reply_markup=kb_cancel(language),
        delete_origin=True,
    )
    await state.set_state(InviterStates.waiting_links)


@router.message(InviterStates.waiting_links)
async def on_links(message: Message, state: FSMContext, language: str) -> None:
    links = await parse_links_from_message(message)
    if not links:
        await smart_answer(message, translate(language, "texts.worm_wait_links_input"))
        return
    added: list[str] = []
    failed = 0
    if not account_manager.accounts:
        await smart_answer(message, translate(language, "texts.scrape_no_accounts"))
        await state.clear()
        return
    async with account_manager.acquire(None) as account:
        for link in links:
            if len(added) >= Config.WORM_MAX_SOURCES:
                break
            identifier = parse_identifier(link)
            if not identifier:
                failed += 1
                continue
            try:
                validated = await validate_and_test_chat(account, identifier)
            except FloodWaitError as e:
                account_manager.handle_flood_wait(account, float(getattr(e, "seconds", 60)))
                break
            except AuthKeyUnregisteredError:
                account.is_valid = False
                break
            except TELEGRAM_ERRORS as e:
                logger.warning(f"Ошибка добавления {identifier}: {e}")
                failed += 1
                continue
            if validated is None:
                failed += 1
            else:
                added.append(
                    translate(
                        language,
                        "texts.chat_added_result",
                        chat_name=html.escape(validated.chat_name),
                        user_count=validated.user_count,
                    )
                )
    await state.clear()
    text = translate(
        language,
        "texts.chats_added_title",
        count=len(links),
    )
    if added:
        text += "\n\n" + "\n".join(added)
    if failed:
        text += f"\n\n{translate(language, 'texts.more_results', count=failed)}"
    await smart_answer(message, text, reply_markup=kb_main(language))


@router.callback_query(F.data == "menu:update_chats")
async def cb_update_chats(call: CallbackQuery, language: str) -> None:
    await call.answer()
    total = await chat_db.get_active_chats_count()
    await smart_answer(
        call,
        translate(language, "texts.update_db_start", total=total),
        reply_markup=kb_main(language),
        delete_origin=True,
    )
    if not account_manager.accounts:
        await safe_send_message(
            call.bot,
            call.from_user.id,
            translate(language, "texts.no_available_accounts"),
        )
        return
    async with account_manager.acquire(None) as account:
        try:
            result = await check_and_clean_chats(account)
        except AuthKeyUnregisteredError:
            account.is_valid = False
            await safe_send_message(
                call.bot, call.from_user.id, translate(language, "texts.invalid_account")
            )
            return
        except FloodWaitError as e:
            seconds = float(getattr(e, "seconds", 60))
            account_manager.handle_flood_wait(account, seconds)
            await safe_send_message(
                call.bot,
                call.from_user.id,
                translate(language, "texts.floodwait_pause", task_id="-", seconds=int(seconds)),
            )
            return
    await safe_send_message(
        call.bot,
        call.from_user.id,
        translate(
            language,
            "texts.update_db_done",
            checked=result.checked,
            added=0,
            removed=result.removed,
            errors=result.errors,
        ),
    )


@router.callback_query(F.data == "menu:clear_cache")
async def cb_clear_cache(call: CallbackQuery, language: str) -> None:
    await call.answer()
    removed = await cache_db.clear()
    await entity_cache.clear()
    await full_chat_cache.clear()
    await smart_answer(
        call,
        translate(language, "texts.cache_cleared", count=removed),
        delete_origin=True,
    )


# --- Задачи ---
@router.callback_query(F.data == "task:list:active")
async def cb_task_list_active(call: CallbackQuery, language: str) -> None:
    await call.answer()
    tasks = [task for task in await tasks_db.get_active_tasks()]
    if not tasks:
        await smart_answer(call, translate(language, "texts.no_active_tasks"), delete_origin=True)
        return
    lines = ["📋 " + translate(language, "buttons.task_list")]
    lines.extend(format_task_row(task, language) for task in tasks[:20])
    await smart_answer(call, "\n".join(lines), reply_markup=kb_main(language), delete_origin=True)


@router.callback_query(F.data == "task:list:mine")
async def cb_task_list_mine(call: CallbackQuery, language: str) -> None:
    await call.answer()
    tasks = [task for task in await tasks_db.get_user_tasks(call.from_user.id) if task.is_active]
    if not tasks:
        await smart_answer(call, translate(language, "texts.no_active_tasks"), delete_origin=True)
        return
    lines = ["📋 " + translate(language, "buttons.my_tasks")]
    lines.extend(format_task_row(task, language) for task in tasks[:20])
    await smart_answer(call, "\n".join(lines), reply_markup=kb_main(language), delete_origin=True)


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
        call, translate(language, "texts.task_paused", task_id=task_id), delete_origin=True
    )


@router.callback_query(F.data.startswith("task:resume:"))
async def cb_task_resume(call: CallbackQuery, language: str) -> None:
    task_id = call.data.split(":", 2)[2]
    if not await get_queue_manager().request_resume(task_id):
        await call.answer(translate(language, "texts.task_not_found"), show_alert=True)
        return
    await call.answer()
    await smart_answer(
        call, translate(language, "texts.task_resumed", task_id=task_id), delete_origin=True
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
        delete_origin=True,
    )


@router.callback_query(F.data == "menu:cancel_all")
async def cb_cancel_all(call: CallbackQuery, language: str) -> None:
    await call.answer()
    await smart_answer(
        call,
        translate(language, "texts.task_cancel_confirm"),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=translate(language, "texts.task_confirm_yes"),
                        callback_data="cancel_all:yes",
                    ),
                    InlineKeyboardButton(
                        text=translate(language, "texts.task_confirm_no"),
                        callback_data="menu:main",
                    ),
                ]
            ]
        ),
        delete_origin=True,
    )


@router.callback_query(F.data == "cancel_all:yes")
async def cb_cancel_all_yes(call: CallbackQuery, language: str) -> None:
    await call.answer()
    count = await tasks_db.cancel_all_active()
    for task in await tasks_db.get_user_tasks(call.from_user.id):
        control = get_queue_manager().get_control(task.task_id)
        if control is not None:
            control.cancel()
    await smart_answer(
        call,
        translate(language, "texts.tasks_cancelled", count=count),
        reply_markup=kb_main(language),
        delete_origin=True,
    )


async def _submit_task(
    bot: Bot,
    user_id: int,
    language: str,
    task_type: str,
    data: dict[str, Any],
    launch_text: str,
) -> bool:
    if not account_manager.accounts:
        await safe_send_message(bot, user_id, translate(language, "texts.scrape_no_accounts"))
        return False
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
        )
        return False
    await safe_send_message(bot, user_id, launch_text, reply_markup=kb_main(language))
    return True


# --- Сбор пользователей и инвайты ---
@router.callback_query(F.data == "menu:scrape")
async def cb_scrape(call: CallbackQuery, state: FSMContext, language: str) -> None:
    await state.clear()
    await call.answer()
    await smart_answer(
        call,
        translate(language, "texts.waiting_source"),
        reply_markup=kb_cancel(language),
        delete_origin=True,
    )
    await state.set_state(InviterStates.waiting_scrape_source)


@router.message(InviterStates.waiting_scrape_source)
async def on_scrape_source(message: Message, state: FSMContext, language: str) -> None:
    identifier = parse_identifier(message.text or "")
    if not identifier:
        await smart_answer(message, translate(language, "texts.invalid_format"))
        return
    await state.update_data(source=identifier)
    await smart_answer(
        message,
        translate(language, "texts.waiting_target"),
        reply_markup=kb_cancel(language),
    )
    await state.set_state(InviterStates.waiting_scrape_target)


@router.message(InviterStates.waiting_scrape_target)
async def on_scrape_target(message: Message, state: FSMContext, language: str) -> None:
    identifier = parse_identifier(message.text or "")
    if not identifier:
        await smart_answer(message, translate(language, "texts.invalid_format"))
        return
    data = await state.get_data()
    if identifier == data.get("source"):
        await smart_answer(message, translate(language, "texts.invalid_format"))
        return
    await state.update_data(target=identifier)
    await smart_answer(
        message,
        translate(language, "texts.waiting_mode"),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=translate(language, "texts.mode_messages"),
                        callback_data="scrape:mode:messages",
                    ),
                    InlineKeyboardButton(
                        text=translate(language, "texts.mode_users"),
                        callback_data="scrape:mode:users",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=translate(language, "buttons.cancel"), callback_data="menu:main"
                    )
                ],
            ]
        ),
    )
    await state.set_state(InviterStates.waiting_scrape_mode)


@router.callback_query(F.data.startswith("scrape:mode:"))
async def cb_scrape_mode(call: CallbackQuery, state: FSMContext, language: str) -> None:
    mode = call.data.split(":")[-1]
    if mode not in ("messages", "users"):
        await call.answer(translate(language, "texts.invalid_format"), show_alert=True)
        return
    await state.update_data(mode=mode)
    await call.answer()
    await smart_answer(
        call,
        translate(
            language,
            "texts.waiting_message_limit" if mode == "messages" else "texts.waiting_user_count",
        ),
        reply_markup=kb_cancel(language),
        delete_origin=True,
    )
    await state.set_state(InviterStates.waiting_scrape_limit)


@router.message(InviterStates.waiting_scrape_limit)
async def on_scrape_limit(message: Message, state: FSMContext, language: str) -> None:
    data = await state.get_data()
    mode = str(data.get("mode") or "messages")
    value = to_int(message.text or "", -1)
    if mode == "messages":
        if not Config.SCRAPE_MIN_MESSAGE_LIMIT <= value <= Config.SCRAPE_MAX_MESSAGE_LIMIT:
            await smart_answer(
                message,
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
            await smart_answer(
                message,
                translate(
                    language,
                    "texts.invalid_number",
                    min_val=Config.SCRAPE_MIN_USER_COUNT,
                    max_val=Config.SCRAPE_MAX_USER_COUNT,
                ),
            )
            return
        await state.update_data(user_limit=value)
    await smart_answer(
        message,
        translate(language, "texts.scrape_select_account"),
        reply_markup=kb_account_choice(language, "scrape:account"),
    )
    await state.set_state(InviterStates.waiting_scrape_account)


@router.callback_query(F.data.startswith("scrape:account:"))
async def cb_scrape_account(call: CallbackQuery, state: FSMContext, language: str) -> None:
    payload = call.data.split(":")
    session = "auto" if payload[-1] == "auto" else ":".join(payload[2:])
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
    await _submit_task(
        call.bot,
        call.from_user.id,
        language,
        "scrape_invite",
        task_data,
        translate(
            language,
            "texts.task_launched",
            task_id="(см. карточку ниже)",
            source=html.escape(str(task_data["source"])),
            target=html.escape(str(task_data["target"])),
            mode=translate(language, "texts.mode_messages")
            if task_data["mode"] == "messages"
            else translate(language, "texts.mode_users"),
            account=html.escape(session),
        ),
    )
    if call.message is not None:
        with suppress(TelegramBadRequest, TelegramNetworkError):
            await call.message.delete()


# --- Режим червя ---
@router.callback_query(F.data == "menu:worm")
async def cb_worm(call: CallbackQuery, state: FSMContext, language: str) -> None:
    if not Features.WORM_MODE:
        await call.answer(translate(language, "texts.invalid_format"), show_alert=True)
        return
    await state.clear()
    await call.answer()
    await smart_answer(
        call,
        translate(language, "texts.worm_waiting_chat"),
        reply_markup=kb_cancel(language),
        delete_origin=True,
    )
    await state.set_state(InviterStates.waiting_worm_chats)


@router.message(InviterStates.waiting_worm_chats)
async def on_worm_chats(message: Message, state: FSMContext, language: str) -> None:
    links = await parse_links_from_message(message)
    sources: list[str] = []
    for link in links:
        identifier = parse_identifier(link)
        if identifier and identifier not in sources:
            sources.append(identifier)
    if not sources:
        await smart_answer(message, translate(language, "texts.invalid_format"))
        return
    if len(sources) > Config.WORM_MAX_SOURCES:
        await smart_answer(
            message, translate(language, "texts.worm_max_sources", count=Config.WORM_MAX_SOURCES)
        )
        sources = sources[: Config.WORM_MAX_SOURCES]
    await state.clear()
    await _submit_task(
        message.bot,
        message.from_user.id,
        language,
        "worm",
        {"sources": sources, "chats_label": f"{len(sources)}"},
        translate(
            language,
            "texts.worm_started_multi",
            chats=len(sources),
            sources="\n".join(html.escape(item) for item in sources),
        ),
    )


@router.message(Command("stop_worm"))
async def cmd_stop_worm(message: Message, language: str) -> None:
    cancelled = 0
    for task in await tasks_db.get_user_tasks(message.from_user.id):
        if task.type == "worm" and task.is_active:
            await get_queue_manager().request_cancel(task.task_id, message.from_user.id)
            cancelled += 1
    await smart_answer(
        message,
        translate(language, "texts.worm_stopped_multi", messages=0, links=0, added=0, errors=0)
        if cancelled
        else translate(language, "texts.no_active_tasks"),
        reply_markup=kb_main(language),
    )


# --- Массовая рассылка ---
@router.callback_query(F.data == "menu:mailing")
async def cb_mailing(call: CallbackQuery, state: FSMContext, language: str) -> None:
    if not Features.MAILING:
        await call.answer(translate(language, "texts.invalid_format"), show_alert=True)
        return
    await state.clear()
    await call.answer()
    await smart_answer(
        call,
        translate(language, "texts.bulkmail_target_select"),
        reply_markup=kb_mail_target(language),
        delete_origin=True,
    )


@router.callback_query(F.data.startswith("mail:target:"))
async def cb_mail_target(call: CallbackQuery, state: FSMContext, language: str) -> None:
    target = call.data.split(":")[-1]
    if target not in ("chats", "users"):
        await call.answer(translate(language, "texts.invalid_format"), show_alert=True)
        return
    if target == "users" and not Features.MAILING_TO_USERS:
        await call.answer(translate(language, "texts.invalid_format"), show_alert=True)
        return
    await state.update_data(target=target)
    chat_count = await chat_db.get_active_chats_count()
    if chat_count == 0:
        await call.answer(translate(language, "texts.bulkmail_db_empty"), show_alert=True)
        return
    await call.answer()
    await smart_answer(
        call,
        translate(language, "texts.bulkmail_step1_db", count=chat_count),
        reply_markup=kb_source_choice(language, "mail:source"),
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
        await smart_answer(
            call,
            translate(language, "texts.bulkmail_step2_delay"),
            reply_markup=kb_cancel(language),
            delete_origin=True,
        )
        await state.set_state(InviterStates.waiting_mail_delay)
        return
    if mode != "manual":
        await call.answer(translate(language, "texts.invalid_format"), show_alert=True)
        return
    await call.answer()
    await smart_answer(
        call,
        translate(language, "texts.bulkmail_step1_manual"),
        reply_markup=kb_cancel(language),
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
        await smart_answer(message, translate(language, "texts.bulkmail_empty_chats"))
        return
    await state.update_data(chats=identifiers)
    await smart_answer(
        message,
        translate(language, "texts.bulkmail_step2_delay"),
        reply_markup=kb_cancel(language),
    )
    await state.set_state(InviterStates.waiting_mail_delay)


@router.message(InviterStates.waiting_mail_delay)
async def on_mail_delay(message: Message, state: FSMContext, language: str) -> None:
    parts = re.split(r"[\s,]+", (message.text or "").strip())
    if len(parts) != 2:
        await smart_answer(message, translate(language, "texts.bulkmail_invalid_delay"))
        return
    min_delay, max_delay = to_int(parts[0], -1), to_int(parts[1], -1)
    if min_delay < 0 or max_delay < 0 or min_delay > max_delay:
        await smart_answer(message, translate(language, "texts.bulkmail_invalid_delay"))
        return
    await state.update_data(min_delay=min_delay, max_delay=max_delay)
    await smart_answer(
        message,
        translate(language, "texts.bulkmail_step3_text_first"),
        reply_markup=kb_cancel(language),
    )
    await state.set_state(InviterStates.waiting_mail_text)


@router.message(InviterStates.waiting_mail_text)
async def on_mail_text(message: Message, state: FSMContext, language: str) -> None:
    text_value = (message.text or "").strip()
    if not text_value:
        await smart_answer(message, translate(language, "texts.empty_input"))
        return
    data = await state.get_data()
    texts: list[str] = list(data.get("texts", []))
    texts.append(text_value)
    await state.update_data(texts=texts)
    await smart_answer(
        message,
        translate(language, "texts.bulkmail_texts_received", count=len(texts)),
        reply_markup=kb_mail_more_text(language),
    )


@router.callback_query(F.data == "mail:texts_done")
async def cb_mail_texts_done(call: CallbackQuery, state: FSMContext, language: str) -> None:
    data = await state.get_data()
    if not data.get("texts"):
        await call.answer(translate(language, "texts.bulkmail_no_texts"), show_alert=True)
        return
    await call.answer()
    await smart_answer(
        call,
        translate(language, "texts.bulkmail_step4_account"),
        reply_markup=kb_account_choice(language, "mail:account"),
        delete_origin=True,
    )
    await state.set_state(InviterStates.waiting_mail_account)


@router.callback_query(F.data.startswith("mail:account:"))
async def cb_mail_account(call: CallbackQuery, state: FSMContext, language: str) -> None:
    payload = call.data.split(":")
    session = "auto" if payload[-1] == "auto" else ":".join(payload[2:])
    await state.update_data(account=session)
    await call.answer()
    await smart_answer(
        call,
        translate(language, "texts.bulkmail_sender_selected", sender=html.escape(session)),
        reply_markup=kb_cancel(language),
        delete_origin=True,
    )
    await state.set_state(InviterStates.waiting_mail_total)


@router.message(InviterStates.waiting_mail_total)
async def on_mail_total(message: Message, state: FSMContext, language: str) -> None:
    total = to_int(message.text or "", 0)
    if total <= 0:
        await smart_answer(message, translate(language, "texts.bulkmail_total_error"))
        return
    data = await state.get_data()
    chats: list[str] = list(data.get("chats", []))
    task_data = {
        "chats": chats,
        "chat_count": len(chats),
        "chats_label": f"{len(chats)}",
        "texts": list(data.get("texts", [])),
        "min_delay": to_int(data.get("min_delay"), Config.MAILING_MIN_DELAY),
        "max_delay": to_int(data.get("max_delay"), Config.MAILING_MAX_DELAY),
        "account": str(data.get("account") or "auto"),
        "target": str(data.get("target") or "chats"),
        "total": total,
    }
    await state.clear()
    await _submit_task(
        message.bot,
        message.from_user.id,
        language,
        "bulkmail",
        task_data,
        translate(
            language,
            "texts.bulkmail_sent",
            task_id="(см. карточку ниже)",
            sent=total,
        ),
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, language: str) -> None:
    current = await state.get_state()
    if current is None:
        return
    await state.clear()
    await _drop_login_client(message.from_user.id)
    await smart_answer(
        message, translate(language, "buttons.cancel"), reply_markup=kb_main(language)
    )


@router.message(Command("done"))
async def cmd_done(message: Message, state: FSMContext, language: str) -> None:
    if await state.get_state() != InviterStates.waiting_mail_text.state:
        await smart_answer(message, translate(language, "texts.invalid_format"))
        return
    data = await state.get_data()
    if not data.get("texts"):
        await smart_answer(message, translate(language, "texts.bulkmail_no_texts"))
        return
    await smart_answer(
        message,
        translate(language, "texts.bulkmail_step4_account"),
        reply_markup=kb_account_choice(language, "mail:account"),
    )
    await state.set_state(InviterStates.waiting_mail_account)


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
        except RUNTIME_ERRORS as e:
            logger.error(f"Ошибка очистки кэша: {e}")


# --- Запуск ---
async def on_startup(bot: Bot, dispatcher: Dispatcher) -> None:
    global queue_manager
    Config.validate()
    load_languages()
    language_errors = validate_languages()
    for error in language_errors:
        logger.error(f"Языки: {error}")
    feature_errors = Features.validate()
    for error in feature_errors:
        logger.warning(f"Функции: {error}")
    await tasks_db.connect()
    await chat_db.connect()
    await cache_db.connect()
    await user_db.connect()
    account_manager.load_accounts()
    queue_manager = TaskQueueManager(bot=bot)
    await queue_manager.start()
    await account_manager.start_health_check()
    BACKGROUND_TASKS.add(asyncio.create_task(cache_cleanup_loop(), name="cache-cleanup"))
    me = await bot.get_me()
    logger.info(f"Бот запущен: @{me.username}")
    await notify_owners(
        bot,
        translate(
            DEFAULT_LANGUAGE,
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
        ),
    )


async def on_shutdown(bot: Bot) -> None:
    logger.info("Останавливаю компоненты...")
    for task in list(BACKGROUND_TASKS):
        task.cancel()
    for task in list(BACKGROUND_TASKS):
        with suppress(asyncio.CancelledError, Exception):
            await task
    BACKGROUND_TASKS.clear()
    if queue_manager is not None:
        await queue_manager.stop()
    await account_manager.stop_health_check()
    await account_manager.release_all()
    await tasks_db.close()
    await chat_db.close()
    await cache_db.close()
    await user_db.close()
    await notify_owners(bot, translate(DEFAULT_LANGUAGE, "texts.admin_shutdown"))
    logger.info("Остановка завершена")


def install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        logger.info("Получен сигнал остановки")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError, ValueError, RuntimeError):
            loop.add_signal_handler(sig, _request_stop)


async def main() -> None:
    Config.validate()
    load_languages()
    if not LANGUAGES:
        raise ConfigError(f"Не найдены языковые файлы в {LANGS_PATH}")
    bot = Bot(
        token=Config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.update.outer_middleware(ErrorLogMiddleware())
    dispatcher.update.middleware(OwnerMiddleware())
    dispatcher.include_router(router)

    stop_event = asyncio.Event()
    install_signal_handlers(stop_event)

    await on_startup(bot, dispatcher)
    polling = asyncio.create_task(
        dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types()),
        name="polling",
    )
    stop_wait = asyncio.create_task(stop_event.wait(), name="stop-signal")
    try:
        await asyncio.wait({polling, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop_wait.cancel()
        polling.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await polling
        await on_shutdown(bot)
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Принудительная остановка")
    except ConfigError as error:
        logger.critical(error.message)
        sys.exit(1)
    except BotError as error:
        logger.critical(f"Критическая ошибка: {error}")
        sys.exit(1)
