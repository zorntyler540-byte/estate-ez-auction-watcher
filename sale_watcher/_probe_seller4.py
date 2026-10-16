"""Extract bidder report columns + invoice list usage from seller SPA."""
from __future__ import annotations

import re

import requests

s = requests.Session()
s.headers["User-Agent"] = "Mozilla/5.0"
blob = s.get(
    "https://seller.ctbids.com/static/js/main.82a9026c.chunk.js", timeout=60
).text

i = blob.find('name:"bidder report"')
print("===== bidder report config (2k) =====")
print(blob[i : i + 2500])

# buyerbidstatus / unpaid-ish property names nearby
print("\n===== property names in bidder report window =====")
window = blob[i : i + 4000]
props = re.findall(r'property:"([a-zA-Z0-9_]+)"', window)
print(props)
names = re.findall(r'name:"([^"]+)"', window)
print("col names:", names)

# How isReport requests are posted
print("\n===== isReport request pattern =====")
for needle in ("isReport", "reports.ctbids.com", "V+", "ve(V"):
    pass
j = blob.find("function ve(")
print("ve fn:", repr(blob[j : j + 250]) if j >= 0 else None)

# Find where config.url is called with isReport
for m in re.finditer(r"isReport", blob):
    ctx = blob[m.start() - 80 : m.start() + 120]
    if "url" in ctx or "post" in ctx.lower() or "get" in ctx.lower():
        print(repr(ctx))
        print("---")

# Auth token
k = blob.find("/api/v1/auth/token")
print("\n===== auth/token ctx =====")
print(repr(blob[max(0, k - 200) : k + 300]))
