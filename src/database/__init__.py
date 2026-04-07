"""
Veritabanı Yöneticisi - SQLite (async)
Tüm metadata, dedup bilgileri ve Q/A çiftleri burada tutulur.
WAL modunda çalışır (eşzamanlı okuma/yazma desteği).
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from src.models import (
    PipelineStage,
    QAPair,
    RoundStats,
    StoredDocument,
)
from src.utils.logger import get_logger

logger = get_logger("database")


class DatabaseManager:
    """SQLite veritabanı yönetimi"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def initialize(self):
        """Veritabanını oluştur ve tabloları kur"""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row

        # WAL modu
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA cache_size=-64000")  # 64MB cache
        await self._db.execute("PRAGMA temp_store=MEMORY")

        await self._create_tables()
        await self._db.commit()

        logger.info("database_initialized", path=self.db_path)

    async def _create_tables(self):
        """Tüm tabloları oluştur"""
        await self._db.executescript("""
            -- Ana döküman tablosu
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_name TEXT NOT NULL,
                source_url TEXT NOT NULL,
                page_url TEXT NOT NULL,
                url_hash TEXT NOT NULL UNIQUE,
                title TEXT DEFAULT '',
                content_hash TEXT NOT NULL,
                simhash TEXT DEFAULT '',
                file_path_html TEXT DEFAULT '',
                file_path_pdf TEXT DEFAULT '',
                file_size_bytes INTEGER DEFAULT 0,
                content_length INTEGER DEFAULT 0,
                category TEXT DEFAULT 'article',
                author TEXT DEFAULT '',
                date_published TIMESTAMP,
                date_scraped TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                date_processed TIMESTAMP,
                pipeline_stage TEXT DEFAULT 'scraped',
                is_duplicate INTEGER DEFAULT 0,
                duplicate_of INTEGER,
                qa_count INTEGER DEFAULT 0,
                round_number INTEGER DEFAULT 0,
                metadata_json TEXT DEFAULT '{}',
                tags TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Q/A çiftleri tablosu
            CREATE TABLE IF NOT EXISTS qa_pairs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL,
                source_name TEXT NOT NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                qa_hash TEXT NOT NULL UNIQUE,
                category TEXT DEFAULT '',
                car_brand TEXT DEFAULT '',
                car_model TEXT DEFAULT '',
                car_year TEXT DEFAULT '',
                quality_score REAL DEFAULT 0.0,
                date_generated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                model_used TEXT DEFAULT '',
                metadata_json TEXT DEFAULT '{}',
                FOREIGN KEY (document_id) REFERENCES documents(id)
            );

            -- Tur istatistikleri
            CREATE TABLE IF NOT EXISTS rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_number INTEGER NOT NULL UNIQUE,
                start_time TIMESTAMP NOT NULL,
                end_time TIMESTAMP,
                total_urls_visited INTEGER DEFAULT 0,
                total_items_scraped INTEGER DEFAULT 0,
                total_items_stored INTEGER DEFAULT 0,
                total_duplicates_found INTEGER DEFAULT 0,
                total_qa_generated INTEGER DEFAULT 0,
                errors_count INTEGER DEFAULT 0,
                sources_completed TEXT DEFAULT '[]',
                status TEXT DEFAULT 'running'
            );

            -- Ziyaret edilen URL log
            CREATE TABLE IF NOT EXISTS visited_urls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url_hash TEXT NOT NULL,
                url TEXT NOT NULL,
                source_name TEXT NOT NULL,
                last_visited TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                visit_count INTEGER DEFAULT 1,
                last_content_hash TEXT DEFAULT '',
                has_changed INTEGER DEFAULT 0
            );

            -- SimHash index (hızlı benzerlik araması)
            CREATE TABLE IF NOT EXISTS simhash_index (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL UNIQUE,
                simhash_value TEXT NOT NULL,
                bucket_0 TEXT DEFAULT '',
                bucket_1 TEXT DEFAULT '',
                bucket_2 TEXT DEFAULT '',
                bucket_3 TEXT DEFAULT '',
                FOREIGN KEY (document_id) REFERENCES documents(id)
            );

            -- İndeksler
            CREATE INDEX IF NOT EXISTS idx_doc_url_hash ON documents(url_hash);
            CREATE INDEX IF NOT EXISTS idx_doc_content_hash ON documents(content_hash);
            CREATE INDEX IF NOT EXISTS idx_doc_source ON documents(source_name);
            CREATE INDEX IF NOT EXISTS idx_doc_stage ON documents(pipeline_stage);
            CREATE INDEX IF NOT EXISTS idx_doc_duplicate ON documents(is_duplicate);
            CREATE INDEX IF NOT EXISTS idx_doc_round ON documents(round_number);
            CREATE INDEX IF NOT EXISTS idx_qa_doc ON qa_pairs(document_id);
            CREATE INDEX IF NOT EXISTS idx_qa_hash ON qa_pairs(qa_hash);
            CREATE INDEX IF NOT EXISTS idx_qa_brand ON qa_pairs(car_brand);
            CREATE INDEX IF NOT EXISTS idx_visited_hash ON visited_urls(url_hash);
            CREATE INDEX IF NOT EXISTS idx_visited_source ON visited_urls(source_name);
            CREATE INDEX IF NOT EXISTS idx_simhash_b0 ON simhash_index(bucket_0);
            CREATE INDEX IF NOT EXISTS idx_simhash_b1 ON simhash_index(bucket_1);
            CREATE INDEX IF NOT EXISTS idx_simhash_b2 ON simhash_index(bucket_2);
            CREATE INDEX IF NOT EXISTS idx_simhash_b3 ON simhash_index(bucket_3);
        """)

    # =========================================================================
    # Döküman İşlemleri
    # =========================================================================

    async def insert_document(self, doc: StoredDocument) -> int:
        """Yeni döküman ekle ve ID döndür"""
        async with self._lock:
            cursor = await self._db.execute("""
                INSERT OR IGNORE INTO documents 
                (source_name, source_url, page_url, url_hash, title, content_hash,
                 simhash, file_path_html, file_path_pdf, file_size_bytes, content_length,
                 category, author, date_published, date_scraped, pipeline_stage,
                 round_number, metadata_json, tags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                doc.source_name, doc.source_url, doc.page_url, doc.url_hash,
                doc.title, doc.content_hash, doc.simhash or "",
                doc.file_path_html or "", doc.file_path_pdf or "",
                doc.file_size_bytes, doc.content_length,
                doc.category.value if hasattr(doc.category, "value") else doc.category,
                doc.author, doc.date_published, doc.date_scraped,
                doc.pipeline_stage.value if hasattr(doc.pipeline_stage, "value") else doc.pipeline_stage,
                doc.round_number, doc.metadata_json, doc.tags,
            ))
            await self._db.commit()
            return cursor.lastrowid

    async def update_document_stage(self, doc_id: int, stage: PipelineStage):
        """Döküman pipeline aşamasını güncelle"""
        async with self._lock:
            await self._db.execute("""
                UPDATE documents SET pipeline_stage = ?, updated_at = ? WHERE id = ?
            """, (stage.value, datetime.utcnow(), doc_id))
            await self._db.commit()

    async def mark_duplicate(self, doc_id: int, duplicate_of: int):
        """Dökümanı duplikat olarak işaretle"""
        async with self._lock:
            await self._db.execute("""
                UPDATE documents SET is_duplicate = 1, duplicate_of = ?,
                pipeline_stage = 'deduplicated', updated_at = ? WHERE id = ?
            """, (duplicate_of, datetime.utcnow(), doc_id))
            await self._db.commit()

    async def get_document_by_url_hash(self, url_hash: str) -> Optional[dict]:
        """URL hash'e göre döküman bul"""
        cursor = await self._db.execute(
            "SELECT * FROM documents WHERE url_hash = ?", (url_hash,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_document_by_content_hash(self, content_hash: str) -> Optional[dict]:
        """İçerik hash'e göre döküman bul"""
        cursor = await self._db.execute(
            "SELECT * FROM documents WHERE content_hash = ? AND is_duplicate = 0",
            (content_hash,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_documents_for_qa(self, limit: int = 100) -> list[dict]:
        """Q/A üretimi için hazır dökümanları getir"""
        cursor = await self._db.execute("""
            SELECT * FROM documents 
            WHERE pipeline_stage = 'deduplicated' AND is_duplicate = 0 AND qa_count = 0
            ORDER BY content_length DESC
            LIMIT ?
        """, (limit,))
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_non_duplicate_documents(self, batch_size: int = 1000, offset: int = 0) -> list[dict]:
        """Duplikat olmayan dökümanları toplu getir"""
        cursor = await self._db.execute("""
            SELECT id, content_hash, simhash FROM documents 
            WHERE is_duplicate = 0
            ORDER BY id
            LIMIT ? OFFSET ?
        """, (batch_size, offset))
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_total_documents(self) -> int:
        """Toplam döküman sayısı"""
        cursor = await self._db.execute("SELECT COUNT(*) FROM documents")
        row = await cursor.fetchone()
        return row[0]

    async def get_total_unique_documents(self) -> int:
        """Duplikat olmayan döküman sayısı"""
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM documents WHERE is_duplicate = 0"
        )
        row = await cursor.fetchone()
        return row[0]

    # =========================================================================
    # Q/A İşlemleri
    # =========================================================================

    async def insert_qa_pairs(self, pairs: list[QAPair]) -> int:
        """Toplu Q/A çifti ekle, eklenen sayıyı döndür"""
        inserted = 0
        async with self._lock:
            for pair in pairs:
                try:
                    await self._db.execute("""
                        INSERT OR IGNORE INTO qa_pairs
                        (document_id, source_name, question, answer, qa_hash,
                         category, car_brand, car_model, car_year, quality_score,
                         model_used, metadata_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        pair.document_id, pair.source_name,
                        pair.question, pair.answer, pair.qa_hash,
                        pair.category, pair.car_brand, pair.car_model,
                        pair.car_year, pair.quality_score,
                        pair.model_used, pair.metadata_json,
                    ))
                    inserted += 1
                except Exception:
                    pass  # Duplikat Q/A hash — atla
            await self._db.commit()

            # Dökümanın qa_count'unu güncelle
            if pairs:
                doc_id = pairs[0].document_id
                await self._db.execute("""
                    UPDATE documents SET qa_count = qa_count + ?,
                    pipeline_stage = 'qa_generated', updated_at = ? WHERE id = ?
                """, (inserted, datetime.utcnow(), doc_id))
                await self._db.commit()

        return inserted

    async def get_total_qa_pairs(self) -> int:
        """Toplam Q/A çifti sayısı"""
        cursor = await self._db.execute("SELECT COUNT(*) FROM qa_pairs")
        row = await cursor.fetchone()
        return row[0]

    async def export_qa_pairs(self, limit: int = 0, offset: int = 0) -> list[dict]:
        """Q/A çiftlerini dışa aktar"""
        query = "SELECT * FROM qa_pairs ORDER BY id"
        params = []
        if limit > 0:
            query += " LIMIT ? OFFSET ?"
            params = [limit, offset]
        cursor = await self._db.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # =========================================================================
    # Ziyaret Edilen URL İşlemleri
    # =========================================================================

    async def is_url_visited(self, url_hash: str) -> bool:
        """URL daha önce ziyaret edilmiş mi?"""
        cursor = await self._db.execute(
            "SELECT id FROM visited_urls WHERE url_hash = ?", (url_hash,)
        )
        return await cursor.fetchone() is not None

    async def mark_url_visited(self, url_hash: str, url: str, source_name: str,
                                content_hash: str = ""):
        """URL'yi ziyaret edilmiş olarak işaretle"""
        async with self._lock:
            await self._db.execute("""
                INSERT INTO visited_urls (url_hash, url, source_name, last_content_hash)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(url_hash) DO UPDATE SET
                    last_visited = CURRENT_TIMESTAMP,
                    visit_count = visit_count + 1,
                    has_changed = CASE 
                        WHEN last_content_hash != excluded.last_content_hash THEN 1 
                        ELSE 0 
                    END,
                    last_content_hash = excluded.last_content_hash
            """, (url_hash, url, source_name, content_hash))
            await self._db.commit()

    async def get_changed_urls(self, source_name: str) -> list[dict]:
        """İçeriği değişmiş URL'leri getir"""
        cursor = await self._db.execute("""
            SELECT * FROM visited_urls 
            WHERE source_name = ? AND has_changed = 1
        """, (source_name,))
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # =========================================================================
    # SimHash İndeks İşlemleri
    # =========================================================================

    async def insert_simhash(self, document_id: int, simhash_value: str,
                              buckets: tuple[str, str, str, str]):
        """SimHash değerini indekse ekle"""
        async with self._lock:
            await self._db.execute("""
                INSERT OR REPLACE INTO simhash_index
                (document_id, simhash_value, bucket_0, bucket_1, bucket_2, bucket_3)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (document_id, simhash_value, *buckets))
            await self._db.commit()

    async def find_similar_simhashes(self, buckets: tuple[str, str, str, str]) -> list[dict]:
        """Benzer simhash değerlerini bul (en az bir bucket eşleşmesi)"""
        cursor = await self._db.execute("""
            SELECT DISTINCT si.document_id, si.simhash_value
            FROM simhash_index si
            WHERE si.bucket_0 = ? OR si.bucket_1 = ? OR si.bucket_2 = ? OR si.bucket_3 = ?
        """, buckets)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # =========================================================================
    # Tur İstatistikleri
    # =========================================================================

    async def start_round(self, round_number: int) -> int:
        """Yeni tur başlat"""
        async with self._lock:
            cursor = await self._db.execute("""
                INSERT INTO rounds (round_number, start_time)
                VALUES (?, ?)
            """, (round_number, datetime.utcnow()))
            await self._db.commit()
            return cursor.lastrowid

    async def update_round_stats(self, round_number: int, stats: dict):
        """Tur istatistiklerini güncelle"""
        async with self._lock:
            set_clauses = ", ".join(f"{k} = ?" for k in stats.keys())
            values = list(stats.values()) + [round_number]
            await self._db.execute(
                f"UPDATE rounds SET {set_clauses} WHERE round_number = ?", values
            )
            await self._db.commit()

    async def complete_round(self, round_number: int, stats: RoundStats):
        """Turu tamamla"""
        async with self._lock:
            await self._db.execute("""
                UPDATE rounds SET
                    end_time = ?,
                    total_urls_visited = ?,
                    total_items_scraped = ?,
                    total_items_stored = ?,
                    total_duplicates_found = ?,
                    total_qa_generated = ?,
                    errors_count = ?,
                    sources_completed = ?,
                    status = 'completed'
                WHERE round_number = ?
            """, (
                datetime.utcnow(),
                stats.total_urls_visited,
                stats.total_items_scraped,
                stats.total_items_stored,
                stats.total_duplicates_found,
                stats.total_qa_generated,
                stats.errors_count,
                json.dumps(stats.sources_completed),
                round_number,
            ))
            await self._db.commit()

    async def get_last_round_number(self) -> int:
        """Son tur numarasını getir"""
        cursor = await self._db.execute(
            "SELECT MAX(round_number) FROM rounds"
        )
        row = await cursor.fetchone()
        return row[0] if row[0] is not None else 0

    # =========================================================================
    # İstatistikler
    # =========================================================================

    async def get_stats(self) -> dict:
        """Genel istatistikler"""
        stats = {}
        for query, key in [
            ("SELECT COUNT(*) FROM documents", "total_documents"),
            ("SELECT COUNT(*) FROM documents WHERE is_duplicate = 0", "unique_documents"),
            ("SELECT COUNT(*) FROM documents WHERE is_duplicate = 1", "duplicate_documents"),
            ("SELECT COUNT(*) FROM qa_pairs", "total_qa_pairs"),
            ("SELECT COUNT(*) FROM visited_urls", "total_urls_visited"),
            ("SELECT COUNT(DISTINCT source_name) FROM documents", "active_sources"),
        ]:
            cursor = await self._db.execute(query)
            row = await cursor.fetchone()
            stats[key] = row[0]

        # Kaynak başına istatistik
        cursor = await self._db.execute("""
            SELECT source_name, COUNT(*) as count,
                   SUM(CASE WHEN is_duplicate = 0 THEN 1 ELSE 0 END) as unique_count,
                   SUM(qa_count) as total_qa
            FROM documents GROUP BY source_name
        """)
        rows = await cursor.fetchall()
        stats["per_source"] = {
            row["source_name"]: {
                "total": row["count"],
                "unique": row["unique_count"],
                "qa_pairs": row["total_qa"],
            }
            for row in rows
        }

        return stats

    async def close(self):
        """Veritabanı bağlantısını kapat"""
        if self._db:
            await self._db.close()
            logger.info("database_closed")
