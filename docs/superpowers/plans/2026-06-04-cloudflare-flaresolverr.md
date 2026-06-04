# Cloudflare FlareSolverr Integration + Bypass Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `cloudflare_protected` forums fetchable via a per-source FlareSolverr session, and fix four defects in the remaining Playwright bypass path.

**Architecture:** A new `FlareSolverrClient` talks to a FlareSolverr Docker service over HTTP. `BaseScraper` routes `cloudflare_protected` sources exclusively through FlareSolverr (Playwright not started for them), opening one session per source per round and skipping the source if the service is down. Separately, `CloudflareBypass` gets four targeted fixes (stale status, dead cloudscraper fallback, no rotation before retry, UA/OS mismatch).

**Tech Stack:** Python 3, asyncio, aiohttp, pytest + pytest-asyncio, FlareSolverr, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-06-04-cloudflare-flaresolverr-design.md`

## File Structure

- Create: `src/scrapers/bypass/flaresolverr.py` — `FlareSolverrClient` (FlareSolverr API only).
- Modify: `src/scrapers/base_scraper.py` — routing for `cloudflare_protected`; UA/OS filter.
- Modify: `src/scrapers/bypass/cloudflare.py` — B1/B2/B3/B4 fixes.
- Modify: `config/settings.yaml` — `flaresolverr:` block.
- Modify: `docker-compose.yml` — `flaresolverr` service + `FLARESOLVERR_URL` env.
- Modify: `requirements.txt` — pytest dev deps (test-only).
- Create: `tests/conftest.py`, `tests/test_flaresolverr_client.py`, `tests/test_base_scraper_routing.py`, `tests/test_cloudflare_bypass_fixes.py`.
- Modify: `CLAUDE.md` — document `FLARESOLVERR_URL`.

---

### Task 1: Test infrastructure

**Files:**
- Modify: `requirements.txt`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Add pytest deps to requirements.txt**

Append to the end of `requirements.txt`:

```
# =============================================================================
# Testing (dev only)
# =============================================================================
pytest>=8.0.0
pytest-asyncio>=0.23.0
```

- [ ] **Step 2: Install them**

Run: `source venv/bin/activate && pip install pytest pytest-asyncio`
Expected: installs successfully.

- [ ] **Step 3: Create tests/__init__.py**

Create empty file `tests/__init__.py` (content: a single newline).

- [ ] **Step 4: Create tests/conftest.py**

```python
"""Shared pytest configuration for the test suite."""
import asyncio

import pytest

# Run all `async def test_*` without per-test decorators.
pytest_plugins = ()


def pytest_collection_modifyitems(items):
    for item in items:
        if asyncio.iscoroutinefunction(getattr(item, "function", None)):
            item.add_marker(pytest.mark.asyncio)
```

Also create `pytest.ini` at repo root:

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
```

- [ ] **Step 5: Verify pytest runs (no tests yet)**

Run: `source venv/bin/activate && pytest -q`
Expected: `no tests ran` (exit code 5) — confirms collection works.

- [ ] **Step 6: Commit**

```bash
git add requirements.txt tests/__init__.py tests/conftest.py pytest.ini
git commit -m "test: add pytest + pytest-asyncio infrastructure"
```

---

### Task 2: FlareSolverrClient

**Files:**
- Create: `src/scrapers/bypass/flaresolverr.py`
- Test: `tests/test_flaresolverr_client.py`

The client resolves its endpoint from `FLARESOLVERR_URL` env var, else
`config['flaresolverr']['url']`, else `http://localhost:8191` (mirrors how
`DatabaseManager` resolves its own DSN). It exposes `create_session`, `fetch`,
`destroy_session`, `health`. All HTTP goes through two thin internal methods
(`_command` for `POST /v1`, `_ping` for `GET /`) so tests can monkeypatch them
without mocking aiohttp.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source venv/bin/activate && pytest tests/test_flaresolverr_client.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.scrapers.bypass.flaresolverr'`.

- [ ] **Step 3: Write the implementation**

Create `src/scrapers/bypass/flaresolverr.py`:

```python
"""
FlareSolverr client.

Talks to a FlareSolverr service (https://github.com/FlareSolverr/FlareSolverr)
to fetch pages behind Cloudflare. FlareSolverr runs its own browser and returns
solved HTML, so we do not start Playwright for these sources.

Endpoint resolution order: FLARESOLVERR_URL env var > config.flaresolverr.url >
http://localhost:8191.
"""

import os
from typing import Optional, Tuple

import aiohttp

from src.utils.logger import get_logger

logger = get_logger("flaresolverr")

DEFAULT_URL = "http://localhost:8191"


class FlareSolverrClient:
    """Thin async wrapper around the FlareSolverr v1 API."""

    def __init__(self, config: dict, session: Optional[aiohttp.ClientSession] = None):
        fs_cfg = (config or {}).get("flaresolverr", {})
        self.url = (os.getenv("FLARESOLVERR_URL")
                    or fs_cfg.get("url")
                    or DEFAULT_URL).rstrip("/")
        self.max_timeout_ms = int(fs_cfg.get("max_timeout_ms", 60000))
        self._external_session = session
        self._session: Optional[aiohttp.ClientSession] = session

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _command(self, payload: dict) -> dict:
        """POST a command to /v1 and return the parsed JSON."""
        session = await self._ensure_session()
        # +10s slack so our HTTP read outlives FlareSolverr's own maxTimeout.
        timeout = aiohttp.ClientTimeout(total=self.max_timeout_ms / 1000 + 10)
        async with session.post(f"{self.url}/v1", json=payload, timeout=timeout) as resp:
            return await resp.json()

    async def _ping(self) -> bool:
        """GET the root endpoint; FlareSolverr answers 200 when ready."""
        session = await self._ensure_session()
        timeout = aiohttp.ClientTimeout(total=5)
        async with session.get(self.url, timeout=timeout) as resp:
            return resp.status == 200

    async def health(self) -> bool:
        try:
            return await self._ping()
        except Exception as e:
            logger.warning("flaresolverr_health_failed", url=self.url, error=str(e))
            return False

    async def create_session(self, name: str) -> Optional[str]:
        try:
            data = await self._command({"cmd": "sessions.create", "session": name})
        except Exception as e:
            logger.warning("flaresolverr_create_session_error", session=name, error=str(e))
            return None
        if data.get("status") == "ok":
            return name
        # A leftover session from a crashed round is fine — reuse the name.
        if "exist" in str(data.get("message", "")).lower():
            return name
        logger.warning("flaresolverr_create_session_failed", session=name,
                       message=data.get("message"))
        return None

    async def fetch(self, url: str, session_id: Optional[str]) -> Tuple[Optional[str], int]:
        payload = {"cmd": "request.get", "url": url, "maxTimeout": self.max_timeout_ms}
        if session_id:
            payload["session"] = session_id
        try:
            data = await self._command(payload)
        except Exception as e:
            logger.warning("flaresolverr_fetch_error", url=url, error=str(e))
            return None, 0
        if data.get("status") == "ok":
            solution = data.get("solution", {})
            return solution.get("response"), int(solution.get("status", 0))
        logger.warning("flaresolverr_fetch_failed", url=url, message=data.get("message"))
        return None, 0

    async def destroy_session(self, session_id: str) -> None:
        try:
            await self._command({"cmd": "sessions.destroy", "session": session_id})
        except Exception as e:
            logger.warning("flaresolverr_destroy_session_error", session=session_id, error=str(e))

    async def close(self) -> None:
        """Close the owned aiohttp session (no-op if one was injected)."""
        if self._external_session is None and self._session and not self._session.closed:
            await self._session.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `source venv/bin/activate && pytest tests/test_flaresolverr_client.py -q`
Expected: PASS (10 tests).

- [ ] **Step 5: Commit**

```bash
git add src/scrapers/bypass/flaresolverr.py tests/test_flaresolverr_client.py
git commit -m "feat: add FlareSolverrClient for cloudflare-protected sources"
```

---

### Task 3: Config + Docker service

**Files:**
- Modify: `config/settings.yaml`
- Modify: `docker-compose.yml`

- [ ] **Step 1: Add flaresolverr block to settings.yaml**

Insert immediately after the `cloudflare:` block (after its last key, before the
`playwright:` block at line ~95):

```yaml
# -----------------------------------------------------------------------------
# FlareSolverr (primary fetch path for cloudflare_protected sources)
# -----------------------------------------------------------------------------
flaresolverr:
  enabled: true
  # Overridden by FLARESOLVERR_URL env var when set.
  url: "http://localhost:8191"
  # Max time FlareSolverr spends solving a single request, in milliseconds.
  max_timeout_ms: 60000
```

- [ ] **Step 2: Add the flaresolverr service to docker-compose.yml**

Insert this service block after the `redis:` service block and before the
`# Ollama runs natively` comment / `volumes:` section:

```yaml
  # ===========================================
  # FlareSolverr (Cloudflare challenge solver)
  # ===========================================
  flaresolverr:
    image: ghcr.io/flaresolverr/flaresolverr:latest
    container_name: car-collector-flaresolverr
    restart: unless-stopped
    environment:
      - LOG_LEVEL=info
      - TZ=America/New_York
    ports:
      - "8191:8191"
    networks:
      - collector-net
    deploy:
      resources:
        limits:
          memory: 1G
          cpus: "1.0"
```

- [ ] **Step 3: Wire the env var into the collector service**

In `docker-compose.yml`, under `collector:` → `environment:`, add after the
`OLLAMA_URL` line:

```yaml
      # Cloudflare solver
      - FLARESOLVERR_URL=http://flaresolverr:8191
```

- [ ] **Step 4: Validate compose file**

Run: `docker compose config -q`
Expected: no output, exit code 0 (YAML is valid).

- [ ] **Step 5: Commit**

```bash
git add config/settings.yaml docker-compose.yml
git commit -m "feat: add FlareSolverr service + config for cloudflare sources"
```

---

### Task 4: BaseScraper routing for cloudflare_protected

**Files:**
- Modify: `src/scrapers/base_scraper.py` (`__init__`, `start`, `fetch_page`, `stop`)
- Test: `tests/test_base_scraper_routing.py`

Routing rules: for a `cloudflare_protected` source, `start()` does NOT init
Playwright; it health-checks FlareSolverr and opens a session, or sets
`self._cf_skip = True`. `fetch_page()` returns the FlareSolverr HTML, or returns
`None` immediately when `_cf_skip` (no retries). `stop()` destroys the session.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source venv/bin/activate && pytest tests/test_base_scraper_routing.py -q`
Expected: FAIL — `AttributeError: ... has no attribute '_cf_skip'` (or import error for
`FlareSolverrClient` not present in `base_scraper`).

- [ ] **Step 3: Add the import and init fields**

In `src/scrapers/base_scraper.py`, add the import next to the CloudflareBypass import
(line ~22):

```python
from src.scrapers.bypass.cloudflare import CloudflareBypass
from src.scrapers.bypass.flaresolverr import FlareSolverrClient
```

In `__init__`, after the `self._cloudflare_bypass` line (~52), add:

```python
        # FlareSolverr (used only for cloudflare_protected sources)
        self._flaresolverr: Optional[FlareSolverrClient] = None
        self._fs_session: Optional[str] = None
        self._cf_skip: bool = False
```

- [ ] **Step 4: Rewrite the bypass-init branch of start()**

In `start()`, replace the current `if self.use_playwright or self.cloudflare_protected:`
block (lines ~74-85) with:

```python
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
```

- [ ] **Step 5: Rewrite the protected branch of fetch_page()**

In `fetch_page()`, replace the current `if self.use_playwright or self.cloudflare_protected:`
block (lines ~136-153) with:

```python
                # Cloudflare-protected sources: FlareSolverr only.
                if self.cloudflare_protected:
                    if self._cf_skip or not self._flaresolverr:
                        return None  # service down → skip, no retries
                    content, status = await self._flaresolverr.fetch(url, self._fs_session)
                    if content and status == 200:
                        self._pages_scraped += 1
                        return content
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
```

- [ ] **Step 6: Update stop() cleanup**

In `stop()`, after the `if self._cloudflare_bypass:` block (lines ~110-111), add:

```python
        if self._flaresolverr:
            if self._fs_session:
                await self._flaresolverr.destroy_session(self._fs_session)
            await self._flaresolverr.close()
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `source venv/bin/activate && pytest tests/test_base_scraper_routing.py -q`
Expected: PASS (2 tests).

- [ ] **Step 8: Commit**

```bash
git add src/scrapers/base_scraper.py tests/test_base_scraper_routing.py
git commit -m "feat: route cloudflare_protected sources through FlareSolverr"
```

---

### Task 5: B1 — stale status fix (challenge promotion)

**Files:**
- Modify: `src/scrapers/bypass/cloudflare.py` (add helper + use it in `_fetch_with_playwright`)
- Test: `tests/test_cloudflare_bypass_fixes.py`

Extract a pure helper `evaluate_challenge_status(initial_status, html, title)` so the
success decision is unit-testable without a real browser, then call it in
`_fetch_with_playwright` after content is retrieved.

- [ ] **Step 1: Write the failing test**

```python
"""Unit tests for the four CloudflareBypass fixes."""
import pytest

from src.scrapers.bypass.cloudflare import (
    CloudflareBypass,
    evaluate_challenge_status,
    filter_user_agents_for_platform,
)


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source venv/bin/activate && pytest tests/test_cloudflare_bypass_fixes.py -q`
Expected: FAIL — `ImportError: cannot import name 'evaluate_challenge_status'`.

- [ ] **Step 3: Add the helper and use it**

In `src/scrapers/bypass/cloudflare.py`, add at module level (after the `logger =`
line, ~26):

```python
CHALLENGE_MARKERS = (
    "just a moment",
    "cf-challenge",
    "enable javascript and cookies",
    "attention required",
    "cf-browser-verification",
)


def evaluate_challenge_status(initial_status: int, html: str, title: str) -> int:
    """Promote a stale 403 to 200 when the page is clearly the real content.

    The initial navigation to a Cloudflare site returns 403 with the interstitial.
    After the challenge clears, the same Page holds the real HTML but `response.status`
    is still 403. If neither the title nor the body shows challenge markers, treat it
    as a success.
    """
    if initial_status == 200:
        return 200
    haystack = f"{title}\n{html}".lower()
    if any(marker in haystack for marker in CHALLENGE_MARKERS):
        return initial_status
    return 200
```

In `_fetch_with_playwright`, replace the content-return section (lines ~221-231,
from `content = await page.content()` through the challenge-save block and
`return content, status`) with:

```python
            content = await page.content()
            title = await page.title()
            status = evaluate_challenge_status(status, content, title)

            # If we just cleared a challenge, persist cookies immediately so
            # the bypass survives a crash before close() runs.
            if challenge_seen and self.state_path:
                await self.save_state()

            return content, status
```

- [ ] **Step 4: Run test to verify it passes**

Run: `source venv/bin/activate && pytest tests/test_cloudflare_bypass_fixes.py -k evaluate -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/scrapers/bypass/cloudflare.py tests/test_cloudflare_bypass_fixes.py
git commit -m "fix: promote stale 403 to 200 after cloudflare challenge clears (B1)"
```

---

### Task 6: B2 + B3 — reach cloudscraper fallback, rotate before retry

**Files:**
- Modify: `src/scrapers/bypass/cloudflare.py` (`fetch` loop)
- Test: `tests/test_cloudflare_bypass_fixes.py`

The `fetch` retry loop currently `continue`s on 403, skipping cloudscraper, and
retries with the same identity. New order per attempt: Playwright → cloudscraper →
(if both fail and attempts remain) `rotate_identity()` then wait then retry.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cloudflare_bypass_fixes.py`:

```python
def _bypass():
    cfg = {"cloudflare": {"enabled": True, "use_playwright": True,
                          "use_cloudscraper": True, "min_delay": 0, "max_delay": 0,
                          "max_retries": 2, "retry_delay": 0}}
    b = CloudflareBypass(cfg)
    b._browser = object()  # truthy so the playwright branch is taken
    b._cloudscraper = object()  # truthy so cloudscraper branch is reachable
    return b


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source venv/bin/activate && pytest tests/test_cloudflare_bypass_fixes.py -k "cloudscraper_reached or rotate" -q`
Expected: FAIL — `calls == ['pw']` (cloudscraper not reached) / rotations empty.

- [ ] **Step 3: Rewrite the fetch retry loop**

In `src/scrapers/bypass/cloudflare.py`, replace the body of the
`for attempt in range(self.max_retries):` loop (lines ~311-335) with:

```python
        for attempt in range(self.max_retries):
            # 1) Try Playwright.
            if should_use_playwright and self._browser:
                content, status = await self._fetch_with_playwright(url, wait_for_selector)
                if content and status == 200:
                    logger.debug("fetch_success", url=url, method="playwright", status=status)
                    return content, status

            # 2) Fall back to cloudscraper (now actually reached on a 403).
            if self.use_cloudscraper and self._cloudscraper:
                content, status = self._fetch_with_cloudscraper(url)
                if content and status == 200:
                    logger.debug("fetch_success", url=url, method="cloudscraper", status=status)
                    return content, status

            # 3) Both strategies failed — rotate identity before the next attempt
            #    so the retry differs from the one that was just blocked.
            if attempt < self.max_retries - 1:
                logger.warning("cloudflare_blocked", url=url, attempt=attempt + 1)
                await self.rotate_identity()
                await asyncio.sleep(self.retry_delay / (attempt + 1))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `source venv/bin/activate && pytest tests/test_cloudflare_bypass_fixes.py -k "cloudscraper_reached or rotate" -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/scrapers/bypass/cloudflare.py tests/test_cloudflare_bypass_fixes.py
git commit -m "fix: reach cloudscraper fallback + rotate identity before retry (B2/B3)"
```

---

### Task 7: B4 — UA/OS match

**Files:**
- Modify: `src/scrapers/bypass/cloudflare.py` (add `filter_user_agents_for_platform`, use in `_get_random_user_agent`)
- Modify: `src/scrapers/base_scraper.py` (`_get_random_user_agent`)
- Test: `tests/test_cloudflare_bypass_fixes.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cloudflare_bypass_fixes.py`:

```python
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/122.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) Chrome/122.0 Safari/537.36",
]


def test_filter_picks_macos_on_darwin():
    out = filter_user_agents_for_platform(UA_POOL, "darwin")
    assert out
    assert all("Macintosh" in ua for ua in out)


def test_filter_picks_windows_on_win32():
    out = filter_user_agents_for_platform(UA_POOL, "win32")
    assert all("Windows" in ua for ua in out)


def test_filter_falls_back_to_full_pool_when_no_match():
    pool = ["Mozilla/5.0 (X11; Linux x86_64) Chrome/122.0"]
    out = filter_user_agents_for_platform(pool, "darwin")
    assert out == pool  # no macOS UA → don't return empty
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source venv/bin/activate && pytest tests/test_cloudflare_bypass_fixes.py -k filter -q`
Expected: FAIL — `ImportError: cannot import name 'filter_user_agents_for_platform'`.

- [ ] **Step 3: Add the helper in cloudflare.py**

In `src/scrapers/bypass/cloudflare.py`, add after `evaluate_challenge_status`:

```python
_PLATFORM_UA_TOKEN = {
    "darwin": "Macintosh",
    "win32": "Windows",
    "linux": "Linux",
}


def filter_user_agents_for_platform(agents: list[str], platform: str) -> list[str]:
    """Return only UAs whose OS token matches the host platform.

    A macOS Chrome advertising a Linux/Windows UA conflicts with Client-Hints
    (Sec-CH-UA-Platform) and is trivially flagged. Falls back to the full pool
    when no UA matches (never returns empty).
    """
    token = _PLATFORM_UA_TOKEN.get(platform)
    if not token:
        return agents
    matching = [ua for ua in agents if token in ua]
    return matching or agents
```

Add `import sys` at the top of `cloudflare.py` (next to `import random`).

In `CloudflareBypass._get_random_user_agent` (lines ~58-62), replace the body with:

```python
    def _get_random_user_agent(self) -> str:
        """Get a random user agent matching the host OS (cleaner fingerprint)."""
        pool = filter_user_agents_for_platform(self.user_agents, sys.platform)
        if pool:
            return random.choice(pool)
        return "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
```

- [ ] **Step 4: Use the same filter in base_scraper.py**

In `src/scrapers/base_scraper.py`, add `import sys` near the top imports (next to
`import random`), and add the import next to the FlareSolverr import:

```python
from src.scrapers.bypass.cloudflare import CloudflareBypass, filter_user_agents_for_platform
```

Replace `_get_random_user_agent` (lines ~66-70) with:

```python
    def _get_random_user_agent(self) -> str:
        """Get random user agent matching the host OS (cleaner fingerprint)."""
        pool = filter_user_agents_for_platform(self._user_agents, sys.platform)
        if pool:
            return random.choice(pool)
        return "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `source venv/bin/activate && pytest tests/test_cloudflare_bypass_fixes.py -q`
Expected: PASS (whole file: B1 + B2/B3 + B4 tests).

- [ ] **Step 6: Commit**

```bash
git add src/scrapers/bypass/cloudflare.py src/scrapers/base_scraper.py tests/test_cloudflare_bypass_fixes.py
git commit -m "fix: match user-agent OS token to host platform (B4)"
```

---

### Task 8: Docs + full verification

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Document FLARESOLVERR_URL**

In `CLAUDE.md`, in the "Environment variables override" list (the one with
`DATABASE_URL` / `REDIS_URL` / `OLLAMA_URL`), add:

```markdown
- `FLARESOLVERR_URL` — FlareSolverr API URL for cloudflare_protected sources (e.g. `http://localhost:8191`)
```

- [ ] **Step 2: Run the full test suite**

Run: `source venv/bin/activate && pytest -q`
Expected: PASS — all tests across the three test files (no failures, no errors).

- [ ] **Step 3: Import smoke check**

Run: `source venv/bin/activate && python -c "from src.scrapers.base_scraper import BaseScraper; from src.scrapers.bypass.flaresolverr import FlareSolverrClient; print('imports ok')"`
Expected: `imports ok`.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document FLARESOLVERR_URL env override"
```

---

## Self-Review

**Spec coverage:**
- Part A FlareSolverrClient → Task 2. ✓
- Routing (no Playwright for CF, session per source, skip on down) → Task 4. ✓
- Persistent session per source → Task 4 (`create_session` in start, `destroy_session` in stop). ✓
- Degradation = skip + warning → Task 4 (`_cf_skip`, `flaresolverr_unavailable`). ✓
- docker-compose service + settings block + env → Task 3. ✓
- B1 stale status → Task 5. ✓
- B2 dead fallback → Task 6. ✓
- B3 rotate before retry → Task 6. ✓
- B4 UA/OS match → Task 7. ✓
- Testing (mocked client, routing, B1–B4) → Tasks 2,4,5,6,7. ✓
- Docs `FLARESOLVERR_URL` → Task 8. ✓

**Type consistency:** `FlareSolverrClient` methods (`health`, `create_session`,
`fetch`, `destroy_session`, `close`) are defined in Task 2 and used identically in
Task 4 and the `_FakeFS` test double. `evaluate_challenge_status` and
`filter_user_agents_for_platform` defined in Tasks 5/7 and imported in tests with
matching signatures.

**Placeholder scan:** none — every code step contains full code.
