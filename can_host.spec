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
        # 本地遥测模拟器是两个平台都发布的工程工具：串口输出要 pyserial，
        # PCAN 输出复用 BMS 模拟帧定义，所以这三项必须显式随包。
        "canhost.telemetry.simulator",
        "canhost.bms.simulator",
        "serial",
        "serial.tools.list_ports",
        "serial.tools.list_ports_windows",
        # 保留 updater TLS 依赖（certifi）在冻结包内，跨平台一致。
        "certifi",
    ],
    hookspath=[],
    runtime_hooks=[],
    # The field release drives real PCAN hardware, so the CAN debug simulation
    # channel stays out of the EXE (the runtime gate rejects it as well) and the
    # packaging self-check asserts it stays unavailable.  The local telemetry
    # is an engineering tool that both packages ship, which is why
    # canhost.bms.simulator and pyserial stay in: dropping them silently disables
    # the tool's CAN and serial outputs while the page still opens.
    # The updater uses only the standard library; keep it in frozen builds so
    # the release can check GitHub, verify and replace itself.
    excludes=["canhost.vehicle.simulator"],
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
