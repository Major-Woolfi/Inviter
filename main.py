import asyncio
import html
import json
import logging
import os
import random
import secrets
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

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

# --- Логирование ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(LOG_DIR, f"{datetime.now().strftime('%Y-%m-%d')}.log"),
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger(__name__)


# --- Конфиг ---
def str_to_bool(val: str) -> bool:
    return str(val).strip().lower() in ("1", "true", "yes", "y", "on")


class Config:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    API_ID: int = int(os.getenv("API_ID", "0"))
    API_HASH: str = os.getenv("API_HASH", "")
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


for path in [Config.SESSIONS_DIR, Config.DATA_DIR, LOG_DIR]:
    try:
        os.makedirs(path, exist_ok=True)
    except Exception as e:
        logger.warning(f"Не удалось создать папку {path}: {e}")


# --- Утилиты ---
def kb(rows: List[List[Dict[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(**button) for button in row] for row in rows
        ]
    )


async def safe_send_message(
    bot: Bot,
    user_id: int,
    message: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
):
    try:
        await bot.send_message(
            user_id, message, parse_mode=ParseMode.HTML, reply_markup=reply_markup
        )
    except TelegramBadRequest as e:
        logger.warning(
            f"HTML parse error for user {user_id}: {e}. Trying escaped HTML then plain text."
        )
        try:
            await bot.send_message(
                user_id,
                html.escape(message),
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
        except Exception:
            try:
                await bot.send_message(user_id, message, reply_markup=reply_markup)
            except Exception as e2:
                logger.error(f"Ошибка отправки plain message {user_id}: {e2}")
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения {user_id}: {str(e)}")


async def notify_admins(
    bot: Bot, message: str, reply_markup: Optional[InlineKeyboardMarkup] = None
):
    for admin_id in Config.ADMIN_USER_IDS:
        await safe_send_message(bot, admin_id, message, reply_markup=reply_markup)


async def notify_user(
    bot: Bot,
    user_id: int,
    message: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
):
    await safe_send_message(bot, user_id, message, reply_markup=reply_markup)


async def smart_answer(
    event, bot: Bot, text: str, reply_markup=None, delete_origin=False, show_alert=False
):
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
                await event.answer(show_alert=show_alert)
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Ошибка в smart_answer: {e}")


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
    except TelegramBadRequest:
        return False
    except Exception as e:
        logger.debug(f"Error updating message: {e}")
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
class JSONStorage:
    def __init__(self, path: str):
        self.path = path
        self._lock = asyncio.Lock()

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

    async def read_all(self) -> List[Dict[str, Any]]:
        await self._ensure_file()
        async with self._lock:
            async with aiofiles.open(self.path, "r", encoding="utf-8") as f:
                content = await f.read()
        if not content:
            return []
        try:
            data = json.loads(content)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    async def write_all(self, data: List[Dict[str, Any]]) -> None:
        await self._ensure_file()
        async with self._lock:
            async with aiofiles.open(self.path, "w", encoding="utf-8") as f:
                await f.write(json.dumps(data, ensure_ascii=False, indent=2))

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


# --- AccountPoolManager ---
class AccountPoolManager:
    def __init__(self):
        self.accounts: List[Dict[str, Any]] = []
        self.lock = asyncio.Lock()
        self._load_accounts()
        self._health_check_task = None

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
        """Периодическая проверка аккаунтов"""
        while True:
            try:
                await asyncio.sleep(Config.HEALTH_CHECK_INTERVAL)
                await self._check_all_accounts()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health check error: {e}")

    async def _check_all_accounts(self):
        """Проверка всех аккаунтов"""
        async with self.lock:
            for acc in self.accounts:
                if not acc["in_use"]:
                    if not acc["client"] or not acc["client"].is_connected():
                        try:
                            if acc["client"]:
                                await acc["client"].disconnect()
                            acc["client"] = await self._create_client(
                                acc["session_string"]
                            )
                            me = await acc["client"].get_me()
                            if not me:
                                raise Exception("Not authorized")
                            acc["is_valid"] = True
                            acc["last_check"] = datetime.now()
                            logger.info(f"Account health OK: {acc['session_file']}")
                        except Exception as e:
                            logger.error(
                                f"Account health check failed {acc['session_file']}: {e}"
                            )
                            acc["is_valid"] = False

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
                        }
                    )
                except Exception as e:
                    logger.error(f"Error loading session {filename}: {e}")

    async def _reset_account_errors(self, acc: Dict):
        """Сбрасывает счётчик ошибок"""
        acc["error_count"] = 0
        acc["invite_count"] = 0

    async def _handle_flood_wait(self, acc: Dict, wait_time: float):
        """Обрабатывает FloodWait с увеличенным временем ожидания"""
        extended_wait = wait_time * Config.FLOOD_WAIT_MULTIPLIER
        acc["flood_wait_until"] = datetime.now() + timedelta(seconds=extended_wait)
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
        """Проверяет rate limit аккаунта"""
        if acc.get("last_used"):
            elapsed = (datetime.now() - acc["last_used"]).total_seconds()
            if elapsed < Config.MIN_INVITE_DELAY:
                logger.info(
                    f"Account {acc['session_file']} rate limited: {elapsed:.1f}s since last use"
                )
                return False
        return True

    async def _detect_bot_user(self, user: Any) -> bool:
        """Определяет, является ли пользователь ботом"""
        if hasattr(user, "bot") and user.bot:
            return True
        if hasattr(user, "is_bot") and user.is_bot:
            return True
        return False

    async def _safe_get_user(
        self, client: TelegramClient, user_id: int
    ) -> Optional[Any]:
        """Безопасно получает пользователя с retry logic"""
        for attempt in range(Config.MAX_RETRIES):
            try:
                return await client.get_entity(user_id)
            except FloodWaitError as e:
                logger.warning(f"Flood wait on get_entity {user_id}: {e.value}s")
                await self._handle_flood_wait(
                    next(
                        (acc for acc in self.accounts if acc.get("client") == client),
                        None,
                    ),
                    e.value,
                )
                raise
            except Exception as e:
                if attempt < Config.MAX_RETRIES - 1:
                    wait_time = 2**attempt  # Exponential backoff
                    logger.warning(
                        f"Retry get_entity {user_id} attempt {attempt + 1}: {e}"
                    )
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(
                        f"Failed to get entity {user_id} after {Config.MAX_RETRIES} attempts: {e}"
                    )
                    return None

    async def _safe_invite(
        self, client: TelegramClient, chat_id: str, user_id: int
    ) -> bool:
        """Безопасно приглашает пользователя с retry и анти-бан механизмами"""
        for attempt in range(Config.MAX_RETRIES):
            try:
                await client(
                    functions.channels.InviteToChannelRequest(
                        channel=chat_id, users=[user_id]
                    )
                )
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
                await self._handle_flood_wait(
                    next(
                        (acc for acc in self.accounts if acc.get("client") == client),
                        None,
                    ),
                    e.value,
                )
                raise
            except ChatAdminRequiredError:
                logger.error(f"Need admin rights for chat {chat_id}")
                return False
            except Exception as e:
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
                # Сортировка по последнему использованию (round-robin)
                self.accounts.sort(key=lambda x: x["last_used"] or datetime.min)

                for acc in self.accounts:
                    if not acc["in_use"] and acc["is_valid"]:
                        # Проверка flood wait
                        if not await self._check_account_flood(acc):
                            continue
                        # Проверка rate limit
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
                                acc["client"] = await self._create_client(
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
                        f"Released account: {account['session_file']} (invites: {account['invite_count']})"
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
                    account["client"] = await self._create_client(
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


class TaskQueueManager:
    def __init__(self, max_concurrent_tasks: int = 3):
        self.max_concurrent_tasks = max_concurrent_tasks
        self.queue = asyncio.Queue()
        self.active_tasks: Dict[str, asyncio.Task] = {}
        self.task_controls: Dict[str, TaskControl] = {}
        self.user_tasks: Dict[int, Set[str]] = {}
        self.logger = logging.getLogger("task_queue")
        self.tasks_storage: Optional[JSONStorage] = None
        self._worker_tasks = []

    def set_storage(self, storage: JSONStorage):
        self.tasks_storage = storage

    def start_workers(self):
        for i in range(self.max_concurrent_tasks):
            task = asyncio.create_task(self._worker(f"worker-{i + 1}"))
            self._worker_tasks.append(task)

    async def stop_workers(self):
        """Останавливает всех воркеров"""
        for task in self._worker_tasks:
            task.cancel()
        await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        self._worker_tasks = []

    async def _worker(self, name: str):
        self.logger.info(f"Worker {name} started")
        while True:
            try:
                task_func, task_id, args, kwargs = await self.queue.get()
                control = self.task_controls.setdefault(task_id, TaskControl())
                try:
                    self.active_tasks[task_id] = asyncio.current_task()
                    await task_func(control, *args, **kwargs)
                except asyncio.CancelledError:
                    self.logger.info(f"Task {task_id} cancelled")
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


class KeyGeneration(StatesGroup):
    waiting_user_id = State()


class BulkMailStates(StatesGroup):
    waiting_chats = State()
    waiting_delay = State()
    waiting_text = State()
    waiting_sender = State()
    waiting_count = State()


class ClearCacheStates(StatesGroup):
    waiting_confirmation = State()


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
tasks_storage = JSONStorage(Config.TASKS_FILE)
account_pool = AccountPoolManager()
task_queue = TaskQueueManager(max_concurrent_tasks=Config.MAX_CONCURRENT_TASKS)
task_queue.set_storage(tasks_storage)

pending_auth = {}


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
            text = "🔑 <b>Требуется авторизация!</b>\nПожалуйста, введите ключ доступа, который вы получили от администратора:"
            keyboard = [[{"text": "Главная", "callback_data": "start"}]]
            await smart_answer(
                event, bot, text, reply_markup=kb(keyboard), delete_origin=False
            )
        else:
            await smart_answer(
                event, bot, "⌛️ Ожидаю ввода ключа доступа...", delete_origin=False
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
        text = "🔒 <b>Требуется авторизация!</b>\n\nДля использования бота вам необходим ключ доступа.\nПожалуйста, введите ключ, который вы получили от администратора:"
        await smart_answer(event, bot, text, delete_origin=False)
        return

    if is_admin:
        text = "👑 <b>Добро пожаловать, администратор!</b>"
        keyboard = [
            [{"text": "📋 Список задач", "callback_data": "task_list"}],
            [{"text": "👥 Список аккаунтов", "callback_data": "list_accounts"}],
            [{"text": "🔑 Генерация ключа", "callback_data": "genkey"}],
            [{"text": "🗑️ Сброс кэша", "callback_data": "clear_cache"}],
            [{"text": "📊 Статистика", "callback_data": "task_stats"}],
            [{"text": "📱 Добавить аккаунт", "callback_data": "add_account"}],
            [{"text": "🔍 Начать сбор", "callback_data": "start_scraping"}],
            [{"text": "📨 Массовая рассылка", "callback_data": "bulk_mailing"}],
            [{"text": "❓ Помощь", "callback_data": "help"}],
            [{"text": "💸 Рефералка", "callback_data": "ref"}],
        ]
    else:
        text = "👋 <b>Добро пожаловать в бота-инвайтера!</b>"
        keyboard = [
            [{"text": "📱 Добавить аккаунт", "callback_data": "add_account"}],
            [{"text": "👥 Мои аккаунты", "callback_data": "list_accounts"}],
            [{"text": "🔍 Начать сбор", "callback_data": "start_scraping"}],
            [{"text": "📨 Массовая рассылка", "callback_data": "bulk_mailing"}],
            [{"text": "📊 Мои задачи", "callback_data": "my_tasks"}],
            [{"text": "❓ Помощь", "callback_data": "help"}],
            [{"text": "💸 Рефералка", "callback_data": "ref"}],
        ]

    await smart_answer(event, bot, text, reply_markup=kb(keyboard), delete_origin=True)


@router.callback_query(F.data == "cancel")
async def cmd_cancel(event, state: FSMContext):
    await state.clear()
    await cmd_start(event, state)


@router.message(lambda message: message.from_user.id in pending_auth)
async def process_auth_key(message: Message):
    user_id = message.from_user.id
    key = message.text.strip()

    if await auth_manager.verify_key(user_id, key):
        await auth_manager.add_authorized_user(user_id)
        del pending_auth[user_id]
        text = "✅ <b>Авторизация успешна!</b>\n\nТеперь вы можете использовать все функции бота."
        await smart_answer(message, bot, text, delete_origin=False)
        await cmd_start(message, None)
    else:
        await notify_admins(
            bot,
            f"⚠️ <b>Попытка несанкционированного доступа!</b>\n• Пользователь: {user_id}\n• Введенный ключ: {key}",
        )
        text = "❌ <b>Неверный ключ доступа!</b>\n\nАдминистраторы уведомлены о попытке входа.\nПожалуйста, свяжитесь с администратором для получения действительного ключа."
        await smart_answer(message, bot, text, delete_origin=False)


@router.callback_query(F.data == "add_account")
async def cmd_add_account(event: CallbackQuery, state: FSMContext):
    text = "📱 <b>Шаг 1/3</b>\nПожалуйста, отправьте ваш номер телефона в международном формате (например, +71234567890):"
    keyboard = [[{"text": "Отмена", "callback_data": "cancel"}]]
    await smart_answer(event, bot, text, reply_markup=kb(keyboard), delete_origin=True)
    await state.set_state(AddAccountStates.waiting_phone)


@router.message(AddAccountStates.waiting_phone)
async def process_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    await state.update_data(phone=phone)
    text = "⚠️ <b>Уведомление о безопасности</b>\n\nДобавляя ваш аккаунт:\n• Этот бот будет использовать ваш аккаунт Telegram\n• Ваши другие сессии НЕ будут завершены\n• Вы можете продолжать использовать Telegram как обычно\n\nВы согласны продолжить? (да/нет)"
    keyboard = [
        [{"text": "✅ Да", "callback_data": "confirm_yes"}],
        [{"text": "❌ Нет", "callback_data": "cancel"}],
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
            "❌ Отсутствует номер телефона. Начните заново с /start",
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
        text = f"🔑 <b>Шаг 2/3</b>\nTelegram отправил код на ваш телефон ({phone}).\nПожалуйста, введите код в формате: <code>12345</code>"
        keyboard = [[{"text": "Отмена", "callback_data": "cancel"}]]
        await smart_answer(
            event, bot, text, reply_markup=kb(keyboard), delete_origin=True
        )
        await state.set_state(AddAccountStates.waiting_code)
    except (PhoneNumberInvalidError, FloodWaitError) as e:
        await smart_answer(event, bot, f"❌ Ошибка: {str(e)}", delete_origin=True)
        await client.disconnect()
        await state.clear()
    except Exception as e:
        await smart_answer(
            event, bot, f"❌ Непредвиденная ошибка: {str(e)}", delete_origin=True
        )
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
            "❌ Ошибка состояния. Начните заново с /start",
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
            text = f"✅ <b>Аккаунт успешно добавлен!</b>\n• Имя: {me.first_name or ''} {me.last_name or ''}\n• Имя пользователя: @{me.username}\n• Телефон: {phone}\n\n⚠️ <b>Помните:</b> Ваши другие сессии останутся активными."
            keyboard = [[{"text": "Главная", "callback_data": "start"}]]
            await smart_answer(
                message, bot, text, reply_markup=kb(keyboard), delete_origin=False
            )
            await persistent_client.disconnect()
            await state.clear()
        else:
            text = "🔒 <b>Шаг 3/3</b>\nПожалуйста, введите ваш пароль двухфакторной аутентификации:"
            keyboard = [[{"text": "Отмена", "callback_data": "cancel"}]]
            await smart_answer(
                message, bot, text, reply_markup=kb(keyboard), delete_origin=False
            )
            await state.update_data(password_attempts=0)
            await state.set_state(AddAccountStates.waiting_password)
    except SessionPasswordNeededError:
        text = "🔒 <b>Шаг 3/3</b>\nПожалуйста, введите ваш пароль двухфакторной аутентификации:"
        keyboard = [[{"text": "Отмена", "callback_data": "cancel"}]]
        await smart_answer(
            message, bot, text, reply_markup=kb(keyboard), delete_origin=False
        )
        await state.update_data(password_attempts=0)
        await state.set_state(AddAccountStates.waiting_password)
    except PhoneCodeExpiredError:
        try:
            sent_code = await client.send_code_request(phone)
            await state.update_data(phone_code_hash=sent_code.phone_code_hash)
            text = "⚠️ <b>Код устарел!</b>\nНовый код был отправлен на ваш телефон.\nПожалуйста, введите новый код:"
            keyboard = [[{"text": "Отмена", "callback_data": "cancel"}]]
            await smart_answer(
                message, bot, text, reply_markup=kb(keyboard), delete_origin=False
            )
        except Exception as e:
            await smart_answer(
                message,
                bot,
                f"❌ Ошибка при отправке нового кода: {str(e)}",
                delete_origin=False,
            )
            await client.disconnect()
            await state.clear()
    except (PhoneCodeInvalidError, FloodWaitError) as e:
        await smart_answer(message, bot, f"❌ Ошибка: {str(e)}", delete_origin=False)
        await client.disconnect()
        await state.clear()
    except Exception as e:
        await smart_answer(
            message, bot, f"❌ Непредвиденная ошибка: {str(e)}", delete_origin=False
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
            "❌ Ошибка состояния. Начните заново с /start",
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
            text = f"✅ <b>Аккаунт успешно добавлен!</b>\n• Имя: {me.first_name or ''} {me.last_name or ''}\n• Имя пользователя: @{me.username}\n• Телефон: {phone}\n\n⚠️ <b>Помните:</b> Ваши другие сессии останутся активными."
            keyboard = [[{"text": "Главная", "callback_data": "start"}]]
            await smart_answer(
                message, bot, text, reply_markup=kb(keyboard), delete_origin=False
            )
            await persistent_client.disconnect()
            await state.clear()
        else:
            await smart_answer(
                message,
                bot,
                "❌ Авторизация не удалась. Пожалуйста, попробуйте снова.",
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
                "❌ Превышено количество попыток ввода пароля. Добавление аккаунта отменено.",
                delete_origin=False,
            )
            await client.disconnect()
            await state.clear()
        else:
            await state.update_data(password_attempts=attempts)
            await smart_answer(
                message,
                bot,
                f"❌ Неверный пароль. Осталось попыток: {3 - attempts}",
                delete_origin=False,
            )
    except Exception as e:
        await smart_answer(message, bot, f"❌ Ошибка: {str(e)}", delete_origin=False)
        await client.disconnect()
        await state.clear()


@router.callback_query(F.data == "list_accounts")
async def cmd_list_accounts(event: CallbackQuery):
    if not account_pool.accounts:
        text = "ℹ️ Нет доступных аккаунтов. Используйте кнопку 'Добавить аккаунт', чтобы добавить."
        keyboard = [
            [{"text": "📱 Добавить аккаунт", "callback_data": "add_account"}],
            [{"text": "Главная", "callback_data": "start"}],
        ]
        await smart_answer(
            event, bot, text, reply_markup=kb(keyboard), delete_origin=True
        )
        return

    text = "📋 <b>Доступные аккаунты:</b>\n\n"
    for i, acc in enumerate(account_pool.accounts, 1):
        status = "🟢 Свободен" if not acc["in_use"] else "🔴 Используется"
        validity = "🟢 Рабочий" if acc["is_valid"] else "🔴 Не рабочий"
        flood = (
            f"⏳ FloodWait до {acc['flood_wait_until']}"
            if acc.get("flood_wait_until")
            else ""
        )
        text += f"{i}. <code>{acc['session_file']}</code>\nСтатус: {status} | {validity} {flood}\nПоследнее использование: {acc['last_used'] or 'Никогда'}\n\n"

    keyboard = [[{"text": "Главная", "callback_data": "start"}]]
    if event.from_user.id in Config.ADMIN_USER_IDS:
        keyboard.insert(
            0, [{"text": "🔄 Обновить список", "callback_data": "list_accounts"}]
        )

    await smart_answer(event, bot, text, reply_markup=kb(keyboard), delete_origin=True)


@router.callback_query(F.data == "genkey")
async def cmd_genkey(event: CallbackQuery, state: FSMContext):
    if event.from_user.id not in Config.ADMIN_USER_IDS:
        await smart_answer(
            event,
            bot,
            "🚫 Только администраторы могут генерировать ключи доступа!",
            show_alert=True,
        )
        return

    text = "🔑 <b>Генерация ключа доступа</b>\n\nПожалуйста, введите ID пользователя, для которого нужно сгенерировать ключ:"
    keyboard = [[{"text": "Отмена", "callback_data": "cancel"}]]
    await smart_answer(event, bot, text, reply_markup=kb(keyboard), delete_origin=True)
    await state.set_state(KeyGeneration.waiting_user_id)


@router.message(KeyGeneration.waiting_user_id)
async def process_user_id(message: Message, state: FSMContext):
    try:
        user_id = int(message.text.strip())
        key = await auth_manager.generate_key(user_id)
        text = f"✅ <b>Ключ успешно сгенерирован!</b>\n\n• ID пользователя: <code>{user_id}</code>\n• Ключ доступа: <code>{key}</code>\n\nПередайте этот ключ пользователю. После ввода ключа пользователь получит доступ к боту."
        keyboard = [[{"text": "Главная", "callback_data": "start"}]]
        await smart_answer(
            message, bot, text, reply_markup=kb(keyboard), delete_origin=False
        )
        await state.clear()
    except ValueError:
        await smart_answer(
            message,
            bot,
            "❌ ID должен быть числом. Попробуйте снова:",
            delete_origin=False,
        )


@router.callback_query(F.data == "start_scraping")
async def cmd_start_scraping(event: CallbackQuery, state: FSMContext):
    if not account_pool.accounts:
        text = "❌ Нет доступных аккаунтов! Сначала добавьте аккаунты с помощью кнопки 'Добавить аккаунт'"
        keyboard = [
            [{"text": "📱 Добавить аккаунт", "callback_data": "add_account"}],
            [{"text": "Главная", "callback_data": "start"}],
        ]
        await smart_answer(
            event, bot, text, reply_markup=kb(keyboard), delete_origin=True
        )
        return

    if not task_queue.can_user_add_task(event.from_user.id):
        text = f"❌ Вы уже имеете максимальное количество активных задач ({Config.MAX_TASKS_PER_USER}).\nДождитесь завершения текущей задачи."
        keyboard = [
            [{"text": "📊 Мои задачи", "callback_data": "my_tasks"}],
            [{"text": "Главная", "callback_data": "start"}],
        ]
        await smart_answer(
            event, bot, text, reply_markup=kb(keyboard), delete_origin=True
        )
        return

    text = "🔍 <b>Шаг 1/4</b>\nОтправьте @username или пригласительную ссылку чата/канала, из которого нужно собрать пользователей:"
    keyboard = [[{"text": "Отмена", "callback_data": "cancel"}]]
    await smart_answer(event, bot, text, reply_markup=kb(keyboard), delete_origin=True)
    await state.set_state(ScrapingStates.waiting_source)


@router.message(ScrapingStates.waiting_source)
async def process_source(message: Message, state: FSMContext):
    source = message.text.strip()
    await state.update_data(source=source)
    text = "🎯 <b>Шаг 2/4</b>\nОтправьте @username или пригласительную ссылку группы/канала, в которую нужно пригласить пользователей:"
    keyboard = [[{"text": "Отмена", "callback_data": "cancel"}]]
    await smart_answer(
        message, bot, text, reply_markup=kb(keyboard), delete_origin=False
    )
    await state.set_state(ScrapingStates.waiting_target)


@router.message(ScrapingStates.waiting_target)
async def process_target(message: Message, state: FSMContext):
    target = message.text.strip()
    await state.update_data(target=target)
    text = "⚙️ <b>Шаг 3/4</b>\nВыберите режим сбора:\n\n1️⃣ Обработка последних сообщений\n2️⃣ Количество пользователей\n\n<b>Напишите цифру 1 или 2:</b>"
    keyboard = [
        [{"text": "1️⃣ Сообщения", "callback_data": "mode:1"}],
        [{"text": "2️⃣ Пользователи", "callback_data": "mode:2"}],
        [{"text": "Отмена", "callback_data": "cancel"}],
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
        text = "📊 <b>Шаг 4/4</b>\nВведите количество сообщений для анализа (рекомендуется 1000-5000):\n\n<b>Пример: 2000</b>"
        keyboard = [[{"text": "Отмена", "callback_data": "cancel"}]]
        await smart_answer(
            event, bot, text, reply_markup=kb(keyboard), delete_origin=True
        )
        await state.set_state(ScrapingStates.waiting_message_limit)
    elif mode == "2":
        text = "📊 <b>Шаг 4/4</b>\nВведите количество пользователей для приглашения (от 10 до 1000):\n\n<b>Пример: 100</b>"
        keyboard = [[{"text": "Отмена", "callback_data": "cancel"}]]
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
            "⚠️ Неверное число! Пожалуйста, введите значение от 50 до 5000.",
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
            "⚠️ Неверное число! Введите от 10 до 1000.",
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
            "⚠️ Нет доступных аккаунтов. Сначала добавьте хотя бы один.",
            reply_markup=kb(
                [[{"text": "➕ Добавить аккаунт", "callback_data": "add_account"}]]
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
            f"✅ Используем аккаунт {accounts_list[0]['session_file']}",
            delete_origin=False,
        )
        await launch_scraping_task(event, state)
        return

    lines = ["🧾 <b>Шаг 5/5</b>\nВыберите аккаунт или 'auto':"]
    keyboard_rows = [[{"text": "🤖 Авто", "callback_data": "acc:auto"}]]
    for i, acc in enumerate(accounts_list, 1):
        status = "🟢" if acc["is_valid"] else "⚫"
        busy = " ⏳" if acc["in_use"] else ""
        flood = " ⏱" if acc.get("flood_wait_until") else ""
        label = f"{i}. {acc['session_file'][:18]} {status}{busy}{flood}"
        keyboard_rows.append(
            [{"text": label, "callback_data": f"acc:{acc['session_file']}"}]
        )
    keyboard_rows.append([{"text": "Отмена", "callback_data": "cancel"}])

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

    text = (
        f"✅ <b>Задача запущена!</b>\n\n"
        f"• ID: <code>{task_id}</code>\n"
        f"• Источник: {source}\n"
        f"• Цель: {target}\n"
        f"• Режим: {'сообщения' if mode=='1' else 'пользователи'}\n"
        f"• Аккаунт: {sender_session or 'авто'}"
    )
    keyboard = [
        [{"text": "📊 Мои задачи", "callback_data": "my_tasks"}],
        [{"text": "Главная", "callback_data": "start"}],
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
        text = f"❌ Вы уже имеете максимальное количество активных задач ({Config.MAX_TASKS_PER_USER}).\nДождитесь завершения текущей задачи."
        keyboard = [
            [{"text": "📊 Мои задачи", "callback_data": "my_tasks"}],
            [{"text": "Главная", "callback_data": "start"}],
        ]
        await smart_answer(
            event, bot, text, reply_markup=kb(keyboard), delete_origin=True
        )
        return

    text = "✉️ <b>Массовая рассылка</b>\n\n<b>Шаг 1/4</b>\nОтправьте список чатов/каналов, через пробел, запятую или с новой строки.\n\n<b>Формат:</b>\n@chat1 @chat2\nhttps://t.me/xxxx"
    keyboard = [[{"text": "Отмена", "callback_data": "cancel"}]]
    await smart_answer(event, bot, text, reply_markup=kb(keyboard), delete_origin=True)
    await state.set_state(BulkMailStates.waiting_chats)


@router.message(BulkMailStates.waiting_chats)
async def bm_waiting_chats(message: Message, state: FSMContext):
    raw = message.text.strip()
    parts = [p.strip() for p in raw.replace(",", " ").split() if p.strip()]
    if not parts:
        await smart_answer(
            message,
            bot,
            "⚠️ Список чатов пуст. Пожалуйста, отправьте корректный список.",
            delete_origin=False,
        )
        return

    await state.update_data(chats=parts)
    text = "⏱️ <b>Шаг 2/4</b>\nВведите задержку между отправками в секундах в формате: <code>min max</code>\n\n<b>Пример:</b>\n<code>10 20</code> (будет случайная задержка от 10 до 20 секунд)"
    keyboard = [
        [{"text": "5 10", "callback_data": "delay:5:10"}],
        [{"text": "10 20", "callback_data": "delay:10:20"}],
        [{"text": "20 30", "callback_data": "delay:20:30"}],
        [{"text": "30 60", "callback_data": "delay:30:60"}],
        [{"text": "Отмена", "callback_data": "cancel"}],
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
    text = "📝 <b>Шаг 3/4</b>\nОтправьте текст сообщения, которое нужно разослать."
    keyboard = [[{"text": "Отмена", "callback_data": "cancel"}]]
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
            "⚠️ Неверный формат. Введите две неотрицательные цифры: min max (min <= max).",
            delete_origin=False,
        )
        return

    await state.update_data(delay_min=dmin, delay_max=dmax)
    text = "📝 <b>Шаг 3/4</b>\nОтправьте текст сообщения, которое нужно разослать."
    keyboard = [[{"text": "Отмена", "callback_data": "cancel"}]]
    await smart_answer(
        message, bot, text, reply_markup=kb(keyboard), delete_origin=False
    )
    await state.set_state(BulkMailStates.waiting_text)


@router.message(BulkMailStates.waiting_text)
async def bm_waiting_text(message: Message, state: FSMContext):
    text_msg = message.text
    if not text_msg or not text_msg.strip():
        await smart_answer(
            message,
            bot,
            "⚠️ Сообщение не может быть пустым. Введите текст сообщения.",
            delete_origin=False,
        )
        return

    await state.update_data(message_text=text_msg)
    accounts_list = account_pool.accounts

    if not accounts_list:
        text = "🔢 <b>Шаг 4/4</b>\nНет доступных аккаунтов — рассылка будет выполняться автоматически из пула.\nВведите общее количество отправок (целое число, например 100)."
        keyboard = [
            [{"text": "50", "callback_data": "total:50"}],
            [{"text": "100", "callback_data": "total:100"}],
            [{"text": "200", "callback_data": "total:200"}],
            [{"text": "500", "callback_data": "total:500"}],
            [{"text": "Отмена", "callback_data": "cancel"}],
        ]
        await smart_answer(
            message, bot, text, reply_markup=kb(keyboard), delete_origin=False
        )
        await state.set_state(BulkMailStates.waiting_count)
        return

    lines = [
        "🧾 <b>Шаг 4/4</b>\nВыберите аккаунт-отправитель или введите 'auto' для автоматического распределения:"
    ]
    for i, acc in enumerate(accounts_list, 1):
        status = "🔴" if acc["in_use"] else "🟢" if acc["is_valid"] else "⚫"
        flood = f" ⏳ flood" if acc.get("flood_wait_until") else ""
        lines.append(f"{i}. {acc['session_file']} {status}{flood}")

    lines.append("\n<b>Введите номер аккаунта (например 1) или 'auto':</b>")

    keyboard = (
        [[{"text": "🤖 Авто (auto)", "callback_data": "sender:auto"}]]
        + [
            [
                {
                    "text": f"{i}. {acc['session_file'][:20]}",
                    "callback_data": f"sender:{acc['session_file']}",
                }
            ]
            for i, acc in enumerate(accounts_list, 1)
        ]
        + [[{"text": "Отмена", "callback_data": "cancel"}]]
    )

    await smart_answer(
        message, bot, "\n".join(lines), reply_markup=kb(keyboard), delete_origin=False
    )
    await state.set_state(BulkMailStates.waiting_sender)


@router.callback_query(F.data.startswith("sender:"), BulkMailStates.waiting_sender)
async def bm_waiting_sender_callback(event: CallbackQuery, state: FSMContext):
    sender = event.data.split(":", 1)[1]
    await state.update_data(sender_session=sender if sender != "auto" else None)

    text = f"✅ Выбран аккаунт: <code>{sender if sender != 'auto' else 'авто (пул аккаунтов)'}</code>\n\n<b>Введите общее количество отправок (целое число):</b>"
    keyboard = [
        [{"text": "50", "callback_data": "total:50"}],
        [{"text": "100", "callback_data": "total:100"}],
        [{"text": "200", "callback_data": "total:200"}],
        [{"text": "500", "callback_data": "total:500"}],
        [{"text": "Отмена", "callback_data": "cancel"}],
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
            "⚠️ Неверное число. Введите положительное целое количество отправок.",
            delete_origin=False,
        )
        return

    await process_bulk_mailing_final(message, state, total)


async def process_bulk_mailing_final(event, state: FSMContext, total: int):
    data = await state.get_data()
    chats = data.get("chats", [])
    delay_min = data.get("delay_min", 1)
    delay_max = data.get("delay_max", 1)
    message_text = data.get("message_text", "")
    sender_session = data.get("sender_session", None)
    user_id = event.from_user.id

    task_id = f"mail_{user_id}_{int(time.time())}"
    task_data = {
        "task_id": task_id,
        "user_id": user_id,
        "type": "mailing",
        "chats": chats,
        "delay_min": delay_min,
        "delay_max": delay_max,
        "message_text": (
            message_text[:200] + "..." if len(message_text) > 200 else message_text
        ),
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
        message_text=message_text,
        total_sends=total,
        user_id=user_id,
        sender_session_file=sender_session,
        task_id=task_id,
    )

    safe_preview = html.escape(
        message_text[:200] + "..." if len(message_text) > 200 else message_text
    )
    sender_info = (
        f"• Отправитель: {sender_session}"
        if sender_session
        else "• Отправитель: авто (пул аккаунтов)"
    )

    text = f"✅ <b>Задача массовой рассылки запущена!</b>\n\n• ID задачи: <code>{task_id}</code>\n• Чатов: {len(chats)}\n• Задержка: {delay_min}-{delay_max} сек\n{sender_info}\n• Текст сообщения: (первые 200 символов)\n\n{safe_preview}\n\n• Всего отправок: {total}\n\nВы получите отчет по завершении."
    keyboard = [
        [{"text": "📊 Мои задачи", "callback_data": "my_tasks"}],
        [{"text": "Главная", "callback_data": "start"}],
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

    text = f"{icon} <b>Задача ID:</b> <code>{task_id}</code>\n"
    text += f"<b>Тип:</b> {task_type}\n"
    text += f"<b>Статус:</b> {status}\n"
    text += f"<b>Создана:</b> {time_str}\n"

    if task_type == "scraping":
        source = task.get("source", "")
        target = task.get("target", "")
        mode = task.get("mode", "")
        if mode == "messages":
            limit = task.get("limit", 0)
            text += f"<b>Источник:</b> {source}\n"
            text += f"<b>Цель:</b> {target}\n"
            text += f"<b>Сообщений:</b> {limit}\n"
        else:
            user_count = task.get("user_count", 0)
            text += f"<b>Источник:</b> {source}\n"
            text += f"<b>Цель:</b> {target}\n"
            text += f"<b>Пользователей:</b> {user_count}\n"

    elif task_type == "mailing":
        chats = task.get("chats", [])
        total_sends = task.get("total_sends", 0)
        sent = task.get("sent", 0)
        text += f"<b>Чатов:</b> {len(chats)}\n"
        text += f"<b>Отправлено:</b> {sent}/{total_sends}\n"

    if status in ["pending", "running", "paused"]:
        text += f"\n<b>Прогресс:</b>\n{progress_bar}\n"
        text += f"<b>Текущий статус:</b> {progress_text}"

    keyboard = []
    can_control = for_admin or task.get("user_id") == user_id

    if status == "running" and can_control:
        keyboard.append(
            [
                {"text": "⏸ Пауза", "callback_data": f"pause_task:{task_id}"},
                {"text": "❌ Отмена", "callback_data": f"cancel_task_id:{task_id}"},
            ]
        )
    elif status == "paused" and can_control:
        keyboard.append(
            [
                {"text": "▶️ Возобновить", "callback_data": f"resume_task:{task_id}"},
                {"text": "❌ Отмена", "callback_data": f"cancel_task_id:{task_id}"},
            ]
        )
    elif status == "pending" and can_control:
        keyboard.append(
            [
                {
                    "text": "❌ Отменить задачу",
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
                        "text": "🚪 Выйти из чатов",
                        "callback_data": f"leave_chats:{task_id}",
                    }
                ]
            )

    keyboard.append(
        [{"text": "🔄 Обновить", "callback_data": f"refresh_task:{task_id}"}]
    )

    # Для администратора - информация о пользователе
    if for_admin:
        task_user_id = task.get("user_id")
        keyboard.append(
            [
                {
                    "text": f"👤 Пользователь: {task_user_id}",
                    "callback_data": f"user_info:{task_user_id}",
                }
            ]
        )

    keyboard.append(
        [
            {
                "text": "📋 Все задачи",
                "callback_data": "task_list" if for_admin else "my_tasks",
            }
        ]
    )
    keyboard.append([{"text": "Главная", "callback_data": "start"}])

    return text, kb(keyboard)


@router.callback_query(F.data == "my_tasks")
async def cmd_my_tasks(event: CallbackQuery):
    user_id = event.from_user.id
    tasks = await tasks_storage.read_all()
    user_tasks = [t for t in tasks if t.get("user_id") == user_id]

    if not user_tasks:
        text = "📭 <b>У вас нет задач</b>\n\nНачните новую задачу, используя меню."
        keyboard = [
            [{"text": "🔍 Начать сбор", "callback_data": "start_scraping"}],
            [{"text": "📨 Массовая рассылка", "callback_data": "bulk_mailing"}],
            [{"text": "Главная", "callback_data": "start"}],
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
        text = "📭 <b>У вас нет активных задач</b>"
        keyboard = [
            [{"text": "🔍 Начать сбор", "callback_data": "start_scraping"}],
            [{"text": "📨 Массовая рассылка", "callback_data": "bulk_mailing"}],
            [{"text": "Главная", "callback_data": "start"}],
        ]
        await smart_answer(
            event, bot, text, reply_markup=kb(keyboard), delete_origin=True
        )

    # Показываем неактивные задачи списком (если есть)
    if inactive_tasks:
        text = "📁 <b>Завершенные задачи:</b>\n\n"
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

            text += (
                f"{i}. {icon} <code>{task_id[:10]}...</code> - {task_type} - {status}\n"
            )

        keyboard = [
            [
                {
                    "text": "📊 Все задачи",
                    "callback_data": (
                        "task_list" if user_id in Config.ADMIN_USER_IDS else "my_tasks"
                    ),
                }
            ],
            [{"text": "Главная", "callback_data": "start"}],
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
            "⛔ Эта команда доступна только администраторам!",
            show_alert=True,
        )
        return

    tasks = await tasks_storage.read_all()
    active_tasks = [
        t for t in tasks if t.get("status") in ["pending", "running", "paused"]
    ]

    if not active_tasks:
        text = "📭 <b>Нет активных задач</b>"
        keyboard = [[{"text": "Главная", "callback_data": "start"}]]
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
        await smart_answer(event, bot, "❌ Задача не найдена", show_alert=True)
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
        await smart_answer(event, bot, "❌ Задача не найдена", show_alert=True)
        return

    user_id = event.from_user.id
    task_user_id = task_data.get("user_id")
    if user_id not in Config.ADMIN_USER_IDS and user_id != task_user_id:
        await smart_answer(
            event, bot, "⛔ У вас нет прав ставить задачу на паузу", show_alert=True
        )
        return

    await task_queue.pause_task(task_id)
    await smart_answer(event, bot, f"⏸ Задача {task_id} на паузе", show_alert=True)

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
        await smart_answer(event, bot, "❌ Задача не найдена", show_alert=True)
        return

    user_id = event.from_user.id
    task_user_id = task_data.get("user_id")
    if user_id not in Config.ADMIN_USER_IDS and user_id != task_user_id:
        await smart_answer(
            event, bot, "⛔ У вас нет прав возобновлять эту задачу", show_alert=True
        )
        return

    if task_id not in task_queue.active_tasks:
        await queue_task_from_storage(task_data, resume=True)

    await task_queue.resume_task(task_id)
    await smart_answer(event, bot, f"▶️ Задача {task_id} возобновлена", show_alert=True)

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
        await smart_answer(event, bot, "❌ Задача не найдена", show_alert=True)
        return

    user_id = event.from_user.id
    task_user_id = task_data.get("user_id")

    # Проверяем права
    if user_id not in Config.ADMIN_USER_IDS and user_id != task_user_id:
        await smart_answer(
            event, bot, "❌ У вас нет прав для отмены этой задачи", show_alert=True
        )
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
        await notify_user(bot, user_id, f"✅ Ваша задача {task_id} отменена!")
    else:
        await notify_user(
            bot, task_user_id, f"✅ Ваша задача {task_id} отменена администратором!"
        )
        await notify_user(
            bot, user_id, f"✅ Задача {task_id} пользователя {task_user_id} отменена!"
        )

    await smart_answer(event, bot, f"✅ Задача {task_id} отменена!", show_alert=True)

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
        await smart_answer(event, bot, "❌ Задача не найдена", show_alert=True)
        return

    user_id = event.from_user.id
    task_user_id = task_data.get("user_id")
    if user_id not in Config.ADMIN_USER_IDS and user_id != task_user_id:
        await smart_answer(
            event, bot, "⛔ У вас нет прав на это действие", show_alert=True
        )
        return

    joined_chats = task_data.get("joined_chats") or []
    if not joined_chats:
        await smart_answer(event, bot, "⚠️ Нечего покидать", show_alert=True)
        return

    sender_session = task_data.get("sender_session")
    if not sender_session:
        await smart_answer(
            event,
            bot,
            "⚠️ Неизвестен аккаунт для выхода (sender_session не сохранён)",
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
            event, bot, f"❌ Не удалось выполнить выход: {e}", show_alert=True
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

    msg_lines = ["🚪 Выход выполнен:"]
    if left:
        msg_lines.append("• Покинуты: " + ", ".join(left))
    if errors:
        msg_lines.append("• Ошибки: " + "; ".join([f"{c} ({e})" for c, e in errors]))
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

    stats = (
        "📊 <b>Статистика задач</b>\n\n"
        f"• Всего задач: {total}\n"
        f"• В ожидании: {pending}\n"
        f"• Выполняются: {running}\n"
        f"• На паузе: {paused}\n"
        f"• Завершены: {completed}\n"
        f"• Отменены: {cancelled}\n"
        f"• Ошибки: {failed}\n\n"
        "<b>По типам:</b>\n"
        f"• Сбор пользователей: {scraping}\n"
        f"• Массовая рассылка: {mailing}\n\n"
        "<b>Очередь:</b>\n"
        f"• Активные задачи: {len(task_queue.active_tasks)}\n"
        f"• Задачи в очереди: {task_queue.queue.qsize()}\n"
        f"• Доступные аккаунты: {len([a for a in account_pool.accounts if not a['in_use'] and a['is_valid']])}/{len(account_pool.accounts)}"
    )

    keyboard = [
        [{"text": "📋 Список задач", "callback_data": "task_list"}],
        [{"text": "🔄 Обновить", "callback_data": "task_stats"}],
        [{"text": "Главная", "callback_data": "start"}],
    ]

    await smart_answer(event, bot, stats, reply_markup=kb(keyboard), delete_origin=True)


@router.callback_query(F.data == "clear_cache")
async def cmd_clear_cache(event: CallbackQuery, state: FSMContext):
    user_id = event.from_user.id
    if user_id not in Config.ADMIN_USER_IDS:
        await smart_answer(
            event,
            bot,
            "⛔ Эта команда доступна только администраторам!",
            show_alert=True,
        )
        return

    text = "🗑️ <b>Сброс кэша</b>\n\nВы уверены, что хотите очистить весь кэш участников чатов?\nЭто действие нельзя отменить."
    keyboard = [
        [{"text": "✅ Да, очистить", "callback_data": "clear_cache_confirm"}],
        [{"text": "❌ Нет, отмена", "callback_data": "start"}],
    ]
    await smart_answer(event, bot, text, reply_markup=kb(keyboard), delete_origin=True)


@router.callback_query(F.data == "clear_cache_confirm")
async def process_clear_cache_confirm(event: CallbackQuery):
    cleared = await cache_manager.clear_cache()
    text = f"✅ <b>Кэш очищен!</b>\n\nУдалено записей: {cleared}"
    keyboard = [[{"text": "Главная", "callback_data": "start"}]]
    await smart_answer(event, bot, text, reply_markup=kb(keyboard), delete_origin=True)


@router.callback_query(F.data == "help")
async def cmd_help(event: CallbackQuery):
    text = "📚 <b>Руководство по использованию бота</b>\n\n1. <b>Добавление аккаунтов</b> - используйте кнопку 'Добавить аккаунт' для добавления ваших аккаунтов Telegram\n2. <b>Начать сбор</b> - используйте 'Начать сбор' для сбора пользователей\n3. <b>Приглашение пользователей</b> - собранные пользователи будут приглашены в вашу целевую группу\n\n⚙️ <b>Как это работает:</b>\n- Я анализирую сообщения в исходном чате\n- Собираю активных пользователей\n- Приглашаю их в вашу целевую группу\n\n⚠️ <b>Безопасность:</b>\n- Ваши другие сессии Telegram НЕ будут завершены\n- Сессии надежно хранятся и никогда не передаются третьим лицам"
    keyboard = [[{"text": "Главная", "callback_data": "start"}]]
    await smart_answer(event, bot, text, reply_markup=kb(keyboard), delete_origin=True)


@router.callback_query(F.data == "ref")
async def cmd_ref(event: CallbackQuery):
    text = "💸 <b>Зарабатывай с рефералкой:</b>\n\n👥 <b>1 человек</b> = +200₽\n👥 <b>3 человека</b> = +700₽\n👥 <b>5 человек</b> = +1500₽\n👥 <b>10 человек</b> = +4000₽\n\nЧтобы реферал считался приведённым вами он должен при регистрации сообщить ваш юзернейм."
    keyboard = [[{"text": "Главная", "callback_data": "start"}]]
    await smart_answer(event, bot, text, reply_markup=kb(keyboard), delete_origin=True)


# --- Исправленные задачи ---
async def ensure_join_target(
    client: TelegramClient,
    target: str,
    account: Dict[str, Any],
    task_id: str,
    user_id: int,
):
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
                "error": f"Не удалось вступить в {target}: {e}",
                "completed_at": datetime.now().isoformat(),
            },
            id_field="task_id",
        )
        await notify_user(bot, user_id, f"❌ Не удалось вступить в {target}: {e}")
        return None


async def ensure_leave_target(client: TelegramClient, target: str):
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
                        "progress_text": f"📊 Сбор: {progress_text} ({processed}/{limit})",
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
                    f"⏳ Задача {task_id}: floodwait {wait_seconds}с, ставлю на паузу",
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
                        "progress_text": f"📊 Инвайты: {format_progress_bar(progress)} ({processed}/{total_users})",
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
                    bot, user_id, f"❌ Не найдено активных пользователей в {source}"
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
                f"🔄 Задача {task_id}: приглашаю {len(collected_users)} пользователей",
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
                await notify_user(bot, user_id, f"⏹ Задача {task_id} отменена")
                return

            await tasks_storage.update_by_id(
                task_id,
                {
                    "status": "completed",
                    "results": results,
                    "completed_at": datetime.now().isoformat(),
                    "progress": 100,
                    "progress_text": f"{len(collected_users)}/{len(collected_users)}",
                    "checkpoints": {"remaining_users": []},
                },
                id_field="task_id",
            )
            task_queue.remove_user_task(user_id, task_id)

            await notify_user(
                bot,
                user_id,
                f"✅ Задача {task_id} завершена\nИсточник: {source}\nЦель: {target}\nПриглашено: {len(collected_users)}\nУспехов: {results['success']}\nФэйлов: {results['failed']}\nПриватность: {results['privacy_errors']}\nАккаунт: {account['session_file']}",
            )
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
            f"🔥 <b>Задача {task_id} не выполнена!</b>\n\nОшибка: {str(e)}",
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
                    bot, user_id, f"❌ Не найдено активных пользователей в {source}"
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
                f"🔄 Задача {task_id}: приглашаю {len(collected_users)} пользователей",
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
                await notify_user(bot, user_id, f"⏹ Задача {task_id} отменена")
                return

            await tasks_storage.update_by_id(
                task_id,
                {
                    "status": "completed",
                    "results": results,
                    "completed_at": datetime.now().isoformat(),
                    "progress": 100,
                    "progress_text": f"{len(collected_users)}/{len(collected_users)}",
                    "checkpoints": {"remaining_users": []},
                },
                id_field="task_id",
            )
            task_queue.remove_user_task(user_id, task_id)

            await notify_user(
                bot,
                user_id,
                f"✅ Задача {task_id} завершена\nИсточник: {source}\nЦель: {target}\nСообщений: {message_limit}\nАктивных пользователей: {len(collected_users)}\nУспехов: {results['success']}\nФэйлов: {results['failed']}\nПриватность: {results['privacy_errors']}\nАккаунт: {account['session_file']}",
            )
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
            f"🔥 <b>Задача {task_id} не выполнена!</b>\n\nОшибка: {str(e)}",
        )


async def bulk_mailing_task(
    control: TaskControl,
    chats: List[str],
    delay_min: int,
    delay_max: int,
    message_text: str,
    total_sends: int,
    user_id: int,
    sender_session_file: Optional[str] = None,
    task_id: Optional[str] = None,
    checkpoint: Optional[Dict[str, Any]] = None,
):
    logger.info(
        f"Starting bulk mailing: chats={len(chats)} total_sends={total_sends} sender={sender_session_file or 'auto'}"
    )

    if control.cancelled:
        return

    sent = 0
    per_chat_sent = {c: 0 for c in chats}
    next_chat_idx = 0

    if checkpoint:
        sent = checkpoint.get("sent", 0)
        per_chat_sent.update(checkpoint.get("per_chat_sent", {}))
        next_chat_idx = checkpoint.get("next_chat_idx", 0)

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
                    try:
                        # Безопасно получаем сущность чата
                        target = (
                            await account_pool._safe_get_user(client, chat)
                            if isinstance(chat, int)
                            else chat
                        )
                        if isinstance(chat, str):
                            target = await client.get_entity(chat)

                        # Анти-блокировочная задержка
                        if sent > 0:
                            delay = random.uniform(
                                delay_min + 5, delay_max + 10  # Добавляем буфер
                            )
                            logger.info(f"Mailing delay: {delay:.1f}s")
                            await asyncio.sleep(delay)

                        # Безопасная отправка с retry
                        for attempt in range(Config.MAX_RETRIES):
                            try:
                                await client.send_message(target, message_text)
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
                                if attempt < Config.MAX_RETRIES - 1:
                                    await asyncio.sleep(2**attempt)
                                    continue
                                raise

                        sent += 1
                        per_chat_sent[chat] = per_chat_sent.get(chat, 0) + 1
                        next_chat_idx += 1

                        # Обновление прогресса каждые 10 отправок
                        if sent % 10 == 0 or sent == total_sends:
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
                                    "sender_session": account["session_file"],
                                },
                            },
                            id_field="task_id",
                        )
                        await notify_user(
                            bot,
                            user_id,
                            f"⏳ Задача {task_id}: floodwait {wait_seconds}с (удлинено до {extended_wait:.0f}с), пауза",
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
                        "sender_session": sender_session_file,
                    },
                },
                id_field="task_id",
            )
            task_queue.remove_user_task(user_id, task_id)
            await notify_user(bot, user_id, f"⏹ Рассылка {task_id} отменена")
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
                    "sender_session": sender_session_file,
                },
            },
            id_field="task_id",
        )
        task_queue.remove_user_task(user_id, task_id)

        report_lines = [
            "📬 <b>Массовая рассылка завершена!</b>",
            f"• Задача: <code>{task_id}</code>",
            f"• Всего отправлено: {sent}",
            "• Отправлено по чатам:",
        ]
        for c, cnt in per_chat_sent.items():
            report_lines.append(f"  - {c}: {cnt}")

        await notify_user(bot, user_id, "\n".join(report_lines))

    except Exception as e:
        logger.exception("Bulk mailing task failed")
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
            bot, user_id, f"🔥 <b>Задача {task_id} не выполнена!</b>\n\nОшибка: {e}"
        )


async def queue_task_from_storage(task: Dict[str, Any], resume: bool = False):
    task_id = task.get("task_id")
    task_type = task.get("type")
    user_id = task.get("user_id")
    checkpoint = task.get("checkpoints")

    if not task_id or not task_type or not user_id:
        return

    if task_type == "scraping":
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
            message_text=task.get("message_text", ""),
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
                    "progress_text": "Пауза после рестарта",
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

        task_queue.start_workers()
        await account_pool.start_health_check()
        await restore_tasks_on_startup()

        # Фоновые задачи
        asyncio.create_task(cleanup_old_tasks())
        asyncio.create_task(cleanup_entity_cache())
        asyncio.create_task(simulate_account_activity())

        for admin_id in Config.ADMIN_USER_IDS:
            await notify_user(
                bot,
                admin_id,
                f"🟢 <b>Бот успешно запущен!</b>\n\n"
                f"• Загружено аккаунтов: {len(account_pool.accounts)}\n"
                f"• Максимум одновременных задач: {Config.MAX_CONCURRENT_TASKS}\n"
                f"• Максимум задач на пользователя: {Config.MAX_TASKS_PER_USER}\n"
                f"• Задержка приглашений: {Config.MIN_INVITE_DELAY}-{Config.MAX_INVITE_DELAY}с\n"
                f"• Множитель FloodWait: {Config.FLOOD_WAIT_MULTIPLIER}x\n"
                f"• Максимум попыток: {Config.MAX_RETRIES}\n"
                f"• Эмуляция активности: активна\n"
                f"• Кэширование сущностей: активно\n\n"
                f"⚠️ <b>Важно:</b> Ваши аккаунты теперь контролируются ботом. Ваши другие сессии Telegram останутся активными.",
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
        await auth_manager.save()

        for admin_id in Config.ADMIN_USER_IDS:
            await notify_user(
                bot,
                admin_id,
                "🔴 <b>Бот остановлен!</b>\n\nВсе сессии Telegram были освобождены. Теперь вы можете использовать свои аккаунты как обычно.",
            )

        await bot.session.close()
        logger.info("All accounts released. Bot shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
