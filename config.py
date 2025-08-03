import os
from dotenv import load_dotenv

# Завантажуємо змінні оточення з .env файлу
load_dotenv()

# --- Конфігурація Telegram Bot API та каналу (Обов'язково) ---
# Отримайте його від BotFather в Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# ID каналу для публікації новин. Може бути як числом, так і рядком.
# Отримайте його, надіславши будь-яке повідомлення в канал, а потім перевіривши `update.message.chat.id` через інший бот.
# Зверніть увагу: це має бути приватний канал, де ваш бот є адміністратором.
NEWS_CHANNEL_ID = os.getenv("NEWS_CHANNEL_ID")

# --- Налаштування бази даних (Обов'язково) ---
# Рядок підключення до бази даних PostgreSQL (наприклад, Neon.tech)
DATABASE_URL = os.getenv("DATABASE_URL")
