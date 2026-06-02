"""
Data Models - Pydantic schemas
Data structures used throughout the pipeline
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, computed_field


class SourceType(str, Enum):
    FORUM = "forum"
    CONTENT = "content"
    TECHNICAL = "technical"
    QA = "qa"


class ContentCategory(str, Enum):
    FORUM_THREAD = "forum_thread"
    FORUM_POST = "forum_post"
    ARTICLE = "article"
    TECHNICAL = "technical"
    TECHNICAL_SPEC = "technical_spec"
    QA = "qa"
    QA_DISCUSSION = "qa_discussion"
    REVIEW = "review"
    COMPLAINT = "complaint"


class PipelineStage(str, Enum):
    SCRAPED = "scraped"
    STORED = "stored"
    DEDUPLICATED = "deduplicated"
    QUALITY_CHECKED = "quality_checked"
    QA_GENERATED = "qa_generated"
    FAILED = "failed"


class ScrapedItem(BaseModel):
    """Raw data from scraper"""
    source_name: str
    source_url: str
    page_url: str
    title: str = ""
    content_html: str = ""
    content_text: str = ""
    author: str = ""
    date_published: Optional[datetime] = None
    date_scraped: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    category: ContentCategory = ContentCategory.ARTICLE
    language: str = "en"
    metadata: dict[str, Any] = Field(default_factory=dict)
    parent_url: Optional[str] = None  # Forum thread URL for posts
    tags: list[str] = Field(default_factory=list)
    
    # Hash fields - can be set by scraper
    content_hash: Optional[str] = None
    url_hash: Optional[str] = None
    simhash: Optional[int] = None
    
    # Quality fields
    quality_score: Optional[float] = None
    word_count: Optional[int] = None

    def compute_content_hash(self) -> str:
        """Compute SHA-256 hash of content"""
        text = self.content_text or self.content_html
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def compute_url_hash(self) -> str:
        """Compute SHA-256 hash of URL"""
        return hashlib.sha256(self.page_url.encode("utf-8")).hexdigest()
    
    def compute_word_count(self) -> int:
        """Count words in content"""
        return len(self.content_text.split()) if self.content_text else 0


class StoredDocument(BaseModel):
    """Stored document metadata"""
    id: Optional[str] = None  # UUID
    source_name: str
    source_url: str
    page_url: str
    url_hash: str
    title: str
    content_hash: str
    simhash: Optional[int] = None
    file_path_html: Optional[str] = None
    file_path_pdf: Optional[str] = None
    file_size_bytes: int = 0
    content_length: int = 0
    word_count: int = 0
    category: ContentCategory = ContentCategory.ARTICLE
    language: str = "en"
    author: str = ""
    date_published: Optional[datetime] = None
    date_scraped: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    date_processed: Optional[datetime] = None
    pipeline_stage: PipelineStage = PipelineStage.SCRAPED
    is_duplicate: bool = False
    duplicate_of: Optional[str] = None
    quality_score: Optional[float] = None
    qa_count: int = 0
    round_number: int = 0
    metadata_json: str = "{}"
    tags: str = ""


class QAPair(BaseModel):
    """Question-Answer pair"""
    id: Optional[str] = None  # UUID
    document_id: str
    source_name: str
    question: str
    answer: str
    category: str = ""
    car_brand: str = ""
    car_model: str = ""
    car_year: str = ""
    quality_score: float = 0.0
    date_generated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    model_used: str = ""
    is_verified: bool = False
    metadata_json: str = "{}"

    @computed_field
    @property
    def qa_hash(self) -> str:
        combined = f"{self.question}|{self.answer}"
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()


class SourceConfig(BaseModel):
    """Source configuration"""
    name: str
    type: SourceType
    enabled: bool = True
    base_url: str
    start_urls: list[str] = Field(default_factory=list)
    category: str = "general"
    priority: int = 1
    rate_limit: float = 5.0
    scrape_interval_hours: int = 12
    max_pages_per_round: int = 500
    pagination_type: str = "page_number"
    use_playwright: bool = False
    cloudflare_protected: bool = False
    selectors: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RoundStats(BaseModel):
    """Round statistics"""
    round_number: int
    start_time: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    end_time: Optional[datetime] = None
    total_urls_visited: int = 0
    total_items_scraped: int = 0
    total_items_stored: int = 0
    total_duplicates_found: int = 0
    total_quality_filtered: int = 0
    total_qa_generated: int = 0
    errors_count: int = 0
    sources_completed: list[str] = Field(default_factory=list)
    status: str = "running"  # running, completed, failed


class HealthStatus(BaseModel):
    """System health status"""
    is_healthy: bool = True
    uptime_seconds: float = 0
    current_round: int = 0
    total_documents: int = 0
    unique_documents: int = 0
    total_qa_pairs: int = 0
    disk_usage_percent: float = 0
    disk_free_gb: float = 0
    database_connected: bool = True
    redis_connected: bool = True
    ollama_connected: bool = False
    active_scrapers: int = 0
    last_error: Optional[str] = None
    last_scrape_time: Optional[datetime] = None
    sources_status: dict[str, str] = Field(default_factory=dict)
