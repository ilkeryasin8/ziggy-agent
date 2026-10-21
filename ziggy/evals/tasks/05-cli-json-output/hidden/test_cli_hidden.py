import io
import json
import unittest

from inventory.cli import main

ITEMS = [
    {"name": "washer", "qty": 5},
    {"name": "bolt", "qty": 120},
    {"name": "nut", "qty": 80},
]


def run(argv, items=ITEMS):
    out = io.StringIO()
    code = main(argv, stdout=out, items=items)
    return code, out.getvalue()


class HiddenCliTests(unittest.TestCase):
    def test_json_shape_and_order(self):
        code, text = run(["--json"])
        self.assertEqual(code, 0)
        data = json.loads(text)
        self.assertEqual([i["name"] for i in data["items"]], ["bolt", "nut", "washer"])
        self.assertEqual(data["items"][0], {"name": "bolt", "qty": 120})
        self.assertEqual(data["total"], 205)

    def test_json_with_min_qty(self):
        _, text = run(["--json", "--min-qty", "50"])
        data = json.loads(text)
        self.assertEqual([i["name"] for i in data["items"]], ["bolt", "nut"])
        self.assertEqual(data["total"], 200)

    def test_json_flag_order_independent(self):
        _, a = run(["--min-qty", "50", "--json"])
        _, b = run(["--json", "--min-qty", "50"])
        self.assertEqual(json.loads(a), json.loads(b))

    def test_json_empty(self):
        code, text = run(["--json", "--min-qty", "1000"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text), {"items": [], "total": 0})

    def test_json_only_document(self):
        _, text = run(["--json"])
        self.assertEqual(text.strip(), json.dumps(json.loads(text)).strip()
                         if text.strip().startswith("{") and "\n" not in text.strip() else text.strip())
        json.loads(text)

    def test_text_mode_unchanged(self):
        _, text = run([])
        self.assertEqual(text, "bolt: 120\nnut: 80\nwasher: 5\n")
        with self.assertRaises(ValueError):
            json.loads(text)
