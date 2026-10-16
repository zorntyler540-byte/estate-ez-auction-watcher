"""Find how CT Seller downloads CSVs (bidder report / exports)."""
from __future__ import annotations

import re

import requests

blob = requests.get(
    "https://seller.ctbids.com/static/js/main.82a9026c.chunk.js",
    timeout=60,
    headers={"User-Agent": "Mozilla/5.0"},
).text

needles = (
    "csv",
    "CSV",
    "download",
    "export",
    "bidder-report",
    "filename",
    "text/csv",
    "Blob",
    "saveAs",
    "file-saver",
)
for needle in needles:
    print(f"\n===== {needle} count={blob.lower().count(needle.lower())} =====")

# CSV-related API paths
paths = sorted(set(re.findall(r"/api/v1/[a-zA-Z0-9_./-]{2,80}", blob)))
for p in paths:
    if any(x in p.lower() for x in ("csv", "export", "download", "report")):
        print("path", p)

# Around filename:"bidder-report"
i = blob.find('filename:"bidder-report"')
print("\n===== bidder-report filename ctx =====")
print(blob[max(0, i - 200) : i + 800])

# Find downloadCsv / exportCsv / generateCSV functions
print("\n===== csv function names =====")
for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_]{0,40}[Cc]sv[A-Za-z0-9_]{0,40}", blob):
    name = m.group(0)
    if len(name) < 60:
        pass
names = sorted(set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{0,40}[Cc]sv[A-Za-z0-9_]{0,40}", blob)))
print(names[:60])

# Look for how reports build CSV
for needle in (
    "downloadCSV",
    "downloadCsv",
    "exportCSV",
    "exportToCsv",
    "generateCSV",
    "csvColumn",
    "text/csv",
    "application/csv",
    ".csv",
):
    j = 0
    n = 0
    while n < 2:
        k = blob.find(needle, j)
        if k < 0:
            break
        print(f"\n--- {needle} @ {k} ---")
        print(repr(blob[max(0, k - 120) : k + 220]))
        j = k + len(needle)
        n += 1
