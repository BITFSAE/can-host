# BITFSAE BMS Control Desk

这是 F405 BMS 主控的 CAN 上位机，主要面向 Windows + PCAN-USB。使用者操作、三类界面分工、CAN1/CANB 接线、台架模拟从控和 IVT 配置流程统一见 [`DOC/CAN上位机与工具使用.md`](../../DOC/CAN上位机与工具使用.md)。本文只保留安装、运行和发布信息；本目录的进度、风险、验证和历史变更统一记录在 `todo.md`，不再单独维护 `CHANGELOG.md`。

## 运行依赖

- Windows：PEAK PCAN 驱动、PCAN-Basic、Microsoft Edge WebView2 Runtime。
- Python 源码运行：Python 3.11 或更新版本、`requirements.txt` 中的依赖。
- 实体 CAN：PCAN-USB 和正确的总线位率；源码中保留内置模拟服务供开发测试，Windows 发布版不打包模拟器。

## Windows 源码运行

```powershell
py -3.11 -m venv .venv-bms-host
.\.venv-bms-host\Scripts\Activate.ps1
python -m pip install -r TOOLS\bms_host\requirements.txt
python -m TOOLS.bms_host
```

开发界面时可加 `--debug`。源码运行的内置模拟数据只用于界面和协议开发，不进入 Windows 发布版。

## macOS 源码运行

```bash
python3 -m venv .venv-bms-host
.venv-bms-host/bin/python -m pip install -r TOOLS/bms_host/requirements.txt
.venv-bms-host/bin/python -m TOOLS.bms_host
```

macOS 主要用于界面开发和内置模拟数据调试。实体 PCAN 联调按 Windows + PEAK 驱动环境执行。

## 构建 Windows 程序

```powershell
powershell -ExecutionPolicy Bypass -File TOOLS\bms_host\build_windows.ps1
```

产物位于 `dist\BITFSAE_BMS_Control_Desk\`，采用 one-folder 形式。发布版只支持真实 PCAN；复制整个文件夹到目标电脑，不能只复制 exe。

目标电脑仍需安装：

1. PEAK PCAN-USB 驱动及 PCAN-Basic 运行组件；
2. Microsoft Edge WebView2 Runtime。

## 文件入口

| 文件 | 作用 |
|---|---|
| `app.py` | PyWebView 窗口和 JavaScript API |
| `can_service.py` | 主监视、独立台架连接、独立 IVT 连接、独立风扇连接和 CAN 记录服务 |
| `protocol.py` | F405 报文解析、F405 工具命令编码和 FanController 命令编码 |
| `ivt.py` | IVT 请求、响应解析和 BMS CANB 目标比较 |
| `simulator.py` | 源码开发用内置模拟数据 |
| `web/` | 无网络依赖的 HTML/CSS/JavaScript 界面 |
| `todo.md` | 上位机待办、风险、验证和变更摘要 |

主控协议字段以 [`DOC/CAN通信协议.md`](../../DOC/CAN通信协议.md) 为准；上位机实现细节不在本文展开。
