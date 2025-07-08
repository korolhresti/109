import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Telegram Bot Token (Обов'язково)
# Отримайте його від BotFather в Telegram.
# НЕ ЗБЕРІГАЙТЕ ТОКЕН БЕЗПОСЕРЕДНЬО ТУТ У ПРОДАКШН! Використовуйте змінні оточення.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Telegram ID адміністратора (Обов'язково)
# Використовується для доступу до адмін-панелі бота.
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0")) # Замініть на ваш Telegram ID

# URL веб-додатка (Обов'язково для веб-частини)
# Це URL, за яким буде доступний ваш FastAPI додаток на Render.
# НЕ ЗБЕРІГАЙТЕ URL БЕЗПОСЕРЕДНЬО ТУТ У ПРОДАКШН! Використовуйте змінні оточення.
WEB_APP_URL = os.getenv("WEB_APP_URL")

# API Key для доступу до адмін-панелі через веб-інтерфейс (Обов'язково для веб-частини)
# Створіть свій власний надійний ключ.
# НЕ ЗБЕРІГАЙТЕ КЛЮЧ БЕЗПОСЕРЕДНЬО ТУТ У ПРОДАКШН! Використовуйте змінні оточення.
API_KEY_NAME = "X-API-Key"
API_KEY = os.getenv("API_KEY")

# ID Telegram каналу, куди будуть публікуватися новини (Обов'язково)
# Це має бути числовий ID каналу (наприклад, -1001234567890).
# Бот повинен бути адміністратором цього каналу.
NEWS_CHANNEL_ID = int(os.getenv("NEWS_CHANNEL_ID", "-1002766273069"))

# Налаштування бази даних (Обов'язково)
DATABASE_URL = os.getenv("DATABASE_URL")

