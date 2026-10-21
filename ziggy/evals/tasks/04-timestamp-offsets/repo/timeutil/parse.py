"""Timestamp helpers; everything is normalised to UTC."""
from datetime import datetime, timezone


def parse_timestamp(value):
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=timezone.utc)


def format_utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
