"""Find how CTBids calls token refresh in the bundle."""
from __future__ import annotations

import re

import requests

js = requests.get("https://ctbids.com/static/js/main.19682176.js", timeout=60).text

# variable assigned to refresh path
for pat in [
    r'bM="/api/v1/buyer/token/refresh"',
    r"token/refresh",
    r"function O\(M\)",
    r"localStorage\.setItem\(t,",
    r"InvalidTokenError",
]:
    print("\n====", pat)
    for m in re.finditer(pat, js):
        i = m.start()
        print(js[max(0, i - 250) : i + 350])
        print("---")
        break

# Find usages of bM near fetch/axios
for m in re.finditer(r".{0,60}\bbM\b.{0,120}", js):
    s = m.group(0)
    if "http" in s or "post" in s.lower() or "fetch" in s.lower() or "axios" in s.lower() or "request" in s.lower() or "(" in s:
        if "token/refresh" in s or "bM=" in s:
            continue
        print("USE:", s[:200])
