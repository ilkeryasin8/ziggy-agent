import os
import tempfile
import unittest

from files.serve import AccessDenied, read_file


class HiddenServeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = os.path.realpath(self.tmp.name)
        self.root = os.path.join(base, "pub")
        self.sibling = os.path.join(base, "pub-private")
        self.outside = os.path.join(base, "secret.txt")
        os.makedirs(os.path.join(self.root, "docs"))
        os.makedirs(self.sibling)
        for path, data in (
            (os.path.join(self.root, "index.html"), b"ok"),
            (os.path.join(self.sibling, "key.txt"), b"private"),
            (self.outside, b"secret"),
        ):
            with open(path, "wb") as fh:
                fh.write(data)

    def tearDown(self):
        self.tmp.cleanup()

    def test_parent_traversal(self):
        with self.assertRaises(AccessDenied):
            read_file(self.root, "../secret.txt")
        with self.assertRaises(AccessDenied):
            read_file(self.root, "docs/../../secret.txt")

    def test_sibling_prefix_directory(self):
        with self.assertRaises(AccessDenied):
            read_file(self.root, "../pub-private/key.txt")

    def test_absolute_path(self):
        with self.assertRaises(AccessDenied):
            read_file(self.root, self.outside)

    def test_inner_dotdot_allowed(self):
        self.assertEqual(read_file(self.root, "docs/../index.html"), b"ok")

    def test_symlink_escape(self):
        link = os.path.join(self.root, "link.txt")
        os.symlink(self.outside, link)
        with self.assertRaises(AccessDenied):
            read_file(self.root, "link.txt")

    def test_missing_inside_root(self):
        with self.assertRaises(FileNotFoundError):
            read_file(self.root, "docs/missing.txt")

    def test_nul_byte(self):
        with self.assertRaises(AccessDenied):
            read_file(self.root, "index.html\x00.png")

    def test_root_with_trailing_slash(self):
        self.assertEqual(read_file(self.root + "/", "index.html"), b"ok")
