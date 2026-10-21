import json
import os
import tempfile
import unittest
from unittest import mock

from conf.config import DEFAULTS, load_config


class HiddenConfigTests(unittest.TestCase):
    def test_int_override(self):
        cfg = load_config(env={"APP_PORT": "9001"})
        self.assertEqual(cfg["port"], 9001)
        self.assertIsInstance(cfg["port"], int)

    def test_bool_override(self):
        self.assertIs(load_config(env={"APP_DEBUG": "True"})["debug"], True)
        self.assertIs(load_config(env={"APP_DEBUG": "yes"})["debug"], True)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"debug": True}, fh)
            self.assertIs(load_config(path, env={"APP_DEBUG": "off"})["debug"], False)
            self.assertIs(load_config(path, env={"APP_DEBUG": "0"})["debug"], False)

    def test_bad_bool(self):
        with self.assertRaises(ValueError):
            load_config(env={"APP_DEBUG": "maybe"})

    def test_bad_int(self):
        with self.assertRaises(ValueError):
            load_config(env={"APP_PORT": "eighty"})

    def test_nested(self):
        cfg = load_config(env={"APP_DB__HOST": "db.prod", "APP_DB__PORT": "6432"})
        self.assertEqual(cfg["db"], {"host": "db.prod", "port": 6432})

    def test_env_beats_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"port": 7000}, fh)
            self.assertEqual(load_config(path, env={"APP_PORT": "7100"})["port"], 7100)
            self.assertEqual(load_config(path, env={})["port"], 7000)

    def test_ignored_variables(self):
        env = {"OTHER_PORT": "1", "APP_UNKNOWN": "x", "APP_DB__NOPE": "y", "APP_PORT__X": "z",
               "PORT": "5"}
        self.assertEqual(load_config(env=env), DEFAULTS)

    def test_custom_prefix(self):
        cfg = load_config(env={"MYAPP_PORT": "1234", "APP_PORT": "1"}, prefix="MYAPP_")
        self.assertEqual(cfg["port"], 1234)

    def test_defaults_untouched_and_os_environ_default(self):
        with mock.patch.dict(os.environ, {"APP_PORT": "4321"}):
            self.assertEqual(load_config()["port"], 4321)
        self.assertEqual(DEFAULTS["port"], 8000)
        self.assertEqual(DEFAULTS["db"]["port"], 5432)
