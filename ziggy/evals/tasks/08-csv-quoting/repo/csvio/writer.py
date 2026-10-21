"""Minimal CSV writer."""


def to_csv_line(fields):
    out = []
    for field in fields:
        text = "" if field is None else str(field)
        if "," in text:
            text = '"' + text + '"'
        out.append(text)
    return ",".join(out)


def rows_to_csv(rows):
    return "".join(to_csv_line(row) + "\n" for row in rows)
