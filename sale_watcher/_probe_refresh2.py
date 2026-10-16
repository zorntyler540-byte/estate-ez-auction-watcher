"""Extract buyer/token/refresh call shape from CTBids JS."""
from __future__ import annotations

import re

import requests
from playwright.sync_api import sync_playwright

from watcher import profile_dir

js = requests.get("https://ctbids.com/static/js/main.19682176.js", timeout=60).text
idx = js.find("buyer/token/refresh")
print("idx", idx)
print(js[max(0, idx - 400) : idx + 500])

# also search for token/refresh usages
for m in re.finditer(r".{0,120}token/refresh.{0,200}", js):
    print("---")
    print(m.group(0)[:350])

with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir()),
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = context.pages[0] if context.pages else context.new_page()
    page.goto("https://ctbids.com/", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1000)
    refresh = page.evaluate("() => localStorage.getItem('refresh')")
    access = page.evaluate("() => localStorage.getItem('access')")
    context.close()

# Try body shapes against buyer/token/refresh
url = "https://api.ctbids.com/services/api/v1/buyer/token/refresh"
bodies = [
    {"data": {"refresh": refresh}},
    {"data": {"refreshToken": refresh}},
    {"data": {"token": refresh}},
    {"refresh": refresh},
    {"refreshToken": refresh},
    {"token": refresh},
    {"access": access, "refresh": refresh},
    {"data": {"access": access, "refresh": refresh}},
]
for body in bodies:
    for auth_tok in (None, refresh, access):
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if auth_tok:
            headers["Authorization"] = f"Bearer {auth_tok}"
        r = requests.post(url, json=body, headers=headers, timeout=20)
        print(r.status_code, "auth", "Y" if auth_tok else "N", list(body.keys()), r.text[:180])
