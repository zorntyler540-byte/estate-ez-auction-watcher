"""Pull full suspend list and cross-check against this sale's high bidders."""
from __future__ import annotations

import json
from pathlib import Path

from ctbids_client import CtbidsClient, merge_bids
from watcher import resolve_access_token

token = resolve_access_token(validate=True)
c = CtbidsClient(token)

r = c.session.post(
    "https://api.ctbids.com/services/api/v1/buyer/user/suspend/list",
    json={"data": {}},
    timeout=60,
)
payload = r.json()
rows = payload.get("data") or []
print("suspend list status", r.status_code, "count", len(rows))
if rows:
    print("keys", list(rows[0].keys()))
    # active = no revoke date?
    active = [x for x in rows if not x.get("revokedate")]
    revoked = [x for x in rows if x.get("revokedate")]
    print("active-ish", len(active), "revoked", len(revoked))
    print("sample active", json.dumps(active[:3], indent=2))
    print("suspensiontype values", sorted({str(x.get("suspensiontype")) for x in rows})[:20])
    print("ispublic values", sorted({str(x.get("ispublic")) for x in rows}))
    print("reason samples", [x.get("reason") for x in rows if x.get("reason")][:10])

suspended_ids = {int(x["buyerid"]) for x in rows if x.get("buyerid") is not None and not x.get("revokedate")}
print("unique active suspended buyerids", len(suspended_ids))

# Cross-check sale bidders
items = c.fetch_sale_items(46500)
merge_bids(items, c.fetch_current_bids([i.item_id for i in items]))
bidding = [i for i in items if i.bid_price > 0]

sale_buyers = {}
for it in bidding:
    hist = c.session.get(
        f"https://api.ctbids.com/services/api/v1/buyer/auction/bid/history/{it.item_id}",
        timeout=30,
    ).json()
    for row in (hist.get("data") or {}).get("bidhistory") or []:
        bid = row.get("buyerid")
        if bid is None:
            continue
        bid = int(bid)
        info = sale_buyers.setdefault(
            bid,
            {
                "nickname": row.get("nickname"),
                "membershipCompleted": row.get("membershipCompleted"),
                "items": set(),
                "high_on": set(),
            },
        )
        info["nickname"] = row.get("nickname") or info["nickname"]
        info["membershipCompleted"] = row.get("membershipCompleted")
        info["items"].add(it.sku or str(it.item_id))
        if row.get("biddingstatus") == "Current Bid":
            info["high_on"].add(it.sku or str(it.item_id))

hits = []
for bid, info in sale_buyers.items():
    if bid in suspended_ids:
        hits.append(
            {
                "buyerid": bid,
                "nickname": info["nickname"],
                "membershipCompleted": info["membershipCompleted"],
                "high_on": sorted(info["high_on"]),
                "items": sorted(info["items"])[:8],
            }
        )

print("sale unique bidders seen in history", len(sale_buyers))
print("suspended AND bidding this sale", len(hits))
for h in hits:
    print(h)

members = sum(1 for v in sale_buyers.values() if v.get("membershipCompleted") in (1, True, "1"))
print("membershipCompleted=1 among sale bidders", members, "/", len(sale_buyers))

Path("_bidder_probe_out3.json").write_text(
    json.dumps(
        {
            "suspend_count": len(rows),
            "active_suspended_ids": len(suspended_ids),
            "sale_bidders": len(sale_buyers),
            "suspended_hits": hits,
            "suspend_sample": rows[:5],
        },
        indent=2,
        default=list,
    ),
    encoding="utf-8",
)
print("done")
