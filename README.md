# BITFSAE CAN HOST

BITFSAE 车队 CAN 上位机：BMS 监视四页（运行总览、电芯与温度、故障与记录、参数与命令）+ 整车 CANB 总览（SOP、自有 IVT-S、赛会能量计、PDM 低压、ECU 四轮、胎温、整车风扇）。主要面向 Windows + PCAN-USB。使用者操作、页面分工、连接关系和总线安全边界统一见 [`DOC/CAN上位机与工具使用.md`](DOC/CAN上位机与工具使用.md)。本文只保留安装、运行和发布信息；进度、风险、验证和历史变更统一记录在 [`todo.md`](todo.md)。

接口权威：BMS 帧以 [BMS-MASTER-F405](https://github.com/BITFSAE/BMS-MASTER-F405) 固件与 `DOC/CAN通信协议.md` 为准；整车风扇以 [FanController](https://github.com/BITFSAE/FanController)、PDM 以 [PDM](https://github.com/BITFSAE/PDM) 固件仓库为准；整车 DBC 中央登记在 [vehicle-interfaces](https://github.com/BITFSAE/vehicle-interfaces)。

## 运行依赖

- Windows：PEAK PCAN 驱动、PCAN-Basic、Microsoft Edge WebView2 Runtime。
- Python 源码运行：Python 3.11 或更新版本、`requirements.txt` 中的依赖。
- 实体 CAN：PCAN-USB 和正确的总线位率；源码中保留 BMS 与整车两套内置模拟数据供开发测试，Windows 发布版不打包模拟器。

## Windows 源码运行

```powershell
py -3.11 -m venv .venv-canhost
.\.venv-canhost\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m canhost
```

开发界面时可加 `--debug`。源码运行的内置模拟数据只用于界面和协议开发，不进入 Windows 发布版。

## macOS 源码运行

```bash
python3 -m venv .venv-canhost
.venv-canhost/bin/python -m pip install -r requirements.txt
.venv-canhost/bin/python -m canhost
```

macOS 主要用于界面开发和内置模拟数据调试（BMS 主连接与整车连接都有模拟数据）。实体 PCAN 联调按 Windows + PEAK 驱动环境执行。

## 构建 Windows 程序

```powershell
powershell -ExecutionPolicy Bypass -File build_windows.ps1
```

产物位于 `dist\BITFSAE_CAN_Host\`，采用 one-folder 形式。发布版只支持真实 PCAN；复制整个文件夹到目标电脑，不能只复制 exe。exe 图标取自根目录 `app_icon.ico`（由 `canhost/web/assets/shark-mark.svg` 生成），换图标时替换该文件后重新构建。

目标电脑仍需安装：

1. PEAK PCAN-USB 驱动及 PCAN-Basic 运行组件；
2. Microsoft Edge WebView2 Runtime。

## CI 自动构建与发布

仓库在 `.github/workflows/` 下提供两条 GitHub Actions 工作流，都在 windows-latest 上运行：

- `tests.yml`：main 分支推送和 Pull Request 时自动运行全部上位机单元测试。
- `release.yml`：推送 `v*` 标签（如 `v0.2.0`）时触发。先核对标签版本与 `canhost/__init__.py` 的 `__version__` 一致，再执行 `build_windows.ps1`（测试 + PyInstaller 打包），把 `dist\BITFSAE_CAN_Host\` 压缩为 `BITFSAE_CAN_Host_v0.2.0.zip` 并创建 GitHub Release 附上该 zip。也可在 Actions 页面手动触发一次构建，此时只在该次运行页面提供 zip 产物下载，不创建 Release。
- 标签版本号后带后缀（如 `v0.2.0-rc1`）时创建的是 GitHub 预发布（Pre-release），版本号主体仍须与 `__version__` 一致；不带后缀的 `vX.Y.Z` 创建正式 Release。

发布新版本的操作：

```powershell
# 1. 更新 canhost/__init__.py 的 __version__ 和 __version_date__，连同代码一起提交推送
# 2. 打标签并推送，CI 自动构建并创建 Release
git tag v0.2.0
git push origin v0.2.0
```

Release 附件是完整 one-folder 目录的压缩包，目标电脑安装要求与上一节相同。

## 文件入口

| 文件 | 作用 |
|---|---|
| `canhost/app.py` | PyWebView 窗口和 JavaScript API（主/整车/台架/IVT 四类连接） |
| `canhost/transport.py` | 线程化 python-can 传输层：连接、记录、回放、命令发送 |
| `canhost/decoders.py` | 全部 CAN 帧格式的唯一定义（SOP、包状态、IVT、赛会能量计、PDM、风扇、ECU、胎温） |
| `canhost/bms/` | BMS 协议状态机、工具命令编码、BMS 模拟器 |
| `canhost/vehicle/` | 整车协议状态机（含风扇命令应答）与整车模拟器 |
| `canhost/ivt.py` | IVT 请求、响应解析和 BMS CANB 目标比较 |
| `canhost/web/` | 无网络依赖的 HTML/CSS/JavaScript 界面（`js/` 按页面模块拆分） |
| `cli/` | 独立命令行工具 `pcan_bms_bench.py`、`pcan_ivt_tool.py` |
| `Tests/` | 单元测试（decoders / bms / fan / ivt / vehicle） |
| `todo.md` | 上位机待办、风险、验证和变更摘要 |
