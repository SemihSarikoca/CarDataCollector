"""
Forum Scraper - For XenForo, phpBB, vBulletin, Reddit-style forums
Supports both standard forums and Q&A style sites.
"""

from datetime import datetime
from typing import Optional

from src.models import ContentCategory, ScrapedItem, SourceConfig
from src.scrapers.base_scraper import BaseScraper
from src.utils.helpers import clean_html_text, clean_text
from src.utils.logger import get_logger

logger = get_logger("forum_scraper")


class ForumScraper(BaseScraper):
    """
    General forum scraper.
    Works through CSS selectors, supports different forum software.
    """

    def __init__(self, source_config: SourceConfig, global_config: dict):
        super().__init__(source_config, global_config)

    async def scrape_listing(self, url: str) -> list[str]:
        """Extract thread URLs from forum listing page"""
        html = await self.fetch_page(url)
        if not html:
            return []

        soup = self.parse_html(html)
        thread_selector = self.selectors.get("thread_list", "")
        if not thread_selector:
            logger.warning("no_thread_selector", source=self.name)
            return []

        urls = self.extract_all_links(soup, thread_selector)
        logger.debug("listing_urls_found", source=self.name, count=len(urls))
        return urls

    async def scrape_item(self, url: str) -> Optional[ScrapedItem]:
        """Scrape a single forum thread, collecting all posts"""
        all_posts_text = []
        all_posts_html = []
        title = ""
        author = ""
        date_published = None
        current_url = url
        page_num = 1

        while current_url:
            html = await self.fetch_page(current_url)
            if not html:
                break

            soup = self.parse_html(html)

            # Get title and author from first page
            if page_num == 1:
                title_selector = self.selectors.get("thread_title", "")
                if title_selector:
                    title = self.extract_text(soup, title_selector)

                author_selector = self.selectors.get("post_author", "")
                if author_selector:
                    author = self.extract_text(soup, author_selector)

                date_selector = self.selectors.get("post_date", "")
                if date_selector:
                    date_published = self.extract_date(soup, date_selector)

            # Collect posts
            post_content_selector = self.selectors.get("post_content", "")
            if post_content_selector:
                for post_elem in soup.select(post_content_selector):
                    post_html = str(post_elem)
                    post_text = clean_text(post_elem.get_text(separator=" "))
                    if post_text and len(post_text) > 10:  # Skip very short posts
                        all_posts_html.append(post_html)
                        all_posts_text.append(post_text)

            # Also collect comments if selector exists (for Reddit-style)
            comment_selector = self.selectors.get("comment_content", "")
            if comment_selector:
                for comment_elem in soup.select(comment_selector):
                    comment_text = clean_text(comment_elem.get_text(separator=" "))
                    if comment_text and len(comment_text) > 10:
                        all_posts_text.append(comment_text)
                        all_posts_html.append(str(comment_elem))

            # Follow thread pagination
            next_url = await self.get_next_page_url(soup, current_url, page_num)
            if next_url and next_url != current_url:
                current_url = next_url
                page_num += 1
                # Limit thread pages
                if page_num > 20:
                    break
            else:
                break

        if not all_posts_text:
            return None

        combined_text = "\n\n---\n\n".join(all_posts_text)
        combined_html = "\n".join(all_posts_html)

        if len(combined_text) < 50:
            return None

        return ScrapedItem(
            source_name=self.name,
            source_url=self.base_url,
            page_url=url,
            title=title or "Untitled Thread",
            content_html=combined_html,
            content_text=combined_text,
            author=author,
            date_published=date_published,
            category=ContentCategory.FORUM_THREAD,
            language="en",
            metadata={
                "total_posts": len(all_posts_text),
                "total_pages": page_num,
            },
        )


class ContentScraper(BaseScraper):
    """
    Content/news site scraper.
    Used for article/news pages.
    """

    def __init__(self, source_config: SourceConfig, global_config: dict):
        super().__init__(source_config, global_config)

    async def scrape_listing(self, url: str) -> list[str]:
        """Extract article URLs from listing page"""
        html = await self.fetch_page(url)
        if not html:
            return []

        soup = self.parse_html(html)
        article_selector = self.selectors.get("article_list", "")
        if not article_selector:
            logger.warning("no_article_selector", source=self.name)
            return []

        urls = self.extract_all_links(soup, article_selector)
        logger.debug("listing_urls_found", source=self.name, count=len(urls))
        return urls

    async def scrape_item(self, url: str) -> Optional[ScrapedItem]:
        """Scrape a single article/news page"""
        html = await self.fetch_page(url)
        if not html:
            return None

        soup = self.parse_html(html)

        # Title
        title_selector = self.selectors.get("article_title", "h1")
        title = self.extract_text(soup, title_selector)

        # Content
        content_selector = self.selectors.get("article_content", "article")
        content_html = self.extract_html(soup, content_selector)
        content_text = clean_html_text(content_html) if content_html else ""

        if not content_text or len(content_text) < 50:
            return None

        # Date
        date_selector = self.selectors.get("article_date", "time")
        date_published = self.extract_date(soup, date_selector)

        # Author
        author_selector = self.selectors.get("article_author", "")
        author = self.extract_text(soup, author_selector) if author_selector else ""

        return ScrapedItem(
            source_name=self.name,
            source_url=self.base_url,
            page_url=url,
            title=title or "Untitled Article",
            content_html=content_html,
            content_text=content_text,
            author=author,
            date_published=date_published,
            category=ContentCategory.ARTICLE,
            language="en",
            metadata={},
        )


class QAScraper(BaseScraper):
    """
    Q&A site scraper.
    For sites like 2CarPros, JustAnswer, etc.
    """

    def __init__(self, source_config: SourceConfig, global_config: dict):
        super().__init__(source_config, global_config)

    async def scrape_listing(self, url: str) -> list[str]:
        """Extract question URLs from listing page"""
        html = await self.fetch_page(url)
        if not html:
            return []

        soup = self.parse_html(html)
        thread_selector = self.selectors.get("thread_list", "")
        if not thread_selector:
            logger.warning("no_thread_selector", source=self.name)
            return []

        urls = self.extract_all_links(soup, thread_selector)
        logger.debug("listing_urls_found", source=self.name, count=len(urls))
        return urls

    async def scrape_item(self, url: str) -> Optional[ScrapedItem]:
        """Scrape a Q&A page with question and answers"""
        html = await self.fetch_page(url)
        if not html:
            return None

        soup = self.parse_html(html)

        # Question title
        title_selector = self.selectors.get("thread_title", "h1")
        title = self.extract_text(soup, title_selector)

        # Question body
        question_selector = self.selectors.get("post_content", "")
        question_text = ""
        if question_selector:
            question_text = self.extract_text(soup, question_selector)

        # Answers
        answer_selector = self.selectors.get("answer_content", "")
        answers = []
        if answer_selector:
            answers = self.extract_all_text(soup, answer_selector)

        # Combine into Q&A format
        combined_parts = []
        if question_text:
            combined_parts.append(f"**Question:**\n{question_text}")
        if answers:
            for i, answer in enumerate(answers, 1):
                if len(answer) > 20:  # Skip very short answers
                    combined_parts.append(f"**Answer {i}:**\n{answer}")

        if not combined_parts:
            return None

        combined_text = "\n\n".join(combined_parts)

        if len(combined_text) < 100:
            return None

        # Author (questioner)
        author_selector = self.selectors.get("post_author", "")
        author = self.extract_text(soup, author_selector) if author_selector else ""

        # Date
        date_selector = self.selectors.get("post_date", "")
        date_published = self.extract_date(soup, date_selector) if date_selector else None

        return ScrapedItem(
            source_name=self.name,
            source_url=self.base_url,
            page_url=url,
            title=title or "Untitled Question",
            content_html="",  # Q&A format is text-based
            content_text=combined_text,
            author=author,
            date_published=date_published,
            category=ContentCategory.QA,
            language="en",
            metadata={
                "question_length": len(question_text),
                "answer_count": len(answers),
            },
        )


class TechnicalScraper(BaseScraper):
    """
    Technical/repair guide scraper.
    For sites like RepairPal, CarComplaints, NHTSA.
    """

    def __init__(self, source_config: SourceConfig, global_config: dict):
        super().__init__(source_config, global_config)

    async def scrape_listing(self, url: str) -> list[str]:
        """Extract technical article URLs from listing"""
        html = await self.fetch_page(url)
        if not html:
            return []

        soup = self.parse_html(html)
        article_selector = self.selectors.get("article_list", "")
        if not article_selector:
            logger.warning("no_article_selector", source=self.name)
            return []

        urls = self.extract_all_links(soup, article_selector)
        logger.debug("listing_urls_found", source=self.name, count=len(urls))
        return urls

    async def scrape_item(self, url: str) -> Optional[ScrapedItem]:
        """Scrape technical content page"""
        html = await self.fetch_page(url)
        if not html:
            return None

        soup = self.parse_html(html)

        # Title
        title_selector = self.selectors.get("article_title", "h1")
        title = self.extract_text(soup, title_selector)

        # Main content
        content_selector = self.selectors.get("article_content", "")
        content_html = ""
        content_text = ""
        
        if content_selector:
            content_html = self.extract_html(soup, content_selector)
            content_text = clean_html_text(content_html) if content_html else ""

        # For complaint sites, also extract complaint details
        complaint_selector = self.selectors.get("complaint_details", "")
        if complaint_selector:
            complaint_text = self.extract_text(soup, complaint_selector)
            if complaint_text:
                content_text = f"{content_text}\n\n{complaint_text}" if content_text else complaint_text

        if not content_text or len(content_text) < 50:
            return None

        # Try to extract vehicle info from title or content
        vehicle_info = self._extract_vehicle_info(title, content_text)

        return ScrapedItem(
            source_name=self.name,
            source_url=self.base_url,
            page_url=url,
            title=title or "Untitled Technical Content",
            content_html=content_html,
            content_text=content_text,
            author="",
            date_published=None,
            category=ContentCategory.TECHNICAL,
            language="en",
            metadata={
                "vehicle_info": vehicle_info,
            },
        )

    def _extract_vehicle_info(self, title: str, content: str) -> dict:
        """Try to extract vehicle make/model/year from text"""
        import re
        
        info = {}
        combined = f"{title} {content[:500]}"
        
        # Common car makes
        makes = [
            "Toyota", "Honda", "Ford", "Chevrolet", "Chevy", "BMW", "Mercedes",
            "Audi", "Volkswagen", "VW", "Nissan", "Hyundai", "Kia", "Mazda",
            "Subaru", "Jeep", "Dodge", "Ram", "GMC", "Lexus", "Acura", "Infiniti",
            "Cadillac", "Buick", "Lincoln", "Volvo", "Porsche", "Tesla", "Rivian"
        ]
        
        for make in makes:
            if make.lower() in combined.lower():
                info["make"] = make
                break
        
        # Year pattern (1990-2030)
        year_match = re.search(r'\b(19[9][0-9]|20[0-2][0-9]|2030)\b', combined)
        if year_match:
            info["year"] = year_match.group(1)
        
        return info
