import asyncio
import html
import json
import logging
import os
import random
import re
import secrets
import signal
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union, Callable, TypeVar

import aiofiles
import aiosqlite
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
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
from telethon import TelegramClient, functions, types as telethon_types
from telethon.errors import (
    AuthKeyUnregisteredError,
    ChannelPrivateError,
    ChatAdminRequiredError,
    FloodError,
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
    UserNotParticipantError,
    UserPrivacyRestrictedError,
)
from telethon.sessions import StringSession
from dotenv import load_dotenv

# === Типы ===
T = TypeVar("T")
JSONValue = Union[
    str, int, float, bool, None, List["JsonValue"], Dict[str, "JsonValue"]
]
JsonValue = Dict[str, JSONValue]


# === Настройка окружения ===
BASE_DIR: Path = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

# === Настройка логирования ===
LOGS_DIR: Path = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)
LOG_FILE: Path = LOGS_DIR / f"bot_{datetime.now().strftime('%Y-%m-%d')}.log"

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
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8", mode="a")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
logger.info("=== Логгер инициализирован ===")

# === Языки ===
LANGS_DIR: Path = BASE_DIR / "langs"
DEFAULT_LANGUAGE: str = "ru"


# === Загрузка всех языков ===
def load_languages() -> Dict[str, Dict[str, Any]]:
    languages: Dict[str, Dict[str, Any]] = {}
    if not LANGS_DIR.exists():
        logger.warning(f"Папка языков не найдена: {LANGS_DIR}")
        return languages
    for path in sorted(LANGS_DIR.glob("*.json")):
        try:
            raw = path.read_bytes()
            if raw[:3] == b"\xef\xbb\xbf":
                raw = raw[3:]
            data = json.loads(raw.decode("utf-8"))
            code = (
                str(data.get("meta", {}).get("code", path.stem)).strip().lower()
                or path.stem
            )
            languages[code] = data
        except Exception as e:
            logger.warning(f"Ошибка загрузки {path.stem}: {e}")
    if not languages:
        logger.warning("Не удалось загрузить ни одного языка")
    return languages


LANGUAGES = load_languages()


def get_available_languages() -> List[str]:
    """Возвращает список доступных кодов языков"""
    return list(LANGUAGES.keys())


def get_language_display_name(code: str) -> str:
    """Возвращает отображаемое имя языка из meta.name"""
    data = LANGUAGES.get(code, {})
    return str(data.get("meta", {}).get("name", code)).strip() or code


# === Языки и перевод ===
_LANG_CACHE: Dict[str, Dict[str, Any]] = {}
_LANG_CACHE_MAX_SIZE: int = 100


def _resolve_key(data: Dict[str, Any], key: str) -> Optional[Any]:
    node = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def translate(key: str, lang: Optional[str] = None, **kwargs: Any) -> str:
    language: str = (lang or DEFAULT_LANGUAGE).strip().lower()
    data: Optional[Dict[str, Any]] = _LANG_CACHE.get(language)
    if data is None:
        data = LANGUAGES.get(language)
        if data is None:
            path = LANGS_DIR / f"{language}.json"
            if path.exists():
                try:
                    raw = path.read_bytes()
                    if raw[:3] == b"\xef\xbb\xbf":
                        raw = raw[3:]
                    data = json.loads(raw.decode("utf-8"))
                    _LANG_CACHE[language] = data
                except Exception:
                    data = None
        if data is None and language != DEFAULT_LANGUAGE:
            data = LANGUAGES.get(DEFAULT_LANGUAGE)
            if data is None:
                data = _LANG_CACHE.get(DEFAULT_LANGUAGE)
        if data is None:
            data = {}
        _LANG_CACHE[language] = data
    text: Optional[Any] = _resolve_key(data, key)
    if text is None and language != DEFAULT_LANGUAGE:
        data = (
            LANGUAGES.get(DEFAULT_LANGUAGE) or _LANG_CACHE.get(DEFAULT_LANGUAGE) or {}
        )
        text = _resolve_key(data, key)
    if text is None:
        return key
    if kwargs and isinstance(text, str):
        try:
            return text.format(**kwargs)
        except Exception:
            return text
    return str(text)


# Загрузка языков при старте
for _lang_code in get_available_languages():
    _LANG_CACHE[_lang_code] = LANGUAGES.get(_lang_code, {})
logger.info(f"Языковая система инициализирована: {list(_LANG_CACHE.keys())}")


# --- Конфиг ---
def str_to_bool(val: str) -> bool:
    return str(val).strip().lower() in ("1", "true", "yes", "y", "on")


class Config:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
    API_ID: int = int(os.getenv("API_ID", "0"))
    API_HASH: str = os.getenv("API_HASH", "").strip()
    ADMIN_USER_IDS: List[int] = [
        int(x.strip()) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()
    ]
    MAX_CONCURRENT_TASKS: int = int(os.getenv("MAX_CONCURRENT_TASKS", "10"))
    MAX_TASKS_PER_USER: int = int(os.getenv("MAX_TASKS_PER_USER", "5"))
    SESSIONS_DIR: str = os.getenv("SESSIONS_DIR", "sessions")
    DATA_DIR: str = os.getenv("DATA_DIR", "data")
    AUTH_FILE: str = os.getenv("AUTH_FILE", os.path.join(DATA_DIR, "auth.json"))
    TASKS_FILE: str = os.getenv("TASKS_FILE", os.path.join(DATA_DIR, "tasks.json"))
    CACHE_DB_PATH: str = os.getenv("CACHE_DB_PATH", os.path.join(DATA_DIR, "cache.db"))
    CHATS_DB_PATH: str = os.getenv("CHATS_DB_PATH", os.path.join(DATA_DIR, "chats.db"))
    AUTO_LEAVE_AFTER_INVITE: bool = str_to_bool(
        os.getenv("AUTO_LEAVE_AFTER_INVITE", "false")
    )

    # Anti-block settings
    MIN_INVITE_DELAY: int = int(os.getenv("MIN_INVITE_DELAY", "40"))
    MAX_INVITE_DELAY: int = int(os.getenv("MAX_INVITE_DELAY", "70"))
    FLOOD_WAIT_MULTIPLIER: float = float(os.getenv("FLOOD_WAIT_MULTIPLIER", "1.5"))
    MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "3"))
    HEALTH_CHECK_INTERVAL: int = int(os.getenv("HEALTH_CHECK_INTERVAL", "300"))
    MAX_ACCOUNTS_PER_TASK: int = int(os.getenv("MAX_ACCOUNTS_PER_TASK", "3"))
    SESSION_TIMEOUT: int = int(os.getenv("SESSION_TIMEOUT", "1800"))  # 30 min

    # Worker settings
    MIN_WORKERS: int = int(os.getenv("MIN_WORKERS", "2"))
    MAX_WORKERS: int = int(os.getenv("MAX_WORKERS", "10"))
    WORKER_CHECK_INTERVAL: int = int(os.getenv("WORKER_CHECK_INTERVAL", "10"))
    WORKER_IDLE_TIMEOUT: int = int(os.getenv("WORKER_IDLE_TIMEOUT", "300"))

    # Worm settings
    WORM_BATCH_SIZE: int = int(os.getenv("WORM_BATCH_SIZE", "50"))
    WORM_CHECK_INTERVAL: int = int(os.getenv("WORM_CHECK_INTERVAL", "10"))
    WORM_MIN_DELAY: int = int(os.getenv("WORM_MIN_DELAY", "3"))
    WORM_MAX_DELAY: int = int(os.getenv("WORM_MAX_DELAY", "7"))
    WORM_MESSAGE_LIMIT: int = int(os.getenv("WORM_MESSAGE_LIMIT", "0"))
    WORM_MAX_CONCURRENT: int = int(os.getenv("WORM_MAX_CONCURRENT", "5"))
    WORM_SCAN_LIMIT: int = int(
        os.getenv("WORM_SCAN_LIMIT", "1000")
    )  # Лимит сообщений на чат

    # Validator settings
    VALIDATOR_TEST_DELETE_WAIT: int = int(os.getenv("VALIDATOR_TEST_DELETE_WAIT", "5"))
    VALIDATOR_RECONNECT_TIMEOUT: int = int(
        os.getenv("VALIDATOR_RECONNECT_TIMEOUT", "30")
    )

    # Mailing settings
    MAILING_BATCH_SIZE: int = int(os.getenv("MAILING_BATCH_SIZE", "10"))
    MAILING_CHECK_INTERVAL: int = int(os.getenv("MAILING_CHECK_INTERVAL", "10"))
    MAILING_MIN_DELAY: int = int(os.getenv("MAILING_MIN_DELAY", "45"))
    MAILING_MAX_DELAY: int = int(os.getenv("MAILING_MAX_DELAY", "80"))

    @classmethod
    def validate(cls) -> None:
        errors: List[str] = []
        if not cls.BOT_TOKEN:
            errors.append("BOT_TOKEN не установлен")
        if cls.API_ID == 0:
            errors.append("API_ID не установлен")
        if not cls.API_HASH:
            errors.append("API_HASH не установлен")
        if errors:
            raise ConfigError(
                "Ошибка конфигурации:\n" + "\n".join(f"  • {e}" for e in errors)
            )


for path in [Config.SESSIONS_DIR, Config.DATA_DIR, LOGS_DIR]:
    try:
        os.makedirs(path, exist_ok=True)
    except Exception as e:
        logger.warning(f"Не удалось создать папку {path}: {e}")


# === Кастомные исключения ===
class BotError(Exception):

    def __init__(self, message: str, code: int = 500):
        self.message = message
        self.code = code
        super().__init__(self.message)


class ConfigError(BotError):
    def __init__(self, message: str):
        super().__init__(message, code=400)


class DatabaseError(BotError):
    def __init__(self, message: str, original_error: Optional[Exception] = None):
        self.original_error = original_error
        super().__init__(message, code=500)


class ValidationError(BotError):
    def __init__(self, message: str, field: Optional[str] = None):
        self.field = field
        super().__init__(message, code=400)


# === Утилиты логирования ===
def log_error(func: Callable) -> Callable:
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Ошибка в {func.__name__}: {e}", exc_info=True)
            raise

    return wrapper


def log_warning(func: Callable) -> Callable:
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            logger.warning(f"Предупреждение в {func.__name__}: {e}")
            raise

    return wrapper


# === Утилиты для работы с событиями ===
async def safe_send_message(
    bot: Bot,
    user_id: int,
    message: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> bool:
    if not bot.session:
        logger.error(
            f"safe_send_message: сессия бота не инициализирована для user {user_id}"
        )
        return False
    try:
        await bot.send_message(
            user_id, message, parse_mode=ParseMode.HTML, reply_markup=reply_markup
        )
        return True
    except TelegramBadRequest as e:
        error_msg = str(e).lower()
        if "blocked" in error_msg or "bot was blocked" in error_msg:
            return False
        try:
            await bot.send_message(
                user_id,
                html.escape(message),
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
            return True
        except Exception:
            try:
                await bot.send_message(user_id, message, reply_markup=reply_markup)
                return True
            except Exception as e2:
                logger.error(f"Ошибка отправки сообщения {user_id}: {e2}")
                return False
    except Exception as e:
        logger.error(f"Ошибка отправки {user_id}: {e}")
        return False


# === Клавиатуры ===
def kb(rows: List[List[Dict[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(**btn) for btn in row] for row in rows]
    )


# === Retry паттерн ===
async def retry_async(
    func: Callable,
    max_retries: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: Tuple[type, ...] = (Exception,),
) -> Any:
    last_exception: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            return await func()
        except exceptions as e:
            last_exception = e
            if attempt < max_retries - 1:
                wait_time = delay * (backoff**attempt)
                logger.warning(
                    f"Повторная попытка {attempt + 1}/{max_retries} "
                    f"через {wait_time:.1f}с: {type(e).__name__}: {e}"
                )
                await asyncio.sleep(wait_time)
            else:
                logger.error(
                    f"Все {max_retries} попытки исчерпаны: {type(e).__name__}: {e}",
                    exc_info=True,
                )
    if last_exception:
        raise last_exception
    raise BotError("Неизвестная ошибка в retry_async")


# === Утилиты-конвертеры ===
def to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        try:
            return int(float(value))
        except Exception:
            return default


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def format_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


async def notify_admins(
    bot: Bot, message: str, reply_markup: Optional[InlineKeyboardMarkup] = None
) -> None:
    for admin_id in Config.ADMIN_USER_IDS:
        await safe_send_message(bot, admin_id, message, reply_markup=reply_markup)


async def notify_user(
    bot: Bot,
    user_id: int,
    message: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    await safe_send_message(bot, user_id, message, reply_markup=reply_markup)


async def update_message(
    bot: Bot, chat_id: int, message_id: int, text: str, reply_markup=None
) -> bool:
    """Авто-обновление сообщения без создания нового"""
    try:
        await bot.edit_message_text(
            text,
            chat_id,
            message_id,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
        )
        return True
    except TelegramBadRequest as e:
        error_msg = str(e).lower()
        if "message is not modified" in error_msg:
            return True
        return False
    except Exception as e:
        logger.debug(f"Error updating message: {e}")
        return False


async def smart_answer(
    event: Union[Message, CallbackQuery],
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    delete_origin: bool = False,
) -> bool:
    try:
        if isinstance(event, Message):
            await event.answer(text, reply_markup=reply_markup)
        elif isinstance(event, CallbackQuery):
            if event.message:
                await event.message.answer(text, reply_markup=reply_markup)
                if delete_origin:
                    try:
                        await event.message.delete()
                    except Exception:
                        pass
            try:
                await event.answer()
            except Exception:
                pass
        return True
    except Exception as e:
        logger.error(f"smart_answer error: {e}")
        return False


def create_telegram_client(session_string: Optional[str] = None) -> TelegramClient:
    session = StringSession(session_string) if session_string else StringSession()
    return TelegramClient(
        session,
        Config.API_ID,
        Config.API_HASH,
        device_model="Inviter",
        system_version="Linux",
        app_version="3.1",
        system_lang_code="en",
        lang_code="en",
        catch_up=False,
    )


def format_progress_bar(percentage: float, length: int = 15) -> str:
    """Создает красивый прогресс-бар с эмодзи"""
    filled = int(length * percentage / 100)
    bar = "🟩" * filled + "⬜" * (length - filled)
    return f"{bar} {percentage:.1f}%"


# --- Entity Cache ---
entity_cache: Dict[int, Any] = {}
cache_lock = asyncio.Lock()


async def get_cached_entity(client: TelegramClient, user_id: int) -> Optional[Any]:
    """Кэширование сущностей для ускорения работы"""
    async with cache_lock:
        if user_id in entity_cache:
            return entity_cache[user_id]

    try:
        entity = await client.get_entity(user_id)
        async with cache_lock:
            entity_cache[user_id] = entity
        return entity
    except Exception:
        return None


async def clear_entity_cache():
    """Очистка кэша сущностей"""
    async with cache_lock:
        count = len(entity_cache)
        entity_cache.clear()
    logger.info(f"Entity cache cleared ({count} entries)")


# --- JSON Storage ---
# === TasksDB (SQLite) ===
class TasksDB:
    """База данных задач на SQLite (замена JSONStorage)"""

    def __init__(self, db_path: str):
        self.db_path: str = db_path
        self.conn: Optional[aiosqlite.Connection] = None
        self.lock: asyncio.Lock = asyncio.Lock()

    async def connect(self) -> None:
        try:
            parent = os.path.dirname(self.db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)

            async def _connect() -> aiosqlite.Connection:
                conn = await aiosqlite.connect(self.db_path, timeout=30.0)
                return conn

            self.conn = await retry_async(
                _connect,
                max_retries=3,
                delay=1.0,
                exceptions=(aiosqlite.Error, Exception),
            )
            self.conn.row_factory = aiosqlite.Row
            await self.conn.execute("PRAGMA foreign_keys = ON")
            await self.conn.execute("PRAGMA journal_mode = WAL")
            await self.conn.execute("PRAGMA busy_timeout = 5000")
            await self.init_db()
            logger.info(f"База данных задач подключена: {self.db_path}")
        except Exception as e:
            logger.critical(f"Не удалось подключиться к БД задач: {e}")
            raise DatabaseError(f"Ошибка подключения к БД задач: {e}") from e

    async def close(self) -> None:
        if self.conn:
            try:
                await self.conn.close()
                logger.info("База данных задач закрыта")
            except Exception as e:
                logger.warning(f"Ошибка закрытия БД задач: {e}")
            finally:
                self.conn = None

    async def init_db(self) -> None:
        if not self.conn:
            return
        async with self.lock:
            try:
                await self.conn.execute("""
                    CREATE TABLE IF NOT EXISTS tasks (
                        task_id TEXT PRIMARY KEY,
                        type TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        user_id INTEGER NOT NULL,
                        data TEXT NOT NULL DEFAULT '{}',
                        results TEXT DEFAULT 'null',
                        error TEXT DEFAULT '',
                        progress REAL DEFAULT 0.0,
                        progress_text TEXT DEFAULT '',
                        sent INTEGER DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        completed_at TIMESTAMP,
                        checkpoints TEXT DEFAULT 'null'
                    )
                """)
                await self.conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tasks_user_id ON tasks(user_id)"
                )
                await self.conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)"
                )
                await self.conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at)"
                )
                await self.conn.commit()
                logger.info("Таблица tasks создана/проверена")
            except Exception as e:
                logger.error(f"Ошибка инициализации БД задач: {e}")
                raise DatabaseError(f"Ошибка init_db задач: {e}") from e

    @log_error
    async def add_task(self, task_data: Dict[str, Any]) -> bool:
        if not self.conn:
            return False
        try:
            async with self.lock:
                await self.conn.execute(
                    "INSERT OR IGNORE INTO tasks (task_id, type, status, user_id, data) VALUES (?, ?, ?, ?, ?)",
                    (
                        task_data.get("task_id", ""),
                        task_data.get("type", ""),
                        task_data.get("status", "pending"),
                        task_data.get("user_id", 0),
                        json.dumps(task_data.get("data", {}), ensure_ascii=False),
                    ),
                )
                await self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"add_task: {e}")
            return False

    @log_error
    async def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        if not self.conn:
            return None
        try:
            async with self.lock:
                cur = await self.conn.execute(
                    "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
                )
                row = await cur.fetchone()
                if row:
                    data = dict(row)
                    # Парсим JSON поля
                    if data.get("data"):
                        try:
                            data["data"] = json.loads(data["data"])
                        except Exception:
                            data["data"] = {}
                    if data.get("results"):
                        try:
                            data["results"] = json.loads(data["results"])
                        except Exception:
                            data["results"] = None
                    if data.get("checkpoints"):
                        try:
                            data["checkpoints"] = json.loads(data["checkpoints"])
                        except Exception:
                            data["checkpoints"] = None
                    return data
                return None
        except Exception as e:
            logger.error(f"get_task {task_id}: {e}")
            return None

    @log_error
    async def update_task(self, task_id: str, updates: Dict[str, Any]) -> bool:
        if not self.conn or not updates:
            return False
        try:
            async with self.lock:
                set_clause = []
                values = []
                for key, value in updates.items():
                    if key in ("data", "results", "checkpoints") and isinstance(
                        value, (dict, list)
                    ):
                        set_clause.append(f"{key} = ?")
                        values.append(json.dumps(value, ensure_ascii=False))
                    else:
                        set_clause.append(f"{key} = ?")
                        values.append(value)
                values.append(task_id)
                set_clause.append("task_id = ?")
                await self.conn.execute(
                    f"UPDATE tasks SET {', '.join(set_clause)} WHERE task_id = ?",
                    values,
                )
                await self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"update_task {task_id}: {e}")
            return False

    @log_error
    async def get_tasks_by_user(self, user_id: int) -> List[Dict[str, Any]]:
        if not self.conn:
            return []
        try:
            async with self.lock:
                cur = await self.conn.execute(
                    "SELECT * FROM tasks WHERE user_id = ? ORDER BY created_at DESC",
                    (user_id,),
                )
                rows = await cur.fetchall()
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"get_tasks_by_user {user_id}: {e}")
            return []

    @log_error
    async def get_active_tasks(self) -> List[Dict[str, Any]]:
        if not self.conn:
            return []
        try:
            async with self.lock:
                cur = await self.conn.execute(
                    "SELECT * FROM tasks WHERE status IN ('pending', 'running', 'paused') ORDER BY created_at DESC"
                )
                rows = await cur.fetchall()
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"get_active_tasks: {e}")
            return []

    @log_error
    async def cancel_task(self, task_id: str) -> bool:
        if not self.conn:
            return False
        try:
            async with self.lock:
                await self.conn.execute(
                    "UPDATE tasks SET status = 'cancelled', completed_at = ? WHERE task_id = ?",
                    (datetime.now().isoformat(), task_id),
                )
                await self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"cancel_task {task_id}: {e}")
            return False

    @log_error
    async def cancel_all_user_tasks(self, user_id: int) -> int:
        if not self.conn:
            return 0
        try:
            async with self.lock:
                cur = await self.conn.execute(
                    "UPDATE tasks SET status = 'cancelled', completed_at = ? WHERE user_id = ? AND status IN ('pending', 'running', 'paused')",
                    (datetime.now().isoformat(), user_id),
                )
                await self.conn.commit()
                return cur.rowcount
        except Exception as e:
            logger.error(f"cancel_all_user_tasks {user_id}: {e}")
            return 0

    @log_error
    async def delete_task(self, task_id: str) -> bool:
        if not self.conn:
            return False
        try:
            async with self.lock:
                cur = await self.conn.execute(
                    "DELETE FROM tasks WHERE task_id = ?", (task_id,)
                )
                await self.conn.commit()
                return cur.rowcount > 0
        except Exception as e:
            logger.error(f"delete_task {task_id}: {e}")
            return False

    # === Методы-алиасы для совместимости с JSONStorage ===
    async def add(self, item: Dict[str, Any]) -> bool:
        """Алиас для add_task"""
        return await self.add_task(item)

    async def update_by_id(
        self, item_id: str, updates: Dict[str, Any], id_field: str = "id"
    ) -> bool:
        """Алиас для update_task"""
        return await self.update_task(item_id, updates)

    async def find_by_id(
        self, item_id: str, id_field: str = "id"
    ) -> Optional[Dict[str, Any]]:
        """Алиас для get_task"""
        return await self.get_task(item_id)

    async def read_all(self) -> List[Dict[str, Any]]:
        """Алиас для получения всех задач"""
        if not self.conn:
            return []
        try:
            async with self.lock:
                cur = await self.conn.execute(
                    "SELECT * FROM tasks ORDER BY created_at DESC"
                )
                rows = await cur.fetchall()
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"read_all: {e}")
            return []

    async def write_all(self, data: List[Dict[str, Any]]) -> None:
        """Алиас для полной перезаписи задач (не рекомендуется для производительности)"""
        if not self.conn:
            return
        try:
            async with self.lock:
                # Удаляем все задачи
                await self.conn.execute("DELETE FROM tasks")
                # Добавляем новые
                for task in data:
                    task_data = task.get("data", {})
                    if isinstance(task_data, str):
                        task_data = json.loads(task_data) if task_data else {}
                    await self.conn.execute(
                        "INSERT OR REPLACE INTO tasks (task_id, type, status, user_id, data, results, error, progress, progress_text, sent, created_at, completed_at, checkpoints) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            task.get("task_id", ""),
                            task.get("type", ""),
                            task.get("status", "pending"),
                            task.get("user_id", 0),
                            json.dumps(task.get("data", {}), ensure_ascii=False),
                            json.dumps(task.get("results"), ensure_ascii=False),
                            task.get("error", ""),
                            task.get("progress", 0.0),
                            task.get("progress_text", ""),
                            task.get("sent", 0),
                            task.get("created_at", datetime.now().isoformat()),
                            task.get("completed_at"),
                            json.dumps(task.get("checkpoints"), ensure_ascii=False),
                        ),
                    )
                await self.conn.commit()
        except Exception as e:
            logger.error(f"write_all: {e}")


# --- JSONStorage (legacy) ---
class JSONStorage:
    def __init__(self, path: str):
        self.path = path
        self._lock = asyncio.Lock()
        self._backup_path = f"{path}.bak"
        self._max_retries = 3
        self._retry_delay = 0.5

    async def _ensure_file(self) -> None:
        if os.path.exists(self.path):
            return
        try:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        except Exception as e:
            logger.warning(f"Не удалось создать папку для {self.path}: {e}")
        async with aiofiles.open(self.path, "w", encoding="utf-8") as f:
            await f.write("[]")

    async def _backup_file(self) -> None:
        """Создаёт резервную копию файла"""
        try:
            if os.path.exists(self.path):
                async with aiofiles.open(self.path, "r", encoding="utf-8") as f:
                    content = await f.read()
                async with aiofiles.open(self._backup_path, "w", encoding="utf-8") as f:
                    await f.write(content)
                logger.debug(f"Backup created for {self.path}")
        except Exception as e:
            logger.warning(f"Failed to create backup for {self.path}: {e}")

    async def _safe_write(self, content: str) -> bool:
        """Безопасная запись с retry и atomic write"""
        for attempt in range(self._max_retries):
            try:
                temp_path = f"{self.path}.tmp"
                async with aiofiles.open(temp_path, "w", encoding="utf-8") as f:
                    await f.write(content)
                await asyncio.to_thread(os.replace, temp_path, self.path)
                return True
            except Exception as e:
                logger.warning(
                    f"Write attempt {attempt + 1} failed for {self.path}: {e}"
                )
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(self._retry_delay * (2**attempt))
                else:
                    try:
                        async with aiofiles.open(self.path, "w", encoding="utf-8") as f:
                            await f.write(content)
                        logger.warning(f"Fallback write succeeded for {self.path}")
                        return True
                    except Exception as e2:
                        logger.error(f"All write attempts failed for {self.path}: {e2}")
                        return False
        return False

    async def read_all(self) -> List[Dict[str, Any]]:
        await self._ensure_file()
        async with self._lock:
            for attempt in range(self._max_retries):
                try:
                    async with aiofiles.open(self.path, "r", encoding="utf-8") as f:
                        content = await f.read()
                    if not content:
                        return []
                    data = json.loads(content)
                    return data if isinstance(data, list) else []
                except json.JSONDecodeError:
                    logger.warning(
                        f"JSON decode error for {self.path}, attempting recovery"
                    )
                    if os.path.exists(self._backup_path):
                        async with aiofiles.open(
                            self._backup_path, "r", encoding="utf-8"
                        ) as f:
                            content = await f.read()
                        data = json.loads(content)
                        return data if isinstance(data, list) else []
                    return []
                except Exception as e:
                    if attempt < self._max_retries - 1:
                        await asyncio.sleep(self._retry_delay)
                    else:
                        logger.error(f"Read error for {self.path}: {e}")
                        return []
        return []

    async def write_all(self, data: List[Dict[str, Any]]) -> None:
        await self._ensure_file()
        async with self._lock:
            await self._backup_file()
            content = json.dumps(data, ensure_ascii=False, indent=2)
            await self._safe_write(content)

    async def add(self, item: Dict[str, Any]) -> None:
        data = await self.read_all()
        data.append(item)
        await self.write_all(data)

    async def remove(self, predicate) -> None:
        data = await self.read_all()
        new_data = [x for x in data if not predicate(x)]
        await self.write_all(new_data)

    async def find_by_id(
        self, item_id: str, id_field: str = "id"
    ) -> Optional[Dict[str, Any]]:
        data = await self.read_all()
        for item in data:
            if item.get(id_field) == item_id:
                return item
        return None

    async def remove_by_id(self, item_id: str, id_field: str = "id") -> None:
        await self.remove(lambda x: x.get(id_field) == item_id)

    async def update_by_id(
        self, item_id: str, updates: Dict[str, Any], id_field: str = "id"
    ) -> bool:
        data = await self.read_all()
        for item in data:
            if item.get(id_field) == item_id:
                item.update(updates)
                await self.write_all(data)
                return True
        return False


# --- AuthManager ---
class AuthManager:
    def __init__(self, auth_file: str):
        self.auth_file = auth_file
        self.auth_data = {"keys": {}, "authorized": []}
        self._lock = asyncio.Lock()

    async def _load_auth_data(self) -> Dict[str, Any]:
        try:
            if os.path.exists(self.auth_file):
                async with aiofiles.open(self.auth_file, "r", encoding="utf-8") as f:
                    content = await f.read()
                    if content:
                        return json.loads(content)
        except Exception as e:
            logger.error(f"Error loading auth data: {e}")
        return {"keys": {}, "authorized": []}

    async def _save_auth_data(self):
        try:
            async with aiofiles.open(self.auth_file, "w", encoding="utf-8") as f:
                await f.write(json.dumps(self.auth_data, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.error(f"Error saving auth data: {e}")

    async def load(self):
        async with self._lock:
            self.auth_data = await self._load_auth_data()
            logger.info(
                f"Loaded auth data: {len(self.auth_data.get('authorized', []))} authorized users, {len(self.auth_data.get('keys', {}))} keys"
            )

    async def save(self):
        async with self._lock:
            await self._save_auth_data()

    async def generate_key(self, user_id: int) -> str:
        async with self._lock:
            key = secrets.token_urlsafe(32)
            self.auth_data.setdefault("keys", {})[str(user_id)] = key
            await self._save_auth_data()
            return key

    async def get_key_for_user(self, user_id: int) -> Optional[str]:
        async with self._lock:
            return self.auth_data.get("keys", {}).get(str(user_id))

    async def verify_key(self, user_id: int, key: str) -> bool:
        async with self._lock:
            stored_key = self.auth_data.get("keys", {}).get(str(user_id))
            if stored_key and stored_key == key:
                if str(user_id) not in self.auth_data.get("authorized", []):
                    self.auth_data.setdefault("authorized", []).append(str(user_id))
                await self._save_auth_data()
                return True
        return False

    async def is_authorized(self, user_id: int) -> bool:
        async with self._lock:
            return (
                str(user_id) in self.auth_data.get("authorized", [])
                or user_id in Config.ADMIN_USER_IDS
            )

    async def add_authorized_user(self, user_id: int):
        async with self._lock:
            if str(user_id) not in self.auth_data.get("authorized", []):
                self.auth_data.setdefault("authorized", []).append(str(user_id))
                await self._save_auth_data()


# --- CacheManager ---
class CacheManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn: Optional[aiosqlite.Connection] = None
        self.lock = asyncio.Lock()

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await self.init_db()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def init_db(self) -> None:
        if not self.conn:
            return
        async with self.lock:
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_participants (
                    chat_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, user_id)
                )
                """)
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS invited_users (
                    chat_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    invited_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    task_id TEXT,
                    PRIMARY KEY (chat_id, user_id)
                )
                """)
            await self.conn.commit()

    async def cache_participants(self, chat_id: str, user_ids: List[int]) -> None:
        if not self.conn:
            return
        async with self.lock:
            await self.conn.execute(
                "DELETE FROM chat_participants WHERE chat_id = ?", (chat_id,)
            )

            for user_id in user_ids:
                await self.conn.execute(
                    "INSERT OR IGNORE INTO chat_participants (chat_id, user_id) VALUES (?, ?)",
                    (chat_id, user_id),
                )
            await self.conn.commit()

    async def get_cached_participants(self, chat_id: str) -> List[int]:
        if not self.conn:
            return []
        async with self.lock:
            async with self.conn.execute(
                "SELECT user_id FROM chat_participants WHERE chat_id = ?", (chat_id,)
            ) as cursor:
                rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def clear_cache(self, chat_id: Optional[str] = None) -> int:
        if not self.conn:
            return 0
        async with self.lock:
            if chat_id:
                cursor = await self.conn.execute(
                    "DELETE FROM chat_participants WHERE chat_id = ?", (chat_id,)
                )
            else:
                cursor = await self.conn.execute("DELETE FROM chat_participants")
            await self.conn.commit()
            return cursor.rowcount

    async def mark_invited(
        self, chat_id: str, user_id: int, task_id: Optional[str] = None
    ) -> None:
        if not self.conn:
            return
        async with self.lock:
            await self.conn.execute(
                "INSERT OR IGNORE INTO invited_users (chat_id, user_id, task_id) VALUES (?, ?, ?)",
                (chat_id, user_id, task_id),
            )
            await self.conn.commit()

    async def is_invited(self, chat_id: str, user_id: int) -> bool:
        if not self.conn:
            return False
        async with self.lock:
            async with self.conn.execute(
                "SELECT 1 FROM invited_users WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            ) as cursor:
                row = await cursor.fetchone()
        return row is not None


# --- ChatDB (БД собранных чатов) ---
class ChatDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn: Optional[aiosqlite.Connection] = None
        self.lock = asyncio.Lock()

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.db_path, timeout=30.0)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode = WAL")
        await self.conn.execute("PRAGMA busy_timeout = 5000")
        await self.init_db()
        logger.info(f"ChatDB подключена: {self.db_path}")

    async def close(self) -> None:
        if self.conn:
            try:
                await self.conn.close()
                logger.info("ChatDB закрыта")
            except Exception as e:
                logger.warning(f"Ошибка закрытия ChatDB: {e}")
            finally:
                self.conn = None

    async def init_db(self) -> None:
        if not self.conn:
            return
        async with self.lock:
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS collected_chats (
                    chat_id TEXT PRIMARY KEY,
                    chat_name TEXT DEFAULT '',
                    chat_url TEXT DEFAULT '',
                    chat_type TEXT DEFAULT 'unknown',
                    user_count INTEGER DEFAULT 0,
                    verified INTEGER DEFAULT 0,
                    last_check TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'active'
                )
            """)
            await self.conn.commit()

    async def add_chat(
        self,
        chat_id: str,
        chat_name: str,
        chat_url: str,
        chat_type: str,
        user_count: int,
    ) -> bool:
        if not self.conn:
            return False
        try:
            async with self.lock:
                await self.conn.execute(
                    """INSERT OR REPLACE INTO collected_chats
                    (chat_id, chat_name, chat_url, chat_type, user_count, verified, last_check, status)
                    VALUES (?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP, 'active')""",
                    (
                        str(chat_id),
                        chat_name,
                        chat_url,
                        chat_type,
                        user_count,
                    ),
                )
                await self.conn.commit()
            logger.info(f"✅ Чат {chat_name} ({chat_id}) добавлен в ChatDB")
            return True
        except Exception as e:
            logger.error(f"Ошибка добавления чата {chat_id} в ChatDB: {e}")
            return False

    async def update_chat(self, chat_id: str, data: Dict[str, Any]) -> bool:
        if not self.conn:
            return False
        try:
            async with self.lock:
                fields = []
                values = []
                for key, value in data.items():
                    fields.append(f"{key} = ?")
                    values.append(value)
                values.append(str(chat_id))
                query = (
                    f"UPDATE collected_chats SET {', '.join(fields)} WHERE chat_id = ?"
                )
                await self.conn.execute(query, tuple(values))
                await self.conn.commit()
            logger.info(f"✅ Чат {chat_id} обновлён в ChatDB")
            return True
        except Exception as e:
            logger.error(f"Ошибка обновления чата {chat_id} в ChatDB: {e}")
            return False

    async def remove_chat(self, chat_id: str) -> bool:
        if not self.conn:
            return False
        try:
            async with self.lock:
                cur = await self.conn.execute(
                    "DELETE FROM collected_chats WHERE chat_id = ?", (str(chat_id),)
                )
                await self.conn.commit()
                removed = cur.rowcount > 0
            if removed:
                logger.info(f"🗑️ Чат {chat_id} удалён из ChatDB")
            return removed
        except Exception as e:
            logger.error(f"Ошибка удаления чата {chat_id} из ChatDB: {e}")
            return False

    async def update_chat_status(self, chat_id: str, status: str) -> bool:
        if not self.conn:
            return False
        try:
            async with self.lock:
                await self.conn.execute(
                    "UPDATE collected_chats SET status = ?, last_check = CURRENT_TIMESTAMP WHERE chat_id = ?",
                    (status, str(chat_id)),
                )
                await self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"Ошибка обновления статуса чата {chat_id}: {e}")
            return False

    async def get_all_chats(self) -> List[Dict[str, Any]]:
        if not self.conn:
            return []
        try:
            async with self.lock:
                cur = await self.conn.execute(
                    "SELECT * FROM collected_chats WHERE status = 'active'"
                )
                rows = await cur.fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Ошибка получения чатов из ChatDB: {e}")
            return []

    async def get_chat(self, chat_id: str) -> Optional[Dict[str, Any]]:
        if not self.conn:
            return None
        try:
            async with self.lock:
                cur = await self.conn.execute(
                    "SELECT * FROM collected_chats WHERE chat_id = ?", (str(chat_id),)
                )
                row = await cur.fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"Ошибка получения чата {chat_id} из ChatDB: {e}")
            return None

    async def get_total_users(self) -> int:
        if not self.conn:
            return 0
        try:
            async with self.lock:
                cur = await self.conn.execute(
                    "SELECT COALESCE(SUM(user_count), 0) as total FROM collected_chats WHERE status = 'active'"
                )
                row = await cur.fetchone()
            return int(row["total"]) if row else 0
        except Exception as e:
            logger.error(f"Ошибка подсчёта пользователей ChatDB: {e}")
            return 0

    async def get_active_chats_count(self) -> int:
        if not self.conn:
            return 0
        try:
            async with self.lock:
                cur = await self.conn.execute(
                    "SELECT COUNT(*) as count FROM collected_chats WHERE status = 'active'"
                )
                row = await cur.fetchone()
            return int(row["count"]) if row else 0
        except Exception as e:
            logger.error(f"Ошибка подсчёта чатов ChatDB: {e}")
            return 0

    async def clear_db(self) -> int:
        if not self.conn:
            return 0
        try:
            async with self.lock:
                cur = await self.conn.execute("DELETE FROM collected_chats")
                await self.conn.commit()
                return cur.rowcount
        except Exception as e:
            logger.error(f"Ошибка очистки ChatDB: {e}")
            return 0


# --- UserDB (БД пользователей и их настроек) ---
class UserDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn: Optional[aiosqlite.Connection] = None
        self.lock = asyncio.Lock()

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.db_path, timeout=30.0)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode = WAL")
        await self.conn.execute("PRAGMA busy_timeout = 5000")
        await self.init_db()
        logger.info(f"UserDB подключена: {self.db_path}")

    async def close(self) -> None:
        if self.conn:
            try:
                await self.conn.close()
                logger.info("UserDB закрыта")
            except Exception as e:
                logger.warning(f"Ошибка закрытия UserDB: {e}")
            finally:
                self.conn = None

    async def init_db(self) -> None:
        if not self.conn:
            return
        async with self.lock:
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    language TEXT DEFAULT 'ru'
                )
            """)
            await self.conn.commit()

    async def add_user(self, user_id: int) -> bool:
        if not self.conn:
            return False
        try:
            async with self.lock:
                await self.conn.execute(
                    "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
                    (user_id,),
                )
                await self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"add_user {user_id}: {e}")
            return False

    async def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        if not self.conn:
            return None
        try:
            async with self.lock:
                cur = await self.conn.execute(
                    "SELECT * FROM users WHERE user_id = ?", (user_id,)
                )
                row = await cur.fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"get_user {user_id}: {e}")
            return None

    async def update_user(self, user_id: int, **kwargs) -> bool:
        if not self.conn or not kwargs:
            return False
        allowed = {"language"}
        values_by_column = {k: v for k, v in kwargs.items() if k in allowed}
        if not values_by_column:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in values_by_column)
        values = list(values_by_column.values()) + [user_id]
        try:
            async with self.lock:
                await self.conn.execute(
                    f"UPDATE users SET {set_clause} WHERE user_id = ?",
                    tuple(values),
                )
                await self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"update_user {user_id}: {e}")
            return False

    async def get_user_language(self, user_id: int) -> str:
        if not self.conn:
            return DEFAULT_LANGUAGE
        try:
            user = await self.get_user(user_id)
            lang = str(user.get("language", "") if user else "").strip().lower()
            available = get_available_languages()
            return lang if lang in available else DEFAULT_LANGUAGE
        except Exception as e:
            logger.error(f"get_user_language {user_id}: {e}")
            return DEFAULT_LANGUAGE

    async def set_user_language(self, user_id: int, language: str) -> bool:
        if not self.conn:
            return False
        await self.add_user(user_id)
        return await self.update_user(user_id, language=language)


# === Regex для извлечения ссылок Telegram ===
_TELEGRAM_LINK_RE = re.compile(
    r"(?:https?://)?"  # протокол (опционально)
    r"(?:www\.)?"  # www (опционально)
    r"(?:t\.me/|telegram\.me/|telegram\.dog/)"  # домен
    r"([A-Za-z0-9_+/=-]{2,32})"  # юзернейм/хеш
    r"(?:\?|/|$|[^A-Za-z0-9_])",  # конец
    re.IGNORECASE,
)

_TELEGRAM_SHORT_RE = re.compile(
    r"t\.me/([A-Za-z0-9_+/=-]{2,32})",
    re.IGNORECASE,
)

_TELEGRAM_USERNAME_RE = re.compile(
    r"@([A-Za-z0-9_]{2,32})\b",
    re.IGNORECASE,
)


def extract_telegram_links(text: str) -> List[str]:
    """Извлекает все t.me ссылки из текста"""
    if not text:
        return []
    links: List[str] = []
    seen: Set[str] = set()

    # Полные ссылки
    for match in _TELEGRAM_LINK_RE.finditer(text):
        link = f"https://t.me/{match.group(1)}"
        if link not in seen:
            seen.add(link)
            links.append(link)

    # Короткие ссылки
    for match in _TELEGRAM_SHORT_RE.finditer(text):
        link = f"https://t.me/{match.group(1)}"
        if link not in seen:
            seen.add(link)
            links.append(link)

    # @username
    for match in _TELEGRAM_USERNAME_RE.finditer(text):
        username = match.group(1)
        # Пропускаем служебные слова
        if username.lower() in ("all", "channel", "bot", "support", "help", "admin"):
            continue
        link = f"https://t.me/{username}"
        if link not in seen:
            seen.add(link)
            links.append(link)

    return links


def extract_links_from_text(text: str) -> List[str]:
    """Универсальный парсинг ссылок из текста (включая вложенные)"""
    if not text:
        return []
    return extract_telegram_links(text)


def parse_link_to_identifier(link: str) -> Optional[str]:
    """Парсит ссылку и возвращает идентификатор чата (username или хеш)"""
    if not link:
        return None
    link = link.strip()
    # https://t.me/username
    # https://t.me/+hash
    # t.me/username
    for pattern in [
        r"https?://t\.me/[+]?([A-Za-z0-9_+/=-]{2,32})",
        r"t\.me/[+]?([A-Za-z0-9_+/=-]{2,32})",
    ]:
        m = re.search(pattern, link, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


async def validate_and_test_chat(
    client: TelegramClient,
    account: Dict[str, Any],
    chat_identifier: str,
    update_existing: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Проверяет чат (только группы!):
    1. Определяет тип (user/chat/channel)
    2. Если channel или user — пропускает
    3. Проверяет забанен ли бот
    4. Проверяет может ли писать
    5. Отправляет тестовое сообщение
    6. Ждёт Config.VALIDATOR_TEST_DELETE_WAIT сек
    7. Если не удалено — удаляет сам
    8. Записывает в БД только если всё ок
    9. Если update_existing=True — обновляет существующий чат
    10. Если бот забанен или не может писать — удаляет из БД
    """
    try:
        entity = await client.get_entity(chat_identifier)
    except Exception as e:
        logger.warning(f"Не удалось получить сущность {chat_identifier}: {e}")
        return None

    # 1. Определяем тип
    is_user = isinstance(entity, telethon_types.User)
    is_channel = isinstance(entity, telethon_types.Channel)
    is_chat = isinstance(entity, telethon_types.Chat)

    if is_user:
        logger.info(f"Пропуск пользователя: {chat_identifier}")
        # Удаляем из БД если был
        if update_existing:
            await chat_db.remove_chat(str(entity.id))
        return None

    if is_channel:
        logger.info(f"Пропуск канала: {chat_identifier}")
        # Удаляем из БД если был
        if update_existing:
            await chat_db.remove_chat(str(entity.id))
        return None

    if not is_chat:
        logger.info(f"Пропуск неизвестного типа: {chat_identifier}")
        return None

    chat_id = str(entity.id)
    chat_name = str(
        entity.title
        or getattr(entity, "first_name", chat_identifier)
        or chat_identifier
    )
    chat_type = "group"  # Только группы

    # Получаем username/ссылку
    chat_username = getattr(entity, "username", None)
    if chat_username:
        chat_url = f"https://t.me/{chat_username}"
    else:
        chat_url = f"https://t.me/{chat_identifier}"

    # Получаем количество пользователей
    user_count = entity.users_count or 0

    # 2. Проверяем, забанен ли бот в этом чате
    is_banned = False
    try:
        # Проверяем, может ли бот получить информацию о чате
        await client.get_entity(chat_identifier)
    except Exception as e:
        logger.warning(f"Бот может быть забанен в {chat_name}: {e}")
        is_banned = True

    if is_banned:
        logger.info(f"Бот забанен в чате {chat_name} — удаляем из БД")
        if update_existing:
            await chat_db.remove_chat(chat_id)
        return None

    # 3. Проверяем, может ли бот писать
    test_msg_id = None
    msg_deleted = False

    try:
        test_text = "🐛 Test message from Inviter Bot"
        sent_msg = await client.send_message(entity, test_text)
        test_msg_id = sent_msg.id
        logger.info(f"Тестовое сообщение отправлено в {chat_name} ({chat_id})")

        # 4. Ждём указанное время из .env
        await asyncio.sleep(Config.VALIDATOR_TEST_DELETE_WAIT)

        # 5. Проверяем, не удалено ли сообщение
        try:
            updated = await client.get_messages(entity, ids=test_msg_id)
            if not updated or len(updated) == 0:
                msg_deleted = True
                logger.info(f"Тестовое сообщение удалено в {chat_name}")
        except Exception:
            msg_deleted = True
            logger.info(f"Сообщение не найдено в {chat_name} (возможно удалено)")

        # 6. Если не удалено — удаляем сами
        if not msg_deleted and test_msg_id:
            try:
                await client.delete_messages(entity, test_msg_id)
                logger.info(f"Тестовое сообщение удалено вручную в {chat_name}")
            except Exception as e:
                logger.warning(
                    f"Не удалось удалить тестовое сообщение в {chat_name}: {e}"
                )

    except (ChatAdminRequiredError, FloodWaitError) as e:
        logger.warning(f"Бот не может писать в {chat_name}: {e}")
        msg_deleted = False
        return None  # Бот не может писать — удаляем из БД и выходим
    except Exception as e:
        logger.warning(f"Ошибка проверки письма в {chat_name}: {e}")
        msg_deleted = False
        return None  # Ошибка — удаляем из БД и выходим

    # 7. Если сообщение удалено — не записываем
    if msg_deleted:
        logger.info(f"Сообщение удалено в {chat_name} — не записываем")
        return None

    # 8. Записываем/обновляем чат в БД
    try:
        if update_existing:
            # Обновляем существующий чат
            await chat_db.update_chat(
                chat_id,
                {
                    "chat_name": chat_name,
                    "chat_url": chat_url,
                    "chat_type": chat_type,
                    "user_count": user_count,
                    "verified": 1,
                    "last_check": datetime.now().isoformat(),
                    "status": "active",
                },
            )
            logger.info(f"✅ Чат {chat_name} ({chat_id}) обновлён в ChatDB")
        else:
            # Добавляем новый чат
            await chat_db.add_chat(
                chat_id,
                chat_name,
                chat_url,
                chat_type,
                user_count,
            )
            logger.info(f"✅ Чат {chat_name} ({chat_id}) добавлен в ChatDB")
    except Exception as e:
        logger.error(f"Ошибка записи чата {chat_name} в БД: {e}")
        return None

    # Возвращаем данные чата
    return {
        "chat_id": chat_id,
        "chat_name": chat_name,
        "chat_url": chat_url,
        "chat_type": chat_type,
        "user_count": user_count,
    }


async def check_and_clean_banned_chats(
    client: TelegramClient,
    bot_telegram: Bot,
    bot_user_id: int,
) -> Dict[str, int]:
    """
    Проверяет все чаты в БД:
    - Если бот забанен — удаляет чат из БД
    - Возвращает статистику
    """
    result = {"checked": 0, "removed": 0, "errors": 0}
    chats = await chat_db.get_all_chats()
    result["checked"] = len(chats)

    for chat in chats:
        try:
            chat_id = chat["chat_id"]
            chat_name = chat["chat_name"]
            chat_url = chat["chat_url"]

            # Проверяем можно ли получить сущность
            try:
                entity = await client.get_entity(chat_url)
            except Exception:
                # Если не можем получить — возможно забанен
                removed = await chat_db.remove_chat(chat_id)
                if removed:
                    result["removed"] += 1
                    logger.info(f"🗑️ Чат {chat_name} удалён (недоступен)")
                continue

            # Проверяем тип — если это пользователь, удаляем
            if isinstance(entity, telethon_types.User):
                removed = await chat_db.remove_chat(chat_id)
                if removed:
                    result["removed"] += 1
                    logger.info(f"🗑️ Чат {chat_name} удалён (пользователь)")
                continue

            # Проверяем can_send_messages через бота aiogram
            try:
                await bot_telegram.get_chat(chat_id)
                # Если бот aiogram может получить чат — он активен
            except TelegramBadRequest as e:
                if "not found" in str(e).lower() or "migrate" not in str(e).lower():
                    # Возможно забанен
                    logger.info(f"Бот aiogram не может получить чат {chat_name}: {e}")
                    # Для телефон-клиента проверяем через отправление сообщения
                    try:
                        test_msg = await client.send_message(entity, "🐛 Health check")
                        await client.delete_messages(entity, test_msg.id)
                    except (ChatAdminRequiredError, FloodWaitError):
                        removed = await chat_db.remove_chat(chat_id)
                        if removed:
                            result["removed"] += 1
                            logger.info(f"🗑️ Чат {chat_name} удалён (бан)")
                        continue
                    except Exception:
                        pass

        except Exception as e:
            result["errors"] += 1
            logger.error(
                f"Ошибка проверки чата {chat.get('chat_name', 'unknown')}: {e}"
            )

    return result


# --- AccountPoolManager ---
class AccountPoolManager:
    def __init__(self):
        self.accounts: List[Dict[str, Any]] = []
        self.lock = asyncio.Lock()
        self._load_accounts()
        self._health_check_task = None
        # Smart metrics
        self._account_success_rate: Dict[str, float] = {}
        self._account_last_error: Dict[str, datetime] = {}
        self._max_consecutive_errors: int = 5
        self._adaptive_delay_base: float = 5.0
        self._adaptive_delay_max: float = 120.0

    async def start_health_check(self):
        """Запускает фоновый health-check для аккаунтов"""
        self._health_check_task = asyncio.create_task(self._periodic_health_check())

    async def stop_health_check(self):
        """Останавливает health-check"""
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass

    async def _periodic_health_check(self):
        """Периодическая проверка аккаунтов с adaptive delay"""
        while True:
            try:
                # Adaptive delay между проверками
                delay = max(60, Config.HEALTH_CHECK_INTERVAL / 2)
                await asyncio.sleep(delay)
                await self._check_all_accounts()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health check error: {e}")
                await asyncio.sleep(30)  # Short delay on error

    async def _check_all_accounts(self):
        """Проверка всех аккаунтов с recovery"""
        async with self.lock:
            for acc in self.accounts:
                if not acc["in_use"]:
                    if not acc["client"] or not acc["client"].is_connected():
                        try:
                            if acc["client"]:
                                await acc["client"].disconnect()
                            acc["client"] = await self._create_client_with_retry(
                                acc["session_string"]
                            )
                            me = await acc["client"].get_me()
                            if not me:
                                raise Exception("Not authorized")
                            acc["is_valid"] = True
                            acc["last_check"] = datetime.now()
                            # Update success rate
                            self._update_account_success_rate(acc["session_file"], True)
                            logger.info(f"Account health OK: {acc['session_file']}")
                        except Exception as e:
                            logger.error(
                                f"Account health check failed {acc['session_file']}: {e}"
                            )
                            acc["is_valid"] = False
                            self._update_account_success_rate(
                                acc["session_file"], False
                            )

    async def _create_client_with_retry(self, session_string: str) -> TelegramClient:
        """Создаёт клиент с retry logic"""
        for attempt in range(3):
            try:
                client = create_telegram_client(session_string)
                await asyncio.wait_for(client.connect(), timeout=30.0)
                return client
            except Exception as e:
                if attempt < 2:
                    wait_time = 2**attempt
                    logger.warning(
                        f"Client creation attempt {attempt + 1} failed: {e}, retrying in {wait_time}s"
                    )
                    await asyncio.sleep(wait_time)
                else:
                    raise
        raise Exception("Failed to create client after 3 attempts")

    def _load_accounts(self):
        for filename in os.listdir(Config.SESSIONS_DIR):
            if filename.endswith(".session"):
                session_path = os.path.join(Config.SESSIONS_DIR, filename)
                try:
                    with open(session_path, "r") as f:
                        session_string = f.read().strip()
                    self.accounts.append(
                        {
                            "session_file": filename,
                            "session_string": session_string,
                            "client": None,
                            "in_use": False,
                            "last_used": None,
                            "last_check": None,
                            "is_valid": True,
                            "flood_wait_until": None,
                            "error_count": 0,
                            "invite_count": 0,
                            "success_count": 0,
                            "consecutive_errors": 0,
                        }
                    )
                    self._account_success_rate[filename] = 1.0
                except Exception as e:
                    logger.error(f"Error loading session {filename}: {e}")

    async def _reset_account_errors(self, acc: Dict):
        """Сбрасывает счётчик ошибок"""
        acc["error_count"] = 0
        acc["invite_count"] = 0
        acc["consecutive_errors"] = 0

    def _update_account_success_rate(self, session_file: str, success: bool):
        """Обновляет статистику успеха аккаунта"""
        if session_file not in self._account_success_rate:
            self._account_success_rate[session_file] = 1.0

        current_rate = self._account_success_rate[session_file]
        # Exponential moving average
        alpha = 0.1
        new_rate = alpha * (1.0 if success else 0.0) + (1 - alpha) * current_rate
        self._account_success_rate[session_file] = new_rate

        if not success:
            self._account_last_error[session_file] = datetime.now()

    async def _handle_flood_wait(self, acc: Dict, wait_time: float):
        """Обрабатывает FloodWait с увеличенным временем ожидания"""
        extended_wait = wait_time * Config.FLOOD_WAIT_MULTIPLIER
        acc["flood_wait_until"] = datetime.now() + timedelta(seconds=extended_wait)
        acc["consecutive_errors"] += 1
        logger.warning(
            f"Flood wait for {acc['session_file']}: {extended_wait:.1f}s (original: {wait_time:.1f}s)"
        )

    async def _check_account_flood(self, acc: Dict) -> bool:
        """Проверяет, не в flood wait ли аккаунт"""
        if acc.get("flood_wait_until"):
            if acc["flood_wait_until"] > datetime.now():
                remaining = (acc["flood_wait_until"] - datetime.now()).total_seconds()
                logger.info(
                    f"Account {acc['session_file']} in flood wait: {remaining:.1f}s remaining"
                )
                return False
            else:
                acc["flood_wait_until"] = None
        return True

    async def _check_account_rate_limit(self, acc: Dict) -> bool:
        """Проверяет rate limit аккаунта с adaptive delay"""
        if acc.get("last_used"):
            elapsed = (datetime.now() - acc["last_used"]).total_seconds()
            # Adaptive delay based on success rate
            success_rate = self._account_success_rate.get(acc["session_file"], 1.0)
            adaptive_delay = self._adaptive_delay_base * (1.0 / max(success_rate, 0.1))
            adaptive_delay = min(adaptive_delay, self._adaptive_delay_max)

            if elapsed < adaptive_delay:
                logger.info(
                    f"Account {acc['session_file']} rate limited: {elapsed:.1f}s since last use (adaptive: {adaptive_delay:.1f}s)"
                )
                return False
        return True

    async def _get_adaptive_delay(self, acc: Dict) -> float:
        """Получает adaptive delay для аккаунта"""
        success_rate = self._account_success_rate.get(acc["session_file"], 1.0)
        delay = self._adaptive_delay_base * (1.0 / max(success_rate, 0.1))
        delay = min(delay, self._adaptive_delay_max)
        # Add randomness
        delay *= random.uniform(0.8, 1.2)
        return delay

    async def _detect_bot_user(self, user: Any) -> bool:
        """Определяет, является ли пользователь ботом"""
        if hasattr(user, "bot") and user.bot:
            return True
        if hasattr(user, "is_bot") and user.is_bot:
            return True
        return False

    async def _safe_get_user(
        self, client: TelegramClient, user_id: int, max_retries: int = None
    ) -> Optional[Any]:
        """Безопасно получает пользователя с retry logic и adaptive delay"""
        if max_retries is None:
            max_retries = Config.MAX_RETRIES

        for attempt in range(max_retries):
            try:
                return await client.get_entity(user_id)
            except FloodWaitError as e:
                logger.warning(f"Flood wait on get_entity {user_id}: {e.value}s")
                acc = next(
                    (acc for acc in self.accounts if acc.get("client") == client),
                    None,
                )
                if acc:
                    await self._handle_flood_wait(acc, e.value)
                raise
            except Exception as e:
                if attempt < max_retries - 1:
                    wait_time = 2**attempt  # Exponential backoff
                    logger.warning(
                        f"Retry get_entity {user_id} attempt {attempt + 1}: {e}"
                    )
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(
                        f"Failed to get entity {user_id} after {max_retries} attempts: {e}"
                    )
                    return None

    async def _safe_invite(
        self, client: TelegramClient, chat_id: str, user_id: int, acc: Dict = None
    ) -> bool:
        """Безопасно приглашает пользователя с retry и анти-бан механизмами"""
        for attempt in range(Config.MAX_RETRIES):
            try:
                await client(
                    functions.channels.InviteToChannelRequest(
                        channel=chat_id, users=[user_id]
                    )
                )
                # Update success rate
                if acc:
                    self._update_account_success_rate(acc["session_file"], True)
                    acc["success_count"] = acc.get("success_count", 0) + 1
                    acc["consecutive_errors"] = 0
                return True
            except UserPrivacyRestrictedError:
                logger.info(f"User {user_id} privacy restricted - skipping")
                return False
            except UserNotParticipantError:
                logger.warning(f"User {user_id} not participant - retrying")
                if attempt < Config.MAX_RETRIES - 1:
                    await asyncio.sleep(random.uniform(1, 3))
                    continue
                return False
            except FloodWaitError as e:
                logger.warning(f"Flood wait on invite {user_id}: {e.value}s")
                if acc:
                    await self._handle_flood_wait(acc, e.value)
                raise
            except ChatAdminRequiredError:
                logger.error(f"Need admin rights for chat {chat_id}")
                return False
            except Exception as e:
                # Update error rate
                if acc:
                    self._update_account_success_rate(acc["session_file"], False)
                    acc["consecutive_errors"] = acc.get("consecutive_errors", 0) + 1

                if attempt < Config.MAX_RETRIES - 1:
                    wait_time = 2**attempt
                    logger.warning(f"Retry invite {user_id} attempt {attempt + 1}: {e}")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(
                        f"Failed to invite {user_id} after {Config.MAX_RETRIES} attempts: {e}"
                    )
                    return False
        return False

    async def _random_delay(
        self, min_delay: Optional[int] = None, max_delay: Optional[int] = None
    ):
        """Случайная задержка для имитации человеческого поведения"""
        delay = random.uniform(
            min_delay or Config.MIN_INVITE_DELAY, max_delay or Config.MAX_INVITE_DELAY
        )
        logger.info(f"Anti-block delay: {delay:.1f}s")
        await asyncio.sleep(delay)

    async def human_delay(self):
        """Генерация человеческого паттерна поведения"""
        # 80% обычный интервал, 20% "перерыв на кофе"
        if random.random() < 0.8:
            await self._random_delay()
        else:
            # "Перерыв" 5-15 минут
            break_time = random.uniform(300, 900)
            logger.info(f"☕ Human break: {break_time:.0f}s")
            await asyncio.sleep(break_time)

    async def simulate_activity(self, client: TelegramClient):
        """Эмуляция активности аккаунта для защиты от detection"""
        try:
            # Чтение сообщений из диалогов
            dialogs = await client.get_dialogs()
            if dialogs:
                sample = random.sample(dialogs, min(5, len(dialogs)))
                for dialog in sample:
                    if dialog.entity:
                        try:
                            async for _ in client.iter_messages(dialog.entity, limit=1):
                                pass
                        except Exception:
                            pass
                logger.info("📖 Simulated reading messages")

            await asyncio.sleep(random.uniform(60, 180))
        except Exception as e:
            logger.debug(f"Simulate activity error: {e}")

    async def _create_client(self, session_string: str) -> TelegramClient:
        client = create_telegram_client(session_string)
        await client.connect()
        return client

    @asynccontextmanager
    async def acquire_account(self):
        account = None
        try:
            async with self.lock:
                now = datetime.now()

                # Smart rotation: сортировка по success rate и последнему использованию
                def account_sort_key(acc):
                    success_rate = self._account_success_rate.get(
                        acc["session_file"], 1.0
                    )
                    last_used = acc["last_used"] or datetime.min
                    # Приоритет: success rate > last used > invite count
                    return (-success_rate, last_used, acc.get("invite_count", 0))

                self.accounts.sort(key=account_sort_key)

                for acc in self.accounts:
                    if not acc["in_use"] and acc["is_valid"]:
                        # Проверка flood wait
                        if not await self._check_account_flood(acc):
                            continue
                        # Проверка rate limit с adaptive delay
                        if not await self._check_account_rate_limit(acc):
                            continue
                        # Проверка лимита инвайтов
                        if acc.get("invite_count", 0) >= Config.MAX_ACCOUNTS_PER_TASK:
                            await self._reset_account_errors(acc)

                        acc["in_use"] = True
                        acc["last_used"] = now
                        if not acc["client"] or not acc["client"].is_connected():
                            try:
                                if acc["client"]:
                                    await acc["client"].disconnect()
                                acc["client"] = await self._create_client_with_retry(
                                    acc["session_string"]
                                )
                                me = await acc["client"].get_me()
                                if not me:
                                    raise Exception("Not authorized")
                            except Exception as e:
                                logger.error(f"Error connecting: {e}")
                                acc["is_valid"] = False
                                acc["in_use"] = False
                                continue
                        logger.info(f"Acquired account: {acc['session_file']}")
                        account = acc
                        break

                if not account:
                    raise Exception("No available accounts")
            yield account
        finally:
            if account:
                async with self.lock:
                    account["in_use"] = False
                    account["invite_count"] = account.get("invite_count", 0) + 1
                    logger.info(
                        f"Released account: {account['session_file']} (invites: {account['invite_count']}, success_rate: {self._account_success_rate.get(account['session_file'], 0):.2f})"
                    )

    @asynccontextmanager
    async def acquire_specific_account(self, session_file: str):
        account = None
        async with self.lock:
            now = datetime.now()
            for acc in self.accounts:
                if acc["session_file"] == session_file:
                    if acc["in_use"]:
                        raise Exception(
                            "Указанный аккаунт в данный момент используется"
                        )
                    if not acc["is_valid"]:
                        raise Exception("Указанный аккаунт невалиден")
                    if not await self._check_account_flood(acc):
                        raise Exception(
                            "Указанный аккаунт в режиме ожидания из-за flood"
                        )
                    acc["in_use"] = True
                    acc["last_used"] = now
                    account = acc
                    break
            if not account:
                raise Exception("Указанный аккаунт не найден")
            if not account["client"] or not account["client"].is_connected():
                try:
                    if account["client"]:
                        await account["client"].disconnect()
                    account["client"] = await self._create_client_with_retry(
                        account["session_string"]
                    )
                    me = await account["client"].get_me()
                    if not me:
                        raise Exception("Not authorized")
                except Exception as e:
                    account["is_valid"] = False
                    account["in_use"] = False
                    raise Exception(f"Ошибка при подключении к аккаунту: {e}")
            logger.info(f"Acquired specific account: {account['session_file']}")
        try:
            yield account
        finally:
            async with self.lock:
                account["in_use"] = False
                logger.info(f"Released specific account: {account['session_file']}")

    def add_account(self, session_string: str, session_name: str):
        session_path = os.path.join(Config.SESSIONS_DIR, f"{session_name}.session")
        with open(session_path, "w") as f:
            f.write(session_string)
        self.accounts.append(
            {
                "session_file": f"{session_name}.session",
                "session_string": session_string,
                "client": None,
                "in_use": False,
                "last_used": None,
                "last_check": None,
                "is_valid": True,
                "flood_wait_until": None,
                "error_count": 0,
                "invite_count": 0,
            }
        )
        logger.info(f"Added new account: {session_name}.session")


# --- TaskQueueManager ---
class TaskControl:
    def __init__(self):
        self.pause_event = asyncio.Event()
        self.pause_event.set()
        self.cancelled = False
        self.start_time = datetime.now()
        self.last_activity = datetime.now()
        self.timeout: int = 3600  # 1 hour default timeout

    def update_activity(self):
        """Обновляет timestamp последней активности"""
        self.last_activity = datetime.now()

    def is_timed_out(self) -> bool:
        """Проверяет, не истёк ли таймаут задачи"""
        elapsed = (datetime.now() - self.start_time).total_seconds()
        return elapsed > self.timeout


class TaskQueueManager:
    def __init__(self, max_concurrent_tasks: int = 3):
        self.max_concurrent_tasks = max_concurrent_tasks
        self.queue = asyncio.Queue()
        self.active_tasks: Dict[str, asyncio.Task] = {}
        self.task_controls: Dict[str, TaskControl] = {}
        self.user_tasks: Dict[int, Set[str]] = {}
        self.logger = logging.getLogger("task_queue")
        self.tasks_storage: Optional[TasksDB] = None
        self._worker_tasks: Dict[str, asyncio.Task] = {}
        self._health_check_task = None
        self._task_timeout: int = 3600  # 1 hour default
        self._active_worker_count: int = 0
        self._scaling_task = None

    def set_storage(self, storage: TasksDB) -> None:
        self.tasks_storage = storage

    def start_workers(self):
        # Запуск минимального количества воркеров
        for i in range(Config.MIN_WORKERS):
            self._create_worker(f"worker-{i + 1}")
        # Запуск авто-масштабирования
        self._scaling_task = asyncio.create_task(self._auto_scale_workers())
        # Start health check
        self._health_check_task = asyncio.create_task(self._task_health_check())

    def _create_worker(self, name: str) -> None:
        """Создаёт нового воркера"""
        if name not in self._worker_tasks:
            task = asyncio.create_task(self._worker(name))
            self._worker_tasks[name] = task
            self._active_worker_count += 1
            self.logger.info(
                f"Worker {name} created (total: {self._active_worker_count})"
            )

    async def _auto_scale_workers(self):
        """Автоматическое масштабирование воркеров"""
        while True:
            try:
                await asyncio.sleep(Config.WORKER_CHECK_INTERVAL)

                queue_size = self.queue.qsize()
                active_count = len(
                    [t for t in self._worker_tasks.values() if not t.done()]
                )

                # Если очередь большая — добавляем воркеры
                if queue_size > active_count * 2 and active_count < Config.MAX_WORKERS:
                    new_count = min(
                        Config.MAX_WORKERS - active_count, (queue_size + 1) // 2
                    )
                    for i in range(new_count):
                        name = f"worker-{active_count + i + 1}"
                        self._create_worker(name)

                # Если воркеры простаивают — удаляем
                elif queue_size == 0 and active_count > Config.MIN_WORKERS:
                    idle_count = 0
                    for name, task in list(self._worker_tasks.items()):
                        if task.done() or (not self.queue.empty() and task.done()):
                            del self._worker_tasks[name]
                            self._active_worker_count -= 1
                            idle_count += 1
                            if idle_count >= active_count - Config.MIN_WORKERS:
                                break

                    if idle_count == 0:
                        # Проверяем idle timeout
                        now = datetime.now()
                        for name, task in list(self._worker_tasks.items()):
                            if not task.done() and self.queue.empty():
                                # Проверяем последний запуск
                                if hasattr(self, "_last_task_time"):
                                    if (
                                        now - self._last_task_time
                                    ).total_seconds() > Config.WORKER_IDLE_TIMEOUT:
                                        task.cancel()
                                        del self._worker_tasks[name]
                                        self._active_worker_count -= 1
                                        break
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Worker scaling error: {e}")
                await asyncio.sleep(30)

    async def stop_workers(self):
        """Останавливает всех воркеров"""
        if self._scaling_task:
            self._scaling_task.cancel()
            try:
                await self._scaling_task
            except asyncio.CancelledError:
                pass
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass
        for task in self._worker_tasks.values():
            task.cancel()
        await asyncio.gather(*self._worker_tasks.values(), return_exceptions=True)
        self._worker_tasks.clear()
        self._active_worker_count = 0

    async def _task_health_check(self):
        """Проверка здоровья задач (таймауты, зависания)"""
        while True:
            try:
                await asyncio.sleep(60)  # Check every minute
                for task_id, control in list(self.task_controls.items()):
                    if control.is_timed_out():
                        self.logger.warning(f"Task {task_id} timed out, cancelling")
                        control.cancelled = True
                        # Update task status
                        if self.tasks_storage:
                            await self.tasks_storage.update_by_id(
                                task_id,
                                {
                                    "status": "failed",
                                    "error": "Task timeout",
                                    "completed_at": datetime.now().isoformat(),
                                },
                                id_field="task_id",
                            )
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Task health check error: {e}")
                await asyncio.sleep(30)

    async def _worker(self, name: str):
        self.logger.info(f"Worker {name} started")
        while True:
            try:
                task_func, task_id, args, kwargs = await self.queue.get()
                self._last_task_time = datetime.now()  # Отмечаем последний запуск
                control = self.task_controls.setdefault(task_id, TaskControl())
                control.timeout = self._task_timeout
                try:
                    self.active_tasks[task_id] = asyncio.current_task()
                    # Wrap task with timeout and error handling
                    try:
                        await asyncio.wait_for(
                            task_func(control, *args, **kwargs),
                            timeout=self._task_timeout,
                        )
                    except asyncio.TimeoutError:
                        self.logger.warning(f"Task {task_id} timed out")
                        control.cancelled = True
                        if self.tasks_storage:
                            await self.tasks_storage.update_by_id(
                                task_id,
                                {
                                    "status": "failed",
                                    "error": "Task timeout",
                                    "completed_at": datetime.now().isoformat(),
                                },
                                id_field="task_id",
                            )
                except asyncio.CancelledError:
                    self.logger.info(f"Task {task_id} cancelled")
                    if self.tasks_storage:
                        await self.tasks_storage.update_by_id(
                            task_id,
                            {
                                "status": "cancelled",
                                "completed_at": datetime.now().isoformat(),
                            },
                            id_field="task_id",
                        )
                except Exception as e:
                    self.logger.exception(f"Task {task_id} error: {e}")
                    if self.tasks_storage:
                        await self.tasks_storage.update_by_id(
                            task_id,
                            {
                                "status": "failed",
                                "error": str(e),
                                "completed_at": datetime.now().isoformat(),
                            },
                            id_field="task_id",
                        )
                finally:
                    self.active_tasks.pop(task_id, None)
                    self.queue.task_done()
            except asyncio.CancelledError:
                self.logger.info(f"Worker {name} cancelled")
                break
            except Exception as e:
                self.logger.error(f"Worker {name} error: {e}")
                await asyncio.sleep(1)

    async def add_task(self, queue_task_id: str, task_func, *args, **kwargs):
        self.task_controls.setdefault(queue_task_id, TaskControl())
        await self.queue.put((task_func, queue_task_id, args, kwargs))
        self.logger.info(
            f"Task {queue_task_id} added to queue, queue size: {self.queue.qsize()}"
        )

    async def cancel_task(self, task_id: str) -> bool:
        control = self.task_controls.get(task_id)
        if not control:
            return False
        control.cancelled = True
        task = self.active_tasks.get(task_id)
        if task:
            task.cancel()
        if self.tasks_storage:
            await self.tasks_storage.update_by_id(
                task_id,
                {"status": "cancelled", "cancelled_at": datetime.now().isoformat()},
                id_field="task_id",
            )
        return True

    async def pause_task(self, task_id: str) -> bool:
        control = self.task_controls.get(task_id)
        if not control:
            return False
        control.pause_event.clear()
        if self.tasks_storage:
            await self.tasks_storage.update_by_id(
                task_id,
                {"status": "paused", "paused_at": datetime.now().isoformat()},
                id_field="task_id",
            )
        return True

    async def resume_task(self, task_id: str) -> bool:
        control = self.task_controls.get(task_id)
        if not control:
            return False
        control.pause_event.set()
        if self.tasks_storage:
            await self.tasks_storage.update_by_id(
                task_id,
                {"status": "running", "resumed_at": datetime.now().isoformat()},
                id_field="task_id",
            )
        return True

    def get_user_active_tasks(self, user_id: int) -> List[str]:
        return list(self.user_tasks.get(user_id, set()))

    def can_user_add_task(self, user_id: int) -> bool:
        return len(self.user_tasks.get(user_id, set())) < Config.MAX_TASKS_PER_USER

    def add_user_task(self, user_id: int, task_id: str):
        self.user_tasks.setdefault(user_id, set()).add(task_id)

    def remove_user_task(self, user_id: int, task_id: str):
        if user_id in self.user_tasks:
            self.user_tasks[user_id].discard(task_id)


# --- FSM States ---
class AddAccountStates(StatesGroup):
    waiting_phone = State()
    waiting_confirmation = State()
    waiting_code = State()
    waiting_password = State()


class ScrapingStates(StatesGroup):
    waiting_source = State()
    waiting_target = State()
    waiting_mode = State()
    waiting_message_limit = State()
    waiting_user_count = State()
    waiting_account = State()
    waiting_source_type = State()  # Выбор источника: ручной или БД


class KeyGeneration(StatesGroup):
    waiting_user_id = State()


class BulkMailStates(StatesGroup):
    waiting_chats = State()
    waiting_delay = State()
    waiting_text = State()
    waiting_sender = State()
    waiting_count = State()
    waiting_texts = State()  # Множественные тексты для рассылки
    waiting_db_source = State()  # Выбор источника (ручной или БД)


class ClearCacheStates(StatesGroup):
    waiting_confirmation = State()


class WormModeStates(StatesGroup):
    waiting_sources = State()
    active = State()


class AddChatsStates(StatesGroup):
    waiting_links = State()


# --- Инициализация объектов ---
bot = Bot(
    token=Config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)
dp.callback_query.middleware(CallbackAnswerMiddleware())

auth_manager = AuthManager(Config.AUTH_FILE)
cache_manager = CacheManager(Config.CACHE_DB_PATH)
chat_db = ChatDB(os.path.join(Config.DATA_DIR, "chats.db"))
user_db = UserDB(os.path.join(Config.DATA_DIR, "users.db"))
tasks_storage = TasksDB(Config.TASKS_FILE)
account_pool = AccountPoolManager()
task_queue = TaskQueueManager(max_concurrent_tasks=Config.MAX_CONCURRENT_TASKS)
task_queue.set_storage(tasks_storage)

pending_auth = {}

# --- Worm Mode state ---
_worm_active: bool = False
_worm_task: Optional[asyncio.Task] = None
_worm_chat_id: Optional[str] = None
_worm_stats: Dict[str, Dict[str, int]] = {}
_worm_lock = asyncio.Lock()


# --- Middleware ---
async def auth_middleware(handler, event, data):
    if isinstance(event, Message):
        user_id = event.from_user.id
    elif isinstance(event, CallbackQuery):
        user_id = event.from_user.id
    else:
        return await handler(event, data)

    if user_id in Config.ADMIN_USER_IDS or await auth_manager.is_authorized(user_id):
        return await handler(event, data)

    if isinstance(event, Message) and event.text == "/start":
        return await handler(event, data)

    if isinstance(event, Message) and user_id in pending_auth:
        return await handler(event, data)

    if isinstance(event, Message):
        if user_id not in pending_auth:
            pending_auth[user_id] = True
            text = translate("auth_required")
            keyboard = [[{"text": translate("buttons.main"), "callback_data": "start"}]]
            await smart_answer(
                event, bot, text, reply_markup=kb(keyboard), delete_origin=False
            )
        else:
            await smart_answer(
                event, bot, translate("waiting_key"), delete_origin=False
            )
    return None


router.message.middleware(auth_middleware)
router.callback_query.middleware(auth_middleware)


# --- Обработчики команд ---
@router.message(Command("start"))
@router.callback_query(F.data == "start")
async def cmd_start(event, state: FSMContext):
    await state.clear()

    if isinstance(event, Message):
        user_id = event.from_user.id
    else:
        user_id = event.from_user.id

    is_admin = user_id in Config.ADMIN_USER_IDS
    is_authorized = await auth_manager.is_authorized(user_id)

    if not is_admin and not is_authorized:
        pending_auth[user_id] = True
        text = translate("auth_required")
        await smart_answer(event, bot, text, delete_origin=False)
        return

    # Получаем статистику
    accounts_count = len(account_pool.accounts)
    chats_count = await chat_db.get_active_chats_count() if chat_db.conn else 0
    total_users = await chat_db.get_total_users() if chat_db.conn else 0

    if is_admin:
        text = translate(
            "welcome_admin",
            accounts=accounts_count,
            chats=chats_count,
            users=total_users,
        )
        keyboard = [
            [{"text": translate("buttons.task_list"), "callback_data": "task_list"}],
            [
                {
                    "text": translate("buttons.cancel_all_tasks"),
                    "callback_data": "cancel_all_tasks",
                }
            ],
            [
                {
                    "text": translate("buttons.list_accounts"),
                    "callback_data": "list_accounts",
                }
            ],
            [
                {
                    "text": translate("buttons.add_account"),
                    "callback_data": "add_account",
                }
            ],
            [
                {
                    "text": translate("buttons.add_chats_to_db"),
                    "callback_data": "add_chats_to_db",
                }
            ],
            [
                {
                    "text": translate("buttons.update_chats_db"),
                    "callback_data": "update_chats_db",
                }
            ],
            [
                {
                    "text": translate("buttons.clear_cache"),
                    "callback_data": "clear_cache",
                }
            ],
            [{"text": translate("buttons.worm_mode"), "callback_data": "worm_mode"}],
            [{"text": translate("buttons.genkey"), "callback_data": "genkey"}],
            [
                {
                    "text": translate("buttons.start_scraping"),
                    "callback_data": "start_scraping",
                }
            ],
            [
                {
                    "text": translate("buttons.bulk_mailing"),
                    "callback_data": "bulk_mailing",
                }
            ],
            [
                {
                    "text": "🌐 Language",
                    "callback_data": "language_select",
                }
            ],
        ]
    else:
        text = translate("welcome_user")
        keyboard = [
            [
                {
                    "text": translate("buttons.add_account"),
                    "callback_data": "add_account",
                }
            ],
            [
                {
                    "text": translate("buttons.list_accounts"),
                    "callback_data": "list_accounts",
                }
            ],
            [
                {
                    "text": translate("buttons.start_scraping"),
                    "callback_data": "start_scraping",
                }
            ],
            [
                {
                    "text": translate("buttons.bulk_mailing"),
                    "callback_data": "bulk_mailing",
                }
            ],
            [{"text": translate("buttons.my_tasks"), "callback_data": "my_tasks"}],
            [{"text": translate("buttons.worm_mode"), "callback_data": "worm_mode"}],
            [
                {
                    "text": "🌐 Language",
                    "callback_data": "language_select",
                }
            ],
        ]

    await smart_answer(event, bot, text, reply_markup=kb(keyboard), delete_origin=True)


@router.callback_query(F.data == "cancel")
async def cmd_cancel(event, state: FSMContext):
    await state.clear()
    await cmd_start(event, state)


def is_admin_user(user_id: int) -> bool:
    """Проверяет, является ли пользователь администратором"""
    return user_id in Config.ADMIN_USER_IDS


# --- Language selection ---
class LanguageStates(StatesGroup):
    waiting_choice = State()


def build_language_keyboard() -> InlineKeyboardMarkup:
    """Создаёт клавиатуру выбора языка"""
    rows: List[List[Dict[str, str]]] = []
    lang_buttons: List[Dict[str, str]] = []
    for code in get_available_languages():
        display_name = get_language_display_name(code)
        lang_buttons.append({"text": display_name, "callback_data": f"lang:{code}"})
    if lang_buttons:
        rows.append(lang_buttons)
    rows.append([{"text": translate("buttons.cancel"), "callback_data": "cancel"}])
    return kb(rows)


async def get_lang(state: FSMContext, user_id: int) -> str:
    """Получает язык пользователя из FSM или UserDB"""
    try:
        data = await state.get_data()
        lang = data.get("language", "")
        if lang:
            return lang
    except Exception:
        pass
    return await user_db.get_user_language(user_id)


@router.callback_query(F.data == "language_select")
async def cmd_language_select(event: CallbackQuery, state: FSMContext):
    user_id = event.from_user.id

    if is_admin_user(user_id):
        await event.answer(translate("texts.admin_language_notice"), show_alert=True)
        return

    await state.set_state(LanguageStates.waiting_choice)
    text = translate("texts.language_prompt")
    await event.answer(text, show_alert=False)
    await event.message.edit_text(text, reply_markup=build_language_keyboard())


@router.callback_query(F.data.startswith("lang:"))
async def cmd_language_change(event: CallbackQuery, state: FSMContext):
    user_id = event.from_user.id
    lang = event.data.split(":", 1)[1]

    if is_admin_user(user_id):
        await event.answer(translate("texts.admin_language_notice"), show_alert=True)
        return

    if lang not in get_available_languages():
        await event.answer(translate("texts.language_not_supported"), show_alert=True)
        return

    await user_db.add_user(user_id)
    await user_db.set_user_language(user_id, lang)
    logger.info(f"🌍 Пользователь {user_id} выбрал язык: {lang}")

    lang_name = get_language_display_name(lang)
    await event.answer(
        translate("texts.language_selected", language=lang_name),
        show_alert=True,
    )


@router.message(lambda message: message.from_user.id in pending_auth)
async def process_auth_key(message: Message):
    user_id = message.from_user.id
    key = message.text.strip()

    if await auth_manager.verify_key(user_id, key):
        await auth_manager.add_authorized_user(user_id)
        del pending_auth[user_id]
        text = translate("auth_success")
        await smart_answer(message, bot, text, delete_origin=False)
        await cmd_start(message, None)
    else:
        await notify_admins(
            bot,
            translate(
                "admin_notif_unauthorized",
                user_id=user_id,
                key=key,
            ),
        )
        text = translate("auth_invalid")
        await smart_answer(message, bot, text, delete_origin=False)


@router.callback_query(F.data == "add_account")
async def cmd_add_account(event: CallbackQuery, state: FSMContext):
    text = translate("waiting_phone")
    keyboard = [[{"text": translate("buttons.cancel"), "callback_data": "cancel"}]]
    await smart_answer(event, bot, text, reply_markup=kb(keyboard), delete_origin=True)
    await state.set_state(AddAccountStates.waiting_phone)


@router.message(AddAccountStates.waiting_phone)
async def process_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    await state.update_data(phone=phone)
    text = translate("safety_notice")
    keyboard = [
        [{"text": translate("buttons.confirm_yes"), "callback_data": "confirm_yes"}],
        [{"text": translate("buttons.confirm_no"), "callback_data": "cancel"}],
    ]
    await smart_answer(
        message, bot, text, reply_markup=kb(keyboard), delete_origin=False
    )
    await state.set_state(AddAccountStates.waiting_confirmation)


@router.callback_query(F.data == "confirm_yes", AddAccountStates.waiting_confirmation)
async def process_confirmation_yes(event: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    phone = data.get("phone")
    if not phone:
        await smart_answer(
            event,
            bot,
            translate("phone_missing"),
            delete_origin=True,
        )
        await state.clear()
        return

    client = create_telegram_client()
    await client.connect()
    try:
        sent_code = await client.send_code_request(phone)
        await state.update_data(
            client=client,
            phone_code_hash=sent_code.phone_code_hash,
            password_attempts=0,
        )
        text = translate("waiting_code", phone=phone)
        keyboard = [[{"text": translate("buttons.cancel"), "callback_data": "cancel"}]]
        await smart_answer(
            event, bot, text, reply_markup=kb(keyboard), delete_origin=True
        )
        await state.set_state(AddAccountStates.waiting_code)
    except (PhoneNumberInvalidError, FloodWaitError) as e:
        await smart_answer(event, bot, translate("invalid_format"), delete_origin=True)
        await client.disconnect()
        await state.clear()
    except Exception as e:
        await smart_answer(event, bot, translate("invalid_format"), delete_origin=True)
        await client.disconnect()
        await state.clear()


@router.message(AddAccountStates.waiting_code)
async def process_code(message: Message, state: FSMContext):
    code = message.text.strip()
    data = await state.get_data()
    client = data.get("client")
    phone = data.get("phone")
    phone_code_hash = data.get("phone_code_hash")

    if not client or not phone or not phone_code_hash:
        await smart_answer(
            message,
            bot,
            translate("auth_error_state"),
            delete_origin=False,
        )
        await state.clear()
        return

    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        if await client.is_user_authorized():
            session_string = client.session.save()
            await client.disconnect()
            persistent_client = create_telegram_client(session_string)
            await persistent_client.connect()
            me = await persistent_client.get_me()
            if not me:
                raise Exception("Authorization failed after reconnect")
            session_name = f"account_{me.id}"
            account_pool.add_account(session_string, session_name)
            name = f"{me.first_name or ''} {me.last_name or ''}".strip()
            text = translate(
                "account_added",
                name=name,
                username=me.username or "none",
                phone=phone,
            )
            keyboard = [[{"text": translate("buttons.main"), "callback_data": "start"}]]
            await smart_answer(
                message, bot, text, reply_markup=kb(keyboard), delete_origin=False
            )
            await persistent_client.disconnect()
            await state.clear()
        else:
            text = translate("waiting_password")
            keyboard = [
                [{"text": translate("buttons.cancel"), "callback_data": "cancel"}]
            ]
            await smart_answer(
                message, bot, text, reply_markup=kb(keyboard), delete_origin=False
            )
            await state.update_data(password_attempts=0)
            await state.set_state(AddAccountStates.waiting_password)
    except SessionPasswordNeededError:
        text = translate("waiting_password")
        keyboard = [[{"text": translate("buttons.cancel"), "callback_data": "cancel"}]]
        await smart_answer(
            message, bot, text, reply_markup=kb(keyboard), delete_origin=False
        )
        await state.update_data(password_attempts=0)
        await state.set_state(AddAccountStates.waiting_password)
    except PhoneCodeExpiredError:
        try:
            sent_code = await client.send_code_request(phone)
            await state.update_data(phone_code_hash=sent_code.phone_code_hash)
            text = translate("waiting_code", phone=phone)
            keyboard = [
                [{"text": translate("buttons.cancel"), "callback_data": "cancel"}]
            ]
            await smart_answer(
                message, bot, text, reply_markup=kb(keyboard), delete_origin=False
            )
        except Exception as e:
            await smart_answer(
                message,
                bot,
                translate("invalid_format"),
                delete_origin=False,
            )
            await client.disconnect()
            await state.clear()
    except (PhoneCodeInvalidError, FloodWaitError) as e:
        await smart_answer(
            message, bot, translate("invalid_format"), delete_origin=False
        )
        await client.disconnect()
        await state.clear()
    except Exception as e:
        await smart_answer(
            message, bot, translate("invalid_format"), delete_origin=False
        )
        await client.disconnect()
        await state.clear()


@router.message(AddAccountStates.waiting_password)
async def process_password(message: Message, state: FSMContext):
    password = message.text.strip()
    data = await state.get_data()
    client = data.get("client")
    phone = data.get("phone")
    attempts = data.get("password_attempts", 0)

    if not client:
        await smart_answer(
            message,
            bot,
            translate("auth_error_state"),
            delete_origin=False,
        )
        await state.clear()
        return

    try:
        await client.sign_in(password=password)
        if await client.is_user_authorized():
            session_string = client.session.save()
            await client.disconnect()
            persistent_client = create_telegram_client(session_string)
            await persistent_client.connect()
            me = await persistent_client.get_me()
            if not me:
                raise Exception("Authorization failed after reconnect")
            session_name = f"account_{me.id}"
            account_pool.add_account(session_string, session_name)
            name = f"{me.first_name or ''} {me.last_name or ''}".strip()
            text = translate(
                "account_added",
                name=name,
                username=me.username or "none",
                phone=phone,
            )
            keyboard = [[{"text": translate("buttons.main"), "callback_data": "start"}]]
            await smart_answer(
                message, bot, text, reply_markup=kb(keyboard), delete_origin=False
            )
            await persistent_client.disconnect()
            await state.clear()
        else:
            await smart_answer(
                message,
                bot,
                translate("auth_error_password"),
                delete_origin=False,
            )
            await client.disconnect()
            await state.clear()
    except SessionPasswordNeededError:
        attempts += 1
        if attempts >= 3:
            await smart_answer(
                message,
                bot,
                translate("auth_max_attempts"),
                delete_origin=False,
            )
            await client.disconnect()
            await state.clear()
        else:
            await state.update_data(password_attempts=attempts)
            await smart_answer(
                message,
                bot,
                translate("auth_attempts_left", attempts=3 - attempts),
                delete_origin=False,
            )
    except Exception as e:
        await smart_answer(
            message, bot, translate("invalid_format"), delete_origin=False
        )
        await client.disconnect()
        await state.clear()


@router.callback_query(F.data == "list_accounts")
async def cmd_list_accounts(event: CallbackQuery):
    if not account_pool.accounts:
        text = translate("no_accounts")
        keyboard = [
            [
                {
                    "text": translate("buttons.add_account"),
                    "callback_data": "add_account",
                }
            ],
            [{"text": translate("buttons.main"), "callback_data": "start"}],
        ]
        await smart_answer(
            event, bot, text, reply_markup=kb(keyboard), delete_origin=True
        )
        return

    lines = [translate("accounts_list")]
    for i, acc in enumerate(account_pool.accounts, 1):
        status = (
            translate("account_status_free")
            if not acc["in_use"]
            else translate("account_status_busy")
        )
        validity = (
            translate("account_status_valid")
            if acc["is_valid"]
            else translate("account_status_invalid")
        )
        flood = (
            translate("account_flood_wait", until=acc["flood_wait_until"])
            if acc.get("flood_wait_until")
            else ""
        )
        lines.append(
            f"{i}. <code>{acc['session_file']}</code>\n"
            f"{translate('account_last_used', when=acc['last_used'] or translate('never'))} {flood}\n"
        )

    text = "\n".join(lines)
    keyboard = [[{"text": translate("buttons.main"), "callback_data": "start"}]]
    if event.from_user.id in Config.ADMIN_USER_IDS:
        keyboard.insert(
            0,
            [
                {
                    "text": translate("account_list_refresh"),
                    "callback_data": "list_accounts",
                }
            ],
        )

    await smart_answer(event, bot, text, reply_markup=kb(keyboard), delete_origin=True)


@router.callback_query(F.data == "genkey")
async def cmd_genkey(event: CallbackQuery, state: FSMContext):
    if event.from_user.id not in Config.ADMIN_USER_IDS:
        await smart_answer(event, bot, translate("only_admin"), show_alert=True)
        return

    text = translate("genkey_title")
    keyboard = [[{"text": translate("buttons.cancel"), "callback_data": "cancel"}]]
    await smart_answer(event, bot, text, reply_markup=kb(keyboard), delete_origin=True)
    await state.set_state(KeyGeneration.waiting_user_id)


@router.message(KeyGeneration.waiting_user_id)
async def process_user_id(message: Message, state: FSMContext):
    try:
        user_id = int(message.text.strip())
        key = await auth_manager.generate_key(user_id)
        text = translate(
            "genkey_success",
            user_id=user_id,
            key=key,
        )
        keyboard = [[{"text": translate("buttons.main"), "callback_data": "start"}]]
        await smart_answer(
            message, bot, text, reply_markup=kb(keyboard), delete_origin=False
        )
        await state.clear()
    except ValueError:
        await smart_answer(
            message,
            bot,
            translate("genkey_error"),
            delete_origin=False,
        )


@router.callback_query(F.data == "start_scraping")
async def cmd_start_scraping(event: CallbackQuery, state: FSMContext):
    if not account_pool.accounts:
        text = translate("no_available_accounts_btn")
        keyboard = [
            [
                {
                    "text": translate("buttons.add_account"),
                    "callback_data": "add_account",
                }
            ],
            [{"text": translate("buttons.main"), "callback_data": "start"}],
        ]
        await smart_answer(
            event, bot, text, reply_markup=kb(keyboard), delete_origin=True
        )
        return

    if not task_queue.can_user_add_task(event.from_user.id):
        text = translate(
            "max_tasks_reached",
            max_tasks=Config.MAX_TASKS_PER_USER,
        )
        keyboard = [
            [{"text": translate("buttons.my_tasks"), "callback_data": "my_tasks"}],
            [{"text": translate("buttons.main"), "callback_data": "start"}],
        ]
        await smart_answer(
            event, bot, text, reply_markup=kb(keyboard), delete_origin=True
        )
        return

    text = translate("scraping_source_select")
    keyboard = [
        [
            {
                "text": translate("scraping_source_manual"),
                "callback_data": "scraping_source:manual",
            }
        ],
        [
            {
                "text": translate("scraping_source_db_btn"),
                "callback_data": "scraping_source:db",
            }
        ],
        [{"text": translate("scraping_source_cancel"), "callback_data": "cancel"}],
    ]
    await smart_answer(event, bot, text, reply_markup=kb(keyboard), delete_origin=True)
    await state.set_state(ScrapingStates.waiting_source_type)


@router.callback_query(
    F.data.startswith("scraping_source:"), ScrapingStates.waiting_source_type
)
async def scraping_source_select(event: CallbackQuery, state: FSMContext):
    source_type = event.data.split(":")[1]
    await state.update_data(scraping_source_type=source_type)

    if source_type == "manual":
        text = translate("waiting_source")
        keyboard = [[{"text": translate("buttons.cancel"), "callback_data": "cancel"}]]
        await smart_answer(
            event, bot, text, reply_markup=kb(keyboard), delete_origin=True
        )
        await state.set_state(ScrapingStates.waiting_source)
    elif source_type == "db":
        chats = await chat_db.get_all_chats()
        if not chats:
            await smart_answer(
                event, bot, translate("scraping_db_empty"), show_alert=True
            )
            return
        await state.update_data(db_chats_count=len(chats))
        text = translate("scraping_source_db", count=len(chats))
        keyboard = [
            [
                {
                    "text": translate("buttons.continue"),
                    "callback_data": "scraping_db_continue",
                }
            ],
            [{"text": translate("buttons.cancel"), "callback_data": "cancel"}],
        ]
        await smart_answer(
            event, bot, text, reply_markup=kb(keyboard), delete_origin=True
        )


@router.callback_query(
    F.data == "scraping_db_continue", ScrapingStates.waiting_source_type
)
async def scraping_db_continue(event: CallbackQuery, state: FSMContext):
    text = translate("waiting_target")
    keyboard = [[{"text": translate("buttons.cancel"), "callback_data": "cancel"}]]
    await smart_answer(event, bot, text, reply_markup=kb(keyboard), delete_origin=True)
    await state.set_state(ScrapingStates.waiting_target)


@router.message(ScrapingStates.waiting_source)
async def process_source(message: Message, state: FSMContext):
    source = message.text.strip()
    await state.update_data(source=source)
    text = translate("scrape_step2")
    keyboard = [[{"text": translate("buttons.cancel"), "callback_data": "cancel"}]]
    await smart_answer(
        message, bot, text, reply_markup=kb(keyboard), delete_origin=False
    )
    await state.set_state(ScrapingStates.waiting_target)


@router.message(ScrapingStates.waiting_target)
async def process_target(message: Message, state: FSMContext):
    target = message.text.strip()
    await state.update_data(target=target)
    text = translate("scrape_step3")
    keyboard = [
        [{"text": translate("mode_messages"), "callback_data": "mode:1"}],
        [{"text": translate("mode_users"), "callback_data": "mode:2"}],
        [{"text": translate("buttons.cancel"), "callback_data": "cancel"}],
    ]
    await smart_answer(
        message, bot, text, reply_markup=kb(keyboard), delete_origin=False
    )
    await state.set_state(ScrapingStates.waiting_mode)


@router.callback_query(F.data.startswith("mode:"), ScrapingStates.waiting_mode)
async def process_mode_callback(event: CallbackQuery, state: FSMContext):
    mode = event.data.split(":")[1]
    await state.update_data(mode=mode)

    if mode == "1":
        text = translate("scrape_step4_msg")
        keyboard = [[{"text": translate("buttons.cancel"), "callback_data": "cancel"}]]
        await smart_answer(
            event, bot, text, reply_markup=kb(keyboard), delete_origin=True
        )
        await state.set_state(ScrapingStates.waiting_message_limit)
    elif mode == "2":
        text = translate("scrape_step4_users")
        keyboard = [[{"text": translate("buttons.cancel"), "callback_data": "cancel"}]]
        await smart_answer(
            event, bot, text, reply_markup=kb(keyboard), delete_origin=True
        )
        await state.set_state(ScrapingStates.waiting_user_count)


@router.callback_query(
    F.data.startswith("limit:"), ScrapingStates.waiting_message_limit
)
async def process_limit_callback(event: CallbackQuery, state: FSMContext):
    limit = int(event.data.split(":")[1])
    await state.update_data(limit=limit)
    await ask_for_account(event, state)


@router.message(ScrapingStates.waiting_message_limit)
async def process_limit(message: Message, state: FSMContext):
    try:
        limit = int(message.text)
        if limit < 50 or limit > 5000:
            raise ValueError
    except ValueError:
        await smart_answer(
            message,
            bot,
            translate("limit_error", min=50, max=5000),
            delete_origin=False,
        )
        return

    await state.update_data(limit=limit)
    await ask_for_account(message, state)


@router.callback_query(F.data.startswith("users:"), ScrapingStates.waiting_user_count)
async def process_user_count_callback(event: CallbackQuery, state: FSMContext):
    user_count = int(event.data.split(":")[1])
    await state.update_data(user_count=user_count)
    await ask_for_account(event, state)


@router.message(ScrapingStates.waiting_user_count)
async def process_user_count(message: Message, state: FSMContext):
    try:
        user_count = int(message.text)
        if user_count < 10 or user_count > 1000:
            raise ValueError
    except ValueError:
        await smart_answer(
            message,
            bot,
            translate("user_count_error", min=10, max=1000),
            delete_origin=False,
        )
        return

    await state.update_data(user_count=user_count)
    await ask_for_account(message, state)


async def ask_for_account(event, state: FSMContext):
    accounts_list = account_pool.accounts
    if not accounts_list:
        await smart_answer(
            event,
            bot,
            translate("scrape_no_accounts"),
            reply_markup=kb(
                [
                    [
                        {
                            "text": translate("scrape_add_account"),
                            "callback_data": "add_account",
                        }
                    ]
                ]
            ),
            delete_origin=False,
        )
        await state.clear()
        return

    if len(accounts_list) == 1:
        # Авто-выбор единственного аккаунта
        await state.update_data(sender_session=accounts_list[0]["session_file"])
        await smart_answer(
            event,
            bot,
            translate("selected_account", name=accounts_list[0]["session_file"]),
            delete_origin=False,
        )
        await launch_scraping_task(event, state)
        return

    lines = [translate("scrape_select_account")]
    keyboard_rows = [[{"text": translate("scrape_auto"), "callback_data": "acc:auto"}]]
    for i, acc in enumerate(accounts_list, 1):
        status = "🟢" if acc["is_valid"] else "⚫"
        busy = " ⏳" if acc["in_use"] else ""
        flood = " ⏱" if acc.get("flood_wait_until") else ""
        label = f"{i}. {acc['session_file'][:18]} {status}{busy}{flood}"
        keyboard_rows.append(
            [{"text": label, "callback_data": f"acc:{acc['session_file']}"}]
        )
    keyboard_rows.append(
        [{"text": translate("buttons.cancel"), "callback_data": "cancel"}]
    )

    await smart_answer(
        event,
        bot,
        "\n".join(lines),
        reply_markup=kb(keyboard_rows),
        delete_origin=False,
    )
    await state.set_state(ScrapingStates.waiting_account)


@router.callback_query(F.data.startswith("acc:"), ScrapingStates.waiting_account)
async def process_account_choice(event: CallbackQuery, state: FSMContext):
    val = event.data.split(":", 1)[1]
    sender_session = None if val == "auto" else val
    await state.update_data(sender_session=sender_session)
    await launch_scraping_task(event, state)


@router.message(ScrapingStates.waiting_account)
async def process_account_manual(message: Message, state: FSMContext):
    txt = message.text.strip().lower()
    sender_session = None
    if txt not in ("auto", "a"):
        sender_session = txt if txt.endswith(".session") else f"{txt}.session"
    await state.update_data(sender_session=sender_session)
    await launch_scraping_task(message, state)


async def launch_scraping_task(event, state: FSMContext):
    data = await state.get_data()
    source = data.get("source")
    target = data.get("target")
    mode = data.get("mode")
    sender_session = data.get("sender_session")
    user_id = event.from_user.id

    task_id = (
        f"scrape_{user_id}_{int(time.time())}"
        if mode == "1"
        else f"scrape_users_{user_id}_{int(time.time())}"
    )

    task_data = {
        "task_id": task_id,
        "user_id": user_id,
        "type": "scraping",
        "source": source,
        "target": target,
        "mode": "messages" if mode == "1" else "users",
        "limit": data.get("limit"),
        "user_count": data.get("user_count"),
        "status": "pending",
        "created_at": datetime.now().isoformat(),
        "progress": 0,
        "progress_text": "0/0",
        "priority": 1 if user_id in Config.ADMIN_USER_IDS else 0,
        "sender_session": sender_session,
        "joined_chats": [],
    }
    await tasks_storage.add(task_data)
    task_queue.add_user_task(user_id, task_id)

    if mode == "1":
        await task_queue.add_task(
            task_id,
            scrape_and_invite_task,
            source=source,
            target=target,
            message_limit=data.get("limit"),
            user_id=user_id,
            task_id=task_id,
            sender_session=sender_session,
        )
    else:
        await task_queue.add_task(
            task_id,
            scrape_and_invite_by_user_count_task,
            source=source,
            target=target,
            user_count=data.get("user_count"),
            user_id=user_id,
            task_id=task_id,
            sender_session=sender_session,
        )

    text = translate(
        "scrape_launch",
        task_id=task_id,
        source=source,
        target=target,
        mode=translate("messages") if mode == "1" else translate("users"),
        account=sender_session or "auto",
    )
    keyboard = [
        [{"text": translate("buttons.my_tasks"), "callback_data": "my_tasks"}],
        [{"text": translate("buttons.main"), "callback_data": "start"}],
    ]
    await smart_answer(
        event,
        bot,
        text,
        reply_markup=kb(keyboard),
        delete_origin=isinstance(event, CallbackQuery),
    )
    await state.clear()


@router.callback_query(F.data == "bulk_mailing")
async def cmd_bulk_mailing(event: CallbackQuery, state: FSMContext):
    if not task_queue.can_user_add_task(event.from_user.id):
        text = translate(
            "bulkmail_max_tasks",
            max_tasks=Config.MAX_TASKS_PER_USER,
        )
        keyboard = [
            [{"text": translate("buttons.my_tasks"), "callback_data": "my_tasks"}],
            [{"text": translate("buttons.main"), "callback_data": "start"}],
        ]
        await smart_answer(
            event, bot, text, reply_markup=kb(keyboard), delete_origin=True
        )
        return

    text = translate("bulkmail_source_select")
    keyboard = [
        [
            {
                "text": translate("bulkmail_source_manual"),
                "callback_data": "bulkmail_source:manual",
            }
        ],
        [
            {
                "text": translate("bulkmail_source_db_btn"),
                "callback_data": "bulkmail_source:db",
            }
        ],
        [{"text": translate("buttons.cancel"), "callback_data": "cancel"}],
    ]
    await smart_answer(event, bot, text, reply_markup=kb(keyboard), delete_origin=True)
    await state.set_state(BulkMailStates.waiting_db_source)


@router.callback_query(
    F.data.startswith("bulkmail_source:"), BulkMailStates.waiting_db_source
)
async def bm_source_select(event: CallbackQuery, state: FSMContext):
    source_type = event.data.split(":")[1]
    await state.update_data(chats_source_type=source_type)

    if source_type == "manual":
        text = translate("bulkmail_step1_manual")
        keyboard = [[{"text": translate("buttons.cancel"), "callback_data": "cancel"}]]
        await smart_answer(
            event, bot, text, reply_markup=kb(keyboard), delete_origin=True
        )
        await state.set_state(BulkMailStates.waiting_chats)
    elif source_type == "db":
        chats = await chat_db.get_all_chats()
        if not chats:
            await smart_answer(
                event, bot, translate("bulkmail_db_empty"), show_alert=True
            )
            return
        await state.update_data(db_chats_count=len(chats))
        text = translate("bulkmail_step1_db", count=len(chats))
        keyboard = [
            [
                {
                    "text": translate("buttons.continue"),
                    "callback_data": "bulkmail_db_continue",
                }
            ],
            [{"text": translate("buttons.cancel"), "callback_data": "cancel"}],
        ]
        await smart_answer(
            event, bot, text, reply_markup=kb(keyboard), delete_origin=True
        )


@router.callback_query(
    F.data == "bulkmail_db_continue", BulkMailStates.waiting_db_source
)
async def bm_db_continue(event: CallbackQuery, state: FSMContext):
    text = translate("bulkmail_step2_delay")
    keyboard = [
        [{"text": "5 10", "callback_data": "delay:5:10"}],
        [{"text": "10 20", "callback_data": "delay:10:20"}],
        [{"text": "20 30", "callback_data": "delay:20:30"}],
        [{"text": "30 60", "callback_data": "delay:30:60"}],
        [{"text": translate("buttons.cancel"), "callback_data": "cancel"}],
    ]
    await smart_answer(event, bot, text, reply_markup=kb(keyboard), delete_origin=True)
    await state.set_state(BulkMailStates.waiting_delay)


@router.message(BulkMailStates.waiting_chats)
async def bm_waiting_chats(message: Message, state: FSMContext):
    raw = message.text.strip()
    parts = [p.strip() for p in raw.replace(",", " ").split() if p.strip()]
    if not parts:
        await smart_answer(
            message,
            bot,
            translate("bulkmail_empty_chats"),
            delete_origin=False,
        )
        return

    await state.update_data(chats=parts)
    text = translate("bulkmail_step2_delay")
    keyboard = [
        [{"text": "5 10", "callback_data": "delay:5:10"}],
        [{"text": "10 20", "callback_data": "delay:10:20"}],
        [{"text": "20 30", "callback_data": "delay:20:30"}],
        [{"text": "30 60", "callback_data": "delay:30:60"}],
        [{"text": translate("buttons.cancel"), "callback_data": "cancel"}],
    ]
    await smart_answer(
        message, bot, text, reply_markup=kb(keyboard), delete_origin=False
    )
    await state.set_state(BulkMailStates.waiting_delay)


@router.callback_query(F.data.startswith("delay:"), BulkMailStates.waiting_delay)
async def bm_waiting_delay_callback(event: CallbackQuery, state: FSMContext):
    parts = event.data.split(":")
    dmin = int(parts[1])
    dmax = int(parts[2])
    await state.update_data(delay_min=dmin, delay_max=dmax)
    text = translate("bulkmail_step3_text_first")
    keyboard = [
        [
            {
                "text": translate("bulkmail_texts_done"),
                "callback_data": "bulkmail_texts_done",
            }
        ],
        [{"text": translate("buttons.cancel"), "callback_data": "cancel"}],
    ]
    await smart_answer(event, bot, text, reply_markup=kb(keyboard), delete_origin=True)
    await state.set_state(BulkMailStates.waiting_text)


@router.message(BulkMailStates.waiting_delay)
async def bm_waiting_delay(message: Message, state: FSMContext):
    parts = message.text.strip().split()
    try:
        if len(parts) != 2:
            raise ValueError
        dmin = int(parts[0])
        dmax = int(parts[1])
        if dmin < 0 or dmax < 0 or dmin > dmax:
            raise ValueError
    except ValueError:
        await smart_answer(
            message,
            bot,
            translate("bulkmail_invalid_delay"),
            delete_origin=False,
        )
        return

    await state.update_data(delay_min=dmin, delay_max=dmax)
    text = translate("bulkmail_step3_text_first")
    keyboard = [
        [
            {
                "text": translate("bulkmail_texts_done"),
                "callback_data": "bulkmail_texts_done",
            }
        ],
        [{"text": translate("buttons.cancel"), "callback_data": "cancel"}],
    ]
    await smart_answer(
        message, bot, text, reply_markup=kb(keyboard), delete_origin=False
    )
    await state.set_state(BulkMailStates.waiting_text)


@router.message(BulkMailStates.waiting_text)
async def bm_waiting_text(message: Message, state: FSMContext):
    if message.text == "/done" or message.text == "/закончить":
        await smart_answer(
            message,
            bot,
            translate("bulkmail_texts_confirm"),
            delete_origin=False,
        )
        return

    data = await state.get_data()
    texts = data.get("texts", [])
    texts.append(message.text)
    await state.update_data(texts=texts)

    current_count = len(texts)
    text = translate("bulkmail_texts_received", count=current_count)
    keyboard = [
        [
            {
                "text": translate("bulkmail_add_text"),
                "callback_data": "bulkmail_add_text",
            }
        ],
        [
            {
                "text": translate("bulkmail_texts_done"),
                "callback_data": "bulkmail_texts_done",
            }
        ],
        [{"text": translate("buttons.cancel"), "callback_data": "cancel"}],
    ]
    await smart_answer(
        message, bot, text, reply_markup=kb(keyboard), delete_origin=False
    )


@router.callback_query(F.data == "bulkmail_add_text", BulkMailStates.waiting_text)
async def bm_add_text_callback(event: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    texts = data.get("texts", [])
    text = translate("bulkmail_step3_text_next", count=len(texts))
    keyboard = [
        [
            {
                "text": translate("bulkmail_add_text"),
                "callback_data": "bulkmail_add_text",
            }
        ],
        [
            {
                "text": translate("bulkmail_texts_done"),
                "callback_data": "bulkmail_texts_done",
            }
        ],
        [{"text": translate("buttons.cancel"), "callback_data": "cancel"}],
    ]
    await smart_answer(event, bot, text, reply_markup=kb(keyboard), delete_origin=True)


@router.callback_query(F.data == "bulkmail_texts_done", BulkMailStates.waiting_text)
async def bm_texts_done_callback(event: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    texts = data.get("texts", [])
    if not texts:
        await smart_answer(event, bot, translate("bulkmail_no_texts"), show_alert=True)
        return
    await state.update_data(message_text=texts[0])  # For backward compatibility
    await _proceed_to_sender_selection(event, state)


async def _proceed_to_sender_selection(event, state: FSMContext):
    """Переход к выбору аккаунта-отправителя"""
    accounts_list = account_pool.accounts

    if not accounts_list:
        text = translate("bulkmail_no_accounts")
        keyboard = [
            [{"text": "50", "callback_data": "total:50"}],
            [{"text": "100", "callback_data": "total:100"}],
            [{"text": "200", "callback_data": "total:200"}],
            [{"text": "500", "callback_data": "total:500"}],
            [{"text": translate("buttons.cancel"), "callback_data": "cancel"}],
        ]
        await smart_answer(
            event, bot, text, reply_markup=kb(keyboard), delete_origin=False
        )
        await state.set_state(BulkMailStates.waiting_count)
        return

    lines = [translate("bulkmail_step4_account")]
    for i, acc in enumerate(accounts_list, 1):
        status = "🔴" if acc["in_use"] else "🟢" if acc["is_valid"] else "⚫"
        flood = translate("flood_indicator") if acc.get("flood_wait_until") else ""
        lines.append(f"{i}. {acc['session_file']} {status}{flood}")

    lines.append(f"\n<b>{translate('bulkmail_step4_account').split(':')[0]}:</b>")

    keyboard = (
        [[{"text": translate("bulkmail_auto"), "callback_data": "sender:auto"}]]
        + [
            [
                {
                    "text": f"{i}. {acc['session_file'][:20]}",
                    "callback_data": f"sender:{acc['session_file']}",
                }
            ]
            for i, acc in enumerate(accounts_list, 1)
        ]
        + [[{"text": translate("buttons.cancel"), "callback_data": "cancel"}]]
    )

    await smart_answer(
        event, bot, "\n".join(lines), reply_markup=kb(keyboard), delete_origin=False
    )
    await state.set_state(BulkMailStates.waiting_sender)


@router.callback_query(F.data.startswith("sender:"), BulkMailStates.waiting_sender)
async def bm_waiting_sender_callback(event: CallbackQuery, state: FSMContext):
    sender = event.data.split(":", 1)[1]
    await state.update_data(sender_session=sender if sender != "auto" else None)

    text = translate(
        "bulkmail_sender_selected",
        sender=sender if sender != "auto" else "авто (пул аккаунтов)",
    )
    keyboard = [
        [{"text": "50", "callback_data": "total:50"}],
        [{"text": "100", "callback_data": "total:100"}],
        [{"text": "200", "callback_data": "total:200"}],
        [{"text": "500", "callback_data": "total:500"}],
        [{"text": translate("buttons.cancel"), "callback_data": "cancel"}],
    ]
    await smart_answer(event, bot, text, reply_markup=kb(keyboard), delete_origin=True)
    await state.set_state(BulkMailStates.waiting_count)


@router.callback_query(F.data.startswith("total:"), BulkMailStates.waiting_count)
async def bm_waiting_count_callback(event: CallbackQuery, state: FSMContext):
    total = int(event.data.split(":")[1])
    await process_bulk_mailing_final(event, state, total)


@router.message(BulkMailStates.waiting_count)
async def bm_waiting_count(message: Message, state: FSMContext):
    try:
        total = int(message.text.strip())
        if total <= 0:
            raise ValueError
    except ValueError:
        await smart_answer(
            message,
            bot,
            translate("bulkmail_total_error"),
            delete_origin=False,
        )
        return

    await process_bulk_mailing_final(message, state, total)


async def process_bulk_mailing_final(event, state: FSMContext, total: int):
    data = await state.get_data()
    chats = data.get("chats", [])
    chats_source_type = data.get("chats_source_type", "manual")
    delay_min = data.get("delay_min", 1)
    delay_max = data.get("delay_max", 1)
    texts = data.get("texts", [])
    message_text = texts[0] if texts else data.get("message_text", "")
    sender_session = data.get("sender_session", None)
    user_id = event.from_user.id

    # Если используем чаты из БД
    if chats_source_type == "db":
        chats = await chat_db.get_all_chats()
        if not chats:
            await smart_answer(
                event, bot, translate("bulkmail_db_empty"), show_alert=True
            )
            await state.clear()
            return
        chats = [c["chat_url"] or c["chat_id"] for c in chats]

    if not chats:
        await smart_answer(event, bot, translate("bulkmail_no_chats"), show_alert=True)
        await state.clear()
        return

    if not texts:
        texts = [message_text]

    task_id = f"mail_{user_id}_{int(time.time())}"
    task_data = {
        "task_id": task_id,
        "user_id": user_id,
        "type": "mailing",
        "chats": chats,
        "chats_source_type": chats_source_type,
        "delay_min": delay_min,
        "delay_max": delay_max,
        "message_texts": [t[:500] for t in texts],  # Сохраняем все тексты
        "message_text_preview": (
            texts[0][:200] + "..." if len(texts[0]) > 200 else texts[0]
        ),
        "texts_count": len(texts),
        "sender_session": sender_session,
        "total_sends": total,
        "sent": 0,
        "status": "pending",
        "created_at": datetime.now().isoformat(),
        "progress": 0,
        "progress_text": "0/0",
        "priority": 1 if user_id in Config.ADMIN_USER_IDS else 0,
    }

    await tasks_storage.add(task_data)
    task_queue.add_user_task(user_id, task_id)

    await task_queue.add_task(
        task_id,
        bulk_mailing_task,
        chats=chats,
        delay_min=delay_min,
        delay_max=delay_max,
        message_texts=texts,  # Передаём список текстов
        total_sends=total,
        user_id=user_id,
        sender_session_file=sender_session,
        task_id=task_id,
    )

    texts_preview = "\n".join(
        [
            f"{i+1}. {t[:100]}{'...' if len(t) > 100 else ''}"
            for i, t in enumerate(texts[:3])
        ]
    )
    if len(texts) > 3:
        texts_preview += f"\n{translate('more_texts', count=len(texts) - 3)}"

    source_info = (
        translate("scraping_source_db_btn")
        if chats_source_type == "db"
        else translate("source_manual", count=len(chats))
    )

    text = translate(
        "task_launched",
        task_id=task_id,
        source=source_info,
        target=str(len(texts)),
        mode=f"{delay_min}-{delay_max}с",
        account=sender_session or translate("sender_placeholder"),
    )
    text += f"\n\n{translate('mailing_texts_header')}\n{texts_preview}\n\n"
    text += translate("bulkmail_sent", task_id=task_id, sent=total)
    keyboard = [
        [{"text": translate("buttons.my_tasks"), "callback_data": "my_tasks"}],
        [{"text": translate("buttons.main"), "callback_data": "start"}],
    ]

    if isinstance(event, CallbackQuery):
        await smart_answer(
            event, bot, text, reply_markup=kb(keyboard), delete_origin=True
        )
    else:
        await smart_answer(
            event, bot, text, reply_markup=kb(keyboard), delete_origin=False
        )

    await state.clear()


# --- Новый формат отображения задач ---
async def show_task_details(
    bot: Bot, user_id: int, task: Dict[str, Any], for_admin: bool = False
):
    """Показывает детали задачи в отдельном сообщении (как в VPN-боте)"""
    task_id = task.get("task_id", "unknown")
    task_type = task.get("type", "unknown")
    status = task.get("status", "unknown")
    created = task.get("created_at", "")
    progress = task.get("progress", 0)
    progress_text = task.get("progress_text", "0/0")

    # Иконки статусов
    status_icons = {
        "pending": "⏳",
        "running": "🔄",
        "completed": "✅",
        "cancelled": "❌",
        "failed": "🔥",
    }
    icon = status_icons.get(status, "❓")

    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        time_str = dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        time_str = created

    # Прогресс-бар
    progress_bar = format_progress_bar(progress)

    text = f"{icon} {translate('task_id_label')} <code>{task_id}</code>\n"
    text += f"{translate('task_type_label')} {task_type}\n"
    text += f"{translate('task_status_label')} {status}\n"
    text += f"{translate('task_created_label')} {time_str}\n"

    if task_type == "scraping":
        source = task.get("source", "")
        target = task.get("target", "")
        mode = task.get("mode", "")
        if mode == "messages":
            limit = task.get("limit", 0)
            text += f"{translate('task_source_label')} {source}\n"
            text += f"{translate('task_target_label')} {target}\n"
            text += f"{translate('task_messages_label')} {limit}\n"
        else:
            user_count = task.get("user_count", 0)
            text += f"{translate('task_source_label')} {source}\n"
            text += f"{translate('task_target_label')} {target}\n"
            text += f"{translate('task_users_label')} {user_count}\n"

    elif task_type == "mailing":
        chats = task.get("chats", [])
        total_sends = task.get("total_sends", 0)
        sent = task.get("sent", 0)
        text += f"{translate('task_chats_label')} {len(chats)}\n"
        text += f"{translate('task_sent_label')} {sent}/{total_sends}\n"

    if status in ["pending", "running", "paused"]:
        text += f"\n{translate('task_progress_label')}\n{progress_bar}\n"
        text += f"{translate('task_current_status_label')} {progress_text}"

    keyboard = []
    can_control = for_admin or task.get("user_id") == user_id

    if status == "running" and can_control:
        keyboard.append(
            [
                {
                    "text": translate("task_pause"),
                    "callback_data": f"pause_task:{task_id}",
                },
                {
                    "text": translate("task_cancel_btn"),
                    "callback_data": f"cancel_task_id:{task_id}",
                },
            ]
        )
    elif status == "paused" and can_control:
        keyboard.append(
            [
                {
                    "text": translate("task_resume"),
                    "callback_data": f"resume_task:{task_id}",
                },
                {
                    "text": translate("task_cancel_btn"),
                    "callback_data": f"cancel_task_id:{task_id}",
                },
            ]
        )
    elif status == "pending" and can_control:
        keyboard.append(
            [
                {
                    "text": translate("task_cancel_btn"),
                    "callback_data": f"cancel_task_id:{task_id}",
                }
            ]
        )

    # Ручной выход из чатов после завершения/отмены (только если авто-выход выключен)
    if (
        status in ["completed", "cancelled", "failed"]
        and not Config.AUTO_LEAVE_AFTER_INVITE
    ):
        joined_chats = task.get("joined_chats") or []
        if joined_chats:
            keyboard.append(
                [
                    {
                        "text": translate("task_leave_chats"),
                        "callback_data": f"leave_chats:{task_id}",
                    }
                ]
            )

    keyboard.append(
        [
            {
                "text": translate("task_refresh"),
                "callback_data": f"refresh_task:{task_id}",
            }
        ]
    )

    # Для администратора - информация о пользователе
    if for_admin:
        task_user_id = task.get("user_id")
        keyboard.append(
            [
                {
                    "text": translate("user_info_label", user_id=task_user_id),
                    "callback_data": f"user_info:{task_user_id}",
                }
            ]
        )

    keyboard.append(
        [
            {
                "text": translate("task_all_tasks"),
                "callback_data": "task_list" if for_admin else "my_tasks",
            }
        ]
    )
    keyboard.append([{"text": translate("buttons.main"), "callback_data": "start"}])

    return text, kb(keyboard)


@router.callback_query(F.data == "my_tasks")
async def cmd_my_tasks(event: CallbackQuery):
    user_id = event.from_user.id
    tasks = await tasks_storage.read_all()
    user_tasks = [t for t in tasks if t.get("user_id") == user_id]

    if not user_tasks:
        text = translate("no_tasks")
        keyboard = [
            [
                {
                    "text": translate("buttons.start_scraping"),
                    "callback_data": "start_scraping",
                }
            ],
            [
                {
                    "text": translate("buttons.bulk_mailing"),
                    "callback_data": "bulk_mailing",
                }
            ],
            [{"text": translate("buttons.main"), "callback_data": "start"}],
        ]
        await smart_answer(
            event, bot, text, reply_markup=kb(keyboard), delete_origin=True
        )
        return

    # Разделяем задачи на активные и неактивные
    active_tasks = [
        t for t in user_tasks if t.get("status") in ["pending", "running", "paused"]
    ]
    inactive_tasks = [
        t for t in user_tasks if t.get("status") not in ["pending", "running", "paused"]
    ]

    # Сначала показываем активные задачи
    if active_tasks:
        for task in active_tasks[-5:]:  # Последние 5 активных задач
            text, keyboard = await show_task_details(
                bot, user_id, task, for_admin=False
            )
            await smart_answer(
                event, bot, text, reply_markup=keyboard, delete_origin=True
            )
    else:
        text = translate("no_active_tasks")
        keyboard = [
            [
                {
                    "text": translate("buttons.start_scraping"),
                    "callback_data": "start_scraping",
                }
            ],
            [
                {
                    "text": translate("buttons.bulk_mailing"),
                    "callback_data": "bulk_mailing",
                }
            ],
            [{"text": translate("buttons.main"), "callback_data": "start"}],
        ]
        await smart_answer(
            event, bot, text, reply_markup=kb(keyboard), delete_origin=True
        )

    # Показываем неактивные задачи списком (если есть)
    if inactive_tasks:
        task_lines = []
        for i, task in enumerate(inactive_tasks[-10:], 1):
            task_id = task.get("task_id", "unknown")
            task_type = task.get("type", "unknown")
            status = task.get("status", "unknown")

            status_icons = {
                "completed": "✅",
                "cancelled": "❌",
                "failed": "🔥",
            }
            icon = status_icons.get(status, "❓")

            task_lines.append(
                f"{i}. {icon} <code>{task_id[:10]}...</code> - {task_type} - {status}"
            )
        tasks_text = "\n".join(task_lines)

        text = translate("task_list_header", tasks=tasks_text)
        keyboard = [
            [
                {
                    "text": translate("task_all_tasks_btn"),
                    "callback_data": (
                        "task_list" if user_id in Config.ADMIN_USER_IDS else "my_tasks"
                    ),
                }
            ],
            [{"text": translate("buttons.main"), "callback_data": "start"}],
        ]

        await smart_answer(
            event, bot, text, reply_markup=kb(keyboard), delete_origin=True
        )


@router.callback_query(F.data == "task_list")
async def cmd_task_list(event: CallbackQuery):
    user_id = event.from_user.id
    if user_id not in Config.ADMIN_USER_IDS:
        await smart_answer(
            event,
            bot,
            translate("task_list_only_admin"),
            show_alert=True,
        )
        return

    tasks = await tasks_storage.read_all()
    active_tasks = [
        t for t in tasks if t.get("status") in ["pending", "running", "paused"]
    ]

    if not active_tasks:
        text = translate("no_tasks_list")
        keyboard = [[{"text": translate("buttons.main"), "callback_data": "start"}]]
        await smart_answer(
            event, bot, text, reply_markup=kb(keyboard), delete_origin=True
        )
        return

    # Показываем каждую активную задачу в отдельном сообщении
    for task in active_tasks[-10:]:  # Последние 10 активных задач
        text, keyboard = await show_task_details(bot, user_id, task, for_admin=True)
        await smart_answer(event, bot, text, reply_markup=keyboard, delete_origin=True)


@router.callback_query(F.data.startswith("refresh_task:"))
async def refresh_task(event: CallbackQuery):
    task_id = event.data.split(":", 1)[1]
    task = await tasks_storage.find_by_id(task_id, id_field="task_id")

    if not task:
        await smart_answer(event, bot, translate("task_not_found"), show_alert=True)
        return

    user_id = event.from_user.id
    for_admin = user_id in Config.ADMIN_USER_IDS

    text, keyboard = await show_task_details(bot, user_id, task, for_admin=for_admin)
    await smart_answer(event, bot, text, reply_markup=keyboard, delete_origin=True)


@router.callback_query(F.data.startswith("pause_task:"))
async def process_pause_task(event: CallbackQuery):
    task_id = event.data.split(":", 1)[1]
    task_data = await tasks_storage.find_by_id(task_id, id_field="task_id")
    if not task_data:
        await smart_answer(event, bot, translate("task_not_found"), show_alert=True)
        return

    user_id = event.from_user.id
    task_user_id = task_data.get("user_id")
    if user_id not in Config.ADMIN_USER_IDS and user_id != task_user_id:
        await smart_answer(event, bot, translate("pause_no_rights"), show_alert=True)
        return

    await task_queue.pause_task(task_id)
    await smart_answer(
        event, bot, translate("task_paused", task_id=task_id), show_alert=True
    )

    updated_task = await tasks_storage.find_by_id(task_id, id_field="task_id")
    text, keyboard = await show_task_details(
        bot, user_id, updated_task, for_admin=(user_id in Config.ADMIN_USER_IDS)
    )
    await smart_answer(event, bot, text, reply_markup=keyboard, delete_origin=True)


@router.callback_query(F.data.startswith("resume_task:"))
async def process_resume_task(event: CallbackQuery):
    task_id = event.data.split(":", 1)[1]
    task_data = await tasks_storage.find_by_id(task_id, id_field="task_id")
    if not task_data:
        await smart_answer(event, bot, translate("task_not_found"), show_alert=True)
        return

    user_id = event.from_user.id
    task_user_id = task_data.get("user_id")
    if user_id not in Config.ADMIN_USER_IDS and user_id != task_user_id:
        await smart_answer(event, bot, translate("resume_no_rights"), show_alert=True)
        return

    if task_id not in task_queue.active_tasks:
        await queue_task_from_storage(task_data, resume=True)

    await task_queue.resume_task(task_id)
    await smart_answer(
        event, bot, translate("task_resumed", task_id=task_id), show_alert=True
    )

    updated_task = await tasks_storage.find_by_id(task_id, id_field="task_id")
    text, keyboard = await show_task_details(
        bot, user_id, updated_task, for_admin=(user_id in Config.ADMIN_USER_IDS)
    )
    await smart_answer(event, bot, text, reply_markup=keyboard, delete_origin=True)


@router.callback_query(F.data.startswith("cancel_task_id:"))
async def process_cancel_task(event: CallbackQuery):
    task_id = event.data.split(":", 1)[1]

    task_data = await tasks_storage.find_by_id(task_id, id_field="task_id")
    if not task_data:
        await smart_answer(event, bot, translate("task_not_found"), show_alert=True)
        return

    user_id = event.from_user.id
    task_user_id = task_data.get("user_id")

    # Проверяем права
    if user_id not in Config.ADMIN_USER_IDS and user_id != task_user_id:
        await smart_answer(event, bot, translate("cancel_no_rights"), show_alert=True)
        return

    # Обновляем статус задачи
    await tasks_storage.update_by_id(
        task_id,
        {
            "status": "cancelled",
            "cancelled_at": datetime.now().isoformat(),
            "cancelled_by": user_id,
        },
        id_field="task_id",
    )

    # Пытаемся отменить задачу в очереди
    cancelled = await task_queue.cancel_task(task_id)

    # Удаляем задачу из пользовательских
    task_queue.remove_user_task(task_user_id, task_id)

    # Уведомляем пользователя
    if user_id == task_user_id:
        await notify_user(bot, user_id, translate("task_cancelled", task_id=task_id))
    else:
        await notify_user(
            bot, task_user_id, translate("task_cancelled", task_id=task_id)
        )
        await notify_user(
            bot,
            user_id,
            translate("task_cancelled_by_admin", task_id=task_id, user_id=task_user_id),
        )

    await smart_answer(
        event,
        bot,
        translate("task_cancelled_confirm", task_id=task_id),
        show_alert=True,
    )

    # Обновляем отображение задачи
    task_data["status"] = "cancelled"
    text, keyboard = await show_task_details(
        bot, user_id, task_data, for_admin=(user_id in Config.ADMIN_USER_IDS)
    )
    await smart_answer(event, bot, text, reply_markup=keyboard, delete_origin=True)


@router.callback_query(F.data.startswith("leave_chats:"))
async def process_leave_chats(event: CallbackQuery):
    task_id = event.data.split(":", 1)[1]
    task_data = await tasks_storage.find_by_id(task_id, id_field="task_id")
    if not task_data:
        await smart_answer(event, bot, translate("task_not_found"), show_alert=True)
        return

    user_id = event.from_user.id
    task_user_id = task_data.get("user_id")
    if user_id not in Config.ADMIN_USER_IDS and user_id != task_user_id:
        await smart_answer(event, bot, translate("no_rights"), show_alert=True)
        return

    joined_chats = task_data.get("joined_chats") or []
    if not joined_chats:
        await smart_answer(event, bot, translate("leave_nothing"), show_alert=True)
        return

    sender_session = task_data.get("sender_session")
    if not sender_session:
        await smart_answer(
            event,
            bot,
            translate("leave_no_sender"),
            show_alert=True,
        )
        return

    left = []
    errors = []
    try:
        async with account_pool.acquire_specific_account(sender_session) as account:
            client = account["client"]
            for chat in joined_chats:
                try:
                    await ensure_leave_target(client, chat)
                    left.append(chat)
                except Exception as e:
                    errors.append((chat, str(e)))
    except Exception as e:
        await smart_answer(
            event, bot, translate("leave_error", error=e), show_alert=True
        )
        return

    try:
        await tasks_storage.update_by_id(
            task_id,
            {"joined_chats": [], "left_at": datetime.now().isoformat()},
            id_field="task_id",
        )
    except Exception:
        pass

    msg_lines = [translate("leave_done")]
    if left:
        msg_lines.append("• " + translate("left_chats") + ": " + ", ".join(left))
    if errors:
        msg_lines.append(
            "• "
            + translate("errors")
            + ": "
            + "; ".join([f"{c} ({e})" for c, e in errors])
        )
    await smart_answer(event, bot, "\n".join(msg_lines), show_alert=True)


@router.callback_query(F.data == "task_stats")
async def cmd_task_stats(event: CallbackQuery):
    tasks = await tasks_storage.read_all()

    total = len(tasks)
    pending = len([t for t in tasks if t.get("status") == "pending"])
    running = len([t for t in tasks if t.get("status") == "running"])
    paused = len([t for t in tasks if t.get("status") == "paused"])
    completed = len([t for t in tasks if t.get("status") == "completed"])
    cancelled = len([t for t in tasks if t.get("status") == "cancelled"])
    failed = len([t for t in tasks if t.get("status") == "failed"])

    scraping = len([t for t in tasks if t.get("type") == "scraping"])
    mailing = len([t for t in tasks if t.get("type") == "mailing"])

    stats = translate(
        "task_stats",
        total=total,
        pending=pending,
        running=running,
        paused=paused,
        completed=completed,
        cancelled=cancelled,
        failed=failed,
    )

    keyboard = [
        [{"text": translate("buttons.task_list"), "callback_data": "task_list"}],
        [{"text": translate("task_refresh"), "callback_data": "task_stats"}],
        [{"text": translate("buttons.main"), "callback_data": "start"}],
    ]

    await smart_answer(event, bot, stats, reply_markup=kb(keyboard), delete_origin=True)


@router.callback_query(F.data == "clear_cache")
async def cmd_clear_cache(event: CallbackQuery, state: FSMContext):
    user_id = event.from_user.id
    if user_id not in Config.ADMIN_USER_IDS:
        await smart_answer(event, bot, translate("only_admin"), show_alert=True)
        return

    text = translate("clear_cache_confirm")
    keyboard = [
        [
            {
                "text": translate("buttons.confirm_yes"),
                "callback_data": "clear_cache_confirm",
            }
        ],
        [{"text": translate("buttons.confirm_no"), "callback_data": "start"}],
    ]
    await smart_answer(event, bot, text, reply_markup=kb(keyboard), delete_origin=True)


@router.callback_query(F.data == "clear_cache_confirm")
async def process_clear_cache_confirm(event: CallbackQuery):
    cleared = await cache_manager.clear_cache()
    text = translate("cache_cleared", count=cleared)
    keyboard = [[{"text": translate("buttons.main"), "callback_data": "start"}]]
    await smart_answer(event, bot, text, reply_markup=kb(keyboard), delete_origin=True)


# === Отменить все задачи ===
@router.callback_query(F.data == "cancel_all_tasks")
async def cmd_cancel_all_tasks(event: CallbackQuery):
    if event.from_user.id not in Config.ADMIN_USER_IDS:
        await smart_answer(event, bot, translate("only_admin"), show_alert=True)
        return

    text = translate("task_cancel_confirm")
    keyboard = [
        [
            {
                "text": translate("task_confirm_yes"),
                "callback_data": "cancel_all_tasks_confirm",
            }
        ],
        [{"text": translate("task_confirm_no"), "callback_data": "start"}],
    ]
    await smart_answer(event, bot, text, reply_markup=kb(keyboard), delete_origin=True)


@router.callback_query(F.data == "cancel_all_tasks_confirm")
async def process_cancel_all_tasks(event: CallbackQuery):
    if event.from_user.id not in Config.ADMIN_USER_IDS:
        await smart_answer(event, bot, translate("only_admin"), show_alert=True)
        return

    cancelled = 0
    for task_id in list(task_queue.active_tasks.keys()):
        if await task_queue.cancel_task(task_id):
            cancelled += 1

    # Отменяем также все pending задачи
    tasks = await tasks_storage.read_all()
    for task in tasks:
        if task.get("status") in ("pending", "running", "paused"):
            tid = task.get("task_id")
            if tid and tid not in task_queue.active_tasks:
                await tasks_storage.update_by_id(
                    tid,
                    {"status": "cancelled", "cancelled_at": datetime.now().isoformat()},
                    id_field="task_id",
                )
                cancelled += 1

    text = translate("tasks_cancelled", count=cancelled)
    keyboard = [[{"text": translate("buttons.main"), "callback_data": "start"}]]
    await smart_answer(event, bot, text, reply_markup=kb(keyboard), delete_origin=True)


# === Добавить чаты в БД ===
@router.callback_query(F.data == "add_chats_to_db")
async def cmd_add_chats_to_db(event: CallbackQuery, state: FSMContext):
    if not account_pool.accounts:
        text = translate("worm_no_account")
        keyboard = [
            [
                {
                    "text": translate("buttons.add_account"),
                    "callback_data": "add_account",
                }
            ],
            [{"text": translate("buttons.main"), "callback_data": "start"}],
        ]
        await smart_answer(
            event, bot, text, reply_markup=kb(keyboard), delete_origin=True
        )
        return

    text = translate("worm_waiting_links")
    keyboard = [[{"text": translate("buttons.main"), "callback_data": "start"}]]
    await smart_answer(event, bot, text, reply_markup=kb(keyboard), delete_origin=True)
    await state.set_state(AddChatsStates.waiting_links)


@router.message(AddChatsStates.waiting_links)
async def process_add_chats_links(message: Message, state: FSMContext):
    await _process_chats_input(message, state, source="message")


async def _process_chats_input(
    event, state: FSMContext, source: str = "message"
) -> None:
    """Обработка ссылок — из сообщения или файла"""
    raw_links: List[str] = []

    if source == "message":
        text = (getattr(event, "text", None) or "").strip()
        if not text:
            await smart_answer(
                event, bot, translate("empty_input"), delete_origin=False
            )
            return
        # Разбиваем по пробелам, запятым, новым строкам
        parts = re.split(r"[\s,;\n]+", text)
        for part in parts:
            part = part.strip()
            if not part:
                continue
            # Убедимся что это ссылка или username
            if part.startswith("http"):
                raw_links.append(part)
            elif part.startswith("t.me/"):
                raw_links.append(f"https://{part}")
            elif part.startswith("@"):
                raw_links.append(f"https://t.me/{part[1:]}")
            elif re.match(r"^[A-Za-z0-9_]{2,32}$", part):
                raw_links.append(f"https://t.me/{part}")

    elif source == "file":
        # Читаем файл
        if not event.document:
            await smart_answer(
                event, bot, translate("invalid_format"), delete_origin=False
            )
            return
        file = await event.download()
        content = file.read().decode("utf-8", errors="ignore")
        parts = re.split(r"[\s,;\n]+", content)
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if part.startswith("http"):
                raw_links.append(part)
            elif part.startswith("t.me/"):
                raw_links.append(f"https://{part}")
            elif part.startswith("@"):
                raw_links.append(f"https://t.me/{part[1:]}")
            elif re.match(r"^[A-Za-z0-9_]{2,32}$", part):
                raw_links.append(f"https://t.me/{part}")

    if not raw_links:
        await smart_answer(event, bot, translate("empty_input"), delete_origin=False)
        return

    # Убираем дубликаты
    seen: Set[str] = set()
    unique_links: List[str] = []
    for link in raw_links:
        parsed = parse_link_to_identifier(link)
        if parsed and parsed not in seen:
            seen.add(parsed)
            unique_links.append(link)

    if not unique_links:
        await smart_answer(event, bot, translate("empty_input"), delete_origin=False)
        return

    # Получаем аккаунт
    acc = account_pool.accounts[0]
    for a in account_pool.accounts:
        if not a["in_use"] and a["is_valid"]:
            acc = a
            break

    if not acc["client"] or not acc["client"].is_connected():
        try:
            acc["client"] = await account_pool._create_client(acc["session_string"])
            await acc["client"].get_me()
        except Exception as e:
            logger.error(
                f"Не удалось подключиться к аккаунту {acc['session_file']}: {e}"
            )
            await smart_answer(
                event, bot, translate("no_available_accounts"), delete_origin=False
            )
            return

    client = acc["client"]
    added = 0
    errors = 0
    results_text: List[str] = []

    for link in unique_links[:50]:  # Лимит 50 чатов за раз
        identifier = parse_link_to_identifier(link)
        if not identifier:
            errors += 1
            continue

        try:
            result = await validate_and_test_chat(
                client,
                acc,
                identifier,
            )
            if result:
                success = await chat_db.add_chat(
                    result["chat_id"],
                    result["chat_name"],
                    result["chat_url"],
                    result["chat_type"],
                    result["user_count"],
                )
                if success:
                    added += 1
                    results_text.append(
                        translate(
                            "chat_added_result",
                            chat_name=result["chat_name"],
                            user_count=result["user_count"],
                        )
                    )
                else:
                    errors += 1
            else:
                errors += 1
        except Exception as e:
            errors += 1
            logger.error(f"Ошибка обработки чата {link}: {e}")

    output = translate("chats_added_title", count=len(unique_links))
    output += f"\n\n• {translate('added_to_db')}: {added}\n• {translate('errors')}: {errors}\n\n"
    if results_text:
        output += translate("added_chats_header") + "\n" + "\n".join(results_text[:20])
        if len(results_text) > 20:
            output += f"\n\n{translate('more_results', count=len(results_text) - 20)}"

    keyboard = [
        [{"text": translate("buttons.main"), "callback_data": "start"}],
    ]
    await smart_answer(
        event, bot, output, reply_markup=kb(keyboard), delete_origin=False
    )
    await state.clear()


# === Обновить БД чатов ===
@router.callback_query(F.data == "update_chats_db")
async def cmd_update_chats_db(event: CallbackQuery):
    if event.from_user.id not in Config.ADMIN_USER_IDS:
        await smart_answer(event, bot, translate("only_admin"), show_alert=True)
        return

    if not account_pool.accounts:
        await smart_answer(
            event, bot, translate("no_available_accounts"), show_alert=True
        )
        return

    chats = await chat_db.get_all_chats()
    total = len(chats)
    if total == 0:
        await smart_answer(event, bot, translate("no_chats_db"), show_alert=True)
        return

    text = translate("update_db_start", total=total)
    keyboard = [[{"text": translate("buttons.main"), "callback_data": "start"}]]
    msg = await smart_answer(
        event, bot, text, reply_markup=kb(keyboard), delete_origin=True
    )

    # Запускаем проверку в фоне
    asyncio.create_task(_run_update_chats_db(msg, event.from_user.id))


async def _run_update_chats_db(msg: Any, user_id: int):
    """Фоновая проверка всех чатов в БД"""
    if not account_pool.accounts:
        return

    acc = None
    for a in account_pool.accounts:
        if not a["in_use"] and a["is_valid"]:
            acc = a
            break
    if not acc:
        return

    client = acc["client"]
    if not client or not client.is_connected():
        try:
            client = await account_pool._create_client(acc["session_string"])
        except Exception:
            return

    result = await check_and_clean_banned_chats(client, bot, user_id)
    chats = await chat_db.get_all_chats()
    total_users = await chat_db.get_total_users()

    text = translate(
        "update_db_done",
        checked=result["checked"],
        added=0,
        removed=result["removed"],
        errors=result["errors"],
    )
    text += (
        f"\n\n{translate('current_stats', chats=len(chats), total_users=total_users)}"
    )

    try:
        if isinstance(msg, Message) and msg and msg.chat:
            await msg.edit_text(text, parse_mode="HTML")
        elif isinstance(msg, CallbackQuery) and msg.message:
            await msg.message.edit_text(text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка обновления сообщения при обновлении БД: {e}")


# === Режим червя ===
@router.callback_query(F.data == "worm_mode")
async def cmd_worm_mode(event: CallbackQuery, state: FSMContext):
    if not account_pool.accounts:
        text = translate("worm_no_account")
        keyboard = [
            [
                {
                    "text": translate("buttons.add_account"),
                    "callback_data": "add_account",
                }
            ],
            [{"text": translate("buttons.main"), "callback_data": "start"}],
        ]
        await smart_answer(
            event, bot, text, reply_markup=kb(keyboard), delete_origin=True
        )
        return

    text = translate("worm_waiting_chat")
    keyboard = [[{"text": translate("buttons.main"), "callback_data": "start"}]]
    await smart_answer(event, bot, text, reply_markup=kb(keyboard), delete_origin=True)
    await state.set_state(WormModeStates.waiting_sources)


@router.callback_query(F.data == "stop_worm")
async def cmd_stop_worm(event: CallbackQuery):
    global _worm_active, _worm_task

    if not _worm_active:
        await smart_answer(event, bot, translate("invalid_format"), show_alert=True)
        return

    _worm_active = False
    if _worm_task:
        _worm_task.cancel()
        try:
            await _worm_task
        except asyncio.CancelledError:
            pass
        _worm_task = None

    async with _worm_lock:
        stats = dict(_worm_stats)

    # Считаем общую статистику
    total_messages = sum(
        s.get("messages", 0) for s in stats.values() if isinstance(s, dict)
    )
    total_links = sum(s.get("links", 0) for s in stats.values() if isinstance(s, dict))
    total_added = sum(s.get("added", 0) for s in stats.values() if isinstance(s, dict))
    total_errors = sum(
        s.get("errors", 0) for s in stats.values() if isinstance(s, dict)
    )

    text = translate(
        "worm_stopped",
        messages=total_messages,
        links=total_links,
        added=total_added,
        errors=total_errors,
    )
    keyboard = [[{"text": translate("buttons.main"), "callback_data": "start"}]]
    await smart_answer(event, bot, text, reply_markup=kb(keyboard), delete_origin=True)

    async with _worm_lock:
        _worm_stats = {}


@router.message(WormModeStates.waiting_sources)
async def process_worm_sources(message: Message, state: FSMContext):
    global _worm_active, _worm_task, _worm_chat_id, _worm_stats

    if _worm_active:
        await smart_answer(
            message,
            bot,
            translate("worm_already_running", chat=_worm_chat_id),
            delete_origin=False,
        )
        return

    # Парсим несколько источников (каждый с новой строки или разделённый пробелами)
    raw_sources = message.text.strip()
    if not raw_sources:
        await smart_answer(message, bot, translate("empty_input"), delete_origin=False)
        return

    # Разбиваем по строкам, запятым, пробелам
    raw_list = re.split(r"[\s,;\n]+", raw_sources)

    # Парсим каждый источник
    sources: List[str] = []
    for raw in raw_list:
        raw = raw.strip()
        if not raw:
            continue
        identifier = parse_link_to_identifier(raw)
        if identifier:
            sources.append(identifier)
        else:
            logger.warning(f"Не удалось распознать источник: {raw}")

    if not sources:
        await smart_answer(
            message, bot, translate("invalid_format"), delete_origin=False
        )
        return

    # Ограничиваем количество источников
    if len(sources) > Config.WORM_MAX_CONCURRENT:
        await smart_answer(
            message,
            bot,
            translate("worm_max_sources", count=Config.WORM_MAX_CONCURRENT),
            delete_origin=False,
        )
        sources = sources[: Config.WORM_MAX_CONCURRENT]

    # Проверяем аккаунт
    acc = None
    for a in account_pool.accounts:
        if not a["in_use"] and a["is_valid"]:
            acc = a
            break
    if not acc:
        await smart_answer(
            message, bot, translate("no_available_accounts"), delete_origin=False
        )
        return

    client = acc["client"]
    if not client or not client.is_connected():
        try:
            acc["client"] = await account_pool._create_client(acc["session_string"])
            await acc["client"].get_me()
            client = acc["client"]
        except Exception as e:
            logger.error(f"Не удалось подключиться к аккаунту: {e}")
            await smart_answer(
                message, bot, translate("no_available_accounts"), delete_origin=False
            )
            return

    # Запускаем червя
    _worm_active = True
    _worm_chat_id = ", ".join(sources[:3]) + ("..." if len(sources) > 3 else "")
    async with _worm_lock:
        _worm_stats = {
            s: {"messages": 0, "links": 0, "added": 0, "errors": 0} for s in sources
        }

    text = translate(
        "worm_started_multi", chats=len(sources), sources=", ".join(sources[:3])
    )
    keyboard = [
        [{"text": translate("buttons.stop_worm"), "callback_data": "stop_worm"}],
        [{"text": translate("buttons.main"), "callback_data": "start"}],
    ]
    await smart_answer(
        message, bot, text, reply_markup=kb(keyboard), delete_origin=False
    )

    await state.clear()

    # Запускаем фоновую задачу
    _worm_task = asyncio.create_task(
        _run_worm_mode(client, acc, sources, message.from_user.id)
    )


async def _run_worm_mode(
    client: TelegramClient,
    account: Dict[str, Any],
    sources: List[str],
    user_id: int,
):
    """
    Основная логика режима червя.
    sources: список чатов/каналов для сканирования.
    Работает по кругу:
      1. Сканирует сообщения в чате
      2. Находит ссылки → валидирует
      3. Валидатор сам записывает в БД
      4. После полной проверки чата — кидает «копии» в новые валидные чаты
      5. Повторяет по кругу пока не остановлен или нет новых ссылок
    """
    global _worm_active, _worm_stats

    # Инициализация статистики для каждого источника
    for source in sources:
        _worm_stats[source] = {"messages": 0, "links": 0, "added": 0, "errors": 0}

    logger.info(f"🐛 Червь запущен в {len(sources)} чатах: {sources}")

    processed_count = 0
    new_valid_chats: List[Dict[str, Any]] = []  # Новые валидные чаты для копий

    while _worm_active:
        all_valid_empty = True  # Флаг: есть ли новые валидные чаты

        try:
            # Проходим по всем источникам
            for source in sources:
                if not _worm_active:
                    break

                try:
                    entity = await client.get_entity(source)
                except Exception as e:
                    logger.warning(f"Не удалось получить источник {source}: {e}")
                    continue

                # Сканируем сообщения (лимит из .env)
                try:
                    msg_count = 0
                    async for message in client.iter_messages(
                        entity, limit=Config.WORM_SCAN_LIMIT, reverse=True
                    ):
                        if not _worm_active:
                            break

                        try:
                            text = getattr(message, "text", None) or ""
                            if not text.strip():
                                continue

                            async with _worm_lock:
                                _worm_stats[source]["messages"] += 1
                                processed_count += 1

                            # Проверка статуса каждые N сообщений
                            if processed_count % Config.WORM_CHECK_INTERVAL == 0:
                                logger.info(
                                    f"🐛 Червь в {source}: {_worm_stats[source]}"
                                )

                            links = extract_links_from_text(text)
                            if not links:
                                continue

                            async with _worm_lock:
                                _worm_stats[source]["links"] += len(links)

                            # Валидируем каждую ссылку
                            for link in links:
                                if not _worm_active:
                                    break

                                identifier = parse_link_to_identifier(link)
                                if not identifier:
                                    continue

                                try:
                                    result = await validate_and_test_chat(
                                        client,
                                        account,
                                        identifier,
                                    )
                                    if result:
                                        async with _worm_lock:
                                            _worm_stats[source]["added"] += 1
                                        new_valid_chats.append(result)
                                except Exception as e:
                                    async with _worm_lock:
                                        _worm_stats[source]["errors"] += 1
                                    logger.error(f"Ошибка обработки ссылки {link}: {e}")

                                # Анти-флуд
                                await asyncio.sleep(
                                    random.uniform(
                                        Config.WORM_MIN_DELAY, Config.WORM_MAX_DELAY
                                    )
                                )

                        except Exception as e:
                            logger.error(f"Ошибка обработки сообщения: {e}")
                            await asyncio.sleep(1)

                    msg_count += 1

                except Exception as e:
                    logger.error(f"Ошибка сканирования {source}: {e}")
                    await asyncio.sleep(5)

            # После полной проверки всех источников — кидаем «копии» в новые чаты
            if new_valid_chats:
                logger.info(
                    f"🐛 Червь: {len(new_valid_chats)} новых валидных чатов для копий"
                )
                for new_chat in new_valid_chats:
                    if not _worm_active:
                        break
                    try:
                        new_entity = await client.get_entity(new_chat["chat_url"])
                        # Отправляем тестовое сообщение (копию)
                        await client.send_message(
                            new_entity, "🐛 Message from Inviter Bot Worm Mode"
                        )
                        logger.info(f"🐛 Копия отправлена в {new_chat['chat_name']}")
                        await asyncio.sleep(random.uniform(3, 7))
                    except Exception as e:
                        logger.error(
                            f"Ошибка отправки копии в {new_chat['chat_name']}: {e}"
                        )

                new_valid_chats.clear()

        except Exception as e:
            logger.error(f"Ошибка режима червя: {e}")
            await asyncio.sleep(10)

    # Финальная статистика
    logger.info(f"🐛 Червь остановлен. Итоговая статистика: {_worm_stats}")
    async with _worm_lock:
        _worm_chat_id = None


# === Исправленные задачи ---
async def ensure_join_target(
    client: TelegramClient,
    target: str,
    account: Dict[str, Any],
    task_id: str,
    user_id: int,
):
    """
    Бот заходит в целевой чат/канал.
    Ниоткуда не выходит до окончания таска.
    """
    try:
        if target.startswith("https://t.me/+"):
            invite_hash = target.split("+", 1)[1]
            await client(functions.messages.ImportChatInviteRequest(invite_hash))
        else:
            entity = await client.get_entity(target)
            try:
                await client(functions.channels.JoinChannelRequest(entity))
            except Exception as join_err:
                if "USER_ALREADY_PARTICIPANT" not in str(join_err).upper():
                    raise
        entity = await client.get_entity(target)
        logger.info(f"Account {account['session_file']} joined {target}")
        # сохраняем joined chat в задачу
        try:
            task = await tasks_storage.find_by_id(task_id, id_field="task_id")
            if task is not None:
                joined = task.get("joined_chats", [])
                if target not in joined:
                    joined.append(target)
                    await tasks_storage.update_by_id(
                        task_id, {"joined_chats": joined}, id_field="task_id"
                    )
        except Exception as upd_err:
            logger.warning(f"Failed to store joined chat for task {task_id}: {upd_err}")
        return entity
    except Exception as e:
        logger.warning(f"Failed to join target {target}: {e}")
        await tasks_storage.update_by_id(
            task_id,
            {
                "status": "failed",
                "error": translate("join_error", target=target, error=str(e)),
                "completed_at": datetime.now().isoformat(),
            },
            id_field="task_id",
        )
        await notify_user(bot, user_id, translate("no_users_found", source=target))
        return None


async def ensure_join_source(
    client: TelegramClient,
    source: str,
    account: Dict[str, Any],
    task_id: str,
):
    """
    Бот заходит в исходный чат/канал для сканирования.
    """
    try:
        entity = await client.get_entity(source)
        logger.info(f"Account {account['session_file']} joined source {source}")
        return entity
    except Exception as e:
        logger.warning(f"Failed to join source {source}: {e}")
        return None


async def ensure_leave_target(client: TelegramClient, target: str):
    """
    Выход из чата (НЕ используется в новой логике).
    """
    try:
        entity = await client.get_entity(target)
        if isinstance(entity, telethon_types.Channel):
            await client(functions.channels.LeaveChannelRequest(entity))
        else:
            try:
                await client(
                    functions.messages.DeleteChatUserRequest(
                        chat_id=entity.id, user_id="me"
                    )
                )
            except Exception:
                pass
        logger.info(f"Left target {target}")
    except Exception as e:
        logger.warning(f"Failed to leave target {target}: {e}")


async def get_active_users(
    control: TaskControl,
    client: TelegramClient,
    source_entity: str,
    limit: int,
    task_id: str,
    stop_after: Optional[int] = None,
) -> List[int]:
    logger.info(f"Collecting users from: {source_entity}")
    users: Set[int] = set()
    # Попробуем взять из кэша, если ранее уже собирали этот чат
    cached_source = await cache_manager.get_cached_participants(source_entity)
    try:
        entity = await client.get_entity(source_entity)
        processed = 0

        # Асинхронный сбор с параллельной обработкой
        messages_buffer = []
        batch_size = 100

        async for message in client.iter_messages(entity, limit=limit):
            if control.cancelled:
                break
            await control.pause_event.wait()

            if message and message.sender_id:
                try:
                    sender = await message.get_sender()
                    if isinstance(sender, telethon_types.User) and not sender.bot:
                        users.add(sender.id)
                except Exception:
                    users.add(message.sender_id)

            messages_buffer.append(message)
            processed += 1

            # Периодическое обновление прогресса
            if processed % batch_size == 0:
                progress = min(95, (processed / max(1, limit)) * 100)
                progress_text = format_progress_bar(progress)
                await tasks_storage.update_by_id(
                    task_id,
                    {
                        "progress": progress,
                        "progress_text": translate(
                            "progress_scraping",
                            progress_text=progress_text,
                            processed=processed,
                            limit=limit,
                        ),
                        "checkpoints": {
                            "phase": "collect",
                            "processed": processed,
                            "collected": len(users),
                            "source": source_entity,
                        },
                    },
                    id_field="task_id",
                )

            if stop_after and len(users) >= stop_after:
                break

        logger.info(f"Collected {len(users)} users from {source_entity}")
        # Кэшируем собранных (для повторного использования при отсутствии прав в будущем)
        try:
            await cache_manager.cache_participants(source_entity, list(users))
        except Exception as e:
            logger.debug(f"Could not cache collected users for {source_entity}: {e}")
        return list(users)
    except (ChatAdminRequiredError, ChannelPrivateError) as e:
        logger.warning(f"No access to messages in {source_entity}: {e}")
        if cached_source:
            logger.info(
                f"Using cached users for {source_entity}: {len(cached_source)} entries"
            )
            if stop_after:
                return cached_source[:stop_after]
            return cached_source
        raise
    except AuthKeyUnregisteredError as e:
        logger.error(f"AuthKeyUnregisteredError: {e}")
        raise
    except Exception as e:
        logger.error(f"Error collecting users: {e}")
        raise


async def invite_users(
    control: TaskControl,
    account: Dict[str, Any],
    client: TelegramClient,
    user_ids: List[int],
    target_entity: str,
    task_id: str,
    user_id: int,
    source: Optional[str] = None,
) -> Tuple[Dict[str, int], List[int]]:
    logger.info(f"Inviting {len(user_ids)} users to {target_entity}")
    results = {"success": 0, "failed": 0, "privacy_errors": 0, "already_members": 0}
    remaining_users: List[int] = []
    total_users = len(user_ids)
    processed = 0
    invite_buffer = []
    buffer_size = 10  # Буферизация инвайтов

    # Защита от детекта: 5% шанс пропустить пользователя (имитация ошибки)
    def should_skip_user():
        return random.random() < 0.05

    try:
        target = await client.get_entity(target_entity)
        is_channel = isinstance(target, telethon_types.Channel)
        is_chat = isinstance(target, telethon_types.Chat)

        cached_participants = await cache_manager.get_cached_participants(target_entity)
        if cached_participants:
            current_participants = set(cached_participants)
        else:
            current_participants = set()
            try:
                async for user in client.iter_participants(target):
                    current_participants.add(user.id)
                await cache_manager.cache_participants(
                    target_entity, list(current_participants)
                )
            except (ChatAdminRequiredError, ChannelPrivateError) as e:
                logger.warning(
                    f"No rights to list participants in {target_entity}: {e}. Continuing without cache."
                )
            except Exception as e:
                logger.error(f"iter_participants error {target_entity}: {e}")

        idx = 0
        while idx < len(user_ids):
            current_user_id = user_ids[idx]
            if control.cancelled:
                remaining_users = user_ids[idx:]
                break

            await control.pause_event.wait()

            # Проверка на уже приглашённых
            if (
                await cache_manager.is_invited(target_entity, current_user_id)
                or current_user_id in current_participants
            ):
                results["already_members"] += 1
                processed += 1
                idx += 1
                continue

            # Защита от детекта: случайный пропуск
            if should_skip_user():
                logger.info(
                    f"🎭 Simulating human error - skipping user {current_user_id}"
                )
                processed += 1
                idx += 1
                await asyncio.sleep(random.uniform(10, 30))
                continue

            # Человеческий паттерн между инвайтами
            if processed > 0 and processed % 5 == 0:
                await account_pool.human_delay()

            try:
                # Используем кэшированные сущности
                user_entity = await get_cached_entity(client, current_user_id)
                if not user_entity:
                    results["failed"] += 1
                    processed += 1
                    idx += 1
                    continue

                # Проверка на бота
                if await account_pool._detect_bot_user(user_entity):
                    logger.info(f"User {current_user_id} is a bot - skipping")
                    processed += 1
                    idx += 1
                    continue

                # Буферизация инвайтов
                invite_buffer.append(current_user_id)

                # Отправка буфера
                if len(invite_buffer) >= buffer_size:
                    for uid in invite_buffer:
                        try:
                            if is_channel:
                                await client(
                                    functions.channels.InviteToChannelRequest(
                                        channel=target, users=[uid]
                                    )
                                )
                            elif is_chat:
                                user_ent = await get_cached_entity(client, uid)
                                if user_ent:
                                    await client(
                                        functions.messages.AddChatUserRequest(
                                            chat_id=target.id,
                                            user_id=user_ent,
                                            fwd_limit=0,
                                        )
                                    )

                            results["success"] += 1
                            current_participants.add(uid)
                            await cache_manager.mark_invited(
                                target_entity, uid, task_id
                            )
                            account["invite_count"] = account.get("invite_count", 0) + 1
                        except UserPrivacyRestrictedError:
                            results["privacy_errors"] += 1
                        except (ChatAdminRequiredError, ChannelPrivateError):
                            results["failed"] += 1
                        except UserNotParticipantError:
                            results["failed"] += 1
                        except Exception as e:
                            results["failed"] += 1
                            logger.error(f"Invite error for {uid}: {e}")

                    invite_buffer = []
                    # Задержка после буфера
                    await asyncio.sleep(random.uniform(30, 60))

                processed += 1
                idx += 1

            except UserPrivacyRestrictedError:
                results["privacy_errors"] += 1
                processed += 1
                idx += 1
            except (ChatAdminRequiredError, ChannelPrivateError):
                results["failed"] += 1
                processed += 1
                idx += 1
            except (FloodWaitError, FloodError) as e:
                wait_seconds = getattr(e, "seconds", None) or 60
                await account_pool._handle_flood_wait(account, wait_seconds)
                await tasks_storage.update_by_id(
                    task_id,
                    {
                        "status": "paused",
                        "progress": (processed / max(1, total_users)) * 100,
                        "progress_text": format_progress_bar(
                            (processed / max(1, total_users)) * 100
                        ),
                        "checkpoints": {
                            "remaining_users": user_ids[idx:],
                            "sender_session": account["session_file"],
                            "target": target_entity,
                            "source": source,
                        },
                        "flood_wait": wait_seconds,
                    },
                    id_field="task_id",
                )
                await notify_user(
                    bot,
                    user_id,
                    translate("floodwait_pause", task_id=task_id, seconds=wait_seconds),
                )
                await asyncio.sleep(wait_seconds + 5)
                await tasks_storage.update_by_id(
                    task_id,
                    {"status": "running", "flood_wait": None},
                    id_field="task_id",
                )
                # Повторить текущего пользователя после паузы
                continue
            except AuthKeyUnregisteredError:
                account["is_valid"] = False
                results["failed"] += 1
                processed += 1
                idx += 1
            except Exception as e:
                results["failed"] += 1
                processed += 1
                idx += 1
                logger.error(f"Invite error for {current_user_id}: {e}")

            # Обновление прогресса
            if processed % 20 == 0 or processed == total_users:
                progress = (processed / max(1, total_users)) * 100
                await tasks_storage.update_by_id(
                    task_id,
                    {
                        "progress": progress,
                        "progress_text": translate(
                            "progress_inviting",
                            progress_text=format_progress_bar(progress),
                            processed=processed,
                            total=total_users,
                        ),
                        "checkpoints": {
                            "remaining_users": user_ids[idx + 1 :],
                            "sender_session": account["session_file"],
                            "target": target_entity,
                            "source": source,
                        },
                    },
                    id_field="task_id",
                )

        # Отправка оставшегося буфера
        if invite_buffer:
            for uid in invite_buffer:
                try:
                    if is_channel:
                        await client(
                            functions.channels.InviteToChannelRequest(
                                channel=target, users=[uid]
                            )
                        )
                    elif is_chat:
                        user_ent = await get_cached_entity(client, uid)
                        if user_ent:
                            await client(
                                functions.messages.AddChatUserRequest(
                                    chat_id=target.id, user_id=user_ent, fwd_limit=0
                                )
                            )

                    results["success"] += 1
                    current_participants.add(uid)
                    await cache_manager.mark_invited(target_entity, uid, task_id)
                    account["invite_count"] = account.get("invite_count", 0) + 1
                except Exception as e:
                    results["failed"] += 1
                    logger.error(f"Final buffer invite error for {uid}: {e}")

        return results, remaining_users
    except Exception as e:
        logger.error(f"Error inviting users: {e}")
        raise


async def scrape_and_invite_by_user_count_task(
    control: TaskControl,
    source: str,
    target: str,
    user_count: int,
    user_id: int,
    task_id: str,
    sender_session: Optional[str] = None,
    checkpoint: Optional[Dict[str, Any]] = None,
):
    logger.info(
        f"Starting user-count task: {source} -> {target} ({user_count}) via {sender_session or 'auto'}"
    )
    if control.cancelled:
        return
    await tasks_storage.update_by_id(
        task_id,
        {
            "status": "running",
            "progress": 0,
            "progress_text": "0/0",
            "checkpoints": checkpoint or {},
        },
        id_field="task_id",
    )

    await control.pause_event.wait()
    if control.cancelled:
        return

    collected_users: List[int] = []
    try:
        async with (
            account_pool.acquire_specific_account(sender_session)
            if sender_session
            else account_pool.acquire_account()
        ) as account:
            used_session = account.get("session_file")
            try:
                task = await tasks_storage.find_by_id(task_id, id_field="task_id")
                if task and not task.get("sender_session"):
                    await tasks_storage.update_by_id(
                        task_id, {"sender_session": used_session}, id_field="task_id"
                    )
            except Exception:
                pass
            client = account["client"]
            target_entity = await ensure_join_target(
                client, target, account, task_id, user_id
            )
            if not target_entity:
                task_queue.remove_user_task(user_id, task_id)
                return

            if checkpoint and checkpoint.get("remaining_users"):
                collected_users = checkpoint.get("remaining_users", [])
            else:
                collected_users = await get_active_users(
                    control,
                    client,
                    source,
                    limit=10000,
                    task_id=task_id,
                    stop_after=user_count,
                )
                collected_users = collected_users[:user_count]

            if control.cancelled:
                await tasks_storage.update_by_id(
                    task_id,
                    {"status": "cancelled", "completed_at": datetime.now().isoformat()},
                    id_field="task_id",
                )
                task_queue.remove_user_task(user_id, task_id)
                return

            if not collected_users:
                await tasks_storage.update_by_id(
                    task_id,
                    {
                        "status": "failed",
                        "error": "No active users found",
                        "completed_at": datetime.now().isoformat(),
                        "progress": 100,
                    },
                    id_field="task_id",
                )
                task_queue.remove_user_task(user_id, task_id)
                await notify_user(
                    bot, user_id, translate("no_users_found", source=source)
                )
                return

            await tasks_storage.update_by_id(
                task_id,
                {
                    "checkpoints": {
                        "remaining_users": collected_users,
                        "sender_session": account["session_file"],
                        "target": target,
                        "source": source,
                    }
                },
                id_field="task_id",
            )

            await notify_user(
                bot,
                user_id,
                translate(
                    "invite_progress", task_id=task_id, count=len(collected_users)
                ),
            )

            results, remaining = await invite_users(
                control,
                account,
                client,
                collected_users,
                target,
                task_id,
                user_id,
                source=source,
            )

            if control.cancelled:
                await tasks_storage.update_by_id(
                    task_id,
                    {
                        "status": "cancelled",
                        "completed_at": datetime.now().isoformat(),
                        "checkpoints": {
                            "remaining_users": remaining,
                            "sender_session": account["session_file"],
                            "target": target,
                            "source": source,
                        },
                    },
                    id_field="task_id",
                )
                task_queue.remove_user_task(user_id, task_id)
                await notify_user(
                    bot, user_id, translate("task_cancelled_report", task_id=task_id)
                )
                return

            await tasks_storage.update_by_id(
                task_id,
                {
                    "status": "completed",
                    "results": results,
                    "completed_at": datetime.now().isoformat(),
                    "progress": 100,
                    "progress_text": f"{len(collected_users)} users collected",
                    "checkpoints": {"remaining_users": []},
                },
                id_field="task_id",
            )
        task_queue.remove_user_task(user_id, task_id)

        results_data = results or {}
        text = translate(
            "task_completed_report",
            task_id=task_id,
            source=source,
            target=target,
            invited=len(collected_users),
            success=results_data.get("success", 0),
            failed=results_data.get("failed", 0),
            privacy=results_data.get("privacy", 0),
            account=account["session_file"],
        )
        await notify_user(bot, user_id, text)
    except Exception as e:
        logger.exception(f"Task error: {task_id}")
        await tasks_storage.update_by_id(
            task_id,
            {
                "status": "failed",
                "error": str(e),
                "completed_at": datetime.now().isoformat(),
                "progress": 100,
            },
            id_field="task_id",
        )
        task_queue.remove_user_task(user_id, task_id)
        await notify_user(
            bot,
            user_id,
            translate("task_failed_report", task_id=task_id, error=str(e)),
        )


async def scrape_and_invite_task(
    control: TaskControl,
    source: str,
    target: str,
    message_limit: int,
    user_id: int,
    task_id: str,
    sender_session: Optional[str] = None,
    checkpoint: Optional[Dict[str, Any]] = None,
):
    logger.info(
        f"Starting message-limit task: {source} -> {target} ({message_limit}) via {sender_session or 'auto'}"
    )
    if control.cancelled:
        return
    await tasks_storage.update_by_id(
        task_id,
        {
            "status": "running",
            "progress": 0,
            "progress_text": "0/0",
            "checkpoints": checkpoint or {},
        },
        id_field="task_id",
    )

    await control.pause_event.wait()
    if control.cancelled:
        return

    collected_users: List[int] = []
    try:
        async with (
            account_pool.acquire_specific_account(sender_session)
            if sender_session
            else account_pool.acquire_account()
        ) as account:
            used_session = account.get("session_file")
            try:
                task = await tasks_storage.find_by_id(task_id, id_field="task_id")
                if task and not task.get("sender_session"):
                    await tasks_storage.update_by_id(
                        task_id, {"sender_session": used_session}, id_field="task_id"
                    )
            except Exception:
                pass
            client = account["client"]
            target_entity = await ensure_join_target(
                client, target, account, task_id, user_id
            )
            if not target_entity:
                task_queue.remove_user_task(user_id, task_id)
                return

            if checkpoint and checkpoint.get("remaining_users"):
                collected_users = checkpoint.get("remaining_users", [])
            else:
                try:
                    collected_users = await get_active_users(
                        control, client, source, message_limit, task_id
                    )
                except AuthKeyUnregisteredError:
                    account["is_valid"] = False
                    raise

            if control.cancelled:
                await tasks_storage.update_by_id(
                    task_id,
                    {"status": "cancelled", "completed_at": datetime.now().isoformat()},
                    id_field="task_id",
                )
                task_queue.remove_user_task(user_id, task_id)
                return

            if not collected_users:
                await tasks_storage.update_by_id(
                    task_id,
                    {
                        "status": "failed",
                        "error": "No active users found",
                        "completed_at": datetime.now().isoformat(),
                        "progress": 100,
                    },
                    id_field="task_id",
                )
                task_queue.remove_user_task(user_id, task_id)
                await notify_user(
                    bot, user_id, translate("no_users_found", source=source)
                )
                return

            await tasks_storage.update_by_id(
                task_id,
                {
                    "checkpoints": {
                        "remaining_users": collected_users,
                        "sender_session": account["session_file"],
                        "target": target,
                        "source": source,
                    }
                },
                id_field="task_id",
            )

            await notify_user(
                bot,
                user_id,
                translate(
                    "invite_progress", task_id=task_id, count=len(collected_users)
                ),
            )

            results, remaining = await invite_users(
                control,
                account,
                client,
                collected_users,
                target,
                task_id,
                user_id,
                source=source,
            )

            if control.cancelled:
                await tasks_storage.update_by_id(
                    task_id,
                    {
                        "status": "cancelled",
                        "completed_at": datetime.now().isoformat(),
                        "checkpoints": {
                            "remaining_users": remaining,
                            "sender_session": account["session_file"],
                            "target": target,
                            "source": source,
                        },
                    },
                    id_field="task_id",
                )
                task_queue.remove_user_task(user_id, task_id)
                await notify_user(
                    bot, user_id, translate("task_cancelled_report", task_id=task_id)
                )
                return

            await tasks_storage.update_by_id(
                task_id,
                {
                    "status": "completed",
                    "results": results,
                    "completed_at": datetime.now().isoformat(),
                    "progress": 100,
                    "progress_text": f"{len(collected_users)} users collected",
                    "checkpoints": {"remaining_users": []},
                },
                id_field="task_id",
            )
        task_queue.remove_user_task(user_id, task_id)

        results_data = results or {}
        text = translate(
            "task_completed_report",
            task_id=task_id,
            source=source,
            target=target,
            invited=len(collected_users),
            success=results_data.get("success", 0),
            failed=results_data.get("failed", 0),
            privacy=results_data.get("privacy", 0),
            account=account["session_file"],
        )
        await notify_user(bot, user_id, text)
    except Exception as e:
        logger.exception(f"Task error: {task_id}")
        await tasks_storage.update_by_id(
            task_id,
            {
                "status": "failed",
                "error": str(e),
                "completed_at": datetime.now().isoformat(),
                "progress": 100,
            },
            id_field="task_id",
        )
        task_queue.remove_user_task(user_id, task_id)
        await notify_user(
            bot,
            user_id,
            translate("task_failed_report", task_id=task_id, error=str(e)),
        )


async def bulk_mailing_task(
    control: TaskControl,
    chats: List[str],
    delay_min: int,
    delay_max: int,
    message_texts: List[str],
    total_sends: int,
    user_id: int,
    sender_session_file: Optional[str] = None,
    task_id: Optional[str] = None,
    checkpoint: Optional[Dict[str, Any]] = None,
):
    """
    Массовая рассылка с поддержкой нескольких текстов.
    Тексты циклически轮换ются при отправке.
    Graceful degradation: продолжает работу даже при ошибках.
    """
    logger.info(
        f"Starting bulk mailing: chats={len(chats)} texts={len(message_texts)} total_sends={total_sends} sender={sender_session_file or 'auto'}"
    )

    if control.cancelled:
        return

    sent = 0
    per_chat_sent = {c: 0 for c in chats}
    next_chat_idx = 0
    text_index = 0  # Индекс текущего текста
    consecutive_errors = 0
    max_consecutive_errors = 10  # Graceful degradation threshold

    if checkpoint:
        sent = checkpoint.get("sent", 0)
        per_chat_sent.update(checkpoint.get("per_chat_sent", {}))
        next_chat_idx = checkpoint.get("next_chat_idx", 0)
        text_index = checkpoint.get("text_index", 0)
        consecutive_errors = checkpoint.get("consecutive_errors", 0)

    await tasks_storage.update_by_id(
        task_id,
        {
            "status": "running",
            "progress": (sent / max(1, total_sends)) * 100,
            "progress_text": f"{sent}/{total_sends}",
            "sent": sent,
            "checkpoints": {
                "sent": sent,
                "per_chat_sent": per_chat_sent,
                "next_chat_idx": next_chat_idx,
                "text_index": text_index,
                "consecutive_errors": consecutive_errors,
                "sender_session": sender_session_file,
            },
        },
        id_field="task_id",
    )

    await control.pause_event.wait()
    if control.cancelled:
        return

    try:
        while sent < total_sends:
            if control.cancelled:
                break

            await control.pause_event.wait()

            account_ctx = (
                account_pool.acquire_specific_account(sender_session_file)
                if sender_session_file
                else account_pool.acquire_account()
            )

            async with account_ctx as account:
                client = account["client"]

                while sent < total_sends:
                    if control.cancelled:
                        break

                    await control.pause_event.wait()

                    chat = chats[next_chat_idx % len(chats)]
                    current_text = message_texts[text_index % len(message_texts)]

                    try:
                        # Безопасно получаем сущность чата
                        if chat.startswith("https://t.me/") or chat.startswith("@"):
                            target = await client.get_entity(chat)
                        else:
                            try:
                                target = await client.get_entity(int(chat))
                            except (ValueError, TypeError):
                                target = await client.get_entity(chat)

                        # Анти-блокировочная задержка с adaptive delay
                        if sent > 0:
                            delay = random.uniform(
                                Config.MAILING_MIN_DELAY, Config.MAILING_MAX_DELAY
                            )
                            logger.info(f"Mailing delay: {delay:.1f}s")
                            await asyncio.sleep(delay)

                        # Безопасная отправка с retry
                        for attempt in range(Config.MAX_RETRIES):
                            try:
                                await client.send_message(target, current_text)
                                consecutive_errors = 0  # Reset on success
                                break
                            except FloodWaitError as e:
                                wait_time = (
                                    getattr(e, "seconds", 60)
                                    * Config.FLOOD_WAIT_MULTIPLIER
                                )
                                logger.warning(f"Flood wait on send: {wait_time}s")
                                await account_pool._handle_flood_wait(
                                    account, wait_time
                                )
                                await asyncio.sleep(wait_time)
                                if attempt < Config.MAX_RETRIES - 1:
                                    continue
                                raise
                            except Exception as e:
                                consecutive_errors += 1
                                if attempt < Config.MAX_RETRIES - 1:
                                    await asyncio.sleep(2**attempt)
                                    continue
                                raise

                        sent += 1
                        per_chat_sent[chat] = per_chat_sent.get(chat, 0) + 1
                        next_chat_idx += 1
                        text_index += 1

                        # Обновление прогресса каждые N отправок
                        if (
                            sent % Config.MAILING_CHECK_INTERVAL == 0
                            or sent == total_sends
                        ):
                            progress = (sent / max(1, total_sends)) * 100
                            await tasks_storage.update_by_id(
                                task_id,
                                {
                                    "progress": progress,
                                    "progress_text": f"{sent}/{total_sends}",
                                    "sent": sent,
                                    "per_chat_sent": per_chat_sent,
                                    "checkpoints": {
                                        "sent": sent,
                                        "per_chat_sent": per_chat_sent,
                                        "next_chat_idx": next_chat_idx,
                                        "text_index": text_index,
                                        "consecutive_errors": consecutive_errors,
                                        "sender_session": account["session_file"],
                                    },
                                },
                                id_field="task_id",
                            )

                    except (FloodWaitError, FloodError) as e:
                        wait_seconds = getattr(e, "seconds", None) or 60
                        extended_wait = wait_seconds * Config.FLOOD_WAIT_MULTIPLIER
                        account["flood_wait_until"] = datetime.now() + timedelta(
                            seconds=extended_wait
                        )
                        await tasks_storage.update_by_id(
                            task_id,
                            {
                                "status": "paused",
                                "flood_wait": wait_seconds,
                                "progress": (sent / max(1, total_sends)) * 100,
                                "progress_text": f"{sent}/{total_sends}",
                                "checkpoints": {
                                    "sent": sent,
                                    "per_chat_sent": per_chat_sent,
                                    "next_chat_idx": next_chat_idx,
                                    "text_index": text_index,
                                    "consecutive_errors": consecutive_errors,
                                    "sender_session": account["session_file"],
                                },
                            },
                            id_field="task_id",
                        )
                        await notify_user(
                            bot,
                            user_id,
                            translate(
                                "floodwait_extended_pause",
                                task_id=task_id,
                                seconds=wait_seconds,
                                extended=extended_wait,
                            ),
                        )
                        await asyncio.sleep(extended_wait + 5)
                        await tasks_storage.update_by_id(
                            task_id,
                            {"status": "running", "flood_wait": None},
                            id_field="task_id",
                        )
                        if sender_session_file:
                            continue
                        else:
                            break
                    except AuthKeyUnregisteredError:
                        account["is_valid"] = False
                        await tasks_storage.update_by_id(
                            task_id,
                            {
                                "error": "Session invalid",
                                "status": "failed",
                                "completed_at": datetime.now().isoformat(),
                            },
                            id_field="task_id",
                        )
                        raise
                    except Exception as e:
                        logger.error(f"Error sending to {chat}: {e}")
                        consecutive_errors += 1

                        # Graceful degradation: если слишком много ошибок, продолжаем но с предупреждением
                        if consecutive_errors > max_consecutive_errors:
                            logger.warning(
                                f"Too many consecutive errors ({consecutive_errors}), "
                                f"continuing with longer delays"
                            )
                            await asyncio.sleep(30)  # Extended delay on errors
                        else:
                            await asyncio.sleep(2)

            await asyncio.sleep(0)

        if control.cancelled:
            await tasks_storage.update_by_id(
                task_id,
                {
                    "status": "cancelled",
                    "completed_at": datetime.now().isoformat(),
                    "checkpoints": {
                        "sent": sent,
                        "per_chat_sent": per_chat_sent,
                        "next_chat_idx": next_chat_idx,
                        "text_index": text_index,
                        "consecutive_errors": consecutive_errors,
                        "sender_session": sender_session_file,
                    },
                },
                id_field="task_id",
            )
        task_queue.remove_user_task(user_id, task_id)
        await notify_user(
            bot, user_id, translate("mailing_cancelled_report", task_id=task_id)
        )
        return

        await tasks_storage.update_by_id(
            task_id,
            {
                "status": "completed",
                "completed_at": datetime.now().isoformat(),
                "total_sent": sent,
                "per_chat_sent": per_chat_sent,
                "progress": 100,
                "progress_text": f"{sent}/{total_sends}",
                "sent": sent,
                "checkpoints": {
                    "sent": sent,
                    "per_chat_sent": per_chat_sent,
                    "next_chat_idx": next_chat_idx,
                    "text_index": text_index,
                    "consecutive_errors": consecutive_errors,
                    "sender_session": sender_session_file,
                },
            },
            id_field="task_id",
        )
        task_queue.remove_user_task(user_id, task_id)

        chat_report_lines = []
        for c, cnt in per_chat_sent.items():
            chat_report_lines.append(f"  - {c}: {cnt}")
        chats_report = "\n".join(chat_report_lines)

        text = translate(
            "mailing_completed_report",
            task_id=task_id,
            sent=sent,
            texts_count=len(message_texts),
            chats_report=chats_report,
        )
        await notify_user(bot, user_id, text)

    except Exception as e:
        logger.exception("Bulk mailing task failed")
        await tasks_storage.update_by_id(
            task_id,
            {
                "status": "failed",
                "error": str(e),
                "completed_at": datetime.now().isoformat(),
                "progress": (sent / max(1, total_sends)) * 100,
                "progress_text": f"{sent}/{total_sends}",
                "sent": sent,
                "checkpoints": {
                    "sent": sent,
                    "per_chat_sent": per_chat_sent,
                    "next_chat_idx": next_chat_idx,
                    "text_index": text_index,
                    "consecutive_errors": consecutive_errors,
                    "sender_session": sender_session_file,
                },
            },
            id_field="task_id",
        )
        task_queue.remove_user_task(user_id, task_id)

        chat_report_lines = []
        for c, cnt in per_chat_sent.items():
            chat_report_lines.append(f"  - {c}: {cnt}")
        chats_report = "\n".join(chat_report_lines)

        text = translate(
            "mailing_completed_report",
            task_id=task_id,
            sent=sent,
            texts_count=len(message_texts),
            chats_report=chats_report,
        )
        await notify_user(bot, user_id, text)


async def validate_and_add_chats_from_text(
    client: TelegramClient,
    account: Dict[str, Any],
    text: str,
    user_id: int,
    task_id: str,
) -> Dict[str, int]:
    """
    Извлекает ссылки из текста, валидирует и добавляет в БД.
    Возвращает статистику: added, errors, skipped.
    """
    result = {"added": 0, "errors": 0, "skipped": 0}

    # Извлекаем все ссылки
    links = extract_links_from_text(text)

    if not links:
        logger.info(f"Ссылки не найдены в тексте")
        return result

    logger.info(f"Найдено {len(links)} ссылок")

    for link in links:
        identifier = parse_link_to_identifier(link)
        if not identifier:
            result["skipped"] += 1
            continue

        try:
            # Валидируем чат
            chat_data = await validate_and_test_chat(
                client,
                account,
                identifier,
                update_existing=True,  # Обновляем существующий или добавляем новый
            )

            if chat_data:
                result["added"] += 1
                logger.info(f"✅ Чат {chat_data['chat_name']} добавлен/обновлён")
            else:
                result["skipped"] += 1
                logger.info(f"⏭️ Чат {identifier} пропущен")

            # Анти-флуд
            await asyncio.sleep(random.uniform(3, 7))

        except Exception as e:
            result["errors"] += 1
            logger.error(f"Ошибка валидации {link}: {e}")

    return result


async def bulk_mailing_new_chats_task(
    control: TaskControl,
    raw_text: str,
    message_texts: List[str],
    total_sends: int,
    user_id: int,
    task_id: str,
    sender_session_file: Optional[str] = None,
    checkpoint: Optional[Dict[str, Any]] = None,
):
    """
    Рассылка по новым чатам из текста/файла.
    1. Извлекает ссылки из текста
    2. Валидирует каждый чат
    3. Добавляет/обновляет в БД
    4. Отправляет сообщения
    """
    logger.info(f"Starting new chats mailing: {len(raw_text)} chars")

    if control.cancelled:
        return

    # Шаг 1: Валидация и добавление чатов
    account_ctx = (
        account_pool.acquire_specific_account(sender_session_file)
        if sender_session_file
        else account_pool.acquire_account()
    )

    valid_chats: List[Dict[str, Any]] = []

    async with account_ctx as account:
        client = account["client"]

        validation_result = await validate_and_add_chats_from_text(
            client, account, raw_text, user_id, task_id
        )

        logger.info(f"Валидация завершена: {validation_result}")

        # Получаем все валидные чаты из БД (те что мы только что добавили)
        all_chats = await chat_db.get_all_chats()
        valid_chats = all_chats

        if not valid_chats:
            await tasks_storage.update_by_id(
                task_id,
                {
                    "status": "failed",
                    "error": "No valid chats found",
                    "completed_at": datetime.now().isoformat(),
                },
                id_field="task_id",
            )
            task_queue.remove_user_task(user_id, task_id)
            await notify_user(bot, user_id, translate("mailing_no_chats"))
            return

        logger.info(f"Валидных чатов: {len(valid_chats)}")

        # Шаг 2: Рассылка
        chats = [c["chat_url"] for c in valid_chats]
        await bulk_mailing_task(
            control,
            chats=chats,
            delay_min=Config.MAILING_MIN_DELAY,
            delay_max=Config.MAILING_MAX_DELAY,
            message_texts=message_texts,
            total_sends=total_sends,
            user_id=user_id,
            sender_session_file=sender_session_file,
            task_id=task_id,
            checkpoint=checkpoint,
        )


async def db_scrape_and_invite_task(
    control: TaskControl,
    target: str,
    message_limit: int,
    user_id: int,
    task_id: str,
    sender_session: Optional[str] = None,
    checkpoint: Optional[Dict[str, Any]] = None,
):
    """
    Парсинг пользователей из всех чатов в БД с последующим приглашением в target.
    """
    logger.info(
        f"Starting DB scraping: {target} ({message_limit}) via {sender_session or 'auto'}"
    )

    if control.cancelled:
        return

    # Получаем все чаты из БД
    db_chats = await chat_db.get_all_chats()
    if not db_chats:
        await tasks_storage.update_by_id(
            task_id,
            {
                "status": "failed",
                "error": "Chats DB is empty",
                "completed_at": datetime.now().isoformat(),
            },
            id_field="task_id",
        )
        task_queue.remove_user_task(user_id, task_id)
        await notify_user(bot, user_id, translate("scraping_db_empty"))
        return

    logger.info(f"DB scraping: {len(db_chats)} chats from DB")

    all_collected_users: List[int] = []
    chats_processed = 0
    chats_failed = 0

    try:
        async with (
            account_pool.acquire_specific_account(sender_session)
            if sender_session
            else account_pool.acquire_account()
        ) as account:
            client = account["client"]
            target_entity = await ensure_join_target(
                client, target, account, task_id, user_id
            )
            if not target_entity:
                task_queue.remove_user_task(user_id, task_id)
                return

            # Проходим по всем чатам в БД
            for chat in db_chats:
                if control.cancelled:
                    break

                chat_url = chat.get("chat_url") or chat.get("chat_id")
                if not chat_url:
                    chats_failed += 1
                    continue

                try:
                    # Собираем пользователей из чата
                    users = await get_active_users(
                        control, client, chat_url, message_limit, task_id
                    )
                    all_collected_users.extend(users)
                    chats_processed += 1

                    logger.info(
                        f"DB scraping: processed {chat_url}, collected {len(users)} users"
                    )

                except AuthKeyUnregisteredError:
                    account["is_valid"] = False
                    raise
                except Exception as e:
                    logger.error(f"Error scraping chat {chat_url}: {e}")
                    chats_failed += 1
                    continue

            if control.cancelled:
                await tasks_storage.update_by_id(
                    task_id,
                    {"status": "cancelled", "completed_at": datetime.now().isoformat()},
                    id_field="task_id",
                )
                task_queue.remove_user_task(user_id, task_id)
                return

            if not all_collected_users:
                await tasks_storage.update_by_id(
                    task_id,
                    {
                        "status": "failed",
                        "error": "No active users found in any chat",
                        "completed_at": datetime.now().isoformat(),
                        "progress": 100,
                    },
                    id_field="task_id",
                )
                task_queue.remove_user_task(user_id, task_id)
                await notify_user(bot, user_id, translate("scraping_no_users"))
                return

            # Приглашаем пользователей
            await tasks_storage.update_by_id(
                task_id,
                {
                    "status": "running",
                    "progress": 0,
                    "progress_text": f"0/{len(all_collected_users)}",
                    "checkpoints": {
                        "remaining_users": all_collected_users,
                        "sender_session": account["session_file"],
                        "target": target,
                        "source": f"DB ({len(db_chats)} chats)",
                        "chats_processed": chats_processed,
                        "chats_failed": chats_failed,
                    },
                },
                id_field="task_id",
            )

            await notify_user(
                bot,
                user_id,
                translate(
                    "invite_progress_from_chats",
                    task_id=task_id,
                    count=len(all_collected_users),
                    chats=chats_processed,
                ),
            )

            results, remaining = await invite_users(
                control,
                account,
                client,
                all_collected_users,
                target,
                task_id,
                user_id,
                source=f"DB ({len(db_chats)} chats)",
            )

    except AuthKeyUnregisteredError:
        await tasks_storage.update_by_id(
            task_id,
            {
                "status": "failed",
                "error": "Session invalid",
                "completed_at": datetime.now().isoformat(),
            },
            id_field="task_id",
        )
        task_queue.remove_user_task(user_id, task_id)
        raise
    except Exception as e:
        logger.exception("DB scraping task failed")
        await tasks_storage.update_by_id(
            task_id,
            {
                "status": "failed",
                "error": str(e),
                "completed_at": datetime.now().isoformat(),
            },
            id_field="task_id",
        )
        task_queue.remove_user_task(user_id, task_id)
        await notify_user(
            bot,
            user_id,
            translate("mailing_failed_report", task_id=task_id, error=e),
        )


async def queue_task_from_storage(task: Dict[str, Any], resume: bool = False):
    task_id = task.get("task_id")
    task_type = task.get("type")
    user_id = task.get("user_id")
    checkpoint = task.get("checkpoints")

    if not task_id or not task_type or not user_id:
        return

    if task_type == "scraping":
        source_type = task.get("chats_source_type", "manual")
        if source_type == "db":
            # DB scraping task
            await task_queue.add_task(
                task_id,
                db_scrape_and_invite_task,
                target=task.get("target"),
                message_limit=task.get("limit", 0),
                user_id=user_id,
                task_id=task_id,
                sender_session=task.get("sender_session"),
                checkpoint=checkpoint,
            )
        else:
            mode = task.get("mode")
            if mode == "messages":
                await task_queue.add_task(
                    task_id,
                    scrape_and_invite_task,
                    source=task.get("source"),
                    target=task.get("target"),
                    message_limit=task.get("limit", 0),
                    user_id=user_id,
                    task_id=task_id,
                    sender_session=task.get("sender_session"),
                    checkpoint=checkpoint,
                )
            else:
                await task_queue.add_task(
                    task_id,
                    scrape_and_invite_by_user_count_task,
                    source=task.get("source"),
                    target=task.get("target"),
                    user_count=task.get("user_count", 0),
                    user_id=user_id,
                    task_id=task_id,
                    sender_session=task.get("sender_session"),
                    checkpoint=checkpoint,
                )
    elif task_type == "mailing":
        await task_queue.add_task(
            task_id,
            bulk_mailing_task,
            chats=task.get("chats", []),
            delay_min=task.get("delay_min", 1),
            delay_max=task.get("delay_max", 1),
            message_texts=task.get("message_texts", [""]),
            total_sends=task.get("total_sends", 0),
            user_id=user_id,
            sender_session_file=task.get("sender_session"),
            task_id=task_id,
            checkpoint=checkpoint,
        )

    task_queue.add_user_task(user_id, task_id)
    if resume:
        control = task_queue.task_controls.setdefault(task_id, TaskControl())
        control.pause_event.set()


# --- Фоновые задачи ---
async def restore_tasks_on_startup():
    tasks = await tasks_storage.read_all()
    for task in tasks:
        task_id = task.get("task_id")
        status = task.get("status")
        task_user_id = task.get("user_id")

        if status == "running":
            await tasks_storage.update_by_id(
                task_id,
                {
                    "status": "paused",
                    "progress_text": translate("progress_paused"),
                    "paused_at": datetime.now().isoformat(),
                },
                id_field="task_id",
            )
            status = "paused"

        if status == "pending":
            await queue_task_from_storage(task)

        if status == "paused" and task_user_id:
            task_queue.add_user_task(task_user_id, task_id)
        elif status == "pending" and task_user_id:
            task_queue.add_user_task(task_user_id, task_id)

        # Paused задачи оставляем до ручного возобновления


async def cleanup_entity_cache():
    """Фоновая очистка кэша сущностей каждые 30 минут"""
    while True:
        try:
            await asyncio.sleep(1800)  # 30 минут
            await clear_entity_cache()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Cache cleanup error: {e}")
            await asyncio.sleep(300)


async def simulate_account_activity():
    """Фоновая эмуляция активности аккаунтов"""
    while True:
        try:
            await asyncio.sleep(3600)  # Каждый час

            async with account_pool.lock:
                for acc in account_pool.accounts:
                    if not acc["in_use"] and acc["is_valid"] and acc["client"]:
                        try:
                            await account_pool.simulate_activity(acc["client"])
                            logger.info(
                                f"📱 Simulated activity for {acc['session_file']}"
                            )
                        except Exception as e:
                            logger.debug(f"Activity simulation error: {e}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Activity simulation error: {e}")
            await asyncio.sleep(600)


async def cleanup_old_tasks():
    """Очистка старых задач из хранилища"""
    while True:
        try:
            tasks = await tasks_storage.read_all()
            cutoff_time = datetime.now() - timedelta(days=7)
            new_tasks = []

            for task in tasks:
                if task.get("status") in ["completed", "cancelled", "failed"]:
                    completed_at = task.get("completed_at") or task.get("created_at")
                    if completed_at:
                        try:
                            dt = datetime.fromisoformat(
                                completed_at.replace("Z", "+00:00")
                            )
                            if dt >= cutoff_time:
                                new_tasks.append(task)
                        except Exception:
                            new_tasks.append(task)
                    else:
                        new_tasks.append(task)
                else:
                    new_tasks.append(task)

            if len(new_tasks) != len(tasks):
                await tasks_storage.write_all(new_tasks)
                logger.info(f"Cleaned up {len(tasks) - len(new_tasks)} old tasks")

            await asyncio.sleep(86400)
        except Exception as e:
            logger.error(f"Ошибка очистки задач: {e}")
            await asyncio.sleep(3600)


# --- Запуск ---
async def main():
    logger.info("Запуск рефакторированного бота-инвайтера v2.0...")
    logger.info("Анти-блокировочные механизмы активированы")

    if not Config.BOT_TOKEN or Config.BOT_TOKEN == "YOUR_BOT_TOKEN":
        logger.critical("BOT_TOKEN не настроен! Установите его в .env")
        sys.exit(1)

    if Config.API_ID == 0 or not Config.API_HASH:
        logger.critical("API_ID и API_HASH должны быть настроены!")
        sys.exit(1)

    try:
        await auth_manager.load()
        await cache_manager.connect()
        await chat_db.connect()
        await tasks_storage.connect()

        task_queue.start_workers()
        await account_pool.start_health_check()
        await restore_tasks_on_startup()

        # Фоновые задачи
        asyncio.create_task(cleanup_old_tasks())
        asyncio.create_task(cleanup_entity_cache())
        asyncio.create_task(simulate_account_activity())

        for admin_id in Config.ADMIN_USER_IDS:
            chats_count = await chat_db.get_active_chats_count()
            total_users = await chat_db.get_total_users()
            await notify_user(
                bot,
                admin_id,
                translate(
                    "admin_startup",
                    accounts=len(account_pool.accounts),
                    chats=chats_count,
                    users=total_users,
                    max_tasks=Config.MAX_CONCURRENT_TASKS,
                    max_per_user=Config.MAX_TASKS_PER_USER,
                    min_delay=Config.MIN_INVITE_DELAY,
                    max_delay=Config.MAX_INVITE_DELAY,
                    flood_multiplier=Config.FLOOD_WAIT_MULTIPLIER,
                    retries=Config.MAX_RETRIES,
                ),
            )

        await dp.start_polling(bot)

    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Остановка бота по запросу пользователя")
    finally:
        logger.info("Releasing all accounts...")

        # Остановка health-check
        await account_pool.stop_health_check()

        # Остановка воркеров
        await task_queue.stop_workers()

        # Очистка кэша
        await clear_entity_cache()

        for account in account_pool.accounts:
            if account["client"]:
                try:
                    if account["client"].is_connected():
                        await account["client"].disconnect()
                    account["client"] = None
                    account["in_use"] = False
                    logger.info(f"Released session: {account['session_file']}")
                except Exception as e:
                    logger.error(
                        f"Error releasing account {account['session_file']}: {e}"
                    )

        await cache_manager.close()
        await chat_db.close()
        await tasks_storage.close()
        await auth_manager.save()

        for admin_id in Config.ADMIN_USER_IDS:
            await notify_user(
                bot,
                admin_id,
                translate("admin_shutdown"),
            )

        await bot.session.close()
        logger.info("All accounts released. Bot shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
