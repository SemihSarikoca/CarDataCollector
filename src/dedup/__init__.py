"""
Duplikasyon Tespit Motoru
SimHash + MinHash tabanlı yakın-duplikat tespiti.
Tam eşleşme (hash) + yakın benzerlik (simhash hamming mesafesi) ile çalışır.
"""

import hashlib
import re
from typing import Optional

from datasketch import MinHash, MinHashLSH

from src.database import DatabaseManager
from src.utils.logger import get_logger

logger = get_logger("dedup")


class SimHasher:
    """
    SimHash implementasyonu - metin benzerliği için.
    Hamming mesafesi ile benzerlik ölçülür.
    """

    def __init__(self, hash_bits: int = 128):
        self.hash_bits = hash_bits

    def _tokenize(self, text: str, ngram_size: int = 3) -> list[str]:
        """Metni n-gram'lara ayır"""
        text = re.sub(r"\s+", " ", text.lower().strip())
        words = text.split()
        if len(words) < ngram_size:
            return words
        return [" ".join(words[i : i + ngram_size]) for i in range(len(words) - ngram_size + 1)]

    def _string_hash(self, s: str) -> int:
        """String'i sabit uzunlukta hash'e dönüştür"""
        h = hashlib.md5(s.encode("utf-8")).hexdigest()
        return int(h, 16)

    def compute(self, text: str, ngram_size: int = 3) -> int:
        """SimHash hesapla"""
        tokens = self._tokenize(text, ngram_size)
        if not tokens:
            return 0

        v = [0] * self.hash_bits

        for token in tokens:
            token_hash = self._string_hash(token)
            for i in range(self.hash_bits):
                bitmask = 1 << i
                if token_hash & bitmask:
                    v[i] += 1
                else:
                    v[i] -= 1

        fingerprint = 0
        for i in range(self.hash_bits):
            if v[i] > 0:
                fingerprint |= 1 << i

        return fingerprint

    def hamming_distance(self, hash1: int, hash2: int) -> int:
        """İki simhash arasındaki hamming mesafesi"""
        x = hash1 ^ hash2
        distance = 0
        while x:
            distance += 1
            x &= x - 1
        return distance

    def similarity(self, hash1: int, hash2: int) -> float:
        """İki simhash arasındaki benzerlik (0.0 - 1.0)"""
        distance = self.hamming_distance(hash1, hash2)
        return 1.0 - (distance / self.hash_bits)

    def to_hex(self, value: int) -> str:
        """SimHash değerini hex string'e dönüştür"""
        return format(value, f"0{self.hash_bits // 4}x")

    def from_hex(self, hex_str: str) -> int:
        """Hex string'den SimHash değerine dönüştür"""
        return int(hex_str, 16)

    def get_buckets(self, value: int, num_buckets: int = 4) -> tuple[str, ...]:
        """SimHash'i bucket'lara böl (LSH-benzeri yaklaşım)"""
        hex_str = self.to_hex(value)
        chunk_size = len(hex_str) // num_buckets
        return tuple(
            hex_str[i * chunk_size : (i + 1) * chunk_size]
            for i in range(num_buckets)
        )


class MinHasher:
    """MinHash + LSH tabanlı benzerlik tespiti"""

    def __init__(self, num_perm: int = 128, threshold: float = 0.85):
        self.num_perm = num_perm
        self.threshold = threshold
        self.lsh = MinHashLSH(threshold=threshold, num_perm=num_perm)
        self._index_count = 0

    def _tokenize(self, text: str, ngram_size: int = 3) -> set[str]:
        """Metni shingle kümesine dönüştür"""
        text = re.sub(r"\s+", " ", text.lower().strip())
        if len(text) < ngram_size:
            return {text}
        return {text[i : i + ngram_size] for i in range(len(text) - ngram_size + 1)}

    def compute(self, text: str, ngram_size: int = 3) -> MinHash:
        """MinHash hesapla"""
        m = MinHash(num_perm=self.num_perm)
        for shingle in self._tokenize(text, ngram_size):
            m.update(shingle.encode("utf-8"))
        return m

    def add_to_index(self, key: str, minhash: MinHash):
        """LSH indeksine ekle"""
        try:
            self.lsh.insert(key, minhash)
            self._index_count += 1
        except ValueError:
            # Zaten var
            pass

    def query(self, minhash: MinHash) -> list[str]:
        """Benzer dökümanları sorgula"""
        return self.lsh.query(minhash)

    @staticmethod
    def jaccard_similarity(m1: MinHash, m2: MinHash) -> float:
        """İki MinHash arasındaki Jaccard benzerliği"""
        return m1.jaccard(m2)


class DeduplicationEngine:
    """
    Ana duplikasyon tespit motoru.
    3 katmanlı kontrol:
    1. Tam eşleşme (SHA-256 content hash)
    2. SimHash yakın-duplikat tespiti
    3. MinHash doğrulama (opsiyonel, yüksek doğruluk gereken durumlar)
    """

    def __init__(self, db: DatabaseManager, config: dict):
        self.db = db
        self.config = config
        dedup_config = config.get("deduplication", {})
        self.similarity_threshold = dedup_config.get("similarity_threshold", 0.85)
        self.simhasher = SimHasher(hash_bits=dedup_config.get("simhash_bits", 128))
        self.minhasher = MinHasher(
            num_perm=dedup_config.get("minhash_permutations", 128),
            threshold=self.similarity_threshold,
        )
        self.ngram_size = dedup_config.get("ngram_size", 3)
        self.batch_size = dedup_config.get("batch_size", 1000)

    async def check_duplicate(self, content_text: str, content_hash: str,
                               url_hash: str) -> tuple[bool, Optional[int], str]:
        """
        Duplikasyon kontrolü yap.
        
        Returns:
            (is_duplicate, duplicate_of_id, method)
        """
        # Katman 1: Tam hash eşleşmesi
        existing = await self.db.get_document_by_content_hash(content_hash)
        if existing:
            logger.info("exact_duplicate_found", method="content_hash",
                       duplicate_of=existing["id"])
            return True, existing["id"], "exact_hash"

        # URL zaten işlenmiş mi?
        existing_url = await self.db.get_document_by_url_hash(url_hash)
        if existing_url:
            logger.debug("url_already_exists", url_hash=url_hash)
            return True, existing_url["id"], "url_hash"

        # Katman 2: SimHash benzerlik kontrolü
        if content_text and len(content_text) > 50:
            simhash_value = self.simhasher.compute(content_text, self.ngram_size)
            buckets = self.simhasher.get_buckets(simhash_value)

            # Aynı bucket'taki dökümanları bul
            candidates = await self.db.find_similar_simhashes(buckets)
            for candidate in candidates:
                candidate_hash = self.simhasher.from_hex(candidate["simhash_value"])
                similarity = self.simhasher.similarity(simhash_value, candidate_hash)
                if similarity >= self.similarity_threshold:
                    logger.info(
                        "near_duplicate_found",
                        method="simhash",
                        similarity=round(similarity, 3),
                        duplicate_of=candidate["document_id"],
                    )
                    return True, candidate["document_id"], f"simhash({similarity:.3f})"

        return False, None, "unique"

    async def compute_and_store_simhash(self, document_id: int, content_text: str):
        """SimHash hesapla ve veritabanına kaydet"""
        if not content_text or len(content_text) < 50:
            return

        simhash_value = self.simhasher.compute(content_text, self.ngram_size)
        hex_value = self.simhasher.to_hex(simhash_value)
        buckets = self.simhasher.get_buckets(simhash_value)

        await self.db.insert_simhash(document_id, hex_value, buckets)
        return hex_value

    async def run_batch_dedup(self) -> dict:
        """
        Toplu duplikasyon kontrolü çalıştır.
        Henüz kontrol edilmemiş dökümanlar için.
        """
        stats = {"checked": 0, "duplicates_found": 0}
        offset = 0

        while True:
            docs = await self.db.get_non_duplicate_documents(self.batch_size, offset)
            if not docs:
                break

            for doc in docs:
                stats["checked"] += 1
                # SimHash kontrolü
                if doc.get("simhash"):
                    simhash_val = self.simhasher.from_hex(doc["simhash"])
                    buckets = self.simhasher.get_buckets(simhash_val)
                    candidates = await self.db.find_similar_simhashes(buckets)

                    for candidate in candidates:
                        if candidate["document_id"] == doc["id"]:
                            continue
                        cand_hash = self.simhasher.from_hex(candidate["simhash_value"])
                        similarity = self.simhasher.similarity(simhash_val, cand_hash)
                        if similarity >= self.similarity_threshold:
                            # Düşük ID'li olan orijinal olarak kabul edilir
                            if doc["id"] > candidate["document_id"]:
                                await self.db.mark_duplicate(
                                    doc["id"], candidate["document_id"]
                                )
                                stats["duplicates_found"] += 1
                                break

            offset += self.batch_size

        logger.info("batch_dedup_complete", **stats)
        return stats
