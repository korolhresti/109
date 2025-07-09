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
            db_pool = AsyncConnectionPool(conninfo=conninfo, open=False)
            await db_pool.open()
            logger.info("DB pool initialized successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize DB pool: {e}", exc_info=True)
            raise
    return db_pool

async def execute_query(query: str, params: tuple = (), fetchone: bool = False, fetchall: bool = False, commit: bool = False) -> Optional[Union[Dict[str, Any], List[Dict[str, Any]]]]:
    """
    Універсальна функція для виконання запитів до бази даних.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            try:
                await cur.execute(query, params)
                if commit:
                    await conn.commit()
                if fetchone:
                    return await cur.fetchone()
                if fetchall:
                    return await cur.fetchall()
                return None
            except Exception as e:
                logger.error(f"Database query failed: {e}", exc_info=True)
                if commit:
                    await conn.rollback()
                raise

async def get_user_by_telegram_id(telegram_id: int) -> Optional[Dict[str, Any]]:
    """
    Отримує користувача за його Telegram ID.
    """
    query = "SELECT * FROM users WHERE telegram_id = %s;"
    return await execute_query(query, (telegram_id,), fetchone=True)

async def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    """
    Отримує користувача за його внутрішнім ID.
    """
    query = "SELECT * FROM users WHERE id = %s;"
    return await execute_query(query, (user_id,), fetchone=True)

async def update_user_field(telegram_id: int, field_name: str, value: Any) -> bool:
    """
    Оновлює одне поле для користувача за його Telegram ID.
    """
    query = f"UPDATE users SET {field_name} = %s WHERE telegram_id = %s;"
    try:
        await execute_query(query, (value, telegram_id), commit=True)
        return True
    except Exception as e:
        logger.error(f"Failed to update user field {field_name} for {telegram_id}: {e}")
        return False

async def get_all_users() -> List[Dict[str, Any]]:
    """
    Отримує список всіх користувачів.
    """
    query = "SELECT * FROM users;"
    return await execute_query(query, fetchall=True) or []

async def delete_user(telegram_id: int) -> bool:
    """
    Видаляє користувача за Telegram ID та всі пов'язані з ним дані.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        try:
            async with conn.cursor() as cur:
                # Видаляємо записи з user_news_views
                await cur.execute("DELETE FROM user_news_views WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s);", (telegram_id,))
                # Видаляємо записи з user_news_reactions
                await cur.execute("DELETE FROM user_news_reactions WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s);", (telegram_id,))
                # Видаляємо записи з bookmarks
                await cur.execute("DELETE FROM bookmarks WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s);", (telegram_id,))
                # Видаляємо записи з reports
                await cur.execute("DELETE FROM reports WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s);", (telegram_id,))
                # Видаляємо записи з invitations (якщо користувач був запрошувачем)
                await cur.execute("DELETE FROM invitations WHERE inviter_id = (SELECT id FROM users WHERE telegram_id = %s);", (telegram_id,))
                # Видаляємо записи з user_subscriptions
                await cur.execute("DELETE FROM user_subscriptions WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s);", (telegram_id,))
                # Нарешті, видаляємо самого користувача
                await cur.execute("DELETE FROM users WHERE telegram_id = %s;", (telegram_id,))
                deleted_rows = cur.rowcount
                await conn.commit()
                return deleted_rows > 0
        except Exception as e:
            logger.error(f"Error in delete_user: {e}", exc_info=True)
            await conn.rollback()
            raise

async def add_source(source_data: Dict[str, Any]) -> int:
    """
    Додає нове джерело новин до бази даних.
    Експліцитно включаємо всі стовпці, щоб уникнути помилок NOT NULL.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                # Перевіряємо, чи існує джерело з таким URL
                await cur.execute("SELECT id FROM sources WHERE url = %s;", (source_data['url'],))
                existing_source = await cur.fetchone()
                if existing_source:
                    raise psycopg.IntegrityError("Source with this URL already exists.", code='23505')

                await cur.execute(
                    """
                    INSERT INTO sources (
                        url, name, category, language, description, status,
                        last_parsed_at, last_error, original_url, source_name,
                        source_category, source_language, feed_url, parse_interval_minutes
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;
                    """,
                    (
                        source_data['url'],
                        source_data.get('name'),
                        source_data.get('category'),
                        source_data.get('language'),
                        source_data.get('description'),
                        source_data.get('status', 'active'),
                        source_data.get('last_parsed_at'), # Can be None
                        source_data.get('last_error'),     # Can be None
                        source_data.get('original_url'),   # Can be None
                        source_data.get('source_name'),    # Can be None
                        source_data.get('source_category'),# Can be None
                        source_data.get('source_language'),# Can be None
                        source_data.get('feed_url'),       # Can be None
                        source_data.get('parse_interval_minutes', 60)
                    )
                )
                source_id = (await cur.fetchone())[0]
                await conn.commit()
                return source_id
            except Exception as e:
                logger.error(f"Error in add_source: {e}", exc_info=True)
                await conn.rollback()
                raise

async def get_source_by_url(url: str) -> Optional[Dict[str, Any]]:
    """
    Отримує джерело за його URL.
    """
    query = "SELECT * FROM sources WHERE url = %s;"
    return await execute_query(query, (url,), fetchone=True)

async def get_source_by_id(source_id: int) -> Optional[Dict[str, Any]]:
    """
    Отримує джерело за його ID.
    """
    query = "SELECT * FROM sources WHERE id = %s;"
    return await execute_query(query, (source_id,), fetchone=True)

async def get_all_sources() -> List[Dict[str, Any]]:
    """
    Отримує список всіх джерел.
    """
    query = "SELECT * FROM sources ORDER BY name;"
    return await execute_query(query, fetchall=True) or []

async def get_all_active_sources() -> List[Dict[str, Any]]:
    """
    Отримує список всіх активних джерел.
    """
    query = "SELECT * FROM sources WHERE status = 'active' ORDER BY name;"
    return await execute_query(query, fetchall=True) or []

async def update_source_status(source_id: int, updates: Dict[str, Any]) -> bool:
    """
    Оновлює статус або інші поля джерела за його ID.
    `updates` - словник з полями для оновлення.
    """
    if not updates:
        return False
    
    set_clauses = []
    params = []
    for key, value in updates.items():
        set_clauses.append(f"{key} = %s")
        params.append(value)
    
    params.append(source_id)
    query = f"UPDATE sources SET {', '.join(set_clauses)} WHERE id = %s;"
    
    try:
        await execute_query(query, tuple(params), commit=True)
        return True
    except Exception as e:
        logger.error(f"Failed to update source {source_id}: {e}")
        return False

async def update_source_last_parsed(source_id: int, last_parsed_at: datetime) -> bool:
    """
    Оновлює час останнього парсингу для джерела.
    """
    query = "UPDATE sources SET last_parsed_at = %s WHERE id = %s;"
    try:
        await execute_query(query, (last_parsed_at, source_id), commit=True)
        return True
    except Exception as e:
        logger.error(f"Failed to update last_parsed_at for source {source_id}: {e}")
        return False

async def delete_source(source_id: int) -> bool:
    """
    Видаляє джерело за ID та всі пов'язані з ним дані (новини, підписки, статистику).
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        try:
            async with conn.cursor() as cur:
                # Видаляємо пов'язані новини (CASCADE на news)
                # Видаляємо пов'язані підписки (CASCADE на user_subscriptions)
                # Видаляємо пов'язану статистику (CASCADE на source_stats)
                await cur.execute("DELETE FROM news WHERE source_id = %s;", (source_id,))
                await cur.execute("DELETE FROM user_subscriptions WHERE source_id = %s;", (source_id,))
                await cur.execute("DELETE FROM source_stats WHERE source_id = %s;", (source_id,))
                await cur.execute("DELETE FROM sources WHERE id = %s;", (source_id,))
                deleted_rows = cur.rowcount
                await conn.commit()
                return deleted_rows > 0
        except Exception as e:
            logger.error(f"Error in delete_source: {e}", exc_info=True)
            await conn.rollback()
            raise

async def add_news_item(source_id: int, title: str, content: str, source_url: str, image_url: Optional[str] = None, published_at: Optional[datetime] = None, lang: str = 'uk') -> int:
    """
    Додає нову новину до бази даних.
    """
    query = """
    INSERT INTO news (source_id, title, content, source_url, image_url, published_at, lang)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (source_url) DO NOTHING
    RETURNING id;
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(query, (source_id, title, content, source_url, image_url, published_at, lang))
                news_id = await cur.fetchone()
                await conn.commit()
                if news_id:
                    return news_id[0]
                return 0 # Повертаємо 0, якщо новина вже існувала (DO NOTHING)
            except Exception as e:
                logger.error(f"Error adding news item: {e}", exc_info=True)
                await conn.rollback()
                raise

async def get_news_item_by_id(news_id: int) -> Optional[Dict[str, Any]]:
    """
    Отримує новину за її ID.
    """
    query = "SELECT * FROM news WHERE id = %s;"
    return await execute_query(query, (news_id,), fetchone=True)

async def get_news_by_source_id(source_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Отримує останні новини для певного джерела.
    """
    query = "SELECT * FROM news WHERE source_id = %s ORDER BY published_at DESC NULLS LAST, created_at DESC LIMIT %s;"
    return await execute_query(query, (source_id, limit), fetchall=True) or []

async def get_all_news(limit: int = 100) -> List[Dict[str, Any]]:
    """
    Отримує список всіх новин.
    """
    query = "SELECT * FROM news ORDER BY published_at DESC NULLS LAST, created_at DESC LIMIT %s;"
    return await execute_query(query, (limit,), fetchall=True) or []

async def get_last_n_news(n: int) -> List[Dict[str, Any]]:
    """
    Отримує останні N новин з бази даних.
    """
    query = "SELECT * FROM news ORDER BY published_at DESC NULLS LAST, created_at DESC LIMIT %s;"
    return await execute_query(query, (n,), fetchall=True) or []

async def update_news_item(news_id: int, updates: Dict[str, Any]) -> bool:
    """
    Оновлює одне або кілька полів для новини за її ID.
    """
    if not updates:
        return False
    
    set_clauses = []
    params = []
    for key, value in updates.items():
        set_clauses.append(f"{key} = %s")
        params.append(value)
    
    params.append(news_id)
    query = f"UPDATE news SET {', '.join(set_clauses)} WHERE id = %s;"
    
    try:
        await execute_query(query, tuple(params), commit=True)
        return True
    except Exception as e:
        logger.error(f"Failed to update news item {news_id}: {e}")
        return False

async def delete_news_item_by_id(news_id: int) -> bool:
    """
    Видаляє новину за її ID.
    """
    query = "DELETE FROM news WHERE id = %s;"
    try:
        await execute_query(query, (news_id,), commit=True)
        return True
    except Exception as e:
        logger.error(f"Failed to delete news item {news_id}: {e}")
        return False

async def get_one_unsent_news_item() -> Optional[Dict[str, Any]]:
    """
    Отримує одну новину, яка ще не була відправлена в канал.
    """
    query = "SELECT * FROM news WHERE is_sent = FALSE ORDER BY published_at ASC NULLS LAST, created_at ASC LIMIT 1;"
    return await execute_query(query, fetchone=True)

async def mark_news_as_sent(news_id: int) -> bool:
    """
    Позначає новину як відправлену.
    """
    query = "UPDATE news SET is_sent = TRUE WHERE id = %s;"
    try:
        await execute_query(query, (news_id,), commit=True)
        return True
    except Exception as e:
        logger.error(f"Failed to mark news {news_id} as sent: {e}")
        return False

async def get_user_bookmarks(user_id: int) -> List[Dict[str, Any]]:
    """
    Отримує закладки користувача.
    """
    query = """
    SELECT n.*, s.name as source_name
    FROM bookmarks b
    JOIN news n ON b.news_id = n.id
    JOIN sources s ON n.source_id = s.id
    WHERE b.user_id = %s
    ORDER BY b.bookmarked_at DESC;
    """
    return await execute_query(query, (user_id,), fetchall=True) or []

async def add_bookmark(user_id: int, news_id: int) -> bool:
    """
    Додає новину в закладки користувача.
    """
    query = "INSERT INTO bookmarks (user_id, news_id) VALUES (%s, %s) ON CONFLICT (user_id, news_id) DO NOTHING;"
    try:
        await execute_query(query, (user_id, news_id), commit=True)
        return True
    except Exception as e:
        logger.error(f"Failed to add bookmark for user {user_id}, news {news_id}: {e}")
        return False

async def delete_bookmark(user_id: int, news_id: int) -> bool:
    """
    Видаляє новину із закладок користувача.
    """
    query = "DELETE FROM bookmarks WHERE user_id = %s AND news_id = %s;"
    try:
        await execute_query(query, (user_id, news_id), commit=True)
        return True
    except Exception as e:
        logger.error(f"Failed to delete bookmark for user {user_id}, news {news_id}: {e}")
        return False

async def get_user_news_views(user_id: int, news_id: int) -> Optional[Dict[str, Any]]:
    """
    Отримує запис про перегляд новини користувачем.
    """
    query = "SELECT * FROM user_news_views WHERE user_id = %s AND news_id = %s;"
    return await execute_query(query, (user_id, news_id), fetchone=True)

async def add_user_news_view(user_id: int, news_id: int) -> bool:
    """
    Записує перегляд новини користувачем.
    """
    query = "INSERT INTO user_news_views (user_id, news_id) VALUES (%s, %s) ON CONFLICT (user_id, news_id) DO UPDATE SET viewed_at = CURRENT_TIMESTAMP;"
    try:
        await execute_query(query, (user_id, news_id), commit=True)
        return True
    except Exception as e:
        logger.error(f"Failed to add news view for user {user_id}, news {news_id}: {e}")
        return False

async def get_user_news_reactions(user_id: int, news_id: int) -> Optional[Dict[str, Any]]:
    """
    Отримує реакцію користувача на новину.
    """
    query = "SELECT * FROM user_news_reactions WHERE user_id = %s AND news_id = %s;"
    return await execute_query(query, (user_id, news_id), fetchone=True)

async def add_user_news_reaction(user_id: int, news_id: int, reaction: str) -> bool:
    """
    Додає або оновлює реакцію користувача на новину.
    """
    query = """
    INSERT INTO user_news_reactions (user_id, news_id, reaction)
    VALUES (%s, %s, %s)
    ON CONFLICT (user_id, news_id) DO UPDATE SET reaction = EXCLUDED.reaction, reacted_at = CURRENT_TIMESTAMP;
    """
    try:
        await execute_query(query, (user_id, news_id, reaction), commit=True)
        return True
    except Exception as e:
        logger.error(f"Failed to add/update reaction for user {user_id}, news {news_id}: {e}")
        return False

async def get_user_subscriptions(user_id: int) -> List[Dict[str, Any]]:
    """
    Отримує підписки користувача.
    """
    query = """
    SELECT s.* FROM user_subscriptions us
    JOIN sources s ON us.source_id = s.id
    WHERE us.user_id = %s;
    """
    return await execute_query(query, (user_id,), fetchall=True) or []

async def add_user_subscription(user_id: int, source_id: int) -> bool:
    """
    Додає підписку користувача на джерело.
    """
    query = "INSERT INTO user_subscriptions (user_id, source_id) VALUES (%s, %s) ON CONFLICT (user_id, source_id) DO NOTHING;"
    try:
        await execute_query(query, (user_id, source_id), commit=True)
        return True
    except Exception as e:
        logger.error(f"Failed to add subscription for user {user_id}, source {source_id}: {e}")
        return False

async def delete_user_subscription(user_id: int, source_id: int) -> bool:
    """
    Видаляє підписку користувача на джерело.
    """
    query = "DELETE FROM user_subscriptions WHERE user_id = %s AND source_id = %s;"
    try:
        await execute_query(query, (user_id, source_id), commit=True)
        return True
    except Exception as e:
        logger.error(f"Failed to delete subscription for user {user_id}, source {source_id}: {e}")
        return False

async def get_all_subscribed_sources(user_id: int) -> List[Dict[str, Any]]:
    """
    Отримує всі джерела, на які підписаний користувач.
    """
    query = """
    SELECT s.* FROM sources s
    JOIN user_subscriptions us ON s.id = us.source_id
    WHERE us.user_id = %s;
    """
    return await execute_query(query, (user_id,), fetchall=True) or []

async def get_source_stats(source_id: int) -> Optional[Dict[str, Any]]:
    """
    Отримує статистику для джерела.
    """
    query = "SELECT * FROM source_stats WHERE source_id = %s;"
    return await execute_query(query, (source_id,), fetchone=True)

async def update_source_stats(source_id: int, total_news_parsed: int = 0, last_24h_news: int = 0, last_7d_news: int = 0) -> bool:
    """
    Оновлює статистику для джерела.
    """
    query = """
    INSERT INTO source_stats (source_id, total_news_parsed, last_24h_news, last_7d_news, last_update)
    VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
    ON CONFLICT (source_id) DO UPDATE SET
        total_news_parsed = source_stats.total_news_parsed + EXCLUDED.total_news_parsed,
        last_24h_news = EXCLUDED.last_24h_news,
        last_7d_news = EXCLUDED.last_7d_news,
        last_update = CURRENT_TIMESTAMP;
    """
    try:
        await execute_query(query, (source_id, total_news_parsed, last_24h_news, last_7d_news), commit=True)
        return True
    except Exception as e:
        logger.error(f"Failed to update source stats for source {source_id}: {e}")
        return False

async def get_bot_setting(setting_key: str) -> Optional[str]:
    """
    Отримує значення налаштування бота за ключем.
    """
    query = "SELECT setting_value FROM bot_settings WHERE setting_key = %s;"
    result = await execute_query(query, (setting_key,), fetchone=True)
    return result['setting_value'] if result else None

async def update_bot_setting(setting_key: str, setting_value: str) -> bool:
    """
    Оновлює значення налаштування бота за ключем.
    """
    query = """
    INSERT INTO bot_settings (setting_key, setting_value)
    VALUES (%s, %s)
    ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value;
    """
    try:
        await execute_query(query, (setting_key, setting_value), commit=True)
        return True
    except Exception as e:
        logger.error(f"Failed to update bot setting {setting_key}: {e}")
        return False

async def get_news_for_digest(user_id: int, time_period: str = 'daily') -> List[Dict[str, Any]]:
    """
    Отримує новини для дайджесту користувача за вказаний період.
    """
    # TODO: Implement logic to fetch news based on user subscriptions and time_period
    # For now, returning a sample or last few news items
    return await get_last_n_news(5)

async def get_tasks_by_status(status: str = 'pending') -> List[Dict[str, Any]]:
    """
    Отримує завдання з черги за статусом.
    """
    query = "SELECT * FROM task_queue WHERE status = %s ORDER BY scheduled_at ASC;"
    return await execute_query(query, (status,), fetchall=True) or []

async def update_task_status(task_id: int, status: str, error_message: Optional[str] = None) -> bool:
    """
    Оновлює статус завдання в черзі.
    """
    query = "UPDATE task_queue SET status = %s, processed_at = CURRENT_TIMESTAMP, error_message = %s WHERE id = %s;"
    try:
        await execute_query(query, (status, error_message, task_id), commit=True)
        return True
    except Exception as e:
        logger.error(f"Failed to update task status for task {task_id}: {e}")
        return False

async def add_task_to_queue(task_type: str, task_data: Dict[str, Any], scheduled_at: datetime) -> int:
    """
    Додає нове завдання до черги.
    """
    query = "INSERT INTO task_queue (task_type, task_data, scheduled_at) VALUES (%s, %s, %s) RETURNING id;"
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(query, (task_type, json.dumps(task_data), scheduled_at))
                task_id = (await cur.fetchone())[0]
                await conn.commit()
                return task_id
            except Exception as e:
                logger.error(f"Error adding task to queue: {e}", exc_info=True)
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

