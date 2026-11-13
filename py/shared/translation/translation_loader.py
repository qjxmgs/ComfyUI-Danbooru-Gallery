"""Independent ffdkj SQLite dictionary used by gallery translations."""
from contextlib import closing
from pathlib import Path
import re
import sqlite3
import unicodedata

SOURCE_URL = "https://raw.githubusercontent.com/ffdkj/ffdkj-Danbooru_Tag-Chinese-English-Translation-Table/main/tag.sqlite"
DEFAULT_DICTIONARY = Path(__file__).resolve().parents[1] / "data" / "tag.sqlite"


def normalize_tag(tag):
    text = unicodedata.normalize("NFKC", tag).strip().casefold()
    return re.sub(r"\\([()\[\]{}])", r"\1", text).replace(" ", "_")


def validate_dictionary(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity check failed")
        columns = {row[1]: row for row in conn.execute("PRAGMA table_info(tags)")}
        for name, kind in {"name": "TEXT", "cn_name": "TEXT", "category": "INTEGER", "post_count": "INTEGER"}.items():
            if name not in columns or columns[name][2].upper() != kind:
                raise ValueError(f"Invalid tags.{name} column")
        if not columns["name"][5]:
            raise ValueError("tags.name must be the primary key")
        count, invalid = conn.execute("""SELECT COUNT(*), SUM(
            name IS NULL OR typeof(name) != 'text' OR trim(name) = ''
            OR cn_name IS NULL OR typeof(cn_name) != 'text' OR trim(cn_name) = ''
            OR category IS NULL OR category NOT IN (0,1,3,4,5)
            OR post_count IS NULL OR typeof(post_count) != 'integer' OR post_count < 10)
            FROM tags""").fetchone()
        if not count or invalid:
            raise ValueError("Dictionary has no tags or contains invalid rows")
        return count


class TranslationLoader:
    def __init__(self, dictionary_path=None):
        self.dictionary_path = Path(dictionary_path or DEFAULT_DICTIONARY)

    def load_all(self):
        return self.dictionary_path.is_file()

    def _connect(self):
        return sqlite3.connect(self.dictionary_path.resolve().as_uri() + "?mode=ro", uri=True)

    def translate_tags_batch(self, tags):
        tags = list(tags)
        if not tags or not self.load_all():
            return {}
        keys = list(dict.fromkeys(normalize_tag(tag) for tag in tags))
        translations = {}
        with closing(self._connect()) as conn:
            for offset in range(0, len(keys), 500):
                batch = keys[offset:offset + 500]
                placeholders = ",".join("?" for _ in batch)
                translations.update(conn.execute(
                    f"SELECT name, cn_name FROM tags WHERE name IN ({placeholders})", batch))
        return {tag: translations[normalize_tag(tag)] for tag in tags if normalize_tag(tag) in translations}

    def get_chinese(self, tag):
        return self.translate_tags_batch([tag]).get(tag)

    translate_tag = get_chinese
    load_translation_data = load_all

    def search_records(self, query, limit=50, min_post_count=100):
        if not query.strip() or not self.load_all():
            return []
        with closing(self._connect()) as conn:
            rows = conn.execute("""SELECT name, cn_name, category, post_count,
                CASE WHEN cn_name = ? THEN 10 WHEN instr(cn_name, ?) = 1 THEN 8 ELSE 4 END AS score
                FROM tags WHERE post_count >= ? AND instr(cn_name, ?) > 0
                ORDER BY score DESC, post_count DESC, name LIMIT ?""",
                (query.strip(), query.strip(), min_post_count, query.strip(), max(1, min(int(limit), 100)))).fetchall()
        return [dict(tag=r[0], translation_cn=r[1], category=r[2], post_count=r[3], match_score=r[4]) for r in rows]

    def search_english(self, query, limit=20, min_post_count=100):
        if not query.strip() or not self.load_all():
            return []
        key = normalize_tag(query)
        with closing(self._connect()) as conn:
            rows = conn.execute("""SELECT name, cn_name, category, post_count
                FROM tags WHERE post_count >= ? AND substr(name, 1, length(?)) = ?
                ORDER BY (name = ?) DESC, post_count DESC, name LIMIT ?""",
                (min_post_count, key, key, key, max(1, min(int(limit), 100)))).fetchall()
        return [dict(name=r[0], translation=r[1], category=r[2], post_count=r[3], aliases=[]) for r in rows]

    def search_chinese(self, query, limit=50):
        return [(r["tag"], r["translation_cn"]) for r in self.search_records(query, limit)]

    def search_chinese_tags(self, query, limit=10, min_post_count=100):
        return self.search_records(query, limit, min_post_count)

    def get_english(self, text):
        records = self.search_records(text, 1, 10)
        return records[0]["tag"] if records and records[0]["translation_cn"] == text.strip() else None

    def add_translations_to_tags(self, tags):
        translations = self.translate_tags_batch(t.get("tag", "") for t in tags)
        for tag in tags:
            tag["translation_cn"] = translations.get(tag.get("tag", ""))
        return tags

    def get_stats(self):
        if not self.load_all():
            return {"loaded": False, "en_to_cn_count": 0, "cn_to_en_count": 0}
        with closing(self._connect()) as conn:
            total, chinese = conn.execute("SELECT COUNT(*), COUNT(DISTINCT cn_name) FROM tags").fetchone()
        return {"loaded": True, "en_to_cn_count": total, "cn_to_en_count": chinese}


_translation_loader = TranslationLoader()


def get_translation_loader():
    return _translation_loader
