"""参数写入前的核对清单、输入解析与回读判定，以及风扇确认框的当前条件。"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BMS = ROOT / "canhost" / "web" / "js" / "bms.js"
FAN = ROOT / "canhost" / "web" / "js" / "fan.js"

# 与 core.js 中同名函数保持一致；core.js 顶层会访问 DOM，不能直接载入 vm。
PRELUDE = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function load(extra = {}) {
  const toasts = [];
  const state = {
    pendingWrites: {},
    dirty: {thresholds: false, switches: false, charge: false, direction: false, batteryFanCaps: false},
    inputsInitialized: {thresholds: false, switches: false, charge: false},
    saveWatch: null,
    snapshot: null,
    bootstrap: null,
    configConnectionKey: null,
    vehicleSnapshot: null,
  };
  const context = vm.createContext({
    state,
    isFresh: (age, limit = 1.5) => age != null && Number.isFinite(Number(age)) && age >= 0 && age <= limit,
    hasDataAge: age => age != null && Number.isFinite(Number(age)) && age >= 0,
    fmt: (value, digits = 1, fallback = '—') =>
      value == null || Number.isNaN(value) ? fallback : Number(value).toFixed(digits),
    SLOW_DATA_FRESH_MAX_S: 2.5,
    toast: (message, isError) => toasts.push([message, !!isError]),
    console,
    Date,
    ...extra,
  });
  for (const file of process.argv.slice(1)) {
    vm.runInContext(fs.readFileSync(file, 'utf8'), context);
  }
  return {context, state, toasts};
}
const plain = value => JSON.parse(JSON.stringify(value));
"""


def run_node(script: str, *files: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [shutil.which("node"), "-e", PRELUDE + script, *map(str, files)],
        capture_output=True, text=True, check=False,
    )


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for the control UI check")
class ControlWriteUiTest(unittest.TestCase):
    def check(self, script: str, *files: Path) -> None:
        result = run_node(script, *(files or (BMS,)))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_switch_write_summary_lists_every_value(self) -> None:
        self.check(r"""
const {context} = load();
const catalog = [
  {name: '单体过压', key: 'ov'},
  {name: '单体欠压', key: 'uv'},
  {name: '蜂鸣器', key: 'buzzer'},
];
const summary = plain(context.switchWriteSummary(catalog, {ov: true, uv: false, buzzer: true}));
assert.equal(summary.total, 3);
assert.equal(summary.on, 2);
assert.equal(summary.off, 1);
assert.match(summary.detail, /^开 {2}单体过压\n关 {2}单体欠压\n开 {2}蜂鸣器$/);
// 缺失的键按“关”列出，清单为空时 total 为 0，调用方据此拒绝写入
assert.equal(plain(context.switchWriteSummary(catalog, {})).off, 3);
assert.equal(context.switchWriteSummary([], {ov: true}).total, 0);
""")

    def test_echo_needs_fresh_matching_report(self) -> None:
        self.check(r"""
const {context} = load();
const snapshot = (config, relay = {}) => ({config, relay});
const thresholds = {ov_mv: 4300, uv_mv: 3100, ot_c: 60, ut_c: 0};
assert.equal(context.writeEchoMatches('alarm_thresholds',
  {ov_mv: 4300, uv_mv: 3100, ot_c: 60, ut_c: 0},
  snapshot({thresholds_age: 0.5, thresholds})), true);
assert.equal(context.writeEchoMatches('alarm_thresholds',
  {ov_mv: 4200, uv_mv: 3100, ot_c: 60, ut_c: 0},
  snapshot({thresholds_age: 0.5, thresholds})), false);
assert.equal(context.writeEchoMatches('alarm_thresholds',
  {ov_mv: 4300, uv_mv: 3100, ot_c: 60, ut_c: 0},
  snapshot({thresholds_age: 4, thresholds})), false, '过期回报不能算一致');
assert.equal(context.writeEchoMatches('alarm_thresholds',
  {ov_mv: 4300, uv_mv: 3100, ot_c: 60, ut_c: 0},
  snapshot({thresholds})), false, '缺少回报时间不能算一致');

const switches = {ov: true, uv: false, buzzer: true};
assert.equal(context.writeEchoMatches('alarm_switches', {switches},
  snapshot({switches_age: 0.5, switches})), true);
assert.equal(context.writeEchoMatches('alarm_switches', {switches},
  snapshot({switches_age: 0.5, switches: {ov: true, uv: false}})), false,
  '缺少一项回报不能算一致');
assert.equal(context.writeEchoMatches('alarm_switches', {switches},
  snapshot({switches_age: 0.5, switches: {ov: true, uv: true, buzzer: true}})), false,
  '有一项不同不能算一致');
assert.equal(context.writeEchoMatches('alarm_switches', {switches: {}},
  snapshot({switches_age: 0.5, switches})), false, '空写入不能算一致');

// 充电请求电压分辨率 0.1 V，回报与写入相差不超过一个分辨率才算一致
assert.equal(context.writeEchoMatches('charge_config', {voltage_v: 575, current_a: 3},
  snapshot({}, {command_age: 0.5, request_voltage_v: 575.04, request_current_a: 3})), true);
assert.equal(context.writeEchoMatches('charge_config', {voltage_v: 575, current_a: 3},
  snapshot({}, {command_age: 0.5, request_voltage_v: 574.8, request_current_a: 3})), false);
assert.equal(context.writeEchoMatches('charge_config', {voltage_v: 575, current_a: 3},
  snapshot({}, {command_age: 4, request_voltage_v: 575, request_current_a: 3})), false);

// 方向回报是布尔（协议解码）或 0/1，写入值是布尔
assert.equal(context.writeEchoMatches('current_direction', {inverted: true},
  snapshot({runtime_age: 0.5, current_direction_inverted: 0})), false);
assert.equal(context.writeEchoMatches('current_direction', {inverted: true},
  snapshot({runtime_age: 0.5, current_direction_inverted: true})), true);
assert.equal(context.writeEchoMatches('current_direction', {inverted: false},
  snapshot({runtime_age: 0.5, current_direction_inverted: false})), true);
assert.equal(context.writeEchoMatches('current_direction', {inverted: false},
  snapshot({runtime_age: 3, current_direction_inverted: false})), false);
assert.equal(context.writeEchoMatches('rtc', {}, snapshot({})), false, '未登记的命令不做一致判定');
""")

    def test_pending_writes_settle_independently_and_overdue_is_flagged(self) -> None:
        self.check(r"""
const {context, state} = load();
const snapshot = (config, relay = {}) => ({config, relay});
const thresholds = {ov_mv: 4300, uv_mv: 3100, ot_c: 60, ut_c: 0};
const switches = {ov: true, uv: false, buzzer: true};

// 两条命令各自等待，互不覆盖；只有匹配的那条结算
state.dirty.thresholds = true;
state.dirty.switches = true;
state.pendingWrites = {
  thresholds: {name: 'alarm_thresholds', values: thresholds, at: Date.now()},
  switches: {name: 'alarm_switches', values: {switches}, at: Date.now()},
};
state.snapshot = snapshot({thresholds_age: 0.5, thresholds, switches_age: 0.5,
  switches: {ov: true, uv: true, buzzer: true}});
context.settlePendingWrite();
assert.equal(state.pendingWrites.thresholds, undefined);
assert.ok(state.pendingWrites.switches, '尚未回读的开关写入不能被阈值结算覆盖');
assert.equal(state.dirty.thresholds, false);
assert.equal(state.dirty.switches, true);
assert.notEqual(state.pendingWrites.switches.overdue, true, '8 s 内不算超时');

// 超过 8 s 仍不一致：明确标成回读不一致，草稿保留
state.pendingWrites.switches.at = Date.now() - 9000;
context.settlePendingWrite();
assert.equal(state.pendingWrites.switches.overdue, true);
assert.equal(state.dirty.switches, true);
assert.deepEqual(plain(context.writeSyncView('alarm_switches', true)),
  {text: '已下发 · 回读不一致', className: 'bad'});

// 超时之后回报终于一致，也要结算掉
state.snapshot = snapshot({switches_age: 0.5, switches});
context.settlePendingWrite();
assert.equal(state.pendingWrites.switches, undefined);
assert.equal(state.dirty.switches, false);
""")

    def test_sync_view_labels(self) -> None:
        self.check(r"""
const {context, state} = load();
assert.deepEqual(plain(context.writeSyncView('alarm_thresholds', false)),
  {text: '等待主控回报', className: 'neutral'});
assert.deepEqual(plain(context.writeSyncView('alarm_thresholds', true)),
  {text: '已与主控一致', className: 'ok'});
state.dirty.thresholds = true;
assert.deepEqual(plain(context.writeSyncView('alarm_thresholds', true)),
  {text: '页面值待写入', className: 'warn'});
state.pendingWrites.thresholds = {name: 'alarm_thresholds', values: {}, at: Date.now()};
assert.deepEqual(plain(context.writeSyncView('alarm_thresholds', true)),
  {text: '已下发 · 等待回读', className: 'warn'});
""")

    def test_connection_change_clears_drafts_but_keeps_fan_page_draft(self) -> None:
        self.check(r"""
const {context, state, toasts} = load();
const snap = (extra = {}) => ({connection: {connected: true, mode: 'pcan', channel: 'PCAN_USBBUS1',
  bus_profile: 'can1', bitrate: 500000, ...extra}});

// 首次只记录连接标识，不当作“变化”
state.dirty.thresholds = true;
context.syncConfigConnection(snap());
assert.equal(state.dirty.thresholds, true);
assert.equal(toasts.length, 0);

// 同一连接重复渲染不清草稿
context.syncConfigConnection(snap());
assert.equal(state.dirty.thresholds, true);

// 换通道：清 BMS 草稿并提示；风扇页自己的草稿保留
state.dirty.batteryFanCaps = true;
state.dirty.charge = true;
state.pendingWrites = {switches: {name: 'alarm_switches', values: {}, at: Date.now()}};
state.inputsInitialized = {thresholds: true, switches: true, charge: true};
context.syncConfigConnection(snap({channel: 'PCAN_USBBUS2'}));
assert.deepEqual(plain(state.pendingWrites), {});
assert.deepEqual(plain(state.inputsInitialized), {thresholds: false, switches: false, charge: false});
assert.ok(['thresholds', 'switches', 'charge', 'direction'].every(key => state.dirty[key] === false));
assert.equal(state.dirty.batteryFanCaps, true);
assert.equal(toasts.length, 1);
assert.equal(toasts[0][1], true);

// 没有草稿时换连接不弹提示
context.syncConfigConnection(snap({channel: 'PCAN_USBBUS3'}));
assert.equal(toasts.length, 1);

// 断开、换总线类型、换码率都算连接变化
state.dirty.direction = true;
context.syncConfigConnection(snap({connected: false}));
assert.equal(state.dirty.direction, false);
state.dirty.direction = true;
context.syncConfigConnection(snap({bus_profile: 'canb'}));
assert.equal(state.dirty.direction, false);
state.dirty.direction = true;
context.syncConfigConnection(snap({bus_profile: 'canb', bitrate: 250000}));
assert.equal(state.dirty.direction, false);
""")

    def test_threshold_and_charge_inputs_never_turn_blank_into_zero(self) -> None:
        self.check(r"""
const {context} = load();
const thresholds = raw => plain(context.thresholdWriteValues(raw));
assert.deepEqual(thresholds(['4300', '3100', '60', '0']),
  {values: {ov_mv: 4300, uv_mv: 3100, ot_c: 60, ut_c: 0}});
// 固件按整数保存：小数先取整，确认框和下发用同一组值
assert.deepEqual(thresholds(['4300.4', '3099.6', '60.2', '-0.4']).values,
  {ov_mv: 4300, uv_mv: 3100, ot_c: 60, ut_c: 0});
assert.deepEqual(thresholds([' 4300 ', '3100', '60', '-5']).values.ut_c, -5);
// 任何一项为空、纯空白或非数字都拒绝，不能被 Number('') 变成 0
for (const raw of [['', '3100', '60', '0'], ['4300', '3100', '60', ''],
                   ['4300', '   ', '60', '0'], ['4300', '3100', 'abc', '0'],
                   ['4300', '3100', '60'], null]) {
  assert.deepEqual(thresholds(raw), {error: '请填写完整的保护阈值'}, JSON.stringify(raw));
}

const charge = (v, a) => plain(context.chargeWriteValues(v, a));
assert.deepEqual(charge('575', '3'), {values: {voltage_v: 575, current_a: 3}});
assert.deepEqual(charge('575.5', '0'), {values: {voltage_v: 575.5, current_a: 0}});
assert.deepEqual(charge('', '3'), {error: '请填写完整的充电请求'});
assert.deepEqual(charge('575', ' '), {error: '请填写完整的充电请求'});
assert.deepEqual(charge('0', '3'), {error: '请填写有效的充电请求'});
assert.deepEqual(charge('575', '-1'), {error: '请填写有效的充电请求'});
assert.deepEqual(charge('abc', '3'), {error: '请填写有效的充电请求'});
""")

    def test_fan_confirm_condition_line_never_uses_stale_values(self) -> None:
        self.check(r"""
const {context, state} = load();
const fan = (patch = {}) => ({
  diagnostic: {mode_name: '自动', motor_temp_c: 55.04, controller_temp_c: 40},
  power_status: {power_supply_name: '主供电'},
  diagnostic_age: 0.3, power_status_age: 0.4,
  ...patch,
});
const line = patch => {
  state.vehicleSnapshot = {fan: fan(patch)};
  return context.fanConditionLine();
};
assert.equal(line(), '当前：自动 · 电机 55.0 °C · 控制器 40.0 °C · 供电 主供电');

const stale = '当前状态未更新';
assert.equal(line({diagnostic_age: 2}), stale, '诊断帧过期');
assert.equal(line({power_status_age: 2}), stale, '功率状态帧过期');
assert.equal(line({diagnostic_age: null}), stale, '从未收到诊断帧');
assert.equal(line({power_status_age: undefined}), stale, '从未收到功率状态帧');
assert.equal(line({diagnostic: {mode_name: '自动', motor_temp_c: null, controller_temp_c: 40}}), stale,
  '0x7FFF 解码为空的温度不能写成 0');
assert.equal(line({diagnostic: {mode_name: '', motor_temp_c: 55, controller_temp_c: 40}}), stale);
assert.equal(line({power_status: {}}), stale);
state.vehicleSnapshot = null;
assert.equal(context.fanConditionLine(), stale, '没有整车快照也不抛错');
""", FAN)


if __name__ == "__main__":
    unittest.main()
