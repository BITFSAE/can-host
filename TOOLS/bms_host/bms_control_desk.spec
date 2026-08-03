# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

root = Path(SPECPATH).resolve().parents[1]
package = root / "TOOLS" / "bms_host"

a = Analysis(
    [str(package / "__main__.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(package / "web"), "TOOLS/bms_host/web")],
    hiddenimports=["can.interfaces.pcan"],
    hookspath=[],
    runtime_hooks=[],
    # The field release is connected to real PCAN hardware.  Keep the
    # simulator available to source runs, but do not ship it in the EXE.
    excludes=["TOOLS.bms_host.simulator"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BITFSAE_BMS_Control_Desk",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=True, name="BITFSAE_BMS_Control_Desk")
