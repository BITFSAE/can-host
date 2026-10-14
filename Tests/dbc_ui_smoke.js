/* DBC 页的零构建脚本：在最小 DOM 里加载真实模块，核对位布局与后端一致、
 * 显示规则（等待 / 过期 / DLC 不符不得显示成 0）和轮询请求。
 * 用法：把 Python 生成的位布局用例 JSON 写到标准输入（命令行放不下，Windows 限 32K 字符）：node dbc_ui_smoke.js < cases.json */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const source = name => fs.readFileSync(path.join(root, "canhost/web/js", name), "utf8");

function createContext() {
  const nodes = new Map();
  const element = () => ({ value: "", dataset: {}, classList: { toggle() {}, add() {}, remove() {} },
    closest() { return null; }, textContent: "", innerHTML: "", addEventListener() {}, querySelector() { return null; },
    querySelectorAll() { return []; } });
  const document = {
    documentElement: { dataset: {}, style: {} },
    querySelector: selector => { if (!nodes.has(selector)) nodes.set(selector, element()); return nodes.get(selector); },
    querySelectorAll: () => [],
    addEventListener() {},
  };
  const context = { document, localStorage: { getItem: () => null, setItem() {} }, setTimeout, clearTimeout,
    structuredClone, console, window: null, toast() {}, uPlot: undefined };
  context.window = context;
  context.globalThis = context;
  vm.createContext(context);
  for (const name of ["core.js", "monitor.js", "dbc.js", "dbc-plot.js", "dbc-editor.js", "dbc-tools.js"]) {
    vm.runInContext(source(name), context, { filename: name });
  }
  return context;
}

const context = createContext();
// vm 里的数组属于另一个全局对象，直接 deepEqual 会因原型不同而失败；统一转成 JSON 值再比较。
const run = code => {
  const value = vm.runInContext(code, context);
  return value && typeof value === "object" ? JSON.parse(JSON.stringify(value)) : value;
};
const input = fs.readFileSync(0, "utf8").trim();
const cases = JSON.parse(input || "[]");

// 1. 位布局：与 canhost/dbc/layout.py 逐位一致
assert.ok(cases.length > 100, "需要 Python 提供的位布局用例");
for (const { start, length, order, bits } of cases) {
  assert.deepEqual(run(`dbcSignalBits(${start}, ${length}, "${order}")`), bits, `${order} ${start}|${length}`);
}

// 2. 拖选：两个端点都落在结果信号的位里，且长度等于顺序上的距离
for (const order of ["intel", "motorola"]) {
  for (let first = 0; first < 64; first += 5) {
    for (let second = 0; second < 64; second += 7) {
      const range = run(`dbcRangeFromCells(${first}, ${second}, "${order}")`);
      const bits = run(`dbcSignalBits(${range.start}, ${range.length}, "${order}")`);
      assert.ok(bits.includes(first) && bits.includes(second), `${order} ${first}->${second}`);
      assert.equal(new Set(bits).size, range.length);
    }
  }
}

// 2b. 多路复用：不同选择值的信号可以共用位，同一选择值或非复用信号不行
const muxed = value => ({ mux: { role: "multiplexed", value, ext: null } });
const plain = { mux: { role: "none", value: null, ext: null } };
assert.equal(run(`dbcSignalsCoexist(${JSON.stringify(muxed(0))}, ${JSON.stringify(muxed(1))})`), false);
assert.equal(run(`dbcSignalsCoexist(${JSON.stringify(muxed(1))}, ${JSON.stringify(muxed(1))})`), true);
assert.equal(run(`dbcSignalsCoexist(${JSON.stringify(muxed(0))}, ${JSON.stringify(plain)})`), true);
const extended = (low, high) => ({ mux: { role: "multiplexed", value: low, ext: { signal: "Sel", ranges: [[low, high]] } } });
assert.equal(run(`dbcSignalsCoexist(${JSON.stringify(extended(0, 1))}, ${JSON.stringify(extended(2, 3))})`), false);
assert.equal(run(`dbcSignalsCoexist(${JSON.stringify(extended(0, 2))}, ${JSON.stringify(extended(2, 3))})`), true);

// 3. 数值与时间文字
assert.equal(run("dbcNumber(3.0)"), "3");
assert.equal(run("dbcNumber(0.30000000000000004)"), "0.3");
assert.equal(run("dbcNumber(null)"), "—");
assert.equal(run('dbcNumber("18446744073709551615")'), "18446744073709551615");
assert.equal(run("dbcNumber(1e-7)"), "1.0000e-7");
assert.equal(run("dbcAgeText(250)"), "250 ms");
assert.equal(run("dbcAgeText(2500)"), "2.5 s");
assert.equal(run("dbcAgeText(null)"), "—");
assert.equal(run("dbcIdText(291, false)"), "0x123");
assert.equal(run("dbcIdText(0x1806E5F4, true)"), "0x1806E5F4");
assert.equal(run("dbcKey(291, true)"), "e:291");

// 4. 等待数据 / 未生效 / DLC 不足 / 表外值：绝不显示成 0
const waiting = run('dbcValueCells({ state: "waiting", unit: "V" }, false)');
assert.equal(waiting.value, "等待数据");
assert.notEqual(waiting.value, "0");
assert.equal(run('dbcValueCells({ state: "inactive" }, false)').value, "未生效");
assert.equal(run('dbcValueCells({ state: "missing" }, false)').value, "DLC 不足");
const stale = run('dbcValueCells({ state: "ok", value: 12.5, raw: 125, raw_text: "125 (0x7D)", unit: "V", in_range: true }, true)');
assert.match(stale.value, /12\.5/);
assert.equal(stale.cls, "stale");
const unlabeled = run('dbcValueCells({ state: "ok", value: 7, raw: 7, raw_text: "7", has_label: true, label: null, in_range: true }, false)');
assert.match(unlabeled.label, /表外值/);
assert.equal(run('dbcValueCells({ state: "ok", value: 9, raw: 9, raw_text: "9", in_range: false }, false)').cls, "out");

// 5. 报文状态文字
assert.equal(run('dbcStateInfo({ state: "waiting" })').label, "等待数据");
assert.match(run('dbcStateInfo({ state: "stale", age_ms: 3200 })').label, /已过期 3\.2 s/);
assert.equal(run('dbcStateInfo({ state: "live", issue: "dlc_short" })').label, "DLC 不符");
assert.equal(run('dbcStateInfo({ state: "live" })').dot, "live");
assert.equal(run("dbcStateInfo(null).label"), "未绑定");

// 6. 轮询请求：监视器页只取名称，DBC 页取选中报文和观察项
run('state.page = "frames"; state.frameSource = "vehicle"; monitorUi.dbcExpanded = new Set(["vehicle|s:291", "main|s:1"])');
let request = run("dbcLiveRequest()");
assert.equal(request.source, "vehicle");
assert.deepEqual(request.options, { keys: ["s:291"], messages: false, unknown: false });
run(`state.page = "dbc"; dbcUi.source = "main"; dbcUi.tab = "live"; dbcUi.selected = { key: "s:5", signal: null };
     dbcUi.watch = [{ source: "main", key: "s:6", signal: "A" }, { source: "vehicle", key: "s:7", signal: "B" }]`);
request = run("dbcLiveRequest()");
assert.equal(request.source, "main");
assert.deepEqual([...request.options.keys].sort(), ["s:5", "s:6"]);
assert.equal(request.options.unknown, true);
run('dbcUi.tab = "audit"');
assert.equal(run("dbcLiveRequest()").options.unknown, false);

// 7. 监视器行：内置名优先，未登记时用 DBC 名，展开前不画解码行
run('state.frameSource = "main"; monitorUi.dbcExpanded = new Set()');
const group = { arbitration_id: 0x7F0, extended: false, name: "未定义帧" };
assert.match(run(`monitorNameCell(${JSON.stringify(group)}, { "s:2032": "EngineData" })`), /EngineData/);
const registered = run(`monitorNameCell({ arbitration_id: 0x4B0, extended: false, name: "BMS 汇总" }, { "s:1200": "BMS_Summary" })`);
assert.match(registered, /BMS 汇总/);
assert.match(registered, /DBC BMS_Summary/);
assert.doesNotMatch(run(`monitorNameCell({ arbitration_id: 0x1, extended: false, name: "x" }, {})`), /monitor-dbc-toggle/);
assert.equal(run(`monitorDbcRow(${JSON.stringify(group)}, {})`), "");
run('monitorUi.dbcExpanded = new Set(["main|s:2032"])');
const decodedRow = run(`monitorDbcRow(${JSON.stringify(group)}, { "s:2032": { signals: [
  { name: "Speed", state: "ok", value: 50, unit: "km/h", has_label: false },
  { name: "Gear", state: "ok", value: 3, has_label: true, label: "D" },
  { name: "Ana", state: "inactive" }] } })`);
assert.match(decodedRow, /Speed/);
assert.match(decodedRow, /50 km\/h/);
assert.match(decodedRow, /D/);
assert.match(decodedRow, /未生效/);

// 8. 曲线：窗口只保留最近的数据；同一信号不能重复加入
run('dbcPlot.series = []; dbcPlot.mode = "live"; dbcPlot.windowSec = 10; dbcPlot.origin = 100');
run(`dbcPlot.series.push({ sid: "main:s:1:X", source: "main", key: "s:1", signal: "X", color: 0, unit: "" });
     dbcPlot.data.set("main:s:1:X", { t: [100, 105, 110, 115], v: [1, 2, 3, 4], cursor: 4 })`);
const table = run("dbcPlotTable()");
assert.deepEqual(table.tables[0][0], [5, 10, 15]);
assert.equal(table.latest, 15);
assert.equal(run('dbcPlotHas("main", "s:1", "X")'), true);
run("dbcPlot.normalize = true");
const normalized = run("dbcPlotTable().tables[0][1]");
assert.equal(normalized.length, 3);
[100 / 3, 200 / 3, 100].forEach((value, index) => assert.ok(Math.abs(normalized[index] - value) < 1e-9));
assert.equal(run('dbcSid("main", "s:1", "X")'), "main:s:1:X");

// 9. CSV：同一时间轴上的列，缺失处留空
context.uPlot = { join: tables => {
  const times = [...new Set(tables.flatMap(([t]) => t))].sort((a, b) => a - b);
  return [times, ...tables.map(([t, v]) => times.map(x => (t.includes(x) ? v[t.indexOf(x)] : null)))];
} };
run(`dbcPlot.series = [{ sid: "a", source: "main", key: "s:1", signal: "X", color: 0, unit: "V" },
                       { sid: "b", source: "main", key: "s:1", signal: "Y", color: 1, unit: "" }];
     dbcPlot.normalize = false; dbcPlot.origin = 0;
     dbcPlot.data = new Map([["a", { t: [1, 2], v: [10, 20], cursor: 2 }], ["b", { t: [2, 3], v: [5, 6], cursor: 2 }]])`);
const csv = run("dbcPlotCsv()").split("\r\n");
assert.equal(csv[0], "时间 s,X [V],Y");
assert.deepEqual(csv.slice(1), ["1.000000,10,", "2.000000,20,5", "3.000000,,6"]);

console.log("dbc ui smoke ok");
