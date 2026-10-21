import unittest

from records.builder import build_record


class BuilderTests(unittest.TestCase):
    def test_with_explicit_args(self):
        rec = build_record("Widget", ["a"], {"x": 1})
        self.assertEqual(rec["tags"], ["a", "widget"])
        self.assertEqual(rec["meta"], {"x": 1, "source": "builder"})
        self.assertEqual(rec["name"], "Widget")

    def test_defaults_single_call(self):
        rec = build_record("Solo")
        self.assertEqual(rec["tags"], ["solo"])
        self.assertEqual(rec["meta"], {"source": "builder"})
