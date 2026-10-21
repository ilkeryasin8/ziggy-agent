import csv
import io
import unittest

from csvio.writer import rows_to_csv, to_csv_line


class HiddenWriterTests(unittest.TestCase):
    def test_quote_escaping(self):
        self.assertEqual(to_csv_line(['say "hi"']), '"say ""hi"""')

    def test_newline_quoted(self):
        self.assertEqual(to_csv_line(["a\nb", "c"]), '"a\nb",c')
        self.assertEqual(to_csv_line(["a\rb"]), '"a\rb"')

    def test_comma_and_quote_together(self):
        self.assertEqual(to_csv_line(['a,"b"']), '"a,""b"""')

    def test_no_needless_quotes(self):
        self.assertEqual(to_csv_line(["plain", "with space", 3.5, None]), "plain,with space,3.5,")

    def test_roundtrip(self):
        rows = [
            ["name", "note"],
            ['Ann "AJ" Jones', "line one\nline two"],
            ["Bob, Jr.", 'he said "ok", twice'],
            ["", "tail\r\nwindows"],
        ]
        text = rows_to_csv(rows)
        parsed = list(csv.reader(io.StringIO(text, newline="")))
        self.assertEqual(parsed, rows)

    def test_rows_terminator(self):
        self.assertEqual(rows_to_csv([['x"y', "z"]]), '"x""y",z\n')
