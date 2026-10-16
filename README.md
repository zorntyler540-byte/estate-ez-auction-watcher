# Estate-Ez Auction Watcher

Windows desktop app for Caring Transitions / CTBids franchises: **Live Watch**, **At risk**, and **Reports**.

This repo is the Watcher / Sale Suite only. It is **not** the Estate-Ez cataloging website (`CaringTransitions_AI`).

## Run from source

```text
pip install -r requirements.txt
playwright install chromium
copy config.franchise.json config.json
python suite_app.py
```

Local `config.json` stays off git (your franchise name). Send-out zips use `config.franchise.json`.

## Build a franchise zip

```text
python -m PyInstaller --noconfirm --clean "Estate-Ez Auction Watcher.spec"
```

Then copy Chromium from `%LOCALAPPDATA%\ms-playwright` into `dist\Estate-Ez Auction Watcher\` and zip that folder. Do not commit the zip.

Estate-Ez Auction Watcher · Zortron Tech Solutions
