import unittest
from datetime import datetime, timezone

from timeutil.parse import format_utc, parse_timestamp


class ParseTests(unittest.TestCase):
    def test_naive_is_utc(self):
        dt = parse_timestamp("2024-03-01T10:00:00")
        self.assertEqual(dt, datetime(2024, 3, 1, 10, 0, tzinfo=timezone.utc))

    def test_z_suffix(self):
        dt = parse_timestamp("2024-03-01T10:00:00Z")
        self.assertEqual(dt.hour, 10)

    def test_format(self):
        dt = datetime(2024, 3, 1, 10, 5, 7, tzinfo=timezone.utc)
        self.assertEqual(format_utc(dt), "2024-03-01T10:05:07Z")
