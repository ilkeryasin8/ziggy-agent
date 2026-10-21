"""Slug generation."""
import re
import unicodedata

_EXTRA = {"ı": "i", "æ": "ae", "œ": "oe", "ø": "o",
          "đ": "d", "ł": "l"}


def _ascii_fold(text):
    text = text.casefold()
    text = "".join(_EXTRA.get(ch, ch) for ch in text)
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def slugify(text, max_length=None):
    slug = re.sub(r"[^a-z0-9]+", "-", _ascii_fold(text)).strip("-")
    if max_length is not None:
        slug = slug[:max_length].rstrip("-")
    return slug
