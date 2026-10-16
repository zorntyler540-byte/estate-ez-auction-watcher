"""Download seller SPA JS and extract API / bidder-report paths."""
from __future__ import annotations

import re
from collections import Counter

import requests

s = requests.Session()
s.headers["User-Agent"] = "Mozilla/5.0"
base = "https://seller.ctbids.com"

# Also grab admin main for comparison
targets = [
    f"{base}/static/js/main.82a9026c.chunk.js",
    f"{base}/static/js/2.eb37b50b.chunk.js",
]

# Discover more chunks from main
text_blobs: list[str] = []
for url in targets:
    r = s.get(url, timeout=60)
    print(f"{url} -> {r.status_code} len={len(r.content)}")
    if r.status_code != 200:
        continue
    text_blobs.append(r.text)
    # find other chunk refs
    for m in re.findall(r'static/js/[a-zA-Z0-9._-]+\.js', r.text):
        full = f"{base}/{m}"
        if full not in targets:
            targets.append(full)

# Fetch any newly discovered (cap)
for url in targets[2:12]:
    r = s.get(url, timeout=60)
    print(f"{url} -> {r.status_code} len={len(r.content)}")
    if r.status_code == 200:
        text_blobs.append(r.text)

blob = "\n".join(text_blobs)
print(f"\nTotal JS chars: {len(blob)}")

# API-ish strings
patterns = [
    r"/services/api/v1/[a-zA-Z0-9_./-]{3,80}",
    r"/api/v1/[a-zA-Z0-9_./-]{3,80}",
    r"https://[a-zA-Z0-9.-]*ctbids\.com[a-zA-Z0-9_./-]{0,100}",
]
found: Counter[str] = Counter()
for pat in patterns:
    for m in re.findall(pat, blob):
        found[m] += 1

print("\n=== Unique API-like paths (sample)")
keys = sorted(found)
bidderish = [k for k in keys if re.search(r"bidder|invoice|payment|report|suspend|unpaid|buyer", k, re.I)]
print(f"bidder/invoice/report related: {len(bidderish)}")
for k in bidderish[:80]:
    print(" ", k, found[k])

print("\n=== All unique paths containing 'report'")
for k in keys:
    if "report" in k.lower():
        print(" ", k)

print("\n=== Env / base URL hints")
for pat in (
    r"REACT_APP_[A-Z0-9_]+",
    r"process\.env\.[A-Z0-9_]+",
    r"baseURL[\"']?\s*[:=]\s*[\"'][^\"']+",
    r"apiUrl[\"']?\s*[:=]\s*[\"'][^\"']+",
    r"SELLER_[A-Z0-9_]+",
):
    hits = sorted(set(re.findall(pat, blob)))
    if hits:
        print(pat, "->", hits[:30])

# String literals mentioning Bidder Report
print("\n=== UI string hits")
for needle in (
    "Bidder Report",
    "bidder report",
    "BidderReport",
    "Unpaid",
    "invoice",
    "Payment Status",
):
    idx = blob.lower().find(needle.lower())
    print(f"{needle!r}: {'FOUND' if idx >= 0 else 'no'}", f"@ {idx}" if idx >= 0 else "")
    if idx >= 0:
        print("  ctx:", repr(blob[max(0, idx - 80) : idx + 120]))
