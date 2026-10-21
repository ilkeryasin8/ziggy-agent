import json
import os
import tempfile
import unittest

from conf.config import DEFAULTS, load_config


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(load_config(), DEFAULTS)

    def test_file_deep_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"port": 9000, "db": {"host": "db.internal"}}, fh)
            cfg = load_config(path)
        self.assertEqual(cfg["port"], 9000)
        self.assertEqual(cfg["db"], {"host": "db.internal", "port": 5432})

    def test_missing_file_ignored(self):
        self.assertEqual(load_config("/nonexistent/c.json"), DEFAULTS)
