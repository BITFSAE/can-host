# BITFSAE CAN HOST

BITFSAE 车队 CAN 上位机：BMS 监视四页（运行总览、电芯与温度、故障与记录、参数与命令）+ 整车 CANB 总览（SOP、赛会能量计、PDM 低压、ECU 四轮、胎温、整车风扇和电池箱风扇）+ 通用 CAN 监视、记录与受控发送 + MQTT 遥测故障订阅。自有 IVT-S 位于 CAN1，由 BMS 主监视和独立 IVT 配置页处理。Windows 和 Apple Silicon macOS 发布包都面向实体 PCAN-USB 连接、台架联调和实车使用；使用者操作、页面分工、连接关系和总线安全边界统一见 [`DOC/CAN上位机与工具使用.md`](DOC/CAN上位机与工具使用.md)。本文只保留安装、运行和发布信息；进度、风险、验证和历史变更统一记录在 [`todo.md`](todo.md)。

接口权威：BMS 帧以 [BMS-MASTER-F405](https://github.com/BITFSAE/BMS-MASTER-F405) 固件与 `DOC/CAN通信协议.md` 为准；整车风扇以 [FanController](https://github.com/BITFSAE/FanController)、PDM 以 [PDM](https://github.com/BITFSAE/PDM) 固件仓库为准；整车 DBC 中央登记在 [vehicle-interfaces](https://github.com/BITFSAE/vehicle-interfaces)。

## 运行依赖

- Windows：PEAK PCAN 驱动、PCAN-Basic、Microsoft Edge WebView2 Runtime。
- macOS：Apple Silicon（M1 或更新）、macOS 12 或更新；界面使用系统 WKWebView。实体 PCAN-USB 需另行安装 [MacCAN PCBUSB](https://www.mac-can.com/) 0.13 或更新版动态库，安装的 `libPCBUSB` 必须包含 arm64 架构。
- Python 源码运行：Python 3.11 或更新版本、`requirements.txt` 中的依赖。
- 实体 CAN：PCAN-USB 和正确的总线位率。macOS 发布包的用途与 Windows 相同，都是连接真实设备；本次 macOS 过渡版本暂时保留 BMS、整车两套调试模拟数据，不把模拟结果作为驱动、接线、时序或实车协议验证结论。Windows 发布版不打包模拟器。
- 遥测订阅：可访问 MQTT Broker 的网络和只读 `fsae/telemetry` 账号；密码不保存在上位机页面设置中。

## Windows 安装（发布版）

从 [GitHub Releases](https://github.com/BITFSAE/can-host/releases) 下载 `BITFSAE_CAN_Host_vX.Y.Z_setup.exe` 双击安装：

- 每用户安装，不需要管理员权限；默认目录 `%LOCALAPPDATA%\Programs\BITFSAE_CAN_Host`。
- 自动创建开始菜单快捷方式，安装向导可选桌面快捷方式；在 Windows「设置 - 应用」中可卸载。
- 之后的版本升级不需要重新运行安装包：软件内更新会下载新版本、自动替换并重启（见下文「软件内更新」）。
- 安装前目标电脑需要装好 PEAK PCAN-USB 驱动（含 PCAN-Basic）和 Microsoft Edge WebView2 Runtime。

同名的 `BITFSAE_CAN_Host_vX.Y.Z.zip` 保留作便携方式：解压后复制整个 `BITFSAE_CAN_Host` 文件夹到目标电脑运行其中的 exe，不能只复制 exe。软件内更新使用的就是这个 ZIP。

国内网络不便访问 GitHub 时，可从 [CNB 镜像发布](https://cnb.cool/totok22/can-host/-/releases) 匿名下载同一批 Windows 与 macOS 产物；软件内更新也优先使用该镜像。

## macOS 安装（Apple Silicon）

从 [GitHub Releases](https://github.com/BITFSAE/can-host/releases) 下载 `BITFSAE_CAN_Host_macOS_arm64_vX.Y.Z.dmg`，打开后把 `BITFSAE CAN Host` 拖到“应用程序”。DMG 是 M1/M2/M3/M4 等 Apple Silicon 的原生 arm64 包，不支持 Intel Mac。

当前团队发布包使用 ad-hoc 签名，未做 Apple Developer ID 公证。首次打开若被 Gatekeeper 拦截，在“系统设置 → 隐私与安全性”找到被阻止的应用并选择“仍要打开”。软件内自动替换安装目前只支持 Windows；Mac 升级时下载新 DMG 覆盖“应用程序”中的旧版本。

连接 PCAN-USB 前需从 [MacCAN](https://www.mac-can.com/) 安装 0.13 或更新版、包含 arm64 的 `libPCBUSB`（通常安装到 `/usr/local/lib`）；上位机继续使用 `PCAN_USBBUS1..8` 通道名。该驱动是第三方项目，不随 DMG 分发。先用 MacCAN Monitor 或其示例工具确认适配器能够收发，再在上位机中连接 CAN1/CANB；这条实体链路是 macOS 安装包的主要用途和发布验收目标。

底部“调试模拟”入口仅为本次过渡版本临时保留，方便没有接硬件时检查界面和解析流程。模拟数据不会经过 MacCAN 驱动、PCAN-USB、收发器和实际总线，不能代替实体设备验收。

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

Homebrew 的原生 arm64 Python 可避免系统 Python 虚拟环境在 PyWebView 下的键盘焦点问题。源码运行支持内置模拟数据；安装 MacCAN `libPCBUSB` 后也可使用实体 PCAN-USB。

## 构建 Windows 程序

```powershell
powershell -ExecutionPolicy Bypass -File build_windows.ps1
```

脚本依次完成：全部单元测试 → PyInstaller 打包（`can_host.spec`，产物 `dist\BITFSAE_CAN_Host\`）→ 在 `release\` 生成发布产物：

| 产物 | 用途 |
|---|---|
| `BITFSAE_CAN_Host_v<版本>.zip` | 便携发布与软件内更新下载源（内层必须保持 `BITFSAE_CAN_Host/` 目录） |
| `BITFSAE_CAN_Host_v<版本>.zip.sha256` | ZIP 的 SHA256 校验文件，软件内更新强制校验 |
| `BITFSAE_CAN_Host_v<版本>_setup.exe` | Inno Setup 安装包（`packaging/windows/canhost.iss`） |

本地机器未安装 Inno Setup 6 时跳过安装包并给出警告，仍输出 ZIP；加 `-RequireInstaller` 参数改为直接失败（CI 使用）。可用 `-Label v0.9.0` 指定产物命名标签，默认取 `canhost/__init__.py` 的 `__version__`。exe 图标取自根目录 `app_icon.ico`（由 `canhost/web/assets/shark-mark.svg` 生成），换图标时替换该文件后重新构建。

安装包默认安装到 `%LOCALAPPDATA%\Programs\BITFSAE_CAN_Host`（每用户、无需管理员）；目录名与软件内更新器的整目录替换约定一致，不能随意改名。

## 构建 macOS Apple Silicon DMG

在 Apple Silicon Mac 上使用原生 arm64 Python 3.11 运行：

```bash
bash build_macos.sh
```

脚本默认查找 `python3.11`；自定义安装位置可用 `CANHOST_PYTHON=/路径/python3.11 bash build_macos.sh`。

脚本使用 `.venv-canhost-build-macos`，依次执行全部单元测试、PyInstaller arm64 `.app` 打包、实体 PCAN Python 后端完整性检查、成品依赖自检、最低系统版本核对、ad-hoc 重签与签名校验，最后生成：

| 产物 | 用途 |
|---|---|
| `release/BITFSAE_CAN_Host_macOS_arm64_v<版本>.dmg` | 可拖入“应用程序”的 Apple Silicon 安装镜像 |
| 同名 `.dmg.sha256` | DMG 的 SHA256 校验文件 |

可用 `bash build_macos.sh v0.9.0` 指定产物标签，默认读取 `canhost.__version__`。构建机已安装 `libPCBUSB` 时，脚本还会从冻结后的 `.app` 实际加载驱动库；未安装时只检查 PCAN 后端已完整打包，实体适配器收发仍必须在发布前单独验证。应用图标由 PyInstaller/Pillow 从 `app_icon.ico` 转换为 macOS `icns`。脚本只做 ad-hoc 签名；对外分发如需消除 Gatekeeper 提示，仍需配置 Apple Developer ID、时间戳和 notarization。

## CI 自动构建与发布

仓库在 `.github/workflows/` 下提供三条 GitHub Actions 工作流，使用 Windows、macOS 与 Ubuntu runner：

- `tests.yml`：main 分支推送和 Pull Request 时自动运行全部上位机单元测试。
- `release.yml`：推送 `v*` 标签（如 `v0.9.1`）时触发。Windows 与 macOS arm64 并行测试、打包；两边成功后把 Windows ZIP、校验文件、安装器及 macOS DMG、校验文件附加到同一个 GitHub Release，再同步同一批附件到 CNB 并刷新国内更新频道。CNB 同步失败视为发布失败。手动触发时只提供 Actions 产物，不创建 Release。
- `cnb-mirror.yml`：push 时把全部分支与标签镜像到 CNB；Actions 页面手动触发时把 GitHub 发布补做镜像到 CNB（可填标签，留空同步最新正式发布）。
- 标签版本号后带后缀（如 `v0.2.0-rc1`）时创建的是 GitHub 预发布（Pre-release），版本号主体仍须与 `__version__` 一致；不带后缀的 `vX.Y.Z` 创建正式 Release。

CNB 镜像需要仓库 Actions secret `CNB_TOKEN`：CNB 访问令牌（用户名固定 `cnb`，令牌需包含仓库读写与发布读写权限），镜像目标仓库写在两条工作流的 `CNB_REPO` 环境变量里（当前 `totok22/can-host`）。令牌缺失或同步失败会让发布流水线直接失败，避免镜像落后导致国内用户检查不到新版本。

仓库另有 CNB 云原生构建配置 `.cnb.yml`，作为国内侧的 CI 与补镜像兜底，全部自动执行：

- 分支/标签推送到 CNB 时跑全套上位机单元测试（Linux，覆盖协议、解码、更新器和镜像工具等与平台无关的逻辑）；
- `main` 分支每天 03:20（Asia/Shanghai）由定时任务把 GitHub 上最新的正式发布补齐到 CNB：即使某次 GitHub Actions 失败或被跳过，CNB 也会自己补上。`scripts/cnb_publish.py sync` 先比对附件名称与大小，已是最新镜像时只做几次 API 调用就退出，因此重复执行是安全的；它不依赖 GitHub API（CNB 构建节点共享出口 IP，未认证配额经常被用光）：标签走 `git ls-remote`，附件名与大小按发布约定和 HEAD 探测，代价是这种情况下更新窗口显示“此次 Release 未填写说明”；
- 需要立刻补镜像时走 API：`POST /{repo}/-/build/start`，`event` 为 `api_trigger_mirror_release`，`env.TAG` 指定标签（留空取最新）；也可以在 GitHub 上手工触发 `cnb-mirror.yml`。两条路径都用 CNB 流水线内置的 `CNB_TOKEN`，不需要在 GitHub 保存 CNB 令牌。

发布新版本的操作：

```powershell
# 1. 更新 canhost/__init__.py 的 __version__ 和 __version_date__，连同代码一起提交推送
# 2. 打标签并推送，CI 自动构建并创建 Release
git tag v0.2.0
git push origin v0.2.0
```

Release 同时附上 Windows 的 ZIP、ZIP 校验文件、setup.exe，以及 Apple Silicon 的 DMG、DMG 校验文件。ZIP 是完整 one-folder 目录的压缩包；Windows 安装包用户直接走软件内更新，macOS 用户用新 DMG 覆盖安装。

## 软件内更新

Windows 发布版左下角版本信息可点击，会打开“软件内更新”窗口：

1. 启动时自动检查一次正式版：先读 CNB 国内镜像（`cnb.cool/totok22/can-host` 上由 CI 维护的更新频道与发布附件），镜像不可用时回退 `BITFSAE/can-host` 的 GitHub Release；需要手动检查时点击“检查更新”，勾选“包含预发布版（Pre-release）”可列出 `-rc1` 等预发布。窗口“更新源”一行会显示本次结果来自哪个源。
2. 点击“更新到 vX.Y.Z”即确认本次升级。程序随后自动下载 ZIP 和 `.sha256`，先校验 SHA256，再校验 ZIP 只包含预期的 `BITFSAE_CAN_Host/` 目录（拒绝绝对路径、`..` 和符号链接）；校验通过后无需再次点击。
3. 应用自动退出，由隐藏 PowerShell 助手等待旧进程结束、备份旧目录为 `.old-<时间戳>`、替换并启动新版本。新版完成后端初始化、首轮数据读取和页面加载后写入健康信号，助手收到匹配的进程号与版本号才确认成功；进程提前退出或 45 秒内没有健康信号时，会停止失败的新进程、恢复旧目录并重新打开旧版本。
4. 旧版本清理：新版本成功启动后，下一次启动时自动删除超过 15 分钟回退窗口的旧版本备份和更新临时目录；安装包卸载也会一并删除旧版本备份。回退窗口内如需手动回退，备份位于安装目录的上一级，名字形如 `BITFSAE_CAN_Host.old-<时间戳>`；更早的历史版本可随时从 GitHub Release 重新下载。

安装包（setup.exe）安装的版本与软件内更新完全兼容：更新替换目录后会保留卸载器文件，Windows「设置 - 应用」的卸载入口和开始菜单、桌面快捷方式继续有效；用新版 setup.exe 覆盖安装时会先清空旧目录再安装，不会残留旧文件。成功替换后才能删除备份。更新助手的持久日志位于 `%LOCALAPPDATA%\BITFSAE\CAN Host\update-logs`，更新窗口的高级信息也会显示该位置；最多保留最近 20 份。发生回滚时会显示失败提示，重新打开的旧版本也会读取一次失败结果。

公开仓库的 Release 无需任何凭据即可检查、下载；CNB 镜像的更新频道与附件同样匿名可读，两者的下载地址都由检查结果给出，不会把 GitHub 令牌发往 CNB。若仓库保持私有，使用者在更新窗口保存有 `repo:contents:read` 的只读 GitHub PAT；令牌只保存在本机 `%APPDATA%\BITFSAE\CAN Host\settings.json`，不返回前端、不写入日志。源码运行只能检查更新，不能替换安装目录；macOS DMG 版同样只检查版本，升级时从 GitHub 或 CNB 下载新 DMG 覆盖安装。

发布新版本只需更新版本号、提交推送，再打 `v*` 标签：

```powershell
# 1. 更新 canhost/__init__.py 的 __version__ 和 __version_date__，连同代码一起提交推送
# 2. 打标签并推送，CI 自动构建并创建 Release；标签版本必须与 __version__ 一致
git tag v0.9.1
git push origin v0.9.1
```

此后已有发布版会在启动时检测到新版本。示例版本号请按当前 `canhost/__init__.py` 的实际值替换。

## 文件入口

| 文件 | 作用 |
|---|---|
| `canhost/app.py` | PyWebView 窗口和 JavaScript API（主/整车/台架/IVT/MQTT 五类独立连接） |
| `canhost/transport.py` | 线程化 python-can 传输层：连接、记录、回放、命令发送 |
| `canhost/decoders.py` | 全部 CAN 帧格式的唯一定义（SOP、包状态、IVT、赛会能量计、PDM、整车/电池箱风扇、ECU、胎温） |
| `canhost/monitor.py` | CAN 帧按 ID 汇总、内容变体统计和通用发送参数校验 |
| `canhost/bms/` | BMS 协议状态机、工具命令编码、BMS 模拟器 |
| `canhost/vehicle/` | 整车协议状态机（含风扇命令应答）与整车模拟器 |
| `canhost/ivt.py` | IVT 请求、响应解析和 BMS CAN1 目标比较 |
| `canhost/updater.py` | 发布检查（CNB 国内镜像优先、GitHub 回退）、SHA256 校验、安全解压与 Windows 退出安装；macOS 手动替换 DMG |
| `canhost/telemetry/` | MQTT 只读订阅、TelemetryFrame Protobuf 解码和故障变化记录 |
| `canhost/web/` | 无网络依赖的 HTML/CSS/JavaScript 界面（`js/` 按页面模块拆分） |
| `cli/` | 独立命令行工具 `pcan_bms_bench.py`、`pcan_ivt_tool.py` |
| `scripts/cnb_publish.py` | 把发布产物上传 CNB 并维护国内更新频道（CI 与本地补镜像共用） |
| `.cnb.yml` | CNB 云原生构建：Linux 单元测试 + 定时/API 补做发布镜像 |
| `Tests/` | 单元测试（decoders / bms / fan / ivt / vehicle / monitor / telemetry / updater / cnb_publish） |
| `can_host.spec` / `can_host_macos.spec` | Windows one-folder 与 macOS arm64 `.app` 打包定义 |
| `build_windows.ps1` / `build_macos.sh` | 两个平台的测试、打包和发布产物生成入口 |
| `todo.md` | 上位机待办、风险、验证和变更摘要 |
