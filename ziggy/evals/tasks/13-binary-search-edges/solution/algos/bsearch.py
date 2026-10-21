"""Binary search."""


def index_of(a, x):
    lo, hi = 0, len(a)
    while lo < hi:
        mid = (lo + hi) // 2
        if a[mid] < x:
            lo = mid + 1
        else:
            hi = mid
    if lo < len(a) and a[lo] == x:
        return lo
    return -1
