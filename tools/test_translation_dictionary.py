from contextlib import closing
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gallery_translation", ROOT / "py/shared/translation/translation_loader.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DictionaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "tag.sqlite"
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("CREATE TABLE tags(name TEXT PRIMARY KEY, category INTEGER, cn_name TEXT, post_count INTEGER)")
            conn.executemany("INSERT INTO tags VALUES (?,?,?,?)", [
                ("blue_eyes", 0, "蓝眼睛", 1000),
                ("alice_(series)", 4, "爱丽丝", 10),
                ("blue_hair", 0, "蓝头发", 500),
            ])
        self.loader = module.TranslationLoader(self.path)

    def test_normalization_and_low_frequency(self):
        self.assertEqual(self.loader.get_chinese(" BLUE EYES "), "蓝眼睛")
        self.assertEqual(self.loader.get_chinese(r"alice \(series\)"), "爱丽丝")
        self.assertIsNone(self.loader.get_chinese("unknown"))

    def test_search_and_cached_translation_replacement(self):
        self.assertEqual(self.loader.search_records("蓝")[0]["tag"], "blue_eyes")
        tags = [{"tag": "blue_eyes", "translation_cn": "旧翻译"}, {"tag": "unknown", "translation_cn": "旧翻译"}]
        self.loader.add_translations_to_tags(tags)
        self.assertEqual(tags[0]["translation_cn"], "蓝眼睛")
        self.assertIsNone(tags[1]["translation_cn"])

    def test_post_count_threshold(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.executemany("INSERT INTO tags VALUES (?,?,?,?)", [
                ("blue_99", 0, "蓝99", 99),
                ("blue_100", 0, "蓝100", 100),
            ])
        self.assertNotIn("blue_99", [r["name"] for r in self.loader.search_english("blue")])
        self.assertIn("blue_100", [r["name"] for r in self.loader.search_english("blue")])
        self.assertNotIn("blue_99", [r["tag"] for r in self.loader.search_records("蓝")])
        self.assertIn("blue_100", [r["tag"] for r in self.loader.search_records("蓝")])
        self.assertIn("blue_99", [r["name"] for r in self.loader.search_english("blue", min_post_count=10)])
        self.assertEqual(self.loader.search_records("蓝", min_post_count=1001), [])
        self.assertEqual(self.loader.get_chinese("blue_99"), "蓝99")

    def test_update_is_visible_without_restart(self):
        self.assertEqual(self.loader.get_chinese("blue_eyes"), "蓝眼睛")
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE tags SET cn_name = '蓝色眼睛' WHERE name = 'blue_eyes'")
        self.assertEqual(self.loader.get_chinese("blue_eyes"), "蓝色眼睛")

    def test_validation_and_missing_file(self):
        self.assertEqual(module.validate_dictionary(self.path), 3)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE tags SET post_count = 0")
        with self.assertRaises(ValueError):
            module.validate_dictionary(self.path)
        missing = module.TranslationLoader(self.path.parent / "missing.sqlite")
        self.assertEqual(missing.translate_tags_batch(["blue_eyes"]), {})
        self.assertFalse(missing.dictionary_path.exists())


if __name__ == "__main__":
    unittest.main()
