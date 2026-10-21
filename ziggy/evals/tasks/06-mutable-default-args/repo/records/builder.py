"""Build record dictionaries."""


def build_record(name, tags=[], meta={}):
    tags.append(name.lower())
    meta["source"] = "builder"
    return {"name": name, "tags": tags, "meta": meta}
