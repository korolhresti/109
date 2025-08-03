import asyncio
import logging
import logging.handlers
from datetime import datetime, timedelta, timezone, date
import os
import sys
from typing import Optional, Dict, Any

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.utils.markdown import hlink

from aiohttp import ClientSession
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import psycopg
from psycopg.rows import dict_row

# Імпорт лише необхідних функцій з database.py та web_parser.py
from database import (
    get_db_pool,
    get_all_active_sources,
    add_news_item,
    update_source_last_parsed,
    get_one_unsent_news_item,
    mark_news_as_sent,
    get_source_by_id,
    get_bot_setting
)
from web_parser import fetch_recent_news_from_source, fetch_page_content

# --- Налаштування логування ---
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
stream_handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

# --- Глобальні змінні з config.py ---
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
NEWS_CHANNEL_ID = os.getenv("NEWS_CHANNEL_ID")

if not TELEGRAM_BOT_TOKEN:
    logger.error("Змінна оточення TELEGRAM_BOT_TOKEN не встановлена.")
    sys.exit(1)

if not NEWS_CHANNEL_ID:
    logger.warning("Змінна оточення NEWS_CHANNEL_ID не встановлена. Новини не будуть поститись в канал.")
    NEWS_CHANNEL_ID = None
else:
    try:
        NEWS_CHANNEL_ID = int(NEWS_CHANNEL_ID)
    except ValueError:
        logger.error("Змінна оточення NEWS_CHANNEL_ID має бути цілим числом.")
        sys.exit(1)

# --- Бота та планувальник ---
bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
scheduler = AsyncIOScheduler()

async def parse_all_sources_job():
    """
    Планувальник-завдання для парсингу новин з усіх активних джерел.
    """
    logger.info("Запускаю завдання парсингу новин...")
    db_pool = await get_db_pool()
    sources = await get_all_active_sources(db_pool)
    if not sources:
        logger.info("Не знайдено активних джерел для парсингу.")
        return

    for source in sources:
        try:
            logger.info(f"Парсинг джерела: {source['source_name']} ({source['source_url']})")
            
            recent_news = await fetch_recent_news_from_source(source['source_url'], limit=20)
            if not recent_news:
                logger.warning(f"Не вдалося отримати нові новини з джерела {source['source_name']}.")
                continue
            
            for news_item in recent_news:
                # Перевіряємо, чи новина вже існує в базі даних за URL
                existing_news = await get_news_by_url(db_pool, news_item['source_url'])
                if existing_news:
                    logger.debug(f"Новина вже існує: {news_item['source_url']}")
                    continue
                
                # Додаємо нову новину в базу
                news_id = await add_news_item(
                    db_pool,
                    source_id=source['id'],
                    title=news_item.get('title'),
                    content=news_item.get('content'),
                    image_url=news_item.get('image_url'),
                    source_url=news_item['source_url'],
                    published_at=news_item.get('published_at'),
                    is_sent=False
                )
                logger.info(f"Додано нову новину з ID: {news_id} для джерела {source['source_name']}")
            
            await update_source_last_parsed(db_pool, source['id'])
            logger.info(f"Парсинг джерела {source['source_name']} завершено.")
        except Exception as e:
            logger.error(f"Помилка при парсингу джерела {source['source_name']}: {e}", exc_info=True)

async def publish_news_to_channel_job():
    """
    Планувальник-завдання для публікації новин в Telegram-канал.
    """
    if not NEWS_CHANNEL_ID:
        logger.warning("ID каналу для публікації новин не встановлено. Завдання пропускається.")
        return

    logger.info("Запускаю завдання публікації новин...")
    db_pool = await get_db_pool()
    
    # Витягуємо одну найстарішу, ще не надіслану новину
    news_item = await get_one_unsent_news_item(db_pool)
    if not news_item:
        logger.info("Немає нових новин для публікації.")
        return

    try:
        source_info = await get_source_by_id(db_pool, news_item['source_id'])
        if not source_info:
            logger.error(f"Джерело з ID {news_item['source_id']} не знайдено.")
            await mark_news_as_sent(db_pool, news_item['id'])
            return

        # Форматуємо повідомлення
        title = news_item.get('title', 'Без заголовку')
        source_name = source_info.get('source_name', 'Невідоме джерело')
        source_url = news_item.get('source_url', 'https://example.com')
        
        message_text = (
            f"<b>{title}</b>\n\n"
            f"Джерело: {hlink(source_name, source_url)}\n\n"
            f"{news_item.get('content', '')[:500]}..." # Обрізаємо контент для каналу
        )
        
        # Відправляємо новину
        if news_item.get('image_url'):
            await bot.send_photo(
                chat_id=NEWS_CHANNEL_ID,
                photo=news_item['image_url'],
                caption=message_text,
                parse_mode=ParseMode.HTML
            )
        else:
            await bot.send_message(
                chat_id=NEWS_CHANNEL_ID,
                text=message_text,
                parse_mode=ParseMode.HTML
            )
        
        # Позначаємо новину як відправлену
        await mark_news_as_sent(db_pool, news_item['id'])
        logger.info(f"Новину з ID: {news_item['id']} успішно опубліковано в канал.")

    except Exception as e:
        logger.error(f"Помилка при публікації новини з ID {news_item['id']}: {e}", exc_info=True)
        # У разі помилки, позначаємо новину як відправлену, щоб не намагатися надіслати її знову
        await mark_news_as_sent(db_pool, news_item['id'])


async def start_worker_jobs():
    """
    Ініціалізує і запускає планувальник завдань.
    """
    parse_interval_minutes = int(await get_bot_setting('NEWS_PARSE_INTERVAL_MINUTES', '15'))
    publish_interval_minutes = int(await get_bot_setting('NEWS_PUBLISH_INTERVAL_MINUTES', '5'))

    logger.info(f"Планую завдання парсингу новин кожні {parse_interval_minutes} хвилин.")
    scheduler.add_job(parse_all_sources_job, 'interval', minutes=parse_interval_minutes, id='parse_job')
    
    logger.info(f"Планую завдання публікації новин в канал кожні {publish_interval_minutes} хвилин.")
    scheduler.add_job(publish_news_to_channel_job, 'interval', minutes=publish_interval_minutes, id='publish_job')
    
    scheduler.start()
    logger.info("Планувальник запущено.")
    try:
        # Залишаємо цикл подій запущеним для планувальника
        while True:
            await asyncio.sleep(3600)  # Чекаємо годину, або безкінечно
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("Воркер бота вимкнено.")

if __name__ == "__main__":
    asyncio.run(start_worker_jobs())
