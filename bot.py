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
from typing import List, Optional, Dict, Any, Union, Callable, Awaitable
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
import re
import sys

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile, Update
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
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from gtts import gTTS
from croniter import croniter
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pydantic import BaseModel, Field

from database import get_db_pool, get_user_by_telegram_id, update_user_field, get_source_by_id, get_all_active_sources, add_news_item, get_news_by_source_id, get_all_news, get_user_bookmarks, add_bookmark, delete_bookmark, get_user_news_views, add_user_news_view, get_user_news_reactions, add_user_news_reaction, update_news_item, get_news_item_by_id, get_source_by_url, add_source, update_source_status, get_all_sources, get_bot_setting, update_bot_setting, get_user_by_id, get_last_n_news, update_source_last_parsed, get_news_for_digest, get_tasks_by_status, update_task_status, add_task_to_queue, get_all_users, get_user_subscriptions, add_user_subscription, delete_user_subscription, get_all_subscribed_sources, get_source_stats, update_source_stats, delete_user, delete_source, delete_news_item_by_id, get_one_unsent_news_item, mark_news_as_sent
from config import TELEGRAM_BOT_TOKEN, ADMIN_TELEGRAM_ID, WEB_APP_URL, API_KEY_NAME, API_KEY, NEWS_CHANNEL_ID

# Імпортуємо web_parser
import web_parser

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

stream_handler = logging.StreamHandler()
stream_handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

app = FastAPI(title="News Bot API")

api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

async def get_api_key(api_key: str = Depends(api_key_header)):
    if api_key == API_KEY:
        return api_key
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Could not validate credentials")

bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()

scheduler = AsyncIOScheduler()

@dp.errors()
async def errors_handler(update: types.Update, exception: Exception): # Виправлено аргументи
    logger.error(f"Error occurred in handler for update {update.update_id}: {exception}", exc_info=exception)


class UserSettings(StatesGroup):
    choosing_language = State()
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
    # New states for adding source via Telegram
    add_source_url = State()
    add_source_name = State()
    add_source_category = State()
    add_source_language = State()
    add_source_status = State()
    add_source_parse_interval = State()
    # New states for AI Assistant
    ai_assistant_main = State()
    ai_generate_portnikov = State()
    ai_generate_lipsits = State()
    ai_translate_news_select = State()
    ai_translate_news_input = State()
    ai_explain_terms_input = State()


class SourceManagement(StatesGroup):
    waiting_for_url = State()
    waiting_for_name = State()
    waiting_for_category = State()
    waiting_for_language = State()
    waiting_for_status = State()
    waiting_for_parse_interval = State()
    waiting_for_edit_id = State()
    waiting_for_edit_field = State()
    waiting_for_new_value = State()
    waiting_for_delete_id = State()

async def is_admin_check(message: Message) -> bool:
    user_data = await get_user_by_telegram_id(message.from_user.id)
    return user_data and user_data.get('is_admin', False)

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
        await update_user_field(telegram_id, 'last_active', datetime.now(timezone.utc))
        if user.get('ai_last_request_date') != date.today():
            await update_user_field(telegram_id, 'ai_requests_today', 0)
            await update_user_field(telegram_id, 'ai_last_request_date', date.today())

    return user

@dp.message.middleware()
async def user_middleware(
    handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
    message: Message,
    data: Dict[str, Any]
) -> Any:
    if message.from_user:
        await get_or_create_user(
            message.from_user.id,
            message.from_user.username or "",
            message.from_user.first_name or "",
            message.from_user.last_name or ""
        )
    return await handler(message, data)

# --- Scheduled Jobs ---
async def parse_all_sources_job():
    logger.info("Running scheduled news parsing job...")
    active_sources = await get_all_active_sources()
    if not active_sources:
        logger.info("No active sources found to parse.")
        return

    for source in active_sources:
        try:
            logger.info(f"Parsing source: {source['name']} ({source['url']})")
            recent_news_items = await web_parser.fetch_recent_news_from_source(source['url'], limit=5)
            
            if recent_news_items:
                for parsed_data in recent_news_items:
                    try:
                        news_id = await add_news_item(
                            source['id'],
                            parsed_data['title'],
                            parsed_data['content'],
                            parsed_data['source_url'],
                            parsed_data.get('image_url'),
                            parsed_data.get('published_at'),
                            parsed_data.get('lang', 'uk')
                        )
                        logger.info(f"Added new news item from {source['name']}: {parsed_data['title']} (ID: {news_id})")
                    except psycopg.IntegrityError:
                        logger.info(f"News item with URL {parsed_data['source_url']} already exists. Skipping.")
            else:
                logger.warning(f"No new news found or failed to parse from {source['name']} ({source['url']})")
            await update_source_last_parsed(source['id'], datetime.now(timezone.utc))
        except Exception as e:
            logger.error(f"Error during parsing source {source.get('name', source['url'])}: {e}", exc_info=True)
    logger.info("Finished scheduled news parsing job.")

async def publish_news_to_channel_job():
    logger.info("Running scheduled news publishing job...")
    if not NEWS_CHANNEL_ID or NEWS_CHANNEL_ID == 0:
        logger.warning("NEWS_CHANNEL_ID is not set or is 0 in config.py. Skipping news publishing to channel.")
        return

    logger.info(f"Attempting to fetch one unsent news item for channel {NEWS_CHANNEL_ID}...")
    news_item = await get_one_unsent_news_item()
    
    if news_item:
        logger.info(f"Found unsent news item: ID={news_item['id']}, Title='{news_item['title']}'")
        try:
            source = await get_source_by_id(news_item['source_id'])
            source_name = source['name'] if source else "Невідоме джерело"
            
            truncated_content = news_item['content']
            if len(truncated_content) > 500:
                truncated_content = truncated_content[:500] + "..."

            channel_post_text = (
                f"<b>Нова новина з {source_name}!</b>\n\n"
                f"<b>{news_item['title']}</b>\n"
                f"{truncated_content}\n\n" # Truncate content for preview
                f"{hlink('Читати далі', news_item['source_url'])}"
            )
            
            if news_item.get('image_url'):
                logger.info(f"Sending photo with caption for news ID {news_item['id']} to channel {NEWS_CHANNEL_ID} (Image: {news_item['image_url']}).")
                await bot.send_photo(NEWS_CHANNEL_ID, photo=news_item['image_url'], caption=channel_post_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            else:
                logger.info(f"Sending text message for news ID {news_item['id']} to channel {NEWS_CHANNEL_ID}.")
                await bot.send_message(NEWS_CHANNEL_ID, channel_post_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            
            await mark_news_as_sent(news_item['id'])
            logger.info(f"Published news item {news_item['id']} to channel {NEWS_CHANNEL_ID} and marked as sent.")
        except Exception as e:
            logger.error(f"Failed to publish news item {news_item['id']} to channel {NEWS_CHANNEL_ID}: {e}", exc_info=True)
            # Optionally, mark news as failed or retry later if it's a transient error
    else:
        logger.info("No unsent news items found to publish.")
    logger.info("Finished scheduled news publishing job.")

# --- End Scheduled Jobs ---


@router.message(CommandStart())
async def command_start_handler(message: Message, state: FSMContext) -> None:
    user = await get_or_create_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.first_name or "",
        message.from_user.last_name or ""
    )
    kb = [
        [InlineKeyboardButton(text="🔁 Отримати останню новину", callback_data="get_latest_news")],
        [InlineKeyboardButton(text="⚙️ Налаштування", callback_data="settings")],
        [InlineKeyboardButton(text="🤖 AI Асистент", callback_data="ai_assistant")]
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

@router.message(Command("menu"))
async def command_menu_handler(message: Message, state: FSMContext) -> None:
    user = await get_or_create_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.first_name or "",
        message.from_user.last_name or ""
    )
    kb = [
        [InlineKeyboardButton(text="🔁 Отримати останню новину", callback_data="get_latest_news")],
        [InlineKeyboardButton(text="⚙️ Налаштування", callback_data="settings")],
        [InlineKeyboardButton(text="🤖 AI Асистент", callback_data="ai_assistant")]
    ]
    if user.get('is_admin'):
        kb.append([InlineKeyboardButton(text="🛠️ Адмін-панель", callback_data="admin_panel")])

    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    await message.answer("Головне меню:", reply_markup=markup)
    await state.clear()

@router.callback_query(F.data == "get_latest_news")
async def get_latest_news_handler(callback: CallbackQuery):
    await callback.answer("Завантажую останню новину...")
    
    news_item = await get_one_unsent_news_item()
    if news_item:
        source = await get_source_by_id(news_item['source_id'])
        source_name = source.get('name', 'Невідоме джерело')

        truncated_content = news_item['content']
        if len(truncated_content) > 500:
            truncated_content = truncated_content[:500] + "..."

        news_text = (
            f"<b>{news_item['title']}</b>\n\n"
            f"{truncated_content}\n\n"
            f"Джерело: {source_name}\n"
            f"{hlink('Читати далі', news_item['source_url'])}"
        )

        kb = [[InlineKeyboardButton(text="⬅️ Назад до меню", callback_data="menu")]]
        markup = InlineKeyboardMarkup(inline_keyboard=kb)

        if news_item.get('image_url'):
            try:
                await callback.message.answer_photo(photo=news_item['image_url'], caption=news_text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True)
            except Exception as e:
                logger.warning(f"Failed to send photo for news {news_item['id']}: {e}. Sending as text instead.")
                await callback.message.answer(news_text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True)
        else:
            await callback.message.answer(news_text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True)
        
        await mark_news_as_sent(news_item['id'])
        logger.info(f"User {callback.from_user.id} received news item {news_item['id']} and it was marked as sent.")

    else:
        await callback.message.answer("Наразі немає нових новин для відображення. Спробуйте пізніше.",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Назад до меню", callback_data="menu")]
                                     ]))


@router.callback_query(F.data == "settings")
async def settings_callback_handler(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_telegram_id(callback.from_user.id)
    if not user:
        await callback.answer("Користувача не знайдено.", show_alert=True)
        return

    lang_status = "✅" if user.get('preferred_language') == 'uk' else "🇺🇸"

    kb = [
        [InlineKeyboardButton(text=f"Мова новин: {lang_status}", callback_data="set_language")],
        [InlineKeyboardButton(text="➕ Додати джерело новин", callback_data="add_news_source_telegram")],
        [InlineKeyboardButton(text="📄 Список джерел новин", callback_data="list_news_sources_telegram")],
        [InlineKeyboardButton(text="⬅️ Назад до меню", callback_data="menu")]
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    await callback.message.edit_text("Оберіть налаштування:", reply_markup=markup)
    await callback.answer()

@router.callback_query(F.data == "set_language")
async def set_language_callback_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    kb = [
        [InlineKeyboardButton(text="🇺🇦 Українська", callback_data="set_lang_uk")],
        [InlineKeyboardButton(text="🇬🇧 English", callback_data="set_lang_en")],
        [InlineKeyboardButton(text="⬅️ Назад до налаштувань", callback_data="settings")]
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    await callback.message.edit_text("Оберіть мову для перекладу новин:", reply_markup=markup)
    await state.set_state(UserSettings.choosing_language)


@router.callback_query(F.data.startswith("set_lang_"), UserSettings.choosing_language)
async def process_language_choice(callback: CallbackQuery, state: FSMContext):
    await callback.answer() # Додано await callback.answer()
    lang_code = callback.data.split("_")[2]
    await update_user_field(callback.from_user.id, 'preferred_language', lang_code)
    await callback.answer(f"Мову встановлено на {lang_code.upper()}", show_alert=True)
    await state.clear()
    await settings_callback_handler(callback, state)

@router.callback_query(F.data == "add_news_source_telegram")
async def add_news_source_telegram_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("Введіть URL нового джерела:",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="settings")]
                                     ]))
    await state.set_state(UserSettings.add_source_url)

@router.message(UserSettings.add_source_url)
async def process_telegram_source_url(message: Message, state: FSMContext):
    url = message.text
    parsed_url = urlparse(url)
    if not all([parsed_url.scheme, parsed_url.netloc]):
        await message.answer("Будь ласка, введіть дійсний URL (наприклад, https://example.com).",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="settings")]
                             ]))
        return

    existing_source = await get_source_by_url(url)
    if existing_source:
        await message.answer("Джерело з таким URL вже існує в базі даних.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="settings")]
                             ]))
        await state.clear()
        return

    await state.update_data(new_source_url=url)
    await message.answer("Введіть назву джерела (наприклад, 'Європейська Правда'):",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="settings")]
                         ]))
    await state.set_state(UserSettings.add_source_name)

@router.message(UserSettings.add_source_name)
async def process_telegram_source_name(message: Message, state: FSMContext):
    name = message.text
    await state.update_data(new_source_name=name)
    await message.answer("Введіть категорію джерела (наприклад, 'Політика', 'Економіка', 'Спорт'):",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="settings")]
                         ]))
    await state.set_state(UserSettings.add_source_category)

@router.message(UserSettings.add_source_category)
async def process_telegram_source_category(message: Message, state: FSMContext):
    category = message.text
    await state.update_data(new_source_category=category)
    await message.answer("Введіть мову джерела (наприклад, 'uk', 'en'):",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="settings")]
                         ]))
    await state.set_state(UserSettings.add_source_language)

@router.message(UserSettings.add_source_language)
async def process_telegram_source_language(message: Message, state: FSMContext):
    language = message.text
    if language not in ['uk', 'en']:
        await message.answer("Будь ласка, введіть 'uk' або 'en'.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="settings")]
                             ]))
        return

    await state.update_data(new_source_language=language)
    await message.answer("Введіть статус джерела ('active' або 'inactive'):",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="settings")]
                         ]))
    await state.set_state(UserSettings.add_source_status)

@router.message(UserSettings.add_source_status)
async def process_telegram_source_status(message: Message, state: FSMContext):
    status = message.text.lower()
    if status not in ['active', 'inactive']:
        await message.answer("Будь ласка, введіть 'active' або 'inactive'.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="settings")]
                             ]))
        return

    await state.update_data(new_source_status=status)
    await message.answer("Введіть інтервал парсингу в хвилинах (наприклад, '60' для щогодини):",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="settings")]
                         ]))
    await state.set_state(UserSettings.add_source_parse_interval)

@router.message(UserSettings.add_source_parse_interval)
async def process_telegram_source_parse_interval(message: Message, state: FSMContext):
    try:
        parse_interval = int(message.text)
        if parse_interval <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Будь ласка, введіть дійсне число більше нуля.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="settings")]
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
                             [InlineKeyboardButton(text="⬅️ Назад до налаштувань", callback_data="settings")]
                         ]))
    await state.clear()

@router.callback_query(F.data == "list_news_sources_telegram")
async def list_news_sources_telegram_handler(callback: CallbackQuery):
    await callback.answer()
    sources = await get_all_sources()
    if not sources:
        await callback.message.edit_text("Немає доданих джерел новин.",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                             [InlineKeyboardButton(text="⬅️ Назад до налаштувань", callback_data="settings")]
                                         ]))
        return

    sources_list_text = "Список доданих джерел новин:\n\n"
    for source in sources:
        sources_list_text += (
            f"ID: {source.get('id', 'N/A')}\n"
            f"Назва: {source.get('name', 'N/A')}\n"
            f"URL: {source.get('url', 'N/A')}\n"
            f"Категорія: {source.get('category', 'N/A')}\n"
            f"Мова: {source.get('language', 'N/A')}\n"
            f"Статус: {source.get('status', 'N/A')}\n"
            f"Інтервал парсингу: {source.get('parse_interval_minutes', 'N/A')} хв\n\n"
        )

    kb = [[InlineKeyboardButton(text="⬅️ Назад до налаштувань", callback_data="settings")]]
    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    await callback.message.edit_text(sources_list_text, reply_markup=markup, disable_web_page_preview=True)


@router.callback_query(F.data == "ai_assistant")
async def ai_assistant_callback_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    kb = [
        [InlineKeyboardButton(text="Згенерувати новину від Віталія Портнікова", callback_data="ai_portnikov")],
        [InlineKeyboardButton(text="Згенерувати новину від Ігоря Ліпсіца", callback_data="ai_lipsits")],
        [InlineKeyboardButton(text="Перекласти новину", callback_data="ai_translate_news")],
        [InlineKeyboardButton(text="Пояснити терміни (AI-аналітик)", callback_data="ai_explain_terms")],
        [InlineKeyboardButton(text="⬅️ Назад до меню", callback_data="menu")]
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    await callback.message.edit_text("Оберіть тип генерації новини або іншу дію AI:", reply_markup=markup)
    await state.set_state(UserSettings.ai_assistant_main)

async def generate_text_with_gemini(prompt: str) -> str:
    """
    Calls the Gemini API to generate text based on the given prompt.
    """
    chatHistory = []
    chatHistory.push({ "role": "user", "parts": [{ "text": prompt }] })
    payload = { "contents": chatHistory }
    apiKey = os.getenv("GEMINI_API_KEY", "")
    apiUrl = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={apiKey}"

    try:
        async with ClientSession() as session:
            async with session.post(apiUrl, headers={'Content-Type': 'application/json'}, json=payload) as response:
                response.raise_for_status()
                result = await response.json()
                if result.get('candidates') and result['candidates'][0].get('content') and result['candidates'][0]['content'].get('parts'):
                    return result['candidates'][0]['content']['parts'][0]['text']
                else:
                    logger.error(f"Unexpected API response structure: {result}")
                    return "Не вдалося згенерувати відповідь. Неочікувана відповідь від AI."
    except Exception as e:
        logger.error(f"Error calling Gemini API: {e}")
        return f"Виникла помилка при зверненні до AI: {e}"


@router.callback_query(F.data == "ai_portnikov", UserSettings.ai_assistant_main)
async def ai_portnikov_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Генерую новину в стилі Віталія Портнікова...")
    
    prompt = "Згенеруй короткий аналітичний пост про поточну геополітичну ситуацію в Україні та світі у стилі Віталія Портнікова. Використовуй його характерну лексику та манеру викладу, фокусуючись на глибокому аналізі та прогнозах."
    generated_text = await generate_text_with_gemini(prompt)
    
    await callback.message.edit_text(f"<b>Новина від Віталія Портнікова:</b>\n\n{generated_text}",
                                     parse_mode=ParseMode.HTML,
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Назад до AI Асистента", callback_data="ai_assistant")]
                                     ]))
    await state.clear()

@router.callback_query(F.data == "ai_lipsits", UserSettings.ai_assistant_main)
async def ai_lipsits_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Генерую новину в стилі Ігоря Ліпсіца...")

    prompt = "Згенеруй короткий економічний огляд або прогноз щодо української економіки у стилі Ігоря Ліпсіца. Використовуй його характерну термінологію, приклади та аргументацію, орієнтуючись на практичні висновки."
    generated_text = await generate_text_with_gemini(prompt)

    await callback.message.edit_text(f"<b>Новина від Ігоря Ліпсіца:</b>\n\n{generated_text}",
                                     parse_mode=ParseMode.HTML,
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Назад до AI Асистента", callback_data="ai_assistant")]
                                     ]))
    await state.clear()

@router.callback_query(F.data == "ai_translate_news", UserSettings.ai_assistant_main)
async def ai_translate_news_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    kb = [
        [InlineKeyboardButton(text="⬅️ Назад до AI Асистента", callback_data="ai_assistant")]
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    await callback.message.edit_text("Введіть текст новини, яку потрібно перекласти:", reply_markup=markup)
    await state.set_state(UserSettings.ai_translate_news_input)

@router.message(UserSettings.ai_translate_news_input)
async def process_ai_translate_news_input(message: Message, state: FSMContext):
    text_to_translate = message.text
    user = await get_user_by_telegram_id(message.from_user.id)
    target_lang = user.get('preferred_language', 'uk')

    prompt = f"Переклади наступний текст на {target_lang}:\n\n{text_to_translate}"
    translated_text = await generate_text_with_gemini(prompt)

    await message.answer(f"<b>Переклад:</b>\n\n{translated_text}",
                         parse_mode=ParseMode.HTML,
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="⬅️ Назад до AI Асистента", callback_data="ai_assistant")]
                         ]))
    await state.clear()

@router.callback_query(F.data == "ai_explain_terms", UserSettings.ai_assistant_main)
async def ai_explain_terms_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    kb = [
        [InlineKeyboardButton(text="⬅️ Назад до AI Асистента", callback_data="ai_assistant")]
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    await callback.message.edit_text("Введіть терміни або уривок тексту, який потрібно пояснити:", reply_markup=markup)
    await state.set_state(UserSettings.ai_explain_terms_input)

@router.message(UserSettings.ai_explain_terms_input)
async def process_ai_explain_terms_input(message: Message, state: FSMContext):
    text_to_explain = message.text
    
    prompt = f"Поясни наступні терміни або уривок тексту максимально простою мовою, надаючи контекст, якщо це можливо:\n\n{text_to_explain}"
    explanation_text = await generate_text_with_gemini(prompt)

    await message.answer(f"<b>Пояснення:</b>\n\n{explanation_text}",
                         parse_mode=ParseMode.HTML,
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="⬅️ Назад до AI Асистента", callback_data="ai_assistant")]
                         ]))
    await state.clear()


@router.callback_query(F.data == "admin_panel", is_admin_check)
async def admin_panel_callback_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
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

@router.callback_query(F.data == "admin_source_stats", is_admin_check)
async def admin_source_stats_callback_handler(callback: CallbackQuery):
    await callback.answer()
    sources = await get_all_sources()
    if not sources:
        await callback.message.edit_text("Немає джерел для відображення статистики.",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                             [InlineKeyboardButton(text="⬅️ Назад до адмін-панелі", callback_data="admin_panel")]
                                         ]))
        return

    stats_text = "📊 Статистика джерел:\n\n"
    for source in sources:
        source_stats = await get_source_stats(source['id'])
        news_count = source_stats.get('news_count', 0) if source_stats else 0
        last_parsed = source.get('last_parsed_at')
        last_parsed_str = last_parsed.strftime("%d.%m.%Y %H:%M") if last_parsed else "Ніколи"
        stats_text += (
            f"<b>{source.get('name', 'N/A')}</b> (ID: {source.get('id', 'N/A')})\n"
            f"Статус: {source.get('status', 'N/A')}\n"
            f"Категорія: {source.get('category', 'N/A')}\n"
            f"Новин: {news_count}\n"
            f"Останній парсинг: {last_parsed_str}\n\n"
        )

    kb = [[InlineKeyboardButton(text="⬅️ Назад до адмін-панелі", callback_data="admin_panel")]]
    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    await callback.message.edit_text(stats_text, reply_markup=markup, disable_web_page_preview=True)

@router.callback_query(F.data == "admin_send_message", is_admin_check)
async def admin_send_message_callback_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
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

@router.callback_query(F.data.startswith("send_message_"), UserSettings.admin_select_message_target, is_admin_check)
async def process_send_message_target(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
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

@router.callback_query(F.data.startswith("send_message_lang_"), UserSettings.admin_select_message_language_code, is_admin_check)
async def process_send_message_language_code(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    lang_code = callback.data.split("_")[3]
    await state.update_data(message_target_language=lang_code)
    await callback.message.edit_text(f"Ви обрали мову: {lang_code.upper()}.\nБудь ласка, введіть текст повідомлення:",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Назад", callback_data="send_message_language")]
                                     ]))
    await state.set_state(UserSettings.admin_enter_message_text)

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

@router.callback_query(F.data == "confirm_send_message", UserSettings.admin_confirm_send_message, is_admin_check)
async def process_confirm_send_message(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
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
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Не вдалося надіслати повідомлення користувачу {user['telegram_id']}: {e}")
            failed_count += 1

    await callback.message.edit_text(f"Повідомлення надіслано.\nУспішно: {sent_count}\nНевдало: {failed_count}",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Назад до адмін-панелі", callback_data="admin_panel")]
                                     ]))
    await state.clear()

@router.callback_query(F.data == "admin_add_source", is_admin_check)
async def admin_add_source_callback_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("Введіть URL нового джерела:",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                                     ]))
    await state.set_state(SourceManagement.waiting_for_url)

@router.message(SourceManagement.waiting_for_url, is_admin_check)
async def process_source_url(message: Message, state: FSMContext):
    url = message.text
    parsed_url = urlparse(url)
    if not all([parsed_url.scheme, parsed_url.netloc]):
        await message.answer("Будь ласка, введіть дійсний URL (наприклад, https://example.com).",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                             ]))
        return

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
    if language not in ['uk', 'en']:
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

@router.callback_query(F.data == "admin_edit_source", is_admin_check)
async def admin_edit_source_callback_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    sources = await get_all_sources()
    if not sources:
        await callback.message.edit_text("Немає джерел для редагування.",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                             [InlineKeyboardButton(text="⬅️ Назад до адмін-панелі", callback_data="admin_panel")]
                                         ]))
        return

    sources_list_text = "Оберіть джерело для редагування (введіть ID):\n\n"
    for source in sources:
        sources_list_text += f"ID: {source.get('id', 'N/A')}, Назва: {source.get('name', 'N/A')}, Статус: {source.get('status', 'N/A')}\n"

    await callback.message.edit_text(sources_list_text,
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                                     ]))
    await state.set_state(SourceManagement.waiting_for_edit_id)

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
        await message.answer(f"Оберіть, що ви хочете редагувати для джерела ID {source_id} ({source.get('name', 'N/A')}):",
                             reply_markup=markup)
        await state.set_state(SourceManagement.waiting_for_edit_field)
    except ValueError:
        await message.answer("Будь ласка, введіть дійсний числовий ID джерела.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                             ]))

@router.callback_query(F.data.startswith("edit_source_"), SourceManagement.waiting_for_edit_field, is_admin_check)
async def process_edit_source_field(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    field = callback.data.split("_")[2]
    await state.update_data(edit_source_field=field)
    await callback.message.edit_text(f"Введіть нове значення для '{field}':",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                                     ]))
    await state.set_state(SourceManagement.waiting_for_new_value)

@router.message(SourceManagement.waiting_for_new_value, is_admin_check)
async def process_new_source_value(message: Message, state: FSMContext):
    data = await state.get_data()
    source_id = data['edit_source_id']
    field = data['edit_source_field']
    new_value = message.text

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

    await update_source_status(source_id, {field: new_value})
    await message.answer(f"Поле '{field}' для джерела ID {source_id} оновлено.",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="⬅️ Назад до адмін-панелі", callback_data="admin_panel")]
                         ]))
    await state.clear()

@router.callback_query(F.data == "admin_delete_source", is_admin_check)
async def admin_delete_source_callback_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    sources = await get_all_sources()
    if not sources:
        await callback.message.edit_text("Немає джерел для видалення.",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                             [InlineKeyboardButton(text="⬅️ Назад до адмін-панелі", callback_data="admin_panel")]
                                         ]))
        return

    sources_list_text = "Оберіть джерело для видалення (введіть ID):\n\n"
    for source in sources:
        sources_list_text += f"ID: {source.get('id', 'N/A')}, Назва: {source.get('name', 'N/A')}, Статус: {source.get('status', 'N/A')}\n"

    await callback.message.edit_text(sources_list_text,
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                                     ]))
    await state.set_state(SourceManagement.waiting_for_delete_id)

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
        await message.answer(f"Ви впевнені, що хочете видалити джерело ID {source_id} ({source.get('name', 'N/A')})? Це також видалить всі пов'язані новини та підписки.",
                             reply_markup=markup)
        await state.set_state(UserSettings.admin_confirm_delete_source)
    except ValueError:
        await message.answer("Будь ласка, введіть дійсний числовий ID джерела.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                             ]))

@router.callback_query(F.data == "confirm_delete_source", UserSettings.admin_confirm_delete_source, is_admin_check)
async def process_confirm_delete_source(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    source_id = data['delete_source_id']
    await delete_source(source_id)
    await callback.message.edit_text(f"Джерело ID {source_id} успішно видалено.",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Назад до адмін-панелі", callback_data="admin_panel")]
                                     ]))
    await state.clear()

@router.callback_query(F.data == "admin_manage_users", is_admin_check)
async def admin_manage_users_callback_handler(callback: CallbackQuery, page: int = 0):
    await callback.answer()
    users = await get_all_users()
    if not users:
        await callback.message.edit_text("Немає користувачів для керування.",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                             [InlineKeyboardButton(text="⬅️ Назад до адмін-панелі", callback_data="admin_panel")]
                                         ]))
        return

    users_per_page = 10
    total_pages = (len(users) + users_per_page - 1) // users_per_page
    start_index = page * users_per_page
    end_index = start_index + users_per_page
    current_users_page = users[start_index:end_index]

    users_list_text = "Оберіть користувача для керування (введіть Telegram ID):\n\n"
    for user in current_users_page:
        users_list_text += (
            f"ID: {user.get('telegram_id', 'N/A')}, Ім'я: {user.get('first_name', 'N/A')} {user.get('last_name', '')} "
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
        await message.answer(f"Оберіть дію для користувача ID {user_telegram_id} ({user.get('first_name', 'N/A')}):",
                             reply_markup=markup)
        await state.set_state(UserSettings.admin_manage_users)
    except ValueError:
        await message.answer("Будь ласка, введіть дійсний числовий Telegram ID.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                             ]))

@router.callback_query(F.data == "admin_toggle_admin_status", UserSettings.admin_manage_users, is_admin_check)
async def admin_toggle_admin_status_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    user_telegram_id = data['manage_user_id']
    user = await get_user_by_telegram_id(user_telegram_id)
    new_status = not user.get('is_admin', False)
    await update_user_field(user_telegram_id, 'is_admin', new_status)
    status_text = "адміністратором" if new_status else "звичайним користувачем"
    await callback.message.edit_text(f"Користувач {user_telegram_id} тепер є {status_text}.",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Назад до керування користувачами", callback_data="admin_manage_users")]
                                     ]))
    await state.clear()

@router.callback_query(F.data == "admin_edit_user_premium", UserSettings.admin_manage_users, is_admin_check)
async def admin_edit_user_premium_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
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

@router.callback_query(F.data.startswith("set_premium_"), UserSettings.admin_edit_user_premium, is_admin_check)
async def process_set_premium_status(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    user_telegram_id = data['manage_user_id']
    new_status = True if callback.data == "set_premium_true" else False
    await update_user_field(user_telegram_id, 'is_premium', new_status)
    status_text = "увімкнено" if new_status else "вимкнено"
    await callback.message.edit_text(f"Преміум статус для користувача {user_telegram_id} {status_text}.",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Назад до керування користувачами", callback_data="admin_manage_users")]
                                     ]))
    await state.clear()

@router.callback_query(F.data == "admin_edit_user_pro", UserSettings.admin_manage_users, is_admin_check)
async def admin_edit_user_pro_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
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

@router.callback_query(F.data.startswith("set_pro_"), UserSettings.admin_edit_user_pro, is_admin_check)
async def process_set_pro_status(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    user_telegram_id = data['manage_user_id']
    new_status = True if callback.data == "set_pro_true" else False
    await update_user_field(user_telegram_id, 'is_pro', new_status)
    status_text = "увімкнено" if new_status else "вимкнено"
    await callback.message.edit_text(f"PRO статус для користувача {user_telegram_id} {status_text}.",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Назад до керування користувачами", callback_data="admin_manage_users")]
                                     ]))
    await state.clear()

@router.callback_query(F.data == "admin_edit_user_digest", UserSettings.admin_manage_users, is_admin_check)
async def admin_edit_user_digest_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
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

@router.callback_query(F.data.startswith("set_user_digest_"), UserSettings.admin_edit_user_digest, is_admin_check)
async def process_set_user_digest_frequency(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    user_telegram_id = data['manage_user_id']
    freq_code = callback.data.split("_")[3]
    await update_user_field(user_telegram_id, 'digest_frequency', freq_code)
    await callback.message.edit_text(f"Частоту дайджесту для користувача {user_telegram_id} встановлено на {freq_code}.",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Назад до керування користувачами", callback_data="admin_manage_users")]
                                     ]))
    await state.clear()

@router.callback_query(F.data == "admin_edit_user_ai_requests", UserSettings.admin_manage_users, is_admin_check)
async def admin_edit_user_ai_requests_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    user_telegram_id = data['manage_user_id']
    user = await get_user_by_telegram_id(user_telegram_id)
    current_requests = user.get('ai_requests_today', 0)
    await callback.message.edit_text(f"Введіть нову кількість AI запитів на сьогодні для користувача {user_telegram_id} (поточна: {current_requests}):",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_manage_users")]
                                     ]))
    await state.set_state(UserSettings.admin_edit_user_ai_requests)

@router.message(UserSettings.admin_edit_user_ai_requests, is_admin_check)
async def process_set_user_ai_requests(message: Message, state: FSMContext):
    try:
        new_requests = int(message.text)
        if new_requests < 0:
            raise ValueError
        data = await state.get_data()
        user_telegram_id = data['manage_user_id']
        await update_user_field(user_telegram_id, 'ai_requests_today', new_requests)
        await update_user_field(user_telegram_id, 'ai_last_request_date', date.today())
        await message.answer(f"Кількість AI запитів для користувача {user_telegram_id} встановлено на {new_requests}.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [InlineKeyboardButton(text="⬅️ Назад до керування користувачами", callback_data="admin_manage_users")]
                             ]))
        await state.clear()
    except ValueError:
        await message.answer("Будь ласка, введіть дійсне невід'ємне число.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_manage_users")]
                             ]))

@router.callback_query(F.data == "admin_edit_user_language", UserSettings.admin_manage_users, is_admin_check)
async def admin_edit_user_language_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
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

@router.callback_query(F.data.startswith("set_user_lang_"), UserSettings.admin_edit_user_language, is_admin_check)
async def process_set_user_language(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    user_telegram_id = data['manage_user_id']
    lang_code = callback.data.split("_")[3]
    await update_user_field(user_telegram_id, 'preferred_language', lang_code)
    await callback.message.edit_text(f"Мову для користувача {user_telegram_id} встановлено на {lang_code.upper()}.",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Назад до керування користувачами", callback_data="admin_manage_users")]
                                     ]))
    await state.clear()

@router.callback_query(F.data == "admin_delete_user", UserSettings.admin_manage_users, is_admin_check)
async def admin_delete_user_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    user_telegram_id = data['manage_user_id']
    user = await get_user_by_telegram_id(user_telegram_id)

    kb = [
        [InlineKeyboardButton(text="✅ Так, видалити", callback_data="confirm_delete_user")],
        [InlineKeyboardButton(text="❌ Ні, скасувати", callback_data="admin_manage_users")]
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    await callback.message.edit_text(f"Ви впевнені, що хочете видалити користувача {user_telegram_id} ({user.get('first_name', 'N/A')})? Це також видалить всі його дані та підписки.",
                                     reply_markup=markup)
    await state.set_state(UserSettings.admin_confirm_delete_user)

@router.callback_query(F.data == "confirm_delete_user", UserSettings.admin_confirm_delete_user, is_admin_check)
async def process_confirm_delete_user(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    user_telegram_id = data['manage_user_id']
    await delete_user(user_telegram_id)
    await callback.message.edit_text(f"Користувача {user_telegram_id} успішно видалено.",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Назад до адмін-панелі", callback_data="admin_panel")]
                                     ]))
    await state.clear()

@router.callback_query(F.data == "admin_edit_bot_settings", is_admin_check)
async def admin_edit_bot_settings_callback_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    settings_keys = ["DEFAULT_PARSE_INTERVAL_MINUTES", "MAX_AI_REQUESTS_PER_DAY", "NEWS_PUBLISH_INTERVAL_MINUTES", "NEWS_PARSE_INTERVAL_MINUTES"]
    kb = []
    for key in settings_keys:
        setting_value = await get_bot_setting(key)
        kb.append([InlineKeyboardButton(text=f"{key}: {setting_value}", callback_data=f"edit_bot_setting_{key}")])

    kb.append([InlineKeyboardButton(text="⬅️ Назад до адмін-панелі", callback_data="admin_panel")])
    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    await callback.message.edit_text("Оберіть налаштування для редагування:", reply_markup=markup)
    await state.set_state(UserSettings.admin_select_setting_to_edit)

@router.callback_query(F.data.startswith("edit_bot_setting_"), UserSettings.admin_select_setting_to_edit, is_admin_check)
async def process_edit_bot_setting(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    setting_key = callback.data.split("_")[3]
    current_value = await get_bot_setting(setting_key)
    await state.update_data(edit_setting_key=setting_key)
    await callback.message.edit_text(f"Введіть нове значення для '{setting_key}' (поточне: {current_value}):",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="⬅️ Скасувати", callback_data="admin_panel")]
                                     ]))
    await state.set_state(UserSettings.admin_enter_setting_value)

@router.message(UserSettings.admin_enter_setting_value, is_admin_check)
async def process_new_bot_setting_value(message: Message, state: FSMContext):
    data = await state.get_data()
    setting_key = data['edit_setting_key']
    new_value = message.text

    if setting_key in ["DEFAULT_PARSE_INTERVAL_MINUTES", "MAX_AI_REQUESTS_PER_DAY", "NEWS_PUBLISH_INTERVAL_MINUTES", "NEWS_PARSE_INTERVAL_MINUTES"]:
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

    await update_bot_setting(setting_key, str(new_value))
    await message.answer(f"Налаштування '{setting_key}' оновлено до '{new_value}'.",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="⬅️ Назад до адмін-панелі", callback_data="admin_panel")]
                         ]))
    await state.clear()


@router.message()
async def echo_handler(message: types.Message) -> None:
    try:
        await message.send_copy(chat_id=message.chat.id)
    except TypeError:
        await message.answer("Nice try!")


dp.include_router(router)


@app.on_event("startup")
async def startup_event():
    logger.info("Starting up FastAPI app...")
    await get_db_pool()

    if not WEB_APP_URL:
        logger.error("WEB_APP_URL environment variable is not set. Webhook will not be set.")
        raise ValueError("WEB_APP_URL is not set. Cannot set Telegram webhook.")
    else:
        webhook_url = f"{WEB_APP_URL}/webhook"
        logger.info(f"Attempting to set Telegram webhook to: {webhook_url}")
        try:
            await bot.set_webhook(webhook_url)
            logger.info(f"Telegram webhook set successfully to: {webhook_url}")
        except Exception as e:
            logger.error(f"Failed to set Telegram webhook: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to set Telegram webhook: {e}. Check WEB_APP_URL and bot token.")


@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Shutting down FastAPI app...")
    pool = await get_db_pool()
    if pool:
        await pool.close()
    try:
        await bot.delete_webhook()
        logger.info("Telegram webhook deleted.")
    except Exception as e:
        logger.warning(f"Failed to delete Telegram webhook on shutdown: {e}")
    if bot:
        await bot.session.close()

@app.post("/webhook")
async def telegram_webhook(request: Request):
    update = types.Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}


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
    sources = await get_all_sources()
    users = await get_all_users()

    sources_html = ""
    for s in sources:
        sources_html += f"<li><b>{s.get('name', 'N/A')}</b> (ID: {s.get('id', 'N/A')}) - {s.get('url', 'N/A')} - {s.get('status', 'N/A')}</li>"

    users_html = ""
    for u in users:
        users_html += f"<li><b>{u.get('first_name', 'N/A')}</b> (TG ID: {u.get('telegram_id', 'N/A')}) - Admin: {u.get('is_admin', False)}</li>"

    add_source_form = """
    <h2>Додати нове джерело</h2>
    <form action="/admin/add_source" method="post">
        <label for="url">URL:</label><br>
        <input type="text" id="url" name="url" size="50" required><br><br>
        <label for="name">Назва:</label><br>
        <input type="text" id="name" name="name" size="50" required><br><br>
        <label for="category">Категорія:</label><br>
        <input type="text" id="category" name="category" size="50" required><br><br>
        <label for="language">Мова (uk/en):</label><br>
        <input type="text" id="language" name="language" size="10" required><br><br>
        <label for="status">Статус (active/inactive):</label><br>
        <input type="text" id="status" name="status" size="10" required><br><br>
        <label for="parse_interval_minutes">Інтервал парсингу (хвилини):</label><br>
        <input type="number" id="parse_interval_minutes" name="parse_interval_minutes" required><br><br>
        <input type="submit" value="Додати джерело">
    </form>
    """

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
            form {{ margin-top: 20px; padding: 15px; border: 1px solid #ddd; border-radius: 8px; background-color: #f9f9f9; }}
            form label {{ font-weight: bold; }}
            form input[type="text"], form input[type="number"] {{ width: calc(100% - 20px); padding: 8px; margin-bottom: 10px; border: 1px solid #ccc; border-radius: 4px; }}
            form input[type="submit"] {{ background-color: #28a745; color: white; padding: 10px 15px; border: none; border-radius: 4px; cursor: pointer; font-size: 1em; }}
            form input[type="submit"]:hover {{ background-color: #218838; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Адмін-панель</h1>
            <p>Ласкаво просимо до адмін-панелі. Тут ви можете керувати ботом.</p>
            
            {add_source_form}

            <h2>Джерела новин</h2>
            <ul>{sources_html}</ul>

            <h2>Користувачі</h2>
            <ul>{users_html}</ul>

            <p><a href="/">Повернутися до головної</a></p>
        </div>
    </body>
    </html>
    """

class SourceCreate(BaseModel):
    url: str
    name: str
    category: str
    language: str
    status: str
    parse_interval_minutes: int

@app.post("/admin/add_source", response_class=HTMLResponse)
async def add_source_web(source_data: SourceCreate, api_key: str = Depends(get_api_key)):
    try:
        if not all([source_data.url, source_data.name, source_data.category, source_data.language, source_data.status, source_data.parse_interval_minutes]):
            raise HTTPException(status_code=400, detail="All fields are required.")
        if source_data.language not in ['uk', 'en']:
            raise HTTPException(status_code=400, detail="Language must be 'uk' or 'en'.")
        if source_data.status not in ['active', 'inactive']:
            raise HTTPException(status_code=400, detail="Status must be 'active' or 'inactive'.")
        if source_data.parse_interval_minutes <= 0:
            raise HTTPException(status_code=400, detail="Parse interval must be a positive number.")

        existing_source = await get_source_by_url(source_data.url)
        if existing_source:
            raise HTTPException(status_code=409, detail="Source with this URL already exists.")

        source_id = await add_source(source_data.model_dump())
        
        response_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Джерело додано</title>
            <style>
                body {{ font-family: sans-serif; margin: 20px; background-color: #f4f4f4; color: #333; text-align: center; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; background-color: #fff; border-radius: 8px; box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1); }}
                h1 {{ color: #28a745; }}
                p {{ font-size: 1.1em; }}
                a {{ text-decoration: none; color: #3498db; }}
                a:hover {{ text-decoration: underline; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>Успіх!</h1>
                <p>Джерело '{source_data.name}' (ID: {source_id}) успішно додано.</p>
                <p><a href="/admin">Повернутися до адмін-панелі</a></p>
            </div>
        </body>
        </html>
        """
        return HTMLResponse(content=response_content, status_code=200)

    except HTTPException as e:
        error_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Помилка</title>
            <style>
                body {{ font-family: sans-serif; margin: 20px; background-color: #f4f4f4; color: #333; text-align: center; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; background-color: #fff; border-radius: 8px; box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1); }}
                h1 {{ color: #dc3545; }}
                p {{ font-size: 1.1em; }}
                a {{ text-decoration: none; color: #3498db; }}
                a:hover {{ text-decoration: underline; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>Помилка!</h1>
                <p>Помилка при додаванні джерела: {e.detail}</p>
                <p><a href="/admin">Повернутися до адмін-панелі</a></p>
            </div>
        </body>
        </html>
        """
        return HTMLResponse(content=error_content, status_code=e.status_code)
    except Exception as e:
        logger.error(f"Error adding source via web: {e}", exc_info=True)
        error_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Помилка</title>
            <style>
                body {{ font-family: sans-serif; margin: 20px; background-color: #f4f4f4; color: #333; text-align: center; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; background-color: #fff; border-radius: 8px; box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1); }}
                h1 {{ color: #dc3545; }}
                p {{ font-size: 1.1em; }}
                a {{ text-decoration: none; color: #3498db; }}
                a:hover {{ text-decoration: underline; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>Помилка!</h1>
                <p>Виникла невідома помилка при додаванні джерела.</p>
                <p><a href="/admin">Повернутися до адмін-панелі</a></p>
            </div>
        </body>
        </html>
        """
        return HTMLResponse(content=error_content, status_code=500)

async def start_worker_jobs():
    """
    Initializes DB pool and schedules worker jobs.
    This function is now async and will be run by asyncio.run()
    """
    await get_db_pool()
    
    # Retrieve intervals from bot settings or use defaults
    parse_interval_minutes_str = await get_bot_setting("NEWS_PARSE_INTERVAL_MINUTES")
    publish_interval_minutes_str = await get_bot_setting("NEWS_PUBLISH_INTERVAL_MINUTES")

    parse_interval_minutes = int(parse_interval_minutes_str) if parse_interval_minutes_str else 15
    publish_interval_minutes = int(publish_interval_minutes_str) if publish_interval_minutes_str else 5

    logger.info(f"Scheduling news parsing job every {parse_interval_minutes} minutes.")
    scheduler.add_job(parse_all_sources_job, 'interval', minutes=parse_interval_minutes, id='parse_job')
    
    logger.info(f"Scheduling news publishing job every {publish_interval_minutes} minutes.")
    scheduler.add_job(publish_news_to_channel_job, 'interval', minutes=publish_interval_minutes, id='publish_job')
    
    scheduler.start()
    logger.info("Scheduler started.")
    try:
        # Keep the event loop running for the scheduler
        while True:
            await asyncio.sleep(3600) # Sleep for an hour, or indefinitely
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("Bot worker shut down.")


if __name__ == "__main__":
    load_dotenv()
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN environment variable is not set.")
    # Перевіряємо NEWS_CHANNEL_ID після завантаження з config.py
    if not NEWS_CHANNEL_ID or NEWS_CHANNEL_ID == 0:
        logger.warning("NEWS_CHANNEL_ID environment variable is not set or is 0. News will not be posted to a channel.")
    
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        logger.info("Starting bot worker (scheduler)...")
        asyncio.run(start_worker_jobs()) # Run the async worker initialization
    else:
        logger.info("Running FastAPI app locally via uvicorn. Bot polling will not start automatically here.")
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

