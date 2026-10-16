"""Check suspend/list pagination and whether nicknames appear anywhere."""
from __future__ import annotations

from collections import Counter

from ctbids_client import CtbidsClient
from watcher import resolve_access_token

c = CtbidsClient(resolve_access_token(validate=True))

bodies = [
    {"data": {}},
    {"data": {"page": {"size": 100, "next": None}}},
    {"data": {"page": {"size": 2000}}},
    {"data": {"ispublic": 1}},
    {"data": {"ispublic": 0}},
    {"page": {"size": 500}, "data": {}},
]
for body in bodies:
    r = c.session.post(
        "https://api.ctbids.com/services/api/v1/buyer/user/suspend/list",
        json=body,
        timeout=60,
    )
    data = r.json().get("data") or []
    page = r.json().get("page")
    ids = {x.get("buyerid") for x in data}
    print("body", body, "n", len(data), "unique buyers", len(ids), "page", page)

# membershipCompleted meaning: compare with own user detail fields
r = c.session.get("https://api.ctbids.com/services/api/v1/buyer/user/detail/985506", timeout=30)
print("own detail", r.status_code, r.text[:500])
