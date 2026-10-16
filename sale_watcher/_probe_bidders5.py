"""Try filtering suspend/list by buyerid(s) from the current sale."""
from __future__ import annotations

from ctbids_client import CtbidsClient, merge_bids
from watcher import resolve_access_token

c = CtbidsClient(resolve_access_token(validate=True))

# grab a few buyer ids from sale
items = c.fetch_sale_items(46500)
merge_bids(items, c.fetch_current_bids([i.item_id for i in items]))
top = sorted([i for i in items if i.bid_price > 0], key=lambda x: -x.bid_price)[:5]
buyer_ids = []
for it in top:
    hist = c.session.get(
        f"https://api.ctbids.com/services/api/v1/buyer/auction/bid/history/{it.item_id}",
        timeout=30,
    ).json()
    row = ((hist.get("data") or {}).get("bidhistory") or [None])[0]
    if row:
        buyer_ids.append(int(row["buyerid"]))
        print(it.sku, row["nickname"], row["buyerid"], "member", row.get("membershipCompleted"))

# known suspended id from list
buyer_ids.append(4293)

tests = [
    {"data": {"buyerid": buyer_ids[0]}},
    {"data": {"buyerids": buyer_ids}},
    {"data": {"filter": [{"field": "buyerid", "value": buyer_ids[0], "op": "=", "join": "AND"}]}},
    {
        "data": {},
        "filter": [{"field": "buyerid", "value": 4293, "op": "=", "join": "AND"}],
    },
    {
        "filter": [{"field": "buyerid", "value": 4293, "op": "=", "join": "AND"}],
        "page": {"size": 20},
    },
    {"data": {"id": 4293}},
]
for body in tests:
    r = c.session.post(
        "https://api.ctbids.com/services/api/v1/buyer/user/suspend/list",
        json=body,
        timeout=30,
    )
    data = r.json().get("data") or []
    print(body, "->", r.status_code, "n", len(data), "buyers", {x.get("buyerid") for x in data[:5]}, r.text[:120])
