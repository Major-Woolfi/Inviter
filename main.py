import asyncio
import html
import json
import logging
import os
import random
import secrets
import sys
import time
import uuid
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set

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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))
CONFIG_WARNINGS: List[str] = []


def str_to_bool(val: str) -> bool:
    return str(val).strip().lower() in ("1", "true", "yes", "y", "on")


def env_str(name: str, default: str) -> str:
    val = os.getenv(name)
    if val is None or not str(val).strip():
        return default
    return str(val).strip()


def env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or not str(val).strip():
        return default
    try:
        return int(val)
    except ValueError:
        return default


def resolve_path(value: str, default: str) -> str:
    path = str(value).strip() if value is not None else ""
    if not path:
        path = default
    if not path:
        return ""
    path = os.path.expandvars(os.path.expanduser(path))
    if not os.path.isabs(path):
        path = os.path.join(BASE_DIR, path)
    return os.path.normpath(path)


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _is_under(path: str, root: str) -> bool:
    if not path or not root:
        return False
    root_norm = _norm_path(root)
    path_norm = _norm_path(path)
    if path_norm == root_norm:
        return True
    return path_norm.startswith(root_norm + os.sep)


def ensure_writable_dir(path: str, fallback_subdir: str, label: str):
    if not path:
        return "", None, None
    try:
        os.makedirs(path, exist_ok=True)
        test_path = os.path.join(path, "._write_test")
        with open(test_path, "a", encoding="utf-8"):
            pass
        try:
            os.remove(test_path)
        except OSError:
            pass
        return path, None, None
    except OSError as exc:
        fallback = os.path.join(tempfile.gettempdir(), "inviter", fallback_subdir)
        try:
            os.makedirs(fallback, exist_ok=True)
            test_path = os.path.join(fallback, "._write_test")
            with open(test_path, "a", encoding="utf-8"):
                pass
            try:
                os.remove(test_path)
            except OSError:
                pass
        except OSError as exc2:
            warn = (
                f"{label}: не могу писать в '{path}' ({exc}); "
                f"и в '{fallback}' ({exc2})."
            )
            return path, warn, None
        warn = (
            f"{label}: не могу писать в '{path}' ({exc}). "
            f"Будет использована '{fallback}'."
        )
        return fallback, warn, path


class Config:
    BOT_TOKEN: str = env_str("BOT_TOKEN", "")
    API_ID: int = env_int("API_ID", 0)
    API_HASH: str = env_str("API_HASH", "")
    ADMIN_USER_IDS: List[int] = []
    _raw_admins = os.getenv("ADMIN_USER_IDS", "")
    for _p in _raw_admins.split(","):
        _p = _p.strip()
        if not _p:
            continue
        try:
            ADMIN_USER_IDS.append(int(_p))
        except ValueError:
            pass

    MAX_CONCURRENT_TASKS: int = env_int("MAX_CONCURRENT_TASKS", 10)
    SESSIONS_DIR: str = resolve_path(os.getenv("SESSIONS_DIR", ""), "sessions")
    DATA_DIR: str = resolve_path(os.getenv("DATA_DIR", ""), "data")
    AUTH_FILE: str = resolve_path(
        os.getenv("AUTH_FILE", ""), os.path.join("data", "auth.json")
    )
    TASKS_FILE: str = resolve_path(
        os.getenv("TASKS_FILE", ""), os.path.join("data", "tasks.json")
    )
    LOG_DIR: str = resolve_path(os.getenv("LOG_DIR", ""), "logs")
    LOG_LEVEL: str = env_str("LOG_LEVEL", "INFO").upper()
    LEGACY_SESSIONS_DIR: Optional[str] = None
    LEGACY_DATA_DIR: Optional[str] = None

    @classmethod
    def ensure_dirs(cls) -> None:
        global CONFIG_WARNINGS
        original_sessions = cls.SESSIONS_DIR
        original_data = cls.DATA_DIR

        sessions_dir, warn, legacy = ensure_writable_dir(
            cls.SESSIONS_DIR, "sessions", "SESSIONS_DIR"
        )
        if warn:
            CONFIG_WARNINGS.append(warn)
        if legacy:
            cls.LEGACY_SESSIONS_DIR = legacy
        cls.SESSIONS_DIR = sessions_dir

        data_dir, warn, legacy = ensure_writable_dir(cls.DATA_DIR, "data", "DATA_DIR")
        if warn:
            CONFIG_WARNINGS.append(warn)
        if legacy:
            cls.LEGACY_DATA_DIR = legacy
        cls.DATA_DIR = data_dir

        if cls.LEGACY_DATA_DIR:
            if _is_under(cls.AUTH_FILE, cls.LEGACY_DATA_DIR):
                cls.AUTH_FILE = os.path.join(
                    cls.DATA_DIR, os.path.basename(cls.AUTH_FILE)
                )
            if _is_under(cls.TASKS_FILE, cls.LEGACY_DATA_DIR):
                cls.TASKS_FILE = os.path.join(
                    cls.DATA_DIR, os.path.basename(cls.TASKS_FILE)
                )

        if cls.LOG_DIR:
            try:
                os.makedirs(cls.LOG_DIR, exist_ok=True)
            except OSError:
                pass

    @classmethod
    def validate(cls) -> None:
        errors = []
        if not cls.BOT_TOKEN:
            errors.append("BOT_TOKEN is not set")
        if not cls.API_ID:
            errors.append("API_ID is not set")
        if not cls.API_HASH:
            errors.append("API_HASH is not set")
        if errors:
            raise RuntimeError("; ".join(errors))


Config.ensure_dirs()

LOG_LEVEL = getattr(logging, Config.LOG_LEVEL, logging.INFO)
log_handlers = [logging.StreamHandler()]
log_init_warning = None
if Config.LOG_DIR:
    try:
        os.makedirs(Config.LOG_DIR, exist_ok=True)
        log_path = os.path.join(
            Config.LOG_DIR, f"{datetime.now().strftime('%Y-%m-%d')}.log"
        )
        try:
            with open(log_path, "a", encoding="utf-8"):
                pass
        except OSError as exc:
            log_init_warning = (
                f"Не удалось создать лог-файл {log_path}: {exc}. "
                "Продолжаю без FileHandler."
            )
        else:
            log_handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    except OSError as exc:
        log_init_warning = (
            f"Не удалось подготовить директорию логов {Config.LOG_DIR}: {exc}. "
            "Продолжаю без FileHandler."
        )

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    handlers=log_handlers,
)
logger = logging.getLogger(__name__)
if log_init_warning:
    logger.warning(log_init_warning)
for warn in CONFIG_WARNINGS:
    logger.warning(warn)


# --- Утилиты ---


def kb(rows: List[List[Dict[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(**button) for button in row] for row in rows
        ]
    )


def main_menu_keyboard(is_admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [{"text": "Добавить аккаунт", "callback_data": "add_account"}],
        [{"text": "Список аккаунтов", "callback_data": "list_accounts"}],
        [{"text": "Начать сбор", "callback_data": "start_scraping"}],
        [{"text": "Массовая рассылка", "callback_data": "bulk_mailing"}],
        [{"text": "Статистика задач", "callback_data": "task_stats"}],
        [{"text": "Список задач", "callback_data": "task_list"}],
        [{"text": "Справка", "callback_data": "help"}],
        [{"text": "Рефералка", "callback_data": "ref"}],
    ]
    if is_admin:
        rows.insert(0, [{"text": "Сгенерировать ключ", "callback_data": "genkey"}])
    return kb(rows)


def cancel_keyboard() -> InlineKeyboardMarkup:
    return kb([[{"text": "Отмена", "callback_data": "cancel"}]])


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return kb([[{"text": "Главная", "callback_data": "start"}]])


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
    message: str, reply_markup: Optional[InlineKeyboardMarkup] = None
):
    for admin_id in Config.ADMIN_USER_IDS:
        await safe_send_message(bot, admin_id, message, reply_markup=reply_markup)


async def notify_user(
    user_id: int, message: str, reply_markup: Optional[InlineKeyboardMarkup] = None
):
    await safe_send_message(bot, user_id, message, reply_markup=reply_markup)


async def smart_answer(event, text, reply_markup=None, delete_origin=False):
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
    except Exception as e:
        logger.error(f"Ошибка в smart_answer: {e}")


def create_telegram_client(session_string: Optional[str] = None) -> TelegramClient:
    session = StringSession(session_string) if session_string else StringSession()
    return TelegramClient(
        session,
        Config.API_ID,
        Config.API_HASH,
        device_model="Inviter",
        system_version="Linux",
        app_version="2.0",
        system_lang_code="en",
        lang_code="en",
        catch_up=False,
    )


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


class KeyGeneration(StatesGroup):
    waiting_user_id = State()


class BulkMailStates(StatesGroup):
    waiting_chats = State()
    waiting_delay = State()
    waiting_text = State()
    waiting_sender = State()
    waiting_count = State()


# --- AuthManager ---
class AuthManager:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.auth_data = self._load_auth_data()
        self.authorized_users = set(self.auth_data.get("authorized", []))
        logger.info(
            f"Loaded auth data: {len(self.authorized_users)} authorized users, {len(self.auth_data.get('keys', {}))} keys"
        )

    def _load_auth_data(self) -> dict:
        try:
            if os.path.exists(Config.AUTH_FILE):
                with open(Config.AUTH_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if not isinstance(data, dict):
                        return {"keys": {}, "authorized": []}
                    data.setdefault("keys", {})
                    data.setdefault("authorized", [])
                    return data
        except Exception as e:
            logger.error(f"Error loading auth data: {e}")
        return {"keys": {}, "authorized": []}

    def _save_auth_data(self):
        os.makedirs(os.path.dirname(Config.AUTH_FILE), exist_ok=True)
        with open(Config.AUTH_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "keys": self.auth_data.get("keys", {}),
                    "authorized": list(self.authorized_users),
                },
                f,
                ensure_ascii=False,
                indent=2,
            )

    async def save_auth_data(self):
        async with self.lock:
            try:
                self._save_auth_data()
            except Exception as e:
                logger.error(f"Error saving auth data: {e}")

    async def generate_key(self, user_id: int) -> str:
        key = secrets.token_urlsafe(32)
        async with self.lock:
            self.auth_data.setdefault("keys", {})[str(user_id)] = key
            self._save_auth_data()
        return key

    def get_key_for_user(self, user_id: int) -> Optional[str]:
        return self.auth_data.get("keys", {}).get(str(user_id))

    async def verify_key(self, user_id: int, key: str) -> bool:
        async with self.lock:
            stored_key = self.get_key_for_user(user_id)
            if stored_key and stored_key == key:
                self.authorized_users.add(user_id)
                self._save_auth_data()
                return True
        return False

    def is_authorized(self, user_id: int) -> bool:
        return user_id in self.authorized_users or user_id in Config.ADMIN_USER_IDS

    async def add_authorized_user(self, user_id: int):
        async with self.lock:
            self.authorized_users.add(user_id)
            self._save_auth_data()


# --- TaskManager ---
class TaskManager:
    def __init__(self, file_path: str):
        self.file_path = file_path
        self.lock = asyncio.Lock()
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self._last_save: Dict[str, float] = {}
        self._load()
        self._reset_incomplete_tasks()

    def _load(self) -> None:
        if not os.path.exists(self.file_path):
            self.tasks = {}
            return
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "tasks" in data:
                items = data.get("tasks", [])
            elif isinstance(data, list):
                items = data
            else:
                items = []
            self.tasks = {
                str(t.get("id")): t
                for t in items
                if isinstance(t, dict) and t.get("id")
            }
        except Exception as e:
            logger.error(f"Error loading tasks: {e}")
            self.tasks = {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(
                {"tasks": list(self.tasks.values())}, f, ensure_ascii=False, indent=2
            )

    def _reset_incomplete_tasks(self) -> None:
        changed = False
        now = datetime.now().isoformat()
        for task in self.tasks.values():
            if task.get("status") in ("queued", "running", "paused"):
                task["status"] = "stopped"
                task["last_error"] = "restart"
                task["updated_at"] = now
                changed = True
        if changed:
            self._save()

    async def create_task(
        self, task_type: str, user_id: int, payload: Dict[str, Any]
    ) -> str:
        task_id = uuid.uuid4().hex
        now = datetime.now().isoformat()
        task = {
            "id": task_id,
            "type": task_type,
            "user_id": user_id,
            "created_at": now,
            "updated_at": now,
            "status": "queued",
            "payload": payload,
            "progress": {},
            "last_error": "",
        }
        async with self.lock:
            self.tasks[task_id] = task
            self._save()
        return task_id

    async def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        async with self.lock:
            task = self.tasks.get(task_id)
            return dict(task) if task else None

    async def list_tasks(self, user_id: int, is_admin: bool) -> List[Dict[str, Any]]:
        async with self.lock:
            tasks = list(self.tasks.values())
        if not is_admin:
            tasks = [t for t in tasks if t.get("user_id") == user_id]
        tasks.sort(key=lambda t: t.get("created_at", ""), reverse=True)
        return tasks

    async def set_status(
        self, task_id: str, status: str, last_error: Optional[str] = None
    ):
        async with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return False
            task["status"] = status
            task["updated_at"] = datetime.now().isoformat()
            if last_error is not None:
                task["last_error"] = last_error
            self._save()
        return True

    async def update_task(self, task_id: str, **updates):
        async with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return False
            task.update(updates)
            task["updated_at"] = datetime.now().isoformat()
            self._save()
        return True

    async def update_progress(
        self, task_id: str, progress: Dict[str, Any], force: bool = False
    ):
        now = time.time()
        async with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return
            current = task.get("progress") or {}
            current.update(progress)
            task["progress"] = current
            task["updated_at"] = datetime.now().isoformat()
            should_save = force or (now - self._last_save.get(task_id, 0) >= 2.0)
            if should_save:
                self._save()
                self._last_save[task_id] = now

    async def is_paused(self, task_id: str) -> bool:
        async with self.lock:
            task = self.tasks.get(task_id)
            return bool(task and task.get("status") == "paused")

    async def is_canceled(self, task_id: str) -> bool:
        async with self.lock:
            task = self.tasks.get(task_id)
            return bool(task and task.get("status") == "canceled")

    async def wait_if_paused(self, task_id: str):
        while True:
            async with self.lock:
                task = self.tasks.get(task_id)
                if not task or task.get("status") != "paused":
                    return
            await asyncio.sleep(1)

    async def count_by_status(self) -> Dict[str, int]:
        async with self.lock:
            tasks = list(self.tasks.values())
        counts: Dict[str, int] = {}
        for t in tasks:
            status = t.get("status", "unknown")
            counts[status] = counts.get(status, 0) + 1
        return counts


# --- AccountPoolManager ---
class AccountPoolManager:
    def __init__(self):
        self.accounts: List[Dict[str, Any]] = []
        self.lock = asyncio.Lock()
        self._load_accounts()
        logger.info(f"Loaded {len(self.accounts)} accounts")

    def _load_accounts(self):
        dirs: List[str] = []
        if Config.SESSIONS_DIR:
            dirs.append(Config.SESSIONS_DIR)
        legacy_dir = getattr(Config, "LEGACY_SESSIONS_DIR", None)
        if legacy_dir and legacy_dir not in dirs:
            dirs.append(legacy_dir)
        if not dirs:
            return

        seen: Set[str] = set()
        for sessions_dir in dirs:
            try:
                filenames = os.listdir(sessions_dir)
            except Exception as e:
                logger.error(f"Error listing sessions dir {sessions_dir}: {e}")
                continue
            for filename in filenames:
                if not filename.endswith(".session") or filename in seen:
                    continue
                session_path = os.path.join(sessions_dir, filename)
                try:
                    with open(session_path, "r", encoding="utf-8") as f:
                        session_string = f.read().strip()
                    if not session_string:
                        continue
                    self.accounts.append(
                        {
                            "session_file": filename,
                            "session_string": session_string,
                            "client": None,
                            "in_use": False,
                            "last_used": None,
                            "is_valid": True,
                            "flood_wait_until": None,
                        }
                    )
                    seen.add(filename)
                except Exception as e:
                    logger.error(f"Error loading session {filename}: {e}")

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
                self.accounts.sort(key=lambda x: x["last_used"] or datetime.min)
                for acc in self.accounts:
                    if not acc["in_use"] and acc["is_valid"]:
                        if (
                            acc.get("flood_wait_until")
                            and acc["flood_wait_until"] > now
                        ):
                            continue
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
                    logger.info(f"Released account: {account['session_file']}")

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
                    if acc.get("flood_wait_until") and acc["flood_wait_until"] > now:
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
        if not Config.SESSIONS_DIR:
            raise Exception("SESSIONS_DIR не задан.")
        os.makedirs(Config.SESSIONS_DIR, exist_ok=True)
        session_path = os.path.join(Config.SESSIONS_DIR, f"{session_name}.session")
        try:
            with open(session_path, "w", encoding="utf-8") as f:
                f.write(session_string)
        except OSError as e:
            raise Exception(
                "Не удалось сохранить сессию. "
                f"Проверьте доступ к '{Config.SESSIONS_DIR}' "
                "или задайте SESSIONS_DIR в .env на доступный путь "
                "(например, C:\\Temp\\inviter\\sessions). "
                f"Ошибка: {e}"
            )
        self.accounts.append(
            {
                "session_file": f"{session_name}.session",
                "session_string": session_string,
                "client": None,
                "in_use": False,
                "last_used": None,
                "is_valid": True,
                "flood_wait_until": None,
            }
        )
        logger.info(f"Added new account: {session_name}.session")


# --- TaskQueueManager ---
class TaskQueueManager:
    def __init__(self, task_manager: TaskManager, max_concurrent_tasks: int = 3):
        self.task_manager = task_manager
        self.max_concurrent_tasks = max_concurrent_tasks
        self.queue = asyncio.Queue()
        self.active_tasks = 0
        self.active_task_ids: Set[str] = set()
        self.queued_task_ids: Set[str] = set()
        self.handlers: Dict[str, Any] = {}
        self.logger = logging.getLogger("task_queue")

    def register_handler(self, task_type: str, handler):
        self.handlers[task_type] = handler

    def start_workers(self):
        for i in range(self.max_concurrent_tasks):
            asyncio.create_task(self._worker(f"worker-{i + 1}"))

    async def add_task(self, task_id: str):
        if task_id in self.queued_task_ids or task_id in self.active_task_ids:
            return
        self.queued_task_ids.add(task_id)
        await self.queue.put(task_id)
        self.logger.info(f"Task queued: {task_id}")

    def is_active(self, task_id: str) -> bool:
        return task_id in self.active_task_ids

    async def _worker(self, name: str):
        self.logger.info(f"Worker {name} started")
        while True:
            task_id = await self.queue.get()
            started = False
            try:
                self.queued_task_ids.discard(task_id)
                if task_id in self.active_task_ids:
                    continue
                task = await self.task_manager.get_task(task_id)
                if not task:
                    continue
                status = task.get("status")
                if status in ("canceled", "completed", "failed", "stopped"):
                    continue
                if status == "paused":
                    continue

                await self.task_manager.set_status(task_id, "running")
                self.active_tasks += 1
                self.active_task_ids.add(task_id)
                started = True

                handler = self.handlers.get(task.get("type"))
                if not handler:
                    await self.task_manager.set_status(
                        task_id, "failed", "Unknown task type"
                    )
                    continue

                result_status, error_msg = await handler(
                    task_id, task.get("payload", {}), task.get("user_id")
                )
                if result_status == "canceled":
                    await self.task_manager.set_status(task_id, "canceled")
                elif result_status == "failed":
                    await self.task_manager.set_status(
                        task_id, "failed", error_msg or "Task failed"
                    )
                else:
                    await self.task_manager.set_status(task_id, "completed")
            except Exception as e:
                self.logger.exception(f"Task error: {e}")
                try:
                    await self.task_manager.set_status(task_id, "failed", str(e))
                except Exception:
                    pass
            finally:
                if started:
                    self.active_tasks = max(0, self.active_tasks - 1)
                    self.active_task_ids.discard(task_id)
                self.queue.task_done()


# --- Инициализация ---
bot: Optional[Bot] = None
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)
dp.callback_query.middleware(CallbackAnswerMiddleware())

account_pool: Optional[AccountPoolManager] = None
task_queue: Optional[TaskQueueManager] = None
pending_auth: Dict[int, bool] = {}


# --- Инициализация менеджеров ---
auth_manager = AuthManager()
task_manager = TaskManager(Config.TASKS_FILE)


# --- Middleware ---
async def auth_middleware(handler, event, data):
    user_id = event.from_user.id
    if user_id in Config.ADMIN_USER_IDS or auth_manager.is_authorized(user_id):
        return await handler(event, data)

    if isinstance(event, Message) and event.text and event.text.startswith("/start"):
        return await handler(event, data)

    if isinstance(event, CallbackQuery) and event.data == "start":
        return await handler(event, data)

    if isinstance(event, Message) and user_id in pending_auth:
        return await handler(event, data)

    if isinstance(event, CallbackQuery):
        try:
            await event.answer(
                "Требуется авторизация. Нажмите /start и введите ключ.",
                show_alert=True,
            )
        except Exception:
            pass
        return None

    if isinstance(event, Message):
        if user_id not in pending_auth:
            pending_auth[user_id] = True
            await event.answer(
                "🔑 <b>Требуется авторизация!</b>\n"
                "Пожалуйста, введите ключ доступа, который вы получили от администратора:",
                reply_markup=cancel_keyboard(),
            )
        else:
            await event.answer(
                "⌛️ Ожидаю ввода ключа доступа...", reply_markup=cancel_keyboard()
            )
    return None


router.message.middleware(auth_middleware)
router.callback_query.middleware(auth_middleware)


# --- Обработчики ---
@router.message(Command("start"))
@router.callback_query(F.data == "start")
async def cmd_start(event, state: FSMContext):
    await state.clear()
    user_id = event.from_user.id

    if not (user_id in Config.ADMIN_USER_IDS or auth_manager.is_authorized(user_id)):
        pending_auth[user_id] = True
        text = (
            "🔒 <b>Требуется авторизация!</b>\n\n"
            "Для использования бота вам необходим ключ доступа.\n"
            "Пожалуйста, введите ключ, который вы получили от администратора:"
        )
        if isinstance(event, CallbackQuery):
            await smart_answer(
                event, text, reply_markup=cancel_keyboard(), delete_origin=True
            )
        else:
            await event.answer(text, reply_markup=cancel_keyboard())
        return

    is_admin = user_id in Config.ADMIN_USER_IDS
    if is_admin:
        text = (
            "👑 <b>Добро пожаловать, администратор!</b>\n\n"
            "Используйте кнопки ниже для управления ботом."
        )
    else:
        text = (
            "👋 <b>Добро пожаловать!</b>\n\n"
            "Используйте кнопки ниже для управления ботом."
        )

    if isinstance(event, CallbackQuery):
        await smart_answer(
            event, text, reply_markup=main_menu_keyboard(is_admin), delete_origin=True
        )
    else:
        await event.answer(text, reply_markup=main_menu_keyboard(is_admin))


@router.callback_query(F.data == "cancel")
async def cmd_cancel(event: CallbackQuery, state: FSMContext):
    user_id = event.from_user.id
    pending_auth.pop(user_id, None)
    await state.clear()
    await cmd_start(event, state)


@router.message(lambda message: message.from_user.id in pending_auth)
async def process_auth_key(message: Message, state: FSMContext):
    user_id = message.from_user.id
    key = message.text.strip()
    if await auth_manager.verify_key(user_id, key):
        pending_auth.pop(user_id, None)
        await message.answer(
            "✅ <b>Авторизация успешна!</b>\n\n"
            "Теперь вы можете использовать все функции бота."
        )
        await cmd_start(message, state)
    else:
        await notify_admins(
            f"⚠️ <b>Попытка несанкционированного доступа!</b>\n"
            f"• Пользователь: {user_id}\n"
            f"• Введенный ключ: {key}"
        )
        await message.answer(
            "❌ <b>Неверный ключ доступа!</b>\n\n"
            "Администраторы уведомлены. Попробуйте снова:"
        )


@router.callback_query(F.data == "add_account")
async def cmd_add_account(event: CallbackQuery, state: FSMContext):
    await smart_answer(
        event,
        "📱 <b>Шаг 1/3</b>\n"
        "Пожалуйста, отправьте ваш номер телефона в международном формате (например, +71234567890):",
        reply_markup=cancel_keyboard(),
        delete_origin=True,
    )
    await state.set_state(AddAccountStates.waiting_phone)


@router.message(AddAccountStates.waiting_phone)
async def process_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    await state.update_data(phone=phone)
    await message.answer(
        "⚠️ <b>Уведомление о безопасности</b>\n\n"
        "Добавляя ваш аккаунт:\n"
        "• Этот бот будет использовать ваш аккаунт Telegram\n"
        "• Ваши другие сессии НЕ будут завершены\n"
        "• Вы можете продолжать использовать Telegram как обычно\n\n"
        "Вы согласны продолжить?",
        reply_markup=kb(
            [
                [{"text": "Да", "callback_data": "add_confirm_yes"}],
                [{"text": "Нет", "callback_data": "add_confirm_no"}],
                [{"text": "Отмена", "callback_data": "cancel"}],
            ]
        ),
    )
    await state.set_state(AddAccountStates.waiting_confirmation)


@router.callback_query(
    AddAccountStates.waiting_confirmation, F.data == "add_confirm_no"
)
async def process_confirmation_no(event: CallbackQuery, state: FSMContext):
    await state.clear()
    await smart_answer(
        event,
        "❌ Добавление аккаунта отменено",
        reply_markup=back_to_menu_keyboard(),
        delete_origin=True,
    )


@router.callback_query(
    AddAccountStates.waiting_confirmation, F.data == "add_confirm_yes"
)
async def process_confirmation_yes(event: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    phone = data.get("phone")
    if not phone:
        await smart_answer(
            event,
            "❌ Отсутствует номер телефона. Начните заново.",
            reply_markup=back_to_menu_keyboard(),
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
        await smart_answer(
            event,
            "🔑 <b>Шаг 2/3</b>\n"
            f"Telegram отправил код на ваш телефон ({phone}).\n"
            "Пожалуйста, введите код в формате: <code>12345</code>",
            reply_markup=cancel_keyboard(),
            delete_origin=True,
        )
        await state.set_state(AddAccountStates.waiting_code)
    except (PhoneNumberInvalidError, FloodWaitError) as e:
        await smart_answer(
            event,
            f"❌ Ошибка: {str(e)}",
            reply_markup=back_to_menu_keyboard(),
            delete_origin=True,
        )
        await client.disconnect()
        await state.clear()
    except Exception as e:
        await smart_answer(
            event,
            f"❌ Непредвиденная ошибка: {str(e)}",
            reply_markup=back_to_menu_keyboard(),
            delete_origin=True,
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
        await message.answer(
            "❌ Состояние потеряно. Начните заново.",
            reply_markup=back_to_menu_keyboard(),
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
            await message.answer(
                f"✅ <b>Аккаунт успешно добавлен!</b>\n"
                f"• Имя: {me.first_name or ''} {me.last_name or ''}\n"
                f"• Имя пользователя: @{me.username}\n"
                f"• Телефон: {phone}\n\n"
                "⚠️ <b>Помните:</b> Ваши другие сессии останутся активными.",
                reply_markup=back_to_menu_keyboard(),
            )
            await persistent_client.disconnect()
            await state.clear()
        else:
            await message.answer(
                "🔒 <b>Шаг 3/3</b>\nПожалуйста, введите ваш пароль двухфакторной аутентификации:",
                reply_markup=cancel_keyboard(),
            )
            await state.update_data(password_attempts=0)
            await state.set_state(AddAccountStates.waiting_password)
    except SessionPasswordNeededError:
        await message.answer(
            "🔒 <b>Шаг 3/3</b>\nПожалуйста, введите ваш пароль двухфакторной аутентификации:",
            reply_markup=cancel_keyboard(),
        )
        await state.update_data(password_attempts=0)
        await state.set_state(AddAccountStates.waiting_password)
    except PhoneCodeExpiredError:
        try:
            sent_code = await client.send_code_request(phone)
            await state.update_data(phone_code_hash=sent_code.phone_code_hash)
            await message.answer(
                "⚠️ <b>Код устарел!</b>\nНовый код был отправлен на ваш телефон.\nПожалуйста, введите новый код:",
                reply_markup=cancel_keyboard(),
            )
        except Exception as e:
            await message.answer(
                f"❌ Ошибка при отправке нового кода: {str(e)}",
                reply_markup=back_to_menu_keyboard(),
            )
            await client.disconnect()
            await state.clear()
    except (PhoneCodeInvalidError, FloodWaitError) as e:
        await message.answer(
            f"❌ Ошибка: {str(e)}", reply_markup=back_to_menu_keyboard()
        )
        await client.disconnect()
        await state.clear()
    except Exception as e:
        await message.answer(
            f"❌ Непредвиденная ошибка: {str(e)}", reply_markup=back_to_menu_keyboard()
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
        await message.answer(
            "❌ Состояние потеряно. Начните заново.",
            reply_markup=back_to_menu_keyboard(),
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
            await message.answer(
                f"✅ <b>Аккаунт успешно добавлен!</b>\n"
                f"• Имя: {me.first_name or ''} {me.last_name or ''}\n"
                f"• Имя пользователя: @{me.username}\n"
                f"• Телефон: {phone}\n\n"
                "⚠️ <b>Помните:</b> Ваши другие сессии останутся активными.",
                reply_markup=back_to_menu_keyboard(),
            )
            await persistent_client.disconnect()
            await state.clear()
        else:
            await message.answer(
                "❌ Авторизация не удалась. Пожалуйста, попробуйте снова.",
                reply_markup=back_to_menu_keyboard(),
            )
            await client.disconnect()
            await state.clear()
    except SessionPasswordNeededError:
        attempts += 1
        if attempts >= 3:
            await message.answer(
                "❌ Превышено количество попыток ввода пароля. Добавление аккаунта отменено.",
                reply_markup=back_to_menu_keyboard(),
            )
            await client.disconnect()
            await state.clear()
        else:
            await state.update_data(password_attempts=attempts)
            await message.answer(
                f"❌ Неверный пароль. Осталось попыток: {3 - attempts}",
                reply_markup=cancel_keyboard(),
            )
    except Exception as e:
        await message.answer(
            f"❌ Ошибка: {str(e)}", reply_markup=back_to_menu_keyboard()
        )
        await client.disconnect()
        await state.clear()


@router.callback_query(F.data == "list_accounts")
async def cmd_list_accounts(event: CallbackQuery):
    if not account_pool or not account_pool.accounts:
        return await smart_answer(
            event,
            "ℹ️ Нет доступных аккаунтов.",
            reply_markup=back_to_menu_keyboard(),
            delete_origin=True,
        )
    text = "📋 <b>Доступные аккаунты:</b>\n\n"
    for i, acc in enumerate(account_pool.accounts, 1):
        status = "🟢 Свободен" if not acc["in_use"] else "🔴 Используется"
        validity = "🟢 Рабочий" if acc["is_valid"] else "🔴 Не рабочий"
        flood = (
            f"⏳ FloodWait до {acc['flood_wait_until']}"
            if acc.get("flood_wait_until")
            else ""
        )
        text += (
            f"{i}. <code>{acc['session_file']}</code>\n"
            f"   Статус: {status} | {validity} {flood}\n"
            f"   Последнее использование: {acc['last_used'] or 'Никогда'}\n\n"
        )
    await smart_answer(
        event, text, reply_markup=back_to_menu_keyboard(), delete_origin=True
    )


@router.callback_query(F.data == "genkey")
async def cmd_genkey(event: CallbackQuery, state: FSMContext):
    if event.from_user.id not in Config.ADMIN_USER_IDS:
        return await smart_answer(
            event,
            "🚫 Только администраторы могут генерировать ключи доступа!",
            reply_markup=back_to_menu_keyboard(),
            delete_origin=True,
        )
    await smart_answer(
        event,
        "🔑 <b>Генерация ключа доступа</b>\n\n"
        "Пожалуйста, введите ID пользователя, для которого нужно сгенерировать ключ:",
        reply_markup=cancel_keyboard(),
        delete_origin=True,
    )
    await state.set_state(KeyGeneration.waiting_user_id)


@router.message(KeyGeneration.waiting_user_id)
async def process_user_id(message: Message, state: FSMContext):
    try:
        user_id = int(message.text.strip())
        key = await auth_manager.generate_key(user_id)
        await message.answer(
            "✅ <b>Ключ успешно сгенерирован!</b>\n\n"
            f"• ID пользователя: <code>{user_id}</code>\n"
            f"• Ключ доступа: <code>{key}</code>\n\n"
            "Передайте этот ключ пользователю. После ввода ключа пользователь получит доступ к боту.",
            reply_markup=back_to_menu_keyboard(),
        )
        await state.clear()
    except ValueError:
        await message.answer(
            "❌ ID должен быть числом. Попробуйте снова:",
            reply_markup=cancel_keyboard(),
        )


@router.callback_query(F.data == "start_scraping")
async def cmd_start_scraping(event: CallbackQuery, state: FSMContext):
    if not account_pool or not account_pool.accounts:
        return await smart_answer(
            event,
            "❌ Нет доступных аккаунтов! Сначала добавьте аккаунты.",
            reply_markup=back_to_menu_keyboard(),
            delete_origin=True,
        )
    await smart_answer(
        event,
        "🔍 <b>Шаг 1/4</b>\n"
        "Отправьте @username или пригласительную ссылку чата/канала, из которого нужно собрать пользователей:",
        reply_markup=cancel_keyboard(),
        delete_origin=True,
    )
    await state.set_state(ScrapingStates.waiting_source)


@router.message(ScrapingStates.waiting_source)
async def process_source(message: Message, state: FSMContext):
    source = message.text.strip()
    await state.update_data(source=source)
    await message.answer(
        "🎯 <b>Шаг 2/4</b>\n"
        "Отправьте @username или пригласительную ссылку группы/канала, в которую нужно пригласить пользователей:",
        reply_markup=cancel_keyboard(),
    )
    await state.set_state(ScrapingStates.waiting_target)


@router.message(ScrapingStates.waiting_target)
async def process_target(message: Message, state: FSMContext):
    target = message.text.strip()
    await state.update_data(target=target)
    await message.answer(
        "⚙️ <b>Шаг 3/4</b>\nВыберите режим сбора:",
        reply_markup=kb(
            [
                [{"text": "По сообщениям", "callback_data": "scrape_mode_msg"}],
                [{"text": "По количеству", "callback_data": "scrape_mode_count"}],
                [{"text": "Отмена", "callback_data": "cancel"}],
            ]
        ),
    )
    await state.set_state(ScrapingStates.waiting_mode)


@router.callback_query(ScrapingStates.waiting_mode, F.data == "scrape_mode_msg")
async def process_mode_msg(event: CallbackQuery, state: FSMContext):
    await smart_answer(
        event,
        "📊 <b>Шаг 4/4</b>\nВведите количество сообщений для анализа (рекомендуется 1000-5000):",
        reply_markup=cancel_keyboard(),
        delete_origin=True,
    )
    await state.set_state(ScrapingStates.waiting_message_limit)


@router.callback_query(ScrapingStates.waiting_mode, F.data == "scrape_mode_count")
async def process_mode_count(event: CallbackQuery, state: FSMContext):
    await smart_answer(
        event,
        "📊 <b>Шаг 4/4</b>\nВведите количество пользователей для приглашения (от 10 до 1000):",
        reply_markup=cancel_keyboard(),
        delete_origin=True,
    )
    await state.set_state(ScrapingStates.waiting_user_count)


@router.message(ScrapingStates.waiting_message_limit)
async def process_limit(message: Message, state: FSMContext):
    try:
        limit = int(message.text)
        if limit < 50 or limit > 5000:
            raise ValueError
    except ValueError:
        return await message.answer(
            "⚠️ Неверное число! Пожалуйста, введите значение от 50 до 5000.",
            reply_markup=cancel_keyboard(),
        )
    data = await state.get_data()
    source = data["source"]
    target = data["target"]
    task_id = await task_manager.create_task(
        "scrape_messages",
        message.from_user.id,
        {"source": source, "target": target, "message_limit": limit},
    )
    await task_queue.add_task(task_id)
    await message.answer(
        f"✅ <b>Задача запущена!</b>\n\n"
        f"• Источник: {source}\n"
        f"• Цель: {target}\n"
        f"• Сообщений: {limit}\n"
        f"• ID задачи: <code>{task_id}</code>\n\n"
        "Вы получите отчет по завершении.",
        reply_markup=back_to_menu_keyboard(),
    )
    await state.clear()


@router.message(ScrapingStates.waiting_user_count)
async def process_user_count(message: Message, state: FSMContext):
    try:
        user_count = int(message.text)
        if user_count < 10 or user_count > 1000:
            raise ValueError
    except ValueError:
        return await message.answer(
            "⚠️ Неверное число! Введите от 10 до 1000.", reply_markup=cancel_keyboard()
        )
    data = await state.get_data()
    source = data["source"]
    target = data["target"]
    task_id = await task_manager.create_task(
        "scrape_count",
        message.from_user.id,
        {"source": source, "target": target, "user_count": user_count},
    )
    await task_queue.add_task(task_id)
    await message.answer(
        f"✅ <b>Задача запущена!</b>\n\n"
        f"• Источник: {source}\n"
        f"• Цель: {target}\n"
        f"• Пользователей: {user_count}\n"
        f"• ID задачи: <code>{task_id}</code>\n\n"
        "Вы получите отчет по завершении.",
        reply_markup=back_to_menu_keyboard(),
    )
    await state.clear()


@router.callback_query(F.data == "bulk_mailing")
async def cmd_bulk_mailing(event: CallbackQuery, state: FSMContext):
    if not account_pool or not account_pool.accounts:
        return await smart_answer(
            event,
            "❌ Нет доступных аккаунтов! Сначала добавьте аккаунты.",
            reply_markup=back_to_menu_keyboard(),
            delete_origin=True,
        )
    await smart_answer(
        event,
        "✉️ <b>Массовая рассылка</b>\n\n"
        "Шаг 1/4\n"
        "Отправьте список чатов/каналов, через пробел, запятую или с новой строки.\n"
        "Формат: @chat1 @chat2 https://t.me/xxxx",
        reply_markup=cancel_keyboard(),
        delete_origin=True,
    )
    await state.set_state(BulkMailStates.waiting_chats)


@router.message(BulkMailStates.waiting_chats)
async def bm_waiting_chats(message: Message, state: FSMContext):
    raw = message.text.strip()
    parts = [p.strip() for p in raw.replace(",", " ").split() if p.strip()]
    if not parts:
        return await message.answer(
            "⚠️ Список чатов пуст. Пожалуйста, отправьте корректный список.",
            reply_markup=cancel_keyboard(),
        )
    await state.update_data(chats=parts)
    await message.answer(
        "⏱️ Шаг 2/4\n"
        "Введите задержку между отправками в секундах в формате: <code>min max</code>\n"
        "Пример: <code>10 20</code>",
        reply_markup=cancel_keyboard(),
    )
    await state.set_state(BulkMailStates.waiting_delay)


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
        return await message.answer(
            "⚠️ Неверный формат. Введите две неотрицательные цифры: min max (min <= max).",
            reply_markup=cancel_keyboard(),
        )
    await state.update_data(delay_min=dmin, delay_max=dmax)
    await message.answer(
        "📝 Шаг 3/4\n" "Отправьте текст сообщения, которое нужно разослать.",
        reply_markup=cancel_keyboard(),
    )
    await state.set_state(BulkMailStates.waiting_text)


@router.message(BulkMailStates.waiting_text)
async def bm_waiting_text(message: Message, state: FSMContext):
    text = message.text
    if not text or not text.strip():
        return await message.answer(
            "⚠️ Сообщение не может быть пустым. Введите текст сообщения.",
            reply_markup=cancel_keyboard(),
        )
    await state.update_data(message_text=text)
    accounts_list = account_pool.accounts if account_pool else []
    if not accounts_list:
        await message.answer(
            "❌ Нет доступных аккаунтов. Сначала добавьте аккаунты.",
            reply_markup=back_to_menu_keyboard(),
        )
        await state.clear()
        return
    keyboard = [[{"text": "Авто", "callback_data": "bm_sender:auto"}]]
    session_files = []
    for i, acc in enumerate(accounts_list, 1):
        status = "🔴" if acc["in_use"] else "🟢" if acc["is_valid"] else "⚫"
        flood = " ⏳" if acc.get("flood_wait_until") else ""
        keyboard.append(
            [
                {
                    "text": f"{i}. {acc['session_file']} {status}{flood}",
                    "callback_data": f"bm_sender:{i}",
                }
            ]
        )
        session_files.append(acc["session_file"])
    keyboard.append([{"text": "Отмена", "callback_data": "cancel"}])
    await state.update_data(sender_sessions=session_files)
    await message.answer(
        "🔢 Шаг 4/4\n" "Выберите аккаунт-отправитель или авто режим:",
        reply_markup=kb(keyboard),
    )
    await state.set_state(BulkMailStates.waiting_sender)


@router.callback_query(BulkMailStates.waiting_sender, F.data.startswith("bm_sender:"))
async def bm_waiting_sender(event: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    sessions = data.get("sender_sessions", [])
    value = event.data.split(":", 1)[1]
    sender_session = None
    if value == "auto":
        sender_session = None
    else:
        try:
            idx = int(value)
            if idx < 1 or idx > len(sessions):
                raise ValueError
            sender_session = sessions[idx - 1]
        except ValueError:
            return await smart_answer(
                event,
                "⚠️ Неверный выбор.",
                reply_markup=cancel_keyboard(),
                delete_origin=True,
            )

    await state.update_data(sender_session=sender_session)
    sender_info = (
        "авто (пул аккаунтов)" if sender_session is None else f"{sender_session}"
    )
    await smart_answer(
        event,
        f"Выбран отправитель: <code>{sender_info}</code>\n\nТеперь введите общее количество отправок (целое число, например 100).",
        reply_markup=cancel_keyboard(),
        delete_origin=True,
    )
    await state.set_state(BulkMailStates.waiting_count)


@router.message(BulkMailStates.waiting_count)
async def bm_waiting_count(message: Message, state: FSMContext):
    try:
        total = int(message.text.strip())
        if total <= 0:
            raise ValueError
    except ValueError:
        return await message.answer(
            "⚠️ Неверное число. Введите положительное целое количество отправок.",
            reply_markup=cancel_keyboard(),
        )

    data = await state.get_data()
    chats = data.get("chats", [])
    delay_min = data.get("delay_min", 1)
    delay_max = data.get("delay_max", 1)
    message_text = data.get("message_text", "")
    sender_session = data.get("sender_session", None)

    task_id = await task_manager.create_task(
        "bulk_mailing",
        message.from_user.id,
        {
            "chats": chats,
            "delay_min": delay_min,
            "delay_max": delay_max,
            "message_text": message_text,
            "total_sends": total,
            "sender_session_file": sender_session,
        },
    )
    await task_queue.add_task(task_id)

    safe_preview = html.escape(
        (message_text[:200] + "...") if len(message_text) > 200 else message_text
    )
    sender_info = (
        f"• Отправитель: {sender_session}"
        if sender_session
        else "• Отправитель: авто (пул аккаунтов)"
    )
    await message.answer(
        f"✅ <b>Задача массовой рассылки запущена!</b>\n\n"
        f"• Чатов: {len(chats)}\n"
        f"• Задержка: {delay_min}-{delay_max} сек\n"
        f"{sender_info}\n"
        f"• Текст сообщения: (показаны первые 200 символов)\n\n"
        f"{safe_preview}\n\n"
        f"• Всего отправок: {total}\n"
        f"• ID задачи: <code>{task_id}</code>\n\n"
        "Вы получите отчет по завершении.",
        reply_markup=back_to_menu_keyboard(),
    )
    await state.clear()


@router.callback_query(F.data == "task_stats")
async def cmd_task_stats(event: CallbackQuery):
    counts = await task_manager.count_by_status()
    available_accounts = 0
    total_accounts = len(account_pool.accounts) if account_pool else 0
    if account_pool:
        available_accounts = len(
            [a for a in account_pool.accounts if not a["in_use"] and a["is_valid"]]
        )
    stats_lines = [
        "📊 <b>Статистика задач</b>",
        f"• Активные задачи: {task_queue.active_tasks if task_queue else 0}",
        f"• Задачи в очереди: {task_queue.queue.qsize() if task_queue else 0}",
        f"• Доступные аккаунты: {available_accounts}/{total_accounts}",
    ]
    if counts:
        stats_lines.append("\n<b>Статусы:</b>")
        for status, count in counts.items():
            stats_lines.append(f"• {status}: {count}")
    await smart_answer(
        event,
        "\n".join(stats_lines),
        reply_markup=back_to_menu_keyboard(),
        delete_origin=True,
    )


def format_task_card(task: Dict[str, Any], is_admin: bool) -> str:
    type_map = {
        "scrape_messages": "Сбор по сообщениям",
        "scrape_count": "Сбор по количеству",
        "bulk_mailing": "Массовая рассылка",
    }
    status_map = {
        "queued": "В очереди",
        "running": "Выполняется",
        "paused": "Пауза",
        "completed": "Завершена",
        "failed": "Ошибка",
        "canceled": "Отменена",
        "stopped": "Остановлена",
    }
    t_type = type_map.get(task.get("type"), task.get("type", "unknown"))
    status = status_map.get(task.get("status"), task.get("status", "unknown"))
    created = task.get("created_at", "")
    progress = task.get("progress") or {}
    progress_line = ""
    if "done" in progress and "total" in progress:
        done = progress.get("done", 0)
        total = progress.get("total", 0)
        if total:
            pct = int((done / total) * 100)
            progress_line = f"Прогресс: {done}/{total} ({pct}%)"
        else:
            progress_line = f"Прогресс: {done}"
    elif progress:
        progress_line = f"Прогресс: {progress}"
    lines = [
        "🧩 <b>Задача</b>",
        f"• ID: <code>{task.get('id')}</code>",
        f"• Тип: {t_type}",
        f"• Статус: {status}",
    ]
    if is_admin:
        lines.append(f"• Пользователь: <code>{task.get('user_id')}</code>")
    if created:
        lines.append(f"• Создана: {created}")
    if progress_line:
        lines.append(f"• {progress_line}")
    if task.get("last_error"):
        lines.append(f"• Ошибка: {task.get('last_error')}")
    return "\n".join(lines)


def task_action_keyboard(task: Dict[str, Any]) -> InlineKeyboardMarkup:
    status = task.get("status")
    rows = []
    if status in ("queued", "running"):
        rows.append(
            [
                {"text": "Пауза", "callback_data": f"task_pause:{task.get('id')}"},
                {"text": "Отмена", "callback_data": f"task_cancel:{task.get('id')}"},
            ]
        )
    elif status == "paused":
        rows.append(
            [
                {
                    "text": "Продолжить",
                    "callback_data": f"task_resume:{task.get('id')}",
                },
                {"text": "Отмена", "callback_data": f"task_cancel:{task.get('id')}"},
            ]
        )
    rows.append([{"text": "Главная", "callback_data": "start"}])
    return kb(rows)


@router.callback_query(F.data == "task_list")
async def cmd_task_list(event: CallbackQuery):
    user_id = event.from_user.id
    is_admin = user_id in Config.ADMIN_USER_IDS
    tasks = await task_manager.list_tasks(user_id=user_id, is_admin=is_admin)
    if not tasks:
        return await smart_answer(
            event,
            "📭 Список задач пуст.",
            reply_markup=back_to_menu_keyboard(),
            delete_origin=True,
        )

    await smart_answer(
        event, f"🗂 Найдено задач: {len(tasks)}", reply_markup=None, delete_origin=True
    )
    for task in tasks:
        text = format_task_card(task, is_admin=is_admin)
        await event.message.answer(text, reply_markup=task_action_keyboard(task))


@router.callback_query(F.data.startswith("task_pause:"))
async def cmd_task_pause(event: CallbackQuery):
    task_id = event.data.split(":", 1)[1]
    task = await task_manager.get_task(task_id)
    if not task:
        return await smart_answer(
            event,
            "⚠️ Задача не найдена.",
            reply_markup=back_to_menu_keyboard(),
            delete_origin=True,
        )
    user_id = event.from_user.id
    if user_id not in Config.ADMIN_USER_IDS and task.get("user_id") != user_id:
        return await smart_answer(
            event,
            "🚫 Нет доступа к этой задаче.",
            reply_markup=back_to_menu_keyboard(),
            delete_origin=True,
        )
    await task_manager.set_status(task_id, "paused")
    task = await task_manager.get_task(task_id)
    await smart_answer(
        event,
        format_task_card(task, is_admin=user_id in Config.ADMIN_USER_IDS),
        reply_markup=task_action_keyboard(task),
        delete_origin=True,
    )


@router.callback_query(F.data.startswith("task_resume:"))
async def cmd_task_resume(event: CallbackQuery):
    task_id = event.data.split(":", 1)[1]
    task = await task_manager.get_task(task_id)
    if not task:
        return await smart_answer(
            event,
            "⚠️ Задача не найдена.",
            reply_markup=back_to_menu_keyboard(),
            delete_origin=True,
        )
    user_id = event.from_user.id
    if user_id not in Config.ADMIN_USER_IDS and task.get("user_id") != user_id:
        return await smart_answer(
            event,
            "🚫 Нет доступа к этой задаче.",
            reply_markup=back_to_menu_keyboard(),
            delete_origin=True,
        )
    if task_queue and task_queue.is_active(task_id):
        await task_manager.set_status(task_id, "running")
    else:
        await task_manager.set_status(task_id, "queued")
        await task_queue.add_task(task_id)
    task = await task_manager.get_task(task_id)
    await smart_answer(
        event,
        format_task_card(task, is_admin=user_id in Config.ADMIN_USER_IDS),
        reply_markup=task_action_keyboard(task),
        delete_origin=True,
    )


@router.callback_query(F.data.startswith("task_cancel:"))
async def cmd_task_cancel(event: CallbackQuery):
    task_id = event.data.split(":", 1)[1]
    task = await task_manager.get_task(task_id)
    if not task:
        return await smart_answer(
            event,
            "⚠️ Задача не найдена.",
            reply_markup=back_to_menu_keyboard(),
            delete_origin=True,
        )
    user_id = event.from_user.id
    if user_id not in Config.ADMIN_USER_IDS and task.get("user_id") != user_id:
        return await smart_answer(
            event,
            "🚫 Нет доступа к этой задаче.",
            reply_markup=back_to_menu_keyboard(),
            delete_origin=True,
        )
    await task_manager.set_status(task_id, "canceled")
    task = await task_manager.get_task(task_id)
    await smart_answer(
        event,
        format_task_card(task, is_admin=user_id in Config.ADMIN_USER_IDS),
        reply_markup=task_action_keyboard(task),
        delete_origin=True,
    )


@router.callback_query(F.data == "help")
async def cmd_help(event: CallbackQuery):
    text = (
        "📚 <b>Руководство по использованию бота</b>\n\n"
        "1. <b>Добавление аккаунтов</b> - используйте кнопку 'Добавить аккаунт'\n"
        "2. <b>Начать сбор</b> - запуск сбора пользователей\n"
        "3. <b>Массовая рассылка</b> - отправка сообщений в чаты\n\n"
        "⚙️ <b>Как это работает:</b>\n"
        "- Бот анализирует сообщения в исходном чате\n"
        "- Собирает активных пользователей\n"
        "- Приглашает их в вашу целевую группу\n\n"
        "⚠️ <b>Безопасность:</b>\n"
        "- Ваши другие сессии Telegram НЕ будут завершены\n"
        "- Сессии надежно хранятся и никогда не передаются третьим лицам"
    )
    await smart_answer(
        event, text, reply_markup=back_to_menu_keyboard(), delete_origin=True
    )


@router.callback_query(F.data == "ref")
async def cmd_ref(event: CallbackQuery):
    text = (
        "💸 <b>Зарабатывай с рефералкой:</b>\n\n"
        "👥 <b>1 человек</b> = +200₽\n"
        "👥 <b>3 человека</b> = +700₽\n"
        "👥 <b>5 человек</b> = +1500₽\n"
        "👥 <b>10 человек</b> = +4000₽\n\n"
        "Чтобы реферал считался приведённым вами он должен при регистрации сообщить ваш юзернейм."
    )
    await smart_answer(
        event, text, reply_markup=back_to_menu_keyboard(), delete_origin=True
    )


# --- Задачи ---
invited_cache: Set[int] = set()


async def cooperative_sleep(task_id: str, seconds: int) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        await task_manager.wait_if_paused(task_id)
        if await task_manager.is_canceled(task_id):
            return False
        remaining = max(0.0, end - time.time())
        await asyncio.sleep(min(1.0, remaining))
    return True


async def get_active_users(
    client: TelegramClient, source_entity: str, limit: int, task_id: str
) -> List[int]:
    logger.info(f"Collecting users from: {source_entity}")
    users = set()
    try:
        entity = await client.get_entity(source_entity)
        count = 0
        async for message in client.iter_messages(entity, limit=limit):
            await task_manager.wait_if_paused(task_id)
            if await task_manager.is_canceled(task_id):
                return []
            count += 1
            if message and message.sender_id:
                try:
                    sender = await message.get_sender()
                    if isinstance(sender, telethon_types.User) and not sender.bot:
                        users.add(sender.id)
                except Exception:
                    users.add(message.sender_id)
            if count % 200 == 0:
                await task_manager.update_progress(
                    task_id, {"phase": "collecting", "done": count, "total": limit}
                )
        logger.info(f"Found {len(users)} active users")
        return list(users)
    except AuthKeyUnregisteredError as e:
        logger.error(f"AuthKeyUnregisteredError: {e}")
        raise
    except Exception as e:
        logger.error(f"Error collecting users: {str(e)}")
        raise


async def invite_users(
    account,
    client: TelegramClient,
    user_ids: List[int],
    target_entity: str,
    task_id: str,
) -> Dict[str, Any]:
    logger.info(f"Inviting {len(user_ids)} users to {target_entity}")
    results = {
        "success": 0,
        "failed": 0,
        "privacy_errors": 0,
        "already_members": 0,
        "canceled": False,
    }
    try:
        target = await client.get_entity(target_entity)
        if "participants_cache" not in account:
            current_participants = set()
            async for user in client.iter_participants(target):
                current_participants.add(user.id)
            account["participants_cache"] = current_participants
        else:
            current_participants = account["participants_cache"]

        semaphore = asyncio.Semaphore(3)

        async def invite_one(user_id):
            async with semaphore:
                await task_manager.wait_if_paused(task_id)
                if await task_manager.is_canceled(task_id):
                    return
                if user_id in invited_cache or user_id in current_participants:
                    results["already_members"] += 1
                    return
                try:
                    user_entity = await client.get_entity(user_id)
                    if isinstance(user_entity, telethon_types.User) and user_entity.bot:
                        return
                    await client(
                        functions.channels.InviteToChannelRequest(
                            channel=target,
                            users=[user_entity],
                        )
                    )
                    results["success"] += 1
                    current_participants.add(user_id)
                    invited_cache.add(user_id)
                    await task_manager.update_progress(
                        task_id,
                        {
                            "phase": "inviting",
                            "done": results["success"],
                            "total": len(user_ids),
                        },
                    )
                    await cooperative_sleep(task_id, random.randint(60, 90))
                except UserPrivacyRestrictedError:
                    results["privacy_errors"] += 1
                except (ChatAdminRequiredError, ChannelPrivateError):
                    results["failed"] += 1
                except (FloodWaitError, FloodError) as e:
                    account["flood_wait_until"] = datetime.now() + timedelta(
                        seconds=e.seconds + 10
                    )
                    logger.warning(f"Flood error, waiting: {e.seconds} seconds")
                    await cooperative_sleep(task_id, e.seconds + 10)
                except UserNotParticipantError:
                    results["failed"] += 1
                except AuthKeyUnregisteredError:
                    account["is_valid"] = False
                except Exception as e:
                    results["failed"] += 1
                    logger.error(f"Invite error for {user_id}: {str(e)}")

        await asyncio.gather(*(invite_one(uid) for uid in user_ids))

        if await task_manager.is_canceled(task_id):
            results["canceled"] = True
        return results
    except Exception as e:
        logger.error(f"Error inviting users: {str(e)}")
        raise


async def scrape_and_invite_task(task_id: str, payload: Dict[str, Any], user_id: int):
    source = payload.get("source")
    target = payload.get("target")
    message_limit = int(payload.get("message_limit", 0))
    logger.info(f"Starting task: {source} -> {target} (limit: {message_limit})")
    await task_manager.update_progress(
        task_id, {"phase": "collecting", "done": 0, "total": message_limit}, force=True
    )
    try:
        async with account_pool.acquire_account() as account:
            client = account["client"]
            try:
                active_users = await get_active_users(
                    client, source, message_limit, task_id
                )
            except AuthKeyUnregisteredError:
                account["is_valid"] = False
                logger.error(f"Session invalid: {account['session_file']}")
                raise Exception(
                    "Сессия недействительна - пожалуйста, добавьте этот аккаунт заново"
                )

            if await task_manager.is_canceled(task_id):
                await notify_user(user_id, "⏹ Задача отменена пользователем.")
                return "canceled", None

            if not active_users:
                await notify_user(
                    user_id,
                    f"❌ Не найдено активных пользователей в {source}\n"
                    "Пожалуйста, проверьте, есть ли у аккаунта доступ к чату.",
                )
                return "failed", "no_users"

            results = await invite_users(account, client, active_users, target, task_id)
            if results.get("canceled"):
                await notify_user(user_id, "⏹ Задача отменена пользователем.")
                return "canceled", None

            report = (
                f"📊 <b>Задача завершена!</b>\n\n"
                f"• Источник: {source}\n"
                f"• Цель: {target}\n\n"
                f"• Проанализировано сообщений: {message_limit}\n"
                f"• Найдено активных пользователей: {len(active_users)}\n"
                f"• Успешных приглашений: {results['success']}\n"
                f"• Уже участников: {results.get('already_members', 0)}\n"
                f"• Неудачных приглашений: {results['failed']}\n"
                f"• Ограничения приватности: {results['privacy_errors']}\n"
                f"• Использованный аккаунт: {account['session_file']}"
            )
            await notify_user(user_id, report)
            return "completed", None
    except Exception as e:
        error_msg = (
            f"🔥 <b>Задача не выполнена!</b>\n\n"
            f"• Источник: {source}\n"
            f"• Цель: {target}\n\n"
            f"Ошибка: {str(e)}"
        )
        logger.exception(f"Task error: {source} -> {target}")
        await notify_user(user_id, error_msg)
        return "failed", str(e)


async def scrape_and_invite_by_user_count_task(
    task_id: str, payload: Dict[str, Any], user_id: int
):
    source = payload.get("source")
    target = payload.get("target")
    user_count = int(payload.get("user_count", 0))
    logger.info(f"Starting user count task: {source} -> {target} (users: {user_count})")
    await task_manager.update_progress(
        task_id, {"phase": "collecting", "done": 0, "total": user_count}, force=True
    )
    try:
        async with account_pool.acquire_account() as account:
            client = account["client"]
            users = set()
            entity = await client.get_entity(source)
            count = 0
            async for message in client.iter_messages(entity, limit=10000):
                await task_manager.wait_if_paused(task_id)
                if await task_manager.is_canceled(task_id):
                    await notify_user(user_id, "⏹ Задача отменена пользователем.")
                    return "canceled", None
                if message and message.sender_id:
                    try:
                        sender = await message.get_sender()
                        if isinstance(sender, telethon_types.User) and not sender.bot:
                            users.add(sender.id)
                    except Exception:
                        users.add(message.sender_id)
                count += 1
                if len(users) >= user_count:
                    break
                if count % 200 == 0:
                    await task_manager.update_progress(
                        task_id,
                        {
                            "phase": "collecting",
                            "done": len(users),
                            "total": user_count,
                        },
                    )
            active_users = list(users)[:user_count]
            if not active_users:
                await notify_user(
                    user_id,
                    f"❌ Не найдено активных пользователей в {source}\n"
                    "Проверьте доступ к чату.",
                )
                return "failed", "no_users"
            results = await invite_users(account, client, active_users, target, task_id)
            if results.get("canceled"):
                await notify_user(user_id, "⏹ Задача отменена пользователем.")
                return "canceled", None
            report = (
                f"📊 <b>Задача завершена!</b>\n\n"
                f"• Источник: {source}\n"
                f"• Цель: {target}\n"
                f"• Приглашено пользователей: {len(active_users)}\n"
                f"• Успешных приглашений: {results['success']}\n"
                f"• Уже участников: {results.get('already_members', 0)}\n"
                f"• Неудачных приглашений: {results['failed']}\n"
                f"• Ограничения приватности: {results['privacy_errors']}\n"
                f"• Использованный аккаунт: {account['session_file']}"
            )
            await notify_user(user_id, report)
            return "completed", None
    except Exception as e:
        error_msg = (
            f"🔥 <b>Задача не выполнена!</b>\n\n"
            f"• Источник: {source}\n"
            f"• Цель: {target}\n\n"
            f"Ошибка: {str(e)}"
        )
        logger.exception(f"Task error: {source} -> {target}")
        await notify_user(user_id, error_msg)
        return "failed", str(e)


async def bulk_mailing_task(task_id: str, payload: Dict[str, Any], user_id: int):
    chats: List[str] = payload.get("chats", [])
    delay_min = int(payload.get("delay_min", 1))
    delay_max = int(payload.get("delay_max", 1))
    message_text = payload.get("message_text", "")
    total_sends = int(payload.get("total_sends", 0))
    sender_session_file = payload.get("sender_session_file")

    logger.info(
        f"Starting bulk mailing: chats={len(chats)} total_sends={total_sends} delay={delay_min}-{delay_max} sender={sender_session_file or 'auto'}"
    )
    sent = 0
    per_chat_sent = {c: 0 for c in chats}
    idx = 0
    await task_manager.update_progress(
        task_id, {"phase": "sending", "done": 0, "total": total_sends}, force=True
    )
    try:
        if sender_session_file:
            try:
                async with account_pool.acquire_specific_account(
                    sender_session_file
                ) as account:
                    client = account["client"]
                    while sent < total_sends:
                        await task_manager.wait_if_paused(task_id)
                        if await task_manager.is_canceled(task_id):
                            await notify_user(
                                user_id, "⏹ Задача отменена пользователем."
                            )
                            return "canceled", None
                        chat = chats[idx % len(chats)]
                        try:
                            target = await client.get_entity(chat)
                            await client.send_message(target, message_text)
                            sent += 1
                            per_chat_sent[chat] = per_chat_sent.get(chat, 0) + 1
                            idx += 1
                            await task_manager.update_progress(
                                task_id, {"done": sent, "total": total_sends}
                            )
                            if not await cooperative_sleep(
                                task_id, random.randint(delay_min, delay_max)
                            ):
                                await notify_user(
                                    user_id, "⏹ Задача отменена пользователем."
                                )
                                return "canceled", None
                        except (FloodWaitError, FloodError) as e:
                            wait_seconds = getattr(e, "seconds", None) or 60
                            account["flood_wait_until"] = datetime.now() + timedelta(
                                seconds=wait_seconds + 10
                            )
                            logger.warning(
                                f"Flood on {account['session_file']}, wait {wait_seconds}s"
                            )
                            if not await cooperative_sleep(task_id, wait_seconds + 10):
                                await notify_user(
                                    user_id, "⏹ Задача отменена пользователем."
                                )
                                return "canceled", None
                        except AuthKeyUnregisteredError:
                            account["is_valid"] = False
                            logger.error(f"Session invalid: {account['session_file']}")
                            raise
                        except Exception as e:
                            logger.error(
                                f"Error sending to {chat} with specific account: {e}"
                            )
                            await cooperative_sleep(task_id, 1)
            except Exception as e:
                logger.error(
                    f"Error with specific account: {e}, switching to auto mode"
                )
                try:
                    await notify_user(
                        user_id,
                        f"⚠️ Ошибка с указанным аккаунтом: {e}\nПереключаюсь на автоматический режим для оставшихся отправок.",
                    )
                except Exception:
                    pass

        while sent < total_sends:
            async with account_pool.acquire_account() as account:
                client = account["client"]
                while sent < total_sends:
                    await task_manager.wait_if_paused(task_id)
                    if await task_manager.is_canceled(task_id):
                        await notify_user(user_id, "⏹ Задача отменена пользователем.")
                        return "canceled", None
                    chat = chats[idx % len(chats)]
                    try:
                        target = await client.get_entity(chat)
                        await client.send_message(target, message_text)
                        sent += 1
                        per_chat_sent[chat] = per_chat_sent.get(chat, 0) + 1
                        idx += 1
                        await task_manager.update_progress(
                            task_id, {"done": sent, "total": total_sends}
                        )
                        if not await cooperative_sleep(
                            task_id, random.randint(delay_min, delay_max)
                        ):
                            await notify_user(
                                user_id, "⏹ Задача отменена пользователем."
                            )
                            return "canceled", None
                    except (FloodWaitError, FloodError) as e:
                        wait_seconds = getattr(e, "seconds", None) or 60
                        account["flood_wait_until"] = datetime.now() + timedelta(
                            seconds=wait_seconds + 10
                        )
                        logger.warning(
                            f"Flood on {account['session_file']}, wait {wait_seconds}s"
                        )
                        break
                    except AuthKeyUnregisteredError:
                        account["is_valid"] = False
                        logger.error(f"Session invalid: {account['session_file']}")
                        break
                    except Exception as e:
                        logger.error(f"Error sending to {chat}: {e}")
                        await cooperative_sleep(task_id, 1)
                await cooperative_sleep(task_id, 1)

        report_lines = [
            "📬 <b>Массовая рассылка завершена!</b>",
            f"• Всего отправлено: {sent}",
            "• Отправлено по чатам:",
        ]
        for c, cnt in per_chat_sent.items():
            report_lines.append(f"  - {c}: {cnt}")
        await notify_user(user_id, "\n".join(report_lines))
        return "completed", None
    except Exception as e:
        logger.exception("Bulk mailing task failed")
        await notify_user(
            user_id, f"🔥 <b>Задача рассылки не выполнена!</b>\n\nОшибка: {e}"
        )
        return "failed", str(e)


# --- Запуск ---
async def main():
    logger.info(f"Admin IDs: {Config.ADMIN_USER_IDS}")
    logger.info("Запуск бота для инвайтинга трафика...")

    try:
        Config.validate()
    except RuntimeError as e:
        logger.critical(f"Config error: {e}")
        sys.exit(1)

    global bot, account_pool, task_queue
    bot = Bot(
        token=Config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )

    account_pool = AccountPoolManager()
    task_queue = TaskQueueManager(
        task_manager, max_concurrent_tasks=Config.MAX_CONCURRENT_TASKS
    )
    task_queue.register_handler("scrape_messages", scrape_and_invite_task)
    task_queue.register_handler("scrape_count", scrape_and_invite_by_user_count_task)
    task_queue.register_handler("bulk_mailing", bulk_mailing_task)
    task_queue.start_workers()

    if not account_pool.accounts:
        logger.warning("No accounts available! Users won't be able to start tasks")

    try:
        for admin_id in Config.ADMIN_USER_IDS:
            await safe_send_message(
                bot,
                admin_id,
                "🟢 <b>Бот успешно запущен!</b>\n"
                f"• Загружено аккаунтов: {len(account_pool.accounts)}\n"
                f"• Максимум одновременных задач: {Config.MAX_CONCURRENT_TASKS}\n\n"
                "⚠️ <b>Важно:</b> Ваши аккаунты теперь контролируются ботом. "
                "Ваши другие сессии Telegram останутся активными.",
            )
        await dp.start_polling(bot)
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Остановка бота по запросу пользователя (IDE/KeyboardInterrupt)")
    finally:
        try:
            for admin_id in Config.ADMIN_USER_IDS:
                await safe_send_message(
                    bot,
                    admin_id,
                    "🔴 <b>Бот остановлен!</b>\n\n"
                    "Все сессии Telegram были освобождены. "
                    "Теперь вы можете использовать свои аккаунты как обычно.",
                )
        except Exception:
            pass

        if bot:
            await bot.session.close()
        logger.info("Releasing all accounts...")
        if account_pool:
            for account in account_pool.accounts:
                if account.get("client"):
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
        logger.info("All accounts released. Bot shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
