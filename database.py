import os
import logging
import psycopg
from psycopg_pool import AsyncConnectionPool
from dotenv import load_dotenv
from typing import List, Dict, Any, Optional
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
    Додано обробку помилок з'єднання та повторну ініціалізацію пулу.
    """
    global db_pool
    if db_pool is None:
        if not DATABASE_URL:
            logger.error("DATABASE_URL environment variable is not set.")
            raise ValueError("DATABASE_URL environment variable is not set.")
        
        conninfo = DATABASE_URL
        if "sslmode" not in conninfo:
            conninfo += "?sslmode=require"

        try:
            db_pool = AsyncConnectionPool(
                conninfo=conninfo,
                min_size=1,
                max_size=10,
                open=psycopg.AsyncConnection.connect,
                reconnect_timeout=30 # Try to reconnect for 30 seconds
            )
            # Перевіряємо з'єднання
            async with db_pool.connection() as conn:
                await conn.execute("SELECT 1")
            logger.info("DB pool initialized successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize DB pool: {e}", exc_info=True)
            # Якщо ініціалізація не вдалася, закриваємо пул, якщо він був частково створений, і повторно викликаємо
            if db_pool:
                await db_pool.close()
                db_pool = None # Скидаємо пул, щоб він був ініціалізований знову
            raise # Повторно викликаємо помилку, щоб вона була оброблена вище
    
    # Додаткова перевірка перед поверненням пулу, якщо він вже існує
    # Це може допомогти уникнути використання "поганих" з'єднань, хоча reconnect_timeout має це робити
    try:
        async with db_pool.connection() as conn:
            await conn.execute("SELECT 1") # Проста перевірка, чи з'єднання ще працює
    except psycopg.OperationalError as e:
        logger.warning(f"Existing DB pool connection check failed: {e}. Attempting to re-initialize pool.")
        if db_pool:
            await db_pool.close()
        db_pool = None # Скидаємо пул для повторної ініціалізації
        return await get_db_pool() # Рекурсивний виклик для повторної ініціалізації
    except Exception as e:
        logger.error(f"Unexpected error during existing DB pool connection check: {e}", exc_info=True)
        raise

    return db_pool

async def get_user_by_telegram_id(telegram_id: int) -> Optional[Dict[str, Any]]:
    """
    Отримує користувача за його Telegram ID.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            try:
                await cur.execute("SELECT * FROM users WHERE telegram_id = %s;", (telegram_id,))
                return await cur.fetchone()
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in get_user_by_telegram_id: {e}. Retrying connection.", exc_info=True)
                # Якщо з'єднання закрилося, спробуємо отримати нове з пулу і повторити
                # Це може бути надмірним, оскільки пул має це обробляти, але для надійності
                pool = await get_db_pool() # Отримати потенційно новий/свіжий пул
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor(row_factory=psycopg.rows.dict_row) as cur_retry:
                        await cur_retry.execute("SELECT * FROM users WHERE telegram_id = %s;", (telegram_id,))
                        return await cur_retry.fetchone()
            except Exception as e:
                logger.error(f"Error in get_user_by_telegram_id: {e}", exc_info=True)
                raise


async def update_user_field(telegram_id: int, field_name: str, value: Any):
    """
    Оновлює одне поле користувача за його Telegram ID.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(f"UPDATE users SET {field_name} = %s WHERE telegram_id = %s;", (value, telegram_id))
                await conn.commit()
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in update_user_field: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor() as cur_retry:
                        await cur_retry.execute(f"UPDATE users SET {field_name} = %s WHERE telegram_id = %s;", (value, telegram_id))
                        await conn_retry.commit()
            except Exception as e:
                logger.error(f"Error in update_user_field: {e}", exc_info=True)
                await conn.rollback()
                raise

async def get_all_active_sources() -> List[Dict[str, Any]]:
    """
    Отримує всі активні джерела новин з бази даних.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            try:
                await cur.execute("SELECT * FROM sources WHERE status = 'active' ORDER BY id;")
                return await cur.fetchall()
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in get_all_active_sources: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor(row_factory=psycopg.rows.dict_row) as cur_retry:
                        await cur_retry.execute("SELECT * FROM sources WHERE status = 'active' ORDER BY id;")
                        return await cur_retry.fetchall()
            except Exception as e:
                logger.error(f"Error in get_all_active_sources: {e}", exc_info=True)
                raise

async def get_source_by_id(source_id: int) -> Optional[Dict[str, Any]]:
    """
    Отримує джерело за його ID.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            try:
                await cur.execute("SELECT * FROM sources WHERE id = %s;", (source_id,))
                return await cur.fetchone()
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in get_source_by_id: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor(row_factory=psycopg.rows.dict_row) as cur_retry:
                        await cur_retry.execute("SELECT * FROM sources WHERE id = %s;", (source_id,))
                        return await cur_retry.fetchone()
            except Exception as e:
                logger.error(f"Error in get_source_by_id: {e}", exc_info=True)
                raise

async def add_news_item(source_id: int, title: str, content: str, source_url: str, image_url: Optional[str], published_at: Optional[datetime], lang: str) -> int:
    """
    Додає нову новину до бази даних.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(
                    "INSERT INTO news (source_id, title, content, source_url, image_url, published_at, lang, is_sent) VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE) RETURNING id;",
                    (source_id, title, content, source_url, image_url, published_at, lang)
                )
                news_id = (await cur.fetchone())[0]
                await conn.commit()
                return news_id
            except psycopg.IntegrityError:
                await conn.rollback()
                logger.info(f"News item with URL {source_url} already exists. Skipping.")
                # You might want to return a special value or raise a specific exception here
                # For now, we'll just re-raise the IntegrityError for the caller to handle if needed
                raise
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in add_news_item: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor() as cur_retry:
                        await cur_retry.execute(
                            "INSERT INTO news (source_id, title, content, source_url, image_url, published_at, lang, is_sent) VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE) RETURNING id;",
                            (source_id, title, content, source_url, image_url, published_at, lang)
                        )
                        news_id = (await cur_retry.fetchone())[0]
                        await conn_retry.commit()
                        return news_id
            except Exception as e:
                logger.error(f"Error in add_news_item: {e}", exc_info=True)
                await conn.rollback()
                raise

async def get_news_by_source_id(source_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Отримує новини за ID джерела.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            try:
                await cur.execute("SELECT * FROM news WHERE source_id = %s ORDER BY published_at DESC LIMIT %s;", (source_id, limit))
                return await cur.fetchall()
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in get_news_by_source_id: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor(row_factory=psycopg.rows.dict_row) as cur_retry:
                        await cur_retry.execute("SELECT * FROM news WHERE source_id = %s ORDER BY published_at DESC LIMIT %s;", (source_id, limit))
                        return await cur_retry.fetchall()
            except Exception as e:
                logger.error(f"Error in get_news_by_source_id: {e}", exc_info=True)
                raise

async def get_all_news(limit: int = 100) -> List[Dict[str, Any]]:
    """
    Отримує всі новини з бази даних.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            try:
                await cur.execute("SELECT * FROM news ORDER BY published_at DESC LIMIT %s;", (limit,))
                return await cur.fetchall()
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in get_all_news: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor(row_factory=psycopg.rows.dict_row) as cur_retry:
                        await cur_retry.execute("SELECT * FROM news ORDER BY published_at DESC LIMIT %s;", (limit,))
                        return await cur_retry.fetchall()
            except Exception as e:
                logger.error(f"Error in get_all_news: {e}", exc_info=True)
                raise

async def get_user_bookmarks(user_id: int, news_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Отримує закладки користувача.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            try:
                if news_id:
                    await cur.execute("SELECT * FROM bookmarks WHERE user_id = %s AND news_id = %s;", (user_id, news_id))
                    return await cur.fetchone()
                else:
                    await cur.execute("SELECT * FROM bookmarks WHERE user_id = %s ORDER BY created_at DESC;", (user_id,))
                    return await cur.fetchall()
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in get_user_bookmarks: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor(row_factory=psycopg.rows.dict_row) as cur_retry:
                        if news_id:
                            await cur_retry.execute("SELECT * FROM bookmarks WHERE user_id = %s AND news_id = %s;", (user_id, news_id))
                            return await cur_retry.fetchone()
                        else:
                            await cur_retry.execute("SELECT * FROM bookmarks WHERE user_id = %s ORDER BY created_at DESC;", (user_id,))
                            return await cur_retry.fetchall()
            except Exception as e:
                logger.error(f"Error in get_user_bookmarks: {e}", exc_info=True)
                raise

async def add_bookmark(user_id: int, news_id: int) -> bool:
    """
    Додає закладку для користувача.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute("INSERT INTO bookmarks (user_id, news_id) VALUES (%s, %s);", (user_id, news_id))
                await conn.commit()
                return True
            except psycopg.IntegrityError:
                await conn.rollback()
                return False
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in add_bookmark: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor() as cur_retry:
                        await cur_retry.execute("INSERT INTO bookmarks (user_id, news_id) VALUES (%s, %s);", (user_id, news_id))
                        await conn_retry.commit()
                        return True
            except Exception as e:
                logger.error(f"Error in add_bookmark: {e}", exc_info=True)
                await conn.rollback()
                raise

async def delete_bookmark(user_id: int, news_id: int) -> bool:
    """
    Видаляє закладку для користувача.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute("DELETE FROM bookmarks WHERE user_id = %s AND news_id = %s;", (user_id, news_id))
                deleted_rows = cur.rowcount
                await conn.commit()
                return deleted_rows > 0
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in delete_bookmark: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor() as cur_retry:
                        await cur_retry.execute("DELETE FROM bookmarks WHERE user_id = %s AND news_id = %s;", (user_id, news_id))
                        deleted_rows = cur_retry.rowcount
                        await conn_retry.commit()
                        return deleted_rows > 0
            except Exception as e:
                logger.error(f"Error in delete_bookmark: {e}", exc_info=True)
                await conn.rollback()
                raise

async def get_user_news_views(user_id: int, news_id: int) -> Optional[Dict[str, Any]]:
    """
    Отримує перегляди новин користувача.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            try:
                await cur.execute("SELECT * FROM user_news_views WHERE user_id = %s AND news_id = %s;", (user_id, news_id))
                return await cur.fetchone()
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in get_user_news_views: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor(row_factory=psycopg.rows.dict_row) as cur_retry:
                        await cur_retry.execute("SELECT * FROM user_news_views WHERE user_id = %s AND news_id = %s;", (user_id, news_id))
                        return await cur_retry.fetchone()
            except Exception as e:
                logger.error(f"Error in get_user_news_views: {e}", exc_info=True)
                raise

async def add_user_news_view(user_id: int, news_id: int):
    """
    Додає запис про перегляд новини користувачем.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute("INSERT INTO user_news_views (user_id, news_id) VALUES (%s, %s) ON CONFLICT (user_id, news_id) DO NOTHING;", (user_id, news_id))
                await conn.commit()
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in add_user_news_view: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor() as cur_retry:
                        await cur_retry.execute("INSERT INTO user_news_views (user_id, news_id) VALUES (%s, %s) ON CONFLICT (user_id, news_id) DO NOTHING;", (user_id, news_id))
                        await conn_retry.commit()
            except Exception as e:
                logger.error(f"Error adding user news view: {e}", exc_info=True)
                await conn.rollback()

async def get_user_news_reactions(user_id: int, news_id: int) -> Optional[Dict[str, Any]]:
    """
    Отримує реакції користувача на новину.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            try:
                await cur.execute("SELECT * FROM user_news_reactions WHERE user_id = %s AND news_id = %s;", (user_id, news_id))
                return await cur.fetchone()
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in get_user_news_reactions: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor(row_factory=psycopg.rows.dict_row) as cur_retry:
                        await cur_retry.execute("SELECT * FROM user_news_reactions WHERE user_id = %s AND news_id = %s;", (user_id, news_id))
                        return await cur_retry.fetchone()
            except Exception as e:
                logger.error(f"Error in get_user_news_reactions: {e}", exc_info=True)
                raise

async def add_user_news_reaction(user_id: int, news_id: int, reaction_type: Optional[str]):
    """
    Додає або оновлює реакцію користувача на новину.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                if reaction_type:
                    await cur.execute(
                        "INSERT INTO user_news_reactions (user_id, news_id, reaction_type) VALUES (%s, %s, %s) ON CONFLICT (user_id, news_id) DO UPDATE SET reaction_type = EXCLUDED.reaction_type;",
                        (user_id, news_id, reaction_type)
                    )
                else:
                    await cur.execute("DELETE FROM user_news_reactions WHERE user_id = %s AND news_id = %s;", (user_id, news_id))
                await conn.commit()
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in add_user_news_reaction: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor() as cur_retry:
                        if reaction_type:
                            await cur_retry.execute(
                                "INSERT INTO user_news_reactions (user_id, news_id, reaction_type) VALUES (%s, %s, %s) ON CONFLICT (user_id, news_id) DO UPDATE SET reaction_type = EXCLUDED.reaction_type;",
                                (user_id, news_id, reaction_type)
                            )
                        else:
                            await cur_retry.execute("DELETE FROM user_news_reactions WHERE user_id = %s AND news_id = %s;", (user_id, news_id))
                        await conn_retry.commit()
            except Exception as e:
                logger.error(f"Error in add_user_news_reaction: {e}", exc_info=True)
                await conn.rollback()
                raise

async def update_news_item(news_id: int, updates: Dict[str, Any]):
    """
    Оновлює поля новини за її ID.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                set_clauses = [f"{k} = %s" for k in updates.keys()]
                values = list(updates.values())
                values.append(news_id)
                await cur.execute(f"UPDATE news SET {', '.join(set_clauses)} WHERE id = %s;", tuple(values))
                await conn.commit()
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in update_news_item: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor() as cur_retry:
                        set_clauses = [f"{k} = %s" for k in updates.keys()]
                        values = list(updates.values())
                        values.append(news_id)
                        await cur_retry.execute(f"UPDATE news SET {', '.join(set_clauses)} WHERE id = %s;", tuple(values))
                        await conn_retry.commit()
            except Exception as e:
                logger.error(f"Error in update_news_item: {e}", exc_info=True)
                await conn.rollback()
                raise

async def get_news_item_by_id(news_id: int) -> Optional[Dict[str, Any]]:
    """
    Отримує новину за її ID.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            try:
                await cur.execute("SELECT * FROM news WHERE id = %s;", (news_id,))
                return await cur.fetchone()
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in get_news_item_by_id: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor(row_factory=psycopg.rows.dict_row) as cur_retry:
                        await cur_retry.execute("SELECT * FROM news WHERE id = %s;", (news_id,))
                        return await cur_retry.fetchone()
            except Exception as e:
                logger.error(f"Error in get_news_item_by_id: {e}", exc_info=True)
                raise

async def delete_news_item_by_id(news_id: int) -> bool:
    """
    Видаляє новину за її ID.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute("DELETE FROM news WHERE id = %s;", (news_id,))
                deleted_rows = cur.rowcount
                await conn.commit()
                return deleted_rows > 0
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in delete_news_item_by_id: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor() as cur_retry:
                        await cur_retry.execute("DELETE FROM news WHERE id = %s;", (news_id,))
                        deleted_rows = cur_retry.rowcount
                        await conn_retry.commit()
                        return deleted_rows > 0
            except Exception as e:
                logger.error(f"Error in delete_news_item_by_id: {e}", exc_info=True)
                await conn.rollback()
                raise

async def get_one_unsent_news_item() -> Optional[Dict[str, Any]]:
    """
    Отримує одну новину, яка ще не була відправлена в канал.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            try:
                await cur.execute("SELECT * FROM news WHERE is_sent = FALSE ORDER BY published_at ASC LIMIT 1;")
                return await cur.fetchone()
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in get_one_unsent_news_item: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor(row_factory=psycopg.rows.dict_row) as cur_retry:
                        await cur_retry.execute("SELECT * FROM news WHERE is_sent = FALSE ORDER BY published_at ASC LIMIT 1;")
                        return await cur_retry.fetchone()
            except Exception as e:
                logger.error(f"Error in get_one_unsent_news_item: {e}", exc_info=True)
                raise

async def mark_news_as_sent(news_id: int):
    """
    Позначає новину як відправлену.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute("UPDATE news SET is_sent = TRUE WHERE id = %s;", (news_id,))
                await conn.commit()
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in mark_news_as_sent: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor() as cur_retry:
                        await cur_retry.execute("UPDATE news SET is_sent = TRUE WHERE id = %s;", (news_id,))
                        await conn_retry.commit()
            except Exception as e:
                logger.error(f"Error in mark_news_as_sent: {e}", exc_info=True)
                await conn.rollback()
                raise

async def get_source_by_url(url: str) -> Optional[Dict[str, Any]]:
    """
    Отримує джерело за його URL.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            try:
                await cur.execute("SELECT * FROM sources WHERE url = %s;", (url,))
                return await cur.fetchone()
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in get_source_by_url: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor(row_factory=psycopg.rows.dict_row) as cur_retry:
                        await cur_retry.execute("SELECT * FROM sources WHERE url = %s;", (url,))
                        return await cur_retry.fetchone()
            except Exception as e:
                logger.error(f"Error in get_source_by_url: {e}", exc_info=True)
                raise

async def add_source(source_data: Dict[str, Any]) -> int:
    """
    Додає нове джерело до бази даних.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(
                    "INSERT INTO sources (url, name, category, language, status, parse_interval_minutes) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id;",
                    (source_data['url'], source_data['name'], source_data['category'], source_data['language'], source_data['status'], source_data['parse_interval_minutes'])
                )
                source_id = (await cur.fetchone())[0]
                await conn.commit()
                return source_id
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in add_source: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor() as cur_retry:
                        await cur_retry.execute(
                            "INSERT INTO sources (url, name, category, language, status, parse_interval_minutes) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id;",
                            (source_data['url'], source_data['name'], source_data['category'], source_data['language'], source_data['status'], source_data['parse_interval_minutes'])
                        )
                        source_id = (await cur_retry.fetchone())[0]
                        await conn_retry.commit()
                        return source_id
            except Exception as e:
                logger.error(f"Error in add_source: {e}", exc_info=True)
                await conn.rollback()
                raise

async def update_source_status(source_id: int, updates: Dict[str, Any]):
    """
    Оновлює статус джерела.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                set_clauses = [f"{k} = %s" for k in updates.keys()]
                values = list(updates.values())
                values.append(source_id)
                await cur.execute(f"UPDATE sources SET {', '.join(set_clauses)} WHERE id = %s;", tuple(values))
                await conn.commit()
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in update_source_status: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor() as cur_retry:
                        set_clauses = [f"{k} = %s" for k in updates.keys()]
                        values = list(updates.values())
                        values.append(source_id)
                        await cur_retry.execute(f"UPDATE sources SET {', '.join(set_clauses)} WHERE id = %s;", tuple(values))
                        await conn_retry.commit()
            except Exception as e:
                logger.error(f"Error in update_source_status: {e}", exc_info=True)
                await conn.rollback()
                raise

async def get_all_sources() -> List[Dict[str, Any]]:
    """
    Отримує всі джерела новин.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            try:
                await cur.execute("SELECT * FROM sources ORDER BY id;")
                return await cur.fetchall()
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in get_all_sources: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor(row_factory=psycopg.rows.dict_row) as cur_retry:
                        await cur_retry.execute("SELECT * FROM sources ORDER BY id;")
                        return await cur_retry.fetchall()
            except Exception as e:
                logger.error(f"Error in get_all_sources: {e}", exc_info=True)
                raise

async def get_bot_setting(setting_key: str) -> Optional[str]:
    """
    Отримує значення налаштування бота.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            try:
                await cur.execute("SELECT setting_value FROM bot_settings WHERE setting_key = %s;", (setting_key,))
                result = await cur.fetchone()
                return result['setting_value'] if result else None
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in get_bot_setting: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor(row_factory=psycopg.rows.dict_row) as cur_retry:
                        await cur_retry.execute("SELECT setting_value FROM bot_settings WHERE setting_key = %s;", (setting_key,))
                        result = await cur_retry.fetchone()
                        return result['setting_value'] if result else None
            except Exception as e:
                logger.error(f"Error in get_bot_setting: {e}", exc_info=True)
                raise

async def update_bot_setting(setting_key: str, setting_value: str):
    """
    Оновлює значення налаштування бота.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(
                    "INSERT INTO bot_settings (setting_key, setting_value) VALUES (%s, %s) ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value;",
                    (setting_key, setting_value)
                )
                await conn.commit()
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in update_bot_setting: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor() as cur_retry:
                        await cur_retry.execute(
                            "INSERT INTO bot_settings (setting_key, setting_value) VALUES (%s, %s) ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value;",
                            (setting_key, setting_value)
                        )
                        await conn_retry.commit()
            except Exception as e:
                logger.error(f"Error in update_bot_setting: {e}", exc_info=True)
                await conn.rollback()
                raise

async def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    """
    Отримує користувача за його внутрішнім ID.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            try:
                await cur.execute("SELECT * FROM users WHERE id = %s;", (user_id,))
                return await cur.fetchone()
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in get_user_by_id: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor(row_factory=psycopg.rows.dict_row) as cur_retry:
                        await cur_retry.execute("SELECT * FROM users WHERE id = %s;", (user_id,))
                        return await cur_retry.fetchone()
            except Exception as e:
                logger.error(f"Error in get_user_by_id: {e}", exc_info=True)
                raise

async def get_last_n_news(source_ids: Optional[List[int]] = None, limit: int = 5) -> List[Dict[str, Any]]:
    """
    Отримує останні N новин (можна фільтрувати за джерелами).
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            try:
                if source_ids:
                    await cur.execute("SELECT * FROM news WHERE source_id = ANY(%s) ORDER BY published_at DESC LIMIT %s;", (source_ids, limit))
                else:
                    await cur.execute("SELECT * FROM news ORDER BY published_at DESC LIMIT %s;", (limit,))
                return await cur.fetchall()
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in get_last_n_news: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor(row_factory=psycopg.rows.dict_row) as cur_retry:
                        if source_ids:
                            await cur_retry.execute("SELECT * FROM news WHERE source_id = ANY(%s) ORDER BY published_at DESC LIMIT %s;", (source_ids, limit))
                        else:
                            await cur_retry.execute("SELECT * FROM news ORDER BY published_at DESC LIMIT %s;", (limit,))
                        return await cur_retry.fetchall()
            except Exception as e:
                logger.error(f"Error in get_last_n_news: {e}", exc_info=True)
                raise

async def update_source_last_parsed(source_id: int, parsed_at: datetime):
    """
    Оновлює час останнього парсингу для джерела.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute("UPDATE sources SET last_parsed_at = %s WHERE id = %s;", (parsed_at, source_id))
                await conn.commit()
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in update_source_last_parsed: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor() as cur_retry:
                        await cur_retry.execute("UPDATE sources SET last_parsed_at = %s WHERE id = %s;", (parsed_at, source_id))
                        await conn_retry.commit()
            except Exception as e:
                logger.error(f"Error in update_source_last_parsed: {e}", exc_info=True)
                await conn.rollback()
                raise

async def get_news_for_digest(user_id: int, time_frame: str = 'daily') -> List[Dict[str, Any]]:
    """
    Отримує новини для дайджесту користувача за певний проміжок часу.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            try:
                if time_frame == 'daily':
                    time_ago = datetime.now(timezone.utc) - timedelta(days=1)
                elif time_frame == 'weekly':
                    time_ago = datetime.now(timezone.utc) - timedelta(weeks=1)
                else:
                    return []

                await cur.execute(
                    """
                    SELECT n.* FROM news n
                    JOIN user_subscriptions us ON n.source_id = us.source_id
                    WHERE us.user_id = %s AND n.published_at >= %s
                    ORDER BY n.published_at DESC;
                    """,
                    (user_id, time_ago)
                )
                return await cur.fetchall()
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in get_news_for_digest: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor(row_factory=psycopg.rows.dict_row) as cur_retry:
                        if time_frame == 'daily':
                            time_ago = datetime.now(timezone.utc) - timedelta(days=1)
                        elif time_frame == 'weekly':
                            time_ago = datetime.now(timezone.utc) - timedelta(weeks=1)
                        else:
                            return []
                        await cur_retry.execute(
                            """
                            SELECT n.* FROM news n
                            JOIN user_subscriptions us ON n.source_id = us.source_id
                            WHERE us.user_id = %s AND n.published_at >= %s
                            ORDER BY n.published_at DESC;
                            """,
                            (user_id, time_ago)
                        )
                        return await cur_retry.fetchall()
            except Exception as e:
                logger.error(f"Error in get_news_for_digest: {e}", exc_info=True)
                raise

async def get_tasks_by_status(status: str) -> List[Dict[str, Any]]:
    """
    Отримує завдання з черги за статусом.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            try:
                await cur.execute("SELECT * FROM task_queue WHERE status = %s ORDER BY scheduled_at ASC;", (status,))
                return await cur.fetchall()
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in get_tasks_by_status: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor(row_factory=psycopg.rows.dict_row) as cur_retry:
                        await cur_retry.execute("SELECT * FROM task_queue WHERE status = %s ORDER BY scheduled_at ASC;", (status,))
                        return await cur_retry.fetchall()
            except Exception as e:
                logger.error(f"Error in get_tasks_by_status: {e}", exc_info=True)
                raise

async def update_task_status(task_id: int, status: str, error_message: Optional[str] = None):
    """
    Оновлює статус завдання в черзі.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                if error_message:
                    await cur.execute("UPDATE task_queue SET status = %s, processed_at = CURRENT_TIMESTAMP, error_message = %s WHERE id = %s;", (status, error_message, task_id))
                else:
                    await cur.execute("UPDATE task_queue SET status = %s, processed_at = CURRENT_TIMESTAMP WHERE id = %s;", (status, task_id))
                await conn.commit()
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in update_task_status: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor() as cur_retry:
                        if error_message:
                            await cur_retry.execute("UPDATE task_queue SET status = %s, processed_at = CURRENT_TIMESTAMP, error_message = %s WHERE id = %s;", (status, error_message, task_id))
                        else:
                            await cur_retry.execute("UPDATE task_queue SET status = %s, processed_at = CURRENT_TIMESTAMP WHERE id = %s;", (status, task_id))
                        await conn_retry.commit()
            except Exception as e:
                logger.error(f"Error in update_task_status: {e}", exc_info=True)
                await conn.rollback()
                raise

async def add_task_to_queue(task_type: str, task_data: Dict[str, Any], scheduled_at: datetime) -> int:
    """
    Додає нове завдання до черги.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(
                    "INSERT INTO task_queue (task_type, task_data, scheduled_at) VALUES (%s, %s, %s) RETURNING id;",
                    (task_type, json.dumps(task_data), scheduled_at)
                )
                task_id = (await cur.fetchone())[0]
                await conn.commit()
                return task_id
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in add_task_to_queue: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor() as cur_retry:
                        await cur_retry.execute(
                            "INSERT INTO task_queue (task_type, task_data, scheduled_at) VALUES (%s, %s, %s) RETURNING id;",
                            (task_type, json.dumps(task_data), scheduled_at)
                        )
                        task_id = (await cur_retry.fetchone())[0]
                        await conn_retry.commit()
                        return task_id
            except Exception as e:
                logger.error(f"Error in add_task_to_queue: {e}", exc_info=True)
                await conn.rollback()
                raise

async def get_all_users() -> List[Dict[str, Any]]:
    """
    Отримує всіх користувачів.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            try:
                await cur.execute("SELECT * FROM users ORDER BY id;")
                return await cur.fetchall()
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in get_all_users: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor(row_factory=psycopg.rows.dict_row) as cur_retry:
                        await cur_retry.execute("SELECT * FROM users ORDER BY id;")
                        return await cur_retry.fetchall()
            except Exception as e:
                logger.error(f"Error in get_all_users: {e}", exc_info=True)
                raise

async def get_user_subscriptions(user_id: int, source_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Отримує підписки користувача.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            try:
                if source_id:
                    await cur.execute("SELECT * FROM user_subscriptions WHERE user_id = %s AND source_id = %s;", (user_id, source_id))
                    return await cur.fetchone()
                else:
                    await cur.execute("SELECT * FROM user_subscriptions WHERE user_id = %s;", (user_id,))
                    return await cur.fetchall()
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in get_user_subscriptions: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor(row_factory=psycopg.rows.dict_row) as cur_retry:
                        if source_id:
                            await cur_retry.execute("SELECT * FROM user_subscriptions WHERE user_id = %s AND source_id = %s;", (user_id, source_id))
                            return await cur_retry.fetchone()
                        else:
                            await cur_retry.execute("SELECT * FROM user_subscriptions WHERE user_id = %s;", (user_id,))
                            return await cur_retry.fetchall()
            except Exception as e:
                logger.error(f"Error in get_user_subscriptions: {e}", exc_info=True)
                raise

async def add_user_subscription(user_id: int, source_id: int) -> bool:
    """
    Додає підписку користувача на джерело.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute("INSERT INTO user_subscriptions (user_id, source_id) VALUES (%s, %s);", (user_id, source_id))
                await conn.commit()
                return True
            except psycopg.IntegrityError:
                await conn.rollback()
                return False
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in add_user_subscription: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor() as cur_retry:
                        await cur_retry.execute("INSERT INTO user_subscriptions (user_id, source_id) VALUES (%s, %s);", (user_id, source_id))
                        await conn_retry.commit()
                        return True
            except Exception as e:
                logger.error(f"Error in add_user_subscription: {e}", exc_info=True)
                await conn.rollback()
                raise

async def delete_user_subscription(user_id: int, source_id: int) -> bool:
    """
    Видаляє підписку користувача на джерело.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute("DELETE FROM user_subscriptions WHERE user_id = %s AND source_id = %s;", (user_id, source_id))
                deleted_rows = cur.rowcount
                await conn.commit()
                return deleted_rows > 0
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in delete_user_subscription: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor() as cur_retry:
                        await cur_retry.execute("DELETE FROM user_subscriptions WHERE user_id = %s AND source_id = %s;", (user_id, source_id))
                        deleted_rows = cur_retry.rowcount
                        await conn_retry.commit()
                        return deleted_rows > 0
            except Exception as e:
                logger.error(f"Error in delete_user_subscription: {e}", exc_info=True)
                await conn.rollback()
                raise

async def get_all_subscribed_sources(user_id: int) -> List[Dict[str, Any]]:
    """
    Отримує всі джерела, на які підписаний користувач.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            try:
                await cur.execute(
                    """
                    SELECT s.* FROM sources s
                    JOIN user_subscriptions us ON s.id = us.source_id
                    WHERE us.user_id = %s AND s.status = 'active'
                    ORDER BY s.name;
                    """,
                    (user_id,)
                )
                return await cur.fetchall()
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in get_all_subscribed_sources: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor(row_factory=psycopg.rows.dict_row) as cur_retry:
                        await cur_retry.execute(
                            """
                            SELECT s.* FROM sources s
                            JOIN user_subscriptions us ON s.id = us.source_id
                            WHERE us.user_id = %s AND s.status = 'active'
                            ORDER BY s.name;
                            """,
                            (user_id,)
                        )
                        return await cur_retry.fetchall()
            except Exception as e:
                logger.error(f"Error in get_all_subscribed_sources: {e}", exc_info=True)
                raise

async def get_source_stats(source_id: int) -> Optional[Dict[str, Any]]:
    """
    Отримує статистику для джерела.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            try:
                await cur.execute("SELECT * FROM source_stats WHERE source_id = %s;", (source_id,))
                return await cur.fetchone()
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in get_source_stats: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor(row_factory=psycopg.rows.dict_row) as cur_retry:
                        await cur_retry.execute("SELECT * FROM source_stats WHERE source_id = %s;", (source_id,))
                        return await cur_retry.fetchone()
            except Exception as e:
                logger.error(f"Error in get_source_stats: {e}", exc_info=True)
                raise

async def update_source_stats(source_id: int, news_count: int):
    """
    Оновлює статистику для джерела.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(
                    "INSERT INTO source_stats (source_id, news_count) VALUES (%s, %s) ON CONFLICT (source_id) DO UPDATE SET news_count = EXCLUDED.news_count;",
                    (source_id, news_count)
                )
                await conn.commit()
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in update_source_stats: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor() as cur_retry:
                        await cur_retry.execute(
                            "INSERT INTO source_stats (source_id, news_count) VALUES (%s, %s) ON CONFLICT (source_id) DO UPDATE SET news_count = EXCLUDED.news_count;",
                            (source_id, news_count)
                        )
                        await conn_retry.commit()
            except Exception as e:
                logger.error(f"Error in update_source_stats: {e}", exc_info=True)
                await conn.rollback()
                raise

async def delete_user(telegram_id: int) -> bool:
    """
    Видаляє користувача та всі пов'язані з ним дані.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                user = await get_user_by_telegram_id(telegram_id)
                if user:
                    user_id = user['id']
                    await cur.execute("DELETE FROM user_news_views WHERE user_id = %s;", (user_id,))
                    await cur.execute("DELETE FROM user_news_reactions WHERE user_id = %s;", (user_id,))
                    await cur.execute("DELETE FROM bookmarks WHERE user_id = %s;", (user_id,))
                    await cur.execute("DELETE FROM user_subscriptions WHERE user_id = %s;", (user_id,))
                    await cur.execute("DELETE FROM users WHERE telegram_id = %s;", (telegram_id,))
                    deleted_rows = cur.rowcount
                    await conn.commit()
                    return deleted_rows > 0
                return False
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in delete_user: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor() as cur_retry:
                        user = await get_user_by_telegram_id(telegram_id)
                        if user:
                            user_id = user['id']
                            await cur_retry.execute("DELETE FROM user_news_views WHERE user_id = %s;", (user_id,))
                            await cur_retry.execute("DELETE FROM user_news_reactions WHERE user_id = %s;", (user_id,))
                            await cur_retry.execute("DELETE FROM bookmarks WHERE user_id = %s;", (user_id,))
                            await cur_retry.execute("DELETE FROM user_subscriptions WHERE user_id = %s;", (user_id,))
                            await cur_retry.execute("DELETE FROM users WHERE telegram_id = %s;", (telegram_id,))
                            deleted_rows = cur_retry.rowcount
                            await conn_retry.commit()
                            return deleted_rows > 0
                        return False
            except Exception as e:
                logger.error(f"Error in delete_user: {e}", exc_info=True)
                await conn.rollback()
                raise

async def delete_source(source_id: int) -> bool:
    """
    Видаляє джерело та всі пов'язані з ним новини, підписки та статистику.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute("DELETE FROM news WHERE source_id = %s;", (source_id,))
                await cur.execute("DELETE FROM user_subscriptions WHERE source_id = %s;", (source_id,))
                await cur.execute("DELETE FROM source_stats WHERE source_id = %s;", (source_id,))
                await cur.execute("DELETE FROM sources WHERE id = %s;", (source_id,))
                deleted_rows = cur.rowcount
                await conn.commit()
                return deleted_rows > 0
            except psycopg.OperationalError as e:
                logger.error(f"OperationalError in delete_source: {e}. Retrying connection.", exc_info=True)
                pool = await get_db_pool()
                async with pool.connection() as conn_retry:
                    async with conn_retry.cursor() as cur_retry:
                        await cur_retry.execute("DELETE FROM news WHERE source_id = %s;", (source_id,))
                        await cur_retry.execute("DELETE FROM user_subscriptions WHERE source_id = %s;", (source_id,))
                        await cur_retry.execute("DELETE FROM source_stats WHERE source_id = %s;", (source_id,))
                        await cur_retry.execute("DELETE FROM sources WHERE id = %s;", (source_id,))
                        deleted_rows = cur_retry.rowcount
                        await conn_retry.commit()
                        return deleted_rows > 0
            except Exception as e:
                logger.error(f"Error in delete_source: {e}", exc_info=True)
                await conn.rollback()
                raise

if __name__ == "__main__":
    import asyncio
    async def test_db_connection():
        try:
            pool = await get_db_pool()
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT version();")
                    version = await cur.fetchone()
                    print(f"PostgreSQL version: {version[0]}")
            print("Database connection successful.")
        except Exception as e:
            print(f"Database connection failed: {e}")
        finally:
            if db_pool:
                await db_pool.close()
    asyncio.run(test_db_connection())

