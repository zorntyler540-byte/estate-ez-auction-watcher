# Estate-Ez Auction Watcher

Windows desktop app for Caring Transitions / CTBids franchises: **Live Watch**, **At risk**, and **Reports**.

This repo is the Watcher / Sale Suite only. It is **not** the Estate-Ez cataloging website (`CaringTransitions_AI`). It **does** work with Estate-Ez: staff sign in with the same Estate-Ez account, Watcher can load the CT Seller session from that account, and after each refresh it can send a PII-free engagement snapshot (SKU, views, watchers, price — no bidder names or invoices) to Estate-Ez.

## Run from source

```text
pip install -r requirements.txt
playwright install chromium
copy config.franchise.json config.json
copy .env.example .env
python suite_app.py
```

Sign-in buttons open the real Estate-Ez / CTBids / CT Seller pages so one-time codes are entered there, not in the app.

Local `config.json` and `.env` stay off git (franchise name and optional API overrides). CTBids / Estate-Ez passwords stay on those websites and this PC, not in the repo. Send-out zips use `config.franchise.json`.

## Build a franchise zip

```text
python -m PyInstaller --noconfirm --clean "Estate-Ez Auction Watcher.spec"
```

Then copy Chromium from `%LOCALAPPDATA%\ms-playwright` into `dist\Estate-Ez Auction Watcher\` and zip that folder. Do not commit the zip.

## License

Copyright (c) 2026 Tyler Zorn. All Rights Reserved.

This software is **proprietary and confidential**. It is **not** open source. No permission is granted to copy, modify, merge, publish, distribute, sublicense, sell, or use it commercially without Tyler Zorn's prior written permission. Viewing this public repository grants no license.

Inquiries: GitHub [@zorntyler540-byte](https://github.com/zorntyler540-byte).

Provided **AS IS**, without warranty. See [LICENSE](LICENSE) for the full terms, including limitation of liability.

Estate-Ez Auction Watcher · Zortron Tech Solutions
