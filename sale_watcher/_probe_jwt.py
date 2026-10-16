"""Decode JWT exp + find refresh POST body in CTBids JS."""
from __future__ import annotations

import base64
import json
import re
import time

import requests
from playwright.sync_api import sync_playwright

from watcher import profile_dir


def decode_jwt(tok: str) -> dict:
    part = tok.split(".")[1]
    part += "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part.encode()))


with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir()),
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = context.pages[0] if context.pages else context.new_page()
    page.goto("https://ctbids.com/", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1000)
    access = page.evaluate("() => localStorage.getItem('access')")
    refresh = page.evaluate("() => localStorage.getItem('refresh')")
    context.close()

now = time.time()
for name, tok in [("access", access), ("refresh", refresh)]:
    payload = decode_jwt(tok)
    exp = payload.get("exp")
    print(name, "exp", exp, "in", (exp or 0) - now, "sec", "keys", list(payload.keys()))
    print(" ", {k: payload[k] for k in payload if k not in ("exp", "iat")})

js = requests.get("https://ctbids.com/static/js/main.19682176.js", timeout=60).text
# Find the module that defines API path constants including buyer/token/refresh
anchor = 'bM="/api/v1/buyer/token/refresh"'
i = js.find(anchor)
# Walk backwards to exports / object that maps names
chunk = js[i - 50 : i + 80]
print("anchor chunk", chunk)

# Search for refresh invocations with localStorage getItem refresh
for pat in [
    r"getItem\([\"']refresh[\"']\).{0,200}",
    r"getItem\(t\).{0,200}",
    r"buyer/token/refresh.{0,5}",
    r"token/refresh[\"'].{0,300}",
]:
    print("\nPAT", pat)
    count = 0
    for m in re.finditer(pat, js):
        count += 1
        if count <= 5:
            print(m.group(0)[:280])
    print("total", count)
