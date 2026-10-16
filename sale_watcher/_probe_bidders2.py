"""Deeper probe: biddingstatus values, membershipCompleted, suspend/list, item detail."""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import requests

from ctbids_client import CtbidsClient, merge_bids
from watcher import resolve_access_token

token = resolve_access_token(validate=True)
c = CtbidsClient(token)

items = c.fetch_sale_items(46500)
merge_bids(items, c.fetch_current_bids([i.item_id for i in items]))
bidding = sorted([i for i in items if i.bid_price > 0], key=lambda x: -x.bid_price)

status_counts = Counter()
member_counts = Counter()
examples = []
by_nick = {}

for it in bidding[:60]:
    r = c.session.get(
        f"https://api.ctbids.com/services/api/v1/buyer/auction/bid/history/{it.item_id}",
        timeout=30,
    )
    hist = ((r.json().get("data") or {}).get("bidhistory") or [])
    for row in hist[:1]:  # high bidder only
        st = row.get("biddingstatus")
        mem = row.get("membershipCompleted")
        status_counts[str(st)] += 1
        member_counts[str(mem)] += 1
        nick = row.get("nickname")
        by_nick[nick] = {
            "buyerid": row.get("buyerid"),
            "biddingstatus": st,
            "membershipCompleted": mem,
            "sku": it.sku,
            "price": it.bid_price,
        }
        if len(examples) < 15:
            examples.append({"sku": it.sku, **row})

print("biddingstatus counts:", dict(status_counts))
print("membershipCompleted counts:", dict(member_counts))
print("unique high bidders sampled:", len(by_nick))
print("examples:")
for e in examples[:10]:
    print(
        e.get("nickname"),
        "status=",
        e.get("biddingstatus"),
        "member=",
        e.get("membershipCompleted"),
        "sku=",
        e.get("sku"),
    )

# suspend/list variations
print("\n--- suspend/list ---")
for method, body in [
    ("GET", None),
    ("POST", {}),
    ("POST", {"data": {}}),
    ("POST", {"data": {"saleid": 46500}}),
    ("POST", {"data": {"buyerids": [e["buyerid"] for e in examples[:5]]}}),
]:
    if method == "GET":
        r = c.session.get("https://api.ctbids.com/services/api/v1/buyer/user/suspend/list", timeout=20)
    else:
        r = c.session.post(
            "https://api.ctbids.com/services/api/v1/buyer/user/suspend/list",
            json=body,
            timeout=20,
        )
    print(method, body, r.status_code, r.text[:250])

# item detail fields
print("\n--- item detail ---")
r = c.session.get("https://sale.ctbids.com/services/api/v1/search/item/detail/5409115", timeout=30)
# may need different host
if r.status_code != 200 or r.json().get("status") != "success":
    r = c.session.post(
        "https://sale.ctbids.com/services/api/v1/search/item/detail/5409115",
        json={},
        timeout=30,
    )
print("sale detail", r.status_code, r.text[:400])
# try api host patterns from browser
for url in [
    "https://api.ctbids.com/services/api/v1/search/item/detail/5409115",
    "https://sale.ctbids.com/services/api/v1/search/item/detail/5409115",
]:
    r = c.session.get(url, timeout=20)
    print("GET", url.split("/v1/")[-1], r.status_code, r.text[:200])

# JS: biddingstatus + suspensionstatus context
js = requests.get("https://ctbids.com/static/js/main.19682176.js", timeout=60).text
for kw in ("biddingstatus", "membershipCompleted", "suspensionstatus", "suspend/list"):
    print(f"\n==== {kw}")
    for m in re.finditer(rf".{{0,100}}{re.escape(kw)}.{{0,160}}", js):
        print(m.group(0).replace("\n", " ")[:280])
        break

# Any non-empty suspend statuses among ALL history rows for top 30
weird = []
for it in bidding[:30]:
    r = c.session.get(
        f"https://api.ctbids.com/services/api/v1/buyer/auction/bid/history/{it.item_id}",
        timeout=30,
    )
    for row in (r.json().get("data") or {}).get("bidhistory") or []:
        if row.get("biddingstatus") not in (None, "", "Active", "active", "APPROVED", "Approved"):
            weird.append(row)
        if row.get("membershipCompleted") is False:
            weird.append({"nick": row.get("nickname"), "member": False, "status": row.get("biddingstatus")})

print("\nnon-typical rows", len(weird))
for w in weird[:20]:
    print(w)

Path("_bidder_probe_out2.json").write_text(
    json.dumps({"status_counts": status_counts, "member_counts": member_counts, "by_nick": by_nick}, indent=2, default=str),
    encoding="utf-8",
)
