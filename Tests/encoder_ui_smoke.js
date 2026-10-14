/* BRT 编码器页的零构建脚本：在最小 DOM 里加载真实模块，核对曲线取数（窗口、跨圈断线、抽稀）、
 * 输入解析和周期估算。用法：node encoder_ui_smoke.js */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const source = name => fs.readFileSync(path.join(root, "canhost/web/js", name), "utf8");

function createContext() {
  const nodes = new Map();
  const element = () => ({ value: "", dataset: {}, classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
    closest() { return null; }, textContent: "", innerHTML: "", addEventListener() {}, querySelector() { return null; },
    querySelectorAll() { return []; }, setAttribute() {}, removeAttribute() {} });
  const document = {
    documentElement: { dataset: {}, style: {} },
    querySelector: selector => { if (!nodes.has(selector)) nodes.set(selector, element()); return nodes.get(selector); },
    querySelectorAll: () => [],
    addEventListener() {},
  };
  const toasts = [];
  const context = { document, localStorage: { getItem: () => null, setItem() {} }, setTimeout, clearTimeout,
    structuredClone, console, window: null, toast: (message, error) => toasts.push({ message, error: !!error }),
    uPlot: undefined, toasts };
  context.window = context;
  context.globalThis = context;
  vm.createContext(context);
  for (const name of ["core.js", "encoder.js"]) vm.runInContext(source(name), context, { filename: name });
  // core.js 定义了真实的 toast（需要完整 DOM）；加载后换回记录用的桩。
  context.toast = (message, error) => toasts.push({ message, error: !!error });
  return context;
}

const context = createContext();
const run = code => {
  const value = vm.runInContext(code, context);
  return value && typeof value === "object" ? JSON.parse(JSON.stringify(value)) : value;
};

// ---- 输入解析
assert.equal(run('encParseAddress("0x01")'), 1);
assert.equal(run('encParseAddress("0xFF")'), 255);
assert.equal(run('encParseAddress(" 16 ")'), 16);
for (const bad of ["", "0x00", "0x100", "abc", "1.5", "-1"]) {
  context.toasts.length = 0;
  assert.equal(run(`encParseAddress(${JSON.stringify(bad)})`), null, bad);
  assert.equal(context.toasts.length, 1, bad);
  assert.equal(context.toasts[0].error, true);
}
assert.equal(run("encHex(5)"), "0x05");
assert.equal(run("encBitrateText(500000)"), "500 kbit/s");
assert.equal(run("encBitrateText(1000000)"), "1 Mbit/s");

// ---- 曲线取数
const fill = (times, raws, positions, speeds) => {
  vm.runInContext(`encResetData();
    enc.data.t = ${JSON.stringify(times)}; enc.data.raw = ${JSON.stringify(raws)};
    enc.data.pos = ${JSON.stringify(positions)}; enc.data.spd = ${JSON.stringify(speeds)};
    enc.origin = enc.data.t[0];`, context);
};
vm.runInContext("encResetData()", context);
assert.equal(run("encPlotTables()"), null);

// 跨圈：4090 → 4094 → 3 → 10，单圈模式在跨圈处插入空值，累计模式连续。
fill([100, 100.01, 100.02, 100.03], [4090, 4094, 3, 10], [4090, 4094, 4099, 4106], [null, 60, 60, 60]);
vm.runInContext('enc.prefs.windowSec = 10; enc.prefs.angleMode = "single";', context);
let tables = run("encPlotTables()");
assert.equal(tables.angle[1].filter(value => value === null).length, 1);
assert.equal(tables.angle[0].length, 5);
assert.equal(tables.angle[0][0], 0);
assert.ok(Math.abs(tables.angle[0][1] - 0.01) < 1e-9);
assert.ok(tables.angle[0][2] > 0.01 && tables.angle[0][2] < 0.02, "断点落在两个样本之间");
assert.ok(Math.abs(tables.angle[1][1] - 4094 * 360 / 4096) < 1e-9);
assert.deepEqual(tables.speed[1], [null, 10, 10, 10]);      // °/s 换算为 rpm
// ±180°：零位附近的抖动（4095 ↔ 0）不再跨越 0/360 边界，也就不断线。
fill([100, 100.01, 100.02, 100.03], [4095, 0, 4094, 1], [4095, 4096, 4094, 4097], [null, 0, 0, 0]);
vm.runInContext('enc.prefs.angleMode = "signed";', context);
tables = run("encPlotTables()");
assert.equal(tables.angle[1].filter(value => value === null).length, 0);
assert.ok(Math.abs(tables.angle[1][0] + 360 / 4096) < 1e-9);          // 4095 → −0.0879°
assert.ok(Math.abs(tables.angle[1][1]) < 1e-9);
fill([100, 100.01, 100.02, 100.03], [4090, 4094, 3, 10], [4090, 4094, 4099, 4106], [null, 60, 60, 60]);
vm.runInContext('enc.prefs.angleMode = "accum";', context);
tables = run("encPlotTables()");
assert.equal(tables.angle[1].filter(value => value === null).length, 0);
assert.ok(Math.abs(tables.angle[1][3] - 4106 * 360 / 4096) < 1e-9);

// 窗口：只取最近 5 s；起点与最新时间相对首个样本。
const times = Array.from({ length: 2001 }, (_, index) => 50 + index * 0.01);     // 20 s，100 Hz
fill(times, times.map(() => 100), times.map(() => 100), times.map(() => 0));
vm.runInContext('enc.prefs.windowSec = 5; enc.prefs.angleMode = "accum";', context);
tables = run("encPlotTables()");
assert.ok(Math.abs(tables.latest - 20) < 1e-9);
assert.ok(Math.abs(tables.from - 15) < 1e-9);
assert.equal(tables.angle[0].length, 501);
assert.ok(tables.angle[0][0] >= 15 - 1e-9);
vm.runInContext("enc.prefs.windowSec = 60;", context);
tables = run("encPlotTables()");
assert.equal(tables.from, 0);                                // 窗口没填满时从 0 开始

// 抽稀：一次绘制不超过上限，且保留最新样本所在的时间范围。
const many = Array.from({ length: 40000 }, (_, index) => index * 0.001);
fill(many, many.map((_, i) => i % 4096), many.map((_, i) => i), many.map(() => 6));
vm.runInContext('enc.prefs.windowSec = 60; enc.prefs.angleMode = "accum";', context);
tables = run("encPlotTables()");
assert.ok(tables.angle[0].length <= 5000, `点数 ${tables.angle[0].length}`);
assert.ok(tables.latest > 39);

// ---- 二分查找
assert.equal(run("encBisect([1, 2, 3, 4], 2.5)"), 2);
assert.equal(run("encBisect([1, 2, 3, 4], 0)"), 0);
assert.equal(run("encBisect([1, 2, 3, 4], 9)"), 4);
assert.equal(run("encBisect([], 1)"), 0);

// ---- 写入记录文字
assert.deepEqual(run("encRecordText(null)"), { text: "本次连接未执行", level: "" });
assert.equal(run('encRecordText({ time: "10:00:00", value: "当前位置 → 0°", detail: "读回 1", verified: true })').level, "ok");
assert.equal(run('encRecordText({ time: "10:00:00", value: "x", detail: "", verified: false })').level, "warn");
assert.equal(run('encRecordText({ time: "10:00:00", value: "x", detail: "", verified: null })').level, "");
assert.equal(run("encDeviceText({ unsigned: 5, signed: 5 }, 3)"), "5 · 3 s 前");
assert.equal(run("encDeviceText({ unsigned: 4294967291, signed: -5 }, null)"), "4294967291 · int32 -5");
assert.equal(run("encDeviceText(null, null)"), "—");

// ---- 周期估算：占用率按 115 bit/帧，可跟踪转速为每周期半圈
const hint = period => {
  vm.runInContext(`document.querySelector("#encPeriod").value = "${period}"; renderEncoderPeriodHint();`, context);
  return context.document.querySelector("#encPeriodHint");
};
let node = hint(5000);
assert.match(node.textContent, /约 200 帧\/s/);
assert.match(node.textContent, /占用约 4\.6%/);
assert.match(node.textContent, /6,000 rpm/);
node = hint(65535);
assert.match(node.textContent, /约 15\.3 帧\/s/);
for (const outOfRange of ["1000", "4999", "abc", ""]) {
  node = hint(outOfRange);
  assert.match(node.textContent, /5000–65535 µs/, outOfRange);   // 低于 5 ms 不给估算，只提示范围
}

console.log("encoder ui smoke ok");
