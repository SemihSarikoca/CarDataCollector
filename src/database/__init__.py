"""
Database manager — PostgreSQL via asyncpg.

The pool is shared across all components (storage, dedup, qa_generator,
orchestrator). Connection settings come from DATABASE_URL or the `database`
section of settings.yaml.
"""

import asyncio
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import asyncpg

from src.models import (
    PipelineStage,
    QAPair,
    RoundStats,
    StoredDocument,
)
from src.utils.logger import get_logger

logger = get_logger("database")


def _row_to_dict(row: Optional[asyncpg.Record]) -> Optional[dict]:
    if row is None:
        return None
    out = dict(row)
    # Coerce UUIDs to strings for JSON-friendliness
    for k, v in out.items():
        if hasattr(v, "hex") and not isinstance(v, (bytes, bytearray)):
            try:
                out[k] = str(v)
            except Exception:
                pass
    return out


def _resolve_dsn(config: dict) -> str:
    """Resolve PostgreSQL DSN. Env DATABASE_URL takes precedence."""
    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        return env_url
    db_cfg = config.get("database", {}) if config else {}
    if db_cfg.get("dsn"):
        return db_cfg["dsn"]
    user = db_cfg.get("user", "collector")
    password = db_cfg.get("password", "collector_pass")
    host = db_cfg.get("host", "localhost")
    port = db_cfg.get("port", 5432)
    name = db_cfg.get("name", "car_collector")
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


class DatabaseManager:
    """PostgreSQL connection-pool wrapper used across the pipeline."""

    def __init__(self, config: Optional[dict] = None, dsn: Optional[str] = None):
        self.config = config or {}
        self.dsn = dsn or _resolve_dsn(self.config)
        db_cfg = self.config.get("database", {}) if config else {}
        self._pool_min = int(db_cfg.get("pool_min_size", 2))
        self._pool_max = int(db_cfg.get("pool_size", 10))
        self._pool_timeout = float(db_cfg.get("pool_timeout", 30))
        self._pool: Optional[asyncpg.Pool] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self):
        if self._pool is not None:
            return
        self._pool = await asyncpg.create_pool(
            dsn=self.dsn,
            min_size=self._pool_min,
            max_size=self._pool_max,
            command_timeout=self._pool_timeout,
        )
        await self._ensure_schema()
        logger.info("database_initialized", dsn=self._safe_dsn())

    async def close(self):
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info("database_closed")

    def _safe_dsn(self) -> str:
        # mask password
        if "@" not in self.dsn:
            return self.dsn
        head, tail = self.dsn.split("@", 1)
        if ":" in head and "//" in head:
            scheme_user = head.rsplit(":", 1)[0]
            return f"{scheme_user}:***@{tail}"
        return self.dsn

    async def _ensure_schema(self):
        """Run init-db.sql if the main table is missing.

        Postgres in docker runs init-db.sql automatically on first start; this
        is a safety net for local/dev runs against a pre-existing DB.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT to_regclass('public.scraped_data') AS t
            """)
            if row and row["t"]:
                return

        sql_path = Path(__file__).resolve().parents[2] / "scripts" / "init-db.sql"
        if not sql_path.exists():
            logger.warning("init_sql_missing", path=str(sql_path))
            return

        sql = sql_path.read_text(encoding="utf-8")
        async with self._pool.acquire() as conn:
            await conn.execute(sql)
        logger.info("schema_bootstrapped", path=str(sql_path))

    # ------------------------------------------------------------------
    # Sources (sync from settings.yaml at startup)
    # ------------------------------------------------------------------

    async def upsert_source(self, name: str, source_type: str, base_url: str,
                             enabled: bool = True, priority: int = 5,
                             rate_limit: float = 5.0,
                             scrape_interval_hours: int = 12) -> str:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO sources (name, source_type, base_url, enabled,
                                     priority, rate_limit_seconds, scrape_interval_hours)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (name) DO UPDATE SET
                    source_type = EXCLUDED.source_type,
                    base_url = EXCLUDED.base_url,
                    priority = EXCLUDED.priority,
                    rate_limit_seconds = EXCLUDED.rate_limit_seconds,
                    scrape_interval_hours = EXCLUDED.scrape_interval_hours,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING id
            """, name, source_type, base_url, enabled, priority,
                rate_limit, scrape_interval_hours)
            return str(row["id"])

    async def get_source_id(self, name: str) -> Optional[str]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT id FROM sources WHERE name = $1", name)
            return str(row["id"]) if row else None

    async def get_source_enabled_states(self, names: list) -> dict:
        """Return {name: enabled} from the DB for the given source names."""
        if not names:
            return {}
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT name, enabled FROM sources WHERE name = ANY($1)", names
            )
            return {r["name"]: r["enabled"] for r in rows}

    # ------------------------------------------------------------------
    # Document operations
    # ------------------------------------------------------------------

    async def insert_document(self, doc: StoredDocument) -> Optional[str]:
        """Insert a new scraped_data row. Returns UUID string or None on conflict."""
        category = doc.category.value if hasattr(doc.category, "value") else (doc.category or "article")
        stage = doc.pipeline_stage.value if hasattr(doc.pipeline_stage, "value") else (doc.pipeline_stage or "scraped")

        async with self._pool.acquire() as conn:
            source_id = await conn.fetchval(
                "SELECT id FROM sources WHERE name = $1", doc.source_name
            )
            metadata = doc.metadata_json or "{}"
            try:
                metadata_obj = json.loads(metadata) if isinstance(metadata, str) else metadata
            except Exception:
                metadata_obj = {}

            row = await conn.fetchrow("""
                INSERT INTO scraped_data (
                    source_id, source_name, source_url, page_url, url_hash,
                    content_hash, simhash, title, content_text, content_html,
                    author, category, language, word_count, content_length,
                    file_size_bytes, file_path_html, file_path_pdf,
                    pipeline_stage, is_duplicate, duplicate_of, qa_count,
                    round_number, tags, metadata, date_published, date_scraped
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                        $11, $12, $13, $14, $15, $16, $17, $18, $19, $20,
                        $21, $22, $23, $24, $25::jsonb, $26, $27)
                ON CONFLICT (url_hash) DO NOTHING
                RETURNING id
            """,
                source_id, doc.source_name, doc.source_url or "", doc.page_url,
                doc.url_hash, doc.content_hash, doc.simhash or "",
                doc.title or "", "", None,
                doc.author or "", category, doc.language or "en",
                doc.word_count or 0, doc.content_length or 0,
                doc.file_size_bytes or 0, doc.file_path_html or "",
                doc.file_path_pdf or "", stage, doc.is_duplicate,
                doc.duplicate_of, doc.qa_count or 0, doc.round_number or 0,
                doc.tags or "", json.dumps(metadata_obj),
                doc.date_published, doc.date_scraped,
            )
            return str(row["id"]) if row else None

    async def update_document_stage(self, doc_id: str, stage: PipelineStage):
        stage_val = stage.value if hasattr(stage, "value") else stage
        async with self._pool.acquire() as conn:
            await conn.execute("""
                UPDATE scraped_data SET pipeline_stage = $1, updated_at = CURRENT_TIMESTAMP
                WHERE id = $2
            """, stage_val, doc_id)

    async def mark_duplicate(self, doc_id: str, duplicate_of: str):
        async with self._pool.acquire() as conn:
            await conn.execute("""
                UPDATE scraped_data SET is_duplicate = true, duplicate_of = $1,
                pipeline_stage = 'deduplicated', updated_at = CURRENT_TIMESTAMP
                WHERE id = $2
            """, duplicate_of, doc_id)

    async def get_document_by_url_hash(self, url_hash: str) -> Optional[dict]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM scraped_data WHERE url_hash = $1", url_hash
            )
            return _row_to_dict(row)

    async def get_document_by_content_hash(self, content_hash: str) -> Optional[dict]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM scraped_data
                WHERE content_hash = $1 AND is_duplicate = false
                LIMIT 1
            """, content_hash)
            return _row_to_dict(row)

    async def get_documents_for_qa(self, limit: int = 100) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM scraped_data
                WHERE pipeline_stage IN ('deduplicated','quality_checked','stored')
                  AND is_duplicate = false AND qa_count = 0
                  AND word_count >= 20 AND content_length >= 100
                ORDER BY quality_score DESC NULLS LAST, content_length DESC
                LIMIT $1
            """, limit)
            return [_row_to_dict(r) for r in rows]

    async def get_non_duplicate_documents(self, batch_size: int = 1000,
                                           offset: int = 0,
                                           since: Optional[datetime] = None) -> list[dict]:
        async with self._pool.acquire() as conn:
            if since is not None:
                rows = await conn.fetch("""
                    SELECT id, content_hash, simhash, created_at
                    FROM scraped_data
                    WHERE is_duplicate = false AND created_at > $3
                    ORDER BY created_at
                    LIMIT $1 OFFSET $2
                """, batch_size, offset, since)
            else:
                rows = await conn.fetch("""
                    SELECT id, content_hash, simhash, created_at
                    FROM scraped_data
                    WHERE is_duplicate = false
                    ORDER BY created_at
                    LIMIT $1 OFFSET $2
                """, batch_size, offset)
            return [_row_to_dict(r) for r in rows]

    async def get_total_documents(self) -> int:
        async with self._pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM scraped_data") or 0

    async def get_total_unique_documents(self) -> int:
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM scraped_data WHERE is_duplicate = false"
            ) or 0

    # ------------------------------------------------------------------
    # Q/A operations
    # ------------------------------------------------------------------

    async def insert_qa_pairs(self, pairs: list[QAPair]) -> int:
        if not pairs:
            return 0

        inserted = 0
        document_id = pairs[0].document_id

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                for pair in pairs:
                    try:
                        metadata_obj = json.loads(pair.metadata_json) \
                            if isinstance(pair.metadata_json, str) \
                            else (pair.metadata_json or {})
                    except Exception:
                        metadata_obj = {}

                    res = await conn.execute("""
                        INSERT INTO qa_pairs (
                            document_id, source_name, question, answer, qa_hash,
                            category, car_brand, car_model, car_year,
                            quality_score, llm_model, is_verified, metadata
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13::jsonb)
                        ON CONFLICT (qa_hash) DO NOTHING
                    """,
                        pair.document_id, pair.source_name,
                        pair.question, pair.answer, pair.qa_hash,
                        pair.category or "", pair.car_brand or "",
                        pair.car_model or "", pair.car_year or "",
                        float(pair.quality_score or 0), pair.model_used or "",
                        bool(pair.is_verified), json.dumps(metadata_obj),
                    )
                    # asyncpg returns "INSERT 0 1" on success, "INSERT 0 0" on conflict
                    if res.endswith(" 1"):
                        inserted += 1

                if inserted:
                    await conn.execute("""
                        UPDATE scraped_data SET
                            qa_count = qa_count + $1,
                            pipeline_stage = 'qa_generated',
                            date_processed = CURRENT_TIMESTAMP,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = $2
                    """, inserted, document_id)

        return inserted

    async def get_total_qa_pairs(self) -> int:
        async with self._pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM qa_pairs") or 0

    async def export_qa_pairs(self, limit: int = 0, offset: int = 0) -> list[dict]:
        async with self._pool.acquire() as conn:
            if limit > 0:
                rows = await conn.fetch("""
                    SELECT * FROM qa_pairs ORDER BY date_generated LIMIT $1 OFFSET $2
                """, limit, offset)
            else:
                rows = await conn.fetch("""
                    SELECT * FROM qa_pairs ORDER BY date_generated
                """)
            return [_row_to_dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Visited URLs
    # ------------------------------------------------------------------

    async def is_url_visited(self, url_hash: str) -> bool:
        async with self._pool.acquire() as conn:
            return bool(await conn.fetchval(
                "SELECT 1 FROM urls_visited WHERE url_hash = $1", url_hash
            ))

    async def mark_url_visited(self, url_hash: str, url: str, source_name: str,
                                content_hash: str = ""):
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO urls_visited (url_hash, url, source_name, last_content_hash)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (url_hash) DO UPDATE SET
                    last_visited_at = CURRENT_TIMESTAMP,
                    visit_count = urls_visited.visit_count + 1,
                    has_changed = (urls_visited.last_content_hash IS DISTINCT FROM EXCLUDED.last_content_hash),
                    last_content_hash = EXCLUDED.last_content_hash
            """, url_hash, url, source_name, content_hash)

    async def get_changed_urls(self, source_name: str) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM urls_visited
                WHERE source_name = $1 AND has_changed = true
            """, source_name)
            return [_row_to_dict(r) for r in rows]

    # ------------------------------------------------------------------
    # SimHash index
    # ------------------------------------------------------------------

    async def insert_simhash(self, document_id: str, simhash_value: str,
                              buckets: tuple[str, str, str, str]):
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO simhash_index (document_id, simhash_value,
                                           bucket_0, bucket_1, bucket_2, bucket_3)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (document_id) DO UPDATE SET
                    simhash_value = EXCLUDED.simhash_value,
                    bucket_0 = EXCLUDED.bucket_0,
                    bucket_1 = EXCLUDED.bucket_1,
                    bucket_2 = EXCLUDED.bucket_2,
                    bucket_3 = EXCLUDED.bucket_3
            """, document_id, simhash_value, buckets[0], buckets[1],
                buckets[2], buckets[3])

    async def find_similar_simhashes(self, buckets: tuple[str, str, str, str]) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT DISTINCT document_id, simhash_value
                FROM simhash_index
                WHERE bucket_0 = $1 OR bucket_1 = $2 OR bucket_2 = $3 OR bucket_3 = $4
                LIMIT 200
            """, buckets[0], buckets[1], buckets[2], buckets[3])
            return [_row_to_dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Round summaries
    # ------------------------------------------------------------------

    async def cleanup_stale_rounds(self) -> int:
        """Mark all 'running' rounds as 'interrupted' — called at startup to fix rows
        left open by a previous process that was killed before complete_round()."""
        async with self._pool.acquire() as conn:
            res = await conn.execute("""
                UPDATE rounds
                SET status = 'interrupted', end_time = CURRENT_TIMESTAMP
                WHERE status = 'running'
            """)
            try:
                return int(res.split()[-1])
            except Exception:
                return 0

    async def start_round(self, round_number: int) -> int:
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO rounds (round_number, start_time, status)
                VALUES ($1, CURRENT_TIMESTAMP, 'running')
                ON CONFLICT (round_number) DO UPDATE SET
                    start_time = CURRENT_TIMESTAMP,
                    status = 'running'
            """, round_number)
            return round_number

    async def update_round_stats(self, round_number: int, stats: dict):
        if not stats:
            return
        cols = list(stats.keys())
        set_clauses = ", ".join(f"{c} = ${i+1}" for i, c in enumerate(cols))
        values = list(stats.values()) + [round_number]
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"UPDATE rounds SET {set_clauses} WHERE round_number = ${len(values)}",
                *values,
            )

    async def complete_round(self, round_number: int, stats: RoundStats):
        async with self._pool.acquire() as conn:
            await conn.execute("""
                UPDATE rounds SET
                    end_time = CURRENT_TIMESTAMP,
                    total_urls_visited = $1,
                    total_items_scraped = $2,
                    total_items_stored = $3,
                    total_duplicates_found = $4,
                    total_qa_generated = $5,
                    errors_count = $6,
                    sources_completed = $7::jsonb,
                    status = 'completed'
                WHERE round_number = $8
            """,
                stats.total_urls_visited, stats.total_items_scraped,
                stats.total_items_stored, stats.total_duplicates_found,
                stats.total_qa_generated, stats.errors_count,
                json.dumps(stats.sources_completed), round_number,
            )

    async def get_last_round_number(self) -> int:
        async with self._pool.acquire() as conn:
            val = await conn.fetchval("SELECT MAX(round_number) FROM rounds")
            return val or 0

    # ------------------------------------------------------------------
    # Scrape logs
    # ------------------------------------------------------------------

    async def get_scrape_schedule(self, source_names: list) -> dict:
        """Return {source_name: (last_attempt, last_success)} for the given sources.

        last_attempt — MAX(started_at) over all attempts.
        last_success — MAX(started_at) over attempts that actually fetched items
                       (items_scraped > 0), i.e. the site was reached successfully.

        The pipeline throttles a source by its full interval only after it was
        reached successfully (even if everything turned out to be a duplicate);
        attempts that fetched nothing (fetch failure / block) get a much shorter
        retry window so a broken source cannot stay dark for days.
        """
        if not source_names:
            return {}
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT source_name,
                       MAX(started_at) AS last_attempt,
                       MAX(started_at) FILTER (WHERE items_scraped > 0) AS last_success
                FROM scrape_logs
                WHERE source_name = ANY($1)
                GROUP BY source_name
            """, source_names)
            return {
                row["source_name"]: (row["last_attempt"], row["last_success"])
                for row in rows
            }

    async def log_scrape_start(self, source_name: str, round_number: int) -> str:
        """Insert a running scrape_logs row; returns the log UUID."""
        async with self._pool.acquire() as conn:
            source_id = await conn.fetchval(
                "SELECT id FROM sources WHERE name = $1", source_name
            )
            log_id = await conn.fetchval("""
                INSERT INTO scrape_logs (source_id, source_name, round_number, status)
                VALUES ($1, $2, $3, 'running')
                RETURNING id
            """, source_id, source_name, round_number)
            return str(log_id)

    async def log_scrape_complete(self, log_id: str, stats: dict):
        """Finalise a scrape_logs row with results."""
        status = (
            "failed"
            if stats.get("errors", 0) > 0 and stats.get("items", 0) == 0
            else "success"
        )
        async with self._pool.acquire() as conn:
            await conn.execute("""
                UPDATE scrape_logs SET
                    finished_at = CURRENT_TIMESTAMP,
                    status = $2,
                    items_scraped = $3,
                    items_stored = $4,
                    errors_count = $5,
                    duration_seconds = EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - started_at))
                WHERE id = $1
            """, uuid.UUID(log_id), status,
                stats.get("items", 0), stats.get("stored", 0), stats.get("errors", 0))

    # ------------------------------------------------------------------
    # Aggregate stats
    # ------------------------------------------------------------------

    async def get_stats(self) -> dict:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT
                    (SELECT COUNT(*) FROM scraped_data) AS total_documents,
                    (SELECT COUNT(*) FROM scraped_data WHERE is_duplicate = false) AS unique_documents,
                    (SELECT COUNT(*) FROM scraped_data WHERE is_duplicate = true) AS duplicate_documents,
                    (SELECT COUNT(*) FROM qa_pairs) AS total_qa_pairs,
                    (SELECT COUNT(*) FROM urls_visited) AS total_urls_visited,
                    (SELECT COUNT(DISTINCT source_name) FROM scraped_data) AS active_sources
            """)
            stats = dict(row) if row else {}

            per_source_rows = await conn.fetch("""
                SELECT source_name,
                       COUNT(*) AS total,
                       SUM(CASE WHEN is_duplicate = false THEN 1 ELSE 0 END) AS unique,
                       SUM(qa_count) AS qa_pairs
                FROM scraped_data
                GROUP BY source_name
            """)

        stats["per_source"] = {
            r["source_name"]: {
                "total": int(r["total"] or 0),
                "unique": int(r["unique"] or 0),
                "qa_pairs": int(r["qa_pairs"] or 0),
            }
            for r in per_source_rows
        }
        return stats

    # ------------------------------------------------------------------
    # Generic helpers (used by dashboard / CLI scripts)
    # ------------------------------------------------------------------

    async def execute(self, query: str, *args) -> str:
        """Run a DML statement (INSERT / UPDATE / DELETE). Returns asyncpg status string."""
        async with self._pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def fetch(self, query: str, *args) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *args)
            return [_row_to_dict(r) for r in rows]

    async def fetchrow(self, query: str, *args) -> Optional[dict]:
        async with self._pool.acquire() as conn:
            return _row_to_dict(await conn.fetchrow(query, *args))

    async def fetchval(self, query: str, *args) -> Any:
        async with self._pool.acquire() as conn:
            return await conn.fetchval(query, *args)

    async def update_document_content(self, doc_id: str, fields: dict) -> None:
        """Update mutable content fields on a scraped_data row (content changed on re-scrape)."""
        allowed = {
            "content_hash", "content_text", "content_html", "word_count",
            "content_length", "file_path_html", "file_size_bytes",
            "round_number", "pipeline_stage", "simhash",
        }
        data = {k: v for k, v in fields.items() if k in allowed}
        if not data:
            return
        keys = list(data.keys())
        vals = list(data.values())
        set_parts = [f"{k} = ${i + 1}" for i, k in enumerate(keys)]
        sql = f"""
            UPDATE scraped_data
            SET {", ".join(set_parts)}, updated_at = CURRENT_TIMESTAMP
            WHERE id = ${len(keys) + 1}
        """
        async with self._pool.acquire() as conn:
            await conn.execute(sql, *vals, doc_id)

    async def delete_qa_for_document(self, doc_id: str) -> int:
        """Delete all Q/A pairs for a document and reset its qa_count. Returns count deleted."""
        async with self._pool.acquire() as conn:
            res = await conn.execute("DELETE FROM qa_pairs WHERE document_id = $1", doc_id)
            try:
                count = int(res.split()[-1])
            except Exception:
                count = 0
            await conn.execute(
                "UPDATE scraped_data SET qa_count = 0 WHERE id = $1", doc_id
            )
            return count
