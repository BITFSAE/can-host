# BITFSAE CAN HOST

BITFSAE 车队 CAN 上位机：BMS 监视四页（运行总览、电芯与温度、故障与记录、参数与命令）+ 整车 CANB 总览（SOP、赛会能量计、PDM 低压、ECU 四轮、胎温、整车风扇）+ MQTT 遥测故障订阅。自有 IVT-S 位于 CAN1，由 BMS 主监视和独立 IVT 配置页处理。主要面向 Windows + PCAN-USB。使用者操作、页面分工、连接关系和总线安全边界统一见 [`DOC/CAN上位机与工具使用.md`](DOC/CAN上位机与工具使用.md)。本文只保留安装、运行和发布信息；进度、风险、验证和历史变更统一记录在 [`todo.md`](todo.md)。

接口权威：BMS 帧以 [BMS-MASTER-F405](https://github.com/BITFSAE/BMS-MASTER-F405) 固件与 `DOC/CAN通信协议.md` 为准；整车风扇以 [FanController](https://github.com/BITFSAE/FanController)、PDM 以 [PDM](https://github.com/BITFSAE/PDM) 固件仓库为准；整车 DBC 中央登记在 [vehicle-interfaces](https://github.com/BITFSAE/vehicle-interfaces)。

## 运行依赖

- Windows：PEAK PCAN 驱动、PCAN-Basic、Microsoft Edge WebView2 Runtime。
- Python 源码运行：Python 3.11 或更新版本、`requirements.txt` 中的依赖。
- 实体 CAN：PCAN-USB 和正确的总线位率；源码中保留 BMS 与整车两套内置模拟数据供开发测试，Windows 发布版不打包模拟器。
- 遥测订阅：可访问 MQTT Broker 的网络和只读 `fsae/telemetry` 账号；密码不保存在上位机页面设置中。

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

Release 同时附上 `BITFSAE_CAN_Host_vX.Y.Z.zip` 和同名 `.sha256` 校验文件（软件内更新也会使用同样附件）。ZIP 是完整 one-folder 目录的压缩包，目标电脑安装要求与上一节相同。

## 软件内更新

Windows 发布版左下角版本信息可点击，会打开“软件内更新”窗口：

1. 启动时自动检查一次 `BITFSAE/can-host` 的正式 Release；需要手动检查时点击“检查更新”，勾选“包含预发布版（Pre-release）”可列出 `-rc1` 等预发布。
2. 点击“下载更新”后自动下载 ZIP 和 `.sha256`，先校验 SHA256，再校验 ZIP 只包含预期的 `BITFSAE_CAN_Host/` 目录（拒绝绝对路径、`..` 和符号链接）。
3. 点击“退出并安装”后应用退出，由隐藏 PowerShell 助手等待旧进程结束、备份旧目录为 `.old-<时间戳>`、替换为新目录并重新启动；启动新版本失败时自动恢复旧目录。成功替换后才能删除备份。

公开仓库的 Release 无需任何凭据即可检查、下载。若仓库保持私有，使用者在更新窗口保存有 `repo:contents:read` 的只读 GitHub PAT；令牌只保存在本机 `%APPDATA%\BITFSAE\CAN Host\settings.json`，不返回前端、不写入日志。源码运行只能检查更新，不能替换安装目录。

发布新版本只需更新版本号、提交推送，再打 `v*` 标签：

```powershell
# 1. 更新 canhost/__init__.py 的 __version__ 和 __version_date__，连同代码一起提交推送
# 2. 打标签并推送，CI 自动构建并创建 Release；标签版本必须与 __version__ 一致
git tag v0.5.1
git push origin v0.5.1
```

此后已有发布版会在启动时检测到新版本。示例版本号请按当前 `canhost/__init__.py` 的实际值替换。

## 文件入口

| 文件 | 作用 |
|---|---|
| `canhost/app.py` | PyWebView 窗口和 JavaScript API（主/整车/台架/IVT/MQTT 五类独立连接） |
| `canhost/transport.py` | 线程化 python-can 传输层：连接、记录、回放、命令发送 |
| `canhost/decoders.py` | 全部 CAN 帧格式的唯一定义（SOP、包状态、IVT、赛会能量计、PDM、风扇、ECU、胎温） |
| `canhost/bms/` | BMS 协议状态机、工具命令编码、BMS 模拟器 |
| `canhost/vehicle/` | 整车协议状态机（含风扇命令应答）与整车模拟器 |
| `canhost/ivt.py` | IVT 请求、响应解析和 BMS CAN1 目标比较 |
| `canhost/updater.py` | GitHub Release 检查、SHA256 校验、安全解压与 Windows 退出安装 |
| `canhost/telemetry/` | MQTT 只读订阅、TelemetryFrame Protobuf 解码和故障变化记录 |
| `canhost/web/` | 无网络依赖的 HTML/CSS/JavaScript 界面（`js/` 按页面模块拆分） |
| `cli/` | 独立命令行工具 `pcan_bms_bench.py`、`pcan_ivt_tool.py` |
| `Tests/` | 单元测试（decoders / bms / fan / ivt / vehicle / telemetry / updater） |
| `todo.md` | 上位机待办、风险、验证和变更摘要 |
