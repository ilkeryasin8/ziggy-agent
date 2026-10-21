import unittest

from text.slug import slugify


class SlugTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_punctuation_and_underscores(self):
        self.assertEqual(slugify("  --Hello__World!! "), "hello-world")

    def test_max_length(self):
        self.assertEqual(slugify("Hello World", max_length=5), "hello")
