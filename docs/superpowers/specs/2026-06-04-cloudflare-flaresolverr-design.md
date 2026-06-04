# Cloudflare Forum Fetch — FlareSolverr Integration + Bypass Fixes

**Date:** 2026-06-04
**Branch:** fix/scrape-throttle-and-brotli
**Status:** Approved design

## Problem

`cloudflare_protected: true` forums (e.g. `bobistheoilguy`, `cargurus_forum`) never
fetch successfully. Reproduced against `bobistheoilguy.com/forums/`:

| Config tried | init_status | final page title | challenge solved? |
|---|---|---|---|
| patchright headless=True (current) | 403 | `Just a moment...` | no |
| patchright headless=True + real Chrome channel | 403 | `Just a moment...` | no |
| patchright headless=False (headful) | 403 | `Just a moment...` | no |

The Cloudflare managed/JS challenge is never cleared by patchright-stealth in this
environment, in **any** mode. Client-side bypass is not reliably achievable here.

Four code-level defects in `src/scrapers/bypass/cloudflare.py` make the situation
strictly worse (verified by reading code + log evidence showing
`challenge_detected → blocked ×3 → fetch_failed_all_attempts`):

1. **Stale `status`** — `_fetch_with_playwright` captures `status` from the initial
   navigation response and never re-reads it after waiting for the challenge to
   clear. Even on sites where the challenge *does* resolve, the method returns
   `(real_html, 403)` and `fetch()` discards it.
2. **Dead cloudscraper fallback** — when Playwright returns 403, `fetch()` calls
   `continue`, skipping the cloudscraper fallback entirely. The fallback is never
   reached in the exact scenario it exists for.
3. **Wasted 3×60s retries, no rotation** — on 403 the loop sleeps `retry_delay`
   (60s) and retries with the *same* context/cookies/UA. All three attempts fail
   identically → ~3 minutes wasted per blocked URL. `rotate_identity()` exists but
   is never called inside the retry loop.
4. **UA/OS mismatch** — the UA pool mixes Windows/Mac/Linux (3/2/1). On macOS Chrome,
   a randomly-chosen Linux/Windows UA conflicts with Client-Hints
   (`Sec-CH-UA-Platform`), an easily-flagged fingerprint inconsistency.

## Decisions (agreed)

- **Strategy:** For `cloudflare_protected: true` sources, FlareSolverr is the
  **primary and only** fetch path. Playwright/CloudflareBypass is **not started**
  for these sources (avoids running two browsers on M2 / 8 GB RAM).
- **Sessions:** One **persistent FlareSolverr session per source**, created at the
  start of a scrape round, reused for all pages, destroyed at round end. The
  challenge is solved once per round.
- **Degradation:** If FlareSolverr is unreachable, **skip the source** for that
  round with a single clear warning. No retries, no time waste. Consistent with the
  pipeline's existing graceful-degrade philosophy.

## Architecture

### Part A — FlareSolverr integration

**New component: `src/scrapers/bypass/flaresolverr.py` → `FlareSolverrClient`**

Single responsibility: talk to the FlareSolverr HTTP API. Depends only on `aiohttp`
(already a dependency).

Interface:
- `async create_session(name: str) -> Optional[str]` — `sessions.create`, returns
  session id (or None on failure).
- `async fetch(url: str, session_id: Optional[str]) -> Tuple[Optional[str], int]` —
  `request.get` command; returns `(html, status_code)`.
- `async destroy_session(session_id: str) -> None` — `sessions.destroy`.
- `async health() -> bool` — is the service reachable (cheap probe).

Endpoint resolved from `FLARESOLVERR_URL` env var, else `settings.yaml`
`flaresolverr.url`, default `http://localhost:8191`.

**Routing changes in `BaseScraper` (`base_scraper.py`)**

- `start()`: if `self.cloudflare_protected`:
  - Do **not** initialize Playwright / `CloudflareBypass`.
  - Construct `FlareSolverrClient`; call `health()`.
    - healthy → `create_session(self.name)`, store `self._fs_session`.
    - unhealthy → log `flaresolverr_unavailable` once, set `self._cf_skip = True`.
- `fetch_page(url)`: if `self.cloudflare_protected`:
  - `self._cf_skip` → return `None` immediately (no retry, source skipped this round).
  - else → `FlareSolverrClient.fetch(url, self._fs_session)`; on `status == 200`
    return html, else `None`.
  - The aiohttp/Playwright branches are bypassed for these sources.
- `stop()`: destroy `self._fs_session` if open; close FlareSolverr client.

Note: the `@retry` decorator on `fetch_page` stays, but the `_cf_skip` early-return
means a skipped CF source does not incur retry attempts.

**Config + infrastructure**

- `docker-compose.yml`: add `flaresolverr` service
  (`ghcr.io/flaresolverr/flaresolverr:latest`, port `8191:8191`,
  `restart: unless-stopped`). Add `FLARESOLVERR_URL=http://flaresolverr:8191` to the
  app service environment.
- `settings.yaml`: new `flaresolverr:` block — `enabled`, `url`, `max_timeout_ms`
  (passed to FlareSolverr `request.get`), `session_ttl`.
- Docs: document `FLARESOLVERR_URL` alongside the other env overrides.

### Part B — CloudflareBypass fixes (remaining Playwright path)

These apply to sources that are `use_playwright: true` **and**
`cloudflare_protected: false`, which still use `CloudflareBypass`.

- **B1 Stale status:** after the post-challenge `networkidle` wait, re-evaluate
  success. If the page title is no longer "just a moment" and the content has no
  challenge markers (`cf-challenge`, `enable javascript and cookies`), treat as
  `status = 200`. Return the refreshed status.
- **B2 Dead fallback:** in `fetch()`, when Playwright fails (incl. 403), try
  cloudscraper **before** sleeping/retrying. Order: Playwright → cloudscraper →
  (if both fail) rotate + retry.
- **B3 Rotation before retry:** before the `retry_delay` sleep on a failed attempt,
  call `rotate_identity()` (new UA + new context) so the retry differs from the
  previous attempt. Keep `retry_delay` configurable; do not lower the default.
- **B4 UA/OS match:** `_get_random_user_agent` (in both `BaseScraper` and
  `CloudflareBypass`) restricts the pool to UAs matching the host platform
  (macOS → UAs containing "Macintosh"). If none match, fall back to a generic
  Chrome UA consistent with the host.

## Data flow (CF-protected source, happy path)

```
round start
  └─ BaseScraper.start()
       └─ FlareSolverrClient.health() → ok
            └─ create_session("bobistheoilguy") → sess_id
  └─ for each listing/item url:
       └─ fetch_page(url)
            └─ FlareSolverrClient.fetch(url, sess_id) → (html, 200)
  └─ BaseScraper.stop()
       └─ destroy_session(sess_id)
```

CF-protected source, FlareSolverr down:
```
round start
  └─ start() → health() == False → log flaresolverr_unavailable, _cf_skip = True
  └─ fetch_page(url) → _cf_skip → return None  (source skipped, no retries)
```

## Error handling

- FlareSolverr HTTP/timeout errors in `fetch()` → return `(None, 0)`; caller treats
  as a failed page (existing behavior), round continues.
- `create_session` failure at round start → treated like unhealthy → `_cf_skip`.
- `destroy_session` failure → logged at warning, non-fatal.
- All FlareSolverr failures are logged with the source name for traceability.

## Testing

TDD: write the failing test before each fix.

- `FlareSolverrClient` — unit tests with `aiohttp` mocked: `create_session`,
  `fetch`, `destroy_session`, `health`; service-down path returns sane values. No
  real FlareSolverr required.
- `BaseScraper` routing — CF-protected source + FlareSolverr down → source skipped,
  no retry attempts (verified via mock). CF-protected source + healthy → fetch goes
  through FlareSolverr, Playwright never initialized.
- B1–B4 — focused unit tests on the affected `CloudflareBypass` methods with mocked
  page/response objects (stale-status promotion, cloudscraper reached on 403,
  rotate before retry, UA pool filtered by platform).

## Out of scope

- Residential/rotating proxies.
- Changing which sources are enabled (handled separately; this design makes the
  CF-protected ones actually fetchable when FlareSolverr is available).
- Any change to dedup / Q/A / storage stages.
