# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import re


root = Path(SPECPATH).resolve()
package = root / "canhost"
version_text = (package / "__init__.py").read_text(encoding="utf-8")
version = re.search(r'^__version__ = "([^"]+)"', version_text, re.MULTILINE).group(1)

a = Analysis(
    [str(package / "__main__.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(package / "web"), "canhost/web")],
    hiddenimports=[
        "can.interfaces.pcan",
        "cli.pcan_bms_bench",
        "paho.mqtt.client",
        "canhost.telemetry.fsae_telemetry_pb2",
        # HTTPS 更新检查在 macOS 上依赖随包的 CA 证书，必须真正打进去。
        "certifi",
        # This first macOS field release temporarily keeps both lazily imported
        # simulators as a secondary development aid. Real PCAN remains the
        # bundle's primary transport and is covered by the packaging smoke test.
        "canhost.bms.simulator",
        "canhost.vehicle.simulator",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BITFSAE_CAN_Host",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    target_arch="arm64",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="BITFSAE_CAN_Host",
)
app = BUNDLE(
    coll,
    name="BITFSAE CAN Host.app",
    icon=str(root / "app_icon.ico"),
    bundle_identifier="com.bitfsae.canhost",
    version=version,
    info_plist={
        "CFBundleName": "BITFSAE CAN Host",
        "CFBundleDisplayName": "BITFSAE CAN Host",
        "CFBundleVersion": version,
        # protobuf's bundled arm64 extension currently targets macOS 12.0.
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
    },
)
