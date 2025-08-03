import logging
from typing import List, Dict, Any, Optional
from aiogram import Bot
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

async def fetch_recent_news_from_channel(bot: Bot, channel_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Fetches the last N messages from a Telegram channel.
    Note: The bot must be an admin of the channel with permission to read history.
    """
    logger.info(f"Fetching news from Telegram channel: {channel_id}")
    news_items = []
    try:
        history = await bot.get_chat_history(chat_id=channel_id, limit=limit)
        
        for message in history.messages:
            if message.text:
                news_items.append({
                    'title': message.text.split('\n')[0] if '\n' in message.text else message.text[:50],
                    'content': message.text,
                    'source_url': f"https://t.me/{channel_id.lstrip('@')}/{message.message_id}",
                    'image_url': None, # Images need more complex parsing
                    'published_at': message.date
                })

    except Exception as e:
        logger.error(f"Error fetching from Telegram channel {channel_id}: {e}", exc_info=True)
    
    return news_items

