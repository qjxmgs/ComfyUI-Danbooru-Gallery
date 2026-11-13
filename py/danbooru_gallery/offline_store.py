"""Persistent, account-scoped Danbooru history and local tag search."""
from contextlib import closing
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def query_spec(query):
    return {
        "tags": query.get("search[tags]", "").strip(),
        "rating": ",".join(sorted(filter(None, query.get("search[rating]", "").split(",")))),
        "limit": max(1, min(200, int(query.get("limit", 42)))),
    }


class OfflineStore:
    def __init__(self, scope):
        self.path = Path(__file__).parent / "cache" / "offline" / scope / "history.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as conn, conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS histories(id TEXT PRIMARY KEY, spec TEXT, updated REAL);
                CREATE TABLE IF NOT EXISTS pages(history TEXT, page INTEGER, cursor TEXT, saved REAL,
                    payload TEXT, PRIMARY KEY(history, page));
                CREATE TABLE IF NOT EXISTS posts(id INTEGER PRIMARY KEY, payload TEXT, rating TEXT,
                    created TEXT, score INTEGER, preview TEXT);
                CREATE INDEX IF NOT EXISTS posts_preview ON posts(preview);
                CREATE INDEX IF NOT EXISTS posts_created ON posts(created,id);
                CREATE INDEX IF NOT EXISTS posts_score ON posts(score,id);
                CREATE TABLE IF NOT EXISTS page_posts(history TEXT, page INTEGER, post INTEGER,
                    PRIMARY KEY(history,page,post));
                CREATE INDEX IF NOT EXISTS page_posts_post ON page_posts(post);
                CREATE TABLE IF NOT EXISTS tags(post INTEGER, tag TEXT, PRIMARY KEY(post,tag));
                CREATE INDEX IF NOT EXISTS tags_name ON tags(tag,post);
                CREATE TABLE IF NOT EXISTS thumbnails(url TEXT PRIMARY KEY, data BLOB);
                CREATE TABLE IF NOT EXISTS thumbnail_failures(url TEXT PRIMARY KEY, retry_after REAL, attempts INTEGER);
            """)

    def connect(self):
        return sqlite3.connect(self.path, timeout=15)

    def mode(self, value=None):
        with closing(self.connect()) as conn, conn:
            if value is not None:
                if value not in ("auto", "local"):
                    raise ValueError("模式必须为 auto 或 local")
                conn.execute("INSERT OR REPLACE INTO settings VALUES ('mode',?)", (value,))
            row = conn.execute("SELECT value FROM settings WHERE key='mode'").fetchone()
            return row[0] if row else "auto"

    def save_page(self, spec, page, cursor, posts):
        history = fingerprint(spec)
        now = time.time()
        with closing(self.connect()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO histories VALUES (?,?,?)", (history, json.dumps(spec), now))
            conn.execute("INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?)",
                         (history, page, cursor, now, json.dumps(posts)))
            conn.execute("DELETE FROM page_posts WHERE history=? AND page=?", (history, page))
            for post in posts:
                pid = int(post["id"])
                preview = post.get("preview_file_url") or ""
                conn.execute("INSERT OR REPLACE INTO posts VALUES (?,?,?,?,?,?)", (
                    pid, json.dumps(post), post.get("rating", ""), post.get("created_at", ""),
                    post.get("score", 0), preview))
                conn.execute("INSERT OR IGNORE INTO page_posts VALUES (?,?,?)", (history, page, pid))
                conn.execute("DELETE FROM tags WHERE post=?", (pid,))
                all_tags = set(post.get("tag_string", "").split())
                for category in ("artist", "copyright", "character", "general", "meta"):
                    all_tags.update(post.get(f"tag_string_{category}", "").split())
                conn.executemany("INSERT OR IGNORE INTO tags VALUES (?,?)", ((pid, tag) for tag in all_tags))
        return history, now

    def page(self, history, page, cursor=None):
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT payload,saved,cursor FROM pages WHERE history=? AND page=?",
                               (history, page)).fetchone()
        if not row or (cursor is not None and str(cursor) != row[2]):
            return None
        return json.loads(row[0]), row[1]

    def history(self, offset=0):
        with closing(self.connect()) as conn:
            rows = conn.execute("""SELECT h.id,h.spec,h.updated,MIN(p.page),COUNT(p.page)
                FROM histories h JOIN pages p ON p.history=h.id GROUP BY h.id
                ORDER BY h.updated DESC LIMIT 50 OFFSET ?""", (offset,)).fetchall()
        return [dict(id=r[0], spec=json.loads(r[1]), saved=r[2], first_page=r[3], pages=r[4]) for r in rows]

    def thumbnail(self, url, data=None):
        with closing(self.connect()) as conn, conn:
            if data is not None:
                # An in-flight worker must not resurrect data after a clear operation.
                if conn.execute("SELECT 1 FROM posts WHERE preview=? LIMIT 1", (url,)).fetchone():
                    conn.execute("INSERT OR REPLACE INTO thumbnails VALUES (?,?)", (url, data))
                    conn.execute("DELETE FROM thumbnail_failures WHERE url=?", (url,))
                return data
            row = conn.execute("SELECT data FROM thumbnails WHERE url=?", (url,)).fetchone()
            return row[0] if row else None

    def preview_post(self, url):
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT payload FROM posts WHERE preview=? LIMIT 1", (url,)).fetchone()
        return json.loads(row[0]) if row else None

    def missing_thumbnails(self, limit=100):
        with closing(self.connect()) as conn:
            return [r[0] for r in conn.execute("""SELECT DISTINCT preview FROM posts
                WHERE preview != '' AND NOT EXISTS (SELECT 1 FROM thumbnails WHERE url=preview)
                AND NOT EXISTS (SELECT 1 FROM thumbnail_failures WHERE url=preview AND retry_after>?)
                ORDER BY id DESC LIMIT ?""", (time.time(), limit))]

    def thumbnail_failed(self, url):
        with closing(self.connect()) as conn, conn:
            if not conn.execute("SELECT 1 FROM posts WHERE preview=? LIMIT 1", (url,)).fetchone():
                return
            row = conn.execute("SELECT attempts FROM thumbnail_failures WHERE url=?", (url,)).fetchone()
            attempts = min(5, (row[0] if row else 0) + 1)
            conn.execute("INSERT OR REPLACE INTO thumbnail_failures VALUES (?,?,?)",
                         (url, time.time() + min(60, 5 * 2**(attempts-1)), attempts))

    def retry_delay(self):
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT MIN(retry_after) FROM thumbnail_failures").fetchone()
        return None if row[0] is None else max(1, min(60, row[0] - time.time()))

    def stats(self, history=None):
        with closing(self.connect()) as conn:
            if history:
                posts = conn.execute("SELECT COUNT(DISTINCT post) FROM page_posts WHERE history=?", (history,)).fetchone()[0]
                exclusive = conn.execute("""SELECT COUNT(DISTINCT post) FROM page_posts a WHERE history=?
                    AND NOT EXISTS (SELECT 1 FROM page_posts b WHERE b.post=a.post AND b.history != ?)""",
                                         (history, history)).fetchone()[0]
                return dict(posts=posts, exclusive_posts=exclusive)
            total = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
            ready = conn.execute("SELECT COUNT(*) FROM posts WHERE EXISTS (SELECT 1 FROM thumbnails WHERE url=preview)").fetchone()[0]
            histories = conn.execute("SELECT COUNT(*) FROM histories").fetchone()[0]
            data_bytes = conn.execute("SELECT COALESCE(SUM(length(data)),0) FROM thumbnails").fetchone()[0]
        disk_bytes = sum(p.stat().st_size for p in self.path.parent.glob("history.sqlite*") if p.is_file())
        return dict(posts=total, ready=ready, pending=total-ready, histories=histories,
                    thumbnail_bytes=data_bytes, disk_bytes=disk_bytes)

    def delete(self, history=None):
        with closing(self.connect()) as conn, conn:
            if history:
                for table in ("histories", "pages", "page_posts"):
                    column = "id" if table == "histories" else "history"
                    conn.execute(f"DELETE FROM {table} WHERE {column}=?", (history,))
            else:
                for table in ("histories", "pages", "page_posts"):
                    conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM posts WHERE NOT EXISTS (SELECT 1 FROM page_posts WHERE post=posts.id)")
            conn.execute("DELETE FROM tags WHERE NOT EXISTS (SELECT 1 FROM posts WHERE id=tags.post)")
            conn.execute("DELETE FROM thumbnails WHERE NOT EXISTS (SELECT 1 FROM posts WHERE preview=url)")
            conn.execute("DELETE FROM thumbnail_failures WHERE NOT EXISTS (SELECT 1 FROM posts WHERE preview=url)")
        with closing(self.connect()) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")

    def search(self, query, rating, page, limit, translate):
        where, args = [], []
        order = "p.created DESC,p.id DESC"
        ratings = {"general": "g", "sensitive": "s", "questionable": "q", "explicit": "e"}
        selected = [ratings.get(r, r) for r in rating.split(",") if r and r != "all"]
        if selected:
            where.append("p.rating IN (" + ",".join("?" for _ in selected) + ")")
            args.extend(selected)
        for token in re.split(r"[\s,]+", query.strip()):
            if not token:
                continue
            if token.startswith("order:"):
                orders = {"id": "p.id DESC", "id_desc": "p.id DESC", "id_asc": "p.id ASC",
                          "newest": "p.created DESC,p.id DESC", "oldest": "p.created ASC,p.id ASC",
                          "score": "p.score DESC,p.id DESC"}
                if token[6:] not in orders:
                    raise ValueError(f"不支持本地搜索语法：{token}。可从历史记录恢复已保存页面。")
                order = orders[token[6:]]
            elif token.startswith("rating:"):
                value = ratings.get(token[7:], token[7:])
                if value not in ("g", "s", "q", "e"):
                    raise ValueError("不支持此评分筛选")
                where.append("p.rating=?")
                args.append(value)
            elif token.startswith("date:"):
                values = token[5:].split("..")
                if len(values) > 2:
                    raise ValueError("日期格式：date:YYYY-MM-DD 或 date:起始日期..结束日期")
                for value in values:
                    if value:
                        date.fromisoformat(value)
                if not any(values):
                    raise ValueError("日期筛选至少需要一个边界")
                where.append("substr(p.created,1,10) BETWEEN ? AND ?")
                args.extend((values[0] or "0001-01-01", values[-1] or "9999-12-31"))
            else:
                if ":" in token or token.startswith("~") or "*" in token:
                    raise ValueError(f"不支持本地搜索语法：{token}")
                negative = token.startswith("-")
                tag = token[1:] if negative else token
                if re.search(r"[\u3400-\u9fff]", tag):
                    translated = translate(tag)
                    if not translated:
                        raise ValueError(f"未找到中文标签对应词：{tag}")
                    tag = translated
                where.append(("NOT " if negative else "") + "EXISTS (SELECT 1 FROM tags t WHERE t.post=p.id AND t.tag=?)")
                args.append(tag.lower())
        sql = "SELECT p.payload FROM posts p WHERE " + (" AND ".join(where) or "1")
        sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
        with closing(self.connect()) as conn:
            rows = conn.execute(sql, (*args, limit, (page-1)*limit)).fetchall()
        return [json.loads(row[0]) for row in rows]
