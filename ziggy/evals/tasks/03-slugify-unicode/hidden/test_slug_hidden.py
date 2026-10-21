import unittest

from text.slug import slugify


class HiddenSlugTests(unittest.TestCase):
    def test_accents(self):
        self.assertEqual(slugify("Crème Brûlée"), "creme-brulee")

    def test_decomposed_input(self):
        self.assertEqual(slugify("Café Niño"), "cafe-nino")

    def test_eszett(self):
        self.assertEqual(slugify("Straße"), "strasse")

    def test_turkish(self):
        self.assertEqual(slugify("Çağrı"), "cagri")
        self.assertEqual(slugify("İstanbul"), "istanbul")

    def test_non_latin_dropped(self):
        self.assertEqual(slugify("日本語"), "")
        self.assertEqual(slugify("Tokyo 東京 2024"), "tokyo-2024")

    def test_truncation_no_trailing_hyphen(self):
        self.assertEqual(slugify("hello world foo", max_length=6), "hello")
        self.assertEqual(slugify("hello world foo", max_length=7), "hello-w")
