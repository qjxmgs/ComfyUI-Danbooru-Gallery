"""Bounded disk cache of static gallery thumbnails."""
from contextlib import closing
from pathlib import Path
import hashlib
import io
import sqlite3
import time
from PIL import Image

CACHE_PATH = Path(__file__).parent / "cache" / "thumbnails.db"
MAX_BYTES = 256 * 1024 * 1024


def connect():
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(CACHE_PATH, timeout=15)
    conn.execute("CREATE TABLE IF NOT EXISTS thumbnails (key TEXT PRIMARY KEY, data BLOB, accessed REAL)")
    return conn


def get_thumbnail(url):
    key = hashlib.sha256(url.encode()).hexdigest()
    with closing(connect()) as conn, conn:
        row = conn.execute("SELECT data FROM thumbnails WHERE key=?", (key,)).fetchone()
        if row:
            conn.execute("UPDATE thumbnails SET accessed=? WHERE key=?", (time.time(), key))
            return row[0]
    return None


def save_thumbnail(url, data):
    with Image.open(io.BytesIO(data)) as source:
        source.thumbnail((384, 384))
        image = source.convert("RGB")
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=85)
    thumbnail = output.getvalue()
    key = hashlib.sha256(url.encode()).hexdigest()
    with closing(connect()) as conn, conn:
        conn.execute("INSERT OR REPLACE INTO thumbnails VALUES (?,?,?)", (key, thumbnail, time.time()))
        total = conn.execute("SELECT COALESCE(SUM(length(data)),0) FROM thumbnails").fetchone()[0]
        if total > MAX_BYTES:
            for old_key, size in conn.execute("SELECT key,length(data) FROM thumbnails ORDER BY accessed").fetchall():
                if total <= MAX_BYTES * 0.9:
                    break
                conn.execute("DELETE FROM thumbnails WHERE key=?", (old_key,))
                total -= size
    return thumbnail
