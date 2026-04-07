"""
Q/A Üretici - Ollama üzerinden Gemma12b / Qwen ile JSON Q/A çifti üretimi.
Toplanan dökümanları okur, LLM'e gönderir, yapılandırılmış Q/A çiftleri oluşturur.
"""

import asyncio
import json
import re
from datetime import datetime
from typing import Optional

import httpx

from src.database import DatabaseManager
from src.models import QAPair
from src.storage import StorageManager
from src.utils.helpers import extract_car_info, estimate_content_quality
from src.utils.logger import get_logger

logger = get_logger("qa_generator")


class QAGenerator:
    """
    LLM tabanlı Q/A çifti üretici.
    Ollama API üzerinden Gemma2 12B veya Qwen modelleri kullanır.
    """

    def __init__(self, config: dict, db: DatabaseManager, storage: StorageManager):
        self.config = config
        self.db = db
        self.storage = storage

        qa_config = config.get("qa_generator", {})
        self.ollama_url = qa_config.get("ollama_url", "http://localhost:11434")
        self.model_name = qa_config.get("ollama_model_name", "gemma2:12b")
        self.fallback_model = qa_config.get("fallback_model_name", "qwen2.5:14b")
        self.batch_size = qa_config.get("batch_size", 5)
        self.max_qa_per_doc = qa_config.get("max_qa_per_document", 20)
        self.min_qa_per_doc = qa_config.get("min_qa_per_document", 3)
        self.temperature = qa_config.get("temperature", 0.3)
        self.max_tokens = qa_config.get("max_tokens", 4096)
        self.system_prompt = qa_config.get("system_prompt", self._default_system_prompt())

        self._client = httpx.AsyncClient(timeout=300)  # 5 dakika timeout
        self._active_model = self.model_name
        self._total_generated = 0
        self._total_failed = 0

    def _default_system_prompt(self) -> str:
        return """Sen bir otomobil teknik uzmanısın. Verilen Türkçe otomobil teknik metni veya
forum tartışmasından, LLM fine-tuning için kullanılacak yüksek kaliteli
soru-cevap çiftleri oluştur.

Kurallar:
1. Sorular Türkçe ve doğal dilde olmalı
2. Cevaplar teknik olarak doğru ve detaylı olmalı
3. Her Q/A çifti bağımsız olarak anlaşılabilir olmalı
4. Marka, model, yıl bilgisi varsa dahil et
5. Genel bilgi yerine spesifik teknik detaylara odaklan
6. JSON formatında döndür: [{"question": "...", "answer": "..."}]"""

    async def check_ollama_health(self) -> bool:
        """Ollama API sağlık kontrolü"""
        try:
            response = await self._client.get(f"{self.ollama_url}/api/tags")
            if response.status_code == 200:
                data = response.json()
                models = [m["name"] for m in data.get("models", [])]
                logger.info("ollama_available", models=models)

                # Tercih edilen model mevcut mu?
                if self.model_name in models or any(self.model_name.split(":")[0] in m for m in models):
                    self._active_model = self.model_name
                elif self.fallback_model in models or any(self.fallback_model.split(":")[0] in m for m in models):
                    self._active_model = self.fallback_model
                    logger.warning("using_fallback_model", model=self.fallback_model)
                else:
                    logger.error("no_suitable_model", available=models,
                               wanted=[self.model_name, self.fallback_model])
                    return False
                return True
            return False
        except Exception as e:
            logger.error("ollama_not_available", error=str(e))
            return False

    async def generate_qa_for_text(self, text: str, source_name: str = "",
                                    title: str = "") -> list[dict]:
        """
        Metin için Q/A çiftleri üret.
        Returns: [{"question": "...", "answer": "..."}, ...]
        """
        # Metin çok kısaysa atla
        if not text or len(text) < 100:
            return []

        # Metin çok uzunsa kes (LLM context limiti)
        max_chars = 8000
        if len(text) > max_chars:
            text = text[:max_chars] + "\n...[metin kesildi]"

        # Araç bilgisi çıkar
        car_info = extract_car_info(text)
        car_context = ""
        if car_info["brand"]:
            car_context = f"\nBu metin {car_info['brand']}"
            if car_info["year"]:
                car_context += f" {car_info['year']}"
            car_context += " ile ilgili."

        user_prompt = f"""Aşağıdaki Türkçe otomobil teknik metninden/forum tartışmasından 
soru-cevap çiftleri oluştur.{car_context}

Kaynak: {source_name}
Başlık: {title}

---METIN BAŞLANGIÇ---
{text}
---METİN BİTİŞ---

Yukarıdaki metinden {self.min_qa_per_doc} ile {self.max_qa_per_doc} arası
yüksek kaliteli Q/A çifti oluştur. Her çift bağımsız olarak anlaşılabilir olmalı.
Cevaplar detaylı ve teknik olmalı. JSON array formatında döndür:
[{{"question": "...", "answer": "..."}}]

Sadece JSON döndür, başka bir şey yazma."""

        try:
            response = await self._client.post(
                f"{self.ollama_url}/api/generate",
                json={
                    "model": self._active_model,
                    "prompt": user_prompt,
                    "system": self.system_prompt,
                    "stream": False,
                    "options": {
                        "temperature": self.temperature,
                        "num_predict": self.max_tokens,
                    },
                },
            )

            if response.status_code != 200:
                logger.error("ollama_api_error", status=response.status_code)
                return []

            data = response.json()
            generated_text = data.get("response", "")

            # JSON parse
            qa_pairs = self._parse_qa_response(generated_text)
            return qa_pairs

        except httpx.TimeoutException:
            logger.error("ollama_timeout", model=self._active_model)
            return []
        except Exception as e:
            logger.error("qa_generation_error", error=str(e))
            return []

    def _parse_qa_response(self, text: str) -> list[dict]:
        """LLM çıktısından JSON Q/A çiftlerini parse et"""
        # JSON array'i bul
        json_match = re.search(r'\[[\s\S]*\]', text)
        if not json_match:
            logger.warning("no_json_found_in_response", text_preview=text[:200])
            return []

        try:
            qa_list = json.loads(json_match.group())
            if not isinstance(qa_list, list):
                return []

            valid_pairs = []
            for item in qa_list:
                if isinstance(item, dict) and "question" in item and "answer" in item:
                    q = item["question"].strip()
                    a = item["answer"].strip()
                    # Minimum kalite kontrolü
                    if len(q) > 10 and len(a) > 20:
                        valid_pairs.append({"question": q, "answer": a})

            return valid_pairs

        except json.JSONDecodeError as e:
            logger.warning("json_parse_error", error=str(e), text_preview=text[:200])
            return []

    async def process_batch(self) -> dict:
        """
        Hazır dökümanları toplu işle, Q/A üret.
        """
        stats = {"processed": 0, "qa_generated": 0, "failed": 0}

        # Ollama kontrolü
        if not await self.check_ollama_health():
            logger.error("ollama_not_ready_skipping_batch")
            return stats

        # İşlenecek dökümanları getir
        documents = await self.db.get_documents_for_qa(limit=self.batch_size)
        if not documents:
            logger.info("no_documents_for_qa")
            return stats

        logger.info("qa_batch_start", count=len(documents))

        for doc in documents:
            try:
                # Dosyadan metin oku
                text = ""
                if doc.get("file_path_html"):
                    text = await self.storage.get_document_text(doc["file_path_html"])

                if not text or len(text) < 100:
                    continue

                # Kalite kontrolü
                quality = estimate_content_quality(text)
                if quality < 0.3:
                    logger.debug("low_quality_skipped", doc_id=doc["id"], quality=quality)
                    continue

                # Q/A üret
                qa_dicts = await self.generate_qa_for_text(
                    text,
                    source_name=doc.get("source_name", ""),
                    title=doc.get("title", ""),
                )

                if qa_dicts and len(qa_dicts) >= self.min_qa_per_doc:
                    # Model olarak kaydet
                    car_info = extract_car_info(text)

                    qa_pairs = []
                    for qa in qa_dicts:
                        pair = QAPair(
                            document_id=doc["id"],
                            source_name=doc.get("source_name", ""),
                            question=qa["question"],
                            answer=qa["answer"],
                            car_brand=car_info.get("brand", ""),
                            car_model=car_info.get("model", ""),
                            car_year=car_info.get("year", ""),
                            quality_score=quality,
                            model_used=self._active_model,
                        )
                        qa_pairs.append(pair)

                    inserted = await self.db.insert_qa_pairs(qa_pairs)
                    stats["qa_generated"] += inserted
                    self._total_generated += inserted
                    stats["processed"] += 1

                    logger.info("qa_generated", doc_id=doc["id"],
                               title=doc.get("title", "")[:40],
                               count=inserted)
                else:
                    stats["failed"] += 1
                    self._total_failed += 1

                # Aşırı yüklemeyi önle
                await asyncio.sleep(1)

            except Exception as e:
                stats["failed"] += 1
                self._total_failed += 1
                logger.error("qa_process_error", doc_id=doc.get("id"), error=str(e))

        logger.info("qa_batch_complete", **stats)
        return stats

    async def export_all_qa(self, output_path: str, format: str = "jsonl") -> int:
        """
        Tüm Q/A çiftlerini dosyaya dışa aktar.
        Formats: jsonl, json
        """
        import aiofiles

        total_exported = 0
        offset = 0
        batch_size = 5000

        async with aiofiles.open(output_path, "w", encoding="utf-8") as f:
            if format == "json":
                await f.write("[\n")

            first = True
            while True:
                pairs = await self.db.export_qa_pairs(limit=batch_size, offset=offset)
                if not pairs:
                    break

                for pair in pairs:
                    qa_record = {
                        "question": pair["question"],
                        "answer": pair["answer"],
                        "source": pair["source_name"],
                        "car_brand": pair.get("car_brand", ""),
                        "car_model": pair.get("car_model", ""),
                        "car_year": pair.get("car_year", ""),
                        "quality_score": pair.get("quality_score", 0),
                        "model_used": pair.get("model_used", ""),
                    }

                    if format == "jsonl":
                        await f.write(json.dumps(qa_record, ensure_ascii=False) + "\n")
                    elif format == "json":
                        if not first:
                            await f.write(",\n")
                        await f.write("  " + json.dumps(qa_record, ensure_ascii=False))
                        first = False

                    total_exported += 1

                offset += batch_size

            if format == "json":
                await f.write("\n]")

        logger.info("qa_exported", path=output_path, count=total_exported, format=format)
        return total_exported

    async def close(self):
        """Temizlik"""
        await self._client.aclose()

    def get_stats(self) -> dict:
        return {
            "model": self._active_model,
            "total_generated": self._total_generated,
            "total_failed": self._total_failed,
        }
