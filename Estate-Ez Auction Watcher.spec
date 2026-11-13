# -*- mode: python ; coding: utf-8 -*-
import sys

from PyInstaller.utils.hooks import collect_all

try:
    from brand import APP_VERSION
except Exception:
    APP_VERSION = "1.2.0"

datas = []
binaries = []
hiddenimports = ['brand', 'pandas', 'openpyxl', 'requests', 'sale_watcher.app', 'sale_watcher.watcher', 'sale_watcher.auth', 'sale_watcher.estate_ez', 'sale_watcher.ctbids_client', 'sale_watcher.seller_client', 'sale_watcher.export_report', 'sale_watcher.engagement_sync', 'sale_watcher.http_retry', 'sale_watcher.secure_store', 'sale_watcher.app_logging', 'sale_watcher.friendly_errors', 'sale_watcher.push_status', 'sale_watcher.paths', 'engagement_sync', 'http_retry', 'secure_store', 'app_logging', 'friendly_errors', 'push_status', 'paths', 'report_engine', 'build_active_sale_report', 'build_end_of_sale_report', 'build_sku_rollup_report', 'extract_sku_prefixes', 'runtime_paths']
tmp_ret = collect_all('customtkinter')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('tkinterdnd2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('playwright')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['suite_app.py'],
    pathex=['.', 'sale_watcher'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# UPX on Mac breaks the ad-hoc signature Apple silicon requires.
use_upx = sys.platform != "darwin"

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Estate-Ez Auction Watcher',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=use_upx,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=use_upx,
    upx_exclude=[],
    name='Estate-Ez Auction Watcher',
)
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name='Estate-Ez Auction Watcher.app',
        icon=None,
        bundle_identifier='com.zortron.estateez.auctionwatcher',
        info_plist={
            'CFBundleName': 'Estate-Ez Auction Watcher',
            'CFBundleDisplayName': 'Estate-Ez Auction Watcher',
            'CFBundleShortVersionString': APP_VERSION,
            'CFBundleVersion': APP_VERSION,
            'NSHighResolutionCapable': True,
            'LSMinimumSystemVersion': '12.0',
        },
    )
