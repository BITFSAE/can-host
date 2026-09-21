# BITFSAE CAN Host

BITFSAE 车队 CAN 桌面上位机，用于 BMS 监视、整车 CANB 遥测、CAN 记录与受控发送、MQTT 故障订阅，以及 F405、IVT 和风扇台架工作。程序采用 Python、PyWebView 和零构建前端，发布目标为 Windows 与 Apple Silicon macOS；两种发布包都以实体 PCAN-USB 联调为主要用途。

## 文档入口

| 任务 | 文档 |
|---|---|
| 安装、源码运行、测试、构建和发布 | [`DOC/开发与发布.md`](DOC/开发与发布.md) |
| 连接车辆、使用页面、处理常见异常 | [`DOC/CAN上位机与工具使用.md`](DOC/CAN上位机与工具使用.md) |
| 标定或手动调试两套风扇 | [`DOC/风扇标定与手动调试指南.md`](DOC/风扇标定与手动调试指南.md) |
| 查看待办、风险和未完成验证 | [`todo.md`](todo.md) |
| 查看已发布与未发布变更 | [`CHANGELOG.md`](CHANGELOG.md) |
| 查找接口权威仓库和文档职责 | [`DOC/README.md`](DOC/README.md) |

## 主要能力

- BMS：138 串电压、48 路温度、状态机、故障、告警、记录回放和受控参数命令。
- 整车：SOP、BMS 镜像、PDM、ECU、胎温、赛会能量计和两套风扇遥测。
- 工程工具：CAN 监视与留档、F405 从控台架、IVT 配置、风扇标定、MQTT 故障订阅。
- 连接隔离：实体 CAN1、实体 CANB、台架和 IVT 配置各自管理连接，不因切换页面而改变其他连接；一条 CANB 连接同时供给 BMS 镜像与整车页面。
- 软件更新：并行核对 CNB 与 GitHub、按版本选择最新发布（同版本使用 CNB 下载），下载校验后可一键重启更新；升级后首次启动显示更新前后版本和本次更新条目，并可打开对应发布页或浏览版本历史。

## 支持边界

| 环境 | 发布形式 | 实体 PCAN 依赖 |
|---|---|---|
| Windows 10/11 | 安装器或便携 ZIP | PEAK 驱动与 PCAN-Basic |
| Apple Silicon macOS 12+ | arm64 DMG | MacCAN `libPCBUSB` 0.13+（arm64） |
| Python 3.11+ | Windows/macOS 源码运行 | 对应平台驱动 |

发布包可从 [GitHub Releases](https://github.com/BITFSAE/can-host/releases) 或 [CNB 国内镜像](https://cnb.cool/totok22/can-host/-/releases) 获取。“调试模拟”只在本地源码运行时显示，Windows 与 macOS 发布包均隐藏并拒绝该连接模式；模拟数据不经过驱动、适配器和实体总线，不能作为车辆或台架验收结果。

## 接口与安全

上位机只消费既有 CAN 契约，不定义新帧。BMS、FanController、PDM 和整车 DBC 的权威位置见 [`DOC/README.md`](DOC/README.md)。所有对实体设备生效的写命令必须使用对应的真实 PCAN 连接，并由后端校验总线、状态、参数和应答；通用发送区不能绕过项目已登记命令的专用页面。
