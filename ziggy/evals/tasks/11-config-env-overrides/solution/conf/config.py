"""Configuration loading: defaults, an optional JSON file, then environment."""
import copy
import json
import os

DEFAULTS = {
    "debug": False,
    "port": 8000,
    "db": {"host": "localhost", "port": 5432},
}

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _merge(base, extra):
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def _coerce(text, current):
    if isinstance(current, bool):
        lowered = text.strip().lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        raise ValueError(f"invalid boolean: {text!r}")
    if isinstance(current, int):
        return int(text)
    if isinstance(current, float):
        return float(text)
    return text


def _apply_env(cfg, env, prefix):
    for name, text in env.items():
        if not name.startswith(prefix):
            continue
        parts = name[len(prefix):].lower().split("__")
        node = cfg
        for part in parts[:-1]:
            node = node.get(part) if isinstance(node, dict) else None
            if not isinstance(node, dict):
                break
        else:
            leaf = parts[-1]
            if leaf in node and not isinstance(node[leaf], dict):
                node[leaf] = _coerce(text, node[leaf])


def load_config(path=None, env=None, prefix="APP_"):
    cfg = copy.deepcopy(DEFAULTS)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            _merge(cfg, json.load(fh))
    _apply_env(cfg, os.environ if env is None else env, prefix)
    return cfg
