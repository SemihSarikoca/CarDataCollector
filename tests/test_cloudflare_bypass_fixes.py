"""Unit tests for the four CloudflareBypass fixes."""
import pytest

from src.scrapers.bypass.cloudflare import CloudflareBypass, evaluate_challenge_status


def test_evaluate_promotes_solved_page():
    html = "<html><body>Welcome to the forum</body></html>"
    assert evaluate_challenge_status(403, html, "BobIsTheOilGuy Forums") == 200


def test_evaluate_keeps_status_when_still_challenged():
    html = "<html>Enable JavaScript and cookies to continue</html>"
    assert evaluate_challenge_status(403, html, "Just a moment...") == 403


def test_evaluate_keeps_status_on_cf_marker():
    html = "<div class='cf-challenge'>checking</div>"
    assert evaluate_challenge_status(403, html, "Attention Required!") == 403


def test_evaluate_passes_through_200():
    assert evaluate_challenge_status(200, "<html>ok</html>", "Forum") == 200


def _bypass():
    cfg = {"cloudflare": {"enabled": True, "use_playwright": True,
                          "use_cloudscraper": True, "min_delay": 0, "max_delay": 0,
                          "max_retries": 2, "retry_delay": 0}}
    b = CloudflareBypass(cfg)
    b._browser = object()  # truthy so the playwright branch is taken
    b._cloudscraper = object()  # truthy so cloudscraper branch is reachable
    return b


@pytest.mark.asyncio
async def test_cloudscraper_reached_when_playwright_403(monkeypatch):
    b = _bypass()
    calls = []

    async def fake_pw(url, wait_for_selector=None):
        calls.append("pw")
        return "<html>Just a moment...</html>", 403

    def fake_cs(url):
        calls.append("cs")
        return "<html>real</html>", 200

    monkeypatch.setattr(b, "_fetch_with_playwright", fake_pw)
    monkeypatch.setattr(b, "_fetch_with_cloudscraper", fake_cs)
    content, status = await b.fetch("https://x/y")
    assert content == "<html>real</html>"
    assert status == 200
    assert calls == ["pw", "cs"]  # cloudscraper actually tried after pw 403


@pytest.mark.asyncio
async def test_rotate_called_before_retry(monkeypatch):
    b = _bypass()
    rotations = []

    async def fake_pw(url, wait_for_selector=None):
        return None, 403

    def fake_cs(url):
        return None, 403

    async def fake_rotate():
        rotations.append(1)

    monkeypatch.setattr(b, "_fetch_with_playwright", fake_pw)
    monkeypatch.setattr(b, "_fetch_with_cloudscraper", fake_cs)
    monkeypatch.setattr(b, "rotate_identity", fake_rotate)
    content, status = await b.fetch("https://x/y")
    assert content is None
    # max_retries=2 → one rotation between the two attempts.
    assert len(rotations) == 1
