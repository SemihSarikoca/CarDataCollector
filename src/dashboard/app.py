"""
Flask Dashboard for Car Data Collector — backed by PostgreSQL + Redis.

Flask is sync, so we use psycopg2 (sync driver) for DB queries and the sync
redis client. The pipeline owns writes; the dashboard is read-only by default,
except for the manual Q/A trigger which spins up its own asyncpg pool in a
worker thread.
"""

from __future__ import annotations

import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx
import psycopg2
import psycopg2.extras
import redis
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

from src.utils.helpers import get_disk_usage, load_config
from src.utils.logger import get_logger

logger = get_logger("dashboard")

app = Flask(__name__)
CORS(app)

_state: dict = {
    "config": None,
    "dsn": None,
    "redis_url": None,
    "ollama_url": "http://localhost:11434",
    "base_path": "./data",
    "pool": None,         # psycopg2 connection pool
    "redis_client": None,
}


# =============================================================================
# Configuration
# =============================================================================

def configure(config: dict):
    from psycopg2.pool import ThreadedConnectionPool

    _state["config"] = config

    # DSN resolution (mirrors src.database)
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        d = config.get("database", {})
        dsn = (f"host={d.get('host', 'localhost')} port={d.get('port', 5432)} "
               f"dbname={d.get('name', 'car_collector')} "
               f"user={d.get('user', 'collector')} "
               f"password={d.get('password', 'collector_pass')}")
    _state["dsn"] = dsn

    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        r = config.get("redis", {})
        redis_url = f"redis://{r.get('host', 'localhost')}:{r.get('port', 6379)}/{r.get('db', 0)}"
    _state["redis_url"] = redis_url

    _state["ollama_url"] = config.get("qa_generator", {}).get(
        "ollama_url", "http://localhost:11434"
    )
    _state["base_path"] = config.get("storage", {}).get("base_path", "./data")

    # Connection pool (lazy — will surface errors on first request)
    try:
        _state["pool"] = ThreadedConnectionPool(1, 8, dsn=dsn)
        logger.info("dashboard_pg_pool_ready")
    except Exception as e:
        logger.error("dashboard_pg_pool_failed", error=str(e))
        _state["pool"] = None

    try:
        _state["redis_client"] = redis.Redis.from_url(redis_url, decode_responses=True,
                                                       socket_timeout=2)
        _state["redis_client"].ping()
        logger.info("dashboard_redis_ready")
    except Exception as e:
        logger.warning("dashboard_redis_unavailable", error=str(e))
        _state["redis_client"] = None


class _PGConn:
    """Context manager that pulls a connection from the pool and returns it."""

    def __enter__(self):
        pool = _state["pool"]
        if pool is None:
            raise RuntimeError("Database pool not initialised")
        self.conn = pool.getconn()
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        pool = _state["pool"]
        if pool and self.conn:
            try:
                if exc_type is not None:
                    self.conn.rollback()
                pool.putconn(self.conn)
            except Exception:
                pass


def _query(sql: str, params=None, one: bool = False):
    with _PGConn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or ())
            if cur.description is None:
                return None
            rows = cur.fetchall()
            for r in rows:
                for k, v in list(r.items()):
                    if hasattr(v, "hex") and not isinstance(v, (bytes, bytearray)):
                        try:
                            r[k] = str(v)
                        except Exception:
                            pass
            return rows[0] if one else rows


# =============================================================================
# Pages
# =============================================================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    db_ok = False
    redis_ok = False
    try:
        _query("SELECT 1", one=True)
        db_ok = True
    except Exception:
        pass
    if _state["redis_client"]:
        try:
            redis_ok = bool(_state["redis_client"].ping())
        except Exception:
            redis_ok = False
    return jsonify({
        "status": "healthy" if db_ok else "degraded",
        "database": db_ok,
        "redis": redis_ok,
        "timestamp": datetime.utcnow().isoformat(),
    })


# =============================================================================
# Stats
# =============================================================================

@app.route("/api/stats")
def get_stats():
    try:
        row = _query("""
            SELECT
                (SELECT COUNT(*) FROM scraped_data) AS total_documents,
                (SELECT COUNT(*) FROM scraped_data WHERE is_duplicate = false) AS unique_documents,
                (SELECT COUNT(*) FROM scraped_data WHERE is_duplicate = true) AS duplicate_documents,
                (SELECT COUNT(*) FROM qa_pairs) AS total_qa_pairs,
                (SELECT COUNT(*) FROM urls_visited) AS total_urls_visited,
                (SELECT COUNT(DISTINCT source_name) FROM scraped_data) AS active_sources,
                (SELECT MAX(date_scraped) FROM scraped_data) AS last_scrape,
                (SELECT MAX(round_number) FROM rounds) AS last_round,
                (SELECT COUNT(*) FROM scraped_data
                   WHERE is_duplicate = false AND qa_count = 0) AS pending_qa,
                (SELECT COUNT(*) FROM rounds WHERE status = 'running') AS running_rounds
        """, one=True) or {}

        configured = _state["config"].get("sources", []) if _state["config"] else []
        disk = get_disk_usage(_state["base_path"])

        return jsonify({
            "total_documents": row.get("total_documents") or 0,
            "unique_documents": row.get("unique_documents") or 0,
            "duplicate_documents": row.get("duplicate_documents") or 0,
            "total_qa_pairs": row.get("total_qa_pairs") or 0,
            "total_urls_visited": row.get("total_urls_visited") or 0,
            "active_sources": row.get("active_sources") or 0,
            "total_sources": len(configured),
            "pending_qa_documents": row.get("pending_qa") or 0,
            "last_scrape": row.get("last_scrape").isoformat() if row.get("last_scrape") else None,
            "last_round": row.get("last_round") or 0,
            "scraping_status": "running" if (row.get("running_rounds") or 0) > 0 else "idle",
            "disk_usage_percent": disk.get("percent", 0),
            "disk_free_gb": round(disk.get("free_gb", 0), 2),
            "disk_total_gb": round(disk.get("total_gb", 0), 2),
        })
    except Exception as e:
        logger.error("stats_error", error=str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/api/sources")
def get_sources():
    try:
        rows = _query("""
            SELECT s.name, s.source_type, s.base_url, s.enabled, s.priority,
                   s.scrape_interval_hours, s.last_scrape_at,
                   COALESCE(stats.total, 0)        AS total_items,
                   COALESCE(stats.unique_count, 0) AS unique_items,
                   COALESCE(stats.qa_total, 0)     AS qa_pairs,
                   stats.last_scrape
            FROM sources s
            LEFT JOIN (
                SELECT source_name,
                       COUNT(*) AS total,
                       SUM(CASE WHEN is_duplicate = false THEN 1 ELSE 0 END) AS unique_count,
                       SUM(qa_count) AS qa_total,
                       MAX(date_scraped) AS last_scrape
                FROM scraped_data
                GROUP BY source_name
            ) stats ON stats.source_name = s.name
            ORDER BY s.priority, s.name
        """) or []

        configured_names = {s.get("name") for s in (_state["config"].get("sources", []) if _state["config"] else [])}
        out = []
        for r in rows:
            out.append({
                "name": r["name"],
                "type": r["source_type"],
                "base_url": r["base_url"],
                "enabled": r["enabled"],
                "priority": r["priority"],
                "scrape_interval_hours": r["scrape_interval_hours"],
                "status": "active" if r["enabled"] else "idle",
                "in_config": r["name"] in configured_names,
                "total_items": int(r["total_items"] or 0),
                "unique_items": int(r["unique_items"] or 0),
                "qa_pairs": int(r["qa_pairs"] or 0),
                "last_scrape": r["last_scrape"].isoformat() if r["last_scrape"] else None,
            })
        return jsonify(out)
    except Exception as e:
        logger.error("sources_error", error=str(e))
        return jsonify({"error": str(e)}), 500


# =============================================================================
# Documents
# =============================================================================

@app.route("/api/data")
def get_data():
    try:
        page = max(1, request.args.get("page", 1, type=int))
        per_page = min(200, max(1, request.args.get("per_page", 25, type=int)))
        source = request.args.get("source") or None
        category = request.args.get("category") or None
        search = request.args.get("search") or None
        only_unique = request.args.get("only_unique", "0") == "1"
        has_qa = request.args.get("has_qa")

        where = []
        params: list = []
        if source:
            where.append("source_name = %s"); params.append(source)
        if category:
            where.append("category = %s"); params.append(category)
        if search:
            where.append("(title ILIKE %s OR page_url ILIKE %s OR content_text ILIKE %s)")
            like = f"%{search}%"; params.extend([like, like, like])
        if only_unique:
            where.append("is_duplicate = false")
        if has_qa == "1":
            where.append("qa_count > 0")
        elif has_qa == "0":
            where.append("qa_count = 0")
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        total = (_query(f"SELECT COUNT(*) AS n FROM scraped_data {where_sql}",
                        params, one=True) or {}).get("n", 0)

        offset = (page - 1) * per_page
        items = _query(f"""
            SELECT id, source_name, page_url, title, category, author,
                   content_length, file_size_bytes, is_duplicate, qa_count,
                   pipeline_stage, round_number, date_scraped, date_published,
                   quality_score, word_count
            FROM scraped_data
            {where_sql}
            ORDER BY date_scraped DESC NULLS LAST
            LIMIT %s OFFSET %s
        """, params + [per_page, offset]) or []

        for it in items:
            for k in ("date_scraped", "date_published"):
                if it.get(k):
                    it[k] = it[k].isoformat()

        return jsonify({
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page if per_page else 0,
        })
    except Exception as e:
        logger.error("data_error", error=str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/api/data/<doc_id>")
def get_data_item(doc_id: str):
    try:
        item = _query("SELECT * FROM scraped_data WHERE id = %s", (doc_id,), one=True)
        if not item:
            return jsonify({"error": "Not found"}), 404

        for k in ("date_scraped", "date_published", "date_processed",
                  "created_at", "updated_at"):
            if item.get(k):
                item[k] = item[k].isoformat()

        # Populate content_text from disk if column is empty
        if not item.get("content_text") and item.get("file_path_html"):
            path = item["file_path_html"]
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        html = f.read()
                    soup = BeautifulSoup(html, "lxml")
                    div = soup.select_one("div.content")
                    text = (div.get_text(separator="\n") if div
                            else soup.get_text(separator="\n"))
                    item["content_text"] = re.sub(r"\n{3,}", "\n\n", text).strip()
                except Exception as e:
                    logger.warning("read_html_failed", path=path, error=str(e))

        # Q/A pairs for this document
        qa = _query("""
            SELECT id, question, answer, car_brand, car_model, car_year,
                   quality_score, llm_model, date_generated
            FROM qa_pairs WHERE document_id = %s
            ORDER BY date_generated
        """, (doc_id,)) or []
        for q in qa:
            if q.get("date_generated"):
                q["date_generated"] = q["date_generated"].isoformat()
        item["qa_pairs"] = qa

        if not item.get("word_count") and item.get("content_text"):
            item["word_count"] = len(item["content_text"].split())

        return jsonify(item)
    except Exception as e:
        logger.error("data_item_error", error=str(e))
        return jsonify({"error": str(e)}), 500


# =============================================================================
# Q/A
# =============================================================================

@app.route("/api/qa")
def list_qa():
    try:
        page = max(1, request.args.get("page", 1, type=int))
        per_page = min(200, max(1, request.args.get("per_page", 25, type=int)))
        source = request.args.get("source") or None
        brand = request.args.get("brand") or None
        search = request.args.get("search") or None

        where = []
        params: list = []
        if source:
            where.append("source_name = %s"); params.append(source)
        if brand:
            where.append("car_brand = %s"); params.append(brand)
        if search:
            where.append("(question ILIKE %s OR answer ILIKE %s)")
            like = f"%{search}%"; params.extend([like, like])
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        total = (_query(f"SELECT COUNT(*) AS n FROM qa_pairs {where_sql}",
                        params, one=True) or {}).get("n", 0)
        offset = (page - 1) * per_page
        items = _query(f"""
            SELECT id, document_id, source_name, question, answer,
                   car_brand, car_model, car_year, quality_score,
                   llm_model, date_generated
            FROM qa_pairs
            {where_sql}
            ORDER BY date_generated DESC
            LIMIT %s OFFSET %s
        """, params + [per_page, offset]) or []
        for it in items:
            if it.get("date_generated"):
                it["date_generated"] = it["date_generated"].isoformat()

        return jsonify({
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page if per_page else 0,
        })
    except Exception as e:
        logger.error("qa_error", error=str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/api/qa/stats")
def qa_stats():
    try:
        total = (_query("SELECT COUNT(*) AS n FROM qa_pairs", one=True) or {}).get("n", 0)
        per_source = _query("""
            SELECT source_name, COUNT(*) AS n
            FROM qa_pairs GROUP BY source_name ORDER BY n DESC
        """) or []
        per_brand = _query("""
            SELECT COALESCE(NULLIF(car_brand, ''), '(unknown)') AS brand, COUNT(*) AS n
            FROM qa_pairs GROUP BY brand ORDER BY n DESC LIMIT 15
        """) or []
        per_model = _query("""
            SELECT COALESCE(NULLIF(llm_model, ''), '(unknown)') AS model, COUNT(*) AS n
            FROM qa_pairs GROUP BY model ORDER BY n DESC
        """) or []
        avg = _query("""
            SELECT AVG(LENGTH(question))::int AS aq,
                   AVG(LENGTH(answer))::int   AS aa,
                   AVG(quality_score)::float  AS qs
            FROM qa_pairs
        """, one=True) or {}
        pending = (_query("""
            SELECT COUNT(*) AS n FROM scraped_data
            WHERE is_duplicate = false AND qa_count = 0
        """, one=True) or {}).get("n", 0)

        return jsonify({
            "total": total,
            "pending_documents": pending,
            "avg_question_length": avg.get("aq") or 0,
            "avg_answer_length": avg.get("aa") or 0,
            "avg_quality_score": round(avg.get("qs") or 0, 3),
            "per_source": per_source,
            "per_brand": per_brand,
            "per_llm_model": per_model,
        })
    except Exception as e:
        logger.error("qa_stats_error", error=str(e))
        return jsonify({"error": str(e)}), 500


# =============================================================================
# Trends, distribution, rounds
# =============================================================================

@app.route("/api/trends")
def trends():
    try:
        days = max(1, min(60, request.args.get("days", 7, type=int)))
        today = datetime.utcnow().date()
        labels = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
        scraped = {d: 0 for d in labels}
        dups = {d: 0 for d in labels}
        qa = {d: 0 for d in labels}

        rows = _query(f"""
            SELECT DATE(date_scraped) AS d,
                   SUM(CASE WHEN is_duplicate = false THEN 1 ELSE 0 END) AS scraped,
                   SUM(CASE WHEN is_duplicate = true  THEN 1 ELSE 0 END) AS dups
            FROM scraped_data
            WHERE date_scraped >= NOW() - INTERVAL '{days} days'
            GROUP BY d
        """) or []
        for r in rows:
            d = r["d"].isoformat() if r.get("d") else None
            if d in scraped:
                scraped[d] = int(r["scraped"] or 0)
                dups[d] = int(r["dups"] or 0)

        rows = _query(f"""
            SELECT DATE(date_generated) AS d, COUNT(*) AS n
            FROM qa_pairs
            WHERE date_generated >= NOW() - INTERVAL '{days} days'
            GROUP BY d
        """) or []
        for r in rows:
            d = r["d"].isoformat() if r.get("d") else None
            if d in qa:
                qa[d] = int(r["n"] or 0)

        return jsonify({
            "labels": labels,
            "scraped": [scraped[d] for d in labels],
            "duplicates": [dups[d] for d in labels],
            "qa_generated": [qa[d] for d in labels],
        })
    except Exception as e:
        logger.error("trends_error", error=str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/api/source-distribution")
def source_distribution():
    try:
        rows = _query("""
            SELECT source_name, COUNT(*) AS n
            FROM scraped_data
            WHERE is_duplicate = false
            GROUP BY source_name ORDER BY n DESC
        """) or []
        return jsonify({
            "labels": [r["source_name"] for r in rows],
            "values": [int(r["n"]) for r in rows],
        })
    except Exception as e:
        logger.error("distribution_error", error=str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/api/rounds")
def rounds_list():
    try:
        limit = min(50, max(1, request.args.get("limit", 10, type=int)))
        rows = _query("""
            SELECT * FROM rounds ORDER BY round_number DESC LIMIT %s
        """, (limit,)) or []
        for r in rows:
            for k in ("start_time", "end_time"):
                if r.get(k):
                    r[k] = r[k].isoformat()
        return jsonify(rows)
    except Exception as e:
        logger.error("rounds_error", error=str(e))
        return jsonify({"error": str(e)}), 500


# =============================================================================
# System status (Postgres / Redis / Ollama)
# =============================================================================

@app.route("/api/system/status")
def system_status():
    out = {"postgres": False, "redis": False, "ollama": False, "details": {}}
    try:
        size = _query(
            "SELECT pg_size_pretty(pg_database_size(current_database())) AS s",
            one=True,
        )
        out["postgres"] = True
        out["details"]["pg_size"] = (size or {}).get("s")
    except Exception as e:
        out["details"]["postgres_error"] = str(e)

    rc = _state["redis_client"]
    if rc:
        try:
            info = rc.info()
            out["redis"] = True
            out["details"]["redis_version"] = info.get("redis_version")
            out["details"]["redis_memory"] = info.get("used_memory_human")
            out["details"]["redis_clients"] = info.get("connected_clients")
        except Exception as e:
            out["details"]["redis_error"] = str(e)

    try:
        r = httpx.get(f"{_state['ollama_url']}/api/tags", timeout=4)
        if r.status_code == 200:
            data = r.json()
            out["ollama"] = True
            out["details"]["ollama_models"] = [m["name"] for m in data.get("models", [])]
            out["details"]["ollama_url"] = _state["ollama_url"]
    except Exception as e:
        out["details"]["ollama_error"] = str(e)
        out["details"]["ollama_url"] = _state["ollama_url"]

    return jsonify(out)


# =============================================================================
# Manual Q/A generation trigger
# =============================================================================

_qa_lock = threading.Lock()
_qa_state = {"running": False, "started_at": None, "last_result": None}


@app.route("/api/qa/generate", methods=["POST"])
def trigger_qa():
    with _qa_lock:
        if _qa_state["running"]:
            return jsonify({
                "ok": False,
                "error": "A Q/A generation job is already running",
                "started_at": _qa_state["started_at"],
            }), 409

    payload = request.get_json(silent=True) or {}
    batch_size = max(1, min(50, int(payload.get("batch_size", 5))))

    config = _state["config"]
    if not config:
        return jsonify({"ok": False, "error": "Dashboard not configured"}), 500

    def worker(cfg: dict, size: int):
        import asyncio
        from src.cache import RedisManager
        from src.database import DatabaseManager
        from src.dedup import DeduplicationEngine
        from src.qa_generator import QAGenerator
        from src.storage import StorageManager

        async def _run():
            cfg2 = dict(cfg)
            cfg2["qa_generator"] = dict(cfg2.get("qa_generator", {}))
            cfg2["qa_generator"]["batch_size"] = size

            db = DatabaseManager(cfg2)
            await db.initialize()
            cache = RedisManager(cfg2)
            await cache.initialize()
            dedup = DeduplicationEngine(db, cfg2, cache=cache)
            storage = StorageManager(cfg2, db, dedup)
            await storage.initialize()
            qa = QAGenerator(cfg2, db, storage)
            try:
                return await qa.process_batch()
            finally:
                await qa.close()
                await cache.close()
                await db.close()

        try:
            result = asyncio.run(_run())
            with _qa_lock:
                _qa_state["last_result"] = {
                    "ok": True,
                    "stats": result,
                    "finished_at": datetime.utcnow().isoformat(),
                }
        except Exception as e:
            logger.error("qa_job_failed", error=str(e))
            with _qa_lock:
                _qa_state["last_result"] = {
                    "ok": False,
                    "error": str(e),
                    "finished_at": datetime.utcnow().isoformat(),
                }
        finally:
            with _qa_lock:
                _qa_state["running"] = False

    with _qa_lock:
        _qa_state["running"] = True
        _qa_state["started_at"] = datetime.utcnow().isoformat()
        _qa_state["last_result"] = None

    threading.Thread(target=worker, args=(config, batch_size), daemon=True).start()
    return jsonify({
        "ok": True,
        "message": f"Q/A generation started (batch_size={batch_size})",
        "started_at": _qa_state["started_at"],
    })


@app.route("/api/qa/generate/status")
def qa_job_status():
    with _qa_lock:
        return jsonify({
            "running": _qa_state["running"],
            "started_at": _qa_state["started_at"],
            "last_result": _qa_state["last_result"],
        })


# =============================================================================
# Errors & runner
# =============================================================================

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "Internal server error"}), 500


def run_dashboard(config: Optional[dict] = None, host: str = "0.0.0.0",
                  port: int = 5000, debug: bool = False):
    if config is None:
        config = load_config("config/settings.yaml")
    configure(config)
    logger.info("dashboard_starting", host=host, port=port)
    app.run(host=host, port=port, debug=debug, threaded=True)


if __name__ == "__main__":
    run_dashboard(debug=True)
