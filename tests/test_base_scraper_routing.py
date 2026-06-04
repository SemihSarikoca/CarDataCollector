"""Routing tests: cloudflare_protected sources go through FlareSolverr only."""
import pytest

from src.models import SourceConfig, SourceType
from src.scrapers.sources.generic import GenericForumScraper


def _cf_source():
    return SourceConfig(
        name="bobistheoilguy",
        type=SourceType.FORUM,
        base_url="https://bobistheoilguy.com",
        start_urls=["https://bobistheoilguy.com/forums/"],
        cloudflare_protected=True,
        use_playwright=True,
    )


class _FakeFS:
    def __init__(self, healthy=True, html="<html>real</html>", status=200):
        self._healthy = healthy
        self._html = html
        self._status = status
        self.created = None
        self.destroyed = None
        self.fetched = []

    async def health(self):
        return self._healthy

    async def create_session(self, name):
        self.created = name
        return name if self._healthy else None

    async def fetch(self, url, session_id):
        self.fetched.append((url, session_id))
        return self._html, self._status

    async def destroy_session(self, session_id):
        self.destroyed = session_id

    async def close(self):
        pass


async def test_skip_when_flaresolverr_down(monkeypatch):
    scraper = GenericForumScraper(_cf_source(), {"general": {}, "storage": {}})
    fake = _FakeFS(healthy=False)
    monkeypatch.setattr(
        "src.scrapers.base_scraper.FlareSolverrClient", lambda *a, **k: fake
    )
    await scraper.start()
    assert scraper._cf_skip is True
    assert scraper._cloudflare_bypass is None  # Playwright never initialised
    html = await scraper.fetch_page("https://bobistheoilguy.com/forums/")
    assert html is None
    assert fake.fetched == []  # source skipped, no fetch attempted
    await scraper.stop()


async def test_fetch_through_flaresolverr_when_healthy(monkeypatch):
    scraper = GenericForumScraper(_cf_source(), {"general": {}, "storage": {}})
    fake = _FakeFS(healthy=True, html="<html>forum</html>", status=200)
    monkeypatch.setattr(
        "src.scrapers.base_scraper.FlareSolverrClient", lambda *a, **k: fake
    )
    await scraper.start()
    assert scraper._cf_skip is False
    assert scraper._cloudflare_bypass is None
    html = await scraper.fetch_page("https://bobistheoilguy.com/forums/")
    assert html == "<html>forum</html>"
    assert fake.fetched == [("https://bobistheoilguy.com/forums/", "bobistheoilguy")]
    await scraper.stop()
    assert fake.destroyed == "bobistheoilguy"
