/* Navigation preferences, read-only inspection and async DBC view isolation. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const nodes = new Map(), saved = new Map(), timers = new Map(), pending = [];
let nextTimer = 0;
const element = () => ({
  dataset: {}, style: {}, attributes: {}, listeners: {}, textContent: "", innerHTML: "", scrollTop: 0,
  classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
  setAttribute(key, value) { this.attributes[key] = value; },
  removeAttribute(key) { delete this.attributes[key]; },
  addEventListener(key, value) { this.listeners[key] = value; },
  scrollTo(left, top) { this.scrollTop = top; },
  querySelector() { return null; }, querySelectorAll() { return []; }, contains() { return false; },
});
const node = selector => { if (!nodes.has(selector)) nodes.set(selector, element()); return nodes.get(selector); };
const narrow = { matches: true, addEventListener(key, fn) { this.changed = fn; } };
const context = {
  document: { documentElement: { dataset: {}, style: {} }, querySelector: node,
    querySelectorAll: () => [], addEventListener() {} },
  localStorage: { getItem: key => saved.get(key) ?? null, setItem: (key, value) => saved.set(key, value) },
  structuredClone, console, Event: class {},
  setTimeout(fn) { const id = ++nextTimer; timers.set(id, fn); return id; },
  clearTimeout(id) { timers.delete(id); },
  pending, matchMedia: () => narrow, dispatchEvent() {}, toast() {},
};
context.window = context;
vm.createContext(context);
for (const name of ["core.js", "dbc.js", "dbc-editor.js", "dbc-tools.js"]) {
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../canhost/web/js", name), "utf8"), context);
}
const run = code => vm.runInContext(code, context);

async function main() {
  // An explicit choice survives both reload and subsequent breakpoint changes.
  run("bindNavigation()");
  assert.equal(context.document.documentElement.dataset.rail, "collapsed");
  node("#railToggle").listeners.click();
  assert.equal(saved.get("canHostRail"), "expanded");
  narrow.changed();
  assert.equal(context.document.documentElement.dataset.rail, "expanded");
  assert.equal(node("#railToggle").attributes["aria-expanded"], "true");
  run("bindNavigation()");
  assert.equal(context.document.documentElement.dataset.rail, "expanded");

  // Disabled storage must never prevent navigation from initializing.
  const originalStorage = context.localStorage;
  context.localStorage = { getItem() { throw Error("disabled"); }, setItem() { throw Error("disabled"); } };
  run("bindNavigation()");
  node("#railToggle").listeners.click();
  context.localStorage = originalStorage;

  // Read-only bit cells can be inspected but a drag cannot edit the document.
  run(`dbcUi.files = [{ id: "readonly", readonly: true }]; dbcUi.fileId = "readonly";
    dbcUi.details.set("readonly", { catalog: { messages: [{ key: "s:1", signals: [{ name: "Speed", bits: [0, 1] }] }] } });
    dbcUi.selected = { key: "s:1", signal: null }; state.page = "overview";
    dbcEdit.drag = { a: 0, b: 0 }; dbcFinishDrag();`);
  assert.equal(run("dbcUi.selected.signal"), "Speed");
  context.commits = 0;
  run("dbcCommit = () => { commits += 1; }; dbcEdit.drag = { a: 0, b: 5 }; dbcFinishDrag()");
  assert.equal(context.commits, 0);

  // Dragging from another signal's bit moves that signal, not the current selection.
  run(`dbcUi.files = [{ id: "edit", readonly: false }]; dbcUi.fileId = "edit";
    dbcUi.details.set("edit", { catalog: { messages: [{ key: "s:1", signals: [
      { name: "A", bits: [0], byte_order: "intel", mux: { role: "none" } },
      { name: "B", bits: [1], byte_order: "intel", mux: { role: "none" } }] }] } });
    dbcUi.selected = { key: "s:1", signal: "A" }; dbcEdit.drag = { a: 1, b: 2, owner: "B" };
    dbcCommit = ops => { commits += 1; opsSeen = ops; };`);
  context.commits = 0;
  run("dbcFinishDrag()");
  assert.equal(context.commits, 1);
  assert.equal(run("opsSeen[0].signal"), "B");
  assert.equal(run("dbcUi.selected.signal"), "B");

  // A single click on empty space clears the selection; an owned bit selects its signal.
  run(`dbcUi.selected = { key: "s:1", signal: "B" }; dbcEdit.drag = { a: 5, b: 5 }; dbcFinishDrag();`);
  assert.equal(run("dbcUi.selected.signal"), null);
  run(`dbcEdit.drag = { a: 1, b: 1 }; dbcFinishDrag();`);
  assert.equal(run("dbcUi.selected.signal"), "B");

  // Context menus expose editing actions and disable them for a read-only file.
  run(`dbcUi.files = [{ id: "ro", readonly: true }]; dbcUi.fileId = "ro";`);
  const messageMenu = run('dbcMessageMenuItems({ key: "s:1", signals: [], virtual: false })');
  assert.ok(messageMenu.some(item => item.label === "查看报文属性"));
  assert.ok(messageMenu.some(item => item.label === "新建报文…" && item.disabled));
  const signalMenu = run('dbcSignalMenuItems({ key: "s:1", signals: [{ name: "A" }] }, { name: "A" })');
  assert.ok(signalMenu.some(item => item.label === "查看信号属性"));
  assert.ok(signalMenu.some(item => item.label === "删除信号…" && item.disabled));

  // Right-click "new signal at this bit" keeps the clicked position and byte order.
  assert.equal(run('dbcSignalFits({ dlc: 1, signals: [{ bits: [0] }] }, 1, 1, "intel")'), true);
  assert.equal(run('dbcSignalFits({ dlc: 1, signals: [{ bits: [1] }] }, 1, 1, "intel")'), false);
  context.opsSeen = null;
  run(`dbcUi.files = [{ id: "w", readonly: false }]; dbcUi.fileId = "w"; dbcEdit.newOrder = "motorola";
    dbcCommit = ops => { opsSeen = ops; return true; };`);
  await run('dbcCreateSignalAt({ key: "s:9", signals: [{ name: "Signal_2", bits: [0] }] }, 7, 8)');
  assert.equal(run("opsSeen[0].fields.start"), 7);
  assert.equal(run("opsSeen[0].fields.length"), 8);
  assert.equal(run("opsSeen[0].fields.byte_order"), "motorola");
  assert.equal(run("opsSeen[0].fields.name"), "Signal_3");

  // With a multiplexing view selected, clicks and menus act on the signal the bit grid shows.
  const muxMessage = `{ key: "s:768", dlc: 8, signals: [
    { name: "Sel", bits: [0, 1, 2, 3, 4, 5, 6, 7], byte_order: "intel", mux: { role: "multiplexor" } },
    { name: "Ana", bits: [8, 9], byte_order: "intel", mux: { role: "multiplexed", value: 0 } },
    { name: "Cur", bits: [8, 9], byte_order: "intel", mux: { role: "multiplexed", value: 1 } },
    { name: "Hid", bits: [24], byte_order: "intel", mux: { role: "multiplexed", value: 2 } }] }`;
  run(`dbcEdit.muxView = ""`);
  assert.equal(run(`dbcSignalAtBit(${muxMessage}, 8).name`), "Ana");
  run(`dbcEdit.muxView = "1"`);
  assert.equal(run(`dbcSignalAtBit(${muxMessage}, 8).name`), "Cur");
  assert.equal(run(`dbcSignalAtBit(${muxMessage}, 20)`), null);
  run(`dbcEdit.muxView = "0"`);
  assert.equal(run(`dbcSignalAtBit(${muxMessage}, 8).name`), "Ana");
  // A bit that only another branch uses is not offered as a free place for a new signal.
  run(`dbcUi.files = [{ id: "w", readonly: false }]; dbcUi.fileId = "w"; dbcEdit.muxView = "1"`);
  assert.equal(run(`dbcSignalAtBit(${muxMessage}, 24)`), null);
  assert.equal(run(`dbcBitMenuItems(${muxMessage}, 24, null)`)[0].disabled, true);
  assert.equal(run(`dbcBitMenuItems(${muxMessage}, 20, null)`)[0].disabled, false);
  const outside = run(`dbcBitMenuItems({ dlc: 1, signals: [] }, 9, null)`);
  assert.equal(outside[0].disabled, true);
  run(`dbcEdit.muxView = ""`);

  // Attribute edits send the enum index, text for numbers (exact for 64-bit values) and null to clear.
  context.opsSeen = null;
  run(`dbcCommit = ops => { opsSeen = ops; return true; };`);
  await run(`dbcCommitAttribute({ dataset: { attr: "Mode", attrScope: "BO_" }, tagName: "SELECT", value: "1" }, { key: "s:7" })`);
  assert.deepEqual(JSON.parse(JSON.stringify(run("opsSeen"))),
    [{ op: "set_attr_value", scope: "BO_", name: "Mode", value: 1, message: "s:7" }]);
  await run(`dbcCommitAttribute({ dataset: { attr: "Mode", attrScope: "BO_" }, tagName: "SELECT", value: "" }, { key: "s:7" })`);
  assert.equal(run("opsSeen[0].value"), null);
  await run(`dbcCommitAttribute({ dataset: { attr: "Big", attrScope: "" }, tagName: "INPUT", value: " 9223372036854775807 " }, null)`);
  assert.equal(run("opsSeen[0].value"), "9223372036854775807");
  assert.equal(run("opsSeen[0].scope"), "");
  await run(`dbcCommitAttribute({ dataset: { attr: "Role", attrScope: "BU_", attrNode: "ECU" }, tagName: "INPUT", value: "master" }, null)`);
  assert.equal(run("opsSeen[0].node"), "ECU");

  // Renaming a signal group sends new_name and keeps addressing the old one.
  context.opsSeen = null;
  run(`dbcPrompt = async () => ({ name: "Split", repetitions: 2, signals: "A, B，C" });`);
  await run(`dbcEditSignalGroup({ key: "s:7", signal_groups: [{ name: "Fork", repetitions: 1, signals: ["A"] }] }, "Fork")`);
  assert.deepEqual(JSON.parse(JSON.stringify(run("opsSeen[0]"))),
    { op: "set_signal_group", message: "s:7", name: "Fork", repetitions: 2, signals: ["A", "B", "C"], new_name: "Split" });
  await run(`dbcEditSignalGroup({ key: "s:7", signal_groups: [] }, null)`);
  assert.equal(run("opsSeen[0].name"), "Split");
  assert.equal(run("'new_name' in opsSeen[0]"), false);

  // Once an input changes, neither the old payload nor its late reply is usable.
  run(`state.api = { dbc_encode: (...args) => new Promise(resolve => pending.push({ args, resolve })) };
    dbcUi.source = "main"; dbcUi.live.main = { connection: { connected: true, mode: "pcan" } };
    dbcSend.key = "s:1"; dbcSend.values = { Speed: 10 }; dbcSend.payload = { data: "old" }; dbcSendEncode();`);
  assert.equal(run("dbcSend.payload"), null);
  assert.equal(node('#dbcSender [data-act="send-once"]').disabled, true);
  const first = timers.get(nextTimer)();
  run("dbcSend.values.Speed = 20; dbcSendEncode()");
  pending[0].resolve({ ok: true, data: "10", id: 1, dlc: 1 });
  await first;
  assert.equal(run("dbcSend.payload"), null);
  const second = timers.get(nextTimer)();
  assert.equal(pending[1].args[2].Speed, 20);
  pending[1].resolve({ ok: true, data: "20", id: 1, dlc: 1 });
  await second;
  assert.equal(run("dbcSend.payload.data"), "20");
  assert.equal(node('#dbcSender [data-act="send-once"]').disabled, false);
  run("dbcSendEncode()");
  const third = timers.get(nextTimer)();
  run('dbcUi.source = "vehicle"');
  pending[2].resolve({ ok: true, data: "wrong-bus" });
  await third;
  assert.equal(run("dbcSend.payload"), null);

  // Stopping a running task does not depend on a currently valid payload.
  context.stopped = null;
  run(`schedulePoll = () => {}; toast = () => {}; dbcSend.payload = null;
    state.api.configure_monitor_periodic = async (...args) => { stopped = args; return { ok: true }; };`);
  await run("dbcSendStopPeriodic()");
  assert.equal(context.stopped[0], "vehicle");
  assert.equal(context.stopped[3], false);
  assert.equal(context.stopped[4], true);

  // Switching source clears the prior audit immediately and ignores its reply.
  pending.length = 0;
  run(`state.api.dbc_audit = (...args) => new Promise(resolve => pending.push({ args, resolve }));
    dbcUi.source = "main"; dbcAudit.source = "main"; dbcAudit.running = false;
    dbcAudit.report = { frames: 99 }; dbcUi.source = "vehicle"; renderDbcAudit();`);
  assert.equal(run("dbcAudit.report"), null);
  assert.equal(pending[0].args[0], "vehicle");
  run('dbcUi.source = "main"; renderDbcAudit()');
  pending[0].resolve({ ok: true, running: true, report: { frames: 123 } });
  await Promise.resolve();
  assert.equal(run("dbcAudit.report"), null);
  pending[1].resolve({ ok: true, running: false, report: null });
  await Promise.resolve();

  // A late CAN C detail must never replace the selection in CAN A.
  pending.length = 0;
  run(`dbcUi.ready = true; dbcUi.files = [{ id: "A", revision: 1 }, { id: "C", revision: 1 }];
    dbcUi.details.clear(); dbcUi.fileId = "A"; dbcUi.filter = "CDC"; dbcEdit.muxView = "1";
    state.api.dbc_state = async () => ({});
    state.api.dbc_detail = id => new Promise(resolve => pending.push({ id, resolve }));`);
  const selectC = run('dbcSelectFile("C")');
  assert.equal(run("dbcUi.filter"), "");
  assert.equal(run("dbcEdit.muxView"), "");
  assert.equal(run("dbcUi.selected.key"), null);
  assert.equal(node("#dbcSearch").value, "");
  const selectA = run('dbcSelectFile("A")');
  const detail = key => ({ ok: true, file: { revision: 1 }, issues: [],
    catalog: { nodes: [], messages: [{ key, signals: [] }] } });
  pending[1].resolve(detail("s:1"));
  await selectA;
  pending[0].resolve(detail("s:3"));
  await selectC;
  assert.equal(run("dbcUi.fileId"), "A");
  assert.equal(run("dbcUi.selected.key"), "s:1");

  // Older snapshots cannot restore a closed file or an earlier binding.
  pending.length = 0;
  run(`dbcUi.revision = 1; state.api.dbc_state = () => new Promise(resolve => pending.push({ resolve }));`);
  const oldState = run("dbcRefreshState()"), newState = run("dbcRefreshState()");
  pending[1].resolve({ ok: true, revision: 5, files: [{ id: "A", revision: 1, bus: "none" }] });
  await newState;
  pending[0].resolve({ ok: true, revision: 4, files: [{ id: "C", revision: 1, bus: "can1" }] });
  await oldState;
  assert.equal(run("dbcUi.revision"), 5);
  assert.equal(run("dbcUi.files[0].id"), "A");

  // Vector's independent signals are a container, never an overlapping CAN frame.
  run(`dbcUi.files = [{ id: "A", readonly: true }]; dbcUi.fileId = "A";
    dbcUi.selected = { key: "v", signal: null };
    dbcUi.details.set("A", { issues: [], catalog: { nodes: [], messages: [{ key: "v", virtual: true,
      signals: [{ name: "Unused", mux: { role: "none" }, start: 0, length: 8, byte_order: "intel", factor: 1, offset: 0, unit: "" }] }] } });`);
  const unusedHtml = run("dbcMessageEditorHtml(dbcCatalog())");
  assert.match(unusedHtml, /未使用信号/);
  assert.match(unusedHtml, /Unused/);
  assert.doesNotMatch(unusedHtml, /dbc-bitgrid|data-form="message"/);
  assert.equal(run('dbcValueCells({ state: "unassigned" }, false).value'), "未分配报文");
  assert.equal((run('dbcSignalRow(dbcMessageEntry(), { name: "Unused", state: "unassigned" }, null)').match(/ disabled/g) || []).length, 2);

  // Unbound CAN A must not display a different file's CAN1 send form.
  run(`dbcUi.source = "main"; dbcUi.live.main = { revision: 5, messages: [{ file: "other", key: "s:1" }] };
    state.api.dbc_form = () => { throw new Error("wrong file"); }; dbcSend.payload = { data: "old" };
    renderDbcSender();`);
  assert.equal(run("dbcSend.payload"), null);
  assert.match(node("#dbcSender").innerHTML, /当前文件.*没有可用报文/);
  run(`dbcUi.files[0].bus = "none"; dbcUi.live.main.messages[0].file = "A";
    renderDbcSender();`);
  assert.match(node("#dbcSender").innerHTML, /当前文件.*没有可用报文/);

  // A form response also belongs to its selected file, even when IDs are equal.
  pending.length = 0;
  run(`dbcSend.key = "s:1"; dbcSend.form = null;
    state.api.dbc_form = () => new Promise(resolve => pending.push({ resolve }));`);
  const formRequest = run("dbcSendLoadForm()");
  run('dbcResetFileView("C")');
  pending[0].resolve({ ok: true, message: { signals: [] }, values: {} });
  await formRequest;
  assert.equal(run("dbcSend.form"), null);
  assert.equal(run("dbcSend.payload"), null);
  console.log("dbc interaction smoke ok");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
