"""Simple 1-based pagination helpers."""


def total_pages(total_items, per_page):
    return (total_items + per_page - 1) // per_page


def paginate(items, page, per_page):
    if page < 1 or per_page < 1:
        raise ValueError("page and per_page must be >= 1")
    start = (page - 1) * per_page
    pages = total_pages(len(items), per_page)
    return {
        "items": list(items[start:start + per_page]),
        "page": page,
        "total_pages": pages,
        "has_next": page < pages,
    }
