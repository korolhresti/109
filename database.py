import os
import logging
import psycopg
from psycopg_pool import AsyncConnectionPool
from dotenv import load_dotenv
from typing import List, Dict, Any, Optional, Union
from datetime import datetime, timezone, date
import json

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
stream_handler = logging.StreamHandler()
stream_handler.setLevel(logging.INFO)
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

db_pool: Optional[AsyncConnectionPool] = None

async def get_db_pool():
    """
    Ініціалізує та повертає пул з'єднань до бази даних.
    Якщо пул вже існує, повертає існуючий.
    """
    global db_pool
    if db_pool is None:
        if not DATABASE_URL:
            logger.error("Змінна оточення DATABASE_URL не встановлена.")
            raise ValueError("DATABASE_URL environment variable is not set.")
        
        conninfo = DATABASE_URL
        if "sslmode" not in conninfo:
            conninfo += "?sslmode=require"

        db_pool = AsyncConnectionPool(
            conninfo,
            min_size=1,
            max_size=10,
            open=True,
            timeout=10,
            name='main_pool'
        )
    return db_pool

async def get_bot_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """
    Отримує значення налаштування бота з бази даних.
    """
    query = "SELECT setting_value FROM bot_settings WHERE setting_key = %s;"
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(query, (key,))
                result = await cur.fetchone()
                return result[0] if result else default
            except Exception as e:
                logger.error(f"Помилка при отриманні налаштування '{key}': {e}")
                return default

async def get_all_active_sources(pool) -> List[Dict[str, Any]]:
    """
    Отримує список усіх активних джерел новин з бази даних.
    """
    query = "SELECT * FROM sources WHERE is_active = TRUE ORDER BY id;"
    async with pool.connection(row_factory=dict_row) as conn:
        async with conn.cursor() as cur:
            await cur.execute(query)
            return await cur.fetchall()

async def get_source_by_id(pool, source_id: int) -> Optional[Dict[str, Any]]:
    """
    Отримує інформацію про джерело за його ID.
    """
    query = "SELECT * FROM sources WHERE id = %s;"
    async with pool.connection(row_factory=dict_row) as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, (source_id,))
            return await cur.fetchone()

async def get_news_by_url(pool, url: str) -> Optional[Dict[str, Any]]:
    """
    Перевіряє, чи існує новина з таким URL.
    """
    query = "SELECT id FROM news WHERE source_url = %s;"
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, (url,))
            return await cur.fetchone()

async def add_news_item(pool, source_id: int, title: str, content: str, image_url: Optional[str], source_url: str, published_at: datetime, is_sent: bool) -> Optional[int]:
    """
    Додає нову новину в базу даних.
    """
    query = """
    INSERT INTO news (source_id, title, content, image_url, source_url, published_at, is_sent)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    RETURNING id;
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(query, (source_id, title, content, image_url, source_url, published_at, is_sent))
                news_id = (await cur.fetchone())[0]
                await conn.commit()
                return news_id
            except Exception as e:
                logger.error(f"Помилка при додаванні новини: {e}")
                await conn.rollback()
                return None

async def update_source_last_parsed(pool, source_id: int):
    """
    Оновлює час останнього парсингу для джерела.
    """
    query = "UPDATE sources SET last_parsed = %s WHERE id = %s;"
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, (datetime.now(timezone.utc), source_id))
            await conn.commit()

async def get_one_unsent_news_item(pool) -> Optional[Dict[str, Any]]:
    """
    Отримує одну найстарішу, ще не надіслану новину.
    """
    query = "SELECT * FROM news WHERE is_sent = FALSE ORDER BY published_at ASC LIMIT 1;"
    async with pool.connection(row_factory=dict_row) as conn:
        async with conn.cursor() as cur:
            await cur.execute(query)
            return await cur.fetchone()

async def mark_news_as_sent(pool, news_id: int):
    """
    Позначає новину як надіслану.
    """
    query = "UPDATE news SET is_sent = TRUE, sent_at = %s WHERE id = %s;"
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, (datetime.now(timezone.utc), news_id))
            await conn.commit()
