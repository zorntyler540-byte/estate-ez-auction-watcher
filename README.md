# Estate-Ez Auction Watcher

Desktop app for Caring Transitions / CTBids franchises: **Live Watch**, **At risk**, and **Reports**.

The franchise download is a Windows folder (`Estate-Ez Auction Watcher.exe`). A Mac download is built on GitHub's Apple silicon machines (`.github/workflows/mac-app.yml`) and uploaded as `Estate-Ez-Auction-Watcher-mac.zip`. Intel Macs are not part of that build. See `HOW TO INSTALL MAC.txt`.

This repo is the Watcher / Sale Suite only. It is **not** the Estate-Ez cataloging website (`CaringTransitions_AI`). It **does** work with Estate-Ez: staff sign in with the same Estate-Ez account, Watcher can load the CT Seller session from that account, and after each refresh it sends a PII-free engagement snapshot (SKU, views, watchers, price — no bidder names or invoices) to Estate-Ez, including a `complete` flag when CT Seller data is fully fetched. Set `ESTATE_EZ_SEND_COMPLETE=0` or `"engagement_send_complete": false` in `%LOCALAPPDATA%\\SaleWatcher\\watcher_config.json` to omit that field (for older Estate-Ez servers).

## Run from source

```text
pip install -r requirements.txt
playwright install chromium
copy config.franchise.json config.json
copy .env.example .env
python suite_app.py
```

Local `config.json` and `.env` stay off git (franchise name and optional API overrides). CTBids / Estate-Ez passwords are stored on this PC only, not in the repo. Send-out zips use `config.franchise.json`.

Saved session tokens and passwords are encrypted for the signed-in user: Windows DPAPI, or the Mac login Keychain. Older plaintext token files are encrypted the first time they are read. On Linux (tests, dev) tokens fall back to plaintext with a warning in the log, and saving a password is refused.

The per-user folder is `%LOCALAPPDATA%\SaleWatcher` on Windows and `~/Library/Application Support/SaleWatcher` on Mac. Logs are `logs/watcher.log` inside that folder (5 files × 2 MB, rotated). Tokens, passwords and one-time codes are redacted before anything is written. Open the folder from **Logs** (bottom right) or **About**.

Tests: `python -m pytest -q` from the repo root (the Live Watch UI tests skip when no display is available).

Bump `APP_VERSION` in `brand.py` for every franchise build; it shows in the window title, About, and every log line.

## Build a franchise zip

```text
python -m PyInstaller --noconfirm --clean "Estate-Ez Auction Watcher.spec"
```

Then copy Chromium from `%LOCALAPPDATA%\ms-playwright` into `dist\Estate-Ez Auction Watcher\` and zip that folder. Do not commit the zip.

## Build the Mac app

On a Mac, or in GitHub Actions (`Mac app` workflow):

```text
chmod +x build_mac.sh
./build_mac.sh
```

A local run also writes `dist/Estate-Ez-Auction-Watcher-mac.zip`. The GitHub Actions artifact is the folder itself (app, config, and `Open Auction Watcher.command`) so the client unzips once. The GitHub run is Apple silicon. Do not commit the zip.

## License

Copyright (c) 2026 Tyler Zorn. All Rights Reserved.

This software is **proprietary and confidential**. It is **not** open source. No permission is granted to copy, modify, merge, publish, distribute, sublicense, sell, or use it commercially without Tyler Zorn's prior written permission. Viewing this public repository grants no license.

Inquiries: GitHub [@zorntyler540-byte](https://github.com/zorntyler540-byte).

Provided **AS IS**, without warranty. See [LICENSE](LICENSE) for the full terms, including limitation of liability.

Estate-Ez Auction Watcher · Zortron Tech Solutions
