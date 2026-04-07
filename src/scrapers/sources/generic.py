"""
Generic Forum & Content Scrapers
Konfigürasyondaki CSS seçicileri kullanarak çalışan genel scraper'lar.
Yeni kaynak eklendiğinde özel scraper yazmaya gerek kalmadan config ile çalışır.
"""

from datetime import datetime
from typing import Optional

from src.models import ContentCategory, ScrapedItem, SourceConfig
from src.scrapers.base_scraper import BaseScraper
from src.utils.helpers import clean_html_text, clean_text
from src.utils.logger import get_logger

logger = get_logger("scraper.generic")


class GenericForumScraper(BaseScraper):
    """
    Genel forum scraper - config'deki selectors ile çalışır.
    Yeni forum kaynakları eklendiğinde bu kullanılır.
    """

    def __init__(self, source_config: SourceConfig, global_config: dict):
        super().__init__(source_config, global_config)

    async def scrape_listing(self, url: str) -> list[str]:
        html = await self.fetch_page(url)
        if not html:
            return []

        soup = self.parse_html(html)
        selector = self.selectors.get("thread_list", "a")
        urls = self.extract_all_links(soup, selector)

        # Filtrele: dış linkler ve aynı sayfayı gösteren linkler
        filtered = []
        for u in urls:
            if self.base_url in u and u != url:
                if u not in filtered:
                    filtered.append(u)

        logger.debug("generic_forum_listing", source=self.name, count=len(filtered))
        return filtered[:80]

    async def scrape_item(self, url: str) -> Optional[ScrapedItem]:
        all_posts = []
        title = ""
        author = ""
        date_published = None
        current_url = url
        page_num = 1

        while current_url and page_num <= 15:
            html = await self.fetch_page(current_url)
            if not html:
                break

            soup = self.parse_html(html)

            if page_num == 1:
                title_sel = self.selectors.get("thread_title", "h1")
                title = self.extract_text(soup, title_sel)

                author_sel = self.selectors.get("post_author", "")
                if author_sel:
                    author = self.extract_text(soup, author_sel)

                date_sel = self.selectors.get("post_date", "")
                if date_sel:
                    date_published = self.extract_date(soup, date_sel)

            post_sel = self.selectors.get("post_content", "div.content")
            for post in soup.select(post_sel):
                text = clean_text(post.get_text(separator=" "))
                if text and len(text) > 15:
                    all_posts.append(text)

            # Sayfalama
            next_url = await self.get_next_page_url(soup, current_url, page_num)
            if next_url and next_url != current_url:
                current_url = next_url
                page_num += 1
            else:
                break

        if not all_posts:
            return None

        combined = "\n\n---\n\n".join(all_posts)
        if len(combined) < 50:
            return None

        return ScrapedItem(
            source_name=self.name,
            source_url=self.base_url,
            page_url=url,
            title=title or f"{self.name} Konusu",
            content_text=combined,
            content_html="",
            author=author,
            date_published=date_published,
            category=ContentCategory.FORUM_THREAD,
            metadata={"total_posts": len(all_posts), "pages": page_num},
        )


class GenericContentScraper(BaseScraper):
    """
    Genel içerik/makale scraper - config'deki selectors ile çalışır.
    Blog, haber siteleri için kullanılır.
    """

    def __init__(self, source_config: SourceConfig, global_config: dict):
        super().__init__(source_config, global_config)

    async def scrape_listing(self, url: str) -> list[str]:
        html = await self.fetch_page(url)
        if not html:
            return []

        soup = self.parse_html(html)
        selector = self.selectors.get("article_list", "article a")
        urls = self.extract_all_links(soup, selector)

        filtered = []
        for u in urls:
            if u != url and u not in filtered:
                # İç linkler
                if self.base_url in u:
                    # Kategori/tag sayfalarını atla
                    skip_patterns = ["/category/", "/tag/", "/author/", "/page/"]
                    if not any(p in u for p in skip_patterns):
                        filtered.append(u)

        logger.debug("generic_content_listing", source=self.name, count=len(filtered))
        return filtered[:60]

    async def scrape_item(self, url: str) -> Optional[ScrapedItem]:
        html = await self.fetch_page(url)
        if not html:
            return None

        soup = self.parse_html(html)

        # Başlık
        title_sel = self.selectors.get("article_title", "h1")
        title = self.extract_text(soup, title_sel)

        # İçerik
        content_sel = self.selectors.get("article_content", "article, div.content")
        content_el = soup.select_one(content_sel)
        if not content_el:
            return None

        # Gereksiz elementleri kaldır
        for unwanted in content_el.select(
            "script, style, .ad, .advertisement, .social-share,"
            " .related, nav, .comments, .wp-block-embed"
        ):
            unwanted.decompose()

        content_html = str(content_el)
        content_text = clean_html_text(content_html)

        if len(content_text) < 100:
            return None

        # Tarih
        date_sel = self.selectors.get("article_date", "time")
        date_published = self.extract_date(soup, date_sel)

        # Yazar
        author_sel = self.selectors.get("article_author", "")
        author = self.extract_text(soup, author_sel) if author_sel else ""

        return ScrapedItem(
            source_name=self.name,
            source_url=self.base_url,
            page_url=url,
            title=title or f"{self.name} Makalesi",
            content_text=content_text,
            content_html=content_html,
            author=author,
            date_published=date_published,
            category=ContentCategory.ARTICLE,
            metadata={},
        )

    async def get_next_page_url(self, soup, current_url, page_num):
        next_sel = self.selectors.get("pagination_next", "a.next")
        next_el = soup.select_one(next_sel)
        if next_el and next_el.get("href"):
            return self.get_absolute_url(next_el["href"])
        return None
