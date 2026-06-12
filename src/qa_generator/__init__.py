"""
Q/A Üretici - Ollama üzerinden Gemma12b / Qwen ile JSON Q/A çifti üretimi.
Toplanan dökümanları okur, LLM'e gönderir, yapılandırılmış Q/A çiftleri oluşturur.
"""

import asyncio
import json
import os
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

# --- Forum / Reddit noise that leaks usernames and UI cruft into Q/A pairs ---
# Reddit comments are stored as "[author | score N]\n<body>" (see reddit_api.py),
# so usernames bleed straight into the LLM context. Strip these before generating.
_COMMENT_HEADER_RE = re.compile(r'\[[^\]\n]{0,60}\|\s*score[^\]\n]*\]', re.I)  # [author | score N]
_HANDLE_RE = re.compile(r'(?<![A-Za-z0-9])/?[ur]/[A-Za-z0-9_-]+', re.I)        # u/x, r/x, /u/x, /r/x
_DELETED_RE = re.compile(r'\[(?:deleted|removed)\]', re.I)
_MULTISPACE_RE = re.compile(r'[ \t]{2,}')
_MULTINEWLINE_RE = re.compile(r'\n{3,}')

# Questions that lean on the source document instead of standing on their own.
# A fine-tuning question like "What does the post say about X?" is unusable —
# the model won't have "the post" at inference time. Reject these at generation
# time and purge existing ones from the DB (same predicate used in cleanup SQL).
_LEAK_RE = re.compile(
    r'\b(the post|the text|the author|the comment|this post|this text|'
    r'this comment|the user|the discussion|the thread|the article|'
    r'mentioned|someone says|the passage)\b',
    re.I,
)
# Minimum question length (chars) for a self-contained fine-tuning question.
# qwen2.5:3b emits ~80-char questions on average; anything under 60 is a terse
# fragment ("What are quad tips?") with no vehicle/context anchor.
_MIN_QUESTION_CHARS = 60


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
        self.ollama_url = (
            os.environ.get("OLLAMA_URL")
            or qa_config.get("ollama_url", "http://localhost:11434")
        )
        self.model_name = qa_config.get("ollama_model_name", "gemma2:12b")
        self.fallback_model = qa_config.get("fallback_model_name", "qwen2.5:14b")
        self.batch_size = qa_config.get("batch_size", 5)
        self.max_qa_per_doc = qa_config.get("max_qa_per_document", 20)
        self.min_qa_per_doc = qa_config.get("min_qa_per_document", 3)
        self.temperature = qa_config.get("temperature", 0.3)
        self.max_tokens = qa_config.get("max_tokens", 2048)
        self.max_context_chars = qa_config.get("max_context_chars", 8000)
        self.system_prompt = qa_config.get("system_prompt", self._default_system_prompt())
        self.concurrency = max(1, int(qa_config.get("concurrency", 2)))

        # Documents below this source-quality score are skipped before hitting the
        # LLM (short/non-technical text yields junk Q/A). Was hardcoded 0.3.
        self.min_doc_quality = float(qa_config.get("min_doc_quality", 0.6))
        # Sources that are photo/meme/chat-heavy: too little usable text per item
        # to produce self-contained technical Q/A. Skipped entirely.
        self.skip_sources = set(qa_config.get("skip_sources", []))
        # Minimum chars for a self-contained question; defaults to module constant.
        self.min_question_chars = int(qa_config.get("min_question_chars", _MIN_QUESTION_CHARS))

        timeout_seconds = qa_config.get("request_timeout_seconds", 120)
        self._client = httpx.AsyncClient(timeout=timeout_seconds)
        self._active_model = self.model_name
        self._total_generated = 0
        self._total_failed = 0

    def _default_system_prompt(self) -> str:
        return """You are an automotive technical expert. From the given English
automotive technical text or forum discussion, generate high-quality
question-answer pairs for LLM fine-tuning.

Rules:
1. Questions must be in natural English, phrased as a complete, self-contained
   sentence of 12-25 words. Never write a terse fragment like "What are quad tips?".
2. Each question must carry its own context: name the vehicle make, model and
   year whenever the text mentions them, and the specific symptom or component.
3. Answers must be technically accurate and detailed (at least 2-3 sentences).
4. Each Q/A pair must be independently understandable WITHOUT the source text.
5. Focus on specific technical details, not general trivia.
6. NEVER reference "the post", "the text", "the author", "the comment", a
   username or forum handle. Write generic automotive knowledge that stands alone.
7. Return ONLY a valid JSON array, nothing else: [{"question": "...", "answer": "..."}]"""

    def _clean_text(self, text: str) -> str:
        """Strip forum usernames and UI noise so they don't leak into Q/A pairs."""
        text = _COMMENT_HEADER_RE.sub(" ", text)   # [author | score N] headers
        text = _HANDLE_RE.sub(" ", text)           # u/name, r/sub handles
        text = _DELETED_RE.sub(" ", text)          # [deleted] / [removed]
        text = _MULTISPACE_RE.sub(" ", text)
        text = _MULTINEWLINE_RE.sub("\n\n", text)
        return text.strip()

    def _truncate_to_context(self, text: str) -> str:
        if len(text) <= self.max_context_chars:
            return text
        window = text[:self.max_context_chars]
        # Walk sentence boundaries from longest to shortest separator
        for sep in ("\n\n", ". ", "? ", "! ", "\n"):
            pos = window.rfind(sep)
            # Only accept a cut that keeps at least half the window
            if pos > self.max_context_chars // 2:
                return window[: pos + len(sep)].rstrip()
        return window.rstrip()

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
        if not text or len(text) < 100:
            return []

        text = self._clean_text(text)
        text = self._truncate_to_context(text)

        # Extract car info
        car_info = extract_car_info(text)
        car_context = ""
        if car_info["brand"]:
            car_context = f"\nThis text is about {car_info['brand']}"
            if car_info["year"]:
                car_context += f" {car_info['year']}"
            car_context += "."

        user_prompt = f"""Generate question-answer pairs from the automotive
technical text / forum discussion below.{car_context}

Source: {source_name}
Title: {title}

---TEXT START---
{text}
---TEXT END---

Generate between {self.min_qa_per_doc} and {self.max_qa_per_doc} high-quality
Q/A pairs. Requirements for EVERY pair:
- The question is a complete sentence of 12-25 words, self-contained, and names
  the vehicle make/model/year and the component or symptom when the text gives them.
- The answer is detailed and technical (2-3+ sentences).
- The pair makes sense on its own, with NO reference to "the post", "the text",
  "the author", "the comment", usernames or forum handles.

Return ONLY a valid JSON array, no markdown, no commentary:
[{{"question": "...", "answer": "..."}}]"""

        try:
            response = await self._client.post(
                f"{self.ollama_url}/api/generate",
                json={
                    "model": self._active_model,
                    "prompt": user_prompt,
                    "system": self.system_prompt,
                    "stream": False,
                    # NOTE: deliberately NOT using Ollama's format="json" — with
                    # qwen2.5:3b it biases the model toward a single object instead
                    # of an array of pairs, tanking yield. The tolerant parser below
                    # (fences, object-wrap, salvage) handles syntax errors instead.
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
        """LLM çıktısından JSON Q/A çiftlerini parse et (toleranslı)."""
        if not text:
            return []

        # Strip markdown code fences if the model wrapped its output
        cleaned = re.sub(r'```(?:json)?', '', text).strip()
        qa_list = None

        # 1) Direct JSON array
        m = re.search(r'\[[\s\S]*\]', cleaned)
        if m:
            try:
                parsed = json.loads(m.group())
                if isinstance(parsed, list):
                    qa_list = parsed
            except json.JSONDecodeError:
                pass

        # 2) Object wrapping the array, e.g. {"qa_pairs": [...]}, or a single
        #    bare pair {"question": "...", "answer": "..."}
        if qa_list is None:
            try:
                obj = json.loads(cleaned)
                if isinstance(obj, list):
                    qa_list = obj
                elif isinstance(obj, dict):
                    if "question" in obj and "answer" in obj:
                        qa_list = [obj]
                    else:
                        qa_list = next((v for v in obj.values() if isinstance(v, list)), None)
            except json.JSONDecodeError:
                pass

        # 3) Salvage individual pairs from malformed / truncated JSON
        if qa_list is None:
            qa_list = self._salvage_pairs(cleaned)

        if not isinstance(qa_list, list) or not qa_list:
            logger.warning("no_json_found_in_response", text_preview=text[:200])
            return []

        valid_pairs = []
        for item in qa_list:
            if isinstance(item, dict) and "question" in item and "answer" in item:
                q = str(item["question"]).strip()
                a = str(item["answer"]).strip()
                if self._is_acceptable_question(q) and len(a) > 20:
                    valid_pairs.append({"question": q, "answer": a})

        return valid_pairs

    def _is_acceptable_question(self, q: str) -> bool:
        """Reject terse fragments and questions that lean on the source document.

        A fine-tuning question must stand on its own: long enough to carry context
        and free of "the post / the author / mentioned" references that have no
        meaning without the original text.
        """
        if len(q) < self.min_question_chars:
            return False
        if _LEAK_RE.search(q):
            return False
        return True

    def _salvage_pairs(self, text: str) -> list[dict]:
        """Bozuk/yarıda kesilmiş JSON'dan tam {question, answer} objelerini kurtar."""
        obj_re = re.compile(
            r'\{\s*"question"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*'
            r'"answer"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}',
            re.S,
        )
        pairs = []
        for match in obj_re.finditer(text):
            try:
                q = json.loads(f'"{match.group(1)}"')
                a = json.loads(f'"{match.group(2)}"')
                pairs.append({"question": q, "answer": a})
            except json.JSONDecodeError:
                continue
        return pairs

    async def _process_single_doc(self, doc: dict, semaphore: asyncio.Semaphore) -> dict:
        """Process one document: fetch text, run Ollama, insert Q/A pairs."""
        result = {"processed": 0, "qa_generated": 0, "failed": 0, "skipped": 0}
        try:
            source_name = doc.get("source_name", "")
            if source_name in self.skip_sources:
                logger.debug("source_skipped_for_qa", doc_id=doc["id"], source=source_name)
                await self.db.mark_doc_qa_skipped(doc["id"], "source_skiplist")
                result["skipped"] = 1
                return result

            text = doc.get("content_text") or ""
            if (not text or len(text) < 100) and doc.get("file_path_html"):
                text = await self.storage.get_document_text(doc["file_path_html"])

            if not text or len(text) < 100:
                await self.db.mark_doc_qa_skipped(doc["id"], "text_too_short")
                result["skipped"] = 1
                return result

            quality = estimate_content_quality(text)
            if quality < self.min_doc_quality:
                logger.debug("low_quality_skipped", doc_id=doc["id"], quality=quality)
                await self.db.mark_doc_qa_skipped(doc["id"], f"low_quality_{quality:.2f}")
                result["skipped"] = 1
                return result

            # extract_car_info once; pass into generate so the prompt also benefits
            car_info = extract_car_info(text)

            async with semaphore:
                qa_dicts = await self.generate_qa_for_text(
                    text,
                    source_name=doc.get("source_name", ""),
                    title=doc.get("title", ""),
                )

            if qa_dicts and len(qa_dicts) >= self.min_qa_per_doc:
                qa_pairs = [
                    QAPair(
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
                    for qa in qa_dicts
                ]
                inserted = await self.db.insert_qa_pairs(qa_pairs)
                result["qa_generated"] = inserted
                result["processed"] = 1
                self._total_generated += inserted
                logger.info("qa_generated", doc_id=doc["id"],
                            title=doc.get("title", "")[:40], count=inserted)
            else:
                result["failed"] = 1
                self._total_failed += 1
        except Exception as e:
            result["failed"] = 1
            self._total_failed += 1
            logger.error("qa_process_error", doc_id=doc.get("id"), error=str(e))
        return result

    async def process_batch(self) -> dict:
        """Process pending documents concurrently up to `self.concurrency` Ollama calls."""
        stats = {"processed": 0, "qa_generated": 0, "failed": 0, "skipped_docs": 0}

        if not await self.check_ollama_health():
            logger.error("ollama_not_ready_skipping_batch")
            return stats

        documents = await self.db.get_documents_for_qa(limit=self.batch_size)
        if not documents:
            logger.info("no_documents_for_qa")
            return stats

        logger.info("qa_batch_start", count=len(documents), concurrency=self.concurrency)

        semaphore = asyncio.Semaphore(self.concurrency)
        results = await asyncio.gather(
            *[self._process_single_doc(doc, semaphore) for doc in documents],
            return_exceptions=True,
        )

        for r in results:
            if isinstance(r, Exception):
                stats["failed"] += 1
                self._total_failed += 1
                logger.error("qa_task_exception", error=str(r))
            else:
                stats["processed"] += r["processed"]
                stats["qa_generated"] += r["qa_generated"]
                stats["failed"] += r["failed"]
                stats["skipped_docs"] += r.get("skipped", 0)

        logger.info("qa_batch_complete", **stats)
        return stats

    async def export_all_qa(self, output_path: str, format: str = "jsonl") -> int:
        """
        Export all Q/A pairs to file.
        Formats: jsonl, json, huggingface
          - jsonl/json: flat records with question/answer/metadata fields
          - huggingface: chat-format {"messages": [{"role": "user", ...}, {"role": "assistant", ...}]}
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
                    if format == "huggingface":
                        record = {
                            "messages": [
                                {"role": "user", "content": pair["question"]},
                                {"role": "assistant", "content": pair["answer"]},
                            ]
                        }
                    else:
                        record = {
                            "question": pair["question"],
                            "answer": pair["answer"],
                            "source": pair["source_name"],
                            "car_brand": pair.get("car_brand", ""),
                            "car_model": pair.get("car_model", ""),
                            "car_year": pair.get("car_year", ""),
                            "quality_score": pair.get("quality_score", 0),
                            "model_used": pair.get("model_used", ""),
                        }

                    if format in ("jsonl", "huggingface"):
                        await f.write(json.dumps(record, ensure_ascii=False) + "\n")
                    elif format == "json":
                        if not first:
                            await f.write(",\n")
                        await f.write("  " + json.dumps(record, ensure_ascii=False))
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
