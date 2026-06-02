"""
Depolama Yöneticisi
Ham HTML/PDF dosyalarını diske yazar, dosya yollarını yönetir.
Disk doluluk takibi yapar.
"""

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiofiles
from langdetect import detect as _langdetect, DetectorFactory, LangDetectException

DetectorFactory.seed = 0  # deterministic results across runs

from src.models import ContentCategory, PipelineStage, ScrapedItem, StoredDocument
from src.database import DatabaseManager
from src.dedup import DeduplicationEngine
from src.utils.helpers import ensure_dir, get_disk_usage, sanitize_filename
from src.utils.logger import get_logger

logger = get_logger("storage")


class StorageManager:
    """
    Dosya depolama yönetimi.
    Ham verileri HTML/PDF olarak kaydeder.
    Disk doluluk takibi yapar.
    """

    def __init__(self, config: dict, db: DatabaseManager, dedup: DeduplicationEngine):
        self.config = config
        self.db = db
        self.dedup = dedup

        storage_config = config.get("storage", {})
        self.base_path = Path(storage_config.get("base_path", "/data/car-collector"))
        self.raw_html_dir = self.base_path / storage_config.get("raw_html_dir", "raw/html")
        self.raw_pdf_dir = self.base_path / storage_config.get("raw_pdf_dir", "raw/pdf")
        self.processed_dir = self.base_path / storage_config.get("processed_dir", "processed")
        self.temp_dir = self.base_path / storage_config.get("temp_dir", "temp")
        self.max_file_size_mb = storage_config.get("max_file_size_mb", 50)
        self.disk_warning_threshold = storage_config.get("disk_warning_threshold_percent", 97)
        self.disk_minimum_free_gb = storage_config.get("disk_minimum_free_gb", 2)

        quality_config = config.get("quality", {})
        self.enforce_english = quality_config.get("enforce_english", True)
        self.min_content_length = quality_config.get("min_content_length", 100)
        self.min_word_count = quality_config.get("min_word_count", 20)

    async def initialize(self):
        """Dizinleri oluştur"""
        for d in [self.raw_html_dir, self.raw_pdf_dir, self.processed_dir, self.temp_dir]:
            ensure_dir(str(d))
        logger.info("storage_initialized", base_path=str(self.base_path))

    def _get_source_dir(self, source_name: str, file_type: str = "html") -> Path:
        """Kaynak adına göre dizin yolu"""
        base = self.raw_html_dir if file_type == "html" else self.raw_pdf_dir
        return ensure_dir(str(base / source_name))

    def _generate_filename(self, item: ScrapedItem) -> str:
        """Benzersiz dosya adı oluştur"""
        # URL hash + sanitize edilmiş başlık
        url_hash_short = item.url_hash[:12]
        safe_title = sanitize_filename(item.title, max_length=80)
        timestamp = item.date_scraped.strftime("%Y%m%d_%H%M%S")
        return f"{timestamp}_{url_hash_short}_{safe_title}"

    def _detect_language(self, text: str) -> str:
        """Return ISO-639-1 language code; falls back to 'en' on short or ambiguous text."""
        if not text or len(text) < 50:
            return "en"
        try:
            return _langdetect(text[:2000])
        except LangDetectException:
            return "en"

    async def check_disk_space(self) -> bool:
        """Disk alanını kontrol et. Yetersizse False döndür."""
        usage = get_disk_usage(str(self.base_path))
        too_full = usage["percent"] >= self.disk_warning_threshold
        too_low = usage["free_gb"] < self.disk_minimum_free_gb
        if too_full or too_low:
            logger.warning(
                "disk_space_low",
                path=str(self.base_path),
                used_percent=usage["percent"],
                free_gb=usage["free_gb"],
            )
            return False
        return True

    async def _handle_content_update(self, existing: dict, item: ScrapedItem,
                                     round_number: int) -> None:
        """Re-scrape detected a content change: overwrite file, update DB, delete stale Q/A."""
        doc_id = existing["id"]
        html_content = self._wrap_html(item)
        content_bytes = html_content.encode("utf-8")

        filename = self._generate_filename(item)
        source_dir = self._get_source_dir(item.source_name, "html")
        html_path = source_dir / f"{filename}.html"

        async with aiofiles.open(html_path, "w", encoding="utf-8") as f:
            await f.write(html_content)

        word_count = len(item.content_text.split()) if item.content_text else 0
        await self.db.update_document_content(doc_id, {
            "content_hash":    item.content_hash,
            "content_text":    item.content_text or "",
            "content_html":    item.content_html,
            "word_count":      word_count,
            "content_length":  len(item.content_text or ""),
            "file_path_html":  str(html_path),
            "file_size_bytes": len(content_bytes),
            "round_number":    round_number,
            "pipeline_stage":  PipelineStage.STORED.value,
        })

        simhash_hex = await self.dedup.compute_and_store_simhash(doc_id, item.content_text)
        if simhash_hex:
            await self.db.execute(
                "UPDATE scraped_data SET simhash = $1 WHERE id = $2", simhash_hex, doc_id
            )

        await self.db.mark_url_visited(
            item.url_hash, item.page_url, item.source_name, item.content_hash
        )
        if self.dedup.cache:
            await self.dedup.cache.mark_url_visited(item.url_hash, item.content_hash)
            await self.dedup.cache.set_content_hash_doc(item.content_hash, doc_id)

        logger.info("content_updated", url=item.page_url, doc_id=doc_id)

    async def store_item(self, item: ScrapedItem, round_number: int = 0) -> Optional[StoredDocument]:
        """
        Store a scraped item:
        1. Quality / language gate
        2. Content-change check (re-scrape): update document + delete stale Q/A
        3. Deduplication check for new URLs
        4. Write HTML to disk and insert DB row
        5. Compute SimHash
        """
        # Disk check
        if not await self.check_disk_space():
            logger.error("disk_full_skipping_item", url=item.page_url)
            return None

        # Ensure hashes are computed
        if not item.content_hash:
            item.content_hash = item.compute_content_hash()
        if not item.url_hash:
            item.url_hash = item.compute_url_hash()

        # Language detection + quality gate
        text = item.content_text or ""
        item.language = self._detect_language(text)
        if self.enforce_english and item.language != "en":
            logger.debug("non_english_skipped", url=item.page_url,
                         language=item.language, source=item.source_name)
            return None
        if len(text) < self.min_content_length or len(text.split()) < self.min_word_count:
            logger.debug("content_too_short_skipped", url=item.page_url,
                         chars=len(text), words=len(text.split()))
            return None

        # Content-change check: if this URL was previously stored with DIFFERENT content,
        # update the existing document and delete its now-stale Q/A pairs.
        if item.url_hash:
            existing = await self.db.get_document_by_url_hash(item.url_hash)
            if existing and not existing.get("is_duplicate"):
                if existing.get("content_hash") == item.content_hash:
                    return None  # unchanged — nothing to do
                await self._handle_content_update(existing, item, round_number)
                return None  # updated in-place, no new document created

        # Deduplication check for new URLs
        is_dup, dup_of, method = await self.dedup.check_duplicate(
            item.content_text, item.content_hash, item.url_hash
        )

        if is_dup:
            logger.debug("duplicate_skipped", url=item.page_url, method=method,
                        duplicate_of=dup_of)
            # Yine de DB'ye kaydet (duplikat olarak)
            doc = StoredDocument(
                source_name=item.source_name,
                source_url=item.source_url,
                page_url=item.page_url,
                url_hash=item.url_hash,
                title=item.title,
                content_hash=item.content_hash,
                category=item.category,
                author=item.author,
                date_published=item.date_published,
                date_scraped=item.date_scraped,
                pipeline_stage=PipelineStage.DEDUPLICATED,
                is_duplicate=True,
                duplicate_of=dup_of,
                round_number=round_number,
                metadata_json=json.dumps(item.metadata, default=str),
                tags=",".join(item.tags),
            )
            await self.db.insert_document(doc)
            return None

        # HTML olarak kaydet
        filename = self._generate_filename(item)
        source_dir = self._get_source_dir(item.source_name, "html")
        html_path = source_dir / f"{filename}.html"

        html_content = self._wrap_html(item)

        # Boyut kontrolü
        content_bytes = html_content.encode("utf-8")
        if len(content_bytes) > self.max_file_size_mb * 1024 * 1024:
            logger.warning("file_too_large", url=item.page_url,
                          size_mb=len(content_bytes) / (1024 * 1024))
            return None

        async with aiofiles.open(html_path, "w", encoding="utf-8") as f:
            await f.write(html_content)

        # DB'ye yaz - StoredDocument'i kullanmıyoruz çünkü Postgres'in tüm
        # alanları doğrudan kabul etmesi daha basit. Gerekli kısa-yol:
        word_count = len(item.content_text.split()) if item.content_text else 0

        doc = StoredDocument(
            source_name=item.source_name,
            source_url=item.source_url,
            page_url=item.page_url,
            url_hash=item.url_hash,
            title=item.title,
            content_hash=item.content_hash,
            file_path_html=str(html_path),
            file_size_bytes=len(content_bytes),
            content_length=len(item.content_text),
            word_count=word_count,
            category=item.category,
            language=item.language or "en",
            author=item.author,
            date_published=item.date_published,
            date_scraped=item.date_scraped,
            pipeline_stage=PipelineStage.STORED,
            round_number=round_number,
            metadata_json=json.dumps(item.metadata, default=str),
            tags=",".join(item.tags),
        )

        doc_id = await self.db.insert_document(doc)

        if doc_id:
            # Write content_text/html for search/preview
            await self.db.execute("""
                UPDATE scraped_data SET content_text = $1, content_html = $2
                WHERE id = $3
            """, item.content_text or "", item.content_html or None, doc_id)

            # Compute SimHash and index it
            simhash_hex = await self.dedup.compute_and_store_simhash(
                doc_id, item.content_text
            )
            if simhash_hex:
                await self.db.execute(
                    "UPDATE scraped_data SET simhash = $1 WHERE id = $2",
                    simhash_hex, doc_id,
                )
            await self.db.update_document_stage(doc_id, PipelineStage.DEDUPLICATED)

            # URL'yi ziyaret edilmiş olarak işaretle (DB + Redis)
            await self.db.mark_url_visited(
                item.url_hash, item.page_url, item.source_name, item.content_hash
            )
            if self.dedup.cache:
                await self.dedup.cache.mark_url_visited(item.url_hash, item.content_hash)
                await self.dedup.cache.set_content_hash_doc(item.content_hash, doc_id)

            doc.id = doc_id
            doc.simhash = simhash_hex
            logger.info("item_stored", source=item.source_name,
                       title=item.title[:50], doc_id=doc_id)
            return doc

        return None

    def _wrap_html(self, item: ScrapedItem) -> str:
        """ScrapedItem'ı tam HTML dökümanına dönüştür"""
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="source" content="{item.source_name}">
    <meta name="source-url" content="{item.page_url}">
    <meta name="scraped-date" content="{item.date_scraped.isoformat()}">
    <meta name="author" content="{item.author}">
    <meta name="category" content="{item.category.value}">
    <title>{item.title}</title>
</head>
<body>
    <article>
        <h1>{item.title}</h1>
        <div class="metadata">
            <span class="source">{item.source_name}</span>
            <span class="author">{item.author}</span>
            <span class="date">{item.date_published or ''}</span>
            <span class="url">{item.page_url}</span>
        </div>
        <div class="content">
            {item.content_html if item.content_html else '<p>' + item.content_text.replace(chr(10), '</p><p>') + '</p>'}
        </div>
    </article>
</body>
</html>"""

    async def get_document_text(self, file_path: str) -> str:
        """Kaydedilmiş HTML dosyasından metin çıkar"""
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                html_content = await f.read()

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html_content, "lxml")
            content_div = soup.select_one("div.content")
            if content_div:
                return content_div.get_text(separator="\n").strip()
            return soup.get_text(separator="\n").strip()
        except Exception as e:
            logger.error("read_document_error", path=file_path, error=str(e))
            return ""

    async def get_storage_stats(self) -> dict:
        """Depolama istatistikleri"""
        disk = get_disk_usage(str(self.base_path))

        # Dosya sayıları
        html_count = sum(1 for _ in self.raw_html_dir.rglob("*.html"))
        total_size = sum(f.stat().st_size for f in self.raw_html_dir.rglob("*.html"))

        return {
            "disk": disk,
            "html_files": html_count,
            "total_size_gb": round(total_size / (1024**3), 3),
            "base_path": str(self.base_path),
        }
