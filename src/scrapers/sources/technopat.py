"""
Technopat Forum Scraper
XenForo tabanlı forum.
"""

from datetime import datetime
from typing import Optional

from src.models import ContentCategory, ScrapedItem, SourceConfig
from src.scrapers.base_scraper import BaseScraper
from src.utils.helpers import clean_text
from src.utils.logger import get_logger

logger = get_logger("scraper.technopat")


class TechnopatScraper(BaseScraper):
    """Technopat Sosyal - XenForo tabanlı"""

    def __init__(self, source_config: SourceConfig, global_config: dict):
        super().__init__(source_config, global_config)

    async def scrape_listing(self, url: str) -> list[str]:
        html = await self.fetch_page(url)
        if not html:
            return []

        soup = self.parse_html(html)
        urls = []

        # XenForo konu listesi
        for thread in soup.select(
            "div.structItem-title a[data-tp-primary],"
            " div.structItem-title a:not(.labelLink),"
            " a.PreviewTooltip"
        ):
            href = thread.get("href")
            if href and ("/threads/" in href or "/konu/" in href):
                full_url = self.get_absolute_url(href)
                if full_url not in urls:
                    urls.append(full_url)

        logger.debug("technopat_listing_found", count=len(urls))
        return urls[:80]

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
                title_el = soup.select_one("h1.p-title-value")
                if title_el:
                    # Label span'larını kaldır
                    for label in title_el.select("span.label"):
                        label.decompose()
                    title = clean_text(title_el.get_text())

                author_el = soup.select_one("a.username")
                if author_el:
                    author = clean_text(author_el.get_text())

                time_el = soup.select_one("time.u-dt")
                if time_el:
                    date_published = self.extract_date(soup, "time.u-dt")

            # XenForo mesajları
            for post in soup.select("article.message-body div.bbWrapper"):
                text = clean_text(post.get_text(separator=" "))
                if text and len(text) > 15:
                    all_posts.append(text)

            # Sonraki sayfa
            next_el = soup.select_one("a.pageNav-jump--next")
            if next_el and next_el.get("href"):
                next_url = self.get_absolute_url(next_el["href"])
                if next_url != current_url:
                    current_url = next_url
                    page_num += 1
                else:
                    break
            else:
                break

        if not all_posts:
            return None

        combined = "\n\n---\n\n".join(all_posts)
        if len(combined) < 100:
            return None

        return ScrapedItem(
            source_name=self.name,
            source_url=self.base_url,
            page_url=url,
            title=title or "Technopat Konusu",
            content_text=combined,
            content_html="",
            author=author,
            date_published=date_published,
            category=ContentCategory.FORUM_THREAD,
            metadata={"total_posts": len(all_posts), "pages": page_num},
        )

    async def get_next_page_url(self, soup, current_url, page_num):
        next_el = soup.select_one("a.pageNav-jump--next")
        if next_el and next_el.get("href"):
            return self.get_absolute_url(next_el["href"])
        return None
