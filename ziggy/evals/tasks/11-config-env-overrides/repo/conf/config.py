"""Configuration loading: defaults, then an optional JSON file."""
import copy
import json
import os

DEFAULTS = {
    "debug": False,
    "port": 8000,
    "db": {"host": "localhost", "port": 5432},
}


def _merge(base, extra):
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def load_config(path=None):
    cfg = copy.deepcopy(DEFAULTS)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            _merge(cfg, json.load(fh))
    return cfg
