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
            open=psycopg.AsyncConnection.connect
        )
        logger.info("Пул з'єднань з БД ініціалізовано.")
    return db_pool

# Helper function to get row as dictionary
def dict_row(cursor):
    rec = cursor.fetchone()
    if rec:
        columns = [desc[0] for desc in cursor.description]
        return dict(zip(columns, rec))
    return None

async def get_all_active_sources(pool) -> List[Dict[str, Any]]:
    query = "SELECT * FROM sources WHERE is_active = TRUE;"
    async with pool.connection(row_factory=dict_row) as conn:
        async with conn.cursor() as cur:
            await cur.execute(query)
            return await cur.fetchall()

async def add_news_item(pool, source_id: int, title: str, content: str, source_url: str, image_url: Optional[str], published_at: datetime) -> Optional[int]:
    query = """
    INSERT INTO news (source_id, title, content, source_url, image_url, published_at, is_sent)
    VALUES (%s, %s, %s, %s, %s, %s, FALSE)
    ON CONFLICT (source_url) DO NOTHING
    RETURNING id;
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(query, (source_id, title, content, source_url, image_url, published_at))
                news_id = await cur.fetchone()
                if news_id:
                    news_id = news_id[0]
                await conn.commit()
                return news_id
            except Exception as e:
                logger.error(f"Помилка при додаванні новини: {e}")
                await conn.rollback()
                return None

async def add_source(pool, source_url: str, source_type: str, source_name: Optional[str] = None) -> Optional[int]:
    query = """
    INSERT INTO sources (source_url, source_type, source_name, is_active)
    VALUES (%s, %s, %s, TRUE)
    RETURNING id;
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(query, (source_url, source_type, source_name))
                source_id = (await cur.fetchone())[0]
                await conn.commit()
                return source_id
            except Exception as e:
                logger.error(f"Помилка при додаванні джерела: {e}")
                await conn.rollback()
                return None

async def update_source_last_parsed(pool, source_id: int):
    query = "UPDATE sources SET last_parsed = %s WHERE id = %s;"
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, (datetime.now(timezone.utc), source_id))
            await conn.commit()

async def get_one_unsent_news_item(pool) -> Optional[Dict[str, Any]]:
    query = "SELECT * FROM news WHERE is_sent = FALSE ORDER BY published_at ASC LIMIT 1;"
    async with pool.connection(row_factory=dict_row) as conn:
        async with conn.cursor() as cur:
            await cur.execute(query)
            return await cur.fetchone()

async def mark_news_as_sent(pool, news_id: int):
    query = "UPDATE news SET is_sent = TRUE WHERE id = %s;"
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, (news_id,))
            await conn.commit()

async def get_source_by_id(pool, source_id: int) -> Optional[Dict[str, Any]]:
    query = "SELECT * FROM sources WHERE id = %s;"
    async with pool.connection(row_factory=dict_row) as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, (source_id,))
            return await cur.fetchone()

async def get_bot_setting(pool, key: str, default: str) -> str:
    query = "SELECT setting_value FROM bot_settings WHERE setting_key = %s;"
    async with pool.connection(row_factory=dict_row) as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, (key,))
            result = await cur.fetchone()
            if result:
                return result['setting_value']
            return default

async def get_user_by_telegram_id(pool, telegram_id: int) -> Optional[Dict[str, Any]]:
    query = "SELECT * FROM users WHERE telegram_id = %s;"
    async with pool.connection(row_factory=dict_row) as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, (telegram_id,))
            return await cur.fetchone()

async def get_user_by_id(pool, user_id: int) -> Optional[Dict[str, Any]]:
    query = "SELECT * FROM users WHERE id = %s;"
    async with pool.connection(row_factory=dict_row) as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, (user_id,))
            return await cur.fetchone()

async def add_user(pool, telegram_id: int, username: Optional[str], first_name: Optional[str], last_name: Optional[str]) -> int:
    query = """
    INSERT INTO users (telegram_id, username, first_name, last_name) 
    VALUES (%s, %s, %s, %s)
    RETURNING id;
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, (telegram_id, username, first_name, last_name))
            user_id = (await cur.fetchone())[0]
            await conn.commit()
            return user_id

async def update_user_last_active(pool, telegram_id: int):
    query = "UPDATE users SET last_active = %s WHERE telegram_id = %s;"
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, (datetime.now(timezone.utc), telegram_id))
            await conn.commit()

async def update_user_ai_requests(pool, telegram_id: int):
    query = "UPDATE users SET ai_requests_today = ai_requests_today + 1, ai_last_request_date = %s WHERE telegram_id = %s;"
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, (date.today(), telegram_id))
            await conn.commit()
