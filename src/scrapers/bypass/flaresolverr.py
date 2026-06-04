"""
FlareSolverr client.

Talks to a FlareSolverr service (https://github.com/FlareSolverr/FlareSolverr)
to fetch pages behind Cloudflare. FlareSolverr runs its own browser and returns
solved HTML, so we do not start Playwright for these sources.

Endpoint resolution order: FLARESOLVERR_URL env var > config.flaresolverr.url >
http://localhost:8191.
"""

import asyncio
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
        self._session_lock: asyncio.Lock = asyncio.Lock()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        # Fast path: external session is always valid; own healthy session is reused.
        if self._external_session is not None:
            return self._external_session
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._session_lock:
            # Re-check inside the lock; another coroutine may have created it already.
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
        return self._session

    async def _command(self, payload: dict) -> dict:
        """POST a command to /v1 and return the parsed JSON."""
        session = await self._ensure_session()
        # +10s slack so our HTTP read outlives FlareSolverr's own maxTimeout.
        timeout = aiohttp.ClientTimeout(total=self.max_timeout_ms / 1000 + 10)
        async with session.post(f"{self.url}/v1", json=payload, timeout=timeout) as resp:
            if resp.status >= 400:
                logger.warning("flaresolverr_http_status", status=resp.status,
                               url=str(resp.url))
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
