"""
Loglama altyapısı - structlog tabanlı
"""

import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import structlog


def setup_logging(log_level: str = "INFO", log_dir: str = "logs") -> structlog.BoundLogger:
    """
    Yapılandırılmış loglama kurulumu.
    Hem konsola hem dosyaya yazar.
    """
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    log_file = log_path / f"collector_{datetime.now().strftime('%Y%m%d')}.log"

    # Standart logging
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, log_level.upper(), logging.INFO),
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(str(log_file), encoding="utf-8"),
        ],
    )

    # structlog konfigürasyonu
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    return structlog.get_logger()


def get_logger(name: str = "") -> structlog.BoundLogger:
    """İsimlendirilmiş logger al"""
    return structlog.get_logger(component=name)
