import asyncio
import logging
import os
import json
import secrets
import sys
import random
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Set
from contextlib import asynccontextmanager
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from aiogram.enums import ParseMode
from telethon import TelegramClient, functions, types as telethon_types
from telethon.errors import (
    SessionPasswordNeededError, PhoneNumberInvalidError,
    FloodWaitError, PhoneCodeInvalidError, UserPrivacyRestrictedError,
    ChatAdminRequiredError, PhoneCodeExpiredError, ChannelPrivateError,
    UserNotParticipantError, AuthKeyUnregisteredError, FloodError,
)
from telethon.sessions import StringSession

# --- Логирование ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# --- Конфиг ---
class Config:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "YOU_BOT_TOKEN")
    API_ID: int = int(os.getenv("API_ID", 123456))
    API_HASH: str = os.getenv("API_HASH", "YOU_API_HASH")
    ADMIN_USER_IDS: List[int] = [int(x.strip()) for x in os.getenv("ADMIN_USER_IDS", "5225159977").split(",")]
    MAX_CONCURRENT_TASKS: int = int(os.getenv("MAX_CONCURRENT_TASKS", 10))
    SESSIONS_DIR: str = os.getenv("SESSIONS_DIR", "sessions")
    DATA_DIR: str = os.getenv("DATA_DIR", "data")
    AUTH_FILE: str = os.getenv("AUTH_FILE", os.path.join(DATA_DIR, "auth.json"))

os.makedirs(Config.SESSIONS_DIR, exist_ok=True)
os.makedirs(Config.DATA_DIR, exist_ok=True)

# --- Утилиты ---
async def safe_send_message(bot: Bot, user_id: int, message: str):
    try:
        await bot.send_message(user_id, message, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения {user_id}: {str(e)}")

async def notify_admins(message: str):
    for admin_id in Config.ADMIN_USER_IDS:
        await safe_send_message(bot, admin_id, message)

async def notify_user(user_id: int, message: str):
    await safe_send_message(bot, user_id, message)

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
        catch_up=False
    )

# --- FSM States ---
class AddAccountStates(StatesGroup):
    waiting_phone = State()
    waiting_confirmation = State()
    waiting_code = State()
    waiting_password = State()
    waiting_password_retry = State()

class ScrapingStates(StatesGroup):
    waiting_source = State()
    waiting_target = State()
    waiting_message_limit = State()

class KeyGeneration(StatesGroup):
    waiting_user_id = State()

# --- AuthManager ---
class AuthManager:
    def __init__(self):
        self.auth_data = self._load_auth_data()
        self.authorized_users = set(self.auth_data.get("authorized", []))
        logger.info(f"Loaded auth data: {len(self.authorized_users)} authorized users, {len(self.auth_data.get('keys', {}))} keys")

    def _load_auth_data(self) -> dict:
        try:
            if os.path.exists(Config.AUTH_FILE):
                with open(Config.AUTH_FILE, 'r') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"Error loading auth data: {e}")
        return {"keys": {}, "authorized": []}

    def save_auth_data(self):
        try:
            with open(Config.AUTH_FILE, 'w') as f:
                json.dump(
                    {
                        "keys": self.auth_data.get("keys", {}),
                        "authorized": list(self.authorized_users)
                    }, f
                )
        except Exception as e:
            logger.error(f"Error saving auth data: {e}")

    def generate_key(self, user_id: int) -> str:
        key = secrets.token_urlsafe(32)
        self.auth_data.setdefault("keys", {})[str(user_id)] = key
        self.save_auth_data()
        return key

    def get_key_for_user(self, user_id: int) -> Optional[str]:
        return self.auth_data.get("keys", {}).get(str(user_id))

    def verify_key(self, user_id: int, key: str) -> bool:
        stored_key = self.get_key_for_user(user_id)
        if stored_key and stored_key == key:
            self.authorized_users.add(user_id)
            self.save_auth_data()
            return True
        return False

    def is_authorized(self, user_id: int) -> bool:
        return user_id in self.authorized_users or user_id in Config.ADMIN_USER_IDS

    def add_authorized_user(self, user_id: int):
        self.authorized_users.add(user_id)
        self.save_auth_data()

auth_manager = AuthManager()

# --- AccountPoolManager ---
class AccountPoolManager:
    def __init__(self):
        self.accounts: List[Dict[str, Any]] = []
        self.lock = asyncio.Lock()
        self._load_accounts()
        logger.info(f"Loaded {len(self.accounts)} accounts")

    def _load_accounts(self):
        for filename in os.listdir(Config.SESSIONS_DIR):
            if filename.endswith('.session'):
                session_path = os.path.join(Config.SESSIONS_DIR, filename)
                try:
                    with open(session_path, 'r') as f:
                        session_string = f.read().strip()
                    self.accounts.append(
                        {
                            'session_file': filename,
                            'session_string': session_string,
                            'client': None,
                            'in_use': False,
                            'last_used': None,
                            'is_valid': True,
                            'flood_wait_until': None
                        }
                    )
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
                self.accounts.sort(key=lambda x: x['last_used'] or datetime.min)
                for acc in self.accounts:
                    if not acc['in_use'] and acc['is_valid']:
                        if acc.get('flood_wait_until') and acc['flood_wait_until'] > now:
                            continue
                        acc['in_use'] = True
                        acc['last_used'] = now
                        if not acc['client'] or not acc['client'].is_connected():
                            try:
                                if acc['client']:
                                    await acc['client'].disconnect()
                                acc['client'] = await self._create_client(acc['session_string'])
                                me = await acc['client'].get_me()
                                if not me:
                                    raise Exception("Not authorized")
                            except Exception as e:
                                logger.error(f"Error connecting: {e}")
                                acc['is_valid'] = False
                                acc['in_use'] = False
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
                    account['in_use'] = False
                    logger.info(f"Released account: {account['session_file']}")

    def add_account(self, session_string: str, session_name: str):
        session_path = os.path.join(Config.SESSIONS_DIR, f"{session_name}.session")
        with open(session_path, 'w') as f:
            f.write(session_string)
        self.accounts.append(
            {
                'session_file': f"{session_name}.session",
                'session_string': session_string,
                'client': None,
                'in_use': False,
                'last_used': None,
                'is_valid': True,
                'flood_wait_until': None
            }
        )
        logger.info(f"Added new account: {session_name}.session")

# --- TaskQueueManager ---
class TaskQueueManager:
    def __init__(self, max_concurrent_tasks: int = 3):
        self.max_concurrent_tasks = max_concurrent_tasks
        self.queue = asyncio.Queue()
        self.active_tasks = 0
        self.logger = logging.getLogger('task_queue')

    def start_workers(self):
        for i in range(self.max_concurrent_tasks):
            asyncio.create_task(self._worker(f"worker-{i + 1}"))

    async def _worker(self, name: str):
        self.logger.info(f"Worker {name} started")
        while True:
            task_func, args, kwargs = await self.queue.get()
            try:
                self.active_tasks += 1
                await task_func(*args, **kwargs)
            except Exception as e:
                self.logger.exception(f"Task error: {e}")
            finally:
                self.queue.task_done()
                self.active_tasks -= 1

    async def add_task(self, task_func, *args, **kwargs):
        await self.queue.put((task_func, args, kwargs))
        self.logger.info(f"Task added to queue, queue size: {self.queue.qsize()}")

# --- Инициализация ---
bot = Bot(token=Config.BOT_TOKEN, parse_mode=ParseMode.HTML)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)
account_pool = None
task_queue = None
pending_auth = {}

# --- Middleware ---
async def auth_middleware(handler, event, data):
    user_id = event.from_user.id
    if user_id in Config.ADMIN_USER_IDS or auth_manager.is_authorized(user_id):
        return await handler(event, data)
    if isinstance(event, Message) and event.text == '/start':
        return await handler(event, data)
    if isinstance(event, Message) and user_id in pending_auth:
        return await handler(event, data)
    if auth_manager.is_authorized(user_id):
        return await handler(event, data)
    if isinstance(event, Message):
        if user_id not in pending_auth:
            pending_auth[user_id] = True
            await event.answer(
                "🔑 <b>Требуется авторизация!</b>\n"
                "Пожалуйста, введите ключ доступа, который вы получили от администратора:"
            )
        else:
            await event.answer("⌛️ Ожидаю ввода ключа доступа...")
    return None

router.message.middleware(auth_middleware)
router.callback_query.middleware(auth_middleware)

# --- Обработчики команд ---
@router.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    if user_id in Config.ADMIN_USER_IDS:
        text = (
            "👑 <b>Добро пожаловать, администратор!</b>\n\n"
            "🔧 <b>Доступные команды:</b>\n"
            "/add_account - Добавить аккаунт Telegram\n"
            "/list_accounts - Список доступных аккаунтов\n"
            "/genkey - Сгенерировать ключ доступа\n"
            "/start_scraping - Начать сбор пользователей\n"
            "/task_stats - Показать статистику задач\n"
            "/help - Показать справку\n"
            "/ref - Зарабатывай на приглашениях\n\n"
            "⚠️ <b>Важно:</b> Ваши другие сессии Telegram останутся активными."
        )
        await message.answer(text)
        return
    if auth_manager.is_authorized(user_id):
        text = (
            "👋 <b>Добро пожаловать,пользователь!</b>\n\n"
            "🔧 <b>Доступные команды:</b>\n"
            "/add_account - Добавить аккаунт Telegram\n"
            "/list_accounts - Список доступных аккаунтов\n"
            "/start_scraping - Начать сбор пользователей\n"
            "/task_stats - Показать статистику задач\n"
            "/help - Показать справку\n"
            "/ref - Зарабатывай на приглашениях\n\n"
            "⚠️ <b>Важно:</b> Ваши другие сессии Telegram останутся активными."
        )
        await message.answer(text)
        return
    pending_auth[user_id] = True
    await message.answer(
        "🔒 <b>Требуется авторизация!</b>\n\n"
        "Для использования бота вам необходим ключ доступа.\n"
        "Пожалуйста, введите ключ, который вы получили от администратора:"
    )

@router.message(lambda message: message.from_user.id in pending_auth)
async def process_auth_key(message: Message):
    user_id = message.from_user.id
    key = message.text.strip()
    if auth_manager.verify_key(user_id, key):
        auth_manager.add_authorized_user(user_id)
        del pending_auth[user_id]
        await message.answer(
            "✅ <b>Авторизация успешна!</b>\n\n"
            "Теперь вы можете использовать все функции бота.\n"
            "Введите /start для просмотра доступных команд."
        )
    else:
        await notify_admins(
            f"⚠️ <b>Попытка несанкционированного доступа!</b>\n"
            f"• Пользователь: {user_id}\n"
            f"• Введенный ключ: {key}"
        )
        await message.answer(
            "❌ <b>Неверный ключ доступа!</b>\n\n"
            "Администраторы уведомлены о попытке входа.\n"
            "Пожалуйста, свяжитесь с администратором для получения действительного ключа."
        )

@router.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "📚 <b>Руководство по использованию бота</b>\n\n"
        "1. <b>Добавление аккаунтов</b> - используйте /add_account для добавления ваших аккаунтов Telegram\n"
        "2. <b>Начать сбор</b> - используйте /start_scraping для начала сбора пользователей\n"
        "3. <b>Приглашение пользователей</b> - собранные пользователи будут приглашены в вашу целевую группу\n\n"
        "⚙️ <b>Как это работает:</b>\n"
        "- Я анализирую сообщения в исходном чате\n"
        "- Собираю активных пользователей\n"
        "- Приглашаю их в вашу целевую группу\n\n"
        "⚠️ <b>Безопасность:</b>\n"
        "- Ваши другие сессии Telegram НЕ будут завершены\n"
        "- Сессии надежно хранятся и никогда не передаются третьим лицам"
    )
    await message.answer(text)

@router.message(Command("ref"))
async def cmd_ref(message: Message):
    text = (
        "💸 <b>Зарабатывай с рефералкой:</b>\n\n"
        "👥 <b>1 человек</b> = +200₽\n"
        "👥 <b>3 человека</b> = +700₽\n"
        "👥 <b>5 человек</b> = +1500₽\n"
        "👥 <b>10 человек</b> = +4000₽\n\n"
        "Чтобы реферал считался приведённым вами он должен при регистрации сообщить ваш юзернейм."
    )
    await message.answer(text)

@router.message(Command("add_account"))
async def cmd_add_account(message: Message, state: FSMContext):
    await message.answer(
        "📱 <b>Шаг 1/3</b>\n"
        "Пожалуйста, отправьте ваш номер телефона в международном формате (например, +71234567890):"
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
        "Вы согласны продолжить? (да/нет)"
    )
    await state.set_state(AddAccountStates.waiting_confirmation)

@router.message(AddAccountStates.waiting_confirmation)
async def process_confirmation(message: Message, state: FSMContext):
    confirmation = message.text.strip().lower()
    if confirmation not in ['yes', 'y', 'да', 'д']:
        await message.answer("❌ Добавление аккаунта отменено")
        await state.clear()
        return
    phone = (await state.get_data()).get('phone')
    if not phone:
        await message.answer("❌ Отсутствует номер телефона. Начните заново с /add_account")
        await state.clear()
        return
    client = create_telegram_client()
    await client.connect()
    try:
        sent_code = await client.send_code_request(phone)
        await state.update_data(client=client, phone_code_hash=sent_code.phone_code_hash, password_attempts=0)
        await message.answer(
            "🔑 <b>Шаг 2/3</b>\n"
            f"Telegram отправил код на ваш телефон ({phone}).\n"
            "Пожалуйста, введите код в формате: <code>12345</code>"
        )
        await state.set_state(AddAccountStates.waiting_code)
    except (PhoneNumberInvalidError, FloodWaitError) as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
        await client.disconnect()
        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Непредвиденная ошибка: {str(e)}")
        await client.disconnect()
        await state.clear()

@router.message(AddAccountStates.waiting_code)
async def process_code(message: Message, state: FSMContext):
    code = message.text.strip()
    data = await state.get_data()
    client = data['client']
    phone = data['phone']
    phone_code_hash = data['phone_code_hash']
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
                "⚠️ <b>Помните:</b> Ваши другие сессии останутся активными."
            )
            await persistent_client.disconnect()
            await state.clear()
        else:
            await message.answer("🔒 <b>Шаг 3/3</b>\nПожалуйста, введите ваш пароль двухфакторной аутентификации:")
            await state.update_data(password_attempts=0)
            await state.set_state(AddAccountStates.waiting_password)
    except PhoneCodeExpiredError:
        try:
            sent_code = await client.send_code_request(phone)
            await state.update_data(phone_code_hash=sent_code.phone_code_hash)
            await message.answer(
                "⚠️ <b>Код устарел!</b>\n"
                "Новый код был отправлен на ваш телефон.\n"
                "Пожалуйста, введите новый код:"
            )
        except Exception as e:
            await message.answer(f"❌ Ошибка при отправке нового кода: {str(e)}")
            await client.disconnect()
            await state.clear()
    except (PhoneCodeInvalidError, FloodWaitError) as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
        await client.disconnect()
        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Непредвиденная ошибка: {str(e)}")
        await client.disconnect()
        await state.clear()

@router.message(AddAccountStates.waiting_password)
async def process_password(message: Message, state: FSMContext):
    password = message.text.strip()
    data = await state.get_data()
    client = data['client']
    phone = data['phone']
    attempts = data.get('password_attempts', 0)
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
                "⚠️ <b>Помните:</b> Ваши другие сессии останутся активными."
            )
            await persistent_client.disconnect()
            await state.clear()
        else:
            await message.answer("❌ Авторизация не удалась. Пожалуйста, попробуйте снова.")
            await client.disconnect()
            await state.clear()
    except SessionPasswordNeededError:
        attempts += 1
        if attempts >= 3:
            await message.answer("❌ Превышено количество попыток ввода пароля. Добавление аккаунта отменено.")
            await client.disconnect()
            await state.clear()
        else:
            await state.update_data(password_attempts=attempts)
            await message.answer(f"❌ Неверный пароль. Осталось попыток: {3 - attempts}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
        await client.disconnect()
        await state.clear()

@router.message(Command("genkey"))
async def cmd_genkey(message: Message, state: FSMContext):
    if message.from_user.id not in Config.ADMIN_USER_IDS:
        return await message.answer("🚫 Только администраторы могут генерировать ключи доступа!")
    await message.answer(
        "🔑 <b>Генерация ключа доступа</b>\n\n"
        "Пожалуйста, введите ID пользователя, для которого нужно сгенерировать ключ:"
    )
    await state.set_state(KeyGeneration.waiting_user_id)
    return None


@router.message(KeyGeneration.waiting_user_id)
async def process_user_id(message: Message, state: FSMContext):
    try:
        user_id = int(message.text.strip())
        key = auth_manager.generate_key(user_id)
        await message.answer(
            "✅ <b>Ключ успешно сгенерирован!</b>\n\n"
            f"• ID пользователя: <code>{user_id}</code>\n"
            f"• Ключ доступа: <code>{key}</code>\n\n"
            "Передайте этот ключ пользователю. После ввода ключа пользователь получит доступ к боту."
        )
        await state.clear()
    except ValueError:
        await message.answer("❌ ID должен быть числом. Попробуйте снова:")

@router.message(Command("list_accounts"))
async def cmd_list_accounts(message: Message):
    if not account_pool.accounts:
        return await message.answer("ℹ️ Нет доступных аккаунтов. Используйте /add_account, чтобы добавить.")
    text = "📋 <b>Доступные аккаунты:</b>\n\n"
    for i, acc in enumerate(account_pool.accounts, 1):
        status = "🟢 Свободен" if not acc['in_use'] else "🔴 Используется"
        validity = "🟢 Рабочий" if acc['is_valid'] else "🔴 Не рабочий"
        flood = f"⏳ FloodWait до {acc['flood_wait_until']}" if acc.get('flood_wait_until') else ""
        text += (
            f"{i}. <code>{acc['session_file']}</code>\n"
            f"   Статус: {status} | {validity} {flood}\n"
            f"   Последнее использование: {acc['last_used'] or 'Никогда'}\n\n"
        )
    await message.answer(text)
    return None


@router.message(Command("start_scraping"))
async def cmd_start_scraping(message: Message, state: FSMContext):
    if not account_pool.accounts:
        return await message.answer("❌ Нет доступных аккаунтов! Сначала добавьте аккаунты с помощью /add_account")
    await message.answer(
        "🔍 <b>Шаг 1/3</b>\n"
        "Отправьте @username или пригласительную ссылку чата/канала, из которого нужно собрать пользователей:"
    )
    await state.set_state(ScrapingStates.waiting_source)
    return None


@router.message(ScrapingStates.waiting_source)
async def process_source(message: Message, state: FSMContext):
    source = message.text.strip()
    await state.update_data(source=source)
    await message.answer(
        "🎯 <b>Шаг 2/3</b>\n"
        "Отправьте @username или пригласительную ссылку группы/канала, в которую нужно пригласить пользователей:"
    )
    await state.set_state(ScrapingStates.waiting_target)

@router.message(ScrapingStates.waiting_target)
async def process_target(message: Message, state: FSMContext):
    target = message.text.strip()
    await state.update_data(target=target)
    await message.answer(
        "📊 <b>Шаг 3/3</b>\n"
        "Введите количество сообщений для анализа (рекомендуется 1000-5000):"
    )
    await state.set_state(ScrapingStates.waiting_message_limit)

@router.message(ScrapingStates.waiting_message_limit)
async def process_limit(message: Message, state: FSMContext):
    try:
        limit = int(message.text)
        if limit < 50 or limit > 5000:
            raise ValueError
    except ValueError:
        return await message.answer("⚠️ Неверное число! Пожалуйста, введите значение от 50 до 5000.")
    data = await state.get_data()
    source = data['source']
    target = data['target']
    await task_queue.add_task(
        scrape_and_invite_task,
        source=source,
        target=target,
        message_limit=limit,
        user_id=message.from_user.id
    )
    await message.answer(
        f"✅ <b>Задача запущена!</b>\n\n"
        f"• Источник: {source}\n"
        f"• Цель: {target}\n"
        f"• Сообщений: {limit}\n\n"
        "Вы получите отчет по завершении."
    )
    await state.clear()
    return None


@router.message(Command("task_stats"))
async def cmd_task_stats(message: Message):
    stats = (
        f"📊 <b>Статистика задач</b>\n\n"
        f"• Активные задачи: {task_queue.active_tasks}\n"
        f"• Задачи в очереди: {task_queue.queue.qsize()}\n"
        f"• Доступные аккаунты: {len([a for a in account_pool.accounts if not a['in_use'] and a['is_valid']])}/{len(account_pool.accounts)}"
    )
    await message.answer(stats)

# --- Задачи ---
invited_cache: Set[int] = set()

async def scrape_and_invite_task(source: str, target: str, message_limit: int, user_id: int):
    logger.info(f"Starting task: {source} -> {target} (limit: {message_limit})")
    try:
        async with account_pool.acquire_account() as account:
            client = account['client']
            try:
                active_users = await get_active_users(client, source, message_limit)
            except AuthKeyUnregisteredError:
                account['is_valid'] = False
                logger.error(f"Session invalid: {account['session_file']}")
                raise Exception("Сессия недействительна - пожалуйста, добавьте этот аккаунт заново")
            if not active_users:
                return await notify_user(
                    user_id,
                    f"❌ Не найдено активных пользователей в {source}\n"
                    "Пожалуйста, проверьте, есть ли у аккаунта доступ к чату."
                )
            results = await invite_users(account, client, active_users, target)
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
            return None
    except Exception as e:
        error_msg = (
            f"🔥 <b>Задача не выполнена!</b>\n\n"
            f"• Источник: {source}\n"
            f"• Цель: {target}\n\n"
            f"Ошибка: {str(e)}"
        )
        logger.exception(f"Task error: {source} -> {target}")
        await notify_user(user_id, error_msg)
        return None


async def get_active_users(client: TelegramClient, source_entity: str, limit: int) -> List[int]:
    logger.info(f"Collecting users from: {source_entity}")
    users = set()
    try:
        entity = await client.get_entity(source_entity)
        async for message in client.iter_messages(entity, limit=limit):
            if message and message.sender_id:
                try:
                    sender = await message.get_sender()
                    if isinstance(sender, telethon_types.User) and not sender.bot:
                        users.add(sender.id)
                except Exception:
                    users.add(message.sender_id)
        logger.info(f"Found {len(users)} active users")
        return list(users)
    except AuthKeyUnregisteredError as e:
        logger.error(f"AuthKeyUnregisteredError: {e}")
        raise
    except Exception as e:
        logger.error(f"Error collecting users: {str(e)}")
        raise

async def invite_users(account, client: TelegramClient, user_ids: List[int], target_entity: str) -> Dict[str, int]:
    logger.info(f"Inviting {len(user_ids)} users to {target_entity}")
    results = {'success': 0, 'failed': 0, 'privacy_errors': 0, 'already_members': 0}
    try:
        target = await client.get_entity(target_entity)
        current_participants = set()
        if isinstance(target, (telethon_types.Channel, telethon_types.Chat)):
            async for user in client.iter_participants(target):
                current_participants.add(user.id)
        for user_id in user_ids:
            if user_id in invited_cache:
                results['already_members'] += 1
                continue
            try:
                if user_id in current_participants:
                    results['already_members'] += 1
                    logger.info(f"User {user_id} is already a member")
                    continue
                user_entity = await client.get_entity(user_id)
                if isinstance(user_entity, telethon_types.User) and user_entity.bot:
                    logger.debug(f"Skipping bot user: {user_id}")
                    continue
                await client(
                    functions.channels.InviteToChannelRequest(
                        channel=target,
                        users=[user_entity]
                    )
                )
                results['success'] += 1
                logger.info(f"Successfully invited user: {user_id}")
                current_participants.add(user_id)
                invited_cache.add(user_id)
                # Динамическая задержка
                await asyncio.sleep(random.randint(60, 90))
            except UserPrivacyRestrictedError:
                results['privacy_errors'] += 1
                logger.warning(f"Privacy restriction for user: {user_id}")
            except (ChatAdminRequiredError, ChannelPrivateError):
                results['failed'] += 1
                logger.error("Admin rights required for inviting or private channel")
            except (FloodWaitError, FloodError) as e:
                logger.warning(f"Flood error, waiting: {e.seconds} seconds")
                account['flood_wait_until'] = datetime.now() + timedelta(seconds=e.seconds + 10)
                await asyncio.sleep(e.seconds + 10)
                break  # временно исключаем аккаунт из пула
            except UserNotParticipantError:
                results['failed'] += 1
                logger.warning(f"User {user_id} is not a participant in source chat")
            except AuthKeyUnregisteredError:
                account['is_valid'] = False
                logger.error(f"AuthKeyUnregisteredError: {account['session_file']}")
                break
            except Exception as e:
                results['failed'] += 1
                logger.error(f"Invite error for {user_id}: {str(e)}")
        return results
    except Exception as e:
        logger.error(f"Error inviting users: {str(e)}")
        raise

# --- Запуск ---
# main.py

async def main():
    logger.info(f"Admin IDs: {Config.ADMIN_USER_IDS}")
    logger.info("Запуск бота для инвайтинга трафика...")
    global account_pool, task_queue
    if not Config.BOT_TOKEN or Config.BOT_TOKEN == "YOUR_BOT_TOKEN":
        logger.critical("BOT_TOKEN not configured! Please set it in Config class")
        sys.exit(1)
    account_pool = AccountPoolManager()
    task_queue = TaskQueueManager(max_concurrent_tasks=Config.MAX_CONCURRENT_TASKS)
    task_queue.start_workers()
    if not account_pool.accounts:
        logger.warning("No accounts available! Users won't be able to start tasks")
    logger.info("Starting bot...")
    try:
        for admin_id in Config.ADMIN_USER_IDS:
            await safe_send_message(
                bot, admin_id,
                "🟢 <b>Бот успешно запущен!</b>\n"
                f"• Загружено аккаунтов: {len(account_pool.accounts)}\n"
                f"• Максимум одновременных задач: {Config.MAX_CONCURRENT_TASKS}\n\n"
                "⚠️ <b>Важно:</b> Ваши аккаунты теперь контролируются ботом. "
                "Ваши другие сессии Telegram останутся активными."
            )
        await dp.start_polling(bot)
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Остановка бота по запросу пользователя (IDE/KeyboardInterrupt)")
    finally:
        for admin_id in Config.ADMIN_USER_IDS:
            await safe_send_message(
                bot, admin_id,
                "🔴 <b>Бот остановлен!</b>\n\n"
                "Все сессии Telegram были освобождены. "
                "Теперь вы можете использовать свои аккаунты как обычно."
            )
        await bot.session.close()
        logger.info("Releasing all accounts...")
        for account in account_pool.accounts:
            if account['client']:
                try:
                    if account['client'].is_connected():
                        await account['client'].disconnect()
                    account['client'] = None
                    account['in_use'] = False
                    logger.info(f"Released session: {account['session_file']}")
                except Exception as e:
                    logger.error(f"Error releasing account {account['session_file']}: {e}")
        logger.info("All accounts released. Bot shutdown complete.")

if __name__ == "__main__":
    asyncio.run(main())