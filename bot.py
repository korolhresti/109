import asyncio
import logging
import logging.handlers
from datetime import datetime, timedelta, timezone, date
import os
import sys

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.utils.markdown import hlink

from aiohttp import ClientSession
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Імпорт лише необхідних функцій з database.py
from database import get_db_pool, get_all_active_sources, add_news_item, update_source_last_parsed, get_one_unsent_news_item, mark_news_as_sent, get_source_by_id, get_bot_setting

# Імпортуємо web_parser
import web_parser

# Налаштування логування
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

stream_handler = logging.StreamHandler()
stream_handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

# Завантажуємо змінні оточення
load_dotenv()

# Отримуємо токен бота та ID каналу з config.py
# Переконайтеся, що ці змінні встановлені у вашому файлі .env або config.py
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
NEWS_CHANNEL_ID = os.getenv("NEWS_CHANNEL_ID")
if NEWS_CHANNEL_ID:
    NEWS_CHANNEL_ID = int(NEWS_CHANNEL_ID)

# Ініціалізація бота
bot = Bot(token=TELEGRAM_BOT_TOKEN)

# Функція для парсингу одного джерела
async def parse_single_source_job(source):
    """
    Парсить одне джерело новин і зберігає нові статті в БД.
    """
    logger.info(f"Start parsing source: {source['source_name']} ({source['source_url']})")
    start_time = time.time()
    try:
        recent_news = await web_parser.fetch_recent_news_from_source(source['source_url'], limit=10)
        new_articles_count = 0
        for news_item in recent_news:
            if news_item:
                # Вставляємо новину в базу даних
                success = await add_news_item(
                    source_id=source['id'],
                    title=news_item.get('title'),
                    content=news_item.get('content'),
                    source_url=news_item['source_url'],
                    image_url=news_item.get('image_url'),
                    published_at=news_item.get('published_at')
                )
                if success:
                    new_articles_count += 1
        
        # Оновлюємо час останнього парсингу
        await update_source_last_parsed(source['id'])
        end_time = time.time()
        duration_ms = int((end_time - start_time) * 1000)
        logger.info(f"Finished parsing source: {source['source_name']}. New articles: {new_articles_count}. Duration: {duration_ms}ms.")

    except Exception as e:
        logger.error(f"Error parsing source {source['source_name']}: {e}", exc_info=True)


# Функція для парсингу всіх активних джерел
async def parse_all_sources_job():
    """
    Парсить усі активні джерела новин.
    """
    pool = await get_db_pool()
    sources = await get_all_active_sources()
    logger.info(f"Starting to parse {len(sources)} active sources.")
    
    tasks = [parse_single_source_job(source) for source in sources]
    await asyncio.gather(*tasks)

    logger.info("Finished parsing all active sources.")


# Функція для публікації однієї новини в канал
async def publish_news_to_channel_job():
    """
    Вибирає одну невідправлену новину з БД і публікує її в канал.
    """
    if not NEWS_CHANNEL_ID:
        logger.warning("NEWS_CHANNEL_ID is not set. Skipping news publishing.")
        return

    try:
        # Отримуємо одну невідправлену новину
        news_item = await get_one_unsent_news_item()
        
        if news_item:
            source = await get_source_by_id(news_item['source_id'])
            
            # Створюємо посилання на джерело новини
            source_link = hlink(source['source_name'], news_item['source_url'])

            # Формуємо текст повідомлення
            message_text = (
                f"<b>{news_item['title']}</b>\n\n"
                f"{news_item['content']}\n\n"
                f"🔗 Джерело: {source_link}"
            )
            
            # Якщо є зображення, відправляємо його
            if news_item['image_url']:
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
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True
                )
            
            # Позначаємо новину як відправлену
            await mark_news_as_sent(news_item['id'])
            logger.info(f"Successfully published news item ID {news_item['id']} to channel {NEWS_CHANNEL_ID}")
        else:
            logger.info("No unsent news items found to publish.")
    except Exception as e:
        logger.error(f"Error publishing news to channel: {e}", exc_info=True)


# Функція для запуску APScheduler
async def start_worker_jobs():
    """
    Ініціалізує та запускає планувальник завдань (APScheduler).
    """
    # Ініціалізуємо пул з'єднань до БД
    await get_db_pool()

    scheduler = AsyncIOScheduler()

    # Отримуємо інтервали з налаштувань бота
    parse_interval_minutes = int((await get_bot_setting('NEWS_PARSE_INTERVAL_MINUTES')) or '15')
    publish_interval_minutes = int((await get_bot_setting('NEWS_PUBLISH_INTERVAL_MINUTES')) or '5')
    
    logger.info(f"Scheduling news parsing job every {parse_interval_minutes} хвилин.")
    scheduler.add_job(parse_all_sources_job, 'interval', minutes=parse_interval_minutes, id='parse_job')
    
    logger.info(f"Scheduling news publishing job every {publish_interval_minutes} хвилин.")
    scheduler.add_job(publish_news_to_channel_job, 'interval', minutes=publish_interval_minutes, id='publish_job')
    
    scheduler.start()
    logger.info("Планувальник запущено.")
    try:
        # Залишаємо цикл подій запущеним для планувальника
        while True:
            await asyncio.sleep(3600) # Чекаємо годину, або безкінечно
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("Воркер бота вимкнено.")


if __name__ == "__main__":
    # Завантажуємо змінні оточення
    load_dotenv()
    
    if not TELEGRAM_BOT_TOKEN:
        logger.error("Змінна оточення TELEGRAM_BOT_TOKEN не встановлена.")
    
    # Перевіряємо NEWS_CHANNEL_ID після завантаження з config.py
    if not NEWS_CHANNEL_ID or NEWS_CHANNEL_ID == 0:
        logger.warning("Змінна оточення NEWS_CHANNEL_ID не встановлена або дорівнює 0. Новини не будуть поститись в канал.")
    
    # Запускаємо воркер, якщо передано аргумент "worker"
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        logger.info("Запускаю воркер бота (планувальник)...")
        asyncio.run(start_worker_jobs()) # Запускаємо асинхронну ініціалізацію воркера
    else:
        # Запустити веб-сервер, якщо потрібно (ця частина зараз не використовується)
        # Видалено, щоб відповідати `Procfile.txt`
        pass
