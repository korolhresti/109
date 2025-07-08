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

from database import get_db_pool, get_user_by_telegram_id, create_user, update_user_last_active, get_user_language, \
    get_all_active_sources, get_source_by_id, add_news_item, get_unsent_news_items, mark_news_as_sent, \
    add_source, delete_source, get_bot_setting, set_bot_setting, get_source_by_url, update_source_status, \
    update_source_parser_settings, get_all_sources, update_source, get_all_users, toggle_user_auto_notifications, \
    update_user_digest_frequency, get_users_for_digest, get_unsent_digest_news_for_user, get_news_item_by_id, \
    mark_digest_news_as_sent, delete_old_news, get_active_feeds_for_user, add_feed, update_feed, delete_feed, \
    get_feed_by_id, get_all_feeds, get_source_feeds, add_source_to_feed, remove_source_from_feed, \
    get_feed_source_ids, get_user_subscriptions_by_feed, add_user_subscription, remove_user_subscription, \
    get_user_subscription_sources, get_source_stats, create_source_stats_entry, update_source_stats_parsed, \
    update_source_stats_published, get_ai_request_count, increment_ai_request_count, reset_ai_request_count, \
    get_all_source_categories, get_unique_languages, set_user_preferred_language, get_user_preferred_language, \
    get_sources_by_category_and_language, get_user_by_id, set_user_premium_status, get_premium_users, \
    set_user_pro_status, get_pro_users, get_all_news_with_source_info, delete_news_item, \
    get_user_feedback_settings, save_user_feedback_settings, log_user_action

from web_parser import fetch_recent_news_from_source, parse_single_article
from config import TELEGRAM_BOT_TOKEN, ADMIN_TELEGRAM_ID, WEB_APP_URL, NEWS_CHANNEL_ID, API_KEY_NAME, API_KEY, \
    DATABASE_URL

# --- Logging setup ---
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Stream handler for console output
stream_handler = logging.StreamHandler()
stream_handler.setLevel(logging.INFO)
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

# File handler for logging to a file (optional, useful for debugging)
# file_handler = logging.handlers.RotatingFileHandler('bot.log', maxBytes=10485760, backupCount=5)
# file_handler.setLevel(logging.INFO)
# file_handler.setFormatter(formatter)
# logger.addHandler(file_handler)
# --- End Logging setup ---

# Initialize Bot and Dispatcher
bot = None
dp = None
try:
    if TELEGRAM_BOT_TOKEN:
        default_props = DefaultBotProperties(parse_mode=ParseMode.HTML)
        bot = Bot(token=TELEGRAM_BOT_TOKEN, default=default_props)
        dp = Dispatcher()
        # Initialize main router
        main_router = Router()
        dp.include_router(main_router)
        logger.info("Bot and Dispatcher initialized successfully.")
    else:
        logger.error("TELEGRAM_BOT_TOKEN is not set. Bot cannot be initialized.")
        # No raise here, as the app might still run for web panel even without bot functionality
except Exception as e:
    logger.critical(f"CRITICAL ERROR: Failed to initialize Bot or Dispatcher at global scope: {e}", exc_info=True)
    # Re-raise here to prevent the app from starting if bot initialization fails critically
    raise

# FastAPI app
app = FastAPI(
    title="News Bot Web App",
    description="Web interface for managing news sources and bot settings.",
    version="1.0.0",
)

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")


# API Key dependency for secure access to admin endpoints
api_key_header = APIKeyHeader(name=API_KEY_NAME)


async def get_api_key(api_key: str = Depends(api_key_header)):
    if api_key == API_KEY:
        return api_key
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid API Key",
    )


# --- Bot Settings States ---
class BotSettingsStates(StatesGroup):
    waiting_for_setting_key = State()
    waiting_for_setting_value = State()


# --- Admin States ---
class AdminStates(StatesGroup):
    waiting_for_source_url = State()
    waiting_for_source_details = State()
    waiting_for_source_to_delete = State()
    waiting_for_parser_settings = State()
    waiting_for_edit_source_id = State()
    waiting_for_edit_source_field = State()
    waiting_for_edit_source_value = State()
    waiting_for_news_delete_id = State()
    waiting_for_user_id_for_premium = State()
    waiting_for_premium_expiry_date = State()
    waiting_for_user_id_for_pro = State()


# --- User Settings States ---
class UserSettingsStates(StatesGroup):
    waiting_for_digest_frequency = State()
    waiting_for_preferred_language = State()


# --- Feed Management States ---
class FeedStates(StatesGroup):
    waiting_for_feed_name = State()
    waiting_for_feed_to_edit_id = State()
    waiting_for_feed_new_name = State()
    waiting_for_feed_to_delete_id = State()
    waiting_for_feed_add_source_feed_id = State()
    waiting_for_feed_add_source_source_id = State()
    waiting_for_feed_remove_source_feed_id = State()
    waiting_for_feed_remove_source_source_id = State()
    waiting_for_feed_subscribe_id = State()
    waiting_for_feed_unsubscribe_id = State()


# --- Helper function to check admin status ---
async def is_admin(telegram_id: int) -> bool:
    user = await get_user_by_telegram_id(telegram_id)
    return user is not None and user.get('is_admin', False)


# --- Decorator for admin-only commands ---
def admin_only(func: Callable[[Message, ...], Awaitable[Any]]):
    async def wrapper(message: Message, *args, **kwargs):
        if not await is_admin(message.from_user.id):
            await message.reply("Ви не авторизовані для виконання цієї команди.")
            return
        return await func(message, *args, **kwargs)

    return wrapper


# --- Bot Commands ---

@main_router.message(CommandStart())
async def command_start_handler(message: Message) -> None:
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    last_name = message.from_user.last_name

    try:
        user = await get_user_by_telegram_id(user_id)
        if not user:
            await create_user(user_id, username, first_name, last_name)
            logger.info(f"New user created: {username} ({user_id})")
            await message.answer(
                f"Привіт, {first_name}! Я бот для новин. Ви можете використовувати мене, щоб отримувати останні новини."
            )
        else:
            await update_user_last_active(user_id)
            await message.answer(
                f"З поверненням, {first_name}! Ви можете використовувати /help для перегляду доступних команд."
            )
        await log_user_action(user_id, "start_command")
    except Exception as e:
        logger.error(f"Error in command_start_handler for user {user_id}: {e}", exc_info=True)
        await message.answer("Сталася помилка при запуску бота. Будь ласка, спробуйте пізніше.")


@main_router.message(Command("help"))
async def command_help_handler(message: Message) -> None:
    help_text = (
        "<b>Доступні команди:</b>\n"
        "/start - Запуск бота\n"
        "/help - Показати це повідомлення\n"
        "/settings - Налаштування користувача (мова, дайджести)\n"
        "/sources - Показати доступні джерела новин\n"
        "/subscribe - Підписатися на джерело новин\n"
        "/unsubscribe - Відписатися від джерела новин\n"
        "/my_subscriptions - Показати ваші підписки\n"
        "/feeds - Керування вашими стрічками новин\n"
        "/latest_news - Отримати останні новини з ваших підписок\n"
        "/ai_assistant - Задати питання AI-асистенту (тільки для PRO користувачів)\n"
        "/support - Зв'язатися зі службою підтримки\n"
        "/feedback - Надати відгук або повідомити про проблему\n"
    )
    if await is_admin(message.from_user.id):
        help_text += (
            "\n<b>Адмін команди:</b>\n"
            "/admin - Показати адмін-панель\n"
            "/add_source - Додати нове джерело новин\n"
            "/delete_source - Видалити джерело новин\n"
            "/list_sources - Показати всі джерела (включаючи неактивні)\n"
            "/update_source - Оновити джерело новин\n"
            "/set_parser_settings - Встановити налаштування парсера для джерела\n"
            "/list_users - Показати всіх користувачів\n"
            "/delete_news - Видалити новину за ID\n"
            "/view_all_news - Переглянути всі новини в базі даних\n"
            "/bot_settings - Керування налаштуваннями бота\n"
            "/set_premium - Встановити преміум-статус користувачеві\n"
            "/set_pro - Встановити PRO-статус користувачеві\n"
            "/run_parse_job - Запустити завдання парсингу новин вручну\n"
            "/run_publish_job - Запустити завдання публікації новин вручну\n"
        )
    await message.answer(help_text)
    await log_user_action(message.from_user.id, "help_command")


@main_router.message(Command("admin"))
@admin_only
async def admin_panel(message: Message):
    admin_panel_text = (
        "<b>Адмін-панель:</b>\n"
        "Виберіть дію:\n"
        "/add_source - Додати нове джерело новин\n"
        "/delete_source - Видалити джерело новин\n"
        "/list_sources - Показати всі джерела (включаючи неактивні)\n"
        "/update_source - Оновити джерело новин\n"
        "/set_parser_settings - Встановити налаштування парсера для джерела\n"
        "/list_users - Показати всіх користувачів\n"
        "/delete_news - Видалити новину за ID\n"
        "/view_all_news - Переглянути всі новини в базі даних\n"
        "/bot_settings - Керування налаштуваннями бота\n"
        "/set_premium - Встановити преміум-статус користувачеві\n"
        "/set_pro - Встановити PRO-статус користувачеві\n"
        "/run_parse_job - Запустити завдання парсингу новин вручну\n"
        "/run_publish_job - Запустити завдання публікації новин вручну\n"
    )
    await message.answer(admin_panel_text)
    await log_user_action(message.from_user.id, "admin_command")


@main_router.message(Command("add_source"))
@admin_only
async def add_source_command(message: Message, state: FSMContext):
    await message.answer("Будь ласка, введіть URL нового джерела новин:")
    await state.set_state(AdminStates.waiting_for_source_url)
    await log_user_action(message.from_user.id, "add_source_command")


@main_router.message(AdminStates.waiting_for_source_url)
@admin_only
async def process_source_url(message: Message, state: FSMContext):
    source_url = message.text
    if not source_url or not (source_url.startswith('http://') or source_url.startswith('https://')):
        await message.answer("Некоректний URL. Будь ласка, введіть дійсний URL.")
        return

    existing_source = await get_source_by_url(source_url)
    if existing_source:
        await message.answer(f"Джерело з URL '{source_url}' вже існує (ID: {existing_source['id']}).")
        await state.clear()
        return

    await state.update_data(source_url=source_url)
    await message.answer(
        "Тепер введіть назву джерела, категорію, мову (наприклад, 'Українська'), статус (active/inactive), "
        "тип (rss/html), та парсер-селектори для заголовка, URL, дати, вмісту, зображення (у форматі JSON). "
        "Розділіть їх комами. Якщо парсер-селектори відсутні, введіть {}.\n\n"
        "Формат: Назва, Категорія, Мова, Статус, Тип, JSON_Парсер_Селектори\n"
        "Приклад: Моє Джерело, Технології, Українська, active, html, {\"title\": \"h1.title\", \"url\": \"a.link\", ...}\n"
        "Якщо для парсера не потрібно селекторів (наприклад, для RSS), введіть {}."
    )
    await state.set_state(AdminStates.waiting_for_source_details)


@main_router.message(AdminStates.waiting_for_source_details)
@admin_only
async def process_source_details(message: Message, state: FSMContext):
    try:
        details_text = message.text
        parts = details_text.split(',', 5)  # Split by comma, max 5 splits to get the JSON part intact

        if len(parts) < 5:
            await message.answer(
                "Некоректний формат. Будь ласка, переконайтеся, що ви ввели всі 5 частин, включаючи JSON для парсер-селекторів. Якщо для парсера не потрібно селекторів (наприклад, для RSS), введіть {}."
            )
            return

        name = parts[0].strip()
        category = parts[1].strip()
        language = parts[2].strip()
        status = parts[3].strip().lower()
        source_type = parts[4].strip().lower()

        if status not in ['active', 'inactive']:
            await message.answer("Некоректний статус. Будь ласка, використовуйте 'active' або 'inactive'.")
            return
        if source_type not in ['rss', 'html']:
            await message.answer("Некоректний тип джерела. Будь ласка, використовуйте 'rss' або 'html'.")
            return

        parser_settings = {}
        if len(parts) == 6:
            try:
                parser_settings = json.loads(parts[5].strip())
            except json.JSONDecodeError:
                await message.answer("Некоректний формат JSON для парсер-селекторів. Будь ласка, спробуйте ще раз.")
                return

        user_data = await state.get_data()
        source_url = user_data['source_url']

        new_source_id = await add_source(
            url=source_url,
            name=name,
            category=category,
            language=language,
            status=status,
            source_type=source_type,
            parser_settings=parser_settings
        )
        if new_source_id:
            await message.answer(f"Джерело '{name}' (ID: {new_source_id}) успішно додано!")
        else:
            await message.answer("Не вдалося додати джерело. Можливо, воно вже існує або сталася помилка.")
    except Exception as e:
        logger.error(f"Error processing source details: {e}", exc_info=True)
        await message.answer(f"Сталася помилка при додаванні джерела: {e}. Будь ласка, спробуйте ще раз.")
    finally:
        await state.clear()


@main_router.message(Command("delete_source"))
@admin_only
async def delete_source_command(message: Message, state: FSMContext):
    sources = await get_all_sources()
    if not sources:
        await message.answer("Немає доступних джерел для видалення.")
        await state.clear()
        return

    source_list_text = "<b>Доступні джерела для видалення:</b>\n"
    for s in sources:
        source_list_text += f"ID: {s['id']}, Назва: {s['name']}, URL: {s['url']}, Статус: {s['status']}\n"
    source_list_text += "\nБудь ласка, введіть ID джерела, яке ви хочете видалити:"
    await message.answer(source_list_text)
    await state.set_state(AdminStates.waiting_for_source_to_delete)
    await log_user_action(message.from_user.id, "delete_source_command")


@main_router.message(AdminStates.waiting_for_source_to_delete)
@admin_only
async def process_delete_source(message: Message, state: FSMContext):
    try:
        source_id = int(message.text.strip())
        source = await get_source_by_id(source_id)
        if not source:
            await message.answer(f"Джерело з ID {source_id} не знайдено.")
            await state.clear()
            return

        deleted = await delete_source(source_id)
        if deleted:
            await message.answer(f"Джерело '{source['name']}' (ID: {source_id}) успішно видалено.")
        else:
            await message.answer(f"Не вдалося видалити джерело з ID {source_id}.")
    except ValueError:
        await message.answer("Некоректний ID джерела. Будь ласка, введіть числове значення.")
    except Exception as e:
        logger.error(f"Error deleting source: {e}", exc_info=True)
        await message.answer(f"Сталася помилка при видаленні джерела: {e}.")
    finally:
        await state.clear()


@main_router.message(Command("list_sources"))
@admin_only
async def list_sources_command(message: Message):
    sources = await get_all_sources()
    if not sources:
        await message.answer("Наразі немає доданих джерел новин.")
        return

    response_text = "<b>Додані джерела новин:</b>\n\n"
    for s in sources:
        parser_settings_str = json.dumps(s.get('parser_settings', {}), ensure_ascii=False)
        response_text += (
            f"ID: {s['id']}\n"
            f"  Назва: {s['name']}\n"
            f"  URL: {s['url']}\n"
            f"  Категорія: {s['category']}\n"
            f"  Мова: {s['language']}\n"
            f"  Статус: {s['status']}\n"
            f"  Тип: {s['source_type']}\n"
            f"  Парсер: {parser_settings_str}\n"
            f"  Додано: {s['created_at'].strftime('%Y-%m-%d %H:%M')}\n\n"
        )
    await message.answer(response_text)
    await log_user_action(message.from_user.id, "list_sources_command")


@main_router.message(Command("update_source"))
@admin_only
async def update_source_command(message: Message, state: FSMContext):
    sources = await get_all_sources()
    if not sources:
        await message.answer("Немає доступних джерел для оновлення.")
        await state.clear()
        return

    source_list_text = "<b>Доступні джерела для оновлення:</b>\n"
    for s in sources:
        source_list_text += f"ID: {s['id']}, Назва: {s['name']}, URL: {s['url']}\n"
    source_list_text += "\nБудь ласка, введіть ID джерела, яке ви хочете оновити:"
    await message.answer(source_list_text)
    await state.set_state(AdminStates.waiting_for_edit_source_id)
    await log_user_action(message.from_user.id, "update_source_command")


@main_router.message(AdminStates.waiting_for_edit_source_id)
@admin_only
async def process_edit_source_id(message: Message, state: FSMContext):
    try:
        source_id = int(message.text.strip())
        source = await get_source_by_id(source_id)
        if not source:
            await message.answer(f"Джерело з ID {source_id} не знайдено.")
            await state.clear()
            return

        await state.update_data(current_source_id=source_id)
        keyboard = InlineKeyboardBuilder()
        keyboard.add(InlineKeyboardButton(text="Назва", callback_data="update_source_name"))
        keyboard.add(InlineKeyboardButton(text="URL", callback_data="update_source_url"))
        keyboard.add(InlineKeyboardButton(text="Категорія", callback_data="update_source_category"))
        keyboard.add(InlineKeyboardButton(text="Мова", callback_data="update_source_language"))
        keyboard.add(InlineKeyboardButton(text="Статус", callback_data="update_source_status"))
        keyboard.add(InlineKeyboardButton(text="Тип", callback_data="update_source_type"))
        keyboard.add(InlineKeyboardButton(text="Парсер", callback_data="update_source_parser_settings"))
        await message.answer(
            f"Вибрано джерело: {source['name']} (ID: {source_id}). Яке поле ви хочете оновити?",
            reply_markup=keyboard.as_markup()
        )
        await state.set_state(AdminStates.waiting_for_edit_source_field)

    except ValueError:
        await message.answer("Некоректний ID джерела. Будь ласка, введіть числове значення.")
        await state.clear()
    except Exception as e:
        logger.error(f"Error in process_edit_source_id: {e}", exc_info=True)
        await message.answer(f"Сталася помилка: {e}.")
        await state.clear()


@main_router.callback_query(F.data.startswith("update_source_"), StateFilter(AdminStates.waiting_for_edit_source_field))
@admin_only
async def process_edit_source_field_callback(callback_query: CallbackQuery, state: FSMContext):
    field_name = callback_query.data.replace("update_source_", "")
    await state.update_data(field_to_update=field_name)
    await callback_query.message.answer(f"Будь ласка, введіть нове значення для поля '{field_name}':")
    await state.set_state(AdminStates.waiting_for_edit_source_value)
    await callback_query.answer()


@main_router.message(AdminStates.waiting_for_edit_source_value)
@admin_only
async def process_edit_source_value(message: Message, state: FSMContext):
    try:
        user_data = await state.get_data()
        source_id = user_data['current_source_id']
        field_name = user_data['field_to_update']
        new_value = message.text.strip()

        if field_name == "parser_settings":
            try:
                new_value = json.loads(new_value)
            except json.JSONDecodeError:
                await message.answer("Некоректний формат JSON для парсер-селекторів. Будь ласка, спробуйте ще раз.")
                return

        updated = await update_source(source_id, {field_name: new_value})
        if updated:
            await message.answer(f"Джерело (ID: {source_id}) успішно оновлено. Поле '{field_name}' змінено на '{new_value}'.")
        else:
            await message.answer(f"Не вдалося оновити джерело (ID: {source_id}).")

    except Exception as e:
        logger.error(f"Error in process_edit_source_value: {e}", exc_info=True)
        await message.answer(f"Сталася помилка при оновленні джерела: {e}. Будь ласка, спробуйте ще раз.")
    finally:
        await state.clear()


@main_router.message(Command("set_parser_settings"))
@admin_only
async def set_parser_settings_command(message: Message, state: FSMContext):
    sources = await get_all_sources()
    if not sources:
        await message.answer("Немає доступних джерел для налаштування парсера.")
        await state.clear()
        return

    source_list_text = "<b>Доступні джерела для налаштування парсера:</b>\n"
    for s in sources:
        source_list_text += f"ID: {s['id']}, Назва: {s['name']}, URL: {s['url']}\n"
    source_list_text += "\nБудь ласка, введіть ID джерела, для якого ви хочете встановити налаштування парсера:"
    await message.answer(source_list_text)
    await state.set_state(AdminStates.waiting_for_parser_settings)
    await log_user_action(message.from_user.id, "set_parser_settings_command")


@main_router.message(AdminStates.waiting_for_parser_settings)
@admin_only
async def process_parser_settings_source_id(message: Message, state: FSMContext):
    try:
        source_id = int(message.text.strip())
        source = await get_source_by_id(source_id)
    except ValueError:
        await message.answer("Некоректний ID джерела. Будь ласка, введіть числове значення.")
        await state.clear()
        return

    if not source:
        await message.answer(f"Джерело з ID {source_id} не знайдено.")
        await state.clear()
        return

    await state.update_data(parser_settings_source_id=source_id)
    current_settings = source.get('parser_settings', {})
    await message.answer(
        f"Вибрано джерело: {source['name']} (ID: {source_id}).\n"
        f"Поточні налаштування парсера: <code>{json.dumps(current_settings, ensure_ascii=False, indent=2)}</code>\n\n"
        "Будь ласка, введіть нові налаштування парсера у форматі JSON (наприклад, "
        "<code>{\"title\": \"h1.entry-title\", \"content\": \"div.entry-content p\", \"image\": \"img.main-image@src\"}</code>):"
    )
    await state.set_state(AdminStates.waiting_for_parser_settings)  # залишаємо той самий стан, але з іншою логікою


@main_router.message(StateFilter(AdminStates.waiting_for_parser_settings), lambda message: message.text.startswith('{'))
@admin_only
async def process_parser_settings_json(message: Message, state: FSMContext):
    try:
        parser_settings_json = message.text.strip()
        parser_settings = json.loads(parser_settings_json)
        user_data = await state.get_data()
        source_id = user_data['parser_settings_source_id']

        updated = await update_source_parser_settings(source_id, parser_settings)
        if updated:
            await message.answer(f"Налаштування парсера для джерела (ID: {source_id}) успішно оновлено.")
        else:
            await message.answer(f"Не вдалося оновити налаштування парсера для джерела (ID: {source_id}).")
    except json.JSONDecodeError:
        await message.answer("Некоректний формат JSON. Будь ласка, введіть дійсний JSON.")
    except Exception as e:
        logger.error(f"Error processing parser settings JSON: {e}", exc_info=True)
        await message.answer(f"Сталася помилка при оновленні налаштувань парсера: {e}. Будь ласка, спробуйте ще раз.")
    finally:
        await state.clear()


@main_router.message(Command("list_users"))
@admin_only
async def list_users_command(message: Message):
    users = await get_all_users()
    if not users:
        await message.answer("Немає зареєстрованих користувачів.")
        return

    response_text = "<b>Зареєстровані користувачі:</b>\n\n"
    for user in users:
        response_text += (
            f"ID: {user['id']} (Telegram ID: {user['telegram_id']})\n"
            f"  Ім'я: {user['first_name']} {user['last_name']} (@{user['username']})\n"
            f"  Адмін: {'Так' if user['is_admin'] else 'Ні'}\n"
            f"  Авто-сповіщення: {'Так' if user['auto_notifications'] else 'Ні'}\n"
            f"  Частота дайджестів: {user['digest_frequency']}\n"
            f"  PRO: {'Так' if user['is_pro'] else 'Ні'}\n"
            f"  Premium: {'Так' if user['is_premium'] else 'Ні'}"
            f"{f' (до {user["premium_expires_at"].strftime("%Y-%m-%d")})' if user['premium_expires_at'] else ''}\n"
            f"  Мова: {user['language']}\n"
            f"  Уподобана мова перекладу: {user['preferred_language']}\n"
            f"  Останній запит AI: {user.get('ai_last_request_date', 'N/A')}, Запитів сьогодні: {user.get('ai_requests_today', 0)}\n"
            f"  Створено: {user['created_at'].strftime('%Y-%m-%d %H:%M')}\n\n"
        )
    await message.answer(response_text)
    await log_user_action(message.from_user.id, "list_users_command")


@main_router.message(Command("set_premium"))
@admin_only
async def set_premium_command(message: Message, state: FSMContext):
    await message.answer("Введіть Telegram ID користувача, якому потрібно встановити або зняти преміум-статус:")
    await state.set_state(AdminStates.waiting_for_user_id_for_premium)
    await log_user_action(message.from_user.id, "set_premium_command")


@main_router.message(AdminStates.waiting_for_user_id_for_premium)
@admin_only
async def process_user_id_for_premium(message: Message, state: FSMContext):
    try:
        target_telegram_id = int(message.text.strip())
        user = await get_user_by_telegram_id(target_telegram_id)
        if not user:
            await message.answer("Користувача з таким Telegram ID не знайдено.")
            await state.clear()
            return

        await state.update_data(target_user_telegram_id=target_telegram_id)

        keyboard = InlineKeyboardBuilder()
        keyboard.add(InlineKeyboardButton(text="Надати Premium", callback_data="set_premium_true"))
        keyboard.add(InlineKeyboardButton(text="Зняти Premium", callback_data="set_premium_false"))
        await message.answer(
            f"Вибрано користувача {user['first_name']} (@{user['username'] if user['username'] else 'N/A'}). "
            f"Поточний Premium статус: {'Так' if user['is_premium'] else 'Ні'}. "
            "Що ви хочете зробити?",
            reply_markup=keyboard.as_markup()
        )
        await state.set_state(AdminStates.waiting_for_premium_expiry_date) # Re-use this state for callback handling

    except ValueError:
        await message.answer("Некоректний Telegram ID. Будь ласка, введіть числове значення.")
        await state.clear()
    except Exception as e:
        logger.error(f"Error in process_user_id_for_premium: {e}", exc_info=True)
        await message.answer(f"Сталася помилка: {e}.")
        await state.clear()


@main_router.callback_query(F.data.in_({"set_premium_true", "set_premium_false"}), StateFilter(AdminStates.waiting_for_premium_expiry_date))
@admin_only
async def process_set_premium_callback(callback_query: CallbackQuery, state: FSMContext):
    set_status = callback_query.data == "set_premium_true"
    user_data = await state.get_data()
    target_telegram_id = user_data['target_user_telegram_id']

    if set_status:
        await callback_query.message.answer(
            "Будь ласка, введіть термін дії преміум-статусу у форматіYYYY-MM-DD (наприклад, 2025-12-31). "
            "Якщо преміум назавжди, введіть 'назавжди'."
        )
        # Keep state to wait for date
    else:
        await set_user_premium_status(target_telegram_id, False, None)
        await callback_query.message.answer(f"Преміум-статус для користувача (ID: {target_telegram_id}) знято.")
        await state.clear()
    await callback_query.answer()


@main_router.message(AdminStates.waiting_for_premium_expiry_date)
@admin_only
async def process_premium_expiry_date(message: Message, state: FSMContext):
    try:
        user_data = await state.get_data()
        target_telegram_id = user_data['target_user_telegram_id']
        expiry_input = message.text.strip().lower()

        expiry_date = None
        if expiry_input == "назавжди":
            expiry_date = None  # Represent "forever" as None in premium_expires_at
        else:
            try:
                expiry_date = datetime.strptime(expiry_input, '%Y-%m-%d').date()
            except ValueError:
                await message.answer("Некоректний формат дати. Будь ласка, введітьYYYY-MM-DD або 'назавжди'.")
                return

        await set_user_premium_status(target_telegram_id, True, expiry_date)
        if expiry_date:
            await message.answer(f"Преміум-статус для користувача (ID: {target_telegram_id}) встановлено до {expiry_date}.")
        else:
            await message.answer(f"Преміум-статус для користувача (ID: {target_telegram_id}) встановлено назавжди.")

    except Exception as e:
        logger.error(f"Error in process_premium_expiry_date: {e}", exc_info=True)
        await message.answer(f"Сталася помилка: {e}.")
    finally:
        await state.clear()


@main_router.message(Command("set_pro"))
@admin_only
async def set_pro_command(message: Message, state: FSMContext):
    await message.answer("Введіть Telegram ID користувача, якому потрібно встановити або зняти PRO-статус:")
    await state.set_state(AdminStates.waiting_for_user_id_for_pro)
    await log_user_action(message.from_user.id, "set_pro_command")


@main_router.message(AdminStates.waiting_for_user_id_for_pro)
@admin_only
async def process_user_id_for_pro(message: Message, state: FSMContext):
    try:
        target_telegram_id = int(message.text.strip())
        user = await get_user_by_telegram_id(target_telegram_id)
        if not user:
            await message.answer("Користувача з таким Telegram ID не знайдено.")
            await state.clear()
            return

        await state.update_data(target_user_telegram_id=target_telegram_id)

        keyboard = InlineKeyboardBuilder()
        keyboard.add(InlineKeyboardButton(text="Надати PRO", callback_data="set_pro_true"))
        keyboard.add(InlineKeyboardButton(text="Зняти PRO", callback_data="set_pro_false"))
        await message.answer(
            f"Вибрано користувача {user['first_name']} (@{user['username'] if user['username'] else 'N/A'}). "
            f"Поточний PRO статус: {'Так' if user['is_pro'] else 'Ні'}. "
            "Що ви хочете зробити?",
            reply_markup=keyboard.as_markup()
        )
        await state.set_state(AdminStates.waiting_for_user_id_for_pro) # Keep state for callback
    except ValueError:
        await message.answer("Некоректний Telegram ID. Будь ласка, введіть числове значення.")
        await state.clear()
    except Exception as e:
        logger.error(f"Error in process_user_id_for_pro: {e}", exc_info=True)
        await message.answer(f"Сталася помилка: {e}.")
        await state.clear()


@main_router.callback_query(F.data.in_({"set_pro_true", "set_pro_false"}), StateFilter(AdminStates.waiting_for_user_id_for_pro))
@admin_only
async def process_set_pro_callback(callback_query: CallbackQuery, state: FSMContext):
    set_status = callback_query.data == "set_pro_true"
    user_data = await state.get_data()
    target_telegram_id = user_data['target_user_telegram_id']

    await set_user_pro_status(target_telegram_id, set_status)
    status_text = "встановлено" if set_status else "знято"
    await callback_query.message.answer(f"PRO-статус для користувача (ID: {target_telegram_id}) {status_text}.")
    await state.clear()
    await callback_query.answer()


@main_router.message(Command("delete_news"))
@admin_only
async def delete_news_command(message: Message, state: FSMContext):
    await message.answer("Будь ласка, введіть ID новини, яку ви хочете видалити:")
    await state.set_state(AdminStates.waiting_for_news_delete_id)
    await log_user_action(message.from_user.id, "delete_news_command")


@main_router.message(AdminStates.waiting_for_news_delete_id)
@admin_only
async def process_delete_news_by_id(message: Message, state: FSMContext):
    try:
        news_id = int(message.text.strip())
        news_item = await get_news_item_by_id(news_id)
        if not news_item:
            await message.answer(f"Новину з ID {news_id} не знайдено.")
            await state.clear()
            return

        deleted = await delete_news_item(news_id)
        if deleted:
            await message.answer(f"Новину '{news_item['title']}' (ID: {news_id}) успішно видалено.")
        else:
            await message.answer(f"Не вдалося видалити новину з ID {news_id}.")
    except ValueError:
        await message.answer("Некоректний ID новини. Будь ласка, введіть числове значення.")
    except Exception as e:
        logger.error(f"Error deleting news item: {e}", exc_info=True)
        await message.answer(f"Сталася помилка при видаленні новини: {e}.")
    finally:
        await state.clear()


@main_router.message(Command("view_all_news"))
@admin_only
async def view_all_news_command(message: Message):
    all_news = await get_all_news_with_source_info()
    if not all_news:
        await message.answer("Наразі немає новин у базі даних.")
        return

    response_parts = ["<b>Всі новини в базі даних:</b>\n\n"]
    for news_item in all_news:
        title = news_item.get('title', 'Без заголовка')
        source_name = news_item.get('source_name', 'Невідоме джерело')
        published_at_str = news_item['published_at'].strftime('%Y-%m-%d %H:%M') if news_item['published_at'] else 'N/A'
        is_sent_str = 'Так' if news_item['is_sent'] else 'Ні'

        news_text = (
            f"ID: {news_item['id']}\n"
            f"  Заголовок: {title}\n"
            f"  Джерело: {source_name}\n"
            f"  URL: {news_item.get('source_url', 'N/A')}\n"
            f"  Опубліковано: {published_at_str}\n"
            f"  Відправлено в канал: {is_sent_str}\n\n"
        )
        if len("".join(response_parts) + news_text) > 4096:  # Telegram message limit
            await message.answer("".join(response_parts))
            response_parts = [news_text]
        else:
            response_parts.append(news_text)

    await message.answer("".join(response_parts))
    await log_user_action(message.from_user.id, "view_all_news_command")


@main_router.message(Command("bot_settings"))
@admin_only
async def bot_settings_command(message: Message, state: FSMContext):
    keyboard = InlineKeyboardBuilder()
    keyboard.add(InlineKeyboardButton(text="Встановити налаштування", callback_data="set_bot_setting"))
    await message.answer(
        "<b>Налаштування бота:</b>\n"
        "Ви можете керувати глобальними налаштуваннями бота.\n"
        "Виберіть дію:",
        reply_markup=keyboard.as_markup()
    )
    await log_user_action(message.from_user.id, "bot_settings_command")


@main_router.callback_query(F.data == "set_bot_setting")
@admin_only
async def prompt_for_setting_key(callback_query: CallbackQuery, state: FSMContext):
    await callback_query.message.answer("Будь ласка, введіть ключ налаштування (наприклад, NEWS_PARSE_INTERVAL_MINUTES):")
    await state.set_state(BotSettingsStates.waiting_for_setting_key)
    await callback_query.answer()


@main_router.message(BotSettingsStates.waiting_for_setting_key)
@admin_only
async def process_setting_key(message: Message, state: FSMContext):
    setting_key = message.text.strip().upper()
    await state.update_data(setting_key=setting_key)
    current_value = await get_bot_setting(setting_key)
    await message.answer(
        f"Поточне значення для '{setting_key}': '{current_value if current_value is not None else 'Не встановлено'}'.\n"
        "Будь ласка, введіть нове значення для цього налаштування:"
    )
    await state.set_state(BotSettingsStates.waiting_for_setting_value)


@main_router.message(BotSettingsStates.waiting_for_setting_value)
@admin_only
async def process_setting_value(message: Message, state: FSMContext):
    try:
        user_data = await state.get_data()
        setting_key = user_data['setting_key']
        setting_value = message.text.strip()

        # Validate specific settings if necessary
        if "INTERVAL_MINUTES" in setting_key:
            try:
                int_val = int(setting_value)
                if int_val <= 0:
                    await message.answer("Інтервал повинен бути позитивним числом. Будь ласка, спробуйте ще раз.")
                    return
            except ValueError:
                await message.answer("Значення інтервалу повинно бути числом. Будь ласка, спробуйте ще раз.")
                return

        await set_bot_setting(setting_key, setting_value)
        await message.answer(f"Налаштування '{setting_key}' успішно оновлено до '{setting_value}'.")
    except Exception as e:
        logger.error(f"Error setting bot setting: {e}", exc_info=True)
        await message.answer(f"Сталася помилка при оновленні налаштування: {e}. Будь ласка, спробуйте ще раз.")
    finally:
        await state.clear()


@main_router.message(Command("sources"))
async def list_user_sources(message: Message):
    active_sources = await get_all_active_sources()
    if not active_sources:
        await message.answer("Наразі немає доступних джерел новин.")
        return

    response_text = "<b>Доступні джерела новин:</b>\n\n"
    for s in active_sources:
        response_text += (
            f"ID: {s['id']}\n"
            f"  Назва: {s['name']}\n"
            f"  URL: {s['url']}\n"
            f"  Категорія: {s['category']}\n"
            f"  Мова: {s['language']}\n\n"
        )
    await message.answer(response_text)
    await log_user_action(message.from_user.id, "sources_command")


@main_router.message(Command("subscribe"))
async def subscribe_command(message: Message):
    user_id = message.from_user.id
    active_sources = await get_all_active_sources()
    if not active_sources:
        await message.answer("Наразі немає доступних джерел новин для підписки.")
        return

    user_subscribed_source_ids = {s['source_id'] for s in await get_user_subscription_sources(user_id)}

    keyboard = InlineKeyboardBuilder()
    for s in active_sources:
        if s['id'] not in user_subscribed_source_ids:
            keyboard.add(InlineKeyboardButton(text=s['name'], callback_data=f"subscribe_source_{s['id']}"))
    keyboard.adjust(2)

    if not keyboard.buttons:
        await message.answer("Ви вже підписані на всі доступні джерела.")
        return

    await message.answer("Виберіть джерело для підписки:", reply_markup=keyboard.as_markup())
    await log_user_action(message.from_user.id, "subscribe_command")


@main_router.callback_query(F.data.startswith("subscribe_source_"))
async def process_subscribe_callback(callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    source_id = int(callback_query.data.split('_')[-1])

    source = await get_source_by_id(source_id)
    if not source or source['status'] != 'active':
        await callback_query.message.answer("Вибране джерело не знайдено або неактивне.")
        await callback_query.answer()
        return

    # Check if user is already subscribed to this source
    user_subscriptions = await get_user_subscription_sources(user_id)
    if source_id in {s['source_id'] for s in user_subscriptions}:
        await callback_query.message.answer(f"Ви вже підписані на '{source['name']}'.")
        await callback_query.answer()
        return

    # Add subscription to 'default' feed (feed_id = 1)
    # Ensure default feed exists or handle its creation if necessary
    default_feed = await get_feed_by_id(1)
    if not default_feed:
        # This part should ideally be handled by schema.sql or initial setup
        # For robustness, we can try to create it here, assuming 'default_feed_id' is 1 and owned by ADMIN
        try:
            admin_user = await get_user_by_telegram_id(ADMIN_TELEGRAM_ID)
            if admin_user:
                await add_feed("Default Feed", admin_user['id']) # Assuming admin_user['id'] is the internal user ID
                logger.warning("Default feed (ID 1) was not found and was created. Ensure schema.sql handles this.")
                # You might need to re-fetch default_feed after creation, or ensure ID is 1.
                # For simplicity in this example, we proceed assuming it exists after initial setup.
        except Exception as e:
            logger.error(f"Could not ensure default feed exists: {e}")


    success = await add_user_subscription(user_id, source_id, 1) # Assign to default feed
    if success:
        await callback_query.message.answer(f"Ви успішно підписалися на '{source['name']}'!")
    else:
        await callback_query.message.answer(f"Не вдалося підписатися на '{source['name']}'.")
    await callback_query.answer()


@main_router.message(Command("unsubscribe"))
async def unsubscribe_command(message: Message):
    user_id = message.from_user.id
    user_subscriptions = await get_user_subscription_sources(user_id)
    if not user_subscriptions:
        await message.answer("Ви не підписані на жодне джерело.")
        return

    keyboard = InlineKeyboardBuilder()
    for s in user_subscriptions:
        source = await get_source_by_id(s['source_id'])
        if source:
            keyboard.add(InlineKeyboardButton(text=source['name'], callback_data=f"unsubscribe_source_{s['source_id']}"))
    keyboard.adjust(2)

    await message.answer("Виберіть джерело для відписки:", reply_markup=keyboard.as_markup())
    await log_user_action(message.from_user.id, "unsubscribe_command")


@main_router.callback_query(F.data.startswith("unsubscribe_source_"))
async def process_unsubscribe_callback(callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    source_id = int(callback_query.data.split('_')[-1])

    source = await get_source_by_id(source_id)
    if not source:
        await callback_query.message.answer("Вибране джерело не знайдено.")
        await callback_query.answer()
        return

    success = await remove_user_subscription(user_id, source_id) # Assumes subscription is for default feed (ID 1)
    if success:
        await callback_query.message.answer(f"Ви успішно відписалися від '{source['name']}'.")
    else:
        await callback_query.message.answer(f"Не вдалося відписатися від '{source['name']}'.")
    await callback_query.answer()


@main_router.message(Command("my_subscriptions"))
async def my_subscriptions_command(message: Message):
    user_id = message.from_user.id
    user_subscriptions = await get_user_subscription_sources(user_id)

    if not user_subscriptions:
        await message.answer("Ви не підписані на жодне джерело.")
        return

    response_text = "<b>Ваші підписки:</b>\n\n"
    for s in user_subscriptions:
        source = await get_source_by_id(s['source_id'])
        if source:
            response_text += f"- {source['name']} ({source['url']})\n"
    await message.answer(response_text)
    await log_user_action(message.from_user.id, "my_subscriptions_command")


@main_router.message(Command("latest_news"))
async def latest_news_command(message: Message):
    user_id = message.from_user.id
    subscriptions = await get_user_subscription_sources(user_id)
    if not subscriptions:
        await message.answer("Ви не підписані на жодне джерело. Використовуйте /subscribe, щоб підписатися.")
        return

    news_found = False
    for sub in subscriptions:
        source = await get_source_by_id(sub['source_id'])
        if source and source['status'] == 'active':
            logger.info(f"Fetching latest news for user {user_id} from source {source['name']} ({source['url']})")
            # For latest_news command, we fetch directly from the source to get the absolute latest
            # and don't rely on the 'is_sent' flag in the database for *this* command.
            # We fetch a small limit to not overwhelm the user.
            try:
                recent_news_items = await fetch_recent_news_from_source(source['url'], source.get('parser_settings', {}), limit=3)
                if recent_news_items:
                    news_found = True
                    await message.answer(f"<b>Останні новини з {source['name']}:</b>")
                    for news_item in recent_news_items:
                        title = news_item.get('title', 'Без заголовка')
                        url = news_item.get('source_url', '#')
                        published_at = news_item.get('published_at')
                        published_at_str = published_at.strftime('%Y-%m-%d %H:%M') if published_at else 'N/A'
                        news_text = (
                            f"➡️ {hlink(title, url)}\n"
                            f"📅 {published_at_str}"
                        )
                        await message.answer(news_text)
            except Exception as e:
                logger.error(f"Error fetching latest news for user {user_id} from {source['name']}: {e}", exc_info=True)
                await message.answer(f"Не вдалося отримати новини з {source['name']}.")

    if not news_found:
        await message.answer("Немає нових новин з ваших підписок.")
    await log_user_action(user_id, "latest_news_command")


@main_router.message(Command("settings"))
async def user_settings_command(message: Message):
    user_id = message.from_user.id
    user = await get_user_by_telegram_id(user_id)
    if not user:
        await message.answer("Користувача не знайдено. Будь ласка, спробуйте /start.")
        return

    auto_notifications_status = "Увімкнено" if user['auto_notifications'] else "Вимкнено"
    digest_frequency = user['digest_frequency']
    preferred_language = user['preferred_language']

    keyboard = InlineKeyboardBuilder()
    keyboard.row(
        InlineKeyboardButton(text=f"Авто-сповіщення: {auto_notifications_status}", callback_data="toggle_auto_notifications")
    )
    keyboard.row(
        InlineKeyboardButton(text=f"Частота дайджестів: {digest_frequency}", callback_data="set_digest_frequency")
    )
    keyboard.row(
        InlineKeyboardButton(text=f"Мова перекладу: {preferred_language}", callback_data="set_preferred_language")
    )

    await message.answer(
        "<b>Ваші налаштування:</b>\n"
        f"Статус авто-сповіщень: {auto_notifications_status}\n"
        f"Частота дайджестів: {digest_frequency}\n"
        f"Мова для перекладу новин: {preferred_language}\n\n"
        "Виберіть, що змінити:",
        reply_markup=keyboard.as_markup()
    )
    await log_user_action(user_id, "settings_command")


@main_router.callback_query(F.data == "toggle_auto_notifications")
async def toggle_auto_notifications_callback(callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    current_status = (await get_user_by_telegram_id(user_id))['auto_notifications']
    new_status = not current_status
    await toggle_user_auto_notifications(user_id, new_status)
    status_text = "Увімкнено" if new_status else "Вимкнено"
    await callback_query.message.edit_text(
        f"Авто-сповіщення тепер: <b>{status_text}</b>. "
        "Для перегляду всіх налаштувань, знову виконайте /settings."
    )
    await callback_query.answer()


@main_router.callback_query(F.data == "set_digest_frequency")
async def set_digest_frequency_callback(callback_query: CallbackQuery, state: FSMContext):
    keyboard = InlineKeyboardBuilder()
    keyboard.row(
        InlineKeyboardButton(text="Щоденно", callback_data="digest_freq_daily"),
        InlineKeyboardButton(text="Щотижнево", callback_data="digest_freq_weekly")
    )
    keyboard.row(
        InlineKeyboardButton(text="Вимкнено", callback_data="digest_freq_none")
    )
    await callback_query.message.answer("Виберіть частоту для дайджестів новин:", reply_markup=keyboard.as_markup())
    await state.set_state(UserSettingsStates.waiting_for_digest_frequency)
    await callback_query.answer()


@main_router.callback_query(F.data.startswith("digest_freq_"), StateFilter(UserSettingsStates.waiting_for_digest_frequency))
async def process_digest_frequency(callback_query: CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id
    frequency = callback_query.data.replace("digest_freq_", "")
    await update_user_digest_frequency(user_id, frequency)
    await callback_query.message.edit_text(
        f"Частота дайджестів встановлена на: <b>{frequency.capitalize()}</b>. "
        "Для перегляду всіх налаштувань, знову виконайте /settings."
    )
    await state.clear()
    await callback_query.answer()


@main_router.callback_query(F.data == "set_preferred_language")
async def set_preferred_language_callback(callback_query: CallbackQuery, state: FSMContext):
    available_languages = await get_unique_languages() # Fetches languages from sources
    if not available_languages:
        await callback_query.message.answer("Наразі немає доступних мов для вибору.")
        await callback_query.answer()
        return

    keyboard = InlineKeyboardBuilder()
    for lang in sorted(available_languages):
        keyboard.add(InlineKeyboardButton(text=lang.capitalize(), callback_data=f"pref_lang_{lang}"))
    keyboard.adjust(2)
    await callback_query.message.answer("Виберіть мову, якою ви бажаєте отримувати переклади новин:", reply_markup=keyboard.as_markup())
    await state.set_state(UserSettingsStates.waiting_for_preferred_language)
    await callback_query.answer()


@main_router.callback_query(F.data.startswith("pref_lang_"), StateFilter(UserSettingsStates.waiting_for_preferred_language))
async def process_preferred_language(callback_query: CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id
    language_code = callback_query.data.replace("pref_lang_", "")
    await set_user_preferred_language(user_id, language_code)
    await callback_query.message.edit_text(
        f"Ваша уподобана мова перекладу встановлена на: <b>{language_code.capitalize()}</b>. "
        "Для перегляду всіх налаштувань, знову виконайте /settings."
    )
    await state.clear()
    await callback_query.answer()


@main_router.message(Command("feeds"))
async def feeds_command(message: Message):
    user_id = message.from_user.id
    feeds = await get_all_feeds(user_id)

    response_text = "<b>Ваші стрічки новин:</b>\n"
    if not feeds:
        response_text += "У вас немає жодної стрічки новин. Створіть нову за допомогою 'Додати стрічку'."
    else:
        for feed in feeds:
            response_text += f"- ID: {feed['id']}, Назва: {feed['name']}\n"

    keyboard = InlineKeyboardBuilder()
    keyboard.row(
        InlineKeyboardButton(text="Додати стрічку", callback_data="add_feed"),
        InlineKeyboardButton(text="Редагувати стрічку", callback_data="edit_feed"),
    )
    keyboard.row(
        InlineKeyboardButton(text="Видалити стрічку", callback_data="delete_feed"),
        InlineKeyboardButton(text="Джерела стрічки", callback_data="feed_sources"),
    )
    keyboard.row(
        InlineKeyboardButton(text="Підписатися на стрічку", callback_data="subscribe_feed"),
        InlineKeyboardButton(text="Відписатися від стрічки", callback_data="unsubscribe_feed"),
    )

    await message.answer(response_text, reply_markup=keyboard.as_markup())
    await log_user_action(user_id, "feeds_command")


@main_router.callback_query(F.data == "add_feed")
async def add_feed_callback(callback_query: CallbackQuery, state: FSMContext):
    await callback_query.message.answer("Будь ласка, введіть назву для нової стрічки:")
    await state.set_state(FeedStates.waiting_for_feed_name)
    await callback_query.answer()


@main_router.message(FeedStates.waiting_for_feed_name)
async def process_feed_name(message: Message, state: FSMContext):
    feed_name = message.text.strip()
    user_id = message.from_user.id
    try:
        new_feed_id = await add_feed(feed_name, user_id)
        if new_feed_id:
            await message.answer(f"Стрічку '{feed_name}' (ID: {new_feed_id}) успішно додано!")
        else:
            await message.answer("Не вдалося додати стрічку.")
    except Exception as e:
        logger.error(f"Error adding feed: {e}", exc_info=True)
        await message.answer(f"Сталася помилка при додаванні стрічки: {e}.")
    finally:
        await state.clear()


@main_router.callback_query(F.data == "edit_feed")
async def edit_feed_callback(callback_query: CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id
    feeds = await get_all_feeds(user_id)
    if not feeds:
        await callback_query.message.answer("У вас немає стрічок для редагування.")
        await callback_query.answer()
        return

    feed_list_text = "<b>Ваші стрічки для редагування:</b>\n"
    for feed in feeds:
        feed_list_text += f"ID: {feed['id']}, Назва: {feed['name']}\n"
    feed_list_text += "\nБудь ласка, введіть ID стрічки, яку ви хочете редагувати:"
    await callback_query.message.answer(feed_list_text)
    await state.set_state(FeedStates.waiting_for_feed_to_edit_id)
    await callback_query.answer()


@main_router.message(FeedStates.waiting_for_feed_to_edit_id)
async def process_edit_feed_id(message: Message, state: FSMContext):
    try:
        feed_id = int(message.text.strip())
        user_id = message.from_user.id
        feed = await get_feed_by_id(feed_id)
        if not feed or feed['user_id'] != user_id:
            await message.answer(f"Стрічку з ID {feed_id} не знайдено або вона вам не належить.")
            await state.clear()
            return

        await state.update_data(current_feed_id=feed_id)
        await message.answer(f"Вибрано стрічку '{feed['name']}' (ID: {feed_id}). Введіть нову назву для цієї стрічки:")
        await state.set_state(FeedStates.waiting_for_feed_new_name)
    except ValueError:
        await message.answer("Некоректний ID стрічки. Будь ласка, введіть числове значення.")
        await state.clear()
    except Exception as e:
        logger.error(f"Error in process_edit_feed_id: {e}", exc_info=True)
        await message.answer(f"Сталася помилка: {e}.")
        await state.clear()


@main_router.message(FeedStates.waiting_for_feed_new_name)
async def process_feed_new_name(message: Message, state: FSMContext):
    try:
        user_data = await state.get_data()
        feed_id = user_data['current_feed_id']
        new_name = message.text.strip()
        updated = await update_feed(feed_id, new_name)
        if updated:
            await message.answer(f"Стрічку (ID: {feed_id}) успішно оновлено. Нова назва: '{new_name}'.")
        else:
            await message.answer(f"Не вдалося оновити стрічку (ID: {feed_id}).")
    except Exception as e:
        logger.error(f"Error updating feed name: {e}", exc_info=True)
        await message.answer(f"Сталася помилка при оновленні стрічки: {e}.")
    finally:
        await state.clear()


@main_router.callback_query(F.data == "delete_feed")
async def delete_feed_callback(callback_query: CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id
    feeds = await get_all_feeds(user_id)
    if not feeds:
        await callback_query.message.answer("У вас немає стрічок для видалення.")
        await callback_query.answer()
        return

    feed_list_text = "<b>Ваші стрічки для видалення:</b>\n"
    for feed in feeds:
        feed_list_text += f"ID: {feed['id']}, Назва: {feed['name']}\n"
    feed_list_text += "\nБудь ласка, введіть ID стрічки, яку ви хочете видалити:"
    await callback_query.message.answer(feed_list_text)
    await state.set_state(FeedStates.waiting_for_feed_to_delete_id)
    await callback_query.answer()


@main_router.message(FeedStates.waiting_for_feed_to_delete_id)
async def process_delete_feed(message: Message, state: FSMContext):
    try:
        feed_id = int(message.text.strip())
        user_id = message.from_user.id
        feed = await get_feed_by_id(feed_id)
        if not feed or feed['user_id'] != user_id:
            await message.answer(f"Стрічку з ID {feed_id} не знайдено або вона вам не належить.")
            await state.clear()
            return

        deleted = await delete_feed(feed_id)
        if deleted:
            await message.answer(f"Стрічку '{feed['name']}' (ID: {feed_id}) успішно видалено.")
        else:
            await message.answer(f"Не вдалося видалити стрічку з ID {feed_id}.")
    except ValueError:
        await message.answer("Некоректний ID стрічки. Будь ласка, введіть числове значення.")
    except Exception as e:
        logger.error(f"Error deleting feed: {e}", exc_info=True)
        await message.answer(f"Сталася помилка при видаленні стрічки: {e}.")
    finally:
        await state.clear()


@main_router.callback_query(F.data == "feed_sources")
async def feed_sources_callback(callback_query: CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id
    feeds = await get_all_feeds(user_id)
    if not feeds:
        await callback_query.message.answer("У вас немає стрічок для перегляду джерел.")
        await callback_query.answer()
        return

    keyboard = InlineKeyboardBuilder()
    for feed in feeds:
        keyboard.add(InlineKeyboardButton(text=feed['name'], callback_data=f"view_feed_sources_{feed['id']}"))
    keyboard.adjust(1)
    keyboard.row(
        InlineKeyboardButton(text="Додати джерело до стрічки", callback_data="add_source_to_feed"),
        InlineKeyboardButton(text="Видалити джерело зі стрічки", callback_data="remove_source_from_feed")
    )

    await callback_query.message.answer("Виберіть стрічку, щоб переглянути або змінити її джерела:", reply_markup=keyboard.as_markup())
    await callback_query.answer()


@main_router.callback_query(F.data.startswith("view_feed_sources_"))
async def view_feed_sources_callback(callback_query: CallbackQuery):
    feed_id = int(callback_query.data.split('_')[-1])
    user_id = callback_query.from_user.id
    feed = await get_feed_by_id(feed_id)

    if not feed or feed['user_id'] != user_id:
        await callback_query.message.answer("Стрічку не знайдено або вона вам не належить.")
        await callback_query.answer()
        return

    sources_in_feed = await get_source_feeds(feed_id)
    response_text = f"<b>Джерела в стрічці '{feed['name']}' (ID: {feed_id}):</b>\n"
    if not sources_in_feed:
        response_text += "У цій стрічці немає джерел."
    else:
        for s_in_f in sources_in_feed:
            source = await get_source_by_id(s_in_f['source_id'])
            if source:
                response_text += f"- {source['name']} (ID: {source['id']})\n"
    await callback_query.message.answer(response_text)
    await callback_query.answer()


@main_router.callback_query(F.data == "add_source_to_feed")
async def add_source_to_feed_callback(callback_query: CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id
    feeds = await get_all_feeds(user_id)
    if not feeds:
        await callback_query.message.answer("У вас немає стрічок. Спочатку створіть стрічку.")
        await callback_query.answer()
        return

    feed_list_text = "<b>Доступні стрічки:</b>\n"
    for feed in feeds:
        feed_list_text += f"ID: {feed['id']}, Назва: {feed['name']}\n"
    feed_list_text += "\nБудь ласка, введіть ID стрічки, до якої ви хочете додати джерело:"
    await callback_query.message.answer(feed_list_text)
    await state.set_state(FeedStates.waiting_for_feed_add_source_feed_id)
    await callback_query.answer()


@main_router.message(FeedStates.waiting_for_feed_add_source_feed_id)
async def process_add_source_to_feed_feed_id(message: Message, state: FSMContext):
    try:
        feed_id = int(message.text.strip())
        user_id = message.from_user.id
        feed = await get_feed_by_id(feed_id)
        if not feed or feed['user_id'] != user_id:
            await message.answer(f"Стрічку з ID {feed_id} не знайдено або вона вам не належить.")
            await state.clear()
            return

        await state.update_data(target_feed_id_for_source_add=feed_id)

        active_sources = await get_all_active_sources()
        if not active_sources:
            await message.answer("Немає доступних джерел для додавання.")
            await state.clear()
            return

        source_list_text = "<b>Доступні джерела:</b>\n"
        for s in active_sources:
            source_list_text += f"ID: {s['id']}, Назва: {s['name']}\n"
        source_list_text += "\nБудь ласка, введіть ID джерела, яке ви хочете додати до стрічки:"
        await message.answer(source_list_text)
        await state.set_state(FeedStates.waiting_for_feed_add_source_source_id)

    except ValueError:
        await message.answer("Некоректний ID стрічки. Будь ласка, введіть числове значення.")
        await state.clear()
    except Exception as e:
        logger.error(f"Error in process_add_source_to_feed_feed_id: {e}", exc_info=True)
        await message.answer(f"Сталася помилка: {e}.")
        await state.clear()


@main_router.message(FeedStates.waiting_for_feed_add_source_source_id)
async def process_add_source_to_feed_source_id(message: Message, state: FSMContext):
    try:
        source_id = int(message.text.strip())
        source = await get_source_by_id(source_id)
        if not source:
            await message.answer(f"Джерело з ID {source_id} не знайдено.")
            await state.clear()
            return

        user_data = await state.get_data()
        feed_id = user_data['target_feed_id_for_source_add']

        # Check if source is already in feed
        sources_in_feed = await get_feed_source_ids(feed_id)
        if source_id in sources_in_feed:
            await message.answer(f"Джерело '{source['name']}' вже є в цій стрічці.")
            await state.clear()
            return

        added = await add_source_to_feed(feed_id, source_id)
        if added:
            feed = await get_feed_by_id(feed_id)
            await message.answer(f"Джерело '{source['name']}' успішно додано до стрічки '{feed['name']}'.")
        else:
            await message.answer(f"Не вдалося додати джерело '{source['name']}' до стрічки.")
    except ValueError:
        await message.answer("Некоректний ID джерела. Будь ласка, введіть числове значення.")
    except Exception as e:
        logger.error(f"Error adding source to feed: {e}", exc_info=True)
        await message.answer(f"Сталася помилка при додаванні джерела до стрічки: {e}.")
    finally:
        await state.clear()


@main_router.callback_query(F.data == "remove_source_from_feed")
async def remove_source_from_feed_callback(callback_query: CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id
    feeds = await get_all_feeds(user_id)
    if not feeds:
        await callback_query.message.answer("У вас немає стрічок.")
        await callback_query.answer()
        return

    feed_list_text = "<b>Доступні стрічки:</b>\n"
    for feed in feeds:
        feed_list_text += f"ID: {feed['id']}, Назва: {feed['name']}\n"
    feed_list_text += "\nБудь ласка, введіть ID стрічки, з якої ви хочете видалити джерело:"
    await callback_query.message.answer(feed_list_text)
    await state.set_state(FeedStates.waiting_for_feed_remove_source_feed_id)
    await callback_query.answer()


@main_router.message(FeedStates.waiting_for_feed_remove_source_feed_id)
async def process_remove_source_from_feed_feed_id(message: Message, state: FSMContext):
    try:
        feed_id = int(message.text.strip())
        user_id = message.from_user.id
        feed = await get_feed_by_id(feed_id)
        if not feed or feed['user_id'] != user_id:
            await message.answer(f"Стрічку з ID {feed_id} не знайдено або вона вам не належить.")
            await state.clear()
            return

        await state.update_data(target_feed_id_for_source_remove=feed_id)

        sources_in_feed = await get_source_feeds(feed_id)
        if not sources_in_feed:
            await message.answer("У цій стрічці немає джерел для видалення.")
            await state.clear()
            return

        source_list_text = f"<b>Джерела в стрічці '{feed['name']}' (ID: {feed_id}):</b>\n"
        for s_in_f in sources_in_feed:
            source = await get_source_by_id(s_in_f['source_id'])
            if source:
                source_list_text += f"- {source['name']} (ID: {source['id']})\n"
        source_list_text += "\nБудь ласка, введіть ID джерела, яке ви хочете видалити зі стрічки:"
        await message.answer(source_list_text)
        await state.set_state(FeedStates.waiting_for_feed_remove_source_source_id)

    except ValueError:
        await message.answer("Некоректний ID стрічки. Будь ласка, введіть числове значення.")
        await state.clear()
    except Exception as e:
        logger.error(f"Error in process_remove_source_from_feed_feed_id: {e}", exc_info=True)
        await message.answer(f"Сталася помилка: {e}.")
        await state.clear()


@main_router.message(FeedStates.waiting_for_feed_remove_source_source_id)
async def process_remove_source_from_feed_source_id(message: Message, state: FSMContext):
    try:
        source_id = int(message.text.strip())
        source = await get_source_by_id(source_id)
        if not source:
            await message.answer(f"Джерело з ID {source_id} не знайдено.")
            await state.clear()
            return

        user_data = await state.get_data()
        feed_id = user_data['target_feed_id_for_source_remove']

        removed = await remove_source_from_feed(feed_id, source_id)
        if removed:
            feed = await get_feed_by_id(feed_id)
            await message.answer(f"Джерело '{source['name']}' успішно видалено зі стрічки '{feed['name']}'.")
        else:
            await message.answer(f"Не вдалося видалити джерело '{source['name']}' зі стрічки.")
    except ValueError:
        await message.answer("Некоректний ID джерела. Будь ласка, введіть числове значення.")
    except Exception as e:
        logger.error(f"Error removing source from feed: {e}", exc_info=True)
        await message.answer(f"Сталася помилка при видаленні джерела зі стрічки: {e}.")
    finally:
        await state.clear()


@main_router.callback_query(F.data == "subscribe_feed")
async def subscribe_feed_command(callback_query: CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id
    feeds = await get_all_feeds(user_id) # Get feeds owned by the user
    if not feeds:
        await callback_query.message.answer("У вас немає стрічок для підписки.")
        await callback_query.answer()
        return

    user_subscribed_feeds = await get_user_subscriptions_by_feed(user_id)
    subscribed_feed_ids = {sub['feed_id'] for sub in user_subscribed_feeds}

    keyboard = InlineKeyboardBuilder()
    for feed in feeds:
        if feed['id'] not in subscribed_feed_ids:
            keyboard.add(InlineKeyboardButton(text=feed['name'], callback_data=f"subscribe_to_feed_{feed['id']}"))
    keyboard.adjust(2)

    if not keyboard.buttons:
        await callback_query.message.answer("Ви вже підписані на всі свої стрічки.")
        await callback_query.answer()
        return

    await callback_query.message.answer("Виберіть стрічку для підписки:", reply_markup=keyboard.as_markup())
    await state.set_state(FeedStates.waiting_for_feed_subscribe_id)
    await callback_query.answer()


@main_router.callback_query(F.data.startswith("subscribe_to_feed_"), StateFilter(FeedStates.waiting_for_feed_subscribe_id))
async def process_subscribe_to_feed(callback_query: CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id
    feed_id = int(callback_query.data.split('_')[-1])

    feed = await get_feed_by_id(feed_id)
    if not feed or feed['user_id'] != user_id: # Ensure user can only subscribe to their own feeds
        await callback_query.message.answer("Вибрану стрічку не знайдено або вона вам не належить.")
        await callback_query.answer()
        return

    sources_in_feed = await get_source_feeds(feed_id)
    if not sources_in_feed:
        await callback_query.message.answer(f"Стрічка '{feed['name']}' не містить джерел. Додайте джерела спочатку.")
        await state.clear()
        await callback_query.answer()
        return

    success_count = 0
    for source_feed_entry in sources_in_feed:
        source_id = source_feed_entry['source_id']
        # Check if already subscribed to this specific source within this feed context
        # This logic needs to be careful: a user might be subscribed to a source directly,
        # or via another feed. The `user_subscriptions` table links user_id, source_id, and feed_id.
        # So we check for the specific (user_id, source_id, feed_id) tuple.
        already_subscribed = False
        user_subs = await get_user_subscriptions_by_feed(user_id, feed_id)
        for sub in user_subs:
            if sub['source_id'] == source_id:
                already_subscribed = True
                break

        if not already_subscribed:
            added = await add_user_subscription(user_id, source_id, feed_id)
            if added:
                success_count += 1

    if success_count > 0:
        await callback_query.message.answer(f"Ви успішно підписалися на {success_count} джерел у стрічці '{feed['name']}'.")
    else:
        await callback_query.message.answer(f"Ви вже підписані на всі джерела у стрічці '{feed['name']}'.")
    await state.clear()
    await callback_query.answer()


@main_router.callback_query(F.data == "unsubscribe_feed")
async def unsubscribe_feed_command(callback_query: CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id
    user_subscribed_feeds = await get_user_subscriptions_by_feed(user_id)
    if not user_subscribed_feeds:
        await callback_query.message.answer("Ви не підписані на жодну стрічку.")
        await callback_query.answer()
        return

    # Get unique feed IDs that the user is subscribed to
    subscribed_feed_ids = {sub['feed_id'] for sub in user_subscribed_feeds}
    feeds_to_display = []
    for feed_id in subscribed_feed_ids:
        feed = await get_feed_by_id(feed_id)
        if feed:
            feeds_to_display.append(feed)

    if not feeds_to_display:
        await callback_query.message.answer("Ви не підписані на жодну стрічку.")
        await callback_query.answer()
        return

    keyboard = InlineKeyboardBuilder()
    for feed in feeds_to_display:
        keyboard.add(InlineKeyboardButton(text=feed['name'], callback_data=f"unsubscribe_from_feed_{feed['id']}"))
    keyboard.adjust(2)

    await callback_query.message.answer("Виберіть стрічку для відписки:", reply_markup=keyboard.as_markup())
    await state.set_state(FeedStates.waiting_for_feed_unsubscribe_id)
    await callback_query.answer()


@main_router.callback_query(F.data.startswith("unsubscribe_from_feed_"), StateFilter(FeedStates.waiting_for_feed_unsubscribe_id))
async def process_unsubscribe_from_feed(callback_query: CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id
    feed_id = int(callback_query.data.split('_')[-1])

    feed = await get_feed_by_id(feed_id)
    if not feed:
        await callback_query.message.answer("Вибрану стрічку не знайдено.")
        await state.clear()
        await callback_query.answer()
        return

    sources_in_feed = await get_source_feeds(feed_id)
    removed_count = 0
    for source_feed_entry in sources_in_feed:
        source_id = source_feed_entry['source_id']
        removed = await remove_user_subscription(user_id, source_id, feed_id)
        if removed:
            removed_count += 1

    if removed_count > 0:
        await callback_query.message.answer(f"Ви успішно відписалися від {removed_count} джерел у стрічці '{feed['name']}'.")
    else:
        await callback_query.message.answer(f"Ви не були підписані на джерела у стрічці '{feed['name']}'.")
    await state.clear()
    await callback_query.answer()


# --- AI Assistant ---
class AIAssistantStates(StatesGroup):
    waiting_for_ai_query = State()


@main_router.message(Command("ai_assistant"))
async def ai_assistant_command(message: Message, state: FSMContext):
    user_id = message.from_user.id
    user = await get_user_by_telegram_id(user_id)

    if not user or not user.get('is_pro', False):
        await message.answer("Ця функція доступна лише для PRO користувачів.")
        await log_user_action(user_id, "ai_assistant_command_denied")
        return

    # Check AI request limits
    ai_requests_today = user.get('ai_requests_today', 0)
    ai_last_request_date = user.get('ai_last_request_date', date.min)

    today = date.today()
    if ai_last_request_date < today:
        # Reset count if it's a new day
        await reset_ai_request_count(user_id)
        ai_requests_today = 0

    max_ai_requests_str = await get_bot_setting("MAX_AI_REQUESTS_PER_DAY")
    max_ai_requests = int(max_ai_requests_str) if max_ai_requests_str and max_ai_requests_str.isdigit() else 10

    if ai_requests_today >= max_ai_requests:
        await message.answer(f"Ви досягли ліміту в {max_ai_requests} запитів до AI-асистента на сьогодні. Спробуйте завтра.")
        await log_user_action(user_id, "ai_assistant_limit_reached")
        return

    await message.answer("Надішліть мені ваше питання для AI-асистента:")
    await state.set_state(AIAssistantStates.waiting_for_ai_query)
    await log_user_action(user_id, "ai_assistant_command_initiated")


@main_router.message(AIAssistantStates.waiting_for_ai_query)
async def process_ai_query(message: Message, state: FSMContext):
    user_id = message.from_user.id
    user_query = message.text.strip()

    if not user_query:
        await message.answer("Будь ласка, введіть ваше питання.")
        return

    await message.answer("Обробляю ваш запит, зачекайте...")

    try:
        # Increment AI request count
        await increment_ai_request_count(user_id)

        # Call Gemini API
        chat_history = []
        chat_history.push({"role": "user", "parts": [{"text": user_query}]})
        payload = {"contents": chat_history}
        api_key = "" # Canvas will provide this at runtime
        api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"

        async with ClientSession() as session:
            async with session.post(api_url, json=payload) as response:
                if response.status == 200:
                    result = await response.json()
                    if result.get('candidates') and result['candidates'][0].get('content') and result['candidates'][0]['content'].get('parts'):
                        ai_response_text = result['candidates'][0]['content']['parts'][0]['text']
                        await message.answer(f"<b>Відповідь AI:</b>\n{ai_response_text}")
                    else:
                        await message.answer("Не вдалося отримати відповідь від AI. Спробуйте ще раз.")
                        logger.error(f"Unexpected AI response structure: {result}")
                else:
                    error_detail = await response.text()
                    await message.answer(f"Помилка при зверненні до AI-асистента: {response.status} - {error_detail}")
                    logger.error(f"AI API error {response.status}: {error_detail}")

    except Exception as e:
        logger.error(f"Error processing AI query for user {user_id}: {e}", exc_info=True)
        await message.answer("Сталася помилка при обробці вашого запиту до AI. Будь ласка, спробуйте пізніше.")
    finally:
        await state.clear()
        await log_user_action(user_id, "ai_assistant_query_processed")


@main_router.message(Command("support"))
async def support_command(message: Message):
    support_text = (
        "Якщо у вас виникли питання або проблеми, ви можете зв'язатися з адміністратором:\n"
        f"Telegram ID адміністратора: <code>{ADMIN_TELEGRAM_ID}</code>\n"
        "Будь ласка, опишіть вашу проблему якомога детальніше."
    )
    await message.answer(support_text)
    await log_user_action(message.from_user.id, "support_command")


@main_router.message(Command("feedback"))
async def feedback_command(message: Message):
    user_id = message.from_user.id
    feedback_settings = await get_user_feedback_settings(user_id)
    feedback_enabled = feedback_settings.get('enabled', True)
    feedback_channel_id = feedback_settings.get('channel_id')

    status_text = "Увімкнено" if feedback_enabled else "Вимкнено"
    channel_text = f"Канал: <code>{feedback_channel_id}</code>" if feedback_channel_id else "Канал не налаштований"

    keyboard = InlineKeyboardBuilder()
    keyboard.row(
        InlineKeyboardButton(text=f"Відгуки: {status_text}", callback_data="toggle_feedback_status")
    )
    # Only show channel setting if feedback is enabled
    if feedback_enabled:
        keyboard.row(
            InlineKeyboardButton(text="Встановити канал відгуків (для адмінів)", callback_data="set_feedback_channel")
        )

    await message.answer(
        "<b>Налаштування відгуків:</b>\n"
        f"Статус відгуків: {status_text}\n"
        f"{channel_text}\n\n"
        "Виберіть дію:",
        reply_markup=keyboard.as_markup()
    )
    await log_user_action(user_id, "feedback_command")


@main_router.callback_query(F.data == "toggle_feedback_status")
async def toggle_feedback_status_callback(callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    feedback_settings = await get_user_feedback_settings(user_id)
    current_status = feedback_settings.get('enabled', True)
    new_status = not current_status
    await save_user_feedback_settings(user_id, enabled=new_status)
    status_text = "Увімкнено" if new_status else "Вимкнено"
    await callback_query.message.edit_text(f"Відгуки тепер: <b>{status_text}</b>. Для оновлення натисніть /feedback.")
    await callback_query.answer()


@main_router.callback_query(F.data == "set_feedback_channel")
@admin_only
async def set_feedback_channel_callback(callback_query: CallbackQuery, state: FSMContext):
    await callback_query.message.answer("Будь ласка, введіть ID каналу для відгуків (наприклад, -1001234567890):")
    await state.set_state(AdminStates.waiting_for_setting_value) # Re-use generic setting state
    await state.update_data(setting_key="FEEDBACK_CHANNEL_ID") # Store key for process_setting_value
    await callback_query.answer()


# --- Scheduled Jobs ---

async def parse_all_sources_job():
    logger.info("Running scheduled news parsing job...")
    db_pool = await get_db_pool()
    if not db_pool:
        logger.error("Database pool not initialized. Skipping parsing job.")
        return

    active_sources = await get_all_active_sources()
    if not active_sources:
        logger.info("No active sources found to parse.")
        return

    for source in active_sources:
        try:
            logger.info(f"Parsing source: {source['name']} ({source['url']})")
            recent_news = await fetch_recent_news_from_source(source['url'], source.get('parser_settings', {}))
            if recent_news:
                for news_item_data in recent_news:
                    # Add news item to database
                    news_id = await add_news_item(
                        source_id=source['id'],
                        title=news_item_data.get('title'),
                        source_url=news_item_data.get('source_url'),
                        image_url=news_item_data.get('image_url'),
                        published_at=news_item_data.get('published_at'),
                        content=news_item_data.get('content')
                    )
                    if news_id:
                        logger.info(f"Added new news item from {source['name']}: {news_item_data.get('title')}")
                        # Update source stats
                        await update_source_stats_parsed(source['id'])
                    else:
                        logger.debug(f"News item with URL {news_item_data.get('source_url')} already exists. Skipping.")
            else:
                logger.info(f"No new news found for source: {source['name']}")
        except Exception as e:
            logger.error(f"Error during parsing source {source['name']} ({source['url']}): {e}", exc_info=True)
    logger.info("News parsing job finished.")


async def publish_news_to_channel_job():
    logger.info("Running scheduled news publishing job...")
    db_pool = await get_db_pool()
    if not db_pool:
        logger.error("Database pool not initialized. Skipping publishing job.")
        return

    if not NEWS_CHANNEL_ID or NEWS_CHANNEL_ID == 0:
        logger.warning("NEWS_CHANNEL_ID is not set or is 0 in config.py. Skipping news publishing to channel.")
        return

    unsent_news = await get_unsent_news_items()
    if not unsent_news:
        logger.info("No unsent news items found.")
        return

    for news_item in unsent_news:
        try:
            source = await get_source_by_id(news_item['source_id'])
            if not source:
                logger.warning(f"Source with ID {news_item['source_id']} not found for news item {news_item['id']}. Skipping.")
                continue

            news_text = (
                f"<b>{news_item['title']}</b>\n\n"
                f"Джерело: {source['name']}\n"
                f"Опубліковано: {news_item['published_at'].strftime('%Y-%m-%d %H:%M')}\n\n"
                f"<a href='{news_item['source_url']}'>Читати повністю</a>"
            )

            # Add image if available
            if news_item['image_url']:
                try:
                    # Fetch image content
                    async with ClientSession() as session:
                        async with session.get(news_item['image_url']) as resp:
                            resp.raise_for_status()
                            image_bytes = await resp.read()

                    # Send photo with caption
                    await bot.send_photo(
                        chat_id=NEWS_CHANNEL_ID,
                        photo=BufferedInputFile(image_bytes, filename="news_image.jpg"),
                        caption=news_text,
                        parse_mode=ParseMode.HTML
                    )
                except Exception as e:
                    logger.warning(f"Failed to send photo for news item {news_item['id']}: {e}. Sending text only.", exc_info=True)
                    await bot.send_message(chat_id=NEWS_CHANNEL_ID, text=news_text, parse_mode=ParseMode.HTML,
                                           disable_web_page_preview=False)
            else:
                await bot.send_message(chat_id=NEWS_CHANNEL_ID, text=news_text, parse_mode=ParseMode.HTML,
                                       disable_web_page_preview=False)

            await mark_news_as_sent(news_item['id'])
            await update_source_stats_published(news_item['source_id'])
            logger.info(f"Published news item {news_item['id']} to channel {NEWS_CHANNEL_ID} and marked as sent.")
            await asyncio.sleep(1) # Delay to avoid hitting Telegram API limits
        except Exception as e:
            logger.error(f"Failed to publish news item {news_item['id']}: {e}", exc_info=True)
    logger.info("News publishing job finished.")


async def send_daily_digests_job():
    logger.info("Running scheduled daily digest job...")
    db_pool = await get_db_pool()
    if not db_pool:
        logger.error("Database pool not initialized. Skipping digest job.")
        return

    users_for_digest = await get_users_for_digest('daily')
    if not users_for_digest:
        logger.info("No users found for daily digest.")
        return

    for user in users_for_digest:
        user_id = user['telegram_id']
        logger.info(f"Sending daily digest to user {user_id}")
        unsent_digest_news = await get_unsent_digest_news_for_user(user_id, 'daily')

        if not unsent_digest_news:
            logger.info(f"No unsent digest news found for user {user_id}.")
            continue

        digest_text = "<b>Ваш щоденний дайджест новин:</b>\n\n"
        news_count = 0
        sent_news_ids = []

        for news_item in unsent_digest_news:
            if news_count >= 5: # Limit digest to 5 news items for brevity
                break

            source = await get_source_by_id(news_item['source_id'])
            if not source:
                logger.warning(f"Source with ID {news_item['source_id']} not found for digest news item {news_item['id']}. Skipping.")
                continue

            news_link = hlink(news_item['title'], news_item['source_url'])
            digest_text += f"➡️ {news_link}\n"
            digest_text += f"Джерело: {source['name']}\n"
            digest_text += f"Опубліковано: {news_item['published_at'].strftime('%Y-%m-%d %H:%M')}\n\n"
            sent_news_ids.append(news_item['id'])
            news_count += 1

        if news_count > 0:
            try:
                await bot.send_message(chat_id=user_id, text=digest_text, parse_mode=ParseMode.HTML, disable_web_page_preview=False)
                for news_id in sent_news_ids:
                    await mark_digest_news_as_sent(user_id, news_id, 'daily')
                logger.info(f"Sent daily digest with {news_count} news items to user {user_id}.")
            except Exception as e:
                logger.error(f"Failed to send daily digest to user {user_id}: {e}", exc_info=True)
        else:
            logger.info(f"No news to include in daily digest for user {user_id} after filtering.")
        await asyncio.sleep(1) # Delay to avoid hitting Telegram API limits
    logger.info("Daily digest job finished.")


async def send_weekly_digests_job():
    logger.info("Running scheduled weekly digest job...")
    db_pool = await get_db_pool()
    if not db_pool:
        logger.error("Database pool not initialized. Skipping digest job.")
        return

    users_for_digest = await get_users_for_digest('weekly')
    if not users_for_digest:
        logger.info("No users found for weekly digest.")
        return

    for user in users_for_digest:
        user_id = user['telegram_id']
        logger.info(f"Sending weekly digest to user {user_id}")
        unsent_digest_news = await get_unsent_digest_news_for_user(user_id, 'weekly')

        if not unsent_digest_news:
            logger.info(f"No unsent digest news found for user {user_id}.")
            continue

        digest_text = "<b>Ваш щотижневий дайджест новин:</b>\n\n"
        news_count = 0
        sent_news_ids = []

        for news_item in unsent_digest_news:
            if news_count >= 10: # Limit digest to 10 news items for brevity
                break

            source = await get_source_by_id(news_item['source_id'])
            if not source:
                logger.warning(f"Source with ID {news_item['source_id']} not found for digest news item {news_item['id']}. Skipping.")
                continue

            news_link = hlink(news_item['title'], news_item['source_url'])
            digest_text += f"➡️ {news_link}\n"
            digest_text += f"Джерело: {source['name']}\n"
            digest_text += f"Опубліковано: {news_item['published_at'].strftime('%Y-%m-%d %H:%M')}\n\n"
            sent_news_ids.append(news_item['id'])
            news_count += 1

        if news_count > 0:
            try:
                await bot.send_message(chat_id=user_id, text=digest_text, parse_mode=ParseMode.HTML, disable_web_page_preview=False)
                for news_id in sent_news_ids:
                    await mark_digest_news_as_sent(user_id, news_id, 'weekly')
                logger.info(f"Sent weekly digest with {news_count} news items to user {user_id}.")
            except Exception as e:
                logger.error(f"Failed to send weekly digest to user {user_id}: {e}", exc_info=True)
        else:
            logger.info(f"No news to include in weekly digest for user {user_id} after filtering.")
        await asyncio.sleep(1) # Delay to avoid hitting Telegram API limits
    logger.info("Weekly digest job finished.")


async def delete_old_news_job():
    logger.info("Running scheduled old news deletion job...")
    db_pool = await get_db_pool()
    if not db_pool:
        logger.error("Database pool not initialized. Skipping old news deletion job.")
        return

    # Delete news older than 30 days
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=30)
    deleted_count = await delete_old_news(cutoff_date)
    logger.info(f"Deleted {deleted_count} old news items older than {cutoff_date.strftime('%Y-%m-%d %H:%M')}.")
    logger.info("Old news deletion job finished.")


# --- Worker initialization ---
async def start_worker_jobs():
    logger.info("Initializing database pool for worker...")
    await get_db_pool() # Ensure DB pool is initialized for the worker

    # Set up webhook only if WEB_APP_URL and TELEGRAM_BOT_TOKEN are available
    if WEB_APP_URL and TELEGRAM_BOT_TOKEN:
        webhook_url = f"{WEB_APP_URL}/webhook"
        try:
            # Ensure bot is not None before setting webhook
            if bot:
                await bot.set_webhook(webhook_url)
                logger.info(f"Webhook set to {webhook_url}")
            else:
                logger.warning("Bot object is None in worker. Cannot set webhook.")
        except Exception as e:
            logger.error(f"Failed to set webhook in worker: {e}", exc_info=True)
    else:
        logger.warning("WEB_APP_URL or TELEGRAM_BOT_TOKEN not set. Webhook will not be set.")

    scheduler = AsyncIOScheduler()

    # Fetch intervals from bot settings (database)
    # Use default values from schema.sql (15 for parse, 5 for publish) if settings are not found or invalid
    parse_interval_minutes_str = await get_bot_setting("NEWS_PARSE_INTERVAL_MINUTES")
    publish_interval_minutes_str = await get_bot_setting("NEWS_PUBLISH_INTERVAL_MINUTES")

    parse_interval_minutes = int(parse_interval_minutes_str) if parse_interval_minutes_str and parse_interval_minutes_str.isdigit() else 15
    publish_interval_minutes = int(publish_interval_minutes_str) if publish_interval_minutes_str and publish_interval_minutes_str.isdigit() else 5

    logger.info(f"Scheduling news parsing job every {parse_interval_minutes} minutes.")
    scheduler.add_job(parse_all_sources_job, 'interval', minutes=parse_interval_minutes, id='parse_job')

    logger.info(f"Scheduling news publishing job every {publish_interval_minutes} minutes.")
    scheduler.add_job(publish_news_to_channel_job, 'interval', minutes=publish_interval_minutes, id='publish_job')

    # Add job to send daily digests (e.g., daily at a specific time)
    logger.info("Scheduling daily digest job.")
    scheduler.add_job(send_daily_digests_job, 'cron', hour=8, minute=0, id='daily_digest_job') # Runs daily at 8 AM UTC

    # Add job to send weekly digests (e.g., weekly on Monday at a specific time)
    logger.info("Scheduling weekly digest job.")
    scheduler.add_job(send_weekly_digests_job, 'cron', day_of_week='mon', hour=9, minute=0, id='weekly_digest_job') # Runs every Monday at 9 AM UTC

    # Add job to delete old news (e.g., older than 30 days)
    # This can run less frequently, e.g., daily or once every few hours
    logger.info("Scheduling daily old news deletion job.")
    scheduler.add_job(delete_old_news_job, 'cron', hour=3, minute=0, id='delete_old_news_job') # Runs daily at 3 AM UTC


    scheduler.start()
    logger.info("Scheduler started.")
    try:
        # Keep the event loop running for the scheduler
        while True:
            await asyncio.sleep(3600) # Sleep for an hour, or indefinitely
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("Bot worker shut down.")


# --- Main execution block ---
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
        # Ensure the webhook is set if running in web mode and WEB_APP_URL is available
        @app.on_event("startup")
        async def on_startup_web(): # Renamed to avoid conflict
            logger.info("FastAPI app startup: Initializing DB pool and setting webhook...")
            try:
                await get_db_pool() # Initialize DB pool for web process
                logger.info("DB pool initialized for web process.")
                if TELEGRAM_BOT_TOKEN and WEB_APP_URL:
                    webhook_url = f"{WEB_APP_URL}/webhook"
                    logger.info(f"Attempting to set webhook to {webhook_url}")
                    # Ensure bot is not None before calling set_webhook
                    if bot:
                        await bot.set_webhook(webhook_url)
                        logger.info(f"Webhook set to {webhook_url} successfully.")
                    else:
                        logger.error("Bot object is None. Cannot set webhook.")
                        # This should not happen if TELEGRAM_BOT_TOKEN is set, but good to check.
                else:
                    logger.warning("WEB_APP_URL or TELEGRAM_BOT_TOKEN not set. Webhook will not be set on startup.")
            except Exception as e:
                logger.error(f"Failed during FastAPI app startup: {e}", exc_info=True)
                # Re-raise to prevent the app from starting if critical startup fails
                raise # Re-raise the exception

        @app.on_event("shutdown")
        async def on_shutdown():
            logger.info("FastAPI app shutdown: Deleting webhook and closing DB pool...")
            if TELEGRAM_BOT_TOKEN and bot: # Check bot is not None
                try:
                    await bot.delete_webhook()
                    logger.info("Webhook deleted.")
                except Exception as e:
                    logger.error(f"Failed to delete webhook on shutdown: {e}", exc_info=True)
            db_pool = await get_db_pool()
            if db_pool:
                await db_pool.close()
                logger.info("Database pool closed.")


        # --- Webhook handler for Telegram updates ---
        @app.post("/webhook")
        async def telegram_webhook(update: Dict[str, Any]):
            if not bot:
                logger.error("Bot is not initialized. Cannot process webhook update.")
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Bot service unavailable")

            telegram_update = Update.model_validate(update)
            try:
                await dp.feed_update(bot, telegram_update)
            except Exception as e:
                logger.error(f"Error processing update: {e}", exc_info=True)
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal Server Error")
            return {"ok": True}

        # --- Admin Web Panel ---
        class SourceCreate(BaseModel):
            url: str
            name: str
            category: str
            language: str
            status: str = "active"
            source_type: str = "html"
            parser_settings: Dict[str, str] = Field(default_factory=dict)

        class SourceUpdate(BaseModel):
            name: Optional[str] = None
            category: Optional[str] = None
            language: Optional[str] = None
            status: Optional[str] = None
            source_type: Optional[str] = None
            parser_settings: Optional[Dict[str, str]] = None

        class BotSettingUpdate(BaseModel):
            setting_key: str
            setting_value: str

        @app.get("/admin", response_class=HTMLResponse)
        async def admin_panel_web(request: Request, api_key: str = Depends(get_api_key)):
            # HTML for a simple admin panel
            html_content = """
            <!DOCTYPE html>
            <html lang="uk">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Адмін-панель Бота Новин</title>
                <script src="https://cdn.tailwindcss.com"></script>
                <style>
                    body { font-family: 'Inter', sans-serif; }
                    .container { max-width: 960px; }
                </style>
            </head>
            <body class="bg-gray-100 p-4">
                <div class="container mx-auto bg-white p-6 rounded-lg shadow-md">
                    <h1 class="text-2xl font-bold mb-6 text-center">Адмін-панель Бота Новин</h1>

                    <div class="mb-8 p-4 bg-blue-50 rounded-lg">
                        <h2 class="text-xl font-semibold mb-3 text-blue-800">Керування джерелами</h2>
                        <form id="addSourceForm" class="mb-4 space-y-3">
                            <h3 class="text-lg font-medium mb-2">Додати нове джерело</h3>
                            <input type="text" id="add_url" placeholder="URL джерела (обов'язково)" required
                                   class="w-full p-2 border border-gray-300 rounded-md focus:ring-blue-500 focus:border-blue-500">
                            <input type="text" id="add_name" placeholder="Назва джерела"
                                   class="w-full p-2 border border-gray-300 rounded-md">
                            <input type="text" id="add_category" placeholder="Категорія (наприклад, 'Технології')"
                                   class="w-full p-2 border border-gray-300 rounded-md">
                            <input type="text" id="add_language" placeholder="Мова (наприклад, 'Українська')"
                                   class="w-full p-2 border border-gray-300 rounded-md">
                            <select id="add_status"
                                    class="w-full p-2 border border-gray-300 rounded-md focus:ring-blue-500 focus:border-blue-500">
                                <option value="active">Активне</option>
                                <option value="inactive">Неактивне</option>
                            </select>
                            <select id="add_type"
                                    class="w-full p-2 border border-gray-300 rounded-md focus:ring-blue-500 focus:border-blue-500">
                                <option value="html">HTML</option>
                                <option value="rss">RSS</option>
                            </select>
                            <textarea id="add_parser_settings" placeholder="Налаштування парсера (JSON, наприклад, {'title': 'h1.title'})"
                                      class="w-full p-2 border border-gray-300 rounded-md h-20 focus:ring-blue-500 focus:border-blue-500"></textarea>
                            <button type="submit"
                                    class="w-full bg-blue-600 text-white p-2 rounded-md hover:bg-blue-700 transition duration-200">Додати джерело</button>
                        </form>

                        <form id="updateSourceForm" class="mb-4 space-y-3">
                            <h3 class="text-lg font-medium mb-2">Оновити джерело</h3>
                            <input type="number" id="update_id" placeholder="ID джерела" required
                                   class="w-full p-2 border border-gray-300 rounded-md focus:ring-blue-500 focus:border-blue-500">
                            <input type="text" id="update_name" placeholder="Нова назва (залиште порожнім для пропуску)"
                                   class="w-full p-2 border border-gray-300 rounded-md">
                            <input type="text" id="update_category" placeholder="Нова категорія"
                                   class="w-full p-2 border border-gray-300 rounded-md">
                            <input type="text" id="update_language" placeholder="Нова мова"
                                   class="w-full p-2 border border-gray-300 rounded-md">
                            <select id="update_status"
                                    class="w-full p-2 border border-gray-300 rounded-md focus:ring-blue-500 focus:border-blue-500">
                                <option value="">Не змінювати</option>
                                <option value="active">Активне</option>
                                <option value="inactive">Неактивне</option>
                            </select>
                            <select id="update_type"
                                    class="w-full p-2 border border-gray-300 rounded-md focus:ring-blue-500 focus:border-blue-500">
                                <option value="">Не змінювати</option>
                                <option value="html">HTML</option>
                                <option value="rss">RSS</option>
                            </select>
                            <textarea id="update_parser_settings" placeholder="Нові налаштування парсера (JSON)"
                                      class="w-full p-2 border border-gray-300 rounded-md h-20 focus:ring-blue-500 focus:border-blue-500"></textarea>
                            <button type="submit"
                                    class="w-full bg-green-600 text-white p-2 rounded-md hover:bg-green-700 transition duration-200">Оновити джерело</button>
                        </form>

                        <form id="deleteSourceForm" class="space-y-3">
                            <h3 class="text-lg font-medium mb-2">Видалити джерело</h3>
                            <input type="number" id="delete_id" placeholder="ID джерела" required
                                   class="w-full p-2 border border-gray-300 rounded-md focus:ring-blue-500 focus:border-blue-500">
                            <button type="submit"
                                    class="w-full bg-red-600 text-white p-2 rounded-md hover:bg-red-700 transition duration-200">Видалити джерело</button>
                        </form>
                    </div>

                    <div class="mb-8 p-4 bg-yellow-50 rounded-lg">
                        <h2 class="text-xl font-semibold mb-3 text-yellow-800">Керування налаштуваннями бота</h2>
                        <form id="setBotSettingForm" class="space-y-3">
                            <h3 class="text-lg font-medium mb-2">Встановити налаштування бота</h3>
                            <input type="text" id="setting_key" placeholder="Ключ налаштування (наприклад, NEWS_PARSE_INTERVAL_MINUTES)" required
                                   class="w-full p-2 border border-gray-300 rounded-md focus:ring-yellow-500 focus:border-yellow-500">
                            <input type="text" id="setting_value" placeholder="Значення налаштування" required
                                   class="w-full p-2 border border-gray-300 rounded-md focus:ring-yellow-500 focus:border-yellow-500">
                            <button type="submit"
                                    class="w-full bg-yellow-600 text-white p-2 rounded-md hover:bg-yellow-700 transition duration-200">Встановити налаштування</button>
                        </form>
                    </div>

                    <div class="p-4 bg-purple-50 rounded-lg">
                        <h2 class="text-xl font-semibold mb-3 text-purple-800">Керування новинами</h2>
                        <form id="deleteNewsForm" class="space-y-3">
                            <h3 class="text-lg font-medium mb-2">Видалити новину за ID</h3>
                            <input type="number" id="news_delete_id" placeholder="ID новини" required
                                   class="w-full p-2 border border-gray-300 rounded-md focus:ring-purple-500 focus:border-purple-500">
                            <button type="submit"
                                    class="w-full bg-purple-600 text-white p-2 rounded-md hover:bg-purple-700 transition duration-200">Видалити новину</button>
                        </form>
                    </div>

                    <div id="messageBox" class="mt-6 p-3 rounded-md text-center hidden"></div>
                </div>

                <script>
                    const API_KEY = "{{ request.headers.get(API_KEY_NAME) }}"; // Get API key from request headers

                    function showMessage(message, type = 'success') {
                        const messageBox = document.getElementById('messageBox');
                        messageBox.textContent = message;
                        messageBox.className = 'mt-6 p-3 rounded-md text-center';
                        if (type === 'success') {
                            messageBox.classList.add('bg-green-200', 'text-green-800');
                        } else if (type === 'error') {
                            messageBox.classList.add('bg-red-200', 'text-red-800');
                        }
                        messageBox.classList.remove('hidden');
                        setTimeout(() => {
                            messageBox.classList.add('hidden');
                        }, 5000);
                    }

                    document.getElementById('addSourceForm').addEventListener('submit', async (e) => {
                        e.preventDefault();
                        const url = document.getElementById('add_url').value;
                        const name = document.getElementById('add_name').value;
                        const category = document.getElementById('add_category').value;
                        const language = document.getElementById('add_language').value;
                        const status = document.getElementById('add_status').value;
                        const type = document.getElementById('add_type').value;
                        let parser_settings = document.getElementById('add_parser_settings').value;

                        try {
                            parser_settings = parser_settings ? JSON.parse(parser_settings) : {};
                        } catch (error) {
                            showMessage('Некоректний JSON для налаштувань парсера.', 'error');
                            return;
                        }

                        const data = { url, name, category, language, status, source_type: type, parser_settings };

                        try {
                            const response = await fetch('/admin/sources', {
                                method: 'POST',
                                headers: {
                                    'Content-Type': 'application/json',
                                    'X-API-Key': API_KEY
                                },
                                body: JSON.stringify(data)
                            });
                            const result = await response.json();
                            if (response.ok) {
                                showMessage('Джерело успішно додано!');
                                e.target.reset();
                            } else {
                                showMessage(`Помилка: ${result.detail || 'Невідома помилка'}`, 'error');
                            }
                        } catch (error) {
                            showMessage(`Помилка мережі: ${error.message}`, 'error');
                        }
                    });

                    document.getElementById('updateSourceForm').addEventListener('submit', async (e) => {
                        e.preventDefault();
                        const id = document.getElementById('update_id').value;
                        const name = document.getElementById('update_name').value;
                        const category = document.getElementById('update_category').value;
                        const language = document.getElementById('update_language').value;
                        const status = document.getElementById('update_status').value;
                        const type = document.getElementById('update_type').value;
                        let parser_settings = document.getElementById('update_parser_settings').value;

                        const data = {};
                        if (name) data.name = name;
                        if (category) data.category = category;
                        if (language) data.language = language;
                        if (status) data.status = status;
                        if (type) data.source_type = type;
                        if (parser_settings) {
                            try {
                                data.parser_settings = JSON.parse(parser_settings);
                            } catch (error) {
                                showMessage('Некоректний JSON для налаштувань парсера.', 'error');
                                return;
                            }
                        }

                        if (Object.keys(data).length === 0) {
                            showMessage('Будь ласка, введіть хоча б одне поле для оновлення.', 'error');
                            return;
                        }

                        try {
                            const response = await fetch(`/admin/sources/${id}`, {
                                method: 'PUT',
                                headers: {
                                    'Content-Type': 'application/json',
                                    'X-API-Key': API_KEY
                                },
                                body: JSON.stringify(data)
                            });
                            const result = await response.json();
                            if (response.ok) {
                                showMessage('Джерело успішно оновлено!');
                                e.target.reset();
                            } else {
                                showMessage(`Помилка: ${result.detail || 'Невідома помилка'}`, 'error');
                            }
                        } catch (error) {
                            showMessage(`Помилка мережі: ${error.message}`, 'error');
                        }
                    });

                    document.getElementById('deleteSourceForm').addEventListener('submit', async (e) => {
                        e.preventDefault();
                        const id = document.getElementById('delete_id').value;

                        try {
                            const response = await fetch(`/admin/sources/${id}`, {
                                method: 'DELETE',
                                headers: {
                                    'X-API-Key': API_KEY
                                }
                            });
                            const result = await response.json();
                            if (response.ok) {
                                showMessage('Джерело успішно видалено!');
                                e.target.reset();
                            } else {
                                showMessage(`Помилка: ${result.detail || 'Невідома помилка'}`, 'error');
                            }
                        } catch (error) {
                            showMessage(`Помилка мережі: ${error.message}`, 'error');
                        }
                    });

                    document.getElementById('setBotSettingForm').addEventListener('submit', async (e) => {
                        e.preventDefault();
                        const setting_key = document.getElementById('setting_key').value;
                        const setting_value = document.getElementById('setting_value').value;

                        const data = { setting_key, setting_value };

                        try {
                            const response = await fetch('/admin/settings', {
                                method: 'PUT',
                                headers: {
                                    'Content-Type': 'application/json',
                                    'X-API-Key': API_KEY
                                },
                                body: JSON.stringify(data)
                            });
                            const result = await response.json();
                            if (response.ok) {
                                showMessage('Налаштування бота успішно оновлено!');
                                e.target.reset();
                            } else {
                                showMessage(`Помилка: ${result.detail || 'Невідома помилка'}`, 'error');
                            }
                        } catch (error) {
                            showMessage(`Помилка мережі: ${error.message}`, 'error');
                        }
                    });

                    document.getElementById('deleteNewsForm').addEventListener('submit', async (e) => {
                        e.preventDefault();
                        const news_id = document.getElementById('news_delete_id').value;

                        try {
                            const response = await fetch(`/admin/news/${news_id}`, {
                                method: 'DELETE',
                                headers: {
                                    'X-API-Key': API_KEY
                                }
                            });
                            const result = await response.json();
                            if (response.ok) {
                                showMessage('Новина успішно видалена!');
                                e.target.reset();
                            } else {
                                showMessage(`Помилка: ${result.detail || 'Невідома помилка'}`, 'error');
                            }
                        } catch (error) {
                            showMessage(`Помилка мережі: ${error.message}`, 'error');
                        }
                    });
                </script>
            </body>
            </html>
            """
            return HTMLResponse(content=html_content)

        @app.post("/admin/sources", status_code=status.HTTP_201_CREATED)
        async def create_source_web(source: SourceCreate, api_key: str = Depends(get_api_key)):
            try:
                new_source_id = await add_source(
                    url=source.url,
                    name=source.name,
                    category=source.category,
                    language=source.language,
                    status=source.status,
                    source_type=source.source_type,
                    parser_settings=source.parser_settings
                )
                if new_source_id:
                    return {"message": "Source added successfully", "id": new_source_id}
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Failed to add source")
            except Exception as e:
                logger.error(f"Web: Error adding source: {e}", exc_info=True)
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

        @app.put("/admin/sources/{source_id}")
        async def update_source_web(source_id: int, source_update: SourceUpdate, api_key: str = Depends(get_api_key)):
            try:
                # Convert Pydantic model to dict, excluding unset values
                update_data = source_update.model_dump(exclude_unset=True)
                if not update_data:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update provided")

                updated = await update_source(source_id, update_data)
                if updated:
                    return {"message": "Source updated successfully"}
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found or no changes made")
            except Exception as e:
                logger.error(f"Web: Error updating source {source_id}: {e}", exc_info=True)
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

        @app.delete("/admin/sources/{source_id}")
        async def delete_source_web(source_id: int, api_key: str = Depends(get_api_key)):
            try:
                deleted = await delete_source(source_id)
                if deleted:
                    return {"message": "Source deleted successfully"}
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")
            except Exception as e:
                logger.error(f"Web: Error deleting source {source_id}: {e}", exc_info=True)
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

        @app.put("/admin/settings")
        async def set_bot_setting_web(setting: BotSettingUpdate, api_key: str = Depends(get_api_key)):
            try:
                await set_bot_setting(setting.setting_key, setting.setting_value)
                return {"message": f"Setting '{setting.setting_key}' updated to '{setting.setting_value}'"}
            except Exception as e:
                logger.error(f"Web: Error setting bot setting '{setting.setting_key}': {e}", exc_info=True)
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

        @app.delete("/admin/news/{news_id}")
        async def delete_news_web(news_id: int, api_key: str = Depends(get_api_key)):
            try:
                deleted = await delete_news_item(news_id)
                if deleted:
                    return {"message": "News item deleted successfully"}
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="News item not found")
            except Exception as e:
                logger.error(f"Web: Error deleting news item {news_id}: {e}", exc_info=True)
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

