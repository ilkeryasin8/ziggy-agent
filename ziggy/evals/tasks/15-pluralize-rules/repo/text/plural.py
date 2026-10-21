"""Pluralization helpers."""


def pluralize(word):
    return word + "s"


def count_label(n, word):
    return f"{n} {pluralize(word)}"
