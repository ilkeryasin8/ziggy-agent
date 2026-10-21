import io
import unittest

from inventory.cli import main


class CliTests(unittest.TestCase):
    def run_cli(self, argv):
        out = io.StringIO()
        code = main(argv, stdout=out)
        return code, out.getvalue()

    def test_text_output_sorted(self):
        code, text = self.run_cli([])
        self.assertEqual(code, 0)
        self.assertEqual(text, "bolt: 120\nnut: 80\nwasher: 5\n")

    def test_min_qty(self):
        _, text = self.run_cli(["--min-qty", "50"])
        self.assertEqual(text, "bolt: 120\nnut: 80\n")
