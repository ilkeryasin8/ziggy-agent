import os
import tempfile
import unittest

from files.serve import read_file


class ServeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "docs"))
        with open(os.path.join(self.root, "index.html"), "wb") as fh:
            fh.write(b"<h1>hi</h1>")
        with open(os.path.join(self.root, "docs", "a.txt"), "wb") as fh:
            fh.write(b"A")

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_top_level(self):
        self.assertEqual(read_file(self.root, "index.html"), b"<h1>hi</h1>")

    def test_read_nested(self):
        self.assertEqual(read_file(self.root, "docs/a.txt"), b"A")

    def test_missing(self):
        with self.assertRaises(FileNotFoundError):
            read_file(self.root, "nope.txt")
