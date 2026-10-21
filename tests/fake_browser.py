#!/usr/bin/env python3
"""Stand-in for a headless Chromium taking a screenshot (screens.py; tests/test_screens.py): fetches the URL (the last
argument) so the app must really answer, then writes a fake PNG to --screenshot=<path>. A URL with "broken" in it
fails, as a page that does not load would."""
import sys
import urllib.request

url = sys.argv[-1]
shot = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--screenshot="))
if "broken" in url:
    sys.exit(1)
with urllib.request.urlopen(url, timeout=10) as resp:
    body = resp.read()
with open(shot, "wb") as f:
    f.write(b"\x89PNG\r\n\x1a\n" + body[:64])
