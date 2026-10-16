"""Explore CTBids bidder / buyer profile fields available to a signed-in session."""
from __future__ import annotations

import json
import re
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

from ctbids_client import CtbidsClient, merge_bids
from watcher import profile_dir, resolve_access_token

OUT = Path(__file__).with_name("_bidder_probe_out.json")
token = resolve_access_token(validate=True)
print("token", bool(token))
c = CtbidsClient(token)

# 1) Bid history shape for a few items
items = c.fetch_sale_items(46500)
merge_bids(items, c.fetch_current_bids([i.item_id for i in items]))
top = sorted([i for i in items if i.bid_price > 0], key=lambda x: -x.bid_price)[:8]

histories = {}
buyer_ids = set()
for it in top:
    r = c.session.get(
        f"https://api.ctbids.com/services/api/v1/buyer/auction/bid/history/{it.item_id}",
        timeout=30,
    )
    data = r.json()
    hist = ((data.get("data") or {}).get("bidhistory") or [])[:5]
    histories[it.item_id] = {"sku": it.sku, "status": r.status_code, "rows": hist}
    for row in hist:
        if row.get("buyerid"):
            buyer_ids.add(int(row["buyerid"]))
    print("item", it.sku, "keys", list(hist[0].keys()) if hist else None, "n", len(hist))

print("buyer_ids sample", list(buyer_ids)[:10])

# 2) Search JS for bidder/suspend/ban related endpoints
js = requests.get("https://ctbids.com/static/js/main.19682176.js", timeout=60).text
patterns = [
    r"/api/v1/buyer/[^\"']+",
    r"/api/v1/[^\"']*(?:user|buyer|bidder|suspend|ban|fraud|trust|rating|feedback|profile)[^\"']*",
]
found = sorted(set(re.findall(patterns[0], js)))
interesting = [u for u in found if re.search(r"user|bidder|suspend|ban|fraud|trust|rating|feedback|profile|detail|watch", u, re.I)]
print("\ninteresting endpoints", len(interesting))
for u in interesting:
    print(" ", u)

# broader keyword hits
for kw in ("suspend", "banned", "fraud", "trust", "badactor", "blocked", "statusid", "buyerstatus", "accountstatus"):
    hits = []
    for m in re.finditer(rf".{{0,60}}{kw}.{{0,80}}", js, re.I):
        hits.append(m.group(0).replace("\n", " "))
        if len(hits) >= 4:
            break
    print(f"\nKW {kw}: {len(hits)}")
    for h in hits:
        print(" ", h[:160])

# 3) Try likely profile endpoints for one buyer id
sample_buyer = next(iter(buyer_ids)) if buyer_ids else None
probe_urls = []
if sample_buyer:
    probe_urls = [
        f"https://api.ctbids.com/services/api/v1/buyer/user/detail",
        f"https://api.ctbids.com/services/api/v1/buyer/user/detail/{sample_buyer}",
        f"https://api.ctbids.com/services/api/v1/buyer/user/{sample_buyer}",
        f"https://api.ctbids.com/services/api/v1/buyer/user/profile/{sample_buyer}",
        f"https://api.ctbids.com/services/api/v1/buyer/profile/{sample_buyer}",
        f"https://api.ctbids.com/services/api/v1/buyer/bidder/{sample_buyer}",
        f"https://api.ctbids.com/services/api/v1/buyer/auction/bidder/{sample_buyer}",
        f"https://api.ctbids.com/services/api/v1/buyer/auction/buyer/{sample_buyer}",
        f"https://api.ctbids.com/services/api/v1/buyer/feedback/{sample_buyer}",
        f"https://api.ctbids.com/services/api/v1/buyer/rating/{sample_buyer}",
        f"https://api.ctbids.com/services/api/v1/seller/buyer/{sample_buyer}",
        f"https://api.ctbids.com/services/api/v1/seller/bidder/{sample_buyer}",
        f"https://api.ctbids.com/services/api/v1/admin/buyer/{sample_buyer}",
        f"https://sale.ctbids.com/services/api/v1/buyer/{sample_buyer}",
        f"https://sale.ctbids.com/services/api/v1/search/buyer/{sample_buyer}",
    ]

results = []
for url in probe_urls:
    for method in ("GET", "POST"):
        try:
            if method == "GET":
                r = c.session.get(url, timeout=20)
            else:
                r = c.session.post(url, json={"data": {"id": sample_buyer, "buyerid": sample_buyer}}, timeout=20)
            body = r.text[:300]
            if r.status_code not in (404,) and "No api defined" not in body:
                results.append({"method": method, "url": url, "status": r.status_code, "body": body})
                print(method, r.status_code, url.split("/v1/")[-1], body[:140])
        except Exception as exc:
            print("err", url, exc)

# 4) From browser: open item page and sniff network + local UI for bidder badges
browser_notes = {}
with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir()),
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = context.pages[0] if context.pages else context.new_page()
    captured = []

    def on_response(resp):
        u = resp.url
        if "api.ctbids.com" in u or "sale.ctbids.com" in u:
            if any(k in u.lower() for k in ("bid", "buyer", "user", "bidder", "history", "detail", "profile")):
                try:
                    captured.append({"url": u, "status": resp.status, "body": resp.text()[:500]})
                except Exception:
                    captured.append({"url": u, "status": resp.status})

    page.on("response", on_response)
    page.goto("https://ctbids.com/estate-sale/46500/item/5409115", wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(6000)
    # click bid history if present
    try:
        page.get_by_text(re.compile("bid history|history", re.I)).first.click(timeout=3000)
        page.wait_for_timeout(3000)
    except Exception:
        pass
    text = page.evaluate("() => document.body.innerText.slice(0, 2500)")
    browser_notes["item_text"] = text
    browser_notes["captured"] = captured[:40]
    print("\ncaptured network", len(captured))
    for row in captured[:25]:
        print(row["status"], row["url"].split("/v1/")[-1] if "/v1/" in row["url"] else row["url"][-80:], (row.get("body") or "")[:120])
    context.close()

OUT.write_text(
    json.dumps(
        {
            "histories": histories,
            "interesting_endpoints": interesting,
            "probe_hits": results,
            "browser": browser_notes,
            "buyer_ids": list(buyer_ids)[:30],
        },
        indent=2,
    ),
    encoding="utf-8",
)
print("wrote", OUT)
