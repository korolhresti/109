import asyncio
import logging
import logging.handlers
from datetime import datetime, timedelta, timezone, date
import json
import os
import random
import io
import base64
import time
from typing import List, Optional, Dict, Any, Union
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup # Переконайтеся, що State та StatesGroup імпортовані
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
from pydantic import BaseModel, Field # Імпортуємо BaseModel та Field з pydantic

# Імпорт ваших локальних модулів
import web_parser
from database import get_db_pool, get_user_by_telegram_id, update_user_field, get_source_by_id, get_all_active_sources, add_news_item, get_news_by_source_id, get_all_news, get_user_bookmarks, add_bookmark, delete_bookmark, get_user_news_views, add_user_news_view, get_user_news_reactions, add_user_news_reaction, update_news_item, get_news_item_by_id, get_source_by_url, add_source, update_source_status, get_all_sources, get_bot_setting, update_bot_setting, get_user_by_id, get_last_n_news, update_source_last_parsed, get_news_for_digest, get_tasks_by_status, update_task_status, add_task_to_queue, get_all_users, get_user_subscriptions, add_user_subscription, delete_user_subscription, get_all_subscribed_sources, get_source_stats, update_source_stats
from config import TELEGRAM_BOT_TOKEN, ADMIN_TELEGRAM_ID, WEB_APP_URL, API_KEY_NAME, API_KEY # Імпортуємо всі необхідні змінні з config

# Налаштування логування
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Додаємо обробник для виведення логів у консоль
stream_handler = logging.StreamHandler()
stream_handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

# Ініціалізація FastAPI
app = FastAPI(title="News Bot API")

# API Key для доступу до адмін-панелі
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

async def get_api_key(api_key: str = Depends(api_key_header)):
    if api_key == API_KEY:
        return api_key
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Could not validate credentials")

# Ініціалізація бота та диспетчера
# Використовуємо DefaultBotProperties для встановлення ParseMode
bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()

# Визначення станів для FSM
class UserSettings(StatesGroup):
    choosing_language = State()
    choosing_digest_frequency = State()
    choosing_safe_mode = State()
    choosing_view_mode = State()
    choosing_notifications_setting = State()
    admin_panel = State()
    admin_add_source = State()
    admin_edit_source = State()
    admin_delete_source = State()
    admin_manage_users = State()
    admin_edit_bot_settings = State()
    admin_test_parse = State()
    admin_send_message = State()
    admin_select_source_for_parsing = State()
    admin_confirm_delete_source = State()
    admin_select_user_for_management = State()
    admin_confirm_delete_user = State()
    admin_edit_user_premium = State()
    admin_edit_user_pro = State()
    admin_edit_user_digest = State()
    admin_edit_user_ai_requests = State()
    admin_edit_user_language = State()
    admin_select_setting_to_edit = State()
    admin_enter_setting_value = State()
    admin_select_parse_source = State()
    admin_enter_message_text = State()
    admin_confirm_send_message = State()
    admin_select_message_target = State()
    admin_select_message_type = State()
    admin_select_message_user = State()
    admin_select_message_group = State()
    admin_select_message_all = State()
    admin_select_message_premium = State()
    admin_select_message_pro = State()
    admin_select_message_digest_enabled = State()
    admin_select_message_auto_notifications_enabled = State()
    admin_select_message_language = State()
    admin_select_message_language_code = State()
    admin_select_message_language_confirm = State()


class SourceManagement(StatesGroup):
    waiting_for_url = State()
    waiting_for_name = State()
    waiting_for_category = State()
    waiting_for_language = State()
    waiting_for_status = State()
    waiting_for_parse_interval = State()
    waiting_for_edit_id = State()
    waiting_for_delete_id = State()

class NewsDigest(StatesGroup):
    waiting_for_digest_send_time = State()

# Функція для перевірки, чи є користувач адміністратором
async def is_admin_check(message: Message) -> bool:
    user_data = await get_user_by_telegram_id(message.from_user.id)
    return user_data and user_data.get('is_admin', False)

# Функція для отримання або створення користувача
async def get_or_create_user(telegram_id: int, username: str, first_name: str, last_name: str) -> Dict[str, Any]:
    user = await get_user_by_telegram_id(telegram_id)
    if not user:
        async with get_db_pool() as pool:
            async with pool.connection() as conn:
                async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                    await cur.execute(
                        "INSERT INTO users (telegram_id, username, first_name, last_name) VALUES (%s, %s, %s, %s) RETURNING *;",
                        (telegram_id, username, first_name, last_name)
                    )
                    user = await cur.fetchone()
                    await conn.commit()
        logger.info(f"New user registered: {username} ({telegram_id})")
    else:
        # Оновлюємо last_active при кожній взаємодії
        await update_user_field(telegram_id, 'last_active', datetime.now(timezone.utc))
        # Оновлюємо ai_requests_today, якщо дата змінилася
        if user.get('ai_last_request_date') != date.today():
            await update_user_field(telegram_id, 'ai_requests_today', 0)
            await update_user_field(telegram_id, 'ai_last_request_date', date.today())

    return user

# Middleware для всіх повідомлень
@dp.message()
async def user_middleware(message: Message, state: FSMContext):
    if message.from_user:
        await get_or_create_user(
            message.from_user.id,
            message.from_user.username,
            message.from_user.first_name,
            message.from_user.last_name
        )
    await dp.router.process_message(message, state)


# Обробник команди /start
@router.message(CommandStart())
async def command_start_handler(message: Message, state: FSMContext) -> None:
    user = await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.last_name
    )
    kb = [
        [
            InlineKeyboardButton(text="⚙️ Налаштування", callback_data="settings"),
            InlineKeyboardButton(text="📰 Моя стрічка", callback_data="my_feed")
        ],
        [
            InlineKeyboardButton(text="🔖 Закладки", callback_data="bookmarks"),
            InlineKeyboardButton(text="🔍 Пошук новин", callback_data="search_news")
        ],
        [
            InlineKeyboardButton(text="🤖 AI Асистент", callback_data="ai_assistant"),
            InlineKeyboardButton(text="🎁 Преміум", callback_data="premium")
        ],
        [
            InlineKeyboardButton(text="ℹ️ Про бота", callback_data="about_bot")
        ]
    ]
    if user.get('is_admin'):
        kb.append([InlineKeyboardButton(text="🛠️ Адмін-панель", callback_data="admin_panel")])

    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    welcome_text = (
        f"Привіт, {message.from_user.first_name}! Я твій персональний новинний бот. "
        "Обери дію з меню нижче:"
    )
    await message.answer(welcome_text, reply_markup=markup)
    await state.clear()

# Обробник команди /menu
@router.message(Command("menu"))
async def command_menu_handler(message: Message, state: FSMContext) -> None:
    user = await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.last_name
    )
    kb = [
        [
            InlineKeyboardButton(text="⚙️ Налаштування", callback_data="settings"),
            InlineKeyboardButton(text="📰 Моя стрічка", callback_data="my_feed")
        ],
        [
            InlineKeyboardButton(text="🔖 Закладки", callback_data="bookmarks"),
            InlineKeyboardButton(text="🔍 Пошук новин", callback_data="search_news")
        ],
        [
            InlineKeyboardButton(text="🤖 AI Асистент", callback_data="ai_assistant"),
            InlineKeyboardButton(text="🎁 Преміум", callback_data="premium")
        ],
        [
            InlineKeyboardButton(text="ℹ️ Про бота", callback_data="about_bot")
        ]
    ]
    if user.get('is_admin'):
        kb.append([InlineKeyboardButton(text="🛠️ Адмін-панель", callback_data="admin_panel")])

    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    await message.answer("Головне меню:", reply_markup=markup)
    await state.clear()


# Обробник кнопки "Налаштування"
@router.callback_query(F.data == "settings")
async def settings_callback_handler(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_telegram_id(callback.from_user.id)
    if not user:
        await callback.answer("Користувача не знайдено.", show_alert=True)
        return

    lang_status = "✅" if user.get('preferred_language') == 'uk' else "🇺🇸" # Приклад: якщо 'uk' - українська, інакше англійська
    notifications_status = "✅ Увімкнено" if user.get('auto_notifications') else "❌ Вимкнено"
    digest_freq = user.get('digest_frequency', 'daily')
    safe_mode_status = "✅ Увімкнено" if user.get('safe_mode') else "❌ Вимкнено"
    view_mode_status = "Детальний" if user.get('view_mode') == 'detailed' else "Короткий"

    kb = [
        [InlineKeyboardButton(text=f"Мова новин: {lang_status}", callback_data="set_language")],
        [InlineKeyboardButton(text=f"Авто-сповіщення: {notifications_status}", callback_data="toggle_notifications")],
        [InlineKeyboardButton(text=f"Дайджест новин: {digest_freq.capitalize()}", callback_data="set_digest_frequency")],
        [InlineKeyboardButton(text=f"Безпечний режим: {safe_mode_status}", callback_data="toggle_safe_mode")],
        [InlineKeyboardButton(text=f"Режим перегляду: {view_mode_status}", callback_data="set_view_mode")],
        [InlineKeyboardButton(text="⬅️ Назад до меню", callback_data="menu")]
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    await callback.message.edit_text("Оберіть налаштування:", reply_markup=markup)
    await callback.answer()

# Обробник кнопки "Мова новин"
@router.callback_query(F.data == "set_language")
async def set_language_callback_handler(callback: CallbackQuery, state: FSMContext):
    kb = [
        [InlineKeyboardButton(text="🇺🇦 Українська", callback_data="set_lang_uk")],
        [InlineKeyboardButton(text="🇬🇧 English", callback_data="set_lang_en")],
        [InlineKeyboardButton(text="⬅️ Назад до налаштувань", callback_data="settings")]
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    await callback.message.edit_text("Оберіть мову для перекладу новин:", reply_markup=markup)
    await state.set_state(UserSettings.choosing_language)
    await callback.answer()

# Обробник вибору мови
@router.callback_query(F.data.startswith("set_lang_"), UserSettings.choosing_language)
async def process_language_choice(callback: CallbackQuery, state: FSMContext):
    lang_code = callback.data.split("_")[2]
    await update_user_field(callback.from_user.id, 'preferred_language', lang_code)
    await callback.answer(f"Мову встановлено на {lang_code.upper()}", show_alert=True)
    await state.clear()
    await settings_callback_handler(callback, state) # Повертаємося до налаштувань

# Обробник кнопки "Авто-сповіщення"
@router.callback_query(F.data == "toggle_notifications")
async def toggle_notifications_callback_handler(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_telegram_id(callback.from_user.id)
    new_status = not user.get('auto_notifications', False)
    await update_user_field(callback.from_user.id, 'auto_notifications', new_status)
    status_text = "увімкнено" if new_status else "вимкнено"
    await callback.answer(f"Авто-сповіщення {status_text}.", show_alert=True)
    await settings_callback_handler(callback, state)

# Обробник кнопки "Дайджест новин"
@router.callback_query(F.data == "set_digest_frequency")
async def set_digest_frequency_callback_handler(callback: CallbackQuery, state: FSMContext):
    kb = [
        [InlineKeyboardButton(text="Щоденно", callback_data="set_digest_daily")],
        [InlineKeyboardButton(text="Щотижнево", callback_data="set_digest_weekly")],
        [InlineKeyboardButton(text="Вимкнути", callback_data="set_digest_off")],
        [InlineKeyboardButton(text="⬅️ Назад до налаштувань", callback_data="settings")]
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    await callback.message.edit_text("Оберіть частоту дайджесту:", reply_markup=markup)
    await state.set_state(UserSettings.choosing_digest_frequency)
    await callback.answer()

# Обробник вибору частоти дайджесту
@router.callback_query(F.data.startswith("set_digest_"), UserSettings.choosing_digest_frequency)
async def process_digest_frequency_choice(callback: CallbackQuery, state: FSMContext):
    freq_code = callback.data.split("_")[2]
    await update_user_field(callback.from_user.id, 'digest_frequency', freq_code)
    await callback.answer(f"Частоту дайджесту встановлено на {freq_code}.", show_alert=True)
    await state.clear()
    await settings_callback_handler(callback, state)

# Обробник кнопки "Безпечний режим"
@router.callback_query(F.data == "toggle_safe_mode")
async def toggle_safe_mode_callback_handler(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_telegram_id(callback.from_user.id)
    new_status = not user.get('safe_mode', False)
    await update_user_field(callback.from_user.id, 'safe_mode', new_status)
    status_text = "увімкнено" if new_status else "вимкнено"
    await callback.answer(f"Безпечний режим {status_text}.", show_alert=True)
    await settings_callback_handler(callback, state)

# Обробник кнопки "Режим перегляду"
@router.callback_query(F.data == "set_view_mode")
async def set_view_mode_callback_handler(callback: CallbackQuery, state: FSMContext):
    kb = [
        [InlineKeyboardButton(text="Детальний", callback_data="set_view_detailed")],
        [InlineKeyboardButton(text="Короткий", callback_data="set_view_short")],
        [InlineKeyboardButton(text="⬅️ Назад до налаштувань", callback_data="settings")]
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    await callback.message.edit_text("Оберіть режим перегляду новин:", reply_markup=markup)
    await state.set_state(UserSettings.choosing_view_mode)
    await callback.answer()

# Обробник вибору режиму перегляду
@router.callback_query(F.data.startswith("set_view_"), UserSettings.choosing_view_mode)
async def process_view_mode_choice(callback: CallbackQuery, state: FSMContext):
    view_mode = callback.data.split("_")[2]
    await update_user_field(callback.from_user.id, 'view_mode', view_mode)
    await callback.answer(f"Режим перегляду встановлено на {view_mode}.", show_alert=True)
    await state.clear()
    await settings_callback_handler(callback, state)

# Обробник кнопки "Моя стрічка"
@router.callback_query(F.data == "my_feed")
async def my_feed_callback_handler(callback: CallbackQuery, page: int = 0):
    user_id = callback.from_user.id
    user = await get_user_by_telegram_id(user_id)
    if not user:
        await callback.answer("Користувача не знайдено.", show_alert=True)
        return

    subscribed_sources = await get_user_subscriptions(user['id'])
    if not subscribed_sources:
        kb = [[InlineKeyboardButton(text="Підписатися на джерела", callback_data="manage_subscriptions")],
              [InlineKeyboardButton(text="⬅️ Назад до меню", callback_data="menu")]]
        markup = InlineKeyboardMarkup(inline_keyboard=kb)
        await callback.message.edit_text("Ви ще не підписані на жодне джерело. Будь ласка, підпишіться на джерела, щоб бачити новини у своїй стрічці.", reply_markup=markup)
        await callback.answer()
        return

    source_ids = [s['source_id'] for s in subscribed_sources]
    all_news = await get_last_n_news(source_ids=source_ids, limit=100) # Отримуємо більше новин для пагінації

    if not all_news:
        kb = [[InlineKeyboardButton(text="Підписатися на джерела", callback_data="manage_subscriptions")],
              [InlineKeyboardButton(text="⬅️ Назад до меню", callback_data="menu")]]
        markup = InlineKeyboardMarkup(inline_keyboard=kb)
        await callback.message.edit_text("У вашій стрічці поки немає новин з підписаних джерел.", reply_markup=markup)
        await callback.answer()
        return

    # Пагінація
    news_per_page = 5
    total_pages = (len(all_news) + news_per_page - 1) // news_per_page
    start_index = page * news_per_page
    end_index = start_index + news_per_page
    current_news_page = all_news[start_index:end_index]

    if not current_news_page:
        if page > 0: # Якщо ми намагалися перейти на порожню сторінку вперед
            await callback.answer("Це остання сторінка новин.", show_alert=True)
            await my_feed_callback_handler(callback, page=page-1) # Повертаємося на попередню сторінку
        else: # Якщо стрічка порожня
            kb = [[InlineKeyboardButton(text="Підписатися на джерела", callback_data="manage_subscriptions")],
                  [InlineKeyboardButton(text="⬅️ Назад до меню", callback_data="menu")]]
            markup = InlineKeyboardMarkup(inline_keyboard=kb)
            await callback.message.edit_text("У вашій стрічці поки немає новин з підписаних джерел.", reply_markup=markup)
            await callback.answer()
        return

    news_text = ""
    for news_item in current_news_page:
        title = news_item['title']
        source_name = (await get_source_by_id(news_item['source_id']))['name']
        published_at_utc = news_item['published_at']
        # Конвертуємо UTC в локальний час користувача, якщо потрібно
        # Наразі припустимо, що всі дати зберігаються в UTC і відображаються як UTC
        published_at_str = published_at_utc.strftime("%d.%m.%Y %H:%M") if published_at_utc else "Невідомо"
        news_url = news_item['source_url']
        news_id = news_item['id']

        # Перевіряємо, чи новина вже переглянута користувачем
        views = await get_user_news_views(user['id'], news_id)
        viewed_status = "👁️" if views else "" # Якщо є записи про перегляд, позначаємо як переглянуту

        # Перевіряємо, чи новина в закладках
        is_bookmarked = await get_user_bookmarks(user['id'], news_id)
        bookmark_status = "🔖" if is_bookmarked else "🗃️"

        # Визначаємо реакції
        reactions = await get_user_news_reactions(user['id'], news_id)
        like_status = "👍" if reactions and reactions.get('reaction_type') == 'like' else "🤍"
        dislike_status = "👎" if reactions and reactions.get('reaction_type') == 'dislike' else "🖤"

        if user.get('view_mode') == 'detailed':
            news_text += (
                f"<b>{title}</b>\n"
                f"Джерело: {source_name}\n"
                f"Опубліковано: {published_at_str} UTC\n"
                f"{viewed_status} {hlink('Читати далі', news_url)}\n"
                f"Реакції: {like_status} {dislike_status} | {bookmark_status} | <a href='{WEB_APP_URL}/news/{news_id}'>Детальніше</a>\n\n"
            )
        else: # Короткий режим
            news_text += (
                f"<b>{title}</b>\n"
                f"{source_name} | {published_at_str} UTC | {viewed_status} {hlink('Читати', news_url)}\n"
                f"Реакції: {like_status} {dislike_status} | {bookmark_status} | /news_{news_id}\n\n"
            )

        # Додаємо новину до переглянутих
        await add_user_news_view(user['id'], news_id)

    # Кнопки пагінації
    pagination_buttons = []
    if page > 0:
        pagination_buttons.append(InlineKeyboardButton(text="⬅️ Попередня", callback_data=f"my_feed_page_{page-1}"))
    if page < total_pages - 1:
        pagination_buttons.append(InlineKeyboardButton(text="Наступна ➡️", callback_data=f"my_feed_page_{page+1}"))

    kb = [
        pagination_buttons,
        [InlineKeyboardButton(text="➕ Керувати підписками", callback_data="manage_subscriptions")],
        [InlineKeyboardButton(text="🔄 Оновити стрічку", callback_data="my_feed_page_0")],
        [InlineKeyboardButton(text="⬅️ Назад до меню", callback_data="menu")]
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=kb)

    await callback.message.edit_text(news_text, reply_markup=markup, disable_web_page_preview=True)
    await callback.answer()

# Обробник пагінації "Моя стрічка"
@router.callback_query(F.data.startswith("my_feed_page_"))
async def my_feed_pagination_handler(callback: CallbackQuery):
    page = int(callback.data.split("_")[3])
    await my_feed_callback_handler(callback, page=page)

# Обробник кнопки "Керувати підписками"
@router.callback_query(F.data == "manage_subscriptions")
async def manage_subscriptions_callback_handler(callback: CallbackQuery, page: int = 0):
    user_id = callback.from_user.id
    all_sources = await get_all_active_sources()
    user_subscriptions = await get_user_subscriptions(user_id)
    subscribed_source_ids = {s['source_id'] for s in user_subscriptions}

    if not all_sources:
        kb = [[InlineKeyboardButton(text="⬅️ Назад до стрічки", callback_data="my_feed_page_0")],
              [InlineKeyboardButton(text="⬅️ Назад до меню", callback_data="menu")]]
        markup = InlineKeyboardMarkup(inline_keyboard=kb)
        await callback.message.edit_text("Наразі немає доступних джерел для підписки.", reply_markup=markup)
        await callback.answer()
        return

    # Пагінація джерел
    sources_per_page = 5
    total_pages = (len(all_sources) + sources_per_page - 1) // sources_per_page
    start_index = page * sources_per_page
    end_index = start_index + sources_per_page
    current_sources_page = all_sources[start_index:end_index]

    kb = []
    for source in current_sources_page:
        status = "✅ Підписано" if source['id'] in subscribed_source_ids else "➕ Підписатися"
        callback_data = f"toggle_sub_{source['id']}"
        kb.append([InlineKeyboardButton(text=f"{source['name']} ({source['category']}) - {status}", callback_data=callback_data)])

    pagination_buttons = []
    if page > 0:
        pagination_buttons.append(InlineKeyboardButton(text="⬅️ Попередня", callback_data=f"manage_subscriptions_page_{page-1}"))
    if page < total_pages - 1:
        pagination_buttons.append(InlineKeyboardButton(text="Наступна ➡️", callback_data=f"manage_subscriptions_page_{page+1}"))

    kb.append(pagination_buttons)
    kb.append([InlineKeyboardButton(text="⬅️ Назад до стрічки", callback_data="my_feed_page_0")])
    kb.append([InlineKeyboardButton(text="⬅️ Назад до меню", callback_data="menu")])

    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    await callback.message.edit_text("Керування підписками на джерела:", reply_markup=markup)
    await callback.answer()

# Обробник пагінації "Керувати підписками"
@router.callback_query(F.data.startswith("manage_subscriptions_page_"))
async def manage_subscriptions_pagination_handler(callback: CallbackQuery):
    page = int(callback.data.split("_")[3])
    await manage_subscriptions_callback_handler(callback, page=page)

# Обробник перемикання підписки
@router.callback_query(F.data.startswith("toggle_sub_"))
async def toggle_subscription_callback_handler(callback: CallbackQuery, state: FSMContext):
    user_id = (await get_user_by_telegram_id(callback.from_user.id))['id']
    source_id = int(callback.data.split("_")[2])

    is_subscribed = await get_user_subscriptions(user_id, source_id)

    if is_subscribed:
        await delete_user_subscription(user_id, source_id)
        await callback.answer("Ви відписалися від джерела.", show_alert=True)
    else:
        await add_user_subscription(user_id, source_id)
        await callback.answer("Ви підписалися на джерело.", show_alert=True)

    # Оновлюємо список підписок
    await manage_subscriptions_callback_handler(callback)

# Обробник кнопки "Закладки"
@router.callback_query(F.data == "bookmarks")
async def bookmarks_callback_handler(callback: CallbackQuery, page: int = 0):
    user_id = (await get_user_by_telegram_id(callback.from_user.id))['id']
    user_bookmarks = await get_user_bookmarks(user_id)

    if not user_bookmarks:
        kb = [[InlineKeyboardButton(text="⬅️ Назад до меню", callback_data="menu")]]
        markup = InlineKeyboardMarkup(inline_keyboard=kb)
        await callback.message.edit_text("У вас немає закладок.", reply_markup=markup)
        await callback.answer()
        return

    # Пагінація
    news_per_page = 5
    total_pages = (len(user_bookmarks) + news_per_page - 1) // news_per_page
    start_index = page * news_per_page
    end_index = start_index + news_per_page
    current_bookmarks_page = user_bookmarks[start_index:end_index]

    bookmarks_text = "Ваші закладки:\n\n"
    for bookmark in current_bookmarks_page:
        news_item = await get_news_item_by_id(bookmark['news_id'])
        if news_item:
            title = news_item['title']
            source_name = (await get_source_by_id(news_item['source_id']))['name']
            published_at_utc = news_item['published_at']
            published_at_str = published_at_utc.strftime("%d.%m.%Y %H:%M") if published_at_utc else "Невідомо"
            news_url = news_item['source_url']
            news_id = news_item['id']

            bookmarks_text += (
                f"<b>{title}</b>\n"
                f"Джерело: {source_name}\n"
                f"Опубліковано: {published_at_str} UTC\n"
                f"{hlink('Читати далі', news_url)}\n"
                f"Видалити: /del_bookmark_{news_id}\n\n"
            )

    pagination_buttons = []
    if page > 0:
        pagination_buttons.append(InlineKeyboardButton(text="⬅️ Попередня", callback_data=f"bookmarks_page_{page-1}"))
    if page < total_pages - 1:
        pagination_buttons.append(InlineKeyboardButton(text="Наступна ➡️", callback_data=f"bookmarks_page_{page+1}"))

    kb = [
        pagination_buttons,
        [InlineKeyboardButton(text="⬅️ Назад до меню", callback_data="menu")]
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=kb)

    await callback.message.edit_text(bookmarks_text, reply_markup=markup, disable_web_page_preview=True)
    await callback.answer()

# Обробник пагінації "Закладки"
@router.callback_query(F.data.startswith("bookmarks_page_"))
async def bookmarks_pagination_handler(callback: CallbackQuery):
    page = int(callback.data.split("_")[2])
    await bookmarks_callback_handler(callback, page=page)

# Обробник команди для видалення закладки
@router.message(Command(re.compile(r"del_bookmark_(\d+)")))
async def delete_bookmark_command_handler(message: Message):
    news_id = int(message.text.split("_")[2])
    user_id = (await get_user_by_telegram_id(message.from_user.id))['id']

    if await delete_bookmark(user_id, news_id):
        await message.answer("Закладку видалено.")
    else:
        await message.answer("Ця новина не була у ваших закладках.")

    # Оновлюємо список закладок після видалення
    await bookmarks_callback_handler(message)


# Обробник кнопки "Пошук новин"
@router.callback_query(F.data == "search_news")
async def search_news_callback_handler(callback: CallbackQuery):
    await callback.message.edit_text("Наразі функція пошуку новин в розробці. Будь ласка, спробуйте пізніше.",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Назад до меню", callback_data="menu")]
                                     ]))
    await callback.answer()

# Обробник кнопки "AI Асистент"
@router.callback_query(F.data == "ai_assistant")
async def ai_assistant_callback_handler(callback: CallbackQuery):
    await callback.message.edit_text("Наразі AI Асистент в розробці. Будь ласка, спробуйте пізніше.",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Назад до меню", callback_data="menu")]
                                     ]))
    await callback.answer()

# Обробник кнопки "Преміум"
@router.callback_query(F.data == "premium")
async def premium_callback_handler(callback: CallbackQuery):
    await callback.message.edit_text("Інформація про Преміум доступна незабаром. Слідкуйте за оновленнями!",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Назад до меню", callback_data="menu")]
                                     ]))
    await callback.answer()

# Обробник кнопки "Про бота"
@router.callback_query(F.data == "about_bot")
async def about_bot_callback_handler(callback: CallbackQuery):
    about_text = (
        "Цей бот створений для зручного отримання новин з різних джерел.\n"
        "Розробник: [Ваше ім'я або нікнейм]\n"
        "Версія: 1.0\n"
        "Зворотний зв'язок: @your_support_handle (замініть на реальний)\n\n"
        "Дякуємо за використання!"
    )
    await callback.message.edit_text(about_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад до меню", callback_data="menu")]
    ]))
    await callback.answer()

# Обробник кнопки "Адмін-панель"
@router.callback_query(F.data == "admin_panel", is_admin_check)
async def admin_panel_callback_handler(callback: CallbackQuery, state: FSMContext):
    kb = [
        [InlineKeyboardButton(text="➕ Додати джерело", callback_data="admin_add_source")],
        [InlineKeyboardButton(text="✏️ Редагувати джерело", callback_data="admin_edit_source")],
        [InlineKeyboardButton(text="🗑️ Видалити джерело", callback_data="admin_delete_source")],
        [InlineKeyboardButton(text="👥 Керувати користувачами", callback_data="admin_manage_users")],
        [InlineKeyboardButton(text="⚙️ Налаштування бота", callback_data="admin_edit_bot_settings")],
        [InlineKeyboardButton(text="📈 Статистика джерел", callback_data="admin_source_stats")],
        [InlineKeyboardButton(text="✉️ Надіслати повідомлення", callback_data="admin_send_message")],
        [InlineKeyboardButton(text="⬅️ Назад до меню", callback_data="menu")]
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    await callback.message.edit_text("Адмін-панель:", reply_markup=markup)
    await state.set_state(UserSettings.admin_panel)
    await callback.answer()

# Обробник кнопки "Статистика джерел" (Адмін)
@router.callback_query(F.data == "admin_source_stats", is_admin_check)
async def admin_source_stats_callback_handler(callback: CallbackQuery):
    sources = await get_all_sources()
    if not sources:
        await callback.message.edit_text("Немає джерел для відображення статистики.",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                             [InlineKeyboardButton(text="⬅️ Назад до адмін-панелі", callback_data="admin_panel")]
                                         ]))
        await callback.answer()
        return

    stats_text = "📊 Статистика джерел:\n\n"
    for source in sources:
        source_stats = await get_source_stats(source['id'])
        news_count = source_stats.get('news_count', 0) if source_stats else 0
        last_parsed = source.get('last_parsed_at')
        last_parsed_str = last_parsed.strftime("%d.%m.%Y %H:%M") if last_parsed else "Ніколи"
        stats_text += (
            f"<b>{source['name']}</b> (ID: {source['id']})\n"
            f"Статус: {source['status']}\n"
            f"Категорія: {source['category']}\n"
            f"Новин: {news_count}\n"
            f"Останній парсинг: {last_parsed_str}\n\n"
        )

    kb = [[InlineKeyboardButton(text="⬅️ Назад до адмін-панелі", callback_data="admin_panel")]]
    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    await callback.message.edit_text(stats_text, reply_markup=markup, disable_web_page_preview=True)
    await callback.answer()

# Обробник кнопки "Надіслати повідомлення" (Адмін)
@router.callback_query(F.data == "admin_send_message", is_admin_check)
async def admin_send_message_callback_handler(callback: CallbackQuery, state: FSMContext):
    kb = [
        [InlineKeyboardButton(text="Всі користувачі", callback_data="send_message_all")],
        [InlineKeyboardButton(text="Користувачі з дайджестом", callback_data="send_message_digest_enabled")],
        [InlineKeyboardButton(text="Користувачі з авто-сповіщеннями", callback_data="send_message_auto_notifications_enabled")],
        [InlineKeyboardButton(text="Користувачі з преміум", callback_data="send_message_premium")],
        [InlineKeyboardButton(text="Користувачі з PRO", callback_data="send_message_pro")],
        [InlineKeyboardButton(text="Користувачі за мовою", callback_data="send_message_language")],
        [InlineKeyboardButton(text="Конкретний користувач (ID)", callback_data="send_message_user")],
        [InlineKeyboardButton(text="⬅️ Назад до адмін-панелі", callback_data="admin_panel")]
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    await callback.message.edit_text("Оберіть цільову аудиторію для повідомлення:", reply_markup=markup)
    await state.set_state(UserSettings.admin_select_message_target)
    await callback.answer()

# Обробник вибору цільової аудиторії для повідомлення
@router.callback_query(F.data.startswith("send_message_"), UserSettings.admin_select_message_target, is_admin_check)
async def process_send_message_target(callback: CallbackQuery, state: FSMContext):
    target_type = callback.data.split("_")[2]
    await state.update_data(message_target_type=target_type)

    if target_type == "language":
        kb = [
            [InlineKeyboardButton(text="Українська", callback_data="send_message_lang_uk")],
            [InlineKeyboardButton(text="English", callback_data="send_message_lang_en")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_send_message")]
        ]
        markup = InlineKeyboardMarkup(inline_keyboard=kb)
        await callback.message.edit_text("Оберіть мову для фільтрації користувачів:", reply_markup=markup)
        await state.set_state(UserSettings.admin_select_message_language_code)
    elif target_type == "user":
        await callback.message.edit_text("Будь ласка, введіть Telegram ID користувача:",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                             [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_send_message")]
                                         ]))
        await state.set_state(UserSettings.admin_select_message_user)
    else:
        await callback.message.edit_text("Будь ласка, введіть текст повідомлення:",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                             [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_send_message")]
                                         ]))
        await state.set_state(UserSettings.admin_enter_message_text)
    await callback.answer()

# Обробник вибору мови для повідомлення
@router.callback_query(F.data.startswith("send_message_lang_"), UserSettings.admin_select_message_language_code, is_admin_check)
async def process_send_message_language_code(callback: CallbackQuery, state: FSMContext):
    lang_code = callback.data.split("_")[3]
    await state.update_data(message_target_language=lang_code)
    await callback.message.edit_text(f"Ви обрали мову: {lang_code.upper()}.\nБудь ласка, введіть текст повідомлення:",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Назад", callback_data="send_message_language")]
                                     ]))
    await state.set_state(UserSettings.admin_enter_message_text)
    await callback.answer()

# Обробник введення ID користувача для повідомлення
@router.message(UserSettings.admin_select_message_user, is_admin_check)
async def process_send_message_user_id(message: Message, state: FSMContext):
    try:
        user_telegram_id = int(message.text)
        user = await get_user_by_telegram_id(user_telegram_id)
        if user:
            await state.update_data(message_target_user_id=user_telegram_id)
            await message.answer(f"Ви обрали користувача з ID: {user_telegram_id}.\nБудь ласка, введіть текст повідомлення:",
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                     [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_send_message")]
                                 ]))
            await state.set_state(UserSettings.admin_enter_message_text)
        else:
            await message.answer("Користувача з таким Telegram ID не знайдено. Спробуйте ще раз або скасуйте.",
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                     [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_send_message")]
                                 ]))
    except ValueError:
        await message.answer("Будь ласка, введіть дійсний числовий Telegram ID.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_send_message")]
                             ]))

# Обробник введення тексту повідомлення
@router.message(UserSettings.admin_enter_message_text, is_admin_check)
async def process_admin_enter_message_text(message: Message, state: FSMContext):
    message_text = message.text
    await state.update_data(message_text=message_text)

    data = await state.get_data()
    target_type = data.get('message_target_type')
    target_lang = data.get('message_target_language')
    target_user_id = data.get('message_target_user_id')

    confirm_text = f"Ви збираєтеся надіслати наступне повідомлення:\n\n<b>{message_text}</b>\n\n"
    if target_type == "all":
        confirm_text += "Цільова аудиторія: Всі користувачі."
    elif target_type == "digest_enabled":
        confirm_text += "Цільова аудиторія: Користувачі з увімкненим дайджестом."
    elif target_type == "auto_notifications_enabled":
        confirm_text += "Цільова аудиторія: Користувачі з увімкненими авто-сповіщеннями."
    elif target_type == "premium":
        confirm_text += "Цільова аудиторія: Преміум користувачі."
    elif target_type == "pro":
        confirm_text += "Цільова аудиторія: PRO користувачі."
    elif target_type == "language" and target_lang:
        confirm_text += f"Цільова аудиторія: Користувачі з мовою {target_lang.upper()}."
    elif target_type == "user" and target_user_id:
        confirm_text += f"Цільова аудиторія: Користувач з ID {target_user_id}."

    kb = [
        [InlineKeyboardButton(text="✅ Підтвердити та надіслати", callback_data="confirm_send_message")],
        [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    await message.answer(confirm_text, reply_markup=markup, parse_mode=ParseMode.HTML)
    await state.set_state(UserSettings.admin_confirm_send_message)

# Обробник підтвердження надсилання повідомлення
@router.callback_query(F.data == "confirm_send_message", UserSettings.admin_confirm_send_message, is_admin_check)
async def process_confirm_send_message(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    message_text = data.get('message_text')
    target_type = data.get('message_target_type')
    target_lang = data.get('message_target_language')
    target_user_id = data.get('message_target_user_id')

    users_to_send = []
    if target_type == "all":
        users_to_send = await get_all_users()
    elif target_type == "digest_enabled":
        all_users = await get_all_users()
        users_to_send = [u for u in all_users if u.get('digest_frequency') != 'off']
    elif target_type == "auto_notifications_enabled":
        all_users = await get_all_users()
        users_to_send = [u for u in all_users if u.get('auto_notifications')]
    elif target_type == "premium":
        all_users = await get_all_users()
        users_to_send = [u for u in all_users if u.get('is_premium')]
    elif target_type == "pro":
        all_users = await get_all_users()
        users_to_send = [u for u in all_users if u.get('is_pro')]
    elif target_type == "language" and target_lang:
        all_users = await get_all_users()
        users_to_send = [u for u in all_users if u.get('preferred_language') == target_lang]
    elif target_type == "user" and target_user_id:
        user = await get_user_by_telegram_id(target_user_id)
        if user:
            users_to_send = [user]

    sent_count = 0
    failed_count = 0
    for user in users_to_send:
        try:
            await bot.send_message(chat_id=user['telegram_id'], text=message_text, parse_mode=ParseMode.HTML)
            sent_count += 1
            await asyncio.sleep(0.05) # Невелика затримка, щоб уникнути Rate Limit
        except Exception as e:
            logger.error(f"Не вдалося надіслати повідомлення користувачу {user['telegram_id']}: {e}")
            failed_count += 1

    await callback.message.edit_text(f"Повідомлення надіслано.\nУспішно: {sent_count}\nНевдало: {failed_count}",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Назад до адмін-панелі", callback_data="admin_panel")]
                                     ]))
    await state.clear()
    await callback.answer()

# Обробник кнопки "Додати джерело" (Адмін)
@router.callback_query(F.data == "admin_add_source", is_admin_check)
async def admin_add_source_callback_handler(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введіть URL нового джерела:",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                                     ]))
    await state.set_state(SourceManagement.waiting_for_url)
    await callback.answer()

@router.message(SourceManagement.waiting_for_url, is_admin_check)
async def process_source_url(message: Message, state: FSMContext):
    url = message.text
    # Перевірка на валідність URL
    parsed_url = urlparse(url)
    if not all([parsed_url.scheme, parsed_url.netloc]):
        await message.answer("Будь ласка, введіть дійсний URL (наприклад, https://example.com).",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                             ]))
        return

    # Перевірка на дублювання
    existing_source = await get_source_by_url(url)
    if existing_source:
        await message.answer("Джерело з таким URL вже існує в базі даних.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                             ]))
        await state.clear()
        return

    await state.update_data(new_source_url=url)
    await message.answer("Введіть назву джерела (наприклад, 'Європейська Правда'):",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                         ]))
    await state.set_state(SourceManagement.waiting_for_name)

@router.message(SourceManagement.waiting_for_name, is_admin_check)
async def process_source_name(message: Message, state: FSMContext):
    name = message.text
    await state.update_data(new_source_name=name)
    await message.answer("Введіть категорію джерела (наприклад, 'Політика', 'Економіка', 'Спорт'):",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                         ]))
    await state.set_state(SourceManagement.waiting_for_category)

@router.message(SourceManagement.waiting_for_category, is_admin_check)
async def process_source_category(message: Message, state: FSMContext):
    category = message.text
    await state.update_data(new_source_category=category)
    await message.answer("Введіть мову джерела (наприклад, 'uk', 'en'):",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                         ]))
    await state.set_state(SourceManagement.waiting_for_language)

@router.message(SourceManagement.waiting_for_language, is_admin_check)
async def process_source_language(message: Message, state: FSMContext):
    language = message.text
    if language not in ['uk', 'en']: # Додайте інші мови, якщо потрібно
        await message.answer("Будь ласка, введіть 'uk' або 'en'.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                             ]))
        return

    await state.update_data(new_source_language=language)
    await message.answer("Введіть статус джерела ('active' або 'inactive'):",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                         ]))
    await state.set_state(SourceManagement.waiting_for_status)

@router.message(SourceManagement.waiting_for_status, is_admin_check)
async def process_source_status(message: Message, state: FSMContext):
    status = message.text.lower()
    if status not in ['active', 'inactive']:
        await message.answer("Будь ласка, введіть 'active' або 'inactive'.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                             ]))
        return

    await state.update_data(new_source_status=status)
    await message.answer("Введіть інтервал парсингу в хвилинах (наприклад, '60' для щогодини):",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                         ]))
    await state.set_state(SourceManagement.waiting_for_parse_interval)

@router.message(SourceManagement.waiting_for_parse_interval, is_admin_check)
async def process_source_parse_interval(message: Message, state: FSMContext):
    try:
        parse_interval = int(message.text)
        if parse_interval <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Будь ласка, введіть дійсне число більше нуля.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                             ]))
        return

    data = await state.get_data()
    new_source = {
        "url": data['new_source_url'],
        "name": data['new_source_name'],
        "category": data['new_source_category'],
        "language": data['new_source_language'],
        "status": data['new_source_status'],
        "parse_interval_minutes": parse_interval
    }

    source_id = await add_source(new_source)
    await message.answer(f"Джерело '{new_source['name']}' (ID: {source_id}) успішно додано.",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="⬅️ Назад до адмін-панелі", callback_data="admin_panel")]
                         ]))
    await state.clear()

# Обробник кнопки "Редагувати джерело" (Адмін)
@router.callback_query(F.data == "admin_edit_source", is_admin_check)
async def admin_edit_source_callback_handler(callback: CallbackQuery, state: FSMContext):
    sources = await get_all_sources()
    if not sources:
        await callback.message.edit_text("Немає джерел для редагування.",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                             [InlineKeyboardButton(text="⬅️ Назад до адмін-панелі", callback_data="admin_panel")]
                                         ]))
        await callback.answer()
        return

    sources_list_text = "Оберіть джерело для редагування (введіть ID):\n\n"
    for source in sources:
        sources_list_text += f"ID: {source['id']}, Назва: {source['name']}, Статус: {source['status']}\n"

    await callback.message.edit_text(sources_list_text,
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                                     ]))
    await state.set_state(SourceManagement.waiting_for_edit_id)
    await callback.answer()

@router.message(SourceManagement.waiting_for_edit_id, is_admin_check)
async def process_edit_source_id(message: Message, state: FSMContext):
    try:
        source_id = int(message.text)
        source = await get_source_by_id(source_id)
        if not source:
            await message.answer("Джерело з таким ID не знайдено. Спробуйте ще раз.",
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                     [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                                 ]))
            return
        await state.update_data(edit_source_id=source_id)
        kb = [
            [InlineKeyboardButton(text="Змінити URL", callback_data="edit_source_url")],
            [InlineKeyboardButton(text="Змінити назву", callback_data="edit_source_name")],
            [InlineKeyboardButton(text="Змінити категорію", callback_data="edit_source_category")],
            [InlineKeyboardButton(text="Змінити мову", callback_data="edit_source_language")],
            [InlineKeyboardButton(text="Змінити статус", callback_data="edit_source_status")],
            [InlineKeyboardButton(text="Змінити інтервал парсингу", callback_data="edit_source_parse_interval")],
            [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
        ]
        markup = InlineKeyboardMarkup(inline_keyboard=kb)
        await message.answer(f"Оберіть, що ви хочете редагувати для джерела ID {source_id} ({source['name']}):",
                             reply_markup=markup)
        await state.set_state(SourceManagement.waiting_for_edit_field)
    except ValueError:
        await message.answer("Будь ласка, введіть дійсний числовий ID джерела.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                             ]))

@router.callback_query(F.data.startswith("edit_source_"), SourceManagement.waiting_for_edit_field, is_admin_check)
async def process_edit_source_field(callback: CallbackQuery, state: FSMContext):
    field = callback.data.split("_")[2]
    await state.update_data(edit_source_field=field)
    await callback.message.edit_text(f"Введіть нове значення для '{field}':",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                                     ]))
    await state.set_state(SourceManagement.waiting_for_new_value)
    await callback.answer()

@router.message(SourceManagement.waiting_for_new_value, is_admin_check)
async def process_new_source_value(message: Message, state: FSMContext):
    data = await state.get_data()
    source_id = data['edit_source_id']
    field = data['edit_source_field']
    new_value = message.text

    # Додаткова валідація для певних полів
    if field == 'url':
        parsed_url = urlparse(new_value)
        if not all([parsed_url.scheme, parsed_url.netloc]):
            await message.answer("Будь ласка, введіть дійсний URL.",
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                     [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                                 ]))
            return
    elif field == 'language' and new_value not in ['uk', 'en']:
        await message.answer("Будь ласка, введіть 'uk' або 'en'.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                             ]))
        return
    elif field == 'status' and new_value not in ['active', 'inactive']:
        await message.answer("Будь ласка, введіть 'active' або 'inactive'.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                             ]))
        return
    elif field == 'parse_interval' :
        try:
            new_value = int(new_value)
            if new_value <= 0:
                raise ValueError
        except ValueError:
            await message.answer("Будь ласка, введіть дійсне число більше нуля.",
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                     [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                                 ]))
            return

    await update_source_status(source_id, {field: new_value}) # Використовуємо update_source_status для оновлення будь-якого поля
    await message.answer(f"Поле '{field}' для джерела ID {source_id} оновлено.",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="⬅️ Назад до адмін-панелі", callback_data="admin_panel")]
                         ]))
    await state.clear()

# Обробник кнопки "Видалити джерело" (Адмін)
@router.callback_query(F.data == "admin_delete_source", is_admin_check)
async def admin_delete_source_callback_handler(callback: CallbackQuery, state: FSMContext):
    sources = await get_all_sources()
    if not sources:
        await callback.message.edit_text("Немає джерел для видалення.",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                             [InlineKeyboardButton(text="⬅️ Назад до адмін-панелі", callback_data="admin_panel")]
                                         ]))
        await callback.answer()
        return

    sources_list_text = "Оберіть джерело для видалення (введіть ID):\n\n"
    for source in sources:
        sources_list_text += f"ID: {source['id']}, Назва: {source['name']}, Статус: {source['status']}\n"

    await callback.message.edit_text(sources_list_text,
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                                     ]))
    await state.set_state(SourceManagement.waiting_for_delete_id)
    await callback.answer()

@router.message(SourceManagement.waiting_for_delete_id, is_admin_check)
async def process_delete_source_id(message: Message, state: FSMContext):
    try:
        source_id = int(message.text)
        source = await get_source_by_id(source_id)
        if not source:
            await message.answer("Джерело з таким ID не знайдено. Спробуйте ще раз.",
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                     [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                                 ]))
            return

        await state.update_data(delete_source_id=source_id)
        kb = [
            [InlineKeyboardButton(text="✅ Так, видалити", callback_data="confirm_delete_source")],
            [InlineKeyboardButton(text="❌ Ні, скасувати", callback_data="admin_panel")]
        ]
        markup = InlineKeyboardMarkup(inline_keyboard=kb)
        await message.answer(f"Ви впевнені, що хочете видалити джерело ID {source_id} ({source['name']})? Це також видалить всі пов'язані новини та підписки.",
                             reply_markup=markup)
        await state.set_state(UserSettings.admin_confirm_delete_source)
    except ValueError:
        await message.answer("Будь ласка, введіть дійсний числовий ID джерела.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                             ]))

@router.callback_query(F.data == "confirm_delete_source", UserSettings.admin_confirm_delete_source, is_admin_check)
async def process_confirm_delete_source(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    source_id = data['delete_source_id']
    await delete_source(source_id) # Припускаємо, що у вас є функція delete_source в database.py
    await callback.message.edit_text(f"Джерело ID {source_id} успішно видалено.",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Назад до адмін-панелі", callback_data="admin_panel")]
                                     ]))
    await state.clear()
    await callback.answer()

# Обробник кнопки "Керувати користувачами" (Адмін)
@router.callback_query(F.data == "admin_manage_users", is_admin_check)
async def admin_manage_users_callback_handler(callback: CallbackQuery, page: int = 0):
    users = await get_all_users()
    if not users:
        await callback.message.edit_text("Немає користувачів для керування.",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                             [InlineKeyboardButton(text="⬅️ Назад до адмін-панелі", callback_data="admin_panel")]
                                         ]))
        await callback.answer()
        return

    users_per_page = 10
    total_pages = (len(users) + users_per_page - 1) // users_per_page
    start_index = page * users_per_page
    end_index = start_index + users_per_page
    current_users_page = users[start_index:end_index]

    users_list_text = "Оберіть користувача для керування (введіть Telegram ID):\n\n"
    for user in current_users_page:
        users_list_text += (
            f"ID: {user['telegram_id']}, Ім'я: {user['first_name']} {user.get('last_name', '')} "
            f"(Admin: {'✅' if user.get('is_admin') else '❌'})\n"
        )

    pagination_buttons = []
    if page > 0:
        pagination_buttons.append(InlineKeyboardButton(text="⬅️ Попередня", callback_data=f"admin_manage_users_page_{page-1}"))
    if page < total_pages - 1:
        pagination_buttons.append(InlineKeyboardButton(text="Наступна ➡️", callback_data=f"admin_manage_users_page_{page+1}"))

    kb = [
        pagination_buttons,
        [InlineKeyboardButton(text="⬅️ Назад до адмін-панелі", callback_data="admin_panel")]
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=kb)

    await callback.message.edit_text(users_list_text, reply_markup=markup)
    await state.set_state(UserSettings.admin_select_user_for_management)
    await callback.answer()

@router.callback_query(F.data.startswith("admin_manage_users_page_"), is_admin_check)
async def admin_manage_users_pagination_handler(callback: CallbackQuery, state: FSMContext):
    page = int(callback.data.split("_")[4])
    await admin_manage_users_callback_handler(callback, page=page)

@router.message(UserSettings.admin_select_user_for_management, is_admin_check)
async def process_admin_select_user_for_management(message: Message, state: FSMContext):
    try:
        user_telegram_id = int(message.text)
        user = await get_user_by_telegram_id(user_telegram_id)
        if not user:
            await message.answer("Користувача з таким Telegram ID не знайдено. Спробуйте ще раз.",
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                     [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                                 ]))
            return

        await state.update_data(manage_user_id=user_telegram_id)
        kb = [
            [InlineKeyboardButton(text="Змінити статус адміна", callback_data="admin_toggle_admin_status")],
            [InlineKeyboardButton(text="Змінити Преміум", callback_data="admin_edit_user_premium")],
            [InlineKeyboardButton(text="Змінити PRO", callback_data="admin_edit_user_pro")],
            [InlineKeyboardButton(text="Змінити дайджест", callback_data="admin_edit_user_digest")],
            [InlineKeyboardButton(text="Змінити AI запити", callback_data="admin_edit_user_ai_requests")],
            [InlineKeyboardButton(text="Змінити мову", callback_data="admin_edit_user_language")],
            [InlineKeyboardButton(text="Видалити користувача", callback_data="admin_delete_user")],
            [InlineKeyboardButton(text="⬅️ Назад до керування користувачами", callback_data="admin_manage_users")],
            [InlineKeyboardButton(text="⬅️ Назад до адмін-панелі", callback_data="admin_panel")]
        ]
        markup = InlineKeyboardMarkup(inline_keyboard=kb)
        await message.answer(f"Оберіть дію для користувача ID {user_telegram_id} ({user['first_name']}):",
                             reply_markup=markup)
        await state.set_state(UserSettings.admin_manage_users) # Повертаємося до загального стану керування користувачами
    except ValueError:
        await message.answer("Будь ласка, введіть дійсний числовий Telegram ID.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                             ]))

# Обробник перемикання статусу адміна
@router.callback_query(F.data == "admin_toggle_admin_status", UserSettings.admin_manage_users, is_admin_check)
async def admin_toggle_admin_status_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_telegram_id = data['manage_user_id']
    user = await get_user_by_telegram_id(user_telegram_id)
    new_status = not user.get('is_admin', False)
    await update_user_field(user_telegram_id, 'is_admin', new_status)
    status_text = "адміністратором" if new_status else "звичайним користувачем"
    await callback.answer(f"Користувач {user_telegram_id} тепер є {status_text}.", show_alert=True)
    await admin_manage_users_callback_handler(callback) # Повертаємося до списку користувачів

# Обробник кнопки "Змінити Преміум" (Адмін)
@router.callback_query(F.data == "admin_edit_user_premium", UserSettings.admin_manage_users, is_admin_check)
async def admin_edit_user_premium_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_telegram_id = data['manage_user_id']
    user = await get_user_by_telegram_id(user_telegram_id)
    current_status = "Увімкнено" if user.get('is_premium') else "Вимкнено"
    kb = [
        [InlineKeyboardButton(text="Увімкнути Преміум", callback_data="set_premium_true")],
        [InlineKeyboardButton(text="Вимкнути Преміум", callback_data="set_premium_false")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_manage_users")]
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    await callback.message.edit_text(f"Преміум статус для користувача {user_telegram_id}: {current_status}", reply_markup=markup)
    await state.set_state(UserSettings.admin_edit_user_premium)
    await callback.answer()

@router.callback_query(F.data.startswith("set_premium_"), UserSettings.admin_edit_user_premium, is_admin_check)
async def process_set_premium_status(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_telegram_id = data['manage_user_id']
    new_status = True if callback.data == "set_premium_true" else False
    await update_user_field(user_telegram_id, 'is_premium', new_status)
    status_text = "увімкнено" if new_status else "вимкнено"
    await callback.answer(f"Преміум статус для користувача {user_telegram_id} {status_text}.", show_alert=True)
    await admin_manage_users_callback_handler(callback) # Повертаємося до списку користувачів

# Обробник кнопки "Змінити PRO" (Адмін)
@router.callback_query(F.data == "admin_edit_user_pro", UserSettings.admin_manage_users, is_admin_check)
async def admin_edit_user_pro_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_telegram_id = data['manage_user_id']
    user = await get_user_by_telegram_id(user_telegram_id)
    current_status = "Увімкнено" if user.get('is_pro') else "Вимкнено"
    kb = [
        [InlineKeyboardButton(text="Увімкнути PRO", callback_data="set_pro_true")],
        [InlineKeyboardButton(text="Вимкнути PRO", callback_data="set_pro_false")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_manage_users")]
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    await callback.message.edit_text(f"PRO статус для користувача {user_telegram_id}: {current_status}", reply_markup=markup)
    await state.set_state(UserSettings.admin_edit_user_pro)
    await callback.answer()

@router.callback_query(F.data.startswith("set_pro_"), UserSettings.admin_edit_user_pro, is_admin_check)
async def process_set_pro_status(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_telegram_id = data['manage_user_id']
    new_status = True if callback.data == "set_pro_true" else False
    await update_user_field(user_telegram_id, 'is_pro', new_status)
    status_text = "увімкнено" if new_status else "вимкнено"
    await callback.answer(f"PRO статус для користувача {user_telegram_id} {status_text}.", show_alert=True)
    await admin_manage_users_callback_handler(callback) # Повертаємося до списку користувачів

# Обробник кнопки "Змінити дайджест" (Адмін)
@router.callback_query(F.data == "admin_edit_user_digest", UserSettings.admin_manage_users, is_admin_check)
async def admin_edit_user_digest_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_telegram_id = data['manage_user_id']
    user = await get_user_by_telegram_id(user_telegram_id)
    current_freq = user.get('digest_frequency', 'daily')
    kb = [
        [InlineKeyboardButton(text="Щоденно", callback_data="set_user_digest_daily")],
        [InlineKeyboardButton(text="Щотижнево", callback_data="set_user_digest_weekly")],
        [InlineKeyboardButton(text="Вимкнути", callback_data="set_user_digest_off")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_manage_users")]
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    await callback.message.edit_text(f"Частота дайджесту для користувача {user_telegram_id}: {current_freq.capitalize()}", reply_markup=markup)
    await state.set_state(UserSettings.admin_edit_user_digest)
    await callback.answer()

@router.callback_query(F.data.startswith("set_user_digest_"), UserSettings.admin_edit_user_digest, is_admin_check)
async def process_set_user_digest_frequency(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_telegram_id = data['manage_user_id']
    freq_code = callback.data.split("_")[3]
    await update_user_field(user_telegram_id, 'digest_frequency', freq_code)
    await callback.answer(f"Частоту дайджесту для користувача {user_telegram_id} встановлено на {freq_code}.", show_alert=True)
    await admin_manage_users_callback_handler(callback) # Повертаємося до списку користувачів

# Обробник кнопки "Змінити AI запити" (Адмін)
@router.callback_query(F.data == "admin_edit_user_ai_requests", UserSettings.admin_manage_users, is_admin_check)
async def admin_edit_user_ai_requests_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_telegram_id = data['manage_user_id']
    user = await get_user_by_telegram_id(user_telegram_id)
    current_requests = user.get('ai_requests_today', 0)
    await callback.message.edit_text(f"Введіть нову кількість AI запитів на сьогодні для користувача {user_telegram_id} (поточна: {current_requests}):",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_manage_users")]
                                     ]))
    await state.set_state(UserSettings.admin_edit_user_ai_requests)
    await callback.answer()

@router.message(UserSettings.admin_edit_user_ai_requests, is_admin_check)
async def process_set_user_ai_requests(message: Message, state: FSMContext):
    try:
        new_requests = int(message.text)
        if new_requests < 0:
            raise ValueError
        data = await state.get_data()
        user_telegram_id = data['manage_user_id']
        await update_user_field(user_telegram_id, 'ai_requests_today', new_requests)
        await update_user_field(user_telegram_id, 'ai_last_request_date', date.today()) # Оновлюємо дату, щоб скинути лічильник
        await message.answer(f"Кількість AI запитів для користувача {user_telegram_id} встановлено на {new_requests}.", show_alert=True)
        await admin_manage_users_callback_handler(message) # Повертаємося до списку користувачів
    except ValueError:
        await message.answer("Будь ласка, введіть дійсне невід'ємне число.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_manage_users")]
                             ]))

# Обробник кнопки "Змінити мову" (Адмін)
@router.callback_query(F.data == "admin_edit_user_language", UserSettings.admin_manage_users, is_admin_check)
async def admin_edit_user_language_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_telegram_id = data['manage_user_id']
    user = await get_user_by_telegram_id(user_telegram_id)
    current_lang = user.get('preferred_language', 'uk')
    kb = [
        [InlineKeyboardButton(text="Українська", callback_data="set_user_lang_uk")],
        [InlineKeyboardButton(text="English", callback_data="set_user_lang_en")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_manage_users")]
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    await callback.message.edit_text(f"Мова для користувача {user_telegram_id}: {current_lang.upper()}", reply_markup=markup)
    await state.set_state(UserSettings.admin_edit_user_language)
    await callback.answer()

@router.callback_query(F.data.startswith("set_user_lang_"), UserSettings.admin_edit_user_language, is_admin_check)
async def process_set_user_language(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_telegram_id = data['manage_user_id']
    lang_code = callback.data.split("_")[3]
    await update_user_field(user_telegram_id, 'preferred_language', lang_code)
    await callback.answer(f"Мову для користувача {user_telegram_id} встановлено на {lang_code.upper()}.", show_alert=True)
    await admin_manage_users_callback_handler(callback) # Повертаємося до списку користувачів

# Обробник кнопки "Видалити користувача" (Адмін)
@router.callback_query(F.data == "admin_delete_user", UserSettings.admin_manage_users, is_admin_check)
async def admin_delete_user_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_telegram_id = data['manage_user_id']
    user = await get_user_by_telegram_id(user_telegram_id)

    kb = [
        [InlineKeyboardButton(text="✅ Так, видалити", callback_data="confirm_delete_user")],
        [InlineKeyboardButton(text="❌ Ні, скасувати", callback_data="admin_manage_users")]
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    await callback.message.edit_text(f"Ви впевнені, що хочете видалити користувача {user_telegram_id} ({user['first_name']})? Це також видалить всі його дані та підписки.",
                                     reply_markup=markup)
    await state.set_state(UserSettings.admin_confirm_delete_user)
    await callback.answer()

@router.callback_query(F.data == "confirm_delete_user", UserSettings.admin_confirm_delete_user, is_admin_check)
async def process_confirm_delete_user(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_telegram_id = data['manage_user_id']
    await delete_user(user_telegram_id) # Припускаємо, що у вас є функція delete_user в database.py
    await callback.message.edit_text(f"Користувача {user_telegram_id} успішно видалено.",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Назад до адмін-панелі", callback_data="admin_panel")]
                                     ]))
    await state.clear()
    await callback.answer()

# Обробник кнопки "Налаштування бота" (Адмін)
@router.callback_query(F.data == "admin_edit_bot_settings", is_admin_check)
async def admin_edit_bot_settings_callback_handler(callback: CallbackQuery, state: FSMContext):
    settings_keys = ["DEFAULT_PARSE_INTERVAL_MINUTES", "MAX_AI_REQUESTS_PER_DAY"] # Додайте інші налаштування, якщо є
    kb = []
    for key in settings_keys:
        setting_value = await get_bot_setting(key)
        kb.append([InlineKeyboardButton(text=f"{key}: {setting_value}", callback_data=f"edit_bot_setting_{key}")])

    kb.append([InlineKeyboardButton(text="⬅️ Назад до адмін-панелі", callback_data="admin_panel")])
    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    await callback.message.edit_text("Оберіть налаштування для редагування:", reply_markup=markup)
    await state.set_state(UserSettings.admin_select_setting_to_edit)
    await callback.answer()

@router.callback_query(F.data.startswith("edit_bot_setting_"), UserSettings.admin_select_setting_to_edit, is_admin_check)
async def process_edit_bot_setting(callback: CallbackQuery, state: FSMContext):
    setting_key = callback.data.split("_")[3]
    current_value = await get_bot_setting(setting_key)
    await state.update_data(edit_setting_key=setting_key)
    await callback.message.edit_text(f"Введіть нове значення для '{setting_key}' (поточне: {current_value}):",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                                     ]))
    await state.set_state(UserSettings.admin_enter_setting_value)
    await callback.answer()

@router.message(UserSettings.admin_enter_setting_value, is_admin_check)
async def process_new_bot_setting_value(message: Message, state: FSMContext):
    data = await state.get_data()
    setting_key = data['edit_setting_key']
    new_value = message.text

    # Додаткова валідація для числових налаштувань
    if setting_key in ["DEFAULT_PARSE_INTERVAL_MINUTES", "MAX_AI_REQUESTS_PER_DAY"]:
        try:
            new_value = int(new_value)
            if new_value <= 0:
                raise ValueError
        except ValueError:
            await message.answer("Будь ласка, введіть дійсне число більше нуля.",
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                     [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                                 ]))
            return

    await update_bot_setting(setting_key, str(new_value)) # Зберігаємо як текст
    await message.answer(f"Налаштування '{setting_key}' оновлено до '{new_value}'.",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="⬅️ Назад до адмін-панелі", callback_data="admin_panel")]
                         ]))
    await state.clear()

# Обробник команди /news_{id} для детального перегляду новини
@router.message(Command(re.compile(r"news_(\d+)")))
async def show_detailed_news(message: Message):
    news_id = int(message.text.split("_")[1])
    news_item = await get_news_item_by_id(news_id)

    if not news_item:
        await message.answer("Новину не знайдено.")
        return

    user_id = (await get_user_by_telegram_id(message.from_user.id))['id']

    # Додаємо новину до переглянутих
    await add_user_news_view(user_id, news_id)

    title = news_item['title']
    content = news_item['content']
    source_name = (await get_source_by_id(news_item['source_id']))['name']
    published_at_utc = news_item['published_at']
    published_at_str = published_at_utc.strftime("%d.%m.%Y %H:%M") if published_at_utc else "Невідомо"
    image_url = news_item['image_url']
    news_url = news_item['source_url']

    # Перевіряємо, чи новина в закладках
    is_bookmarked = await get_user_bookmarks(user_id, news_id)
    bookmark_action = "add_bookmark" if not is_bookmarked else "remove_bookmark"
    bookmark_text = "🔖 Додати в закладки" if not is_bookmarked else "🗑️ Видалити із закладок"

    # Визначаємо реакції
    reactions = await get_user_news_reactions(user_id, news_id)
    like_action = "like_news" if not (reactions and reactions.get('reaction_type') == 'like') else "unlike_news"
    dislike_action = "dislike_news" if not (reactions and reactions.get('reaction_type') == 'dislike') else "undislike_news"
    like_text = "👍 Подобається" if not (reactions and reactions.get('reaction_type') == 'like') else "👍 Видалити лайк"
    dislike_text = "👎 Не подобається" if not (reactions and reactions.get('reaction_type') == 'dislike') else "👎 Видалити дизлайк"


    response_text = (
        f"<b>{title}</b>\n\n"
        f"Джерело: {source_name}\n"
        f"Опубліковано: {published_at_str} UTC\n\n"
        f"{content}\n\n"
        f"{hlink('Читати оригінал', news_url)}"
    )

    if image_url:
        # Можливо, варто додати перевірку на валідність image_url
        await message.answer_photo(photo=image_url, caption=response_text, parse_mode=ParseMode.HTML,
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                       [InlineKeyboardButton(text=bookmark_text, callback_data=f"{bookmark_action}_{news_id}")],
                                       [InlineKeyboardButton(text=like_text, callback_data=f"{like_action}_{news_id}"),
                                        InlineKeyboardButton(text=dislike_text, callback_data=f"{dislike_action}_{news_id}")],
                                       [InlineKeyboardButton(text="⬅️ Назад до стрічки", callback_data="my_feed")],
                                       [InlineKeyboardButton(text="⬅️ Назад до меню", callback_data="menu")]
                                   ]))
    else:
        await message.answer(response_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [InlineKeyboardButton(text=bookmark_text, callback_data=f"{bookmark_action}_{news_id}")],
                                 [InlineKeyboardButton(text=like_text, callback_data=f"{like_action}_{news_id}"),
                                  InlineKeyboardButton(text=dislike_text, callback_data=f"{dislike_action}_{news_id}")],
                                 [InlineKeyboardButton(text="⬅️ Назад до стрічки", callback_data="my_feed")],
                                 [InlineKeyboardButton(text="⬅️ Назад до меню", callback_data="menu")]
                             ]))

# Обробники для додавання/видалення закладок та реакцій
@router.callback_query(F.data.startswith("add_bookmark_"))
async def add_bookmark_handler(callback: CallbackQuery):
    news_id = int(callback.data.split("_")[2])
    user_id = (await get_user_by_telegram_id(callback.from_user.id))['id']
    if await add_bookmark(user_id, news_id):
        await callback.answer("Новину додано до закладок!", show_alert=True)
    else:
        await callback.answer("Новина вже є у ваших закладках.", show_alert=True)
    # Оновлюємо кнопки після дії
    await show_detailed_news(callback.message, news_id=news_id) # Передаємо news_id для оновлення кнопок

@router.callback_query(F.data.startswith("remove_bookmark_"))
async def remove_bookmark_handler(callback: CallbackQuery):
    news_id = int(callback.data.split("_")[2])
    user_id = (await get_user_by_telegram_id(callback.from_user.id))['id']
    if await delete_bookmark(user_id, news_id):
        await callback.answer("Новину видалено із закладок.", show_alert=True)
    else:
        await callback.answer("Цієї новини немає у ваших закладках.", show_alert=True)
    # Оновлюємо кнопки після дії
    await show_detailed_news(callback.message, news_id=news_id) # Передаємо news_id для оновлення кнопок

@router.callback_query(F.data.startswith("like_news_"))
async def like_news_handler(callback: CallbackQuery):
    news_id = int(callback.data.split("_")[2])
    user_id = (await get_user_by_telegram_id(callback.from_user.id))['id']
    await add_user_news_reaction(user_id, news_id, 'like')
    await callback.answer("Вам сподобалася новина!", show_alert=True)
    # Оновлюємо кнопки після дії
    await show_detailed_news(callback.message, news_id=news_id) # Передаємо news_id для оновлення кнопок

@router.callback_query(F.data.startswith("unlike_news_"))
async def unlike_news_handler(callback: CallbackQuery):
    news_id = int(callback.data.split("_")[2])
    user_id = (await get_user_by_telegram_id(callback.from_user.id))['id']
    await add_user_news_reaction(user_id, news_id, None) # Видаляємо реакцію
    await callback.answer("Лайк видалено.", show_alert=True)
    # Оновлюємо кнопки після дії
    await show_detailed_news(callback.message, news_id=news_id) # Передаємо news_id для оновлення кнопок

@router.callback_query(F.data.startswith("dislike_news_"))
async def dislike_news_handler(callback: CallbackQuery):
    news_id = int(callback.data.split("_")[2])
    user_id = (await get_user_by_telegram_id(callback.from_user.id))['id']
    await add_user_news_reaction(user_id, news_id, 'dislike')
    await callback.answer("Вам не сподобалася новина.", show_alert=True)
    # Оновлюємо кнопки після дії
    await show_detailed_news(callback.message, news_id=news_id) # Передаємо news_id для оновлення кнопок

@router.callback_query(F.data.startswith("undislike_news_"))
async def undislike_news_handler(callback: CallbackQuery):
    news_id = int(callback.data.split("_")[2])
    user_id = (await get_user_by_telegram_id(callback.from_user.id))['id']
    await add_user_news_reaction(user_id, news_id, None) # Видаляємо реакцію
    await callback.answer("Дизлайк видалено.", show_alert=True)
    # Оновлюємо кнопки після дії
    await show_detailed_news(callback.message, news_id=news_id) # Передаємо news_id для оновлення кнопок


# Загальний обробник для невідомих команд/тексту
@router.message()
async def echo_handler(message: types.Message) -> None:
    """
    Handler will forward receive a message back to the user
    By default, message will be retransmitted without modification
    """
    try:
        await message.send_copy(chat_id=message.chat.id)
    except TypeError:
        # But not all the types is supported to be copied so need to handle it
        await message.answer("Nice try!")


# Реєстрація роутера
dp.include_router(router)


# Запуск бота
async def main() -> None:
    # Ініціалізація пулу з'єднань до бази даних
    await get_db_pool()
    # Запускаємо бота
    await dp.start_polling(bot)

# Запуск FastAPI
@app.on_event("startup")
async def startup_event():
    logger.info("Starting up FastAPI app...")
    await get_db_pool() # Ініціалізуємо пул при запуску FastAPI
    # Запускаємо бота в окремому завданні
    asyncio.create_task(main())

@app.get("/", response_class=HTMLResponse)
async def read_root():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>News Bot</title>
        <style>
            body { font-family: sans-serif; margin: 20px; background-color: #f4f4f4; color: #333; text-align: center; }
            .container { max-width: 600px; margin: 0 auto; padding: 20px; background-color: #fff; border-radius: 8px; box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1); }
            h1 { color: #2c3e50; }
            p { font-size: 1.1em; }
            a { text-decoration: none; color: #3498db; }
            a:hover { text-decoration: underline; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>News Bot API</h1>
            <p>Ваш бот працює! Ви можете взаємодіяти з ним через Telegram.</p>
            <p><a href="/admin">Перейти до адмін-панелі (потрібен API ключ)</a></p>
        </div>
    </body>
    </html>
    """

@app.get("/admin", response_class=HTMLResponse)
async def admin_panel_web(api_key: str = Depends(get_api_key)):
    # Тут можна додати логіку для відображення адмін-панелі
    # Наприклад, список джерел, користувачів тощо.
    # Для простоти, поки що заглушка.
    sources = await get_all_sources()
    users = await get_all_users()

    sources_html = ""
    for s in sources:
        sources_html += f"<li><b>{s['name']}</b> (ID: {s['id']}) - {s['url']} - {s['status']}</li>"

    users_html = ""
    for u in users:
        users_html += f"<li><b>{u['first_name']}</b> (TG ID: {u['telegram_id']}) - Admin: {u['is_admin']}</li>"


    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Адмін-панель</title>
        <style>
            body {{ font-family: sans-serif; margin: 20px; background-color: #f4f4f4; color: #333; }}
            .container {{ max-width: 900px; margin: 0 auto; padding: 20px; background-color: #fff; border-radius: 8px; box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1); }}
            h1 {{ color: #2c3e50; }}
            h2 {{ color: #34495e; margin-top: 20px; }}
            ul {{ list-style-type: none; padding: 0; }}
            li {{ margin-bottom: 8px; padding: 8px; border-bottom: 1px solid #eee; }}
            a {{ text-decoration: none; color: #3498db; }}
            a:hover {{ text-decoration: underline; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Адмін-панель</h1>
            <p>Ласкаво просимо до адмін-панелі. Тут ви можете керувати ботом.</p>
            
            <h2>Джерела новин</h2>
            <ul>{sources_html}</ul>

            <h2>Користувачі</h2>
            <ul>{users_html}</ul>

            <p><a href="/">Повернутися до головної</a></p>
        </div>
    </body>
    </html>
    """

@app.get("/news/{news_id}", response_class=HTMLResponse)
async def get_news_detail(news_id: int):
    news_item = await get_news_item_by_id(news_id)
    if not news_item:
        raise HTTPException(status_code=404, detail="News not found")

    source = await get_source_by_id(news_item['source_id'])
    source_name = source['name'] if source else "Невідоме джерело"
    published_at_str = news_item['published_at'].strftime("%d.%m.%Y %H:%M") if news_item['published_at'] else "Невідомо"

    # Простий HTML для відображення новини
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>{news_item['title']}</title>
        <style>
            body {{ font-family: sans-serif; margin: 20px; background-color: #f4f4f4; color: #333; }}
            .container {{ max-width: 800px; margin: 0 auto; padding: 20px; background-color: #fff; border-radius: 8px; box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1); }}
            h1 {{ color: #2c3e50; }}
            .meta {{ color: #7f8c8d; font-size: 0.9em; margin-bottom: 15px; }}
            .content {{ line-height: 1.6; }}
            img {{ max-width: 100%; height: auto; display: block; margin: 15px 0; border-radius: 4px; }}
            .back-link {{ display: block; margin-top: 20px; text-align: center; }}
            a {{ text-decoration: none; color: #3498db; }}
            a:hover {{ text-decoration: underline; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>{news_item['title']}</h1>
            <p class="meta">Джерело: {source_name} | Опубліковано: {published_at_str} UTC</p>
            {"<img src='" + news_item['image_url'] + "' alt='Зображення новини'>" if news_item['image_url'] else ""}
            <div class="content">
                <p>{news_item['content']}</p>
            </div>
            <p class="back-link"><a href="{news_item['source_url']}" target="_blank">Читати оригінал</a></p>
            <p class="back-link"><a href="/">Повернутися до головної</a></p>
        </div>
    </body>
    </html>
    """

@app.get("/news_digest", response_class=HTMLResponse)
async def get_news_digest_web():
    # Ця функція може бути викликана для відображення дайджесту новин у веб-інтерфейсі
    # Наразі вона повертає заглушку. Можна розширити для відображення реальних новин.
    news_html = "<li>Наразі дайджест новин недоступний.</li>"
    # Тут можна отримати новини для дайджесту, наприклад:
    # news_for_digest = await get_news_for_digest_web_version()
    # news_html = "".join([f"<li><a href='{n['source_url']}'>{n['title']}</a></li>" for n in news_for_digest])

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
    # Завантажуємо змінні середовища з .env файлу
    load_dotenv()
    # Перевіряємо, чи встановлено TELEGRAM_BOT_TOKEN
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN environment variable is not set.")
        # Замість sys.exit(1) просто виводимо помилку і дозволяємо FastAPI запуститися
        # Це дозволить веб-частині додатка працювати, навіть якщо бот не може підключитися
    uvicorn.run(app, host="0.0.0.0", port=os.getenv("PORT", 8000))

