"""Serve files from a root directory."""
import os


class AccessDenied(Exception):
    """Raised when a requested path is outside the served root."""


def read_file(root, rel_path):
    full = os.path.join(root, rel_path)
    with open(full, "rb") as fh:
        return fh.read()
