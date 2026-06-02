"""
Backfill content_text from HTML files for docs where it's empty.
Run once: python scripts/backfill_content_text.py
"""
import asyncio
import re
from pathlib import Path

import asyncpg
from bs4 import BeautifulSoup


DSN = "postgresql://collector:collector_pass@localhost:5432/car_collector"


async def main():
    conn = await asyncpg.connect(DSN)
    rows = await conn.fetch("""
        SELECT id, file_path_html FROM scraped_data
        WHERE (content_text IS NULL OR length(content_text) < 50)
          AND file_path_html IS NOT NULL AND file_path_html != ''
    """)
    print(f"Backfilling {len(rows)} docs...")
    updated = 0
    for row in rows:
        path = Path(row["file_path_html"])
        if not path.is_absolute():
            path = Path("/Users/semihsarikoca/Desktop/CarDataCollector") / path
        if not path.exists():
            continue
        try:
            html = path.read_text(encoding="utf-8", errors="replace")
            soup = BeautifulSoup(html, "lxml")
            div = soup.select_one("div.content")
            text = div.get_text(separator="\n") if div else soup.get_text(separator="\n")
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            if len(text) < 50:
                continue
            word_count = len(text.split())
            await conn.execute("""
                UPDATE scraped_data
                SET content_text = $1, word_count = $2
                WHERE id = $3
            """, text, word_count, row["id"])
            updated += 1
        except Exception as e:
            print(f"  Error {row['id']}: {e}")
    print(f"Done — updated {updated} docs.")
    await conn.close()


asyncio.run(main())
