"""Probe CTBids for unpaid / outstanding invoice data on other bidders."""
from __future__ import annotations

import json
import re
from collections import Counter

import requests

from ctbids_client import CtbidsClient, merge_bids
from watcher import resolve_access_token

token = resolve_access_token(validate=True)
c = CtbidsClient(token)
print("signed_in", c.signed_in)

# Pull a few buyer ids from current sale
items = c.fetch_sale_items(46500)
merge_bids(items, c.fetch_current_bids([i.item_id for i in items]))
top = sorted([i for i in items if i.has_bids], key=lambda x: -x.bid_price)[:8]
buyer_ids = []
for it in top:
    info = c.fetch_high_bidder_info(it.item_id)
    if info and info.buyer_id:
        buyer_ids.append((it.sku, info.nickname, info.buyer_id))
print("buyers", buyer_ids[:6])

# Search JS for invoice-related paths
js = requests.get("https://ctbids.com/static/js/main.19682176.js", timeout=60).text
paths = sorted(set(re.findall(r"/api/v1/[^\"']*invoice[^\"']*", js, re.I)))
paths += sorted(set(re.findall(r"/api/v1/[^\"']*(?:unpaid|outstanding|delinquen|balance|owing)[^\"']*", js, re.I)))
print("\ninvoice-ish paths:")
for p in sorted(set(paths)):
    print(" ", p)

# Keyword context
for kw in ("unpaid", "outstanding", "invoice", "paymentstatus", "paidstatus", "balance"):
    hits = []
    for m in re.finditer(rf".{{0,70}}{kw}.{{0,90}}", js, re.I):
        hits.append(m.group(0).replace("\n", " ")[:200])
        if len(hits) >= 3:
            break
    print(f"\nKW {kw} ({len(hits)})")
    for h in hits:
        print(" ", h)

sample_buyer = buyer_ids[0][2] if buyer_ids else 913288
own_id = 985506

candidates = [
    ("GET", f"https://api.ctbids.com/services/api/v1/buyer/invoice/detail/{sample_buyer}", None),
    ("GET", f"https://api.ctbids.com/services/api/v1/buyer/user/invoice/list", None),
    ("POST", "https://api.ctbids.com/services/api/v1/buyer/user/invoice/list", {"data": {}}),
    ("POST", "https://api.ctbids.com/services/api/v1/buyer/user/invoice/list", {"data": {"buyerid": sample_buyer}}),
    ("POST", "https://api.ctbids.com/services/api/v1/buyer/invoice/list", {"data": {"buyerid": sample_buyer}}),
    ("POST", "https://api.ctbids.com/services/api/v1/buyer/invoice/bidder/acceptance", {"data": {"buyerid": sample_buyer}}),
    ("GET", f"https://api.ctbids.com/services/api/v1/buyer/invoice/bidder/acceptance/{sample_buyer}", None),
    ("POST", "https://api.ctbids.com/services/api/v1/seller/invoice/list", {"data": {"buyerid": sample_buyer}}),
    ("POST", "https://api.ctbids.com/services/api/v1/seller/buyer/invoice/list", {"data": {"buyerid": sample_buyer}}),
    ("POST", "https://api.ctbids.com/services/api/v1/buyer/payment/order/detail/invoice", {"data": {"buyerid": sample_buyer}}),
    ("GET", f"https://api.ctbids.com/services/api/v1/buyer/user/detail/{sample_buyer}", None),
    ("GET", f"https://api.ctbids.com/services/api/v1/buyer/user/detail/{own_id}", None),
]

print("\n--- endpoint probes ---")
for method, url, body in candidates:
    try:
        if method == "GET":
            r = c.session.get(url, timeout=25)
        else:
            r = c.session.post(url, json=body or {}, timeout=25)
        text = r.text[:220].replace("\n", " ")
        interesting = r.status_code == 200 or (
            "Unauthorized" not in text and "No api defined" not in text and r.status_code not in (403, 404)
        )
        if interesting or "invoice" in url:
            print(method, r.status_code, url.split("/v1/")[-1][:70], text)
    except Exception as exc:
        print("ERR", url, exc)

# Own invoice list shape
r = c.session.post(
    "https://api.ctbids.com/services/api/v1/buyer/user/invoice/list",
    json={"data": {}, "page": {"size": 5}},
    timeout=30,
)
print("\nown invoice list", r.status_code, r.text[:800])
