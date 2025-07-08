import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Telegram Bot Token (Обов'язково)
# Отримайте його від BotFather в Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8088739294:AAEODIRu7koBI4K9eABlomdKsTnl6AGYi4k")

# Telegram ID адміністратора (Обов'язково)
# Використовується для доступу до адмін-панелі бота
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "8184456641")) # Замініть на ваш Telegram ID

# URL веб-додатка (Обов'язково для веб-частини)
# Це URL, за яким буде доступний ваш FastAPI додаток на Render
WEB_APP_URL = os.getenv("WEB_APP_URL", "https://one07-4t7r.onrender.com") # Updated URL based on user's provided WEBHOOK_URL

# API Key для доступу до адмін-панелі через веб-інтерфейс (Обов'язково для веб-частини)
API_KEY_NAME = "X-API-Key"
API_KEY = os.getenv("API_KEY", "1234567890") # Змініть на надійний ключ!

# Налаштування бази даних (Обов'язково)
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://neondb_owner:npg_oZ2hpO7IYebn@ep-bold-rain-a8719ym5-pooler.eastus2.azure.neon.tech/neondb?sslmode=require") # Updated with user's provided DATABASE_URL

# ID каналу для публікації новин (Обов'язково для публікації новин)
NEWS_CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1002766273069")) # Змінено на CHANNEL_ID згідно з вашими змінними оточення

# API Key для Gemini (Обов'язково для AI Асистента)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AIzaSyBJs77DoM3llifxy3qgYuRhaD8j5emv8_A")

# Інші налаштування, якщо є
MONOBANK_CARD_NUMBER = os.getenv("MONOBANK_CARD_NUMBER", "4441111153021484") # Updated with user's provided MONOBANK_CARD_NUMBER
CARGO_HOME = os.getenv("CARGO_HOME", "/tmp/cargo_home") # Updated with user's provided CARGO_HOME
