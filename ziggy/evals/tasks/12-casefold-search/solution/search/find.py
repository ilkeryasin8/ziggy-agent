"""Substring search over a list of strings."""
import unicodedata


def _normalize(text):
    folded = unicodedata.normalize("NFC", text).casefold()
    return unicodedata.normalize("NFC", folded)


def search(items, query):
    needle = _normalize(query)
    return [item for item in items if needle in _normalize(item)]
