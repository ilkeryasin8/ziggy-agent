import unittest

from search.find import search


class HiddenSearchTests(unittest.TestCase):
    def test_eszett(self):
        self.assertEqual(search(["Straße", "Strand"], "STRASSE"), ["Straße"])
        self.assertEqual(search(["STRASSE"], "straße"), ["STRASSE"])

    def test_greek_final_sigma(self):
        self.assertEqual(search(["ΟΔΟΣ"], "οδοσ"),
                         ["ΟΔΟΣ"])

    def test_decomposed_vs_composed(self):
        self.assertEqual(search(["café au lait"], "CAFÉ"), ["café au lait"])
        self.assertEqual(search(["café"], "cafÉ"), ["café"])

    def test_accents_not_ignored(self):
        self.assertEqual(search(["café"], "cafe"), [])

    def test_empty_query_returns_all(self):
        self.assertEqual(search(["a", "b"], ""), ["a", "b"])

    def test_order_and_duplicates(self):
        self.assertEqual(search(["Bx", "ax", "BX"], "bx"), ["Bx", "BX"])
