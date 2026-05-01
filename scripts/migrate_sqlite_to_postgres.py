#!/usr/bin/env python3
"""
One-shot migration: copy data from the legacy SQLite DB into PostgreSQL.

Reads ./data/db/collector.db (or --sqlite path) and inserts rows into the
Postgres schema defined in scripts/init-db.sql.

Usage:
    python scripts/migrate_sqlite_to_postgres.py
    python scripts/migrate_sqlite_to_postgres.py --sqlite /path/to.db
    python scripts/migrate_sqlite_to_postgres.py --dry-run
"""

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Allow running this file directly without `pip install -e .`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.database import DatabaseManager  # noqa: E402
from src.utils.helpers import load_config  # noqa: E402


def open_sqlite(path: str) -> sqlite3.Connection:
    if not Path(path).exists():
        print(f"[warn] SQLite file not found: {path}")
        sys.exit(1)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        try:
            return datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None


async def migrate(sqlite_path: str, dry_run: bool = False):
    config = load_config("config/settings.yaml")
    pg = DatabaseManager(config)
    await pg.initialize()

    sq = open_sqlite(sqlite_path)
    counts = {"documents": 0, "qa_pairs": 0, "urls_visited": 0,
              "rounds": 0, "simhash": 0, "skipped": 0}

    # ---- documents -> scraped_data
    docs = sq.execute("SELECT * FROM documents").fetchall()
    print(f"[info] {len(docs)} document(s) to migrate")

    # int(sqlite id) -> uuid(pg id)
    id_map: dict[int, str] = {}

    async with pg._pool.acquire() as conn:
        for d in docs:
            row = dict(d)
            try:
                metadata = json.loads(row.get("metadata_json") or "{}")
            except Exception:
                metadata = {}
            params = (
                row.get("source_name"), row.get("source_url") or "",
                row.get("page_url"), row.get("url_hash"),
                row.get("content_hash"), row.get("simhash") or "",
                row.get("title") or "", "", None,
                row.get("author") or "",
                row.get("category") or "article", "en", 0,
                int(row.get("content_length") or 0),
                int(row.get("file_size_bytes") or 0),
                row.get("file_path_html") or "",
                row.get("file_path_pdf") or "",
                row.get("pipeline_stage") or "scraped",
                bool(row.get("is_duplicate")), None,
                int(row.get("qa_count") or 0),
                int(row.get("round_number") or 0),
                row.get("tags") or "", json.dumps(metadata),
                parse_dt(row.get("date_published")),
                parse_dt(row.get("date_scraped")) or datetime.utcnow(),
            )
            if dry_run:
                counts["documents"] += 1
                continue
            try:
                new_id = await conn.fetchval("""
                    INSERT INTO scraped_data (
                        source_name, source_url, page_url, url_hash,
                        content_hash, simhash, title, content_text, content_html,
                        author, category, language, word_count, content_length,
                        file_size_bytes, file_path_html, file_path_pdf,
                        pipeline_stage, is_duplicate, duplicate_of, qa_count,
                        round_number, tags, metadata, date_published, date_scraped
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                            $11, $12, $13, $14, $15, $16, $17, $18, $19, $20,
                            $21, $22, $23, $24::jsonb, $25, $26)
                    ON CONFLICT (url_hash) DO NOTHING
                    RETURNING id
                """, *params)
                if new_id:
                    id_map[int(row["id"])] = str(new_id)
                    counts["documents"] += 1
                else:
                    # Already exists — fetch existing id for FK mapping
                    existing = await conn.fetchval(
                        "SELECT id FROM scraped_data WHERE url_hash = $1",
                        row.get("url_hash"),
                    )
                    if existing:
                        id_map[int(row["id"])] = str(existing)
                    counts["skipped"] += 1
            except Exception as e:
                print(f"[err] doc {row.get('id')}: {e}")
                counts["skipped"] += 1

        # Resolve duplicate_of FK after all rows inserted
        for d in docs:
            if d["is_duplicate"] and d["duplicate_of"]:
                src = id_map.get(int(d["id"]))
                tgt = id_map.get(int(d["duplicate_of"]))
                if src and tgt and not dry_run:
                    await conn.execute(
                        "UPDATE scraped_data SET duplicate_of = $1 WHERE id = $2",
                        tgt, src,
                    )

        # ---- visited_urls -> urls_visited
        try:
            visited = sq.execute("SELECT * FROM visited_urls").fetchall()
        except sqlite3.OperationalError:
            visited = []
        for v in visited:
            v = dict(v)
            if dry_run:
                counts["urls_visited"] += 1
                continue
            try:
                await conn.execute("""
                    INSERT INTO urls_visited (url_hash, url, source_name,
                                              first_visited_at, last_visited_at,
                                              visit_count, last_content_hash, has_changed)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (url_hash) DO NOTHING
                """,
                    v.get("url_hash"), v.get("url"), v.get("source_name"),
                    parse_dt(v.get("last_visited")) or datetime.utcnow(),
                    parse_dt(v.get("last_visited")) or datetime.utcnow(),
                    int(v.get("visit_count") or 1),
                    v.get("last_content_hash") or "",
                    bool(v.get("has_changed")),
                )
                counts["urls_visited"] += 1
            except Exception as e:
                print(f"[err] visited {v.get('url_hash')}: {e}")

        # ---- qa_pairs
        try:
            qa = sq.execute("SELECT * FROM qa_pairs").fetchall()
        except sqlite3.OperationalError:
            qa = []
        for p in qa:
            p = dict(p)
            new_doc_id = id_map.get(int(p["document_id"]))
            if not new_doc_id:
                continue
            try:
                meta = json.loads(p.get("metadata_json") or "{}")
            except Exception:
                meta = {}
            # qa_hash kolonu eski şemada yoktu; üret
            from hashlib import sha256
            qa_hash = sha256(
                f"{p['question']}|{p['answer']}".encode("utf-8")
            ).hexdigest()
            if dry_run:
                counts["qa_pairs"] += 1
                continue
            try:
                await conn.execute("""
                    INSERT INTO qa_pairs (document_id, source_name, question, answer,
                                          qa_hash, category, car_brand, car_model,
                                          car_year, quality_score, llm_model,
                                          metadata, date_generated)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13)
                    ON CONFLICT (qa_hash) DO NOTHING
                """,
                    new_doc_id, p.get("source_name") or "",
                    p.get("question"), p.get("answer"), qa_hash,
                    p.get("category") or "", p.get("car_brand") or "",
                    p.get("car_model") or "", p.get("car_year") or "",
                    float(p.get("quality_score") or 0),
                    p.get("model_used") or "", json.dumps(meta),
                    parse_dt(p.get("date_generated")) or datetime.utcnow(),
                )
                counts["qa_pairs"] += 1
            except Exception as e:
                print(f"[err] qa {p.get('id')}: {e}")

        # ---- rounds
        try:
            rounds = sq.execute("SELECT * FROM rounds").fetchall()
        except sqlite3.OperationalError:
            rounds = []
        for r in rounds:
            r = dict(r)
            if dry_run:
                counts["rounds"] += 1
                continue
            try:
                completed = json.loads(r.get("sources_completed") or "[]")
            except Exception:
                completed = []
            try:
                await conn.execute("""
                    INSERT INTO rounds (round_number, start_time, end_time,
                                        total_urls_visited, total_items_scraped,
                                        total_items_stored, total_duplicates_found,
                                        total_qa_generated, errors_count,
                                        sources_completed, status)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11)
                    ON CONFLICT (round_number) DO NOTHING
                """,
                    int(r["round_number"]),
                    parse_dt(r.get("start_time")) or datetime.utcnow(),
                    parse_dt(r.get("end_time")),
                    int(r.get("total_urls_visited") or 0),
                    int(r.get("total_items_scraped") or 0),
                    int(r.get("total_items_stored") or 0),
                    int(r.get("total_duplicates_found") or 0),
                    int(r.get("total_qa_generated") or 0),
                    int(r.get("errors_count") or 0),
                    json.dumps(completed),
                    r.get("status") or "completed",
                )
                counts["rounds"] += 1
            except Exception as e:
                print(f"[err] round {r.get('round_number')}: {e}")

        # ---- simhash_index
        try:
            simhashes = sq.execute("SELECT * FROM simhash_index").fetchall()
        except sqlite3.OperationalError:
            simhashes = []
        for s in simhashes:
            s = dict(s)
            new_doc_id = id_map.get(int(s["document_id"]))
            if not new_doc_id or dry_run:
                if dry_run:
                    counts["simhash"] += 1
                continue
            try:
                await conn.execute("""
                    INSERT INTO simhash_index (document_id, simhash_value,
                                                bucket_0, bucket_1, bucket_2, bucket_3)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (document_id) DO NOTHING
                """,
                    new_doc_id, s["simhash_value"],
                    s.get("bucket_0") or "", s.get("bucket_1") or "",
                    s.get("bucket_2") or "", s.get("bucket_3") or "",
                )
                counts["simhash"] += 1
            except Exception as e:
                print(f"[err] simhash {s.get('document_id')}: {e}")

    sq.close()
    await pg.close()

    print("\n=== Migration Summary ===")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    if dry_run:
        print("(dry-run — no rows written)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", default="data/db/collector.db",
                        help="Path to legacy SQLite database")
    parser.add_argument("--dry-run", action="store_true",
                        help="Read SQLite but do not write to Postgres")
    args = parser.parse_args()
    asyncio.run(migrate(args.sqlite, args.dry_run))


if __name__ == "__main__":
    main()
