# 文档索引

## 按任务查找

| 要做什么 | 看哪份 |
|---|---|
| 安装、运行、打包发布上位机 | [`../README.md`](../README.md) |
| 使用某个页面（BMS 四页 / 整车总览 / 整车风扇 / 遥测故障 / 台架 / IVT 配置） | [`CAN上位机与工具使用.md`](CAN上位机与工具使用.md) |
| 标定或手动调试整车风扇、电池箱风扇 | [`风扇标定与手动调试指南.md`](风扇标定与手动调试指南.md) |
| 软件内检查、下载和安装 GitHub Release | [`CAN上位机与工具使用.md`](CAN上位机与工具使用.md) 第 10 节 |
| 查某个 CAN 帧的字段、字节序、缩放 | 对应固件仓库协议文档（见下表）；上位机解码定义在 `canhost/decoders.py` |
| 查上位机进度、风险、待验证项 | [`../todo.md`](../todo.md) |

## 接口权威仓库（本地与本仓库同级）

| 接口 | 权威位置 |
|---|---|
| F405 BMS 帧（CAN1/CANB） | `../BMS_MASTER_F405` 的 `DOC/CAN通信协议.md` 与 `App/bms_can_protocol.c` |
| 整车风扇（0x5A2–0x5A9/0x5AE） | `../FanController` 的 `Doc/风扇控制.md` 与 `Core/Src/fan_controller.c` |
| 电池箱风扇（0x5AA–0x5AD） | `../BMS_MASTER_F405` 的 `DOC/风扇控制.md` 与 `App/bms_fan.c` |
| PDM 低压（0x5A0/0x5A1） | `../PDM` 的 `Doc/CAN接口.md` 与 `Core/Src/pdm_monitor.c` |
| 整车 DBC 中央登记 | `../vehicle-interfaces` 的 `can/Vehicle_CanB.dbc` |
| MQTT 遥测 Protobuf | `../vehicle-interfaces` 的 `telemetry/fsae_telemetry.proto`；固件/服务器同步副本在 `../CANRS485_G473/REFERENCE/protobuf-master` |
| IVT 设备配置 | `../BMS_MASTER_F405` 的 `DOC/IVT能量计配置与CANB切换.md` |

固件与 DBC 不一致时以固件为准，并同步修改本仓库解码实现、测试和 vehicle-interfaces。

## 本目录文档

| 文档 | 内容 |
|---|---|
| [`CAN上位机与工具使用.md`](CAN上位机与工具使用.md) | 各页面职责、连接关系、总线安全边界、软件内更新、使用流程、异常处理和验证顺序 |
| [`风扇标定与手动调试指南.md`](风扇标定与手动调试指南.md) | FanController 与 F405 电池箱风扇的接线核对、功率标定、租约手动调试、安全中止和结果验收 |
