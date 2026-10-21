"""Build record dictionaries."""


def build_record(name, tags=None, meta=None):
    new_tags = list(tags) if tags is not None else []
    new_tags.append(name.lower())
    new_meta = dict(meta) if meta is not None else {}
    new_meta["source"] = "builder"
    return {"name": name, "tags": new_tags, "meta": new_meta}
