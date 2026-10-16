"""Extract seller auth + bidder report API details from SPA JS."""
from __future__ import annotations

import re

import requests

s = requests.Session()
s.headers["User-Agent"] = "Mozilla/5.0"
base = "https://seller.ctbids.com"
urls = [
    f"{base}/static/js/main.82a9026c.chunk.js",
    f"{base}/static/js/2.eb37b50b.chunk.js",
]
blob = ""
for url in urls:
    blob += s.get(url, timeout=60).text

# Find host constants near reports.ctbids.com
for needle in (
    "reports.ctbids.com",
    "report/bidder/list",
    "invoice/list",
    "seller.ctbids.com",
    "connect/token",
    "authorization",
    "Bearer",
):
    print(f"\n===== {needle} =====")
    start = 0
    n = 0
    while n < 4:
        i = blob.find(needle, start)
        if i < 0:
            break
        print(repr(blob[max(0, i - 120) : i + 180]))
        print("---")
        start = i + len(needle)
        n += 1

# Look for URL composition: REACT_APP or hardcoded services base
print("\n===== services base candidates =====")
for m in re.finditer(r"https://[a-z0-9.-]+\.ctbids\.com/services", blob):
    print(m.group(0), "@", m.start())

# Route for bidder report
print("\n===== routes near reportbidder =====")
for m in re.finditer(r"/reports/[a-zA-Z0-9_-]+", blob):
    pass
routes = sorted(set(re.findall(r"/reports/[a-zA-Z0-9_-]+", blob)))
print(routes)

# Payload field hints near report/bidder/list
i = blob.find("report/bidder/list")
print("\n===== wider ctx report/bidder/list =====")
print(repr(blob[max(0, i - 400) : i + 600]))

# Auth token endpoint patterns
print("\n===== auth-ish path constants =====")
auth_paths = sorted(
    set(
        re.findall(
            r"/api/v1/(?:auth|seller|admin|user|login|connect)[a-zA-Z0-9_./-]{0,60}",
            blob,
        )
    )
)
for p in auth_paths:
    print(p)
