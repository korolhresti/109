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
from typing import List, Optional, Dict, Any

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
import httpx
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

# Імпорт з оновлених файлів
from database import get_db_pool, get_all_active_sources, add_news_item, update_source_last_parsed, get_one_unsent_news_item, mark_news_as_sent, get_source_by_id, get_bot_setting, get_user_by_telegram_id, add_user, update_user_last_active, get_user_by_id, update_user_ai_requests, add_source
import web_parser
import telegram_parser
import rss_parser
import social_media_parser
from config import TELEGRAM_BOT_TOKEN, NEWS_CHANNEL_ID, DATABASE_URL, WEBHOOK_URL, MONOBANK_CARD_NUMBER, GEMINI_API_KEY

# Налаштування логування
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

file_handler = logging.handlers.RotatingFileHandler('bot.log', maxBytes=10*1024*1024, backupCount=5)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

error_file_handler = logging.handlers.RotatingFileHandler('errors.log', maxBytes=10*1024*1024, backupCount=5)
error_file_handler.setLevel(logging.ERROR)
error_file_handler.setFormatter(formatter)
logger.addHandler(error_file_handler)

stream_handler = logging.StreamHandler()
stream_handler.setLevel(logging.INFO)
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

# Завантажуємо змінні оточення
load_dotenv()

API_TOKEN = TELEGRAM_BOT_TOKEN
NEWS_CHANNEL_LINK = NEWS_CHANNEL_ID
WEBHOOK_URL_BASE = WEBHOOK_URL
MONOBANK_CARD_NUMBER = MONOBANK_CARD_NUMBER
GEMINI_API_KEY = GEMINI_API_KEY
ADMIN_IDS = [123456789] # Placeholder, change with real admin IDs
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "your_admin_api_key")

HELP_BUY_CHANNEL_LINK = "https://t.me/+gT7TDOMh81M3YmY6"
HELP_SELL_BOT_LINK = "https://t.me/BigmoneycreateBot"

# Global settings
AI_REQUEST_LIMIT_DAILY_FREE = 3
GEMINI_MODEL = "gemini-2.5-flash-preview-05-20"
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"

app = FastAPI(title="Telegram AI News Bot API", version="1.0.0")
app.mount("/static", StaticFiles(directory="."), name="static")

api_key_header = APIKeyHeader(name="X-API-Key")

async def get_api_key(api_key: str = Depends(api_key_header)):
    if not ADMIN_API_KEY:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="ADMIN_API_KEY not configured.")
    if api_key is None or api_key != ADMIN_API_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key.")
    return api_key

bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

db_pool: Optional[AsyncConnectionPool] = None

# States for FSM
class NewsSources(StatesGroup):
    waiting_for_url = State()
    waiting_for_type = State()

class AskExpert(StatesGroup):
    waiting_for_question = State()

MESSAGES = {
    'uk': {
        'welcome': "Привіт, {first_name}! Я ваш AI News Bot. Оберіть дію:",
        'main_menu_prompt': "Оберіть дію:",
        'help_text': ("<b>Команди:</b>\n"
                      "/start - Почати\n"
                      "/menu - Меню\n"
                      "/cancel - Скасувати\n"
                      "/my_news - Мої новини\n"
                      "/add_source - Додати джерело\n"
                      "/my_sources - Мої джерела\n"
                      "/ask_expert - Експерт\n"
                      "/invite - Запросити\n"
                      "/subscribe - Підписки\n"
                      "/donate - Донат ☕\n"
                      "<b>AI:</b> під новиною.\n"
                      "<b>AI-медіа:</b> /ai_media_menu"),
        'action_cancelled': "Скасовано. Оберіть дію:",
        'add_source_prompt': "Надішліть URL джерела:",
        'invalid_url': "Невірний URL.",
        'source_url_not_found': "URL джерела не знайдено.",
        'source_added_success': "Джерело '{source_url}' додано!",
        'add_source_error': "Помилка додавання джерела.",
        'no_new_news': "Немає нових новин.",
        'news_not_found': "Новина не знайдена.",
        'no_more_news': "Більше новин немає.",
        'first_news': "Це перша новина.",
        'error_start_menu': "Помилка. Почніть з /menu.",
        'ai_functions_prompt': "AI-функції:",
        'ai_function_premium_only': "Лише для преміум.",
        'news_title_label': "Заголовок:",
        'news_content_label': "Зміст:",
        'published_at_label': "Опубліковано:",
        'news_progress': "Новина {current_index} з {total_news}",
        'read_source_btn': "🔗 Джерело",
        'ai_functions_btn': "🧠 AI-функції",
        'prev_btn': "⬅️ Попередня",
        'next_btn': "➡️ Далі",
        'main_menu_btn': "⬅️ Меню",
        'generating_ai_summary': "Генерую AI-резюме...",
        'ai_summary_label': "AI-резюме",
        'no_ai_summary': "AI-резюме не знайдено.",
        'generating_ai_audio': "Генерую аудіо...",
        'ai_expert_prompt': "Надішліть своє питання AI-експерту:",
        'ai_expert_generating': "AI-експерт готує відповідь...",
        'ai_expert_response_label': "Відповідь AI-експерта:",
        'ai_expert_limit_exceeded': "Ви досягли щоденного ліміту запитів до AI. Спробуйте завтра або отримайте преміум.",
        'admin_menu_prompt': "Адмін-меню:",
        'admin_news_stats_btn': "📊 Статистика новин",
        'admin_news_moderate_btn': "👁️ Модерувати новини",
        'admin_news_sources_btn': "🔧 Джерела новин",
        'admin_users_btn': "👥 Користувачі",
        'admin_settings_btn': "⚙️ Налаштування",
        'admin_menu_btn': "Адмін-меню",
        'back_btn': "⬅️ Назад",
        'add_source_url_prompt': "Надішліть URL джерела новин:",
        'add_source_type_prompt': "Оберіть тип джерела:",
        'source_type_web': "Веб-сайт",
        'source_type_rss': "RSS-стрічка",
        'source_type_telegram': "Telegram-канал",
        'source_type_social': "Соціальна мережа",
    }
}

def get_message(key, lang='uk', **kwargs):
    return MESSAGES.get(lang, {}).get(key, f"_{key}_").format(**kwargs)

async def call_gemini_api(prompt: str) -> Optional[str]:
    """
    Calls the Gemini API to generate text based on a prompt.
    """
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY is not set.")
        return "Помилка: API-ключ не налаштовано."

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}]
            }
        ],
        "safety_settings": [
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        ]
    }
    
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                GEMINI_API_URL,
                json=payload
            )
            response.raise_for_status()
            
            result = response.json()
            if result.get('candidates') and result['candidates'][0].get('content'):
                return result['candidates'][0]['content']['parts'][0]['text']
            else:
                logger.error(f"Gemini API response format error: {result}")
                return "Помилка при отриманні відповіді від AI."

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error with Gemini API: {e}")
        return "Помилка при з'єднанні з AI."
    except Exception as e:
        logger.error(f"Error calling Gemini API: {e}", exc_info=True)
        return "Невідома помилка при роботі з AI."

# === Handlers for user commands ===
@router.message(CommandStart())
async def command_start_handler(message: Message):
    user_info = message.from_user
    pool = await get_db_pool()
    user = await get_user_by_telegram_id(pool, user_info.id)
    if not user:
        user_id = await add_user(pool, user_info.id, user_info.username, user_info.first_name, user_info.last_name)
        user = await get_user_by_id(pool, user_id)
    else:
        await update_user_last_active(pool, user_info.id)
    await message.answer(get_message('welcome', first_name=user_info.first_name))
    await show_main_menu(message)

async def show_main_menu(message: Message, lang='uk'):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Мої новини", callback_data="show_my_news"))
    builder.row(InlineKeyboardButton(text="Мої джерела", callback_data="show_my_sources"))
    builder.row(InlineKeyboardButton(text="Підписка", callback_data="show_subscription"))
    builder.row(InlineKeyboardButton(text="Експерт AI", callback_data="ask_expert_menu"))
    if message.from_user.id in ADMIN_IDS:
        builder.row(InlineKeyboardButton(text="Адмін-меню", callback_data="show_admin_menu"))
    await message.answer(get_message('main_menu_prompt', lang), reply_markup=builder.as_markup())

@router.message(Command("menu"))
async def command_menu_handler(message: Message):
    await show_main_menu(message)

@router.message(Command("help"))
async def command_help_handler(message: Message):
    await message.answer(get_message('help_text'))

@router.message(Command("cancel"))
async def command_cancel_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(get_message('action_cancelled'))
    await show_main_menu(message)

@router.message(Command("add_source"))
async def command_add_source(message: Message, state: FSMContext):
    await message.answer(get_message('add_source_url_prompt'))
    await state.set_state(NewsSources.waiting_for_url)

@router.message(NewsSources.waiting_for_url)
async def process_source_url(message: Message, state: FSMContext):
    source_url = message.text
    # Basic URL validation
    if not (source_url.startswith('http://') or source_url.startswith('https://')):
        await message.answer(get_message('invalid_url'))
        return
    
    await state.update_data(source_url=source_url)
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=get_message('source_type_web'), callback_data="source_type_web"))
    builder.row(InlineKeyboardButton(text=get_message('source_type_rss'), callback_data="source_type_rss"))
    builder.row(InlineKeyboardButton(text=get_message('source_type_telegram'), callback_data="source_type_telegram"))
    builder.row(InlineKeyboardButton(text=get_message('source_type_social'), callback_data="source_type_social"))
    
    await message.answer(get_message('add_source_type_prompt'), reply_markup=builder.as_markup())
    await state.set_state(NewsSources.waiting_for_type)

@router.callback_query(NewsSources.waiting_for_type, F.data.startswith("source_type_"))
async def process_source_type(callback: CallbackQuery, state: FSMContext):
    source_type = callback.data.replace("source_type_", "")
    user_data = await state.get_data()
    source_url = user_data.get('source_url')
    pool = await get_db_pool()
    
    try:
        source_id = await add_source(pool, source_url=source_url, source_type=source_type)
        if source_id:
            await callback.message.answer(get_message('source_added_success', source_url=source_url))
        else:
            await callback.message.answer(get_message('add_source_error'))
    except Exception as e:
        logger.error(f"Error adding source: {e}", exc_info=True)
        await callback.message.answer(get_message('add_source_error'))
    
    await state.clear()
    await show_main_menu(callback.message)
    await callback.answer()

@router.callback_query(F.data == "ask_expert_menu")
async def callback_ask_expert(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(get_message('ai_expert_prompt'))
    await state.set_state(AskExpert.waiting_for_question)
    await callback.answer()

@router.message(AskExpert.waiting_for_question, F.text)
async def process_expert_question(message: Message, state: FSMContext):
    user_id = message.from_user.id
    pool = await get_db_pool()
    user = await get_user_by_telegram_id(pool, user_id)
    
    if not user or (user.get('ai_requests_today', 0) >= AI_REQUEST_LIMIT_DAILY_FREE and not user.get('is_premium', False)):
        await message.answer(get_message('ai_expert_limit_exceeded'))
        await state.clear()
        return

    await message.answer(get_message('ai_expert_generating'))
    
    prompt = message.text
    response_text = await call_gemini_api(prompt)

    await update_user_ai_requests(pool, user_id)
    await message.answer(f"{get_message('ai_expert_response_label')}\n\n{response_text}")
    await state.clear()
    await show_main_menu(message)


# === Scheduler jobs from bot.py ===
async def parse_news_from_sources_job():
    logger.info("Починаю завдання парсингу новин.")
    pool = await get_db_pool()
    sources = await get_all_active_sources(pool)
    if not sources:
        logger.warning("Не знайдено активних джерел для парсингу.")
        return

    async def parse_single_source(source):
        try:
            news_items = []
            source_type = source.get('source_type')
            source_url = source.get('source_url')
            
            if source_type == 'rss':
                news_items = await rss_parser.fetch_recent_news_from_rss(source_url)
            elif source_type == 'telegram':
                # Assuming source_url for Telegram is the channel username or ID
                news_items = await telegram_parser.fetch_recent_news_from_channel(bot, source_url)
            elif source_type == 'social':
                news_items = await social_media_parser.fetch_recent_news_from_social_media(source_url)
            else: # Default to web parser
                news_items = await web_parser.fetch_recent_news_from_source(source_url)

            for news in news_items:
                await add_news_item(
                    pool,
                    source['id'],
                    news.get('title'),
                    news.get('content'),
                    news.get('source_url'),
                    news.get('image_url'),
                    news.get('published_at')
                )
            await update_source_last_parsed(pool, source['id'])
            logger.info(f"Успішно опрацьовано джерело: {source['source_name']}")
        except Exception as e:
            logger.error(f"Помилка при парсингу джерела {source.get('source_name', 'Unknown')}: {e}", exc_info=True)

    tasks = [parse_single_source(s) for s in sources]
    await asyncio.gather(*tasks)
    logger.info("Завдання парсингу новин завершено.")

async def publish_news_to_channel_job():
    logger.info("Починаю завдання публікації новин.")
    pool = await get_db_pool()
    news_item = await get_one_unsent_news_item(pool)

    if news_item:
        source_info = await get_source_by_id(pool, news_item['source_id'])
        if not source_info:
            logger.error(f"Джерело для новини {news_item['id']} не знайдено.")
            return

        text = (
            f"<b>{news_item['title']}</b>\n\n"
            f"{news_item['content']}\n\n"
            f"🔗 Джерело: {hlink(source_info.get('source_name', 'Джерело'), news_item['source_url'])}"
        )
        
        try:
            if news_item['image_url']:
                await bot.send_photo(
                    chat_id=NEWS_CHANNEL_ID,
                    photo=news_item['image_url'],
                    caption=text,
                    parse_mode=ParseMode.HTML
                )
            else:
                await bot.send_message(
                    chat_id=NEWS_CHANNEL_ID,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True
                )
            await mark_news_as_sent(pool, news_item['id'])
            logger.info(f"Новину {news_item['id']} опубліковано.")
        except Exception as e:
            logger.error(f"Помилка при публікації новини {news_item['id']}: {e}", exc_info=True)
    else:
        logger.info("Немає нових новин для публікації.")


# === FastAPI routes ===
@app.post("/" + API_TOKEN)
async def bot_webhook(request: Request):
    update = types.Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"status": "ok"}

@app.on_event("startup")
async def on_startup():
    global db_pool
    # Initialize DB pool
    db_pool = await get_db_pool()

    # Set webhook
    if WEBHOOK_URL_BASE:
        webhook_url = f"{WEBHOOK_URL_BASE}/{API_TOKEN}"
        await bot.set_webhook(webhook_url)
        logger.info(f"Webhook встановлено на {webhook_url}")

    # Start scheduler
    scheduler = AsyncIOScheduler()
    parse_interval_minutes = int(await get_bot_setting(db_pool, 'PARSE_INTERVAL_MINUTES', '15'))
    publish_interval_minutes = int(await get_bot_setting(db_pool, 'PUBLISH_INTERVAL_MINUTES', '15'))

    logger.info(f"Scheduling news parsing job every {parse_interval_minutes} minutes.")
    scheduler.add_job(parse_news_from_sources_job, 'interval', minutes=parse_interval_minutes, id='parse_job', args=[bot])
    
    logger.info(f"Scheduling news publishing job every {publish_interval_minutes} minutes.")
    scheduler.add_job(publish_news_to_channel_job, 'interval', minutes=publish_interval_minutes, id='publish_job')
    
    scheduler.start()
    app.state.scheduler = scheduler
    logger.info("Scheduler started.")

@app.on_event("shutdown")
async def on_shutdown():
    await bot.session.close()
    if db_pool:
        await db_pool.close()
    if hasattr(app.state, 'scheduler'):
        app.state.scheduler.shutdown()
        logger.info("Scheduler shutdown.")

@app.get("/api/admin/news")
async def get_admin_news_api(api_key: str = Depends(get_api_key)):
    # Placeholder for fetching news from DB
    return {"message": "Admin news list"}

if __name__ == "__main__":
    import uvicorn
    # Make sure to pass host and port from environment variables
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

