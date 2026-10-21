import unittest

from text.plural import count_label, pluralize


class HiddenPluralTests(unittest.TestCase):
    def test_es_endings(self):
        for word, plural in (("box", "boxes"), ("bus", "buses"), ("church", "churches"),
                             ("dish", "dishes"), ("buzz", "buzzes")):
            self.assertEqual(pluralize(word), plural)

    def test_y_endings(self):
        self.assertEqual(pluralize("city"), "cities")
        self.assertEqual(pluralize("day"), "days")
        self.assertEqual(pluralize("toy"), "toys")

    def test_irregular_and_uncountable(self):
        self.assertEqual(pluralize("child"), "children")
        self.assertEqual(pluralize("woman"), "women")
        self.assertEqual(pluralize("mouse"), "mice")
        self.assertEqual(pluralize("sheep"), "sheep")
        self.assertEqual(pluralize("information"), "information")

    def test_capitalisation(self):
        self.assertEqual(pluralize("Child"), "Children")
        self.assertEqual(pluralize("BOX"), "BOXES")
        self.assertEqual(pluralize("City"), "Cities")
        self.assertEqual(pluralize("Sheep"), "Sheep")
        self.assertEqual(pluralize("CAT"), "CATS")

    def test_count_one(self):
        self.assertEqual(pluralize("box", 1), "box")
        self.assertEqual(pluralize("box", 0), "boxes")
        self.assertEqual(pluralize("child", 1), "child")

    def test_count_label(self):
        self.assertEqual(count_label(1, "city"), "1 city")
        self.assertEqual(count_label(0, "city"), "0 cities")
        self.assertEqual(count_label(5, "person"), "5 people")

    def test_empty(self):
        self.assertEqual(pluralize(""), "")
