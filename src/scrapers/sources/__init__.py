"""
Kaynak Scraper'ları - Türk otomobil siteleri
Her kaynak için özelleştirilmiş scraper.
Genel ForumScraper/ContentScraper yeterli olmadığında override edilir.
"""

from src.scrapers.forum_scraper import ContentScraper, ForumScraper
from src.scrapers.sources.donanimhaber import DonanimHaberScraper
from src.scrapers.sources.technopat import TechnopatScraper
from src.scrapers.sources.shiftdelete import ShiftDeleteScraper
from src.scrapers.sources.sekizsilindir import SekizSilindirScraper
from src.scrapers.sources.otopark import OtoparkScraper
from src.scrapers.sources.generic import GenericForumScraper, GenericContentScraper

__all__ = [
    "DonanimHaberScraper",
    "TechnopatScraper", 
    "ShiftDeleteScraper",
    "SekizSilindirScraper",
    "OtoparkScraper",
    "GenericForumScraper",
    "GenericContentScraper",
    "ForumScraper",
    "ContentScraper",
]
