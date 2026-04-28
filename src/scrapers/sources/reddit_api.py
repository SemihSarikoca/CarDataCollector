"""
Reddit Scraper using official Reddit API.

Uses PRAW when REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET are set in env,
otherwise falls back to the public JSON endpoint
(https://www.reddit.com/r/<sub>/<sort>.json) which works read-only without auth.

Both paths bypass the fragile HTML scraping path and are far more reliable
than fighting Reddit's anti-bot on the rendered web UI.
"""

import asyncio
import os
import re
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional
from urllib.parse import urlparse

import httpx

from src.models import ContentCategory, ScrapedItem, SourceConfig
from src.scrapers.base_scraper import BaseScraper
from src.utils.helpers import clean_text
from src.utils.logger import get_logger

logger = get_logger("scraper.reddit")

REDDIT_USER_AGENT = "car-data-collector/0.1 (by u/carcollector)"
REDDIT_LISTING_LIMIT = 100  # Reddit max per request


def _parse_subreddit_and_sort(url: str) -> tuple[str, str, Optional[str]]:
    """
    Parse subreddit name + sort + time filter from a Reddit URL.

    Examples:
        /r/MechanicAdvice/top/?t=all  -> ("MechanicAdvice", "top", "all")
        /r/MechanicAdvice/new/        -> ("MechanicAdvice", "new", None)
        /r/MechanicAdvice/            -> ("MechanicAdvice", "hot", None)
    """
    parsed = urlparse(url)
    parts = [p for p in parsed.path.strip("/").split("/") if p]

    subreddit = ""
    sort = "hot"
    if len(parts) >= 2 and parts[0] == "r":
        subreddit = parts[1]
        if len(parts) >= 3 and parts[2] in {"top", "new", "hot", "rising", "controversial"}:
            sort = parts[2]

    time_filter = None
    if parsed.query:
        m = re.search(r"t=([a-z]+)", parsed.query)
        if m:
            time_filter = m.group(1)

    return subreddit, sort, time_filter


class RedditAPIScraper(BaseScraper):
    """
    Reddit scraper that uses the official API instead of HTML scraping.

    Overrides BaseScraper.run() because we don't need fetch_page / HTML parsing.
    The abstract scrape_listing/scrape_item methods are stubbed but unused.
    """

    def __init__(self, source_config: SourceConfig, global_config: dict):
        super().__init__(source_config, global_config)
        self.use_playwright = False
        self.cloudflare_protected = False

        self._client_id = os.getenv("REDDIT_CLIENT_ID")
        self._client_secret = os.getenv("REDDIT_CLIENT_SECRET")
        self._mode = "praw" if (self._client_id and self._client_secret) else "json"
        self._reddit = None  # PRAW client, lazily created

    # --- Required abstract methods (unused for this scraper) ---
    async def scrape_listing(self, url: str) -> list[str]:
        return []

    async def scrape_item(self, url: str) -> Optional[ScrapedItem]:
        return None

    async def run(self) -> AsyncGenerator[ScrapedItem, None]:
        await self.start()
        logger.info("reddit_scraper_mode", source=self.name, mode=self._mode)

        try:
            for start_url in self.config.start_urls:
                subreddit, sort, time_filter = _parse_subreddit_and_sort(start_url)
                if not subreddit:
                    logger.warning("reddit_invalid_url", source=self.name, url=start_url)
                    continue

                if self._mode == "praw":
                    iterator = self._iter_praw(subreddit, sort, time_filter)
                else:
                    iterator = self._iter_json(subreddit, sort, time_filter)

                async for item in iterator:
                    if not self._is_running:
                        break
                    if self._items_scraped >= self.config.max_pages_per_round:
                        logger.info("max_pages_reached", source=self.name)
                        break

                    item.content_hash = self.get_content_hash(item.content_text)
                    item.url_hash = self.get_url_hash(item.page_url)
                    self._items_scraped += 1
                    self._pages_scraped += 1
                    yield item
        finally:
            await self.stop()

    # --- PRAW path (requires credentials) ---
    async def _iter_praw(self, subreddit: str, sort: str, time_filter: Optional[str]) -> AsyncGenerator[ScrapedItem, None]:
        if self._reddit is None:
            import praw  # lazy import so missing creds don't block JSON path
            self._reddit = praw.Reddit(
                client_id=self._client_id,
                client_secret=self._client_secret,
                user_agent=REDDIT_USER_AGENT,
                check_for_async=False,
            )
            self._reddit.read_only = True

        sub = self._reddit.subreddit(subreddit)
        listing_fn = {
            "top": lambda: sub.top(time_filter=time_filter or "all", limit=self.config.max_pages_per_round),
            "new": lambda: sub.new(limit=self.config.max_pages_per_round),
            "hot": lambda: sub.hot(limit=self.config.max_pages_per_round),
            "rising": lambda: sub.rising(limit=self.config.max_pages_per_round),
            "controversial": lambda: sub.controversial(time_filter=time_filter or "all", limit=self.config.max_pages_per_round),
        }.get(sort, lambda: sub.hot(limit=self.config.max_pages_per_round))

        # PRAW is sync; run in a thread so we don't block the event loop
        loop = asyncio.get_event_loop()
        submissions = await loop.run_in_executor(None, lambda: list(listing_fn()))

        for submission in submissions:
            if not self._is_running:
                break
            try:
                # Load comments (top-level only, no replies, capped) in the thread
                def _load():
                    submission.comment_sort = "top"
                    submission.comment_limit = 30
                    submission.comments.replace_more(limit=0)
                    return [c for c in submission.comments[:30]]

                top_comments = await loop.run_in_executor(None, _load)
                yield self._submission_to_item(submission, top_comments, subreddit)
                await asyncio.sleep(0.1)  # gentle pacing
            except Exception as e:
                self._errors += 1
                logger.warning("reddit_praw_item_error", source=self.name, error=str(e))

    def _submission_to_item(self, submission, comments, subreddit: str) -> ScrapedItem:
        body_parts = [submission.selftext or ""]
        for c in comments:
            author = str(c.author) if c.author else "[deleted]"
            body_parts.append(f"\n\n[{author} | score {c.score}]\n{c.body}")
        content_text = clean_text("\n".join(body_parts))

        published = datetime.fromtimestamp(submission.created_utc, tz=timezone.utc)
        return ScrapedItem(
            source_name=self.name,
            source_url=f"https://www.reddit.com/r/{subreddit}",
            page_url=f"https://www.reddit.com{submission.permalink}",
            title=submission.title or "",
            content_text=content_text,
            content_html="",
            author=str(submission.author) if submission.author else "[deleted]",
            date_published=published,
            category=ContentCategory.FORUM_THREAD,
            language="en",
            metadata={
                "subreddit": subreddit,
                "score": submission.score,
                "num_comments": submission.num_comments,
                "upvote_ratio": submission.upvote_ratio,
                "is_self": submission.is_self,
                "post_id": submission.id,
                "comment_count_loaded": len(comments),
            },
        )

    # --- JSON endpoint path (no credentials) ---
    async def _iter_json(self, subreddit: str, sort: str, time_filter: Optional[str]) -> AsyncGenerator[ScrapedItem, None]:
        headers = {"User-Agent": REDDIT_USER_AGENT}
        timeout = httpx.Timeout(30.0)
        after: Optional[str] = None
        fetched = 0
        max_items = self.config.max_pages_per_round

        async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
            while fetched < max_items and self._is_running:
                params = {"limit": min(REDDIT_LISTING_LIMIT, max_items - fetched), "raw_json": 1}
                if time_filter and sort in {"top", "controversial"}:
                    params["t"] = time_filter
                if after:
                    params["after"] = after

                listing_url = f"https://www.reddit.com/r/{subreddit}/{sort}.json"
                async with self.rate_limiter:
                    try:
                        resp = await client.get(listing_url, params=params)
                    except httpx.HTTPError as e:
                        logger.warning("reddit_json_listing_error", source=self.name, error=str(e))
                        return

                if resp.status_code != 200:
                    logger.warning("reddit_json_listing_status", source=self.name, status=resp.status_code, url=str(resp.url))
                    return

                data = resp.json().get("data", {})
                children = data.get("children", []) or []
                after = data.get("after")

                if not children:
                    return

                for child in children:
                    if not self._is_running or fetched >= max_items:
                        return
                    post = child.get("data", {})
                    permalink = post.get("permalink")
                    if not permalink:
                        continue

                    # Fetch comments for this post
                    comments_url = f"https://www.reddit.com{permalink}.json"
                    async with self.rate_limiter:
                        try:
                            cresp = await client.get(comments_url, params={"limit": 30, "raw_json": 1})
                            if cresp.status_code == 200:
                                cdata = cresp.json()
                                comments = self._extract_json_comments(cdata)
                            else:
                                comments = []
                        except httpx.HTTPError:
                            comments = []

                    yield self._json_post_to_item(post, comments, subreddit)
                    fetched += 1

                if not after:
                    return

    @staticmethod
    def _extract_json_comments(payload) -> list[dict]:
        """Extract top-level comments from /comments/<id>.json response."""
        if not isinstance(payload, list) or len(payload) < 2:
            return []
        comments_listing = payload[1].get("data", {}).get("children", [])
        out = []
        for c in comments_listing:
            if c.get("kind") != "t1":
                continue
            d = c.get("data", {})
            out.append({
                "author": d.get("author") or "[deleted]",
                "body": d.get("body") or "",
                "score": d.get("score", 0),
            })
        return out

    def _json_post_to_item(self, post: dict, comments: list[dict], subreddit: str) -> ScrapedItem:
        body_parts = [post.get("selftext") or ""]
        for c in comments:
            body_parts.append(f"\n\n[{c['author']} | score {c['score']}]\n{c['body']}")
        content_text = clean_text("\n".join(body_parts))

        created = post.get("created_utc")
        published = datetime.fromtimestamp(created, tz=timezone.utc) if created else None

        return ScrapedItem(
            source_name=self.name,
            source_url=f"https://www.reddit.com/r/{subreddit}",
            page_url=f"https://www.reddit.com{post.get('permalink', '')}",
            title=post.get("title") or "",
            content_text=content_text,
            content_html="",
            author=post.get("author") or "[deleted]",
            date_published=published,
            category=ContentCategory.FORUM_THREAD,
            language="en",
            metadata={
                "subreddit": subreddit,
                "score": post.get("score"),
                "num_comments": post.get("num_comments"),
                "upvote_ratio": post.get("upvote_ratio"),
                "is_self": post.get("is_self"),
                "post_id": post.get("id"),
                "comment_count_loaded": len(comments),
            },
        )
