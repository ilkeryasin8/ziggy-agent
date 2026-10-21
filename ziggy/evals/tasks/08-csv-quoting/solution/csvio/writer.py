"""Minimal CSV writer."""

_SPECIAL = (",", '"', "\n", "\r")


def to_csv_line(fields):
    out = []
    for field in fields:
        text = "" if field is None else str(field)
        if any(ch in text for ch in _SPECIAL):
            text = '"' + text.replace('"', '""') + '"'
        out.append(text)
    return ",".join(out)


def rows_to_csv(rows):
    return "".join(to_csv_line(row) + "\n" for row in rows)
