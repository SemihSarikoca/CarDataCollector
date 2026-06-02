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


_CAR_MODELS: dict[str, list[str]] = {
    "Toyota":        ["Camry", "Corolla", "RAV4", "Tacoma", "Highlander", "Prius", "Tundra",
                      "Sienna", "Avalon", "Venza", "4Runner", "Sequoia", "Land Cruiser"],
    "Honda":         ["Civic", "Accord", "CR-V", "Pilot", "Odyssey", "HR-V", "Passport",
                      "Ridgeline", "Fit", "Insight"],
    "Ford":          ["F-150", "F-250", "F-350", "Escape", "Explorer", "Mustang", "Bronco",
                      "Edge", "Expedition", "Ranger", "Fusion", "Focus", "Transit"],
    "Chevrolet":     ["Silverado", "Equinox", "Malibu", "Traverse", "Tahoe", "Suburban",
                      "Colorado", "Trax", "Blazer", "Camaro", "Corvette"],
    "Chevy":         ["Silverado", "Equinox", "Malibu", "Traverse", "Tahoe", "Suburban"],
    "Nissan":        ["Altima", "Rogue", "Sentra", "Pathfinder", "Frontier", "Murano",
                      "Armada", "Maxima", "Versa", "Kicks", "Titan"],
    "Hyundai":       ["Elantra", "Sonata", "Tucson", "Santa Fe", "Palisade", "Kona",
                      "Ioniq", "Veloster", "Accent"],
    "Kia":           ["Optima", "Sorento", "Sportage", "Telluride", "Stinger", "Soul",
                      "Forte", "Seltos", "Niro", "K5"],
    "Subaru":        ["Outback", "Forester", "Crosstrek", "Impreza", "Legacy", "Ascent",
                      "WRX", "BRZ"],
    "Jeep":          ["Wrangler", "Grand Cherokee", "Cherokee", "Compass", "Renegade",
                      "Gladiator"],
    "BMW":           ["328i", "330i", "530i", "540i", "X5", "X3", "X1", "3 Series",
                      "5 Series", "7 Series", "M3", "M5", "M4"],
    "Volkswagen":    ["Jetta", "Tiguan", "Atlas", "Passat", "Golf", "GTI", "ID.4", "Taos"],
    "VW":            ["Jetta", "Tiguan", "Atlas", "Passat", "Golf", "GTI"],
    "Mercedes-Benz": ["C300", "E350", "GLC", "GLE", "C-Class", "E-Class", "S-Class",
                      "A-Class", "GLS", "CLA"],
    "Mercedes":      ["C300", "E350", "GLC", "GLE", "C-Class", "E-Class", "S-Class"],
    "Dodge":         ["Ram 1500", "Charger", "Challenger", "Durango", "Journey"],
    "Ram":           ["1500", "2500", "3500", "ProMaster"],
    "GMC":           ["Sierra", "Terrain", "Acadia", "Yukon", "Canyon"],
    "Mazda":         ["CX-5", "Mazda3", "CX-9", "MX-5", "Mazda6", "CX-30"],
    "Lexus":         ["RX350", "RX450h", "IS300", "ES350", "GX460", "RX", "IS", "ES", "GX"],
    "Audi":          ["A4", "Q5", "A3", "Q7", "Q3", "A6", "Q8", "TT", "e-tron"],
    "Tesla":         ["Model 3", "Model Y", "Model S", "Model X", "Cybertruck"],
    "Acura":         ["MDX", "RDX", "TLX", "ILX"],
    "Infiniti":      ["QX60", "Q50", "QX80", "QX50"],
    "Mitsubishi":    ["Outlander", "Eclipse Cross", "Galant", "Lancer"],
    "Volvo":         ["XC90", "XC60", "S60", "V60", "XC40"],
    "Porsche":       ["911", "Cayenne", "Macan", "Panamera", "Taycan"],
    "Cadillac":      ["Escalade", "XT5", "CT5", "XT4"],
    "Buick":         ["Enclave", "Encore", "LaCrosse"],
    "Lincoln":       ["Navigator", "Aviator", "Corsair"],
    "Chrysler":      ["300", "Pacifica", "Voyager"],
}


def extract_car_info(text: str) -> dict:
    """Extract make/model/year from text using a heuristic scan."""
    info = {"brand": "", "model": "", "year": ""}
    text_lower = text.lower()

    # Find brand; first match wins
    for brand, models in _CAR_MODELS.items():
        if brand.lower() in text_lower:
            info["brand"] = brand
            # Find model belonging to that brand
            for model in models:
                if model.lower() in text_lower:
                    info["model"] = model
                    break
            break

    # Year detection (1960-2039 covers classics through near-future)
    year_match = re.search(r"\b(19[6-9]\d|20[0-3]\d)\b", text)
    if year_match:
        info["year"] = year_match.group(1)

    return info


def estimate_content_quality(text: str) -> float:
    """
    Estimate content quality score (0.0 - 1.0).
    Short or non-technical content scores lower.
    """
    if not text:
        return 0.0

    score = 0.0
    word_count = len(text.split())

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

    # Bonus for English automotive technical vocabulary
    technical_terms = [
        "engine", "transmission", "brake", "suspension", "turbo", "diesel", "gasoline",
        "horsepower", "torque", "fuel", "mph", "hp", "cc", "cylinder",
        "automatic", "manual", "clutch", "radiator", "coolant",
        "filter", "brake pad", "shock absorber", "wheel bearing", "tire", "wheel",
        "abs", "traction control", "airbag", "alternator", "starter",
        "injector", "exhaust", "catalytic converter", "intercooler",
        "valve", "camshaft", "crankshaft", "piston", "head gasket",
        "timing belt", "timing chain", "serpentine belt", "oil change",
        "obd", "check engine", "misfire", "spark plug", "dtc",
    ]
    term_count = sum(1 for term in technical_terms if term in text.lower())
    score += min(0.2, term_count * 0.02)

    return min(1.0, score)
