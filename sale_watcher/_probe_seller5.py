"""Find seller token storage + unpaid invoice APIs."""
from __future__ import annotations

import re

import requests

blob = requests.get(
    "https://seller.ctbids.com/static/js/main.82a9026c.chunk.js",
    timeout=60,
    headers={"User-Agent": "Mozilla/5.0"},
).text
lib = requests.get(
    "https://seller.ctbids.com/static/js/2.eb37b50b.chunk.js",
    timeout=60,
    headers={"User-Agent": "Mozilla/5.0"},
).text
allb = blob + "\n" + lib

# Find D and I constant string values near accessToken save
print("===== se() / token key constants =====")
# Pattern: ,D="something",I="something"
for m in re.finditer(r',([DI])="([a-zA-Z0-9_-]{3,40})"', blob):
    print(m.group(0), "@", m.start())

# IndexedDB database names
print("\n===== indexedDB / idb keys =====")
for pat in (
    r'openDB\([\"\']([^\"\']+)',
    r'indexedDB\.open\([\"\']([^\"\']+)',
    r'database[:\s]*[\"\']([^\"\']+)',
    r'storeName[:\s]*[\"\']([^\"\']+)',
    r'name:\s*[\"\']([a-zA-Z0-9_-]*[Tt]oken[a-zA-Z0-9_-]*)[\"\']',
):
    hits = sorted(set(re.findall(pat, allb)))
    if hits:
        print(pat, "->", hits[:40])

# invoiceunpaidcount context
print("\n===== invoiceunpaidcount =====")
i = blob.find("invoiceunpaidcount")
print(repr(blob[max(0, i - 300) : i + 400]))

# buyer detail seller endpoint
print("\n===== buyer detail urls =====")
for p in sorted(set(re.findall(r"/api/v1/buyer/[a-zA-Z0-9_./-]{2,80}", blob))):
    if any(x in p for x in ("detail", "unpaid", "invoice", "suspend", "all")):
        print(p)

# Find invoice list page config (name invoice)
print("\n===== invoice list config =====")
i = blob.find('url:"/api/v1/invoice/list"')
if i < 0:
    i = blob.find("/api/v1/invoice/list")
print("idx", i)
# search backwards for name:
chunk = blob[max(0, i - 800) : i + 1500]
print(chunk[:2200])
