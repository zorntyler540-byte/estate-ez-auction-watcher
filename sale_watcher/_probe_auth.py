"""Test which authenticated endpoints accept the current access token."""
from __future__ import annotations

import requests
from playwright.sync_api import sync_playwright

from watcher import profile_dir

with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir()),
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = context.pages[0] if context.pages else context.new_page()
    page.goto("https://ctbids.com/estate-sale/46500", wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(3000)
    access = page.evaluate("() => localStorage.getItem('access')")
    refresh = page.evaluate("() => localStorage.getItem('refresh')")
    # Capture network for bid history from the page itself
    results = page.evaluate(
        """async (itemId) => {
          const access = localStorage.getItem('access');
          const urls = [
            'https://api.ctbids.com/services/api/v1/buyer/user/detail',
            'https://api.ctbids.com/services/api/v1/buyer/auction/bid/history/' + itemId,
            'https://api.ctbids.com/services/api/v1/buyer/token/refresh',
          ];
          const out = [];
          for (const url of urls) {
            try {
              const opts = {
                method: url.includes('refresh') ? 'POST' : 'GET',
                headers: {
                  'Authorization': 'Bearer ' + access,
                  'Accept': 'application/json',
                  'Content-Type': 'application/json',
                },
              };
              if (url.includes('refresh')) {
                opts.body = JSON.stringify({ data: { refreshToken: localStorage.getItem('refresh') } });
              }
              const r = await fetch(url, opts);
              const t = await r.text();
              out.push({ url: url.split('/v1/')[1], status: r.status, body: t.slice(0, 220) });
            } catch (e) {
              out.push({ url, err: String(e) });
            }
          }
          return out;
        }""",
        5375129,
    )
    print("from browser fetch:")
    for row in results:
        print(row)

    # Also try navigating to an item page and see what the app uses
    page.goto("https://ctbids.com/estate-sale/46500/item/5409115", wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(5000)
    # Look for highest bidder text in DOM
    text = page.evaluate(
        """() => {
          const body = document.body.innerText;
          const m = body.match(/Highest Bidder[\\s\\S]{0,80}/i);
          return { snippet: m ? m[0] : null, accessLen: (localStorage.getItem('access')||'').length };
        }"""
    )
    print("dom", text)
    context.close()

# Compare requests from python with same token
headers = {
    "Authorization": f"Bearer {access}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}
for url in [
    "https://api.ctbids.com/services/api/v1/buyer/user/detail",
    "https://api.ctbids.com/services/api/v1/buyer/auction/bid/history/5409115",
]:
    r = requests.get(url, headers=headers, timeout=30)
    print("python", url.split("/v1/")[-1], r.status_code, r.text[:200])

# try refresh with various bodies using refresh JWT (not expired for ~23h)
url = "https://api.ctbids.com/services/api/v1/buyer/token/refresh"
for body in [
    {"data": {"refreshToken": refresh}},
    {"data": {"refresh": refresh}},
    {"data": {"token": refresh}},
    {"refreshToken": refresh},
]:
    r = requests.post(url, json=body, headers={"Accept": "application/json", "Content-Type": "application/json", "Authorization": f"Bearer {refresh}"}, timeout=30)
    print("refresh", body.keys(), list((body.get('data') or body).keys()), r.status_code, r.text[:200])
