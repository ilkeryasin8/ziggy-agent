import unittest

from records.builder import build_record


class HiddenBuilderTests(unittest.TestCase):
    def test_defaults_not_shared(self):
        build_record("First")
        second = build_record("Second")
        self.assertEqual(second["tags"], ["second"])

    def test_returned_meta_independent(self):
        a = build_record("A")
        a["meta"]["extra"] = True
        a["tags"].append("mutated")
        b = build_record("B")
        self.assertEqual(b["meta"], {"source": "builder"})
        self.assertEqual(b["tags"], ["b"])

    def test_caller_tags_not_mutated(self):
        tags = ["x"]
        rec = build_record("Y", tags)
        self.assertEqual(tags, ["x"])
        self.assertEqual(rec["tags"], ["x", "y"])
        self.assertIsNot(rec["tags"], tags)

    def test_caller_meta_not_mutated(self):
        meta = {"k": "v"}
        rec = build_record("Z", meta=meta)
        self.assertEqual(meta, {"k": "v"})
        self.assertEqual(rec["meta"], {"k": "v", "source": "builder"})
        self.assertIsNot(rec["meta"], meta)

    def test_tuple_tags_accepted(self):
        rec = build_record("T", ("a", "b"))
        self.assertEqual(rec["tags"], ["a", "b", "t"])
