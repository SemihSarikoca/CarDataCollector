"""
Cloudflare Bypass Module
Handles Cloudflare protection, bot detection, and anti-scraping measures.
"""

import asyncio
import random
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

import cloudscraper

# Prefer patchright (playwright fork with built-in stealth); gracefully fall back
# to upstream playwright if patchright isn't installed in the environment.
try:  # pragma: no cover - runtime import guard
    from patchright.async_api import async_playwright, Browser, BrowserContext, Page  # type: ignore
    PATCHRIGHT_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover
    from playwright.async_api import async_playwright, Browser, BrowserContext, Page
    PATCHRIGHT_AVAILABLE = False

from src.utils.logger import get_logger

logger = get_logger("cloudflare_bypass")


class CloudflareBypass:
    """
    Multi-strategy Cloudflare bypass handler.
    Uses Playwright with stealth mode as primary, cloudscraper as fallback.
    """

    def __init__(self, config: dict, state_path: Optional[Path] = None):
        self.config = config.get("cloudflare", {})
        self.playwright_config = config.get("playwright", {})
        self.user_agents = config.get("user_agents", {}).get("agents", [])

        self.enabled = self.config.get("enabled", True)
        self.use_playwright = self.config.get("use_playwright", True)
        self.use_cloudscraper = self.config.get("use_cloudscraper", True)
        self.min_delay = self.config.get("min_delay", 2.0)
        self.max_delay = self.config.get("max_delay", 5.0)
        self.max_retries = self.config.get("max_retries", 3)
        self.retry_delay = self.config.get("retry_delay", 60)

        # Cookie / storage state persistence: when set, the browser context's
        # cookies + localStorage are loaded from this path on init and saved
        # back on close (and after a successful Cloudflare challenge).
        self.state_path: Optional[Path] = Path(state_path) if state_path else None

        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._cloudscraper = None
        self._current_user_agent = None

    def _get_random_user_agent(self) -> str:
        """Get a random user agent from the pool"""
        if self.user_agents:
            return random.choice(self.user_agents)
        return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

    def _get_random_viewport(self) -> dict:
        """Get a random viewport size to appear more human"""
        viewports = [
            {"width": 1920, "height": 1080},
            {"width": 1366, "height": 768},
            {"width": 1536, "height": 864},
            {"width": 1440, "height": 900},
            {"width": 1280, "height": 720},
        ]
        return random.choice(viewports)

    async def initialize(self):
        """Initialize browser and scraper instances"""
        if self.use_playwright:
            try:
                self._playwright = await async_playwright().start()
                
                # patchright applies stealth patches automatically; we should
                # NOT pass --disable-blink-features=AutomationControlled (it
                # actually un-does some of patchright's stealth fixes).
                self._browser = await self._playwright.chromium.launch(
                    headless=self.playwright_config.get("headless", True),
                    args=[
                        "--disable-dev-shm-usage",
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-infobars",
                    ],
                )
                
                self._current_user_agent = self._get_random_user_agent()
                viewport = self._get_random_viewport()

                # Reuse persisted cookies / storage state if available.
                # This lets us skip Cloudflare challenges on subsequent runs
                # for as long as the cookies remain valid (typically hours).
                context_kwargs = dict(
                    user_agent=self._current_user_agent,
                    viewport=viewport,
                    locale="en-US",
                    timezone_id="America/New_York",
                    permissions=["geolocation"],
                    java_script_enabled=True,
                )
                state_loaded = False
                if self.state_path and self.state_path.exists() and self.state_path.stat().st_size > 0:
                    context_kwargs["storage_state"] = str(self.state_path)
                    state_loaded = True

                self._context = await self._browser.new_context(**context_kwargs)

                logger.info(
                    "playwright_initialized",
                    user_agent=self._current_user_agent[:50],
                    state_loaded=state_loaded,
                    state_path=str(self.state_path) if self.state_path else None,
                )
                
            except Exception as e:
                logger.error("playwright_init_failed", error=str(e))
                self.use_playwright = False
        
        if self.use_cloudscraper:
            try:
                self._cloudscraper = cloudscraper.create_scraper(
                    browser={
                        'browser': 'chrome',
                        'platform': 'windows',
                        'mobile': False
                    }
                )
                logger.info("cloudscraper_initialized")
            except Exception as e:
                logger.error("cloudscraper_init_failed", error=str(e))
                self.use_cloudscraper = False

    async def save_state(self) -> bool:
        """Persist cookies + storage state to disk for reuse on next run."""
        if not self._context or not self.state_path:
            return False
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            await self._context.storage_state(path=str(self.state_path))
            logger.debug("storage_state_saved", path=str(self.state_path))
            return True
        except Exception as e:
            logger.warning("storage_state_save_failed", path=str(self.state_path), error=str(e))
            return False

    async def close(self):
        """Clean up resources, persisting cookies first if configured."""
        if self._context:
            await self.save_state()
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("cloudflare_bypass_closed")

    async def _random_delay(self):
        """Add random human-like delay"""
        delay = random.uniform(self.min_delay, self.max_delay)
        await asyncio.sleep(delay)

    async def _fetch_with_playwright(self, url: str, wait_for_selector: str = None) -> Tuple[Optional[str], int]:
        """
        Fetch page using Playwright with stealth mode.
        Returns (html_content, status_code)
        """
        if not self._browser or not self._context:
            return None, 0
        
        page: Optional[Page] = None
        try:
            page = await self._context.new_page()

            # patchright already provides webdriver/plugins/languages overrides;
            # custom init scripts on top of it tend to leak detectable artifacts,
            # so we skip them.

            # Navigate with timeout
            timeout = self.playwright_config.get("timeout", 60000)
            response = await page.goto(
                url, 
                wait_until="domcontentloaded",
                timeout=timeout
            )
            
            if not response:
                return None, 0
            
            status = response.status
            
            # Check for Cloudflare challenge
            challenge_seen = False
            if status == 403 or "cloudflare" in (await page.title()).lower():
                challenge_seen = True
                logger.warning("cloudflare_challenge_detected", url=url)
                # Wait for challenge to resolve
                await asyncio.sleep(5)
                await page.wait_for_load_state("networkidle", timeout=30000)
            
            # Wait for optional selector
            if wait_for_selector:
                try:
                    await page.wait_for_selector(wait_for_selector, timeout=10000)
                except:
                    pass
            
            # Wait for network idle if configured
            if self.playwright_config.get("wait_for_network_idle", True):
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except:
                    pass
            
            # Simulate human behavior
            await self._simulate_human_behavior(page)

            content = await page.content()

            # If we just cleared a challenge, persist cookies immediately so
            # the bypass survives a crash before close() runs.
            if challenge_seen and self.state_path:
                await self.save_state()

            return content, status
            
        except Exception as e:
            logger.error("playwright_fetch_error", url=url, error=str(e))
            return None, 0
        finally:
            if page:
                await page.close()

    async def _simulate_human_behavior(self, page: Page):
        """Simulate human-like behavior on page"""
        try:
            # Random scroll
            await page.evaluate("""
                window.scrollTo({
                    top: Math.random() * 500,
                    behavior: 'smooth'
                });
            """)
            await asyncio.sleep(random.uniform(0.5, 1.5))
            
            # Random mouse movement
            viewport = page.viewport_size
            if viewport:
                x = random.randint(100, viewport["width"] - 100)
                y = random.randint(100, min(500, viewport["height"] - 100))
                await page.mouse.move(x, y)
                
        except Exception:
            pass

    def _fetch_with_cloudscraper(self, url: str) -> Tuple[Optional[str], int]:
        """
        Fetch page using cloudscraper library.
        Returns (html_content, status_code)
        """
        if not self._cloudscraper:
            return None, 0
        
        try:
            response = self._cloudscraper.get(
                url,
                timeout=60,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept-Encoding": "gzip, deflate, br",
                }
            )
            return response.text, response.status_code
            
        except Exception as e:
            logger.error("cloudscraper_fetch_error", url=url, error=str(e))
            return None, 0

    async def fetch(
        self, 
        url: str, 
        use_playwright: bool = None,
        wait_for_selector: str = None
    ) -> Tuple[Optional[str], int]:
        """
        Fetch a URL with Cloudflare bypass.
        
        Args:
            url: The URL to fetch
            use_playwright: Force use of Playwright (overrides config)
            wait_for_selector: CSS selector to wait for before returning
            
        Returns:
            Tuple of (html_content, status_code)
        """
        if not self.enabled:
            return None, 0
        
        # Determine strategy
        should_use_playwright = use_playwright if use_playwright is not None else self.use_playwright
        
        await self._random_delay()
        
        for attempt in range(self.max_retries):
            # Try Playwright first
            if should_use_playwright and self._browser:
                content, status = await self._fetch_with_playwright(url, wait_for_selector)
                
                if content and status == 200:
                    logger.debug("fetch_success", url=url, method="playwright", status=status)
                    return content, status
                
                if status == 403 or status == 503:
                    logger.warning("cloudflare_blocked", url=url, attempt=attempt + 1)
                    await asyncio.sleep(self.retry_delay)
                    continue
            
            # Fallback to cloudscraper
            if self.use_cloudscraper and self._cloudscraper:
                content, status = self._fetch_with_cloudscraper(url)
                
                if content and status == 200:
                    logger.debug("fetch_success", url=url, method="cloudscraper", status=status)
                    return content, status
            
            # Wait before retry
            if attempt < self.max_retries - 1:
                await asyncio.sleep(self.retry_delay / (attempt + 1))
        
        logger.error("fetch_failed_all_attempts", url=url)
        return None, 0

    async def rotate_identity(self):
        """Rotate user agent and create new browser context"""
        if self._context:
            await self._context.close()
        
        self._current_user_agent = self._get_random_user_agent()
        viewport = self._get_random_viewport()
        
        if self._browser:
            self._context = await self._browser.new_context(
                user_agent=self._current_user_agent,
                viewport=viewport,
                locale="en-US",
                timezone_id="America/New_York",
            )
            logger.info("identity_rotated", user_agent=self._current_user_agent[:50])


class BrowserPool:
    """
    Pool of browser contexts for concurrent scraping.
    Each context has its own identity (user agent, cookies, etc.)
    """

    def __init__(self, config: dict, pool_size: int = 4):
        self.config = config
        self.pool_size = pool_size
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._contexts: list[BrowserContext] = []
        self._available: asyncio.Queue = asyncio.Queue()
        self._lock = asyncio.Lock()

    async def initialize(self):
        """Initialize browser and context pool"""
        self._playwright = await async_playwright().start()
        
        playwright_config = self.config.get("playwright", {})
        
        self._browser = await self._playwright.chromium.launch(
            headless=playwright_config.get("headless", True),
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-setuid-sandbox",
            ]
        )
        
        user_agents = self.config.get("user_agents", {}).get("agents", [])
        
        for i in range(self.pool_size):
            user_agent = random.choice(user_agents) if user_agents else f"Mozilla/5.0 Chrome/122.0.{i}"
            
            context = await self._browser.new_context(
                user_agent=user_agent,
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
            )
            
            self._contexts.append(context)
            await self._available.put(context)
        
        logger.info("browser_pool_initialized", size=self.pool_size)

    async def close(self):
        """Close all contexts and browser"""
        for context in self._contexts:
            await context.close()
        
        if self._browser:
            await self._browser.close()
        
        if self._playwright:
            await self._playwright.stop()
        
        logger.info("browser_pool_closed")

    async def acquire(self) -> BrowserContext:
        """Acquire a browser context from the pool"""
        return await self._available.get()

    async def release(self, context: BrowserContext):
        """Release a browser context back to the pool"""
        await self._available.put(context)

    async def fetch_page(self, url: str, wait_for: str = None) -> Tuple[Optional[str], int]:
        """
        Fetch a page using a pooled browser context.
        
        Args:
            url: URL to fetch
            wait_for: Optional CSS selector to wait for
            
        Returns:
            Tuple of (html_content, status_code)
        """
        context = await self.acquire()
        page = None
        
        try:
            page = await context.new_page()
            response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
            if not response:
                return None, 0
            
            if wait_for:
                try:
                    await page.wait_for_selector(wait_for, timeout=10000)
                except:
                    pass
            
            content = await page.content()
            return content, response.status
            
        except Exception as e:
            logger.error("pool_fetch_error", url=url, error=str(e))
            return None, 0
            
        finally:
            if page:
                await page.close()
            await self.release(context)
