"""Find a stable buyer+Void invoice list request."""
from __future__ import annotations

import json

from auth import load_saved_seller_access_token, resolve_seller_access_token
from seller_client import SELLER_API, SellerClient

token = resolve_seller_access_token(validate=False, allow_browser=False) or load_saved_seller_access_token()
c = SellerClient(token)
bid = 370101
fields_full = [
    "locationtitle",
    "saletitle",
    "createddate",
    "invoiceamount",
    "type",
    "invoicestatus",
    "id",
    "buyerid",
]


def call(body):
    r = c.session.post(f"{SELLER_API}/invoice/list", data=json.dumps(body), timeout=30)
    p = r.json() if r.content else {}
    rows = p.get("data") or []
    print(
        r.status_code,
        "n",
        len(rows),
        "total",
        (p.get("page") or {}).get("total"),
        "msg",
        p.get("message"),
        "buyers",
        [x.get("buyerid") for x in rows[:5]],
        "stat",
        [x.get("invoicestatus") for x in rows[:5]],
    )


print("buyer only")
call(
    {
        "field": fields_full,
        "filter": [{"field": "buyerid", "value": int(bid), "op": "=", "join": "AND"}],
        "page": {"size": 5},
    }
)
print("buyer+void")
call(
    {
        "field": fields_full,
        "filter": [
            {"field": "buyerid", "value": int(bid), "op": "=", "join": "AND"},
            {"field": "invoicestatus", "value": "Void", "op": "=", "join": "AND"},
        ],
        "page": {"size": 1},
    }
)
print("buyer+void lowercase")
call(
    {
        "field": fields_full,
        "filter": [
            {"field": "buyerid", "value": int(bid), "op": "=", "join": "AND"},
            {"field": "invoicestatus", "value": "void", "op": "=", "join": "AND"},
        ],
        "page": {"size": 1},
    }
)
print("other buyer+void")
call(
    {
        "field": fields_full,
        "filter": [
            {"field": "buyerid", "value": 4835, "op": "=", "join": "AND"},
            {"field": "invoicestatus", "value": "Void", "op": "=", "join": "AND"},
        ],
        "page": {"size": 1},
    }
)
print("buyer 1 only")
call(
    {
        "field": fields_full,
        "filter": [{"field": "buyerid", "value": 1, "op": "=", "join": "AND"}],
        "page": {"size": 1},
    }
)
