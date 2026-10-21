"""Pluralization helpers."""

UNCOUNTABLE = {"sheep", "fish", "series", "information", "equipment"}
IRREGULAR = {
    "man": "men",
    "woman": "women",
    "child": "children",
    "person": "people",
    "foot": "feet",
    "mouse": "mice",
}


def _match_case(source, result):
    if len(source) > 1 and source.isupper():
        return result.upper()
    if source[:1].isupper():
        return result[:1].upper() + result[1:]
    return result


def pluralize(word, count=2):
    if count == 1 or not word:
        return word
    low = word.lower()
    if low in UNCOUNTABLE:
        return word
    if low in IRREGULAR:
        return _match_case(word, IRREGULAR[low])
    upper = len(word) > 1 and word.isupper()
    if low.endswith(("s", "x", "z", "ch", "sh")):
        return word + ("ES" if upper else "es")
    if low.endswith("y") and len(low) > 1 and low[-2] not in "aeiou":
        return word[:-1] + ("IES" if upper else "ies")
    return word + ("S" if upper else "s")


def count_label(n, word):
    return f"{n} {pluralize(word, n)}"
