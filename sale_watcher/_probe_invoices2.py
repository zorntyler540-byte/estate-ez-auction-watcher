"""Deeper probe: can invoice/list filter by other buyerids? What statuses exist?"""
from __future__ import annotations

import json

from ctbids_client import CtbidsClient
from watcher import resolve_access_token

c = CtbidsClient(resolve_access_token(validate=True))

# Own list with page + fields
bodies = [
    {"data": {}, "page": {"size": 20}},
    {"page": {"size": 20}},
    {"data": {"buyerid": 985506}, "page": {"size": 20}},
    {"data": {"buyerid": 913288}, "page": {"size": 20}},
    {"filter": [{"field": "buyerid", "value": 913288, "op": "=", "join": "AND"}], "page": {"size": 20}},
    {"filter": [{"field": "invoicestatus", "value": "Unpaid", "op": "=", "join": "AND"}], "page": {"size": 20}},
    {"filter": [{"field": "invoicestatus", "value": "Pending", "op": "=", "join": "AND"}], "page": {"size": 20}},
    {"filter": [{"field": "invoicestatus", "value": "Due", "op": "=", "join": "AND"}], "page": {"size": 20}},
    {"data": {"invoicestatus": "Unpaid"}, "page": {"size": 20}},
    {"data": {"saleid": 46500}, "page": {"size": 20}},
    {"filter": [{"field": "saleid", "value": 46500, "op": "=", "join": "AND"}], "page": {"size": 50}},
]

for body in bodies:
    r = c.session.post(
        "https://api.ctbids.com/services/api/v1/buyer/invoice/list",
        json=body,
        timeout=30,
    )
    data = r.json().get("data") or []
    page = r.json().get("page")
    statuses = sorted({str(x.get("invoicestatus")) for x in data})
    buyers = sorted({str(x.get("buyerid") or x.get("userid") or "?") for x in data[:8]})
    print(json.dumps(body)[:90], "->", r.status_code, "n", len(data), "total", (page or {}).get("total"), "status", statuses, "buyers", buyers)

# Full keys on one invoice
r = c.session.post(
    "https://api.ctbids.com/services/api/v1/buyer/invoice/list",
    json={"data": {}, "page": {"size": 3}},
    timeout=30,
)
rows = r.json().get("data") or []
if rows:
    print("\nkeys", list(rows[0].keys()))
    print("sample", {k: rows[0].get(k) for k in rows[0] if k != "details"})
    # details truncated
    d = rows[0].get("details")
    print("details type", type(d), str(d)[:300])

# Try admin / location invoice endpoints from JS
import re, requests
js = requests.get("https://ctbids.com/static/js/main.19682176.js", timeout=60).text
for pat in [
    r"/api/v1/[^\"']*invoice[^\"']*",
    r"/api/v1/[^\"']*payment[^\"']*list[^\"']*",
    r"/api/v1/seller[^\"']*",
    r"/api/v1/admin[^\"']*",
    r"/api/v1/location[^\"']*invoice[^\"']*",
]:
    found = sorted(set(re.findall(pat, js, re.I)))
    if found:
        print("\nPAT", pat, "count", len(found))
        for f in found[:25]:
            print(" ", f)
