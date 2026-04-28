"""
NHTSA API Scraper.

Pulls complaints + recalls + investigations from the official NHTSA API
(https://api.nhtsa.gov/) — no scraping, no anti-bot, just JSON.

The vehicles to query are read from `metadata.vehicles` in the source config:
    metadata:
      vehicles:
        - {make: Toyota, model: Camry, year: 2018}
        - {make: Honda,  model: Civic,  year: 2020}

If `metadata.vehicles` is missing, a small built-in default list is used.
"""

import asyncio
from datetime import datetime
from typing import AsyncGenerator, Optional

import httpx

from src.models import ContentCategory, ScrapedItem, SourceConfig
from src.scrapers.base_scraper import BaseScraper
from src.utils.helpers import clean_text
from src.utils.logger import get_logger

logger = get_logger("scraper.nhtsa")

NHTSA_BASE = "https://api.nhtsa.gov"

DEFAULT_VEHICLES = [
    {"make": "Toyota", "model": "Camry",   "year": 2018},
    {"make": "Toyota", "model": "Corolla", "year": 2019},
    {"make": "Toyota", "model": "RAV4",    "year": 2020},
    {"make": "Honda",  "model": "Civic",   "year": 2018},
    {"make": "Honda",  "model": "Accord",  "year": 2019},
    {"make": "Honda",  "model": "CR-V",    "year": 2020},
    {"make": "Ford",   "model": "F-150",   "year": 2018},
    {"make": "Ford",   "model": "Escape",  "year": 2019},
    {"make": "Ford",   "model": "Explorer","year": 2020},
    {"make": "Chevrolet", "model": "Silverado", "year": 2018},
    {"make": "Chevrolet", "model": "Equinox",   "year": 2019},
    {"make": "Nissan", "model": "Altima",  "year": 2018},
    {"make": "Nissan", "model": "Rogue",   "year": 2019},
    {"make": "Hyundai","model": "Elantra", "year": 2018},
    {"make": "Hyundai","model": "Sonata",  "year": 2019},
    {"make": "Kia",    "model": "Optima",  "year": 2018},
    {"make": "Subaru", "model": "Outback", "year": 2019},
    {"make": "Jeep",   "model": "Wrangler","year": 2018},
    {"make": "BMW",    "model": "328i",    "year": 2014},
    {"make": "Volkswagen","model": "Jetta", "year": 2018},
]


class NHTSAAPIScraper(BaseScraper):
    """
    NHTSA scraper using the official JSON API.
    Yields one ScrapedItem per (vehicle, dataset) — combining complaints,
    recalls and investigations into structured text suitable for downstream Q/A.
    """

    def __init__(self, source_config: SourceConfig, global_config: dict):
        super().__init__(source_config, global_config)
        self.use_playwright = False
        self.cloudflare_protected = False

        meta = getattr(source_config, "metadata", None) or {}
        self.vehicles = meta.get("vehicles") or DEFAULT_VEHICLES

    async def scrape_listing(self, url: str) -> list[str]:
        return []

    async def scrape_item(self, url: str) -> Optional[ScrapedItem]:
        return None

    async def run(self) -> AsyncGenerator[ScrapedItem, None]:
        await self.start()
        timeout = httpx.Timeout(30.0)
        max_items = self.config.max_pages_per_round
        emitted = 0

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                for vehicle in self.vehicles:
                    if not self._is_running or emitted >= max_items:
                        break

                    make = vehicle["make"]
                    model = vehicle["model"]
                    year = vehicle["year"]

                    items = await self._fetch_vehicle(client, make, model, year)
                    for item in items:
                        if emitted >= max_items:
                            break
                        item.content_hash = self.get_content_hash(item.content_text)
                        item.url_hash = self.get_url_hash(item.page_url)
                        self._items_scraped += 1
                        self._pages_scraped += 1
                        emitted += 1
                        yield item
        finally:
            await self.stop()

    async def _fetch_vehicle(self, client: httpx.AsyncClient, make: str, model: str, year: int) -> list[ScrapedItem]:
        """Fetch complaints + recalls + investigations for a single vehicle."""
        params = {"make": make, "model": model, "modelYear": year}
        endpoints = {
            "complaints":     f"{NHTSA_BASE}/complaints/complaintsByVehicle",
            "recalls":        f"{NHTSA_BASE}/recalls/recallsByVehicle",
            "investigations": f"{NHTSA_BASE}/investigations/investigationsByVehicle",
        }

        async def _get(kind: str, url: str):
            async with self.rate_limiter:
                try:
                    resp = await client.get(url, params=params)
                    if resp.status_code != 200:
                        logger.warning("nhtsa_status", kind=kind, status=resp.status_code,
                                       make=make, model=model, year=year)
                        return []
                    return resp.json().get("results", []) or []
                except httpx.HTTPError as e:
                    logger.warning("nhtsa_http_error", kind=kind, error=str(e))
                    return []

        results = await asyncio.gather(*(_get(k, u) for k, u in endpoints.items()))
        complaints, recalls, investigations = results

        items: list[ScrapedItem] = []
        if complaints:
            items.append(self._build_item(make, model, year, "complaints", complaints))
        if recalls:
            items.append(self._build_item(make, model, year, "recalls", recalls))
        if investigations:
            items.append(self._build_item(make, model, year, "investigations", investigations))
        return items

    def _build_item(self, make: str, model: str, year: int, kind: str, records: list[dict]) -> ScrapedItem:
        title = f"{year} {make} {model} — NHTSA {kind} ({len(records)} records)"
        body_lines = [title, ""]

        for r in records[:200]:  # cap to keep documents reasonable
            if kind == "complaints":
                body_lines.append(self._format_complaint(r))
            elif kind == "recalls":
                body_lines.append(self._format_recall(r))
            else:
                body_lines.append(self._format_investigation(r))
            body_lines.append("")

        content_text = clean_text("\n".join(body_lines))
        page_url = (
            f"{NHTSA_BASE}/{kind}/{kind}ByVehicle"
            f"?make={make}&model={model}&modelYear={year}"
        )
        category = (
            ContentCategory.COMPLAINT if kind == "complaints"
            else ContentCategory.TECHNICAL
        )

        return ScrapedItem(
            source_name=self.name,
            source_url=f"{NHTSA_BASE}/{kind}/{kind}ByVehicle",
            page_url=page_url,
            title=title,
            content_text=content_text,
            content_html="",
            author="NHTSA",
            date_published=datetime.utcnow(),
            category=category,
            language="en",
            metadata={
                "make": make,
                "model": model,
                "year": year,
                "kind": kind,
                "record_count": len(records),
            },
        )

    @staticmethod
    def _format_complaint(r: dict) -> str:
        date = r.get("dateComplaintFiled") or r.get("incidentDate") or ""
        component = r.get("components") or "Unknown"
        summary = r.get("summary") or ""
        return f"[{date}] Component: {component}\n{summary}"

    @staticmethod
    def _format_recall(r: dict) -> str:
        campaign = r.get("NHTSACampaignNumber") or ""
        component = r.get("Component") or ""
        summary = r.get("Summary") or ""
        consequence = r.get("Consequence") or ""
        remedy = r.get("Remedy") or ""
        return (
            f"Campaign: {campaign} | Component: {component}\n"
            f"Summary: {summary}\n"
            f"Consequence: {consequence}\n"
            f"Remedy: {remedy}"
        )

    @staticmethod
    def _format_investigation(r: dict) -> str:
        action = r.get("actionNumber") or ""
        component = r.get("component") or ""
        summary = r.get("summary") or ""
        return f"Action: {action} | Component: {component}\n{summary}"
