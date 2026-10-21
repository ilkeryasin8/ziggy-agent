import unittest

from csvio.writer import rows_to_csv, to_csv_line


class WriterTests(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(to_csv_line(["a", 1, None]), "a,1,")

    def test_comma(self):
        self.assertEqual(to_csv_line(["a,b", "c"]), '"a,b",c')

    def test_rows(self):
        self.assertEqual(rows_to_csv([["a", "b"], ["c", "d"]]), "a,b\nc,d\n")
