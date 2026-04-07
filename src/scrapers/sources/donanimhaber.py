"""
DonanımHaber Otomobil Forum Scraper
forum.donanimhaber.com - Türkiye'nin en büyük teknoloji forumlarından
Özel yapı nedeniyle override gerektirir.
"""

from datetime import datetime
from typing import Optional

from src.models import ContentCategory, ScrapedItem, SourceConfig
from src.scrapers.base_scraper import BaseScraper
from src.utils.helpers import clean_html_text, clean_text
from src.utils.logger import get_logger

logger = get_logger("scraper.donanimhaber")


class DonanimHaberScraper(BaseScraper):
    """DonanımHaber forum scraper - özelleştirilmiş"""

    def __init__(self, source_config: SourceConfig, global_config: dict):
        super().__init__(source_config, global_config)

    async def scrape_listing(self, url: str) -> list[str]:
        """Konu listesini çıkar"""
        html = await self.fetch_page(url)
        if not html:
            return []

        soup = self.parse_html(html)
        urls = []

        # DonanımHaber'in konu listesi yapısı
        for thread in soup.select("li.konu-sag a.konu-baslik, h3.konu-baslik a, a[href*='/konu/']"):
            href = thread.get("href")
            if href and "/konu/" in href:
                full_url = self.get_absolute_url(href)
                if full_url not in urls:
                    urls.append(full_url)

        # Alternatif seçiciler
        if not urls:
            for link in soup.select("a[href]"):
                href = link.get("href", "")
                if "/konu/" in href and href not in urls:
                    full_url = self.get_absolute_url(href)
                    # Otomobil ilişkili konuları filtrele
                    urls.append(full_url)

        logger.debug("dh_listing_found", count=len(urls), url=url)
        return urls[:100]  # Sayfa başına max 100 konu

    async def scrape_item(self, url: str) -> Optional[ScrapedItem]:
        """Tek konuyu scrape et"""
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
                # Başlık
                title_el = soup.select_one("h1.konu-baslik, h1, div.konu-baslik-icerik h1")
                if title_el:
                    title = clean_text(title_el.get_text())

                # İlk yazar
                author_el = soup.select_one("a.uye-adi, div.mesaj-bilgi a.username")
                if author_el:
                    author = clean_text(author_el.get_text())

                # Tarih
                date_el = soup.select_one("span.mesaj-tarih, time, span.tarih")
                if date_el:
                    date_published = self.extract_date(soup, "span.mesaj-tarih, time")

            # Mesajları topla
            for post in soup.select("div.mesaj-icerik, div.mesaj-metin, article.message-body"):
                text = clean_text(post.get_text(separator=" "))
                if text and len(text) > 15:
                    all_posts.append(text)

            # Sonraki sayfa
            next_page = soup.select_one("a.sonraki, a[rel='next'], li.next a")
            if next_page and next_page.get("href"):
                next_url = self.get_absolute_url(next_page["href"])
                if next_url != current_url:
                    current_url = next_url
                    page_num += 1
                else:
                    break
            else:
                break

        if not all_posts:
            return None

        combined_text = "\n\n---\n\n".join(all_posts)
        if len(combined_text) < 100:
            return None

        return ScrapedItem(
            source_name=self.name,
            source_url=self.base_url,
            page_url=url,
            title=title or "DonanımHaber Konu",
            content_html="",
            content_text=combined_text,
            author=author,
            date_published=date_published,
            category=ContentCategory.FORUM_THREAD,
            metadata={"total_posts": len(all_posts), "pages": page_num},
        )

    async def get_next_page_url(self, soup, current_url, page_num):
        """DonanımHaber sayfalama"""
        next_el = soup.select_one("a.sonraki, a[rel='next']")
        if next_el and next_el.get("href"):
            return self.get_absolute_url(next_el["href"])
        return None
