"""双路 PCAN 的包状态和故障监视回退。"""

import json
from pathlib import Path
import shutil
import subprocess
import unittest

from canhost.decoders import CanFrame
from canhost.transport import CanService


WEB_JS = Path(__file__).resolve().parents[1] / "canhost" / "web" / "js"


@unittest.skipUnless(shutil.which("node"), "总览脚本测试需要 Node.js")
class BmsOverviewUiTest(unittest.TestCase):
    def test_dual_bus_pack_status_falls_back_and_recovers(self):
        can1 = CanService()
        canb = CanService(protocol_kind="vehicle")
        can1.connection.update(connected=True, mode="pcan", channel="PCAN_USBBUS1")
        canb.connection.update(connected=True, mode="pcan", channel="PCAN_USBBUS2", bus_profile="canb")
        now = 10.0
        can1.protocol.clock = canb.protocol.clock = canb.canb_bms_protocol.clock = lambda: now

        # CAN1 有接收，包状态尚未到达；预充电压仍由 CAN1 供给。
        can1._ingest(CanFrame(0x186350F4, bytes.fromhex("00 00 00 00 00 00 03 E8"), True))
        canb._ingest(CanFrame(0x4B0, bytes.fromhex("16 44 00 0A 4E 1F 50 A2"), False))

        fault = bytes.fromhex("32 00 00 00 01 00 00 04")
        canb._ingest(CanFrame(0x4B1, fault, False))
        canb._ingest(CanFrame(0x4B2, b"\x02" + bytes(7), False))

        def snapshots():
            return [can1.snapshot(), {"vehicle": canb.quick_snapshot()}]

        waiting = snapshots()
        # 一旦 CAN1 恢复，即使新帧明确标记电压无效，也应服从 CAN1 的有效位。
        can1._ingest(CanFrame(0x186050F4, bytes.fromhex("16 44 00 1E 50 1E 30 A0"), True))
        recovered = snapshots()
        can1._ingest(CanFrame(0x187650F4, fault, True))
        matching_fault = snapshots()
        can1._ingest(CanFrame(0x187650F4, bytes.fromhex("31 00 00 00 02 00 00 04"), True))
        differing_fault = snapshots()
        now += 1.6
        expired = snapshots()
        canb._ingest(CanFrame(0x4B0, bytes.fromhex("16 44 00 0A 4E 1F 50 A2"), False))
        fallback_again = snapshots()
        canb.connection["connected"] = False
        disconnected = snapshots()

        script = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const nodes = new Map();
const document = {
  documentElement: { dataset: {} },
  addEventListener() {},
  querySelector(id) {
    if (!nodes.has(id)) nodes.set(id, { style: {}, classList: { toggle() {} } });
    return nodes.get(id);
  },
};
const context = vm.createContext({ document, window: {} });
for (const file of process.argv.slice(1)) vm.runInContext(fs.readFileSync(file, "utf8"), context);
const [waiting, recovered, expired, fallbackAgain, disconnected, matchingFault, differingFault] = JSON.parse(fs.readFileSync(0, "utf8"));
function render([snapshot, quickSnapshot]) {
  Object.assign(context.state, { snapshot, quickSnapshot });
  context.renderOverview();
  return ["#packVoltage", "#packCurrent", "#packSoc", "#prechargeVoltage", "#stateName"]
    .map(id => nodes.get(id).textContent);
}
assert.deepEqual(render(waiting), ["570.0", "1.0", "78.2", "100.0", waiting[1].vehicle.pack.state_name]);
assert.deepEqual(render(recovered), ["等待数据", "3.0", "80.0", "100.0", recovered[0].overview.state_name]);
assert.equal(render(expired)[0], "等待数据");
assert.equal(render(fallbackAgain)[0], "570.0");
assert.equal(render(disconnected)[0], "等待数据");
function alarm([snapshot, quickSnapshot]) {
  Object.assign(context.state, { snapshot, quickSnapshot });
  context.renderAlarmSummary();
  return ["#faultCode", "#alarmPageLevel", "#alarmActiveCount"].map(id => nodes.get(id).textContent);
}
assert.deepEqual(alarm(waiting), ["0x00000001", "二级告警", "1 项"]);
assert.deepEqual(alarm(matchingFault), ["0x00000001", "二级告警", "1 项"]);
assert.deepEqual(alarm(differingFault), ["0x00000002", "一级故障", "等待等级"]);
assert.deepEqual(alarm(expired), ["等待数据", "等待数据", "等待数据"]);
assert.equal(alarm(disconnected)[0], "等待数据");
waiting[0].connection.mode = "replay";
assert.equal(alarm(waiting)[0], "等待数据");
assert.equal(render(waiting)[0], "等待数据");
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(WEB_JS / "core.js"), str(WEB_JS / "bms.js")],
            input=json.dumps([waiting, recovered, expired, fallback_again, disconnected, matching_fault, differing_fault]),
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
