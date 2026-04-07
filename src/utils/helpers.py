"""
Yardımcı fonksiyonlar
"""

import os
import re
import unicodedata
from pathlib import Path
from urllib.parse import urljoin, urlparse

import psutil
import yaml


def load_config(config_path: str = "config/settings.yaml") -> dict:
    """YAML konfigürasyon dosyasını yükle"""
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def normalize_url(url: str) -> str:
    """URL normalleştir - trailing slash, fragment kaldır"""
    parsed = urlparse(url)
    # Fragment ve query parametrelerinin bazılarını kaldır
    normalized = parsed._replace(fragment="")
    result = normalized.geturl()
    return result.rstrip("/")


def make_absolute_url(base_url: str, relative_url: str) -> str:
    """Göreceli URL'yi mutlak URL'ye dönüştür"""
    return urljoin(base_url, relative_url)


def clean_text(text: str) -> str:
    """Metin temizleme - gereksiz boşluklar, özel karakterler"""
    if not text:
        return ""
    # Unicode normalleştirme
    text = unicodedata.normalize("NFKC", text)
    # Çoklu boşlukları tekile indir
    text = re.sub(r"\s+", " ", text)
    # Başta ve sondaki boşlukları kaldır
    text = text.strip()
    return text


def clean_html_text(html_text: str) -> str:
    """HTML'den temiz metin çıkar (basit)"""
    # Script ve style etiketlerini kaldır
    text = re.sub(r"<script[^>]*>.*?</script>", "", html_text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # HTML etiketlerini kaldır
    text = re.sub(r"<[^>]+>", " ", text)
    # HTML entity'leri
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&#\d+;", "", text)
    return clean_text(text)


def sanitize_filename(name: str, max_length: int = 200) -> str:
    """Dosya adı olarak kullanılabilecek şekilde temizle"""
    # Güvenli olmayan karakterleri kaldır
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    # Türkçe karakter desteği
    name = name.strip(". ")
    if len(name) > max_length:
        name = name[:max_length]
    return name or "unnamed"


def get_disk_usage(path: str) -> dict:
    """Disk kullanım bilgisi"""
    try:
        usage = psutil.disk_usage(path)
        return {
            "total_gb": round(usage.total / (1024**3), 2),
            "used_gb": round(usage.used / (1024**3), 2),
            "free_gb": round(usage.free / (1024**3), 2),
            "percent": usage.percent,
        }
    except FileNotFoundError:
        return {"total_gb": 0, "used_gb": 0, "free_gb": 0, "percent": 0}


def ensure_dir(path: str) -> Path:
    """Dizin yoksa oluştur ve Path döndür"""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def extract_car_info(text: str) -> dict:
    """Metinden marka/model/yıl bilgisi çıkarmaya çalış"""
    car_brands = [
        "Toyota", "Honda", "Ford", "BMW", "Mercedes", "Audi", "Volkswagen", "VW",
        "Renault", "Fiat", "Peugeot", "Citroen", "Opel", "Hyundai", "Kia",
        "Nissan", "Mazda", "Volvo", "Skoda", "Seat", "Dacia", "Suzuki",
        "Mitsubishi", "Subaru", "Jeep", "Land Rover", "Range Rover",
        "Porsche", "Ferrari", "Lamborghini", "Maserati", "Alfa Romeo",
        "Mini", "Chevrolet", "Dodge", "Cadillac", "Tesla", "BYD",
        "Togg", "Tofaş", "Proton", "Geely", "Chery", "MG",
        "Cupra", "DS", "Smart", "Lexus", "Infiniti", "Acura",
    ]

    info = {"brand": "", "model": "", "year": ""}

    text_lower = text.lower()
    for brand in car_brands:
        if brand.lower() in text_lower:
            info["brand"] = brand
            break

    # Yıl tespiti (1990-2030)
    year_match = re.search(r"\b(19[9]\d|20[0-3]\d)\b", text)
    if year_match:
        info["year"] = year_match.group(1)

    return info


def estimate_content_quality(text: str) -> float:
    """
    İçerik kalite tahmini (0.0 - 1.0).
    Kısa, anlamsız, reklam içeriklerini düşük puanlar.
    """
    if not text:
        return 0.0

    score = 0.0
    word_count = len(text.split())

    # Minimum uzunluk
    if word_count < 20:
        return 0.1
    elif word_count < 50:
        score += 0.2
    elif word_count < 200:
        score += 0.4
    elif word_count < 500:
        score += 0.6
    else:
        score += 0.8

    # Teknik terimler varsa bonus
    technical_terms = [
        "motor", "şanzıman", "fren", "süspansiyon", "turbo", "dizel", "benzin",
        "beygir", "tork", "yakıt", "km/h", "hp", "cc", "silindir",
        "vites", "otomatik", "manuel", "debriyaj", "radyatör", "yağ",
        "filtre", "balata", "amortisör", "rot", "lastik", "jant",
        "abs", "esp", "airbag", "klima", "lpg", "cng", "elektrik",
        "hibrit", "akü", "alternatör", "marş", "enjektör", "egzoz",
        "katalitik", "turbo", "intercooler", "supap", "eksantrik",
        "krank", "piston", "segman", "conta", "kayış", "triger",
    ]
    term_count = sum(1 for term in technical_terms if term in text.lower())
    score += min(0.2, term_count * 0.02)

    return min(1.0, score)
