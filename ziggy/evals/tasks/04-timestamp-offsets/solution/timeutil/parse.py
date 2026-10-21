"""Timestamp helpers; everything is normalised to UTC."""
from datetime import datetime, timezone


def parse_timestamp(value):
    dt = datetime.fromisoformat(value.strip())
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_utc(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
