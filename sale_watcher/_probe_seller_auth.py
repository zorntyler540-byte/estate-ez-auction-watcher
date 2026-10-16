"""Find seller localStorage keys and invoice list request shape."""
from __future__ import annotations

import re

import requests

blob = requests.get(
    "https://seller.ctbids.com/static/js/main.82a9026c.chunk.js",
    timeout=60,
    headers={"User-Agent": "Mozilla/5.0"},
).text

# localStorage set/get patterns
print("===== localStorage =====")
for m in re.finditer(r"localStorage\.(?:get|set)Item\([\"']([^\"']+)[\"']", blob):
    pass
keys = sorted(set(re.findall(r"localStorage\.(?:get|set)Item\([\"']([^\"']+)[\"']", blob)))
print(keys[:80])

# IndexedDB / redux-persist style
for needle in ("accessToken", "refreshToken", "persist:", "sellerToken", "D=", "I="):
    i = blob.find(needle)
    if i >= 0:
        print(f"\n{needle} @ {i}:", repr(blob[max(0, i - 60) : i + 140]))

# invoice list call site
print("\n===== invoice/list nearby =====")
i = blob.find('Ot="/api/v1/invoice/list"')
print(repr(blob[i : i + 200]))
# find uses of Ot
n = 0
start = 0
while n < 6:
    j = blob.find("Ot", start)
    if j < 0:
        break
    ctx = blob[max(0, j - 40) : j + 80]
    if "invoice" in ctx.lower() or "ve(Ot" in ctx or "Ot)" in ctx or "Ot," in ctx:
        print(repr(ctx))
        print("---")
        n += 1
    start = j + 2

# unpaid / invoicestatus filters
print("\n===== unpaid / invoicestatus =====")
for needle in ("invoicestatus", "Unpaid", "Pending", "pendingamount", "buyerid"):
    hits = 0
    start = 0
    while hits < 3:
        j = blob.find(needle, start)
        if j < 0:
            break
        if abs(j - blob.find("invoice/list")) < 50000 or "invoice" in blob[max(0, j - 100) : j + 100].lower():
            print(repr(blob[max(0, j - 100) : j + 160]))
            print("---")
            hits += 1
        start = j + len(needle)
