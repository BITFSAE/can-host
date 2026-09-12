# 文档索引

## 本仓库文档

| 文档 | 负责内容 |
|---|---|
| [`../README.md`](../README.md) | 项目简介、能力边界和文档入口 |
| [`开发与发布.md`](开发与发布.md) | 安装、源码运行、测试、打包、CI、发布和软件更新 |
| [`CAN上位机与工具使用.md`](CAN上位机与工具使用.md) | 页面、连接、安全边界、异常处理和现场验证 |
| [`风扇标定与手动调试指南.md`](风扇标定与手动调试指南.md) | FanController 与 F405 电池箱风扇的标定、租约和验收 |
| [`../todo.md`](../todo.md) | 当前状态、未完成工作、风险和最近验证 |
| [`../CHANGELOG.md`](../CHANGELOG.md) | 已发布与未发布的行为和兼容性变化 |

## 接口权威位置

兄弟仓库与本仓库同级存放。固件与 DBC 不一致时先以固件确认实际行为，再同步上位机、测试和 vehicle-interfaces。

| 接口 | 权威位置 |
|---|---|
| F405 BMS CAN1/CANB | `../BMS_MASTER_F405/`：`App/bms_can_protocol.c`、`DOC/CAN通信协议.md` |
| FanController | `../FanController/`：`Core/Src/fan_controller.c`、`Doc/风扇控制.md` |
| F405 电池箱风扇 | `../BMS_MASTER_F405/`：`App/bms_fan.c`、`DOC/风扇控制.md` |
| PDM | `../PDM/`：`Core/Src/pdm_monitor.c`、`Doc/CAN接口.md` |
| 整车 DBC | `../vehicle-interfaces/can/Vehicle_CanB.dbc` |
| MQTT Protobuf | `../vehicle-interfaces/telemetry/fsae_telemetry.proto` |
| IVT 目标配置 | `../BMS_MASTER_F405/DOC/IVT能量计配置与CANB切换.md` |

上位机的 CAN 解码只在 `canhost/decoders.py` 定义；协议变化同时更新对应状态机和 `Tests/`。
