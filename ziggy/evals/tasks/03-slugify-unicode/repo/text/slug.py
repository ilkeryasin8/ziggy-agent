"""Slug generation."""
import re


def slugify(text, max_length=None):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if max_length is not None:
        slug = slug[:max_length]
    return slug
