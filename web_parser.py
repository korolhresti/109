import asyncio
from typing import Optional, Dict, Any
from bs4 import BeautifulSoup
import httpx
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

async def parse_website(url: str) -> Optional[Dict[str, Any]]:
    """
    Парсить веб-сторінку за вказаним URL та витягує заголовок, зміст,
    URL зображення та дату публікації.
    """
    print(f"Парсинг веб-сайту: {url}")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36'
            }
            response = await client.get(url, follow_redirects=True, headers=headers)
            response.raise_for_status() # Викликає HTTPStatusError для поганих відповідей (4xx/5xx)

        soup = BeautifulSoup(response.text, 'html.parser')

        domain = urlparse(url).netloc

        # --- 1. Заголовок ---
        title = None
        # Спроба отримати з Open Graph
        og_title = soup.find('meta', property='og:title')
        if og_title and og_title.get('content'):
            title = og_title['content'].strip()
        
        # Якщо Open Graph не знайдено, спроба отримати з тегу <title>
        if not title:
            title_tag = soup.find('title')
            if title_tag:
                title = title_tag.get_text().strip()
        
        # Якщо заголовок все ще не знайдено, спроба отримати з тегів h1
        if not title:
            h1_tag = soup.find('h1')
            if h1_tag:
                title = h1_tag.get_text().strip()

        # --- 2. Зміст (контент) ---
        content = None
        # Спроба знайти основний контент у тегах article, main, div з певними класами
        article_body = soup.find('article') or soup.find('main')
        if article_body:
            paragraphs = article_body.find_all('p')
            content = "\n".join([p.get_text().strip() for p in paragraphs if p.get_text().strip()])
        
        # Якщо контент не знайдено, спроба знайти за загальними класами
        if not content:
            content_div = soup.find('div', class_=['post-content', 'entry-content', 'article-body', 'news-content'])
            if content_div:
                paragraphs = content_div.find_all('p')
                content = "\n".join([p.get_text().strip() for p in paragraphs if p.get_text().strip()])

        # Якщо контент все ще порожній, спробуємо знайти всі параграфи
        if not content:
            all_paragraphs = soup.find_all('p')
            if all_paragraphs:
                content = "\n".join([p.get_text().strip() for p in all_paragraphs if p.get_text().strip()])
                # Обмежимо довжину контенту, щоб уникнути надто великих текстів
                if len(content) > 2000:
                    content = content[:2000] + "..."


        # --- 3. URL зображення ---
        image_url = None
        # Спроба отримати з Open Graph
        og_image = soup.find('meta', property='og:image')
        if og_image and og_image.get('content'):
            image_url = og_image['content']
        
        # Якщо Open Graph не знайдено, спроба знайти зображення в тегу <article> або <body>
        if not image_url:
            img_tag = (article_body or soup).find('img', src=True)
            if img_tag:
                image_url = urljoin(url, img_tag['src']) # Перетворюємо відносний URL на абсолютний

        # --- 4. Дата публікації ---
        published_at = None
        # Спроба знайти дату в різних мета-тегах або тегах часу
        time_tag = soup.find('time')
        if time_tag and time_tag.get('datetime'):
            try:
                published_at = datetime.fromisoformat(time_tag['datetime'].replace('Z', '+00:00'))
            except ValueError:
                pass # Продовжуємо, якщо формат невірний

        if not published_at:
            meta_pubdate = soup.find('meta', property='article:published_time') or \
                           soup.find('meta', property='og:pubdate') or \
                           soup.find('meta', property='date')
            if meta_pubdate and meta_pubdate.get('content'):
                try:
                    published_at = datetime.fromisoformat(meta_pubdate['content'].replace('Z', '+00:00'))
                except ValueError:
                    pass

        # Якщо дата не знайдена, спробуємо знайти її в тексті, але це менш надійно
        # Цей блок можна розширити для більш складного пошуку дати
        if not published_at:
            # Приклад: пошук за патерном у тексті, але це дуже залежить від сайту
            pass

        # Якщо дата все ще не встановлена, використовуємо поточний час як fallback
        if not published_at:
            published_at = datetime.now(timezone.utc)
        else:
            # Переконаємося, що дата має часовий пояс
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)

        return {
            "title": title,
            "content": content,
            "source_url": url,
            "image_url": image_url,
            "published_at": published_at,
            "lang": "uk" # Можна спробувати визначити мову, але для простоти припустимо українську
        }

    except httpx.RequestError as e:
        print(f"HTTP помилка для {url}: {e}")
        return None
    except Exception as e:
        print(f"Помилка при парсингу {url}: {e}")
        return None


# Локальне тестування
if __name__ == "__main__":
    async def main():
        test_urls = [
            "https://www.eurointegration.com.ua/news/2024/10/12/7172815/",
            "https://minprom.ua/news/ukrayinska-promyslovist-zrostaye",
            "https://ua.korrespondent.net/business/economics/4651154",
            "https://news.finance.ua/ua/news/-/531553",
            "https://financy.24tv.ua/novyny-pro-finansy_n2241",
            "https://delo.ua/finance/obligaciyi-v-ukrayini-rostut-v-cini-438902/",
        ]
        for url in test_urls:
            print(f"\n--- Парсинг: {url} ---")
            data = await parse_website(url)
            if data:
                for key, value in data.items():
                    print(f"{key}: {value}")
            else:
                print(f"Не вдалося розпарсити {url}")

    asyncio.run(main())
