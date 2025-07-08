import asyncio
from typing import Optional, Dict, Any, List
from bs4 import BeautifulSoup
import httpx
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
import re

async def fetch_page_content(url: str) -> Optional[str]:
    """
    Fetches the content of a given URL.
    """
    print(f"Fetching content from: {url}")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36'
            }
            response = await client.get(url, follow_redirects=True, headers=headers)
            response.raise_for_status() # Raises HTTPStatusError for bad responses (4xx or 5xx)
            return response.text
    except httpx.RequestError as e:
        print(f"HTTP error for {url}: {e}")
        return None
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return None

async def parse_single_article(url: str, html_content: str) -> Optional[Dict[str, Any]]:
    """
    Parses a single HTML page to extract title, content, image URL, and publication date.
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    domain = urlparse(url).netloc

    title = None
    og_title = soup.find('meta', property='og:title')
    if og_title and og_title.get('content'):
        title = og_title['content']
    elif soup.find('h1'):
        title = soup.find('h1').get_text(strip=True)
    elif soup.find('title'):
        title = soup.find('title').get_text(strip=True)

    content = []
    # Common selectors for article content
    content_tags = ['div', 'article', 'main', 'section']
    content_classes = ['article-content', 'post-content', 'entry-content', 'news-text', 'text-content', 'td-post-content', 'post__text', 'article__text']
    
    article_body = None
    for tag in content_tags:
        for cls in content_classes:
            found_div = soup.find(tag, class_=cls)
            if found_div:
                article_body = found_div
                break
        if article_body:
            break
    
    if not article_body: # Fallback for general paragraphs
        article_body = soup

    for p in article_body.find_all('p'):
        text = p.get_text(strip=True)
        if text:
            content.append(text)
    
    full_content = "\n\n".join(content)
    
    # Limit content length for storage/display
    if len(full_content) > 5000:
        full_content = full_content[:5000] + "..."

    image_url = None
    og_image = soup.find('meta', property='og:image')
    if og_image and og_image.get('content'):
        image_url = og_image['content']
    elif soup.find('img', class_=re.compile(r'article|post|news|main-image', re.IGNORECASE)):
        img_tag = soup.find('img', class_=re.compile(r'article|post|news|main-image', re.IGNORECASE))
        if img_tag and img_tag.get('src'):
            image_url = urljoin(url, img_tag['src'])
    
    published_at = None
    # Try to find publication date from meta tags or common elements
    date_patterns = [
        lambda s: s.find('meta', property='article:published_time'),
        lambda s: s.find('meta', property='og:updated_time'),
        lambda s: s.find('time', class_=re.compile(r'date|time|published', re.IGNORECASE)),
        lambda s: s.find('span', class_=re.compile(r'date|time|published', re.IGNORECASE)),
    ]
    
    for pattern in date_patterns:
        tag = pattern(soup)
        if tag:
            date_str = tag.get('content') or tag.get_text(strip=True)
            if date_str:
                try:
                    # Attempt to parse common date formats
                    if re.match(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}', date_str):
                        published_at = datetime.fromisoformat(date_str).astimezone(timezone.utc)
                    elif re.match(r'\d{4}/\d{2}/\d{2}', date_str):
                        published_at = datetime.strptime(date_str, '%Y/%m/%d').astimezone(timezone.utc)
                    elif re.match(r'\d{2}\.\d{2}\.\d{4}', date_str):
                        published_at = datetime.strptime(date_str, '%d.%m.%Y').astimezone(timezone.utc)
                    else:
                        # Fallback for more general parsing
                        published_at = datetime.fromisoformat(date_str.replace('Z', '+00:00')) if 'Z' in date_str else datetime.fromisoformat(date_str)
                        published_at = published_at.astimezone(timezone.utc)
                    break
                except ValueError:
                    continue
    
    # Fallback to current time if publication date cannot be determined
    if published_at is None:
        published_at = datetime.now(timezone.utc)

    # Simple language detection (can be improved with a library if needed)
    lang = 'uk' # Default to Ukrainian

    if not title or not full_content:
        print(f"Could not extract title or content for {url}")
        return None

    return {
        "title": title,
        "content": full_content,
        "source_url": url,
        "image_url": image_url,
        "published_at": published_at,
        "lang": lang
    }

async def fetch_recent_news_from_source(source_url: str, limit: int = 5) -> List[Dict[str, Any]]:
    """
    Fetches recent news articles from a given source URL.
    Attempts to find article links on the main page and parse them.
    """
    print(f"Fetching recent news from: {source_url}")
    articles_data = []
    
    main_page_content = await fetch_page_content(source_url)
    if not main_page_content:
        return []

    soup = BeautifulSoup(main_page_content, 'html.parser')
    base_domain = urlparse(source_url).netloc

    # Common patterns for news article links
    # This is a generic approach; for specific sites, more targeted selectors would be better.
    # We look for links within common news listing elements.
    link_selectors = [
        'div.news-item a', 'article a', 'h2 a', 'h3 a', 'div.item-news a', 'a.news-link',
        'a[href*="/news/"]', 'a[href*="/articles/"]', 'a[href*="/post/"]', 'a[href*="/story/"]'
    ]
    
    found_links = set() # Use a set to store unique URLs
    for selector in link_selectors:
        for a_tag in soup.select(selector):
            href = a_tag.get('href')
            if href:
                full_url = urljoin(source_url, href)
                parsed_full_url = urlparse(full_url)
                # Ensure it's on the same domain and looks like an article link (not just a category or tag)
                if parsed_full_url.netloc == base_domain and \
                   re.search(r'/\d{4}/\d{2}/\d{2}/|/\d+-\d+-\d+/', parsed_full_url.path) and \
                   full_url not in found_links: # Avoid duplicates
                    found_links.add(full_url)
                    if len(articles_data) >= limit:
                        break
        if len(articles_data) >= limit:
            break

    # If few links found, try broader search for links that might be articles
    if len(found_links) < limit:
        for a_tag in soup.find_all('a', href=True):
            href = a_tag.get('href')
            if href:
                full_url = urljoin(source_url, href)
                parsed_full_url = urlparse(full_url)
                if parsed_full_url.netloc == base_domain and \
                   (re.search(r'/\d{4}/\d{2}/\d{2}/', parsed_full_url.path) or \
                    re.search(r'/\d+-\d+-\d+/', parsed_full_url.path) or \
                    (re.search(r'/(news|articles|post|story)/', parsed_full_url.path) and len(parsed_full_url.path.split('/')) > 3)) and \
                   full_url not in found_links:
                    found_links.add(full_url)
                    if len(articles_data) >= limit:
                        break
        
    tasks = []
    for url_to_parse in list(found_links)[:limit]: # Take up to 'limit' unique URLs
        tasks.append(parse_article_from_url(url_to_parse))

    results = await asyncio.gather(*tasks)
    for res in results:
        if res:
            articles_data.append(res)
    
    # Sort by published_at if available, otherwise by order found
    articles_data.sort(key=lambda x: x.get('published_at', datetime.min.replace(tzinfo=timezone.utc)), reverse=True)

    return articles_data[:limit]

async def parse_article_from_url(url: str) -> Optional[Dict[str, Any]]:
    """
    Fetches content and then parses a single article URL.
    """
    html_content = await fetch_page_content(url)
    if html_content:
        return await parse_single_article(url, html_content)
    return None

# Local testing
if __name__ == "__main__":
    async def main():
        test_sources = [
            {'name': 'Європейська правда', 'url': 'https://www.eurointegration.com.ua/news'},
            {'name': 'Мінпром', 'url': 'https://minprom.ua/news/'},
            {'name': 'Кореспондент', 'url': 'https://ua.korrespondent.net/'},
            {'name': 'Finance.ua', 'url': 'https://news.finance.ua/ua/news/'},
            {'name': '24TV Financy', 'url': 'https://financy.24tv.ua/novyny/'},
            {'name': 'Delo.ua', 'url': 'https://delo.ua/finance/'}
        ]
        
        print("\n--- Testing fetch_recent_news_from_source ---")
        for source in test_sources:
            print(f"\nFetching from {source['name']}: {source['url']}")
            recent_news = await fetch_recent_news_from_source(source['url'], limit=3)
            if recent_news:
                for i, news_item in enumerate(recent_news):
                    print(f"  News {i+1}:")
                    print(f"    Title: {news_item.get('title', 'N/A')}")
                    print(f"    URL: {news_item.get('source_url', 'N/A')}")
                    print(f"    Published At: {news_item.get('published_at', 'N/A')}")
                    print(f"    Content (excerpt): {news_item.get('content', 'N/A')[:200]}...")
                    print(f"    Image: {news_item.get('image_url', 'N/A')}")
            else:
                print("  No news found or error during parsing.")

    asyncio.run(main())
