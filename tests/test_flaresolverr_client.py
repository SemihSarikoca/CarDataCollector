"""Unit tests for FlareSolverrClient (HTTP layer monkeypatched)."""
import pytest

from src.scrapers.bypass.flaresolverr import FlareSolverrClient


def _client(monkeypatch_env=None):
    cfg = {"flaresolverr": {"url": "http://fs:8191", "max_timeout_ms": 55000}}
    return FlareSolverrClient(cfg)


async def test_url_from_config():
    c = _client()
    assert c.url == "http://fs:8191"
    assert c.max_timeout_ms == 55000


async def test_url_env_overrides_config(monkeypatch):
    monkeypatch.setenv("FLARESOLVERR_URL", "http://env:9000")
    c = _client()
    assert c.url == "http://env:9000"


async def test_url_default_when_missing(monkeypatch):
    monkeypatch.delenv("FLARESOLVERR_URL", raising=False)
    c = FlareSolverrClient({})
    assert c.url == "http://localhost:8191"


async def test_create_session_returns_name(monkeypatch):
    c = _client()

    async def fake_command(payload):
        assert payload["cmd"] == "sessions.create"
        assert payload["session"] == "bobistheoilguy"
        return {"status": "ok", "session": "bobistheoilguy"}

    monkeypatch.setattr(c, "_command", fake_command)
    sid = await c.create_session("bobistheoilguy")
    assert sid == "bobistheoilguy"


async def test_create_session_tolerates_existing(monkeypatch):
    c = _client()

    async def fake_command(payload):
        return {"status": "error", "message": "Session already exists"}

    monkeypatch.setattr(c, "_command", fake_command)
    sid = await c.create_session("dup")
    assert sid == "dup"  # reuse the name rather than fail


async def test_create_session_none_on_hard_error(monkeypatch):
    c = _client()

    async def fake_command(payload):
        return {"status": "error", "message": "boom"}

    monkeypatch.setattr(c, "_command", fake_command)
    sid = await c.create_session("x")
    assert sid is None


async def test_fetch_returns_html_and_status(monkeypatch):
    c = _client()

    async def fake_command(payload):
        assert payload["cmd"] == "request.get"
        assert payload["url"] == "https://site/x"
        assert payload["session"] == "sess"
        assert payload["maxTimeout"] == 55000
        return {"status": "ok",
                "solution": {"status": 200, "response": "<html>ok</html>"}}

    monkeypatch.setattr(c, "_command", fake_command)
    html, status = await c.fetch("https://site/x", "sess")
    assert html == "<html>ok</html>"
    assert status == 200


async def test_fetch_error_returns_none(monkeypatch):
    c = _client()

    async def fake_command(payload):
        return {"status": "error", "message": "challenge failed"}

    monkeypatch.setattr(c, "_command", fake_command)
    html, status = await c.fetch("https://site/x", "sess")
    assert html is None
    assert status == 0


async def test_health_true_false(monkeypatch):
    c = _client()

    async def ping_ok():
        return True

    monkeypatch.setattr(c, "_ping", ping_ok)
    assert await c.health() is True

    async def ping_bad():
        return False

    monkeypatch.setattr(c, "_ping", ping_bad)
    assert await c.health() is False
