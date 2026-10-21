"""Substring search over a list of strings."""


def search(items, query):
    needle = query.lower()
    return [item for item in items if needle in item.lower()]
