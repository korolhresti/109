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
NEWS_CHANNEL_ID = int(os.getenv("NEWS_CHANNEL_ID", "0")) # За замовчуванням 0, якщо не встановлено

# Ініціалізація бота
bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

# Ініціалізація планувальника завдань
scheduler = AsyncIOScheduler()

# --- Заплановані завдання ---

async def parse_all_sources_job():
    """
    Завдання для парсингу новин з усіх активних джерел.
    """
    logger.info("Запускаю заплановане завдання парсингу новин...")
    active_sources = await get_all_active_sources()
    if not active_sources:
        logger.info("Не знайдено активних джерел для парсингу.")
        return

    for source in active_sources:
        try:
            logger.info(f"Парсинг джерела: {source['name']} ({source['url']})")
            recent_news_items = await web_parser.fetch_recent_news_from_source(source['url'], limit=5)
            
            if recent_news_items:
                for parsed_data in recent_news_items:
                    try:
                        # Додаємо новину до бази даних. ON CONFLICT (source_url) DO NOTHING
                        # запобігає додаванню дублікатів.
                        news_id = await add_news_item(
                            source['id'],
                            parsed_data['title'],
                            parsed_data['content'],
                            parsed_data['source_url'],
                            parsed_data.get('image_url'),
                            parsed_data.get('published_at'),
                            parsed_data.get('lang', 'uk')
                        )
                        if news_id: # Якщо news_id повернуто, новина була додана
                            logger.info(f"Додано нову новину з {source['name']}: {parsed_data['title']} (ID: {news_id})")
                        else: # Якщо news_id = 0, новина вже існувала
                            logger.info(f"Новина з URL {parsed_data['source_url']} вже існує. Пропускаю.")
                    except Exception as e:
                        logger.error(f"Помилка при додаванні новини з {source['name']}: {e}", exc_info=True)
            else:
                logger.warning(f"Не знайдено нових новин або не вдалося розпарсити з {source['name']} ({source['url']})")
            
            # Оновлюємо час останнього парсингу джерела
            await update_source_last_parsed(source['id'], datetime.now(timezone.utc))
        except Exception as e:
            logger.error(f"Помилка під час парсингу джерела {source.get('name', source['url'])}: {e}", exc_info=True)
    logger.info("Заплановане завдання парсингу новин завершено.")

async def publish_news_to_channel_job():
    """
    Завдання для публікації однієї невідправленої новини в канал.
    """
    logger.info("Запускаю заплановане завдання публікації новин...")
    if not NEWS_CHANNEL_ID or NEWS_CHANNEL_ID == 0:
        logger.warning("NEWS_CHANNEL_ID не встановлено або дорівнює 0. Пропускаю публікацію новин в канал.")
        return

    logger.info(f"Спроба отримати одну невідправлену новину для каналу {NEWS_CHANNEL_ID}...")
    news_item = await get_one_unsent_news_item()
    
    if news_item:
        logger.info(f"Знайдено невідправлену новину: ID={news_item['id']}, Заголовок='{news_item['title']}'")
        try:
            source = await get_source_by_id(news_item['source_id'])
            source_name = source['name'] if source else "Невідоме джерело"
            
            # Обрізаємо контент для публікації в канал, щоб уникнути занадто довгих повідомлень
            truncated_content = news_item['content']
            if len(truncated_content) > 500:
                truncated_content = truncated_content[:500] + "..."

            channel_post_text = (
                f"<b>Нова новина з {source_name}!</b>\n\n"
                f"<b>{news_item['title']}</b>\n"
                f"{truncated_content}\n\n"
                f"{hlink('Читати далі', news_item['source_url'])}"
            )
            
            if news_item.get('image_url'):
                logger.info(f"Відправляю фото з підписом для новини ID {news_item['id']} до каналу {NEWS_CHANNEL_ID} (Зображення: {news_item['image_url']}).")
                await bot.send_photo(NEWS_CHANNEL_ID, photo=news_item['image_url'], caption=channel_post_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            else:
                logger.info(f"Відправляю текстове повідомлення для новини ID {news_item['id']} до каналу {NEWS_CHANNEL_ID}.")
                await bot.send_message(NEWS_CHANNEL_ID, channel_post_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            
            # Позначаємо новину як відправлену
            await mark_news_as_sent(news_item['id'])
            logger.info(f"Новина {news_item['id']} опублікована в канал {NEWS_CHANNEL_ID} та позначена як відправлена.")
        except Exception as e:
            logger.error(f"Не вдалося опублікувати новину {news_item['id']} в канал {NEWS_CHANNEL_ID}: {e}", exc_info=True)
            # Можливо, тут варто додати логіку для повторної спроби або позначення новини як "невдало відправленої"
    else:
        logger.info("Не знайдено невідправлених новин для публікації.")
    logger.info("Заплановане завдання публікації новин завершено.")

# --- Функція запуску воркера ---

async def start_worker_jobs():
    """
    Ініціалізує пул підключень до БД та планує завдання воркера.
    Ця функція є асинхронною і буде запускатися за допомогою asyncio.run().
    """
    await get_db_pool()
    
    # Отримуємо інтервали з налаштувань бота або використовуємо значення за замовчуванням
    parse_interval_minutes_str = await get_bot_setting("NEWS_PARSE_INTERVAL_MINUTES")
    publish_interval_minutes_str = await get_bot_setting("NEWS_PUBLISH_INTERVAL_MINUTES")

    parse_interval_minutes = int(parse_interval_minutes_str) if parse_interval_minutes_str else 15
    publish_interval_minutes = int(publish_interval_minutes_str) if publish_interval_minutes_str else 5

    logger.info(f"Планую завдання парсингу новин кожні {parse_interval_minutes} хвилин.")
    scheduler.add_job(parse_all_sources_job, 'interval', minutes=parse_interval_minutes, id='parse_job')
    
    logger.info(f"Планую завдання публікації новин кожні {publish_interval_minutes} хвилин.")
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
        logger.info("Цей скрипт призначений для запуску як воркер. Для запуску веб-частини використовуйте uvicorn.")
        # Тут можна додати логіку для локального запуску, якщо це потрібно,
        # але для спрощення ми зосереджуємося на функціоналі воркера.
