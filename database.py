import os
import logging
import psycopg
from psycopg_pool import AsyncConnectionPool
from dotenv import load_dotenv
from typing import List, Dict, Any, Optional
from datetime import datetime, date, timezone

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
            logger.error("DATABASE_URL environment variable is not set.")
            raise ValueError("DATABASE_URL environment variable is not set.")
        try:
            # Створюємо пул з'єднань
            db_pool = AsyncConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=10, open=psycopg.AsyncConnection.connect)
            # Перевіряємо з'єднання
            async with db_pool.connection() as conn:
                await conn.execute("SELECT 1")
            logger.info("DB pool initialized successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize DB pool: {e}", exc_info=True)
            raise
    return db_pool

async def get_user_by_telegram_id(telegram_id: int) -> Optional[Dict[str, Any]]:
    """
    Отримує користувача за його Telegram ID.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            await cur.execute("SELECT * FROM users WHERE telegram_id = %s;", (telegram_id,))
            return await cur.fetchone()

async def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    """
    Отримує користувача за його внутрішнім ID.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            await cur.execute("SELECT * FROM users WHERE id = %s;", (user_id,))
            return await cur.fetchone()

async def update_user_field(telegram_id: int, field_name: str, value: Any) -> None:
    """
    Оновлює одне поле для користувача за його Telegram ID.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            # Оновлюємо ai_last_request_date, якщо оновлюється ai_requests_today
            if field_name == 'ai_requests_today':
                await cur.execute(
                    f"UPDATE users SET {field_name} = %s, ai_last_request_date = %s WHERE telegram_id = %s;",
                    (value, date.today(), telegram_id)
                )
            else:
                await cur.execute(
                    f"UPDATE users SET {field_name} = %s WHERE telegram_id = %s;",
                    (value, telegram_id)
                )
            await conn.commit()

async def get_all_active_sources() -> List[Dict[str, Any]]:
    """
    Отримує всі активні джерела новин з бази даних.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            await cur.execute("SELECT * FROM sources WHERE status = 'active' ORDER BY id;")
            return await cur.fetchall()

async def add_news_item(news_data: Dict[str, Any]) -> Optional[int]:
    """
    Додає нову новину до бази даних.
    Повертає ID нової новини або None у разі помилки.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            try:
                await cur.execute(
                    """
                    INSERT INTO news (title, content, source_url, image_url, published_at, source_id, lang)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (source_url) DO NOTHING
                    RETURNING id;
                    """,
                    (news_data['title'], news_data['content'], news_data['source_url'],
                     news_data.get('image_url'), news_data.get('published_at'),
                     news_data['source_id'], news_data.get('lang', 'uk'))
                )
                result = await cur.fetchone()
                await conn.commit()
                return result['id'] if result else None
            except Exception as e:
                logger.error(f"Помилка при додаванні новини: {e}", exc_info=True)
                await conn.rollback()
                return None

async def get_news_by_source_id(source_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Отримує новини за ID джерела.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            await cur.execute("SELECT * FROM news WHERE source_id = %s ORDER BY published_at DESC LIMIT %s;", (source_id, limit))
            return await cur.fetchall()

async def get_all_news(limit: int = 100) -> List[Dict[str, Any]]:
    """
    Отримує всі новини.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            await cur.execute("SELECT * FROM news ORDER BY published_at DESC LIMIT %s;", (limit,))
            return await cur.fetchall()

async def get_user_bookmarks(user_db_id: int, news_id: Optional[int] = None) -> Union[List[Dict[str, Any]], Dict[str, Any], None]:
    """
    Отримує закладки користувача. Якщо news_id надано, перевіряє конкретну закладку.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            if news_id:
                await cur.execute("SELECT * FROM bookmarks WHERE user_id = %s AND news_id = %s;", (user_db_id, news_id))
                return await cur.fetchone()
            else:
                await cur.execute("SELECT * FROM bookmarks WHERE user_id = %s ORDER BY created_at DESC;", (user_db_id,))
                return await cur.fetchall()

async def add_bookmark(user_db_id: int, news_id: int) -> bool:
    """
    Додає новину до закладок користувача.
    Повертає True, якщо додано, False, якщо вже існує.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(
                    "INSERT INTO bookmarks (user_id, news_id) VALUES (%s, %s) ON CONFLICT (user_id, news_id) DO NOTHING;",
                    (user_db_id, news_id)
                )
                rows_affected = cur.rowcount
                await conn.commit()
                return rows_affected > 0
            except Exception as e:
                logger.error(f"Помилка при додаванні закладки: {e}")
                await conn.rollback()
                return False

async def delete_bookmark(user_db_id: int, news_id: int) -> bool:
    """
    Видаляє новину із закладок користувача.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM bookmarks WHERE user_id = %s AND news_id = %s;", (user_db_id, news_id))
            rows_affected = cur.rowcount
            await conn.commit()
            return rows_affected > 0

async def get_user_news_views(user_db_id: int, news_id: int) -> Optional[Dict[str, Any]]:
    """
    Перевіряє, чи користувач переглядав новину.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            await cur.execute("SELECT * FROM user_news_views WHERE user_id = %s AND news_id = %s;", (user_db_id, news_id))
            return await cur.fetchone()

async def add_user_news_view(user_db_id: int, news_id: int) -> None:
    """
    Додає запис про перегляд новини користувачем.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO user_news_views (user_id, news_id) VALUES (%s, %s) ON CONFLICT (user_id, news_id) DO UPDATE SET viewed_at = EXCLUDED.viewed_at;",
                (user_db_id, news_id)
            )
            await conn.commit()

async def get_user_news_reactions(user_db_id: int, news_id: int) -> Optional[Dict[str, Any]]:
    """
    Отримує реакцію користувача на новину.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            await cur.execute("SELECT * FROM user_news_reactions WHERE user_id = %s AND news_id = %s;", (user_db_id, news_id))
            return await cur.fetchone()

async def add_user_news_reaction(user_db_id: int, news_id: int, reaction_type: Optional[str]) -> None:
    """
    Додає або оновлює реакцію користувача на новину.
    Якщо reaction_type None, видаляє реакцію.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            if reaction_type:
                await cur.execute(
                    """
                    INSERT INTO user_news_reactions (user_id, news_id, reaction_type)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (user_id, news_id) DO UPDATE SET reaction_type = EXCLUDED.reaction_type, reacted_at = EXCLUDED.reacted_at;
                    """,
                    (user_db_id, news_id, reaction_type)
                )
            else:
                await cur.execute("DELETE FROM user_news_reactions WHERE user_id = %s AND news_id = %s;", (user_db_id, news_id))
            await conn.commit()

async def update_news_item(news_id: int, update_data: Dict[str, Any]) -> None:
    """
    Оновлює дані новини за її ID.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            set_clauses = [f"{k} = %s" for k in update_data.keys()]
            query = f"UPDATE news SET {', '.join(set_clauses)} WHERE id = %s;"
            await cur.execute(query, (*update_data.values(), news_id))
            await conn.commit()

async def get_news_item_by_id(news_id: int) -> Optional[Dict[str, Any]]:
    """
    Отримує новину за її ID.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            await cur.execute("SELECT * FROM news WHERE id = %s;", (news_id,))
            return await cur.fetchone()

async def get_source_by_url(url: str) -> Optional[Dict[str, Any]]:
    """
    Отримує джерело за його URL.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            await cur.execute("SELECT * FROM sources WHERE url = %s;", (url,))
            return await cur.fetchone()

async def get_source_by_id(source_id: int) -> Optional[Dict[str, Any]]:
    """
    Отримує джерело за його ID.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            await cur.execute("SELECT * FROM sources WHERE id = %s;", (source_id,))
            return await cur.fetchone()

async def add_source(source_data: Dict[str, Any]) -> Optional[int]:
    """
    Додає нове джерело новин до бази даних.
    Повертає ID нового джерела або None у разі помилки.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            try:
                await cur.execute(
                    """
                    INSERT INTO sources (url, name, category, language, status, parse_interval_minutes)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id;
                    """,
                    (source_data['url'], source_data['name'], source_data['category'],
                     source_data['language'], source_data['status'], source_data['parse_interval_minutes'])
                )
                result = await cur.fetchone()
                await conn.commit()
                return result['id'] if result else None
            except Exception as e:
                logger.error(f"Помилка при додаванні джерела: {e}", exc_info=True)
                await conn.rollback()
                return None

async def update_source_status(source_id: int, update_data: Dict[str, Any]) -> None:
    """
    Оновлює статус або інші поля джерела за його ID.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            set_clauses = [f"{k} = %s" for k in update_data.keys()]
            query = f"UPDATE sources SET {', '.join(set_clauses)} WHERE id = %s;"
            await cur.execute(query, (*update_data.values(), source_id))
            await conn.commit()

async def get_all_sources() -> List[Dict[str, Any]]:
    """
    Отримує всі джерела новин з бази даних.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            await cur.execute("SELECT * FROM sources ORDER BY id;")
            return await cur.fetchall()

async def get_bot_setting(setting_key: str) -> Optional[str]:
    """
    Отримує значення налаштування бота за ключем.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT setting_value FROM bot_settings WHERE setting_key = %s;", (setting_key,))
            result = await cur.fetchone()
            return result[0] if result else None

async def update_bot_setting(setting_key: str, setting_value: str) -> None:
    """
    Оновлює або додає налаштування бота.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO bot_settings (setting_key, setting_value)
                VALUES (%s, %s)
                ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value;
                """,
                (setting_key, setting_value)
            )
            await conn.commit()

async def get_last_n_news(source_ids: Optional[List[int]] = None, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Отримує останні N новин, опціонально фільтруючи за ID джерел.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            if source_ids:
                # Використовуємо UNNEST для роботи зі списком ID
                await cur.execute(
                    "SELECT * FROM news WHERE source_id = ANY(%s) ORDER BY published_at DESC LIMIT %s;",
                    (source_ids, limit)
                )
            else:
                await cur.execute("SELECT * FROM news ORDER BY published_at DESC LIMIT %s;", (limit,))
            return await cur.fetchall()

async def update_source_last_parsed(source_id: int) -> None:
    """
    Оновлює час останнього парсингу для джерела.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("UPDATE sources SET last_parsed_at = %s WHERE id = %s;", (datetime.now(timezone.utc), source_id))
            await conn.commit()

async def get_news_for_digest(user_id: int, frequency: str) -> List[Dict[str, Any]]:
    """
    Отримує новини для дайджесту користувача на основі частоти та підписок.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            # Отримуємо підписки користувача
            await cur.execute("SELECT source_id FROM user_subscriptions WHERE user_id = %s;", (user_id,))
            subscribed_source_ids = [row['source_id'] for row in await cur.fetchall()]

            if not subscribed_source_ids:
                return []

            time_delta = None
            if frequency == 'daily':
                time_delta = timedelta(days=1)
            elif frequency == 'weekly':
                time_delta = timedelta(weeks=1)
            else:
                return [] # Невідома частота або вимкнено

            # Отримуємо новини, опубліковані за останній період
            cutoff_time = datetime.now(timezone.utc) - time_delta
            await cur.execute(
                "SELECT * FROM news WHERE source_id = ANY(%s) AND published_at >= %s ORDER BY published_at DESC LIMIT 20;",
                (subscribed_source_ids, cutoff_time)
            )
            return await cur.fetchall()

async def get_tasks_by_status(status: str) -> List[Dict[str, Any]]:
    """
    Отримує завдання з черги за статусом.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            await cur.execute("SELECT * FROM task_queue WHERE status = %s ORDER BY scheduled_at ASC;", (status,))
            return await cur.fetchall()

async def update_task_status(task_id: int, new_status: str, error_message: Optional[str] = None) -> None:
    """
    Оновлює статус завдання в черзі.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            if error_message:
                await cur.execute(
                    "UPDATE task_queue SET status = %s, processed_at = %s, error_message = %s WHERE id = %s;",
                    (new_status, datetime.now(timezone.utc), error_message, task_id)
                )
            else:
                await cur.execute(
                    "UPDATE task_queue SET status = %s, processed_at = %s WHERE id = %s;",
                    (new_status, datetime.now(timezone.utc), task_id)
                )
            await conn.commit()

async def add_task_to_queue(task_type: str, task_data: Dict[str, Any], scheduled_at: datetime) -> Optional[int]:
    """
    Додає нове завдання до черги.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            try:
                await cur.execute(
                    """
                    INSERT INTO task_queue (task_type, task_data, scheduled_at)
                    VALUES (%s, %s, %s)
                    RETURNING id;
                    """,
                    (task_type, json.dumps(task_data), scheduled_at)
                )
                result = await cur.fetchone()
                await conn.commit()
                return result['id'] if result else None
            except Exception as e:
                logger.error(f"Помилка при додаванні завдання до черги: {e}", exc_info=True)
                await conn.rollback()
                return None

async def get_all_users() -> List[Dict[str, Any]]:
    """
    Отримує всіх користувачів.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            await cur.execute("SELECT * FROM users ORDER BY id;")
            return await cur.fetchall()

async def get_user_subscriptions(user_db_id: int, source_id: Optional[int] = None) -> Union[List[Dict[str, Any]], Dict[str, Any], None]:
    """
    Отримує підписки користувача. Якщо source_id надано, перевіряє конкретну підписку.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            if source_id:
                await cur.execute("SELECT * FROM user_subscriptions WHERE user_id = %s AND source_id = %s;", (user_db_id, source_id))
                return await cur.fetchone()
            else:
                await cur.execute("SELECT * FROM user_subscriptions WHERE user_id = %s ORDER BY subscribed_at DESC;", (user_db_id,))
                return await cur.fetchall()

async def add_user_subscription(user_db_id: int, source_id: int) -> bool:
    """
    Додає підписку користувача на джерело.
    Повертає True, якщо додано, False, якщо вже існує.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(
                    "INSERT INTO user_subscriptions (user_id, source_id) VALUES (%s, %s) ON CONFLICT (user_id, source_id) DO NOTHING;",
                    (user_db_id, source_id)
                )
                rows_affected = cur.rowcount
                await conn.commit()
                return rows_affected > 0
            except Exception as e:
                logger.error(f"Помилка при додаванні підписки: {e}")
                await conn.rollback()
                return False

async def delete_user_subscription(user_db_id: int, source_id: int) -> bool:
    """
    Видаляє підписку користувача на джерело.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM user_subscriptions WHERE user_id = %s AND source_id = %s;", (user_db_id, source_id))
            rows_affected = cur.rowcount
            await conn.commit()
            return rows_affected > 0

async def get_all_subscribed_sources(user_db_id: int) -> List[Dict[str, Any]]:
    """
    Отримує всі джерела, на які підписаний користувач.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            await cur.execute("""
                SELECT s.* FROM sources s
                JOIN user_subscriptions us ON s.id = us.source_id
                WHERE us.user_id = %s;
            """, (user_db_id,))
            return await cur.fetchall()

async def get_source_stats(source_id: int) -> Optional[Dict[str, Any]]:
    """
    Отримує статистику для джерела (наприклад, кількість новин).
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            await cur.execute("SELECT news_count FROM source_stats WHERE source_id = %s;", (source_id,))
            return await cur.fetchone()

async def update_source_stats(source_id: int, news_count: int) -> None:
    """
    Оновлює статистику джерела.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO source_stats (source_id, news_count)
                VALUES (%s, %s)
                ON CONFLICT (source_id) DO UPDATE SET news_count = EXCLUDED.news_count;
                """,
                (source_id, news_count)
            )
            await conn.commit()

async def delete_user(user_telegram_id: int) -> None:
    """
    Видаляє користувача та всі пов'язані з ним дані.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            # Отримуємо внутрішній user_id
            await cur.execute("SELECT id FROM users WHERE telegram_id = %s;", (user_telegram_id,))
            user_db_id_row = await cur.fetchone()
            if user_db_id_row:
                user_db_id = user_db_id_row[0]
                # Видаляємо пов'язані дані
                await cur.execute("DELETE FROM user_subscriptions WHERE user_id = %s;", (user_db_id,))
                await cur.execute("DELETE FROM user_news_views WHERE user_id = %s;", (user_db_id,))
                await cur.execute("DELETE FROM user_news_reactions WHERE user_id = %s;", (user_db_id,))
                await cur.execute("DELETE FROM bookmarks WHERE user_id = %s;", (user_db_id,))
                # Нарешті, видаляємо самого користувача
                await cur.execute("DELETE FROM users WHERE id = %s;", (user_db_id,))
                await conn.commit()
                logger.info(f"Користувача {user_telegram_id} та пов'язані дані видалено.")
            else:
                logger.warning(f"Спроба видалити користувача {user_telegram_id}, якого не знайдено.")

async def delete_source(source_id: int) -> None:
    """
    Видаляє джерело та всі пов'язані з ним новини, підписки та статистику.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            # Видаляємо пов'язані дані
            await cur.execute("DELETE FROM user_subscriptions WHERE source_id = %s;", (source_id,))
            await cur.execute("DELETE FROM source_stats WHERE source_id = %s;", (source_id,))
            # Отримуємо ID новин, пов'язаних з цим джерелом
            await cur.execute("SELECT id FROM news WHERE source_id = %s;", (source_id,))
            news_ids_to_delete = [row[0] for row in await cur.fetchall()]

            if news_ids_to_delete:
                # Видаляємо реакції та перегляди для цих новин
                await cur.execute("DELETE FROM user_news_reactions WHERE news_id = ANY(%s);", (news_ids_to_delete,))
                await cur.execute("DELETE FROM user_news_views WHERE news_id = ANY(%s);", (news_ids_to_delete,))
                await cur.execute("DELETE FROM bookmarks WHERE news_id = ANY(%s);", (news_ids_to_delete,))
                # Видаляємо самі новини
                await cur.execute("DELETE FROM news WHERE source_id = %s;", (source_id,))

            # Нарешті, видаляємо саме джерело
            await cur.execute("DELETE FROM sources WHERE id = %s;", (source_id,))
            await conn.commit()
            logger.info(f"Джерело {source_id} та пов'язані дані видалено.")


# Для локального тестування (не запускається при імпорті в інший файл)
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
