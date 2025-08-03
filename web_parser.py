import asyncio
from typing import Optional, Dict, Any, List
from bs4 import BeautifulSoup
import httpx
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
import re

async def fetch_page_content(url: str) -> Optional[str]:
    """
    Виконує HTTP-запит та отримує вміст сторінки.
    """
    print(f"Fetching content from: {url}")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36'
            }
            response = await client.get(url, follow_redirects=True, headers=headers)
            response.raise_for_status()
            return response.text
    except httpx.RequestError as e:
        print(f"HTTP error for {url}: {e}")
        return None
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return None

async def parse_single_article(url: str, html_content: str) -> Optional[Dict[str, Any]]:
    """
    Парсить одну HTML-сторінку для вилучення заголовка, вмісту, URL-адреси зображення та дати публікації.
    """
    if not html_content:
        return None

    soup = BeautifulSoup(html_content, 'lxml')
    
    # Вилучення заголовка
    title = None
    if soup.find('h1'):
        title = soup.find('h1').get_text(strip=True)
    elif soup.find('title'):
        title = soup.find('title').get_text(strip=True)

    # Вилучення вмісту (тексту)
    content = ""
    article_body = soup.find('article') or soup.find(class_=re.compile("article-body|post-content|main-content"))
    if article_body:
        paragraphs = article_body.find_all('p')
        for p in paragraphs:
            content += p.get_text(strip=True) + "\n"
    
    # Вилучення URL-адреси зображення
    image_url = None
    og_image = soup.find('meta', property='og:image')
    if og_image and og_image.get('content'):
        image_url = og_image['content']
    elif soup.find('img'):
        image_url = soup.find('img').get('src')
        if image_url and not urlparse(image_url).netloc:
            image_url = urljoin(url, image_url)
    
    # Вилучення дати публікації
    published_at = datetime.now(timezone.utc)
    published_time = soup.find('meta', property='article:published_time')
    if published_time and published_time.get('content'):
        try:
            published_at = datetime.fromisoformat(published_time['content'].replace('Z', '+00:00'))
        except (ValueError, TypeError):
            pass

    return {
        'title': title,
        'content': content,
        'image_url': image_url,
        'published_at': published_at,
        'source_url': url
    }

async def fetch_recent_news_from_source(source_url: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Витягує останні новини з головної сторінки джерела.
    """
    html_content = await fetch_page_content(source_url)
    if not html_content:
        return []

    soup = BeautifulSoup(html_content, 'lxml')
    news_links = set()
    
    # Знаходимо посилання, які містять 'news' або схожі слова
    for a in soup.find_all('a', href=True):
        href = a['href']
        parsed_href = urlparse(href)
        # Фільтруємо посилання на статті, а не на категорії, теги, тощо
        if re.search(r'/\d{4}/\d{2}/\d{2}/', href) or re.search(r'/\w+-\w+-\d+$', href) or href.endswith('.html'):
            full_url = urljoin(source_url, href)
            # Перевіряємо, що це посилання на той самий домен
            if urlparse(full_url).netloc == urlparse(source_url).netloc:
                news_links.add(full_url)
    
    news_items = []
    # Обмежуємо кількість парсингів
    links_to_parse = list(news_links)[:limit]
    
    for link in links_to_parse:
        article_html = await fetch_page_content(link)
        if article_html:
            article_data = await parse_single_article(link, article_html)
            if article_data and article_data.get('title'):
                news_items.append(article_data)
        
    return news_items
