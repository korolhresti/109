import asyncio
import logging
import logging.handlers
from datetime import datetime, timedelta, timezone
import json
import os
import random
import io
import base64
import time
from typing import List, Optional, Dict, Any
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.utils.markdown import hlink
from aiogram.client.default import DefaultBotProperties

from aiohttp import ClientSession
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status, Depends, Request
from fastapi.security import APIKeyHeader
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from gtts import gTTS
from croniter import croniter
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Імпорт ваших локальних модулів
import web_parser
from database import get_db_pool

# Завантаження змінних середовища з .env файлу
load_dotenv()

# Налаштування логування
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

stream_handler = logging.StreamHandler()
stream_handler.setLevel(logging.INFO)
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

# Отримання токена бота та інших змінних середовища
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID")
CHANNEL_ID = os.getenv("CHANNEL_ID") # ID каналу для публікацій
MONOBANK_DONATE_LINK = os.getenv("MONOBANK_DONATE_LINK", "https://send.monobank.ua/jar/YOUR_JAR_ID") # Посилання на банку Монобанку

# Перевірка наявності необхідних змінних середовища
if not TELEGRAM_BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN environment variable is not set.")
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set.")
if not CHANNEL_ID:
    logger.warning("CHANNEL_ID environment variable is not set. News will not be auto-published to a channel.")

# Ініціалізація FastAPI додатку
app = FastAPI()

# Ініціалізація Telegram бота та диспетчера
bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# APScheduler для фонових завдань
scheduler = AsyncIOScheduler()

# API ключ для адмін-панелі
API_KEY = os.getenv("API_KEY")
api_key_header = APIKeyHeader(name="X-API-Key")

async def get_api_key(api_key: str = Depends(api_key_header)):
    """Залежність для перевірки API ключа."""
    if api_key == API_KEY:
        return api_key
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API Key")

# Моделі даних
class News(dict):
    """Проста модель для новин."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__dict__ = self

class Source(dict):
    """Проста модель для джерел."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__dict__ = self

class User(dict):
    """Модель для користувачів."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__dict__ = self

# Стан для вибору мови
class UserSettings(StatesGroup):
    choosing_language = State()
    waiting_for_term = State() # Новий стан для AI-аналітика

# --- Функції для роботи з базою даних ---

async def get_user_by_telegram_id(telegram_id: int) -> Optional[User]:
    """Отримує користувача з бази даних за Telegram ID."""
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
            rec = await cur.fetchone()
            return User(**rec) if rec else None

async def create_user(telegram_id: int, username: Optional[str] = None, first_name: Optional[str] = None, last_name: Optional[str] = None) -> User:
    """Створює нового користувача в базі даних."""
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "INSERT INTO users (telegram_id, username, first_name, last_name) VALUES (%s, %s, %s, %s) RETURNING *",
                (telegram_id, username, first_name, last_name)
            )
            return User(**await cur.fetchone())

async def update_user_last_active(telegram_id: int):
    """Оновлює час останньої активності користувача."""
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("UPDATE users SET last_active = %s WHERE telegram_id = %s", (datetime.now(timezone.utc), telegram_id))

async def update_user_language(telegram_id: int, lang_code: str):
    """Оновлює бажану мову користувача."""
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("UPDATE users SET preferred_language = %s WHERE telegram_id = %s", (lang_code, telegram_id))
            logger.info(f"Користувач {telegram_id} встановив мову: {lang_code}")

async def get_news_from_db(news_id: int) -> Optional[News]:
    """Отримує новину з бази даних за ID."""
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM news WHERE id = %s", (news_id,))
            rec = await cur.fetchone()
            return News(**rec) if rec else None

async def get_random_unmoderated_news() -> Optional[News]:
    """Отримує випадкову немодеровану новину для публікації в канал."""
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            # Обираємо новини, які не були опубліковані і не прострочені, і мають статус 'approved' або 'pending'
            await cur.execute("""
                SELECT * FROM news
                WHERE is_published_to_channel = FALSE
                AND (expires_at IS NULL OR expires_at > NOW())
                AND (moderation_status = 'approved' OR moderation_status = 'pending')
                ORDER BY RANDOM()
                LIMIT 1;
            """)
            rec = await cur.fetchone()
            return News(**rec) if rec else None

async def mark_news_as_published(news_id: int):
    """Позначає новину як опубліковану в канал."""
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("UPDATE news SET is_published_to_channel = TRUE WHERE id = %s", (news_id,))

async def update_news_in_db(news_id: int, news_data: Dict[str, Any]) -> Optional[News]:
    """Оновлює новину в базі даних."""
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            # Створюємо список полів для оновлення та їх значень
            update_fields = []
            update_values = []
            for key, value in news_data.items():
                if key in ["title", "content", "source_id", "source_url", "normalized_source_url",
                           "image_url", "published_at", "moderation_status", "expires_at",
                           "is_published_to_channel", "ai_classified_topics"]:
                    update_fields.append(f"{key} = %s")
                    if key == "ai_classified_topics":
                        update_values.append(json.dumps(value)) # Зберігаємо як JSON рядок
                    else:
                        update_values.append(value)
            
            if not update_fields:
                return await get_news_from_db(news_id) # Нічого оновлювати

            query = f"UPDATE news SET {', '.join(update_fields)} WHERE id = %s RETURNING *;"
            update_values.append(news_id) # Додаємо news_id для WHERE
            
            await cur.execute(query, tuple(update_values))
            updated_rec = await cur.fetchone()
            return News(**updated_rec) if updated_rec else None

async def delete_news_from_db(news_id: int) -> bool:
    """Видаляє новину з бази даних."""
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM news WHERE id = %s", (news_id,))
            return cur.rowcount > 0

async def add_news_to_db(news_data: Dict[str, Any]) -> Optional[News]:
    """Додає нову новину до бази даних."""
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            # Перевірка наявності обов'язкових полів
            required_fields = ["source_id", "title", "source_url", "normalized_source_url"]
            if not all(field in news_data for field in required_fields):
                raise ValueError(f"Missing required fields: {required_fields}")

            # Заповнення відсутніх полів значеннями за замовчуванням
            news_data.setdefault("content", None)
            news_data.setdefault("image_url", None)
            news_data.setdefault("published_at", datetime.now(timezone.utc))
            news_data.setdefault("moderation_status", "pending")
            news_data.setdefault("expires_at", None)
            news_data.setdefault("is_published_to_channel", False)
            news_data.setdefault("ai_classified_topics", [])

            columns = ", ".join(news_data.keys())
            placeholders = ", ".join(["%s"] * len(news_data))
            values = list(news_data.values())

            # Перетворення JSONB полів на JSON-рядки
            if "ai_classified_topics" in news_data:
                idx = list(news_data.keys()).index("ai_classified_topics")
                values[idx] = json.dumps(values[idx])

            query = f"INSERT INTO news ({columns}) VALUES ({placeholders}) RETURNING *;"
            await cur.execute(query, tuple(values))
            new_rec = await cur.fetchone()
            return News(**new_rec) if new_rec else None

async def get_all_users() -> List[User]:
    """Отримує список усіх користувачів."""
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM users ORDER BY created_at DESC;")
            return [User(**rec) for rec in await cur.fetchall()]

async def get_all_sources() -> List[Source]:
    """Отримує список усіх джерел."""
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM sources ORDER BY id;")
            return [Source(**rec) for rec in await cur.fetchall()]

async def get_all_news(limit: int = 100, offset: int = 0) -> List[News]:
    """Отримує список усіх новин."""
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM news ORDER BY published_at DESC LIMIT %s OFFSET %s;", (limit, offset))
            return [News(**rec) for rec in await cur.fetchall()]

# --- Gemini API інтеграція ---

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "") # Залиште порожнім, Canvas надасть його

async def translate_text_gemini(text: str, target_language: str) -> Optional[str]:
    """Перекладає текст за допомогою Gemini API."""
    if not text:
        return None
    
    prompt = f"Переклади наступний текст на {target_language} мову, зберігаючи оригінальне форматування (наприклад, HTML-теги, якщо вони є). Тільки переклад, без додаткових коментарів:\n\n{text}"
    
    chat_history = []
    chat_history.push({ "role": "user", "parts": [{ "text": prompt }] })
    payload = { "contents": chat_history }

    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    
    try:
        async with ClientSession() as session:
            async with session.post(apiUrl, headers={'Content-Type': 'application/json'}, json=payload) as response:
                response.raise_for_status()
                result = await response.json()
                
                if result.get("candidates") and result["candidates"][0].get("content") and result["candidates"][0]["content"].get("parts"):
                    return result["candidates"][0]["content"]["parts"][0]["text"]
                else:
                    logger.error(f"Неочікувана структура відповіді від Gemini API: {result}")
                    return None
    except Exception as e:
        logger.error(f"Помилка під час виклику Gemini API для перекладу: {e}", exc_info=True)
        return None

async def explain_term_gemini(term: str) -> Optional[str]:
    """Пояснює термін за допомогою Gemini API."""
    if not term:
        return "Будь ласка, введіть термін для пояснення."
    
    prompt = f"Будь ласка, поясніть термін або поняття '{term}' простою та зрозумілою мовою, надаючи ключові визначення та, можливо, короткий приклад. Відповідь має бути українською."
    
    chat_history = []
    chat_history.push({ "role": "user", "parts": [{ "text": prompt }] })
    payload = { "contents": chat_history }

    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    
    try:
        async with ClientSession() as session:
            async with session.post(apiUrl, headers={'Content-Type': 'application/json'}, json=payload) as response:
                response.raise_for_status()
                result = await response.json()
                
                if result.get("candidates") and result["candidates"][0].get("content") and result["candidates"][0]["content"].get("parts"):
                    return result["candidates"][0]["content"]["parts"][0]["text"]
                else:
                    logger.error(f"Неочікувана структура відповіді від Gemini API для пояснення терміна: {result}")
                    return "Не вдалося отримати пояснення для цього терміна."
    except Exception as e:
        logger.error(f"Помилка під час виклику Gemini API для пояснення терміна: {e}", exc_info=True)
        return "Виникла помилка під час отримання пояснення. Спробуйте пізніше."

# --- Aiogram handlers ---

@router.message(CommandStart())
async def command_start_handler(message: Message, state: FSMContext) -> None:
    """
    Обробляє команду /start.
    Реєструє користувача, якщо він новий, і вітає його.
    """
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    last_name = message.from_user.last_name

    user = await get_user_by_telegram_id(user_id)
    if not user:
        user = await create_user(user_id, username, first_name, last_name)
        await message.answer(f"Привіт, {first_name}! Ласкаво просимо до нашого бота. Я створив для вас новий обліковий запис.")
        logger.info(f"Новий користувач зареєстрований: {user_id} ({username})")
    else:
        await message.answer(f"З поверненням, {first_name}!")
        logger.info(f"Користувач {user_id} ({username}) повернувся.")
    
    await update_user_last_active(user_id)

    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🔁 Отримати останню новину", callback_data="get_latest_news"))
    builder.row(types.InlineKeyboardButton(text="⚙️ Налаштування перекладу", callback_data="translation_settings"))
    builder.row(types.InlineKeyboardButton(text="🧠 Пояснити термін (AI)", callback_data="explain_term"))
    builder.row(types.InlineKeyboardButton(text="💎 Преміум", callback_data="premium"))
    builder.row(types.InlineKeyboardButton(text="❤️ Підтримати (Донат)", url=MONOBANK_DONATE_LINK))


    await message.answer(
        "Я бот для новин. Ви можете використовувати мене для отримання та управління новинами.",
        reply_markup=builder.as_markup()
    )

@router.message(Command("help"))
async def command_help_handler(message: Message) -> None:
    """Обробляє команду /help."""
    help_text = (
        "Я бот для новин. Ось що я можу робити:\n"
        "/start - Почати взаємодію з ботом та отримати основні кнопки\n"
        "/help - Показати це повідомлення допомоги\n"
        "/latest_news - Отримати останню новину вручну\n"
        "/translate_settings - Налаштування мови перекладу\n"
        "/explain_term - Пояснити термін за допомогою AI\n"
        "/premium - Дізнатися про преміум-функції\n"
        "/donate - Підтримати проект донатом\n"
    )
    await message.answer(help_text)

@router.message(Command("latest_news"))
@router.callback_query(F.data == "get_latest_news")
async def get_latest_news_handler(callback_or_message: Union[Message, CallbackQuery]) -> None:
    """
    Обробник для отримання останніх новин.
    """
    user_id = callback_or_message.from_user.id
    await update_user_last_active(user_id)

    # Приклад: Отримати 1 останню новину з бази даних
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM news ORDER BY published_at DESC LIMIT 1;")
            news_item = await cur.fetchone()

    if news_item:
        news_item = News(**news_item) # Перетворюємо на об'єкт News
        title = news_item.get("title", "Без заголовка")
        source_url = news_item.get("source_url", "#")
        content_snippet = news_item.get("content", "Немає вмісту.")
        if content_snippet and len(content_snippet) > 500: # Обмеження до 500 символів
            content_snippet = content_snippet[:500] + "..."
        
        response_text = f"<b>{hlink(title, source_url)}</b>\n\n{content_snippet}"
        
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(text="🔗 Читати далі", url=source_url))
        builder.row(types.InlineKeyboardButton(text="🔁 Отримати наступну новину", callback_data="get_latest_news"))
        builder.row(types.InlineKeyboardButton(text="🌐 Перекласти", callback_data=f"translate_news_{news_item['id']}"))


        if isinstance(callback_or_message, Message):
            send_func = callback_or_message.answer
            send_photo_func = callback_or_message.answer_photo
        else: # CallbackQuery
            await callback_or_message.answer() # Відповідаємо на callback, щоб прибрати "годинник"
            send_func = callback_or_message.message.answer
            send_photo_func = callback_or_message.message.answer_photo

        if news_item.get("image_url"):
            try:
                await send_photo_func(
                    photo=news_item["image_url"],
                    caption=response_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=builder.as_markup()
                )
            except Exception as e:
                logger.warning(f"Не вдалося відправити фото для новини {news_item['id']}: {e}. Відправляю текст.")
                await send_func(response_text, parse_mode=ParseMode.HTML, reply_markup=builder.as_markup())
        else:
            await send_func(response_text, parse_mode=ParseMode.HTML, reply_markup=builder.as_markup())
    else:
        if isinstance(callback_or_message, CallbackQuery):
            await callback_or_message.answer("Наразі немає новин.")
        else:
            await callback_or_message.answer("Наразі немає новин.")

@router.callback_query(F.data.startswith("translate_news_"))
async def translate_news_callback_handler(callback: CallbackQuery):
    """Обробляє запит на переклад новини."""
    await callback.answer("Перекладаю новину...", show_alert=False)
    news_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id
    
    user = await get_user_by_telegram_id(user_id)
    if not user or not user.get("preferred_language"):
        await callback.message.answer("Будь ласка, спочатку оберіть мову перекладу в налаштуваннях.")
        return

    news_item = await get_news_from_db(news_id)
    if not news_item or not news_item.get("content"):
        await callback.message.answer("Не вдалося знайти новину або її вміст для перекладу.")
        return

    target_language = user["preferred_language"]
    
    # Виклик Gemini API для перекладу
    translated_content = await translate_text_gemini(news_item["content"], target_language)

    if translated_content:
        title = news_item.get("title", "Без заголовка")
        source_url = news_item.get("source_url", "#")
        
        response_text = f"<b>{hlink(title, source_url)}</b> (переклад на {target_language})\n\n{translated_content}"
        
        # Обмеження довжини тексту для Telegram повідомлення
        if len(response_text) > 4096:
            response_text = response_text[:4000] + "...\n\n(Повний текст за посиланням)"

        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(text="🔗 Оригінал", url=source_url))
        
        await callback.message.answer(response_text, parse_mode=ParseMode.HTML, reply_markup=builder.as_markup())
    else:
        await callback.message.answer("На жаль, не вдалося перекласти новину. Спробуйте пізніше.")


@router.callback_query(F.data == "translation_settings")
async def translation_settings_handler(callback: CallbackQuery, state: FSMContext):
    """Показує налаштування перекладу."""
    await callback.answer()
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="Вибрати мову перекладу", callback_data="select_translation_language"))
    # builder.row(types.InlineKeyboardButton(text="Увімкнути/Вимкнути автопереклад", callback_data="toggle_auto_translate")) # Можна додати пізніше
    await callback.message.answer("Оберіть опцію налаштувань перекладу:", reply_markup=builder.as_markup())

@router.callback_query(F.data == "select_translation_language")
async def select_translation_language_handler(callback: CallbackQuery, state: FSMContext):
    """Пропонує користувачеві вибрати мову перекладу."""
    await callback.answer()
    await state.set_state(UserSettings.choosing_language)

    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="Українська", callback_data="set_lang_uk"))
    builder.row(types.InlineKeyboardButton(text="Англійська", callback_data="set_lang_en"))
    builder.row(types.InlineKeyboardButton(text="Німецька", callback_data="set_lang_de"))
    builder.row(types.InlineKeyboardButton(text="Французька", callback_data="set_lang_fr"))
    builder.row(types.InlineKeyboardButton(text="Польська", callback_data="set_lang_pl"))
    await callback.message.answer("Будь ласка, оберіть мову для перекладу новин:", reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("set_lang_"), UserSettings.choosing_language)
async def set_language_handler(callback: CallbackQuery, state: FSMContext):
    """Встановлює обрану користувачем мову перекладу."""
    await callback.answer()
    lang_code = callback.data.split("_")[2]
    await update_user_language(callback.from_user.id, lang_code)
    await callback.message.answer(f"Мову перекладу встановлено на: {lang_code.upper()}")
    await state.clear()


@router.message(Command("explain_term"))
@router.callback_query(F.data == "explain_term")
async def explain_term_command_handler(callback_or_message: Union[Message, CallbackQuery], state: FSMContext):
    """Запитує термін для пояснення за допомогою AI."""
    if isinstance(callback_or_message, CallbackQuery):
        await callback_or_message.answer()
        await callback_or_message.message.answer("Будь ласка, введіть термін або поняття, яке ви хочете, щоб AI пояснив:")
    else:
        await callback_or_message.answer("Будь ласка, введіть термін або поняття, яке ви хочете, щоб AI пояснив:")
    await state.set_state(UserSettings.waiting_for_term)

@router.message(UserSettings.waiting_for_term)
async def process_term_for_explanation(message: Message, state: FSMContext):
    """Обробляє термін, введений користувачем, і викликає AI для пояснення."""
    term = message.text.strip()
    if not term:
        await message.answer("Ви не ввели термін. Будь ласка, спробуйте ще раз.")
        return

    await message.answer(f"Шукаю пояснення для '{term}'...")
    explanation = await explain_term_gemini(term)
    
    await message.answer(explanation, parse_mode=ParseMode.HTML)
    await state.clear()


@router.message(Command("premium"))
@router.callback_query(F.data == "premium")
async def premium_handler(callback_or_message: Union[Message, CallbackQuery]):
    """Інформація про преміум-функції."""
    text = (
        "💎 **Преміум-функції:**\n"
        "✨ Більше AI-запитів на день\n"
        "📊 Розширена статистика\n"
        "🚀 Пріоритетний доступ до нових функцій\n"
        "Щоб отримати преміум, будь ласка, зв'яжіться з адміністратором: @admin_username (замініть на реальний username)."
    )
    if isinstance(callback_or_message, CallbackQuery):
        await callback_or_message.answer()
        await callback_or_message.message.answer(text, parse_mode=ParseMode.MARKDOWN)
    else:
        await callback_or_message.answer(text, parse_mode=ParseMode.MARKDOWN)

@router.message(Command("donate"))
@router.callback_query(F.data == "donate")
async def donate_handler(callback_or_message: Union[Message, CallbackQuery]):
    """Інформація про донати."""
    text = (
        "❤️ **Підтримати проект:**\n"
        "Ваша підтримка допомагає нам розвивати бота та підтримувати його роботу!\n"
        f"Ви можете зробити донат за посиланням: {MONOBANK_DONATE_LINK}\n"
        "Дякуємо за вашу щедрість!"
    )
    if isinstance(callback_or_message, CallbackQuery):
        await callback_or_message.answer()
        await callback_or_message.message.answer(text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    else:
        await callback_or_message.answer(text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

# --- Background tasks (APScheduler) ---

async def parse_active_sources():
    """
    Фонова задача для парсингу активних джерел новин.
    """
    logger.info("Запуск фонового завдання: parse_active_sources")
    active_sources = await get_all_active_sources()
    for source in active_sources:
        source_id = source["id"]
        source_url = source["feed_url"] # Припускаємо, що feed_url є основним URL для парсингу
        source_type = source["source_type"]
        
        logger.info(f"Парсинг джерела {source_id} ({source_type}): {source_url}")
        
        parsed_data = None
        if source_type == "web":
            parsed_data = await web_parser.parse_website(source_url)
        # Додайте інші типи парсерів тут (rss, telegram, social_media)
        # elif source_type == "rss":
        #     parsed_data = await rss_parser.parse_rss_feed(source_url)
        # elif source_type == "telegram":
        #     parsed_data = await telegram_parser.parse_telegram_channel(source_url)
        # elif source_type == "social_media":
        #     parsed_data = await social_media_parser.parse_social_media(source_url)

        if parsed_data:
            try:
                # Перевіряємо, чи новина вже існує за normalized_source_url
                pool = await get_db_pool()
                async with pool.connection() as conn:
                    async with conn.cursor(row_factory=dict_row) as cur:
                        normalized_url = parsed_data.get("normalized_source_url", parsed_data["source_url"])
                        await cur.execute("SELECT id FROM news WHERE normalized_source_url = %s", (normalized_url,))
                        existing_news = await cur.fetchone()

                        if existing_news:
                            logger.info(f"Новина з URL {normalized_url} вже існує, оновлюємо.")
                            # Оновлення існуючої новини, якщо потрібно
                            # await update_news_in_db(existing_news["id"], parsed_data)
                        else:
                            # Додаємо нову новину
                            parsed_data["source_id"] = source_id
                            parsed_data["normalized_source_url"] = parsed_data.get("normalized_source_url", parsed_data["source_url"])
                            new_news = await add_news_to_db(parsed_data)
                            if new_news:
                                logger.info(f"Додано нову новину: {new_news['title']} (ID: {new_news['id']})")
                                # Оповіщення адміна про нову новину (приклад)
                                if ADMIN_TELEGRAM_ID:
                                    try:
                                        await bot.send_message(
                                            chat_id=ADMIN_TELEGRAM_ID,
                                            text=f"Нова новина додана: {hlink(new_news['title'], new_news['source_url'])}"
                                        )
                                    except Exception as e:
                                        logger.error(f"Помилка при відправці сповіщення адміну: {e}")
            except Exception as e:
                logger.error(f"Помилка при збереженні новини з {source_url} в БД: {e}", exc_info=True)
        else:
            logger.warning(f"Не вдалося розпарсити дані з джерела {source_id}: {source_url}")

async def publish_news_to_channel():
    """
    Фонова задача для публікації новин в Telegram-канал.
    """
    if not CHANNEL_ID:
        logger.warning("CHANNEL_ID не встановлено, публікація в канал неможлива.")
        return

    logger.info("Запуск фонового завдання: publish_news_to_channel")
    news_item = await get_random_unmoderated_news()

    if news_item:
        title = news_item.get("title", "Без заголовка")
        source_url = news_item.get("source_url", "#")
        content_snippet = news_item.get("content", "Немає вмісту.")
        if content_snippet and len(content_snippet) > 500:
            content_snippet = content_snippet[:500] + "..."
        
        message_text = f"<b>{hlink(title, source_url)}</b>\n\n{content_snippet}"

        try:
            if news_item.get("image_url"):
                await bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=news_item["image_url"],
                    caption=message_text,
                    parse_mode=ParseMode.HTML
                )
            else:
                await bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=message_text,
                    parse_mode=ParseMode.HTML
                )
            await mark_news_as_published(news_item["id"])
            logger.info(f"Новина {news_item['id']} опублікована в канал.")
        except Exception as e:
            logger.error(f"Помилка при публікації новини {news_item['id']} в канал: {e}", exc_info=True)
    else:
        logger.info("Немає новин для публікації в канал.")


# --- FastAPI endpoints ---

@app.on_event("startup")
async def on_startup():
    """
    Виконується при запуску FastAPI додатку.
    Ініціалізує базу даних та запускає APScheduler.
    """
    logger.info("Запуск FastAPI додатку...")
    try:
        await get_db_pool() # Перевіряємо з'єднання з БД
        logger.info("З'єднання з базою даних встановлено.")
    except Exception as e:
        logger.error(f"Не вдалося підключитися до бази даних при запуску: {e}", exc_info=True)
        raise

    # Запускаємо APScheduler
    scheduler.start()
    logger.info("APScheduler запущено.")

    # Додаємо завдання для парсингу джерел (кожні 15 хвилин)
    scheduler.add_job(parse_active_sources, 'interval', minutes=15, id='parse_sources_job')
    logger.info("Завдання 'parse_active_sources' додано до планувальника.")

    # Додаємо завдання для публікації новин в канал (кожні 5 хвилин)
    if CHANNEL_ID:
        scheduler.add_job(publish_news_to_channel, 'interval', minutes=5, id='publish_news_job')
        logger.info("Завдання 'publish_news_to_channel' додано до планувальника.")
    else:
        logger.warning("CHANNEL_ID не встановлено, завдання публікації новин не буде додано.")

    # Запускаємо Telegram бота
    asyncio.create_task(dp.start_polling(bot))
    logger.info("Telegram бот запущено в режимі polling.")

@app.on_event("shutdown")
async def on_shutdown():
    """
    Виконується при завершенні роботи FastAPI додатку.
    Зупиняє APScheduler та закриває пул з'єднань з БД.
    """
    logger.info("Завершення роботи FastAPI додатку...")
    if scheduler.running:
        scheduler.shutdown()
        logger.info("APScheduler зупинено.")
    
    # Закриття пулу з'єднань з БД
    global db_pool
    if db_pool:
        await db_pool.close()
        logger.info("Пул з'єднань з базою даних закрито.")

@app.get("/", response_class=HTMLResponse)
async def read_root():
    """Простий корневий ендпоінт для перевірки роботи FastAPI."""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>News Bot API</title>
        <style>
            body { font-family: sans-serif; text-align: center; margin-top: 50px; background-color: #f4f4f4; color: #333; }
            h1 { color: #2c3e50; }
            p { color: #7f8c8d; }
            .container {
                max-width: 800px;
                margin: 0 auto;
                padding: 20px;
                background-color: #fff;
                border-radius: 8px;
                box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
            }
            .button {
                display: inline-block;
                padding: 10px 20px;
                margin: 10px;
                background-color: #3498db;
                color: white;
                text-decoration: none;
                border-radius: 5px;
                transition: background-color 0.3s ease;
            }
            .button:hover {
                background-color: #2980b9;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>News Bot API працює!</h1>
            <p>Це бекенд для вашого Telegram бота новин.</p>
            <p>Перевірте логи на Render для отримання додаткової інформації.</p>
            <p>
                <a href="/admin" class="button">Перейти до Адмін-панелі</a>
            </p>
        </div>
    </body>
    </html>
    """

@app.get("/admin", response_class=HTMLResponse, dependencies=[Depends(get_api_key)])
async def admin_dashboard(request: Request):
    """Веб-панель адміністратора: Головна сторінка."""
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Адмін-панель</title>
        <style>
            body {{ font-family: sans-serif; margin: 20px; background-color: #f4f4f4; color: #333; }}
            .container {{ max-width: 900px; margin: 0 auto; padding: 20px; background-color: #fff; border-radius: 8px; box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1); }}
            h1 {{ color: #2c3e50; }}
            ul {{ list-style-type: none; padding: 0; }}
            li {{ margin-bottom: 10px; }}
            a {{ text-decoration: none; color: #3498db; font-weight: bold; }}
            a:hover {{ text-decoration: underline; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Адмін-панель Бота Новин</h1>
            <p>Ласкаво просимо до панелі адміністратора. Тут ви можете керувати користувачами, джерелами новин та самими новинами.</p>
            <ul>
                <li><a href="/admin/users">Керування користувачами</a></li>
                <li><a href="/admin/sources">Керування джерелами новин</a></li>
                <li><a href="/admin/news">Керування новинами</a></li>
                <li><a href="/">На головну сторінку</a></li>
            </ul>
        </div>
    </body>
    </html>
    """

@app.get("/admin/users", response_class=HTMLResponse, dependencies=[Depends(get_api_key)])
async def admin_users(request: Request):
    """Веб-панель адміністратора: Список користувачів."""
    users = await get_all_users()
    users_html = ""
    for user in users:
        users_html += f"""
        <li>
            <b>ID:</b> {user.get('telegram_id')} (TG: {user.get('username') or 'N/A'}) - 
            <b>Ім'я:</b> {user.get('first_name')} {user.get('last_name') or ''} - 
            <b>Адмін:</b> {user.get('is_admin')} - 
            <b>Мова:</b> {user.get('preferred_language', 'uk')} - 
            <b>Преміум:</b> {user.get('is_premium')}
        </li>
        """
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Користувачі</title>
        <style>
            body {{ font-family: sans-serif; margin: 20px; background-color: #f4f4f4; color: #333; }}
            .container {{ max-width: 900px; margin: 0 auto; padding: 20px; background-color: #fff; border-radius: 8px; box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1); }}
            h1 {{ color: #2c3e50; }}
            ul {{ list-style-type: none; padding: 0; }}
            li {{ margin-bottom: 8px; padding: 8px; border-bottom: 1px solid #eee; }}
            a {{ text-decoration: none; color: #3498db; }}
            a:hover {{ text-decoration: underline; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Користувачі</h1>
            <ul>{users_html}</ul>
            <p><a href="/admin">Повернутися до адмін-панелі</a></p>
        </div>
    </body>
    </html>
    """

@app.get("/admin/sources", response_class=HTMLResponse, dependencies=[Depends(get_api_key)])
async def admin_sources(request: Request):
    """Веб-панель адміністратора: Список джерел."""
    sources = await get_all_sources()
    sources_html = ""
    for source in sources:
        sources_html += f"""
        <li>
            <b>ID:</b> {source.get('id')} - 
            <b>Назва:</b> {source.get('name')} - 
            <b>Тип:</b> {source.get('source_type')} - 
            <b>URL:</b> {source.get('feed_url')} - 
            <b>Статус:</b> {source.get('status')}
        </li>
        """
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Джерела Новин</title>
        <style>
            body {{ font-family: sans-serif; margin: 20px; background-color: #f4f4f4; color: #333; }}
            .container {{ max-width: 900px; margin: 0 auto; padding: 20px; background-color: #fff; border-radius: 8px; box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1); }}
            h1 {{ color: #2c3e50; }}
            ul {{ list-style-type: none; padding: 0; }}
            li {{ margin-bottom: 8px; padding: 8px; border-bottom: 1px solid #eee; }}
            a {{ text-decoration: none; color: #3498db; }}
            a:hover {{ text-decoration: underline; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Джерела Новин</h1>
            <ul>{sources_html}</ul>
            <p><a href="/admin">Повернутися до адмін-панелі</a></p>
        </div>
    </body>
    </html>
    """

@app.get("/admin/news", response_class=HTMLResponse, dependencies=[Depends(get_api_key)])
async def admin_news(request: Request):
    """Веб-панель адміністратора: Список новин."""
    news_items = await get_all_news(limit=20) # Обмежимо для адмін-панелі
    news_html = ""
    for news in news_items:
        news_html += f"""
        <li>
            <b>ID:</b> {news.get('id')} - 
            <b>Заголовок:</b> {news.get('title', 'N/A')[:100]}... - 
            <b>Джерело ID:</b> {news.get('source_id')} - 
            <b>Статус:</b> {news.get('moderation_status')} - 
            <b>Опубліковано:</b> {news.get('is_published_to_channel')}
            <br>
            <small>URL: <a href="{news.get('source_url')}" target="_blank">{news.get('source_url')}</a></small>
        </li>
        """
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Новини</title>
        <style>
            body {{ font-family: sans-serif; margin: 20px; background-color: #f4f4f4; color: #333; }}
            .container {{ max-width: 900px; margin: 0 auto; padding: 20px; background-color: #fff; border-radius: 8px; box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1); }}
            h1 {{ color: #2c3e50; }}
            ul {{ list-style-type: none; padding: 0; }}
            li {{ margin-bottom: 8px; padding: 8px; border-bottom: 1px solid #eee; }}
            a {{ text-decoration: none; color: #3498db; }}
            a:hover {{ text-decoration: underline; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Новини</h1>
            <ul>{news_html}</ul>
            <p><a href="/admin">Повернутися до адмін-панелі</a></p>
        </div>
    </body>
    </html>
    """

# Запуск Uvicorn (для локального тестування, Render використовує Procfile)
if __name__ == "__main__":
    import uvicorn
    # Увага: Цей блок буде виконуватися лише при прямому запуску файлу bot.py
    # Render запускає додаток через 'uvicorn bot:app'
    # Для локального тестування переконайтеся, що у вас встановлені всі залежності
    # і змінні середовища (DATABASE_URL, TELEGRAM_BOT_TOKEN, ADMIN_TELEGRAM_ID, API_KEY, CHANNEL_ID, MONOBANK_DONATE_LINK)
    # встановлені у вашому .env файлі.
    
    uvicorn.run(app, host="0.0.0.0", port=8000)
