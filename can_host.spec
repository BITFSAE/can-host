# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

root = Path(SPECPATH).resolve()
package = root / "canhost"

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
    ],
    hookspath=[],
    runtime_hooks=[],
    # The field release is connected to real PCAN hardware.  Keep the
    # simulator available to source runs, but do not ship it in the EXE.
    # The updater uses only the standard library; keep it in frozen builds so
    # the release can check GitHub, verify and replace itself.  The field tool
    # deliberately does not ship the simulator.
    excludes=["canhost.simulator"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BITFSAE_CAN_Host",
    icon=str(root / "app_icon.ico"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=True, name="BITFSAE_CAN_Host")
