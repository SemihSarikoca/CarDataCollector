"""
Otopark.com Scraper
İçerik/haber sitesi - WordPress tabanlı.
"""

from datetime import datetime
from typing import Optional

from src.models import ContentCategory, ScrapedItem, SourceConfig
from src.scrapers.base_scraper import BaseScraper
from src.utils.helpers import clean_html_text, clean_text
from src.utils.logger import get_logger

logger = get_logger("scraper.otopark")


class OtoparkScraper(BaseScraper):
    """Otopark.com - WordPress tabanlı otomobil haber sitesi"""

    def __init__(self, source_config: SourceConfig, global_config: dict):
        super().__init__(source_config, global_config)

    async def scrape_listing(self, url: str) -> list[str]:
        html = await self.fetch_page(url)
        if not html:
            return []

        soup = self.parse_html(html)
        urls = []

        # WordPress makale linkleri
        for article in soup.select(
            "article a.post-thumbnail,"
            " h2.entry-title a,"
            " h3.entry-title a,"
            " div.post-item a[href],"
            " article a[href]"
        ):
            href = article.get("href")
            if href and self.base_url in href:
                # Kategori ve etiket sayfalarını atla
                if "/category/" not in href and "/tag/" not in href:
                    if href not in urls:
                        urls.append(href)

        logger.debug("otopark_listing_found", count=len(urls))
        return urls[:60]

    async def scrape_item(self, url: str) -> Optional[ScrapedItem]:
        html = await self.fetch_page(url)
        if not html:
            return None

        soup = self.parse_html(html)

        # Başlık
        title_el = soup.select_one("h1.entry-title, h1.post-title, h1")
        title = clean_text(title_el.get_text()) if title_el else ""

        # İçerik
        content_el = soup.select_one(
            "div.entry-content,"
            " div.post-content,"
            " article .entry-content,"
            " div.td-post-content"
        )
        if not content_el:
            return None

        # Reklam ve share butonlarını kaldır
        for unwanted in content_el.select(
            ".sharedaddy, .sd-sharing, .ad-container, script, style,"
            " .social-share, .related-posts, .wp-caption-text"
        ):
            unwanted.decompose()

        content_html = str(content_el)
        content_text = clean_html_text(content_html)

        if len(content_text) < 100:
            return None

        # Tarih
        date_published = self.extract_date(soup, "time.entry-date, time, span.date")

        # Yazar
        author_el = soup.select_one("span.author a, a.author-name, span.td-post-author-name a")
        author = clean_text(author_el.get_text()) if author_el else ""

        # Etiketler
        tags = []
        for tag_el in soup.select("a[rel='tag'], span.tags a"):
            tags.append(clean_text(tag_el.get_text()))

        return ScrapedItem(
            source_name=self.name,
            source_url=self.base_url,
            page_url=url,
            title=title or "Otopark Haberi",
            content_html=content_html,
            content_text=content_text,
            author=author,
            date_published=date_published,
            category=ContentCategory.ARTICLE,
            tags=tags,
            metadata={},
        )

    async def get_next_page_url(self, soup, current_url, page_num):
        next_el = soup.select_one("a.next, a.page-numbers.next, li.next a")
        if next_el and next_el.get("href"):
            return next_el["href"]
        return None
