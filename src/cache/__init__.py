"""
Redis cache layer.

Used as a fast first-tier check for:
  - URL hashes already visited (per-source set)
  - Content hashes seen recently (exact-duplicate short-circuit)
  - Generic K/V pieces (last scrape times, lock keys)

PostgreSQL remains the source of truth; Redis hits skip a DB round-trip.
If Redis is unreachable, all helpers degrade gracefully (return None / False).
"""

import os
from typing import Optional

import redis.asyncio as aioredis

from src.utils.logger import get_logger

logger = get_logger("cache")


def _resolve_redis_url(config: dict) -> str:
    env = os.environ.get("REDIS_URL")
    if env:
        return env
    rcfg = config.get("redis", {}) if config else {}
    if rcfg.get("url"):
        return rcfg["url"]
    host = rcfg.get("host", "localhost")
    port = rcfg.get("port", 6379)
    db = rcfg.get("db", 0)
    password = rcfg.get("password")
    auth = f":{password}@" if password else ""
    return f"redis://{auth}{host}:{port}/{db}"


class RedisManager:
    """Async Redis wrapper. All ops are best-effort — None on connection error."""

    def __init__(self, config: Optional[dict] = None, url: Optional[str] = None):
        self.config = config or {}
        self.url = url or _resolve_redis_url(self.config)
        rcfg = self.config.get("redis", {}) if config else {}
        self.url_ttl = int(rcfg.get("url_cache_ttl", 3600))
        self.dup_ttl = int(rcfg.get("duplicate_cache_ttl", 86400))
        self.max_connections = int(rcfg.get("max_connections", 20))
        self._client: Optional[aioredis.Redis] = None
        self._connected = False

    async def initialize(self) -> bool:
        try:
            self._client = aioredis.from_url(
                self.url,
                max_connections=self.max_connections,
                decode_responses=True,
            )
            await self._client.ping()
            self._connected = True
            logger.info("redis_connected", url=self._safe_url())
            return True
        except Exception as e:
            self._connected = False
            logger.warning("redis_unavailable", error=str(e), url=self._safe_url())
            return False

    @property
    def connected(self) -> bool:
        return self._connected and self._client is not None

    def _safe_url(self) -> str:
        if "@" not in self.url:
            return self.url
        return self.url.split("@", 1)[0].rsplit(":", 1)[0] + ":***@" + self.url.split("@", 1)[1]

    async def close(self):
        if self._client:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None
            self._connected = False

    # ------------------------------------------------------------------
    # URL visited cache
    # ------------------------------------------------------------------

    def _url_key(self, url_hash: str) -> str:
        return f"url:visited:{url_hash}"

    async def is_url_visited(self, url_hash: str) -> Optional[bool]:
        if not self.connected:
            return None
        try:
            return bool(await self._client.exists(self._url_key(url_hash)))
        except Exception:
            return None

    async def mark_url_visited(self, url_hash: str, content_hash: str = ""):
        if not self.connected:
            return
        try:
            await self._client.set(
                self._url_key(url_hash), content_hash or "1", ex=self.url_ttl
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Content-hash cache (exact-duplicate short-circuit)
    # ------------------------------------------------------------------

    def _content_key(self, content_hash: str) -> str:
        return f"content:hash:{content_hash}"

    async def get_content_hash_doc(self, content_hash: str) -> Optional[str]:
        if not self.connected:
            return None
        try:
            return await self._client.get(self._content_key(content_hash))
        except Exception:
            return None

    async def set_content_hash_doc(self, content_hash: str, doc_id: str):
        if not self.connected:
            return
        try:
            await self._client.set(
                self._content_key(content_hash), doc_id, ex=self.dup_ttl
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Generic K/V (used for orchestrator state)
    # ------------------------------------------------------------------

    async def get(self, key: str) -> Optional[str]:
        if not self.connected:
            return None
        try:
            return await self._client.get(key)
        except Exception:
            return None

    async def set(self, key: str, value: str, ttl: Optional[int] = None):
        if not self.connected:
            return
        try:
            await self._client.set(key, value, ex=ttl)
        except Exception:
            pass

    async def info(self) -> dict:
        if not self.connected:
            return {"connected": False}
        try:
            info = await self._client.info()
            return {
                "connected": True,
                "version": info.get("redis_version"),
                "used_memory_human": info.get("used_memory_human"),
                "connected_clients": info.get("connected_clients"),
                "keyspace_hits": info.get("keyspace_hits"),
                "keyspace_misses": info.get("keyspace_misses"),
                "db0_keys": info.get("db0", {}).get("keys") if isinstance(info.get("db0"), dict) else None,
            }
        except Exception as e:
            return {"connected": False, "error": str(e)}
