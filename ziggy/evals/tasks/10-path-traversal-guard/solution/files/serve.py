"""Serve files from a root directory."""
import os


class AccessDenied(Exception):
    """Raised when a requested path is outside the served root."""


def read_file(root, rel_path):
    if "\x00" in rel_path:
        raise AccessDenied(rel_path)
    base = os.path.realpath(root)
    full = os.path.realpath(os.path.join(base, rel_path))
    if os.path.commonpath([base, full]) != base:
        raise AccessDenied(rel_path)
    with open(full, "rb") as fh:
        return fh.read()
