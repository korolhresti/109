import logging
from typing import List, Dict, Any, Optional
import httpx
from datetime import datetime, timezone
import feedparser

logger = logging.getLogger(__name__)

async def fetch_recent_news_from_rss(rss_url: str) -> List[Dict[str, Any]]:
    """
    Fetches and parses news items from an RSS feed.
    """
    logger.info(f"Fetching news from RSS feed: {rss_url}")
    news_items = []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(rss_url)
            response.raise_for_status()

            feed = feedparser.parse(response.text)
            
            for entry in feed.entries:
                title = entry.get('title', 'No Title')
                link = entry.get('link')
                content = entry.get('summary', entry.get('description', ''))
                
                published_at_str = entry.get('published')
                published_at = None
                if published_at_str:
                    try:
                        published_at = datetime.fromtimestamp(entry.published_parsed.timestamp(), tz=timezone.utc)
                    except (ValueError, AttributeError):
                        logger.warning(f"Could not parse date for entry: {title}")

                image_url = None
                if 'media_content' in entry and entry.media_content:
                    image_url = entry.media_content[0].get('url')
                elif 'enclosures' in entry and entry.enclosures:
                    image_url = entry.enclosures[0].get('href')

                if link:
                    news_items.append({
                        'title': title,
                        'content': content,
                        'source_url': link,
                        'image_url': image_url,
                        'published_at': published_at,
                    })

    except httpx.RequestError as e:
        logger.error(f"HTTP error for {rss_url}: {e}")
    except Exception as e:
        logger.error(f"Error fetching/parsing RSS feed {rss_url}: {e}", exc_info=True)
    
    return news_items

