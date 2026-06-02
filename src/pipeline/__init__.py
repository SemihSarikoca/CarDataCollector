"""
Pipeline Orchestrator
Tüm pipeline aşamalarını koordine eder:
  Scrape → Store → Deduplicate → Generate Q/A
  
Sürekli çalışır, turlar halinde tekrar eder.
"""

import asyncio
import signal
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.cache import RedisManager
from src.database import DatabaseManager
from src.dedup import DeduplicationEngine
from src.models import PipelineStage, RoundStats, SourceConfig, SourceType
from src.qa_generator import QAGenerator
from src.scrapers.base_scraper import BaseScraper
from src.scrapers.sources.generic import GenericContentScraper, GenericForumScraper
from src.scrapers.sources.reddit_api import RedditAPIScraper
from src.scrapers.sources.nhtsa_api import NHTSAAPIScraper
from src.storage import StorageManager
from src.utils.helpers import get_disk_usage
from src.utils.logger import get_logger

logger = get_logger("orchestrator")

# Kaynak adı → özel scraper eşleşmesi
CUSTOM_SCRAPERS = {
    "reddit_mechanicadvice": RedditAPIScraper,
    "reddit_cartalk": RedditAPIScraper,
    "reddit_justrolledintotheshop": RedditAPIScraper,
    "reddit_wrenching": RedditAPIScraper,
    "reddit_automechanic": RedditAPIScraper,
    "reddit_cars": RedditAPIScraper,
    "reddit_askmechanics": RedditAPIScraper,
    "reddit_trucks": RedditAPIScraper,
    "reddit_diyauto": RedditAPIScraper,
    "nhtsa": NHTSAAPIScraper,
}


class PipelineOrchestrator:
    """
    Ana pipeline yöneticisi.
    Turlar halinde çalışır:
    1. Tüm kaynaklardan scrape
    2. Depolama ve duplikasyon kontrolü
    3. Q/A üretimi
    4. Tur tamamlanınca bekleme, sonra tekrar
    """

    def __init__(self, config: dict):
        self.config = config
        self._running = False
        self._start_time: Optional[float] = None
        self._current_round = 0

        # Bileşenleri oluştur (Postgres + Redis)
        self.db = DatabaseManager(config)
        self.cache = RedisManager(config)
        self.dedup = DeduplicationEngine(self.db, config, cache=self.cache)
        self.storage = StorageManager(config, self.db, self.dedup)
        self.qa_generator = QAGenerator(config, self.db, self.storage)

        # Pipeline ayarları
        pipeline_config = config.get("pipeline", {})
        self.max_concurrent_scrapers = pipeline_config.get("stages", [{}])[0].get("max_concurrent", 4)
        self.auto_restart = pipeline_config.get("auto_restart", True)
        self.error_backoff = pipeline_config.get("error_backoff_seconds", 300)
        self.health_check_interval = pipeline_config.get("health_check_interval", 60)

        general_config = config.get("general", {})
        self.round_delay = general_config.get("round_delay_seconds", 3600)
        self.max_workers = general_config.get("max_workers", 4)

        # Q/A için minimum döküman eşiği
        qa_stages = [s for s in pipeline_config.get("stages", []) if s.get("name") == "generate_qa"]
        self.min_docs_for_qa = qa_stages[0].get("min_documents_to_start", 100) if qa_stages else 100

    async def initialize(self):
        """Tüm bileşenleri başlat"""
        await self.db.initialize()
        await self.cache.initialize()
        await self.storage.initialize()
        await self._sync_sources_to_db()
        cleaned = await self.db.cleanup_stale_rounds()
        if cleaned:
            logger.info("stale_rounds_cleaned", count=cleaned)
        self._current_round = await self.db.get_last_round_number()
        logger.info("orchestrator_initialized",
                   last_round=self._current_round,
                   redis=self.cache.connected)

    async def _sync_sources_to_db(self):
        """Settings.yaml'deki kaynakları sources tablosuna upsert et."""
        for src in self.config.get("sources", []):
            try:
                await self.db.upsert_source(
                    name=src.get("name"),
                    source_type=src.get("type", "content"),
                    base_url=src.get("base_url", ""),
                    enabled=bool(src.get("enabled", True)),
                    priority=int(src.get("priority", 5)),
                    rate_limit=float(src.get("rate_limit", 5.0)),
                    scrape_interval_hours=int(src.get("scrape_interval_hours", 12)),
                )
            except Exception as e:
                logger.warning("source_sync_failed",
                               name=src.get("name"), error=str(e))

    def _create_scraper(self, source_config: SourceConfig) -> BaseScraper:
        """Kaynak konfigürasyonuna göre uygun scraper oluştur"""
        # Özel scraper var mı?
        if source_config.name in CUSTOM_SCRAPERS:
            return CUSTOM_SCRAPERS[source_config.name](source_config, self.config)

        # Genel scraper kullan
        if source_config.type == SourceType.FORUM:
            return GenericForumScraper(source_config, self.config)
        else:
            return GenericContentScraper(source_config, self.config)

    def _get_enabled_sources(self) -> list[SourceConfig]:
        sources = []
        for src_dict in self.config.get("sources", []):
            if src_dict.get("enabled", True):
                sources.append(SourceConfig(**src_dict))
        sources.sort(key=lambda s: s.priority)
        return sources

    async def _get_due_sources(self) -> list[SourceConfig]:
        """Return enabled sources whose scrape_interval_hours has elapsed since last run.

        Uses the DB as the authoritative source for the `enabled` flag so that
        dashboard toggles take effect without a pipeline restart.
        """
        all_sources = self._get_enabled_sources()
        if not all_sources:
            return []

        # Filter by DB-authoritative enabled state (dashboard may have toggled them)
        db_states = await self.db.get_source_enabled_states([s.name for s in all_sources])
        all_sources = [s for s in all_sources if db_states.get(s.name, True)]
        if not all_sources:
            return []

        names = [s.name for s in all_sources]
        last_scrape_times = await self.db.get_last_scrape_times(names)

        now = datetime.now(timezone.utc)
        due, skipped = [], []
        for source in all_sources:
            last = last_scrape_times.get(source.name)
            if last is None:
                due.append(source)
            else:
                next_due = last + timedelta(hours=source.scrape_interval_hours)
                if now >= next_due:
                    due.append(source)
                else:
                    wait_mins = int((next_due - now).total_seconds() / 60)
                    skipped.append((source.name, wait_mins))

        if skipped:
            logger.info("sources_skipped_not_due",
                        count=len(skipped),
                        sources=[{"name": n, "ready_in_minutes": m} for n, m in skipped])
        return due

    async def _scrape_source(self, source_config: SourceConfig, round_number: int) -> dict:
        stats = {"source": source_config.name, "items": 0, "stored": 0, "errors": 0}
        log_id = await self.db.log_scrape_start(source_config.name, round_number)
        scraper = self._create_scraper(source_config)

        try:
            logger.info("source_scrape_start", source=source_config.name, round=round_number)

            async for item in scraper.run():
                try:
                    doc = await self.storage.store_item(item, round_number)
                    stats["items"] += 1
                    if doc:
                        stats["stored"] += 1
                except Exception as e:
                    stats["errors"] += 1
                    logger.error("store_error", source=source_config.name,
                                 url=item.page_url, error=str(e))

            logger.info("source_scrape_complete", **stats)

        except Exception as e:
            stats["errors"] += 1
            logger.error("source_scrape_failed", source=source_config.name, error=str(e))

        finally:
            await self.db.log_scrape_complete(log_id, stats)

        return stats

    async def run_scrape_round(self, round_number: int) -> dict:
        """
        Bir tur scraping çalıştır.
        Kaynakları paralel olarak (semaphore ile sınırlı) scrape eder.
        """
        round_stats = {
            "total_items": 0,
            "total_stored": 0,
            "total_errors": 0,
            "sources_completed": [],
        }

        sources = await self._get_due_sources()
        if not sources:
            logger.info("no_sources_due", round=round_number)
            return round_stats

        logger.info("scrape_round_start", round=round_number,
                    source_count=len(sources))

        # Eşzamanlılık kontrolü
        semaphore = asyncio.Semaphore(self.max_concurrent_scrapers)

        async def bounded_scrape(src):
            async with semaphore:
                return await self._scrape_source(src, round_number)

        # Tüm kaynakları paralel çalıştır
        tasks = [bounded_scrape(src) for src in sources]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                round_stats["total_errors"] += 1
                logger.error("scrape_task_exception", error=str(result))
            elif isinstance(result, dict):
                round_stats["total_items"] += result.get("items", 0)
                round_stats["total_stored"] += result.get("stored", 0)
                round_stats["total_errors"] += result.get("errors", 0)
                round_stats["sources_completed"].append(result.get("source", ""))

        logger.info("scrape_round_complete", round=round_number, **round_stats)
        return round_stats

    async def run_dedup_round(self) -> dict:
        """Toplu duplikasyon kontrolü çalıştır"""
        logger.info("dedup_round_start")
        stats = await self.dedup.run_batch_dedup()
        logger.info("dedup_round_complete", **stats)
        return stats

    async def run_qa_round(self) -> dict:
        """Q/A üretim turu"""
        # Yeterli döküman var mı?
        total_docs = await self.db.get_total_unique_documents()
        if total_docs < self.min_docs_for_qa:
            logger.info("not_enough_docs_for_qa", total=total_docs,
                       required=self.min_docs_for_qa)
            return {"skipped": True, "reason": "not_enough_docs"}

        logger.info("qa_round_start", total_docs=total_docs)
        stats = await self.qa_generator.process_batch()
        return stats

    async def run_single_round(self) -> RoundStats:
        """Run a full pipeline round.

        Scraping and Q/A generation run in parallel:
        - Scraping fetches new documents from sources.
        - Q/A processes documents that were already pending from previous rounds.
        - Dedup runs after scraping finishes to clean up the newly stored documents.
        """
        self._current_round += 1
        round_number = self._current_round

        round_stats = RoundStats(round_number=round_number)
        await self.db.start_round(round_number)

        try:
            # Stage 1 + 3 in parallel: scrape new data AND generate Q/A on existing pending docs
            scrape_task = asyncio.create_task(self.run_scrape_round(round_number))
            qa_task     = asyncio.create_task(self.run_qa_round())

            results = await asyncio.gather(scrape_task, qa_task, return_exceptions=True)
            scrape_stats, qa_result = results

            if isinstance(scrape_stats, Exception):
                raise scrape_stats

            round_stats.total_items_scraped  = scrape_stats["total_items"]
            round_stats.total_items_stored   = scrape_stats["total_stored"]
            round_stats.errors_count         = scrape_stats["total_errors"]
            round_stats.sources_completed    = scrape_stats["sources_completed"]

            if isinstance(qa_result, Exception):
                logger.error("qa_parallel_failed", round=round_number, error=str(qa_result))
                qa_stats = {}
            else:
                qa_stats = qa_result

            round_stats.total_qa_generated = qa_stats.get("qa_generated", 0)

            # Stage 2: dedup runs after scraping so new documents are included
            dedup_stats = await self.run_dedup_round()
            round_stats.total_duplicates_found = dedup_stats.get("duplicates_found", 0)

            round_stats.status = "completed"

        except Exception as e:
            round_stats.status = "failed"
            logger.error("round_failed", round=round_number, error=str(e))

        round_stats.end_time = datetime.now(timezone.utc)
        await self.db.complete_round(round_number, round_stats)

        # İstatistikleri logla
        db_stats = await self.db.get_stats()
        logger.info(
            "round_summary",
            round=round_number,
            status=round_stats.status,
            items_scraped=round_stats.total_items_scraped,
            items_stored=round_stats.total_items_stored,
            duplicates=round_stats.total_duplicates_found,
            qa_generated=round_stats.total_qa_generated,
            total_documents=db_stats.get("total_documents", 0),
            total_unique=db_stats.get("unique_documents", 0),
            total_qa=db_stats.get("total_qa_pairs", 0),
        )

        return round_stats

    async def run_scrape_only_continuous(self):
        """Scrape-only 7/24 loop — fetches + stores + deduplicates, no Q/A generation."""
        self._running = True
        self._start_time = time.time()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: self._shutdown())

        logger.info("scrape_loop_started", round_delay=self.round_delay)

        while self._running:
            try:
                self._current_round += 1
                round_number = self._current_round
                await self.db.start_round(round_number)

                scrape_stats = await self.run_scrape_round(round_number)
                dedup_stats = await self.run_dedup_round()

                round_stats = RoundStats(round_number=round_number)
                round_stats.total_items_scraped = scrape_stats["total_items"]
                round_stats.total_items_stored = scrape_stats["total_stored"]
                round_stats.errors_count = scrape_stats["total_errors"]
                round_stats.sources_completed = scrape_stats["sources_completed"]
                round_stats.total_duplicates_found = dedup_stats.get("duplicates_found", 0)
                round_stats.status = "completed"
                round_stats.end_time = datetime.now(timezone.utc)
                await self.db.complete_round(round_number, round_stats)

                if not self._running:
                    break

                logger.info("scrape_loop_waiting", delay_seconds=self.round_delay)
                await asyncio.sleep(self.round_delay)

            except asyncio.CancelledError:
                logger.info("scrape_loop_cancelled")
                break
            except Exception as e:
                logger.error("scrape_loop_error", error=str(e))
                if self.auto_restart:
                    logger.info("scrape_loop_backoff", seconds=self.error_backoff)
                    await asyncio.sleep(self.error_backoff)
                else:
                    break

        await self._cleanup()

    async def run_qa_only_continuous(self):
        """Q/A-only 7/24 loop — continuously processes pending documents through Ollama."""
        self._running = True
        self._start_time = time.time()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: self._shutdown())

        logger.info("qa_loop_started", min_docs=self.min_docs_for_qa)

        while self._running:
            try:
                stats = await self.run_qa_round()

                if not self._running:
                    break

                if stats.get("skipped"):
                    await asyncio.sleep(60)
                elif not stats.get("qa_generated"):
                    await asyncio.sleep(30)
                # else: loop immediately for next batch

            except asyncio.CancelledError:
                logger.info("qa_loop_cancelled")
                break
            except Exception as e:
                logger.error("qa_loop_error", error=str(e))
                await asyncio.sleep(self.error_backoff)

        await self._cleanup()

    async def run_continuous(self):
        """
        Sürekli pipeline döngüsü - 7/24 çalışır.
        Her tur sonrası configurable bekleme süresi.
        """
        self._running = True
        self._start_time = time.time()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: self._shutdown())

        logger.info("continuous_pipeline_started",
                   round_delay=self.round_delay,
                   max_workers=self.max_workers)

        while self._running:
            try:
                round_stats = await self.run_single_round()

                if not self._running:
                    break

                # Tur arası bekleme
                logger.info("round_waiting", delay_seconds=self.round_delay,
                           next_round=self._current_round + 1)
                await asyncio.sleep(self.round_delay)

            except asyncio.CancelledError:
                logger.info("pipeline_cancelled")
                break
            except Exception as e:
                logger.error("pipeline_error", error=str(e))
                if self.auto_restart:
                    logger.info("auto_restart_backoff", seconds=self.error_backoff)
                    await asyncio.sleep(self.error_backoff)
                else:
                    break

        await self._cleanup()

    def _shutdown(self):
        """Graceful shutdown"""
        logger.info("shutdown_requested")
        self._running = False

    async def _cleanup(self):
        """Temizlik"""
        await self.qa_generator.close()
        await self.cache.close()
        await self.db.close()
        logger.info("pipeline_stopped",
                   total_rounds=self._current_round,
                   uptime_seconds=time.time() - (self._start_time or time.time()))

    async def get_health(self) -> dict:
        """Sistem sağlık durumu"""
        from src.models import HealthStatus
        
        db_stats = await self.db.get_stats()
        disk = get_disk_usage(str(self.storage.base_path))
        
        return {
            "is_healthy": self._running,
            "uptime_seconds": time.time() - (self._start_time or time.time()),
            "current_round": self._current_round,
            "total_documents": db_stats.get("total_documents", 0),
            "unique_documents": db_stats.get("unique_documents", 0),
            "total_qa_pairs": db_stats.get("total_qa_pairs", 0),
            "disk_usage_percent": disk.get("percent", 0),
            "disk_free_gb": disk.get("free_gb", 0),
            "per_source": db_stats.get("per_source", {}),
        }
