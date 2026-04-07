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
        self.disk_warning_threshold = storage_config.get("disk_warning_threshold_percent", 90)

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

    async def check_disk_space(self) -> bool:
        """Disk alanını kontrol et. Yetersizse False döndür."""
        usage = get_disk_usage(str(self.base_path))
        if usage["percent"] >= self.disk_warning_threshold:
            logger.warning(
                "disk_space_low",
                path=str(self.base_path),
                used_percent=usage["percent"],
                free_gb=usage["free_gb"],
            )
            return False
        return True

    async def store_item(self, item: ScrapedItem, round_number: int = 0) -> Optional[StoredDocument]:
        """
        Scraped item'ı depola:
        1. Duplikasyon kontrolü
        2. HTML olarak kaydet
        3. Veritabanına metadata yaz
        4. SimHash hesapla ve indeksle
        """
        # Disk kontrolü
        if not await self.check_disk_space():
            logger.error("disk_full_skipping_item", url=item.page_url)
            return None

        # Duplikasyon kontrolü
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

        # Veritabanına kaydet
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
            category=item.category,
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
            # SimHash hesapla ve indeksle
            simhash_hex = await self.dedup.compute_and_store_simhash(
                doc_id, item.content_text
            )
            # Aşamayı güncelle
            await self.db.update_document_stage(doc_id, PipelineStage.DEDUPLICATED)

            # URL'yi ziyaret edilmiş olarak işaretle
            await self.db.mark_url_visited(
                item.url_hash, item.page_url, item.source_name, item.content_hash
            )

            doc.id = doc_id
            doc.simhash = simhash_hex
            logger.info("item_stored", source=item.source_name,
                       title=item.title[:50], doc_id=doc_id)
            return doc

        return None

    def _wrap_html(self, item: ScrapedItem) -> str:
        """ScrapedItem'ı tam HTML dökümanına dönüştür"""
        return f"""<!DOCTYPE html>
<html lang="tr">
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
