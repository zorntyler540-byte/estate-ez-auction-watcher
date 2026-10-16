"""Probe seller.ctbids.com for bidder-report / invoice API hints."""
from __future__ import annotations

import re
import sys

import requests

s = requests.Session()
s.headers["User-Agent"] = "Mozilla/5.0"

for base in (
    "https://seller.ctbids.com/",
    "https://seller.ctbids.com/login",
    "https://admin.ctbids.com/",
):
    try:
        r = s.get(base, timeout=25, allow_redirects=True)
        print(f"\n=== {base} -> {r.status_code} final={r.url} len={len(r.text)}")
        m = re.search(r"<title[^>]*>([^<]+)", r.text, re.I)
        if m:
            print("title:", m.group(1).strip())
        scripts = re.findall(r'src=["\']([^"\']+)["\']', r.text)
        print("scripts:", len(scripts))
        for u in scripts[:25]:
            print(" ", u)
        for kw in ("api", "bidder", "invoice", "report", "auth", "token"):
            hits = sorted(set(re.findall(rf"[^\s\"']*{kw}[^\s\"']{{0,60}}", r.text, re.I)))
            if hits:
                print(f"kw {kw}:", hits[:12])
    except Exception as e:
        print(f"\n=== {base} ERR {e}")

# Common seller API hosts
print("\n=== API host probes")
for url in (
    "https://seller.ctbids.com/services/api/v1/",
    "https://api.ctbids.com/services/api/v1/seller/",
    "https://sale.ctbids.com/services/api/v1/seller/",
    "https://sellerapi.ctbids.com/",
    "https://seller.ctbids.com/assets/env.js",
    "https://seller.ctbids.com/main.js",
    "https://seller.ctbids.com/runtime.js",
    "https://seller.ctbids.com/config.json",
):
    try:
        r = s.get(url, timeout=15, allow_redirects=True)
        ctype = r.headers.get("content-type", "")
        print(f"{url} -> {r.status_code} {ctype[:40]} len={len(r.content)}")
        if r.status_code == 200 and len(r.text) < 2000:
            print("  body:", r.text[:500].replace("\n", " "))
    except Exception as e:
        print(f"{url} -> ERR {e}")

sys.exit(0)
