"""
Base Scraper (Abstract Base)
All source scrapers inherit from this class.
Supports both aiohttp (fast, for simple sites) and Playwright (for JS-heavy/protected sites).
"""

import asyncio
import hashlib
import random
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator, Optional
from urllib.parse import urljoin, urlparse

import aiohttp
from aiolimiter import AsyncLimiter
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

from src.models import ContentCategory, ScrapedItem, SourceConfig
from src.scrapers.bypass.cloudflare import CloudflareBypass
from src.scrapers.bypass.flaresolverr import FlareSolverrClient
from src.utils.helpers import clean_html_text, clean_text, normalize_url
from src.utils.logger import get_logger

logger = get_logger("scraper")


class BaseScraper(ABC):
    """
    Base Scraper class.
    Customized scrapers for each source extend this class.
    Supports both simple HTTP requests and Playwright for JS-rendered pages.
    """

    def __init__(self, source_config: SourceConfig, global_config: dict):
        self.config = source_config
        self.global_config = global_config
        self.name = source_config.name
        self.base_url = source_config.base_url
        self.selectors = source_config.selectors
        
        # Rate limiting
        rate_limit = getattr(source_config, 'rate_limit', 5.0)
        self.rate_limiter = AsyncLimiter(1, rate_limit)
        
        # Scraping mode
        self.use_playwright = getattr(source_config, 'use_playwright', False)
        self.cloudflare_protected = getattr(source_config, 'cloudflare_protected', False)
        
        # Cloudflare bypass handler
        self._cloudflare_bypass: Optional[CloudflareBypass] = None

        # FlareSolverr (used only for cloudflare_protected sources)
        self._flaresolverr: Optional[FlareSolverrClient] = None
        self._fs_session: Optional[str] = None
        self._cf_skip: bool = False
        
        # HTTP session (for non-Playwright requests)
        self._session: Optional[aiohttp.ClientSession] = None
        
        # User agents
        self._user_agents = global_config.get("user_agents", {}).get("agents", [])
        
        # Stats
        self._pages_scraped = 0
        self._items_scraped = 0
        self._errors = 0
        self._is_running = False

    def _get_random_user_agent(self) -> str:
        """Get random user agent"""
        if self._user_agents:
            return random.choice(self._user_agents)
        return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

    async def start(self):
        """Initialize session and bypass handlers"""
        self._cf_skip = False
        self._flaresolverr = None
        self._fs_session = None
        # Cloudflare-protected sources go exclusively through FlareSolverr;
        # we do NOT start Playwright for them (avoids two browsers at once).
        if self.cloudflare_protected:
            self._flaresolverr = FlareSolverrClient(self.global_config)
            if await self._flaresolverr.health():
                self._fs_session = await self._flaresolverr.create_session(self.name)
                if not self._fs_session:
                    self._cf_skip = True
                    logger.warning("flaresolverr_session_failed", source=self.name)
                else:
                    logger.info("flaresolverr_ready", source=self.name)
            else:
                self._cf_skip = True
                logger.warning("flaresolverr_unavailable", source=self.name,
                               url=self._flaresolverr.url)
        elif self.use_playwright:
            # Per-source cookie jar so different domains don't cross-contaminate.
            storage_cfg = self.global_config.get("storage", {})
            base_path = Path(storage_cfg.get("base_path", "./data"))
            state_path = base_path / "cookies" / f"{self.name}.json"

            self._cloudflare_bypass = CloudflareBypass(self.global_config, state_path=state_path)
            await self._cloudflare_bypass.initialize()
            logger.info("cloudflare_bypass_started", source=self.name, state_path=str(state_path))
        
        # Initialize aiohttp session for simple requests
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(
                total=self.global_config.get("general", {}).get("request_timeout_seconds", 60)
            )
            headers = {
                "User-Agent": self._get_random_user_agent(),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "DNT": "1",
                "Upgrade-Insecure-Requests": "1",
            }
            self._session = aiohttp.ClientSession(timeout=timeout, headers=headers)
        
        self._is_running = True
        logger.info("scraper_started", source=self.name, playwright=self.use_playwright)

    async def stop(self):
        """Close session and cleanup"""
        self._is_running = False
        
        if self._cloudflare_bypass:
            await self._cloudflare_bypass.close()

        if self._flaresolverr:
            if self._fs_session:
                await self._flaresolverr.destroy_session(self._fs_session)
            await self._flaresolverr.close()

        if self._session and not self._session.closed:
            await self._session.close()
        
        logger.info(
            "scraper_stopped",
            source=self.name,
            pages=self._pages_scraped,
            items=self._items_scraped,
            errors=self._errors
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=30))
    async def fetch_page(self, url: str, wait_for_selector: str = None) -> Optional[str]:
        """
        Fetch page HTML with rate limiting and retry.
        Uses FlareSolverr for cloudflare_protected sources, Playwright for JS-heavy sites,
        and aiohttp for simple sites.
        """
        async with self.rate_limiter:
            try:
                # Random jitter delay
                jitter = random.uniform(1.0, 3.0)
                await asyncio.sleep(jitter)

                # Cloudflare-protected sources: FlareSolverr only.
                if self.cloudflare_protected:
                    if self._cf_skip or not self._flaresolverr:
                        return None  # service down → skip, no retries
                    content, status = await self._flaresolverr.fetch(url, self._fs_session)
                    if content and status == 200:
                        self._pages_scraped += 1
                        return content
                    self._errors += 1
                    logger.warning("flaresolverr_fetch_unsuccessful", source=self.name,
                                   url=url, status=status)
                    return None

                # JS-heavy (non-CF) sources still use Playwright/CloudflareBypass.
                if self.use_playwright:
                    if self._cloudflare_bypass:
                        content, status = await self._cloudflare_bypass.fetch(
                            url,
                            use_playwright=self.use_playwright,
                            wait_for_selector=wait_for_selector
                        )
                        if content and status == 200:
                            self._pages_scraped += 1
                            return content
                        elif status == 403:
                            logger.warning("blocked", source=self.name, url=url)
                            await self._cloudflare_bypass.rotate_identity()
                            return None
                        else:
                            return None
                
                # Use aiohttp for simple requests
                async with self._session.get(url, allow_redirects=True) as response:
                    if response.status == 200:
                        self._pages_scraped += 1
                        content = await response.text(errors="replace")
                        return content
                    elif response.status == 429:
                        retry_after = int(response.headers.get("Retry-After", 60))
                        logger.warning("rate_limited", source=self.name, url=url, retry_after=retry_after)
                        await asyncio.sleep(retry_after)
                        return None
                    elif response.status == 403:
                        logger.warning("forbidden", source=self.name, url=url)
                        return None
                    elif response.status == 404:
                        logger.debug("not_found", source=self.name, url=url)
                        return None
                    else:
                        logger.warning("http_error", source=self.name, url=url, status=response.status)
                        return None
                        
            except asyncio.TimeoutError:
                logger.warning("timeout", source=self.name, url=url)
                self._errors += 1
                return None
            except aiohttp.ClientError as e:
                logger.warning("client_error", source=self.name, url=url, error=str(e))
                self._errors += 1
                return None
            except Exception as e:
                logger.error("fetch_error", source=self.name, url=url, error=str(e))
                self._errors += 1
                return None

    def parse_html(self, html: str) -> BeautifulSoup:
        """Parse HTML to BeautifulSoup object"""
        return BeautifulSoup(html, "lxml")

    def get_absolute_url(self, relative_url: str) -> str:
        """Convert relative URL to absolute URL"""
        return urljoin(self.base_url, relative_url)

    def get_url_hash(self, url: str) -> str:
        """Get SHA-256 hash of URL"""
        return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()

    def get_content_hash(self, content: str) -> str:
        """Get SHA-256 hash of content"""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def extract_text(self, soup: BeautifulSoup, selector: str) -> str:
        """Extract text using CSS selector"""
        element = soup.select_one(selector)
        if element:
            return clean_text(element.get_text(separator=" "))
        return ""

    def extract_html(self, soup: BeautifulSoup, selector: str) -> str:
        """Extract HTML using CSS selector"""
        element = soup.select_one(selector)
        if element:
            return str(element)
        return ""

    def extract_all_links(self, soup: BeautifulSoup, selector: str) -> list[str]:
        """Extract all links using CSS selector"""
        links = []
        for element in soup.select(selector):
            href = element.get("href")
            if href:
                links.append(self.get_absolute_url(href))
        return links

    def extract_all_text(self, soup: BeautifulSoup, selector: str) -> list[str]:
        """Extract all text elements using CSS selector"""
        texts = []
        for element in soup.select(selector):
            text = clean_text(element.get_text(separator=" "))
            if text:
                texts.append(text)
        return texts

    def extract_date(self, soup: BeautifulSoup, selector: str) -> Optional[datetime]:
        """Extract date from element"""
        element = soup.select_one(selector)
        if not element:
            return None

        # Check datetime attribute
        datetime_attr = element.get("datetime")
        if datetime_attr:
            try:
                return datetime.fromisoformat(datetime_attr.replace("Z", "+00:00"))
            except ValueError:
                pass

        # Parse text date
        text = clean_text(element.get_text())
        from dateutil import parser as date_parser
        try:
            return date_parser.parse(text, fuzzy=True)
        except (ValueError, TypeError):
            return None

    @abstractmethod
    async def scrape_listing(self, url: str, html: Optional[str] = None) -> list[str]:
        """
        Extract topic/article URLs from a listing page.
        `html` is the already-fetched page content; if None the implementation
        should fetch it itself (backward-compatible for custom scrapers).
        """
        pass

    @abstractmethod
    async def scrape_item(self, url: str) -> Optional[ScrapedItem]:
        """
        Scrape a single page/topic/article.
        Must be implemented by subclasses.
        """
        pass

    async def get_next_page_url(self, soup: BeautifulSoup, current_url: str, page_num: int) -> Optional[str]:
        """Find next page URL"""
        next_selector = self.selectors.get("pagination_next", "")
        if not next_selector:
            return None

        next_element = soup.select_one(next_selector)
        if next_element:
            href = next_element.get("href")
            if href:
                return self.get_absolute_url(href)
        return None

    async def run(self) -> AsyncGenerator[ScrapedItem, None]:
        """
        Main scraping loop.
        Iterates through start_urls, follows pagination, yields scraped items.
        """
        await self.start()

        try:
            for start_url in self.config.start_urls:
                current_url = start_url
                page_num = 1

                while current_url and self._is_running:
                    if self._pages_scraped >= self.config.max_pages_per_round:
                        logger.info(
                            "max_pages_reached",
                            source=self.name,
                            max_pages=self.config.max_pages_per_round
                        )
                        break

                    logger.debug("scraping_listing", source=self.name, url=current_url, page=page_num)

                    html = await self.fetch_page(current_url)
                    if not html:
                        break

                    soup = self.parse_html(html)

                    # Pass pre-fetched html to avoid a second download of the listing page
                    item_urls = await self.scrape_listing(current_url, html)

                    for item_url in item_urls:
                        if not self._is_running:
                            break

                        try:
                            item = await self.scrape_item(item_url)
                            if item:
                                # Add content hash
                                item.content_hash = self.get_content_hash(item.content_text)
                                item.url_hash = self.get_url_hash(item_url)
                                self._items_scraped += 1
                                yield item
                        except Exception as e:
                            self._errors += 1
                            logger.error(
                                "item_scrape_error",
                                source=self.name,
                                url=item_url,
                                error=str(e)
                            )

                    # Get next page
                    current_url = await self.get_next_page_url(soup, current_url, page_num)
                    page_num += 1

        finally:
            await self.stop()

    def get_stats(self) -> dict:
        """Get scraper statistics"""
        return {
            "source": self.name,
            "pages_scraped": self._pages_scraped,
            "items_scraped": self._items_scraped,
            "errors": self._errors,
            "is_running": self._is_running,
            "use_playwright": self.use_playwright,
        }
