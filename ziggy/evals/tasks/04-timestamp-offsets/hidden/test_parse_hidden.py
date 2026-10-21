import unittest
from datetime import datetime, timedelta, timezone

from timeutil.parse import format_utc, parse_timestamp


class HiddenParseTests(unittest.TestCase):
    def test_positive_offset(self):
        dt = parse_timestamp("2024-03-01T10:00:00+02:00")
        self.assertEqual(dt, datetime(2024, 3, 1, 8, 0, tzinfo=timezone.utc))
        self.assertEqual(dt.utcoffset(), timedelta(0))

    def test_negative_half_hour_offset(self):
        dt = parse_timestamp("2024-03-01T10:00:00-05:30")
        self.assertEqual(dt, datetime(2024, 3, 1, 15, 30, tzinfo=timezone.utc))

    def test_offset_crosses_midnight(self):
        dt = parse_timestamp("2024-03-01T01:00:00+03:00")
        self.assertEqual((dt.month, dt.day, dt.hour), (2, 29, 22))

    def test_space_separator_and_date_only(self):
        self.assertEqual(parse_timestamp("2024-03-01 10:00:00-05:00").hour, 15)
        self.assertEqual(parse_timestamp("2024-03-01"),
                         datetime(2024, 3, 1, tzinfo=timezone.utc))

    def test_always_utc_tzinfo(self):
        for text in ("2024-03-01T10:00:00", "2024-03-01T10:00:00Z", "2024-03-01T10:00:00+09:00"):
            self.assertIs(parse_timestamp(text).tzinfo, timezone.utc)

    def test_format_roundtrip_with_offset(self):
        dt = parse_timestamp("2024-03-01T23:30:00-02:00")
        self.assertEqual(format_utc(dt), "2024-03-02T01:30:00Z")

    def test_format_naive_is_utc(self):
        self.assertEqual(format_utc(datetime(2024, 3, 1, 10, 0, 0)), "2024-03-01T10:00:00Z")
