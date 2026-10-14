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
