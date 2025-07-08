import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Telegram Bot Token (Обов'язково)
# Отримайте його від BotFather в Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8088739294:AAEODIRu7koBI4K9eABlomdKsTnl6AGYi4k")

# Telegram ID адміністратора (Обов'язково)
# Використовується для доступу до адмін-панелі бота
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0")) # Замініть на ваш Telegram ID

# URL веб-додатка (Обов'язково для веб-частини)
# Це URL, за яким буде доступний ваш FastAPI додаток на Render
WEB_APP_URL = os.getenv("WEB_APP_URL", "https://one09-if3u.onrender.com") # Updated URL

# API Key для доступу до адмін-панелі через веб-інтерфейс (Обов'язково для веб-частини)
API_KEY_NAME = "X-API-Key"
API_KEY = os.getenv("API_KEY", "your-secret-api-key") # Змініть на надійний ключ!

# Налаштування бази даних (Обов'язково)
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://neondb_owner:npg_oZ2hpO7IYebn@ep-bold-rain-a8719ym5-pooler.eastus2.azure.neon.tech/neondb?sslmode=require")

# Інші налаштування (необов'язково, якщо не використовуються)
CHANNEL_ID = os.getenv("CHANNEL_ID", "-1002766273069")
MONOBANK_CARD_NUMBER = os.getenv("MONOBANK_CARD_NUMBER", "4441111153021484")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AIzaSyBJs77DoM3llifxy3qgYuRhaD8j5emv8_A")

# GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
# OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
# NEWS_API_KEY = os.getenv("NEWS_API_KEY")
