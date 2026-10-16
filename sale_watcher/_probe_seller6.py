"""Find how seller stores access/refresh tokens (localforage / idb)."""
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

# Around D="access",I="refresh"
i = blob.find(',D="access",I="refresh"')
print("token consts ctx:", repr(blob[i - 200 : i + 400]))

# Find function se( and L(
print("\n===== function se =====")
for m in re.finditer(r"function se\(", blob):
    print(repr(blob[m.start() : m.start() + 350]))
    print("---")

print("\n===== function L( =====")
# L is used as L(I) for refresh - might be getter
for pat in (r"function L\(t\)", r"L=function\(t\)", r",L=function"):
    for m in re.finditer(pat, blob):
        print(pat, repr(blob[m.start() : m.start() + 300]))
        print("---")

# localforage / createInstance
print("\n===== localforage =====")
for needle in ("localforage", "localForage", "createInstance", "keyPrefix", "storeName", "ctbids", "seller"):
    hits = list(re.finditer(re.escape(needle), lib + blob))
    print(needle, len(hits))
    if hits and needle in ("localforage", "createInstance", "keyPrefix", "storeName"):
        h = hits[0]
        src = lib if h.start() < len(lib) else blob
        off = h.start() if h.start() < len(lib) else h.start() - len(lib)
        # messy - just search both
for src_name, src in (("main", blob), ("lib", lib)):
    for m in re.finditer(r'name:\s*[\"\'][^\"\']{2,40}[\"\']', src):
        val = m.group(0)
        if any(x in val.lower() for x in ("token", "seller", "ctbid", "auth", "persist", "redux")):
            print(src_name, val)

# Look for IndexedDB object store creation nearby access
print("\n===== nearby setItem patterns =====")
for needle in ('setItem(D', 'setItem("access"', "setItem(D,", "getItem(D", 'getItem("access"'):
    j = blob.find(needle)
    print(needle, j)
    if j >= 0:
        print(repr(blob[max(0, j - 100) : j + 200]))
