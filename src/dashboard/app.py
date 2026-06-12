"""
Flask Dashboard for Car Data Collector — backed by PostgreSQL + Redis.

Flask is sync, so we use psycopg2 (sync driver) for DB queries and the sync
redis client. The pipeline owns writes; the dashboard is read-only by default,
except for the manual Q/A trigger which spins up its own asyncpg pool in a
worker thread.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import secrets

import httpx
import psycopg2
import psycopg2.extras
import redis
from bs4 import BeautifulSoup
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
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

    _state["ollama_url"] = (
        os.environ.get("OLLAMA_URL")
        or config.get("qa_generator", {}).get("ollama_url", "http://localhost:11434")
    )
    _state["base_path"] = config.get("storage", {}).get("base_path", "./data")

    # Auth — token from env var takes precedence over config
    dashboard_cfg = config.get("dashboard", {})
    _state["auth_token"] = (
        os.environ.get("DASHBOARD_TOKEN")
        or dashboard_cfg.get("token", "")
        or ""
    )
    # Flask needs a stable secret key for sessions; generate one per process if not set
    app.secret_key = os.environ.get("DASHBOARD_SECRET_KEY") or secrets.token_hex(32)

    # Connection pool (lazy — will surface errors on first request)
    try:
        _state["pool"] = ThreadedConnectionPool(1, 8, dsn=dsn)
        logger.info("dashboard_pg_pool_ready")
    except Exception as e:
        logger.error("dashboard_pg_pool_failed", error=str(e))
        _state["pool"] = None

    try:
        _state["redis_client"] = redis.Redis.from_url(
            redis_url, decode_responses=True,
            socket_timeout=5, socket_connect_timeout=5,
            retry_on_timeout=True,
        )
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
                    if hasattr(v, "hex") and not isinstance(v, (bytes, bytearray, float)):
                        try:
                            r[k] = str(v)
                        except Exception:
                            pass
            return rows[0] if one else rows


# =============================================================================
# Authentication
# =============================================================================

@app.before_request
def require_auth():
    token = _state.get("auth_token", "")
    if not token:
        return  # auth disabled — no token configured
    public_paths = ("/login", "/logout", "/static")
    if any(request.path.startswith(p) for p in public_paths):
        return
    if not session.get("authenticated"):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Unauthorized"}), 401
        return redirect(url_for("login_page"))


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        submitted = request.form.get("token", "")
        expected = _state.get("auth_token", "")
        if expected and secrets.compare_digest(submitted, expected):
            session["authenticated"] = True
            return redirect(url_for("index"))
        return render_template("login.html", error="Invalid token. Try again.")
    return render_template("login.html", error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


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
        "timestamp": datetime.now(timezone.utc).isoformat(),
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


# =============================================================================
# Source editing helpers
# =============================================================================

def _update_source_in_yaml(name: str, **kwargs) -> bool:
    """Update source fields in settings.yaml, preserving comments and formatting."""
    config_path = _PROJECT_ROOT / "config" / "settings.yaml"
    if not config_path.exists():
        return False

    def _yaml_val(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        return str(v)

    with open(config_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    in_block = False
    modified = False
    remaining = dict(kwargs)

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped in (f'- name: "{name}"', f"- name: '{name}'"):
            in_block = True
            continue
        if in_block and stripped.startswith("- name:"):
            break
        if in_block and remaining:
            for key in list(remaining):
                if stripped.startswith(f"{key}:"):
                    indent = len(line) - len(line.lstrip())
                    lines[i] = " " * indent + f"{key}: {_yaml_val(remaining.pop(key))}\n"
                    modified = True
                    break

    if modified:
        with open(config_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
    return modified


def _db_update_source(name: str, **kwargs):
    """Sync psycopg2 update of the sources table."""
    allowed = {"enabled", "scrape_interval_hours", "priority"}
    data = {k: v for k, v in kwargs.items() if k in allowed}
    if not data:
        return
    set_parts = [f"{k} = %s" for k in data]
    values = list(data.values()) + [name]
    sql = f"UPDATE sources SET {', '.join(set_parts)}, updated_at = NOW() WHERE name = %s"
    with _PGConn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, values)
        conn.commit()


@app.route("/api/sources/<name>", methods=["PUT"])
def update_source(name: str):
    """Update source enabled/interval/priority in DB and settings.yaml."""
    payload = request.get_json(silent=True) or {}
    updates: dict = {}

    if "enabled" in payload:
        updates["enabled"] = bool(payload["enabled"])
    if "scrape_interval_hours" in payload:
        val = int(payload["scrape_interval_hours"])
        if not (1 <= val <= 8760):
            return jsonify({"ok": False, "error": "interval must be 1–8760 hours"}), 400
        updates["scrape_interval_hours"] = val
    if "priority" in payload:
        val = int(payload["priority"])
        if not (1 <= val <= 10):
            return jsonify({"ok": False, "error": "priority must be 1–10"}), 400
        updates["priority"] = val

    if not updates:
        return jsonify({"ok": False, "error": "Nothing to update"}), 400

    try:
        _db_update_source(name, **updates)
    except Exception as e:
        logger.error("source_db_update_failed", name=name, error=str(e))
        return jsonify({"ok": False, "error": str(e)}), 500

    try:
        yaml_ok = _update_source_in_yaml(name, **updates)
    except Exception as e:
        logger.warning("source_yaml_update_failed", name=name, error=str(e))
        yaml_ok = False

    return jsonify({
        "ok": True,
        "name": name,
        "updated": updates,
        "yaml_updated": yaml_ok,
    })


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
        today = datetime.now(timezone.utc).date()
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


@app.route("/api/rounds/cleanup", methods=["POST"])
def rounds_cleanup():
    """Mark all stale 'running' rounds as 'interrupted'."""
    try:
        with _PGConn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE rounds
                    SET status = 'interrupted', end_time = CURRENT_TIMESTAMP
                    WHERE status = 'running'
                """)
                count = cur.rowcount
            conn.commit()
        return jsonify({
            "ok": True,
            "cleaned": count,
            "message": f"{count} stale round(s) marked as interrupted",
        })
    except Exception as e:
        logger.error("rounds_cleanup_error", error=str(e))
        return jsonify({"ok": False, "error": str(e)}), 500


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
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                }
        except Exception as e:
            logger.error("qa_job_failed", error=str(e))
            with _qa_lock:
                _qa_state["last_result"] = {
                    "ok": False,
                    "error": str(e),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                }
        finally:
            with _qa_lock:
                _qa_state["running"] = False

    with _qa_lock:
        _qa_state["running"] = True
        _qa_state["started_at"] = datetime.now(timezone.utc).isoformat()
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
# Process manager (shared by scrape-loop and qa-loop)
# =============================================================================

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except Exception:
        return False


class _ProcessManager:
    """Manages a long-running subprocess with PID file and dedicated log file."""

    def __init__(self, name: str, pid_file: Path, log_file: Path):
        self.name = name
        self.pid_file = pid_file
        self.log_file = log_file
        self._lock = threading.Lock()
        self._popen: Optional[subprocess.Popen] = None
        self._started_at: Optional[str] = None

    def _read_pid_meta(self) -> dict:
        out = {"pid": None, "command": None, "started_at": None}
        try:
            if not self.pid_file.exists():
                return out
            lines = self.pid_file.read_text().splitlines()
            if lines:
                out["pid"] = int(lines[0].strip())
            if len(lines) > 1:
                out["command"] = lines[1].strip() or None
            if len(lines) > 2:
                out["started_at"] = lines[2].strip() or None
        except Exception:
            pass
        return out

    def _write_pid(self, pid: int, command: str):
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        self.pid_file.write_text(
            f"{pid}\n{command}\n{datetime.now(timezone.utc).isoformat()}\n"
        )

    def _clear_pid(self):
        self.pid_file.unlink(missing_ok=True)

    def is_running(self) -> tuple[bool, Optional[int]]:
        meta = self._read_pid_meta()
        pid = meta.get("pid")
        if pid and _pid_alive(pid):
            return True, pid
        if pid:
            self._clear_pid()
        return False, None

    def status(self) -> dict:
        running, pid = self.is_running()
        meta = self._read_pid_meta()
        with self._lock:
            started = self._started_at or meta.get("started_at")
        return {
            "running": running,
            "pid": pid,
            "command": meta.get("command"),
            "started_at": started,
            "log_file": str(self.log_file),
        }

    def start(self, cli_command: str, extra_args: Optional[list] = None) -> dict:
        running, pid = self.is_running()
        if running:
            return {"ok": False, "error": f"{self.name} already running (pid {pid})", "pid": pid}

        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            log_fh = open(self.log_file, "a", buffering=1, encoding="utf-8")
            log_fh.write(
                f"\n=== {datetime.now(timezone.utc).isoformat()} START: {cli_command}"
                f"{' ' + ' '.join(extra_args) if extra_args else ''} ===\n"
            )
            env = os.environ.copy()
            env.setdefault("PYTHONUNBUFFERED", "1")
            cmd = [sys.executable, "-m", "src.main", cli_command] + (extra_args or [])
            proc = subprocess.Popen(
                cmd,
                cwd=str(_PROJECT_ROOT),
                stdout=log_fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                env=env, start_new_session=True,
            )
        except Exception as e:
            logger.error(f"{self.name}_spawn_failed", error=str(e))
            return {"ok": False, "error": str(e)}

        self._write_pid(proc.pid, cli_command)
        with self._lock:
            self._popen = proc
            self._started_at = datetime.now(timezone.utc).isoformat()

        logger.info(f"{self.name}_started", pid=proc.pid)
        return {
            "ok": True, "pid": proc.pid,
            "message": f"{self.name} started",
            "started_at": self._started_at,
        }

    def stop(self, grace_seconds: int = 15) -> dict:
        running, pid = self.is_running()
        if not running:
            return {"ok": False, "error": f"{self.name} is not running"}

        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except ProcessLookupError:
            self._clear_pid()
            return {"ok": True, "message": f"{self.name} already exited"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

        deadline = time.time() + grace_seconds
        while time.time() < deadline:
            if not _pid_alive(pid):
                break
            time.sleep(0.5)

        forced = False
        if _pid_alive(pid):
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
                forced = True
            except Exception:
                pass

        self._clear_pid()
        with self._lock:
            self._popen = None
            self._started_at = None

        logger.info(f"{self.name}_stopped", pid=pid, forced=forced)
        return {
            "ok": True,
            "message": f"{self.name} stopped{' (forced)' if forced else ''}",
            "pid": pid, "forced": forced,
        }

    def tail_logs(self, lines: int = 200) -> dict:
        if not self.log_file.exists():
            return {"lines": [], "log_file": str(self.log_file), "exists": False}
        try:
            with open(self.log_file, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                chunk = min(size, 256 * 1024)
                f.seek(size - chunk)
                buf = f.read().decode("utf-8", errors="replace")
            tail = deque(buf.splitlines(), maxlen=lines)
            return {
                "lines": list(tail),
                "log_file": str(self.log_file),
                "exists": True,
                "size_bytes": size,
            }
        except Exception as e:
            logger.error(f"{self.name}_log_read_failed", error=str(e))
            return {"error": str(e)}


# Two dedicated process managers
_scrape_mgr = _ProcessManager(
    "scrape-loop",
    _PROJECT_ROOT / "data" / "scrape_loop.pid",
    _PROJECT_ROOT / "logs" / "scrape_loop.log",
)
_qa_loop_mgr = _ProcessManager(
    "qa-loop",
    _PROJECT_ROOT / "data" / "qa_loop.pid",
    _PROJECT_ROOT / "logs" / "qa_loop.log",
)

# Legacy combined pipeline (kept for backward compat; not shown in new UI)
_PIPELINE_PID_FILE = _PROJECT_ROOT / "data" / "pipeline.pid"
_PIPELINE_LOG_FILE = _PROJECT_ROOT / "logs" / "pipeline_dashboard.log"
_pipeline_lock = threading.Lock()
_pipeline_state: dict = {"popen": None, "started_at": None, "command": None, "single_round_running": False}


def _read_pid_meta() -> dict:
    out = {"pid": None, "command": None, "started_at": None}
    try:
        if not _PIPELINE_PID_FILE.exists():
            return out
        lines = _PIPELINE_PID_FILE.read_text().splitlines()
        if lines:
            out["pid"] = int(lines[0].strip())
        if len(lines) > 1:
            out["command"] = lines[1].strip() or None
        if len(lines) > 2:
            out["started_at"] = lines[2].strip() or None
    except Exception:
        pass
    return out


def _pipeline_running() -> tuple[bool, Optional[int]]:
    meta = _read_pid_meta()
    pid = meta.get("pid")
    if pid and _pid_alive(pid):
        return True, pid
    if pid:
        _PIPELINE_PID_FILE.unlink(missing_ok=True)
    return False, None


# =============================================================================
# Scrape loop API
# =============================================================================

@app.route("/api/scrape/status")
def scrape_status():
    return jsonify(_scrape_mgr.status())


@app.route("/api/scrape/start", methods=["POST"])
def scrape_start():
    result = _scrape_mgr.start("scrape-loop")
    if result.get("ok") and _clear_pause_flag():
        result["resumed_pause"] = True
        result["message"] = (result.get("message") or "") + " — stale pause flag cleared"
    code = 409 if not result["ok"] and "already running" in result.get("error", "") else (500 if not result["ok"] else 200)
    return jsonify(result), code


@app.route("/api/scrape/stop", methods=["POST"])
def scrape_stop():
    grace = max(2, min(60, int((request.get_json(silent=True) or {}).get("grace_seconds", 15))))
    result = _scrape_mgr.stop(grace)
    code = 409 if not result["ok"] else 200
    return jsonify(result), code


@app.route("/api/scrape/logs")
def scrape_logs():
    lines = max(10, min(2000, request.args.get("lines", 200, type=int)))
    return jsonify(_scrape_mgr.tail_logs(lines))


# =============================================================================
# Q/A loop API
# =============================================================================

@app.route("/api/qa-loop/status")
def qa_loop_status():
    return jsonify(_qa_loop_mgr.status())


@app.route("/api/qa-loop/start", methods=["POST"])
def qa_loop_start():
    result = _qa_loop_mgr.start("qa-loop")
    if result.get("ok") and _clear_pause_flag():
        result["resumed_pause"] = True
        result["message"] = (result.get("message") or "") + " — stale pause flag cleared"
    code = 409 if not result["ok"] and "already running" in result.get("error", "") else (500 if not result["ok"] else 200)
    return jsonify(result), code


@app.route("/api/qa-loop/stop", methods=["POST"])
def qa_loop_stop():
    grace = max(2, min(60, int((request.get_json(silent=True) or {}).get("grace_seconds", 15))))
    result = _qa_loop_mgr.stop(grace)
    code = 409 if not result["ok"] else 200
    return jsonify(result), code


@app.route("/api/qa-loop/logs")
def qa_loop_logs():
    lines = max(10, min(2000, request.args.get("lines", 200, type=int)))
    return jsonify(_qa_loop_mgr.tail_logs(lines))


# =============================================================================
# Redis-based pipeline pause / resume (works for Docker-managed pipeline)
# =============================================================================

_PIPELINE_PAUSE_KEY = "pipeline:paused"
_HEARTBEAT_MODES = ("run", "scrape-loop", "qa-loop")


def _read_heartbeats() -> dict:
    """Read pipeline:heartbeat:<mode> keys published by orchestrator processes.
    This is the only way to see the Docker collector — it runs in another
    container, invisible to this process's PID files."""
    out = {m: None for m in _HEARTBEAT_MODES}
    rc = _state["redis_client"]
    if not rc:
        return out
    for mode in _HEARTBEAT_MODES:
        try:
            raw = rc.get(f"pipeline:heartbeat:{mode}")
            if not raw:
                continue
            hb = json.loads(raw)
            ts = hb.get("ts")
            if ts:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
                hb["age_seconds"] = max(0, int(age))
            out[mode] = hb
        except Exception:
            continue
    return out


def _clear_pause_flag() -> bool:
    """Delete the global pause flag; True if it was set. Start buttons call
    this — otherwise a freshly started loop silently blocks on a stale pause
    flag while the UI claims it is running."""
    rc = _state["redis_client"]
    if not rc:
        return False
    try:
        return bool(rc.delete(_PIPELINE_PAUSE_KEY))
    except Exception:
        return False


@app.route("/api/pipeline/paused")
def pipeline_pause_status():
    rc = _state["redis_client"]
    if not rc:
        return jsonify({"paused": False, "redis": False})
    try:
        paused = bool(rc.get(_PIPELINE_PAUSE_KEY))
        return jsonify({"paused": paused, "redis": True})
    except Exception as e:
        return jsonify({"paused": False, "redis": False, "error": str(e)})


@app.route("/api/pipeline/pause", methods=["POST"])
def pipeline_pause():
    rc = _state["redis_client"]
    if not rc:
        return jsonify({"ok": False, "error": "Redis not available"}), 503
    try:
        rc.set(_PIPELINE_PAUSE_KEY, "1")
        logger.info("pipeline_paused_via_dashboard")
        return jsonify({"ok": True, "message": "Pipeline paused — current round will finish, next round will wait"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/pipeline/resume", methods=["POST"])
def pipeline_resume():
    rc = _state["redis_client"]
    if not rc:
        return jsonify({"ok": False, "error": "Redis not available"}), 503
    try:
        rc.delete(_PIPELINE_PAUSE_KEY)
        logger.info("pipeline_resumed_via_dashboard")
        return jsonify({"ok": True, "message": "Pipeline resumed"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# =============================================================================
# Pipeline process control (legacy — kept for backward compat)
# =============================================================================

def _spawn_pipeline(cli_command: str) -> subprocess.Popen:
    """Spawn `python -m src.main <cli_command>` detached, logging to file."""
    _PIPELINE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(_PIPELINE_LOG_FILE, "a", buffering=1, encoding="utf-8")
    log_fh.write(
        f"\n=== {datetime.now(timezone.utc).isoformat()} dashboard spawn: {cli_command} ===\n"
    )
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    return subprocess.Popen(
        [sys.executable, "-m", "src.main", cli_command],
        cwd=str(_PROJECT_ROOT),
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )


@app.route("/api/pipeline/status")
def pipeline_status():
    running, pid = _pipeline_running()
    meta = _read_pid_meta()
    with _pipeline_lock:
        single = _pipeline_state["single_round_running"]
    rc = _state["redis_client"]
    paused = False
    if rc:
        try:
            paused = bool(rc.get(_PIPELINE_PAUSE_KEY))
        except Exception:
            pass
    return jsonify({
        "running": running,
        "pid": pid,
        "command": meta.get("command"),
        "started_at": meta.get("started_at"),
        "single_round_running": single,
        "paused": paused,
        "log_file": str(_PIPELINE_LOG_FILE),
    })


@app.route("/api/pipeline/overview")
def pipeline_overview():
    """Read-only aggregate for the dashboard status card: process status,
    latest round (with live counters for running rounds), loop statuses and
    a few headline stats — one round-trip instead of five."""
    running, pid = _pipeline_running()
    meta = _read_pid_meta()
    with _pipeline_lock:
        single = _pipeline_state["single_round_running"]
    paused = False
    rc = _state["redis_client"]
    if rc:
        try:
            paused = bool(rc.get(_PIPELINE_PAUSE_KEY))
        except Exception:
            pass

    heartbeats = _read_heartbeats()
    out: dict = {
        "server_time": datetime.now(timezone.utc).isoformat(),
        "pipeline": {
            "running": running,
            "pid": pid,
            "command": meta.get("command"),
            "started_at": meta.get("started_at"),
            "single_round_running": single,
            "paused": paused,
        },
        "scrape_loop": _scrape_mgr.status(),
        "qa_loop": _qa_loop_mgr.status(),
        "heartbeats": heartbeats,
        "latest_round": None,
        "enabled_sources": 0,
        "stats": None,
    }

    try:
        rnd = _query("SELECT * FROM rounds ORDER BY round_number DESC LIMIT 1", one=True)
        if rnd:
            for k in ("start_time", "end_time"):
                if rnd.get(k):
                    rnd[k] = rnd[k].isoformat()
            # Round counters are only persisted at complete_round(), so for a
            # running round compute live numbers from scraped_data instead.
            if rnd.get("status") == "running":
                live = _query("""
                    SELECT COUNT(*) AS scraped,
                           SUM(CASE WHEN is_duplicate THEN 1 ELSE 0 END) AS duplicates,
                           COUNT(DISTINCT source_name) AS sources_touched,
                           MAX(date_scraped) AS last_item_at
                    FROM scraped_data WHERE round_number = %s
                """, (rnd["round_number"],), one=True) or {}
                rnd["live"] = {
                    "scraped": int(live.get("scraped") or 0),
                    "duplicates": int(live.get("duplicates") or 0),
                    "sources_touched": int(live.get("sources_touched") or 0),
                    "last_item_at": live["last_item_at"].isoformat() if live.get("last_item_at") else None,
                }
            out["latest_round"] = rnd
    except Exception as e:
        out["latest_round_error"] = str(e)

    try:
        out["enabled_sources"] = int((_query(
            "SELECT COUNT(*) AS n FROM sources WHERE enabled = true", one=True
        ) or {}).get("n") or 0)
        s = _query("""
            SELECT
                (SELECT COUNT(*) FROM scraped_data WHERE is_duplicate = false) AS unique_documents,
                (SELECT COUNT(*) FROM qa_pairs) AS total_qa_pairs,
                (SELECT COUNT(*) FROM scraped_data
                   WHERE is_duplicate = false AND qa_count = 0) AS pending_qa,
                (SELECT COUNT(*) FROM scraped_data
                   WHERE pipeline_stage IN ('deduplicated','quality_checked','stored')
                     AND is_duplicate = false AND qa_count = 0
                     AND word_count >= 20 AND content_length >= 100) AS qa_eligible,
                (SELECT MAX(date_scraped) FROM scraped_data) AS last_scrape,
                (SELECT COUNT(*) FROM scraped_data
                   WHERE date_scraped >= NOW() - INTERVAL '10 minutes') AS docs_last_10min
        """, one=True) or {}
        out["stats"] = {
            "unique_documents": int(s.get("unique_documents") or 0),
            "total_qa_pairs": int(s.get("total_qa_pairs") or 0),
            "pending_qa_documents": int(s.get("pending_qa") or 0),
            # Same predicate as get_documents_for_qa — what the Q/A loop will
            # actually pick up. 0 here explains a Q/A loop producing nothing.
            "qa_eligible_documents": int(s.get("qa_eligible") or 0),
            "last_scrape": s["last_scrape"].isoformat() if s.get("last_scrape") else None,
            "docs_last_10min": int(s.get("docs_last_10min") or 0),
        }
    except Exception as e:
        out["stats_error"] = str(e)

    return jsonify(out)


@app.route("/api/pipeline/start", methods=["POST"])
def pipeline_start():
    running, pid = _pipeline_running()
    if running:
        return jsonify({"ok": False, "error": f"Pipeline already running (pid {pid})", "pid": pid}), 409
    try:
        proc = _spawn_pipeline("run")
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    _PIPELINE_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PIPELINE_PID_FILE.write_text(f"{proc.pid}\nrun\n{datetime.now(timezone.utc).isoformat()}\n")
    with _pipeline_lock:
        _pipeline_state.update({"popen": proc, "started_at": datetime.now(timezone.utc).isoformat(), "command": "run"})
    out = {"ok": True, "pid": proc.pid, "message": "Pipeline started"}
    if _clear_pause_flag():
        out["resumed_pause"] = True
        out["message"] += " — stale pause flag cleared"
    return jsonify(out)


@app.route("/api/pipeline/stop", methods=["POST"])
def pipeline_stop():
    running, pid = _pipeline_running()
    if not running:
        return jsonify({"ok": False, "error": "Pipeline is not running"}), 409
    grace = max(2, min(60, int((request.get_json(silent=True) or {}).get("grace_seconds", 15))))
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except ProcessLookupError:
        _PIPELINE_PID_FILE.unlink(missing_ok=True)
        return jsonify({"ok": True, "message": "Pipeline already exited"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    deadline = time.time() + grace
    while time.time() < deadline:
        if not _pid_alive(pid):
            break
        time.sleep(0.5)
    forced = False
    if _pid_alive(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            forced = True
        except Exception:
            pass
    _PIPELINE_PID_FILE.unlink(missing_ok=True)
    with _pipeline_lock:
        _pipeline_state.update({"popen": None, "started_at": None, "command": None})
    return jsonify({"ok": True, "message": f"Pipeline stopped{' (forced)' if forced else ''}", "pid": pid})


@app.route("/api/pipeline/run-single", methods=["POST"])
def pipeline_run_single():
    running, pid = _pipeline_running()
    if running:
        return jsonify({"ok": False, "error": f"Continuous pipeline running (pid {pid})"}), 409
    with _pipeline_lock:
        if _pipeline_state["single_round_running"]:
            return jsonify({"ok": False, "error": "A single round is already running"}), 409
        _pipeline_state["single_round_running"] = True

    def worker():
        try:
            proc = _spawn_pipeline("single-round")
            proc.wait()
        except Exception as e:
            logger.error("single_round_failed", error=str(e))
        finally:
            with _pipeline_lock:
                _pipeline_state["single_round_running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "message": "Single round started"})


@app.route("/api/pipeline/logs")
def pipeline_logs():
    lines = max(10, min(2000, request.args.get("lines", 200, type=int)))
    if not _PIPELINE_LOG_FILE.exists():
        return jsonify({"lines": [], "log_file": str(_PIPELINE_LOG_FILE), "exists": False})
    try:
        with open(_PIPELINE_LOG_FILE, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            chunk = min(size, 256 * 1024)
            f.seek(size - chunk)
            buf = f.read().decode("utf-8", errors="replace")
        tail = deque(buf.splitlines(), maxlen=lines)
        return jsonify({"lines": list(tail), "log_file": str(_PIPELINE_LOG_FILE), "exists": True, "size_bytes": size})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/pipeline/export-qa", methods=["POST"])
def pipeline_export_qa():
    payload = request.get_json(silent=True) or {}
    fmt = payload.get("format", "jsonl")
    if fmt not in ("jsonl", "json", "huggingface"):
        return jsonify({"ok": False, "error": "format must be 'jsonl', 'json', or 'huggingface'"}), 400
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = _PROJECT_ROOT / "data" / "qa_output"
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = "jsonl" if fmt in ("jsonl", "huggingface") else "json"
    out_path = out_dir / f"qa_export_{ts}_{fmt}.{ext}"

    def worker():
        try:
            log_fh = open(_PIPELINE_LOG_FILE, "a", buffering=1, encoding="utf-8")
            log_fh.write(f"\n=== {datetime.now(timezone.utc).isoformat()} export-qa → {out_path} ===\n")
            subprocess.run(
                [sys.executable, "-m", "src.main", "export-qa", "--output", str(out_path), "--format", fmt],
                cwd=str(_PROJECT_ROOT), stdout=log_fh, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, check=False,
            )
        except Exception as e:
            logger.error("qa_export_failed", error=str(e))

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "message": "Export started; check scrape loop logs.", "output_path": str(out_path)})


# =============================================================================
# Q/A-only pipeline (legacy endpoint — kept for backward compat)
# =============================================================================

@app.route("/api/qa/pipeline/status")
def qa_pipeline_status():
    s = _qa_loop_mgr.status()
    return jsonify({"running": s["running"], "pid": s["pid"], "started_at": s["started_at"]})


@app.route("/api/qa/pipeline/start", methods=["POST"])
def qa_pipeline_start():
    result = _qa_loop_mgr.start("qa-loop")
    code = 409 if not result["ok"] and "already running" in result.get("error", "") else (500 if not result["ok"] else 200)
    return jsonify(result), code


@app.route("/api/qa/pipeline/stop", methods=["POST"])
def qa_pipeline_stop():
    result = _qa_loop_mgr.stop(15)
    code = 409 if not result["ok"] else 200
    return jsonify(result), code


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
                  port: int = 5050, debug: bool = False):
    if config is None:
        config = load_config("config/settings.yaml")
    configure(config)
    logger.info("dashboard_starting", host=host, port=port)
    app.run(host=host, port=port, debug=debug, threaded=True)


if __name__ == "__main__":
    run_dashboard(debug=True)
