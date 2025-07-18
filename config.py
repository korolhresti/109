import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8088739294:AAEODIRu7koBI4K9eABlomdKsTnl6AGYi4k")


NEWS_CHANNEL_ID = int(os.getenv("NEWS_CHANNEL_ID", "8184456641"))


DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://neondb_owner:npg_oZ2hpO7IYebn@ep-bold-rain-a8719ym5-pooler.eastus2.azure.neon.tech/neondb?sslmode=require") # Updated with user's provided DATABASE_URL
