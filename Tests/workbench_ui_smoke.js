/* Exercise the real zero-build modules with a minimal DOM and a durable API stub. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const source = name => fs.readFileSync(path.join(root, "canhost/web/js", name), "utf8");
const row = { uid: "saved", name: "发给台架", id: "0x290", extended: false,
  data: "01 02", cycle_ms: 200 };

function element() {
  return { value: "", dataset: {}, classList: { toggle() {} }, closest() { return null; },
    textContent: "", innerHTML: "" };
}

function select(channels) {
  const node = element();
  node.options = [];
  Object.defineProperty(node, "innerHTML", {
    set(html) {
      this.options = [...html.matchAll(/<option value="([^"]*)"/g)]
        .map(match => ({ value: match[1] }));
      this.value = this.options[0]?.value || "";
    },
  });
  node.options = channels.map(value => ({ value }));
  node.value = channels[0] || "";
  return node;
}

function createApp(saved, channels, legacy = new Map()) {
  const nodes = {
    "#can1ConnectChannel": select(channels),
    "#canbConnectChannel": select(channels),
    "#canbConnectBitrate": select(["500000", "250000"]),
    "#connectionSettingsMessage": element(),
    "#monitorTxRows": element(),
    "#monitorTxGate": element(),
  };
  const document = {
    documentElement: { dataset: {} },
    querySelector: selector => nodes[selector] || null,
    querySelectorAll: () => [],
    addEventListener() {},
  };
  const bridge = {
    workbench_preferences: async () => structuredClone(saved),
    set_connection_preferences: async value => {
      saved.connection = structuredClone(value); return { ok: true };
    },
    set_monitor_tx_rows: async value => {
      saved.monitor_tx_rows = structuredClone(value); return { ok: true };
    },
  };
  const context = { document, localStorage: {
    getItem: key => legacy.get(key) ?? null,
    setItem: (key, value) => legacy.set(key, value),
  }, setTimeout, structuredClone, bridge, console };
  context.window = context;
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(source("core.js"), context);
  vm.runInContext(source("monitor.js"), context);
  vm.runInContext(source("vehicle.js"), context);
  vm.runInContext(source("bms.js"), context);
  vm.runInContext("state.api = bridge; state.bootstrap = {pcan_scan: {automatic: true}}", context);
  return { context, nodes };
}

async function main() {
  const display = createApp({}, []);
  const view = (hv, type = 0) => display.context.legacyChargeEndView(hv, type);
  const complete = {age: 0.5, legacy_end_version: 1, legacy_end_state: 4,
    legacy_end_name: "充电完成", legacy_end_hold_s: 30};
  assert.equal(view(complete).complete, true);
  assert.match(view(complete).text, /释放 PA0/);
  assert.equal(view({...complete, age: 1.501}).text, "等待数据");
  assert.equal(view({...complete, age: null}).complete, false);
  assert.equal(view({...complete, legacy_end_version: 0}).text, "固件未提供");
  assert.equal(view({...complete, legacy_end_version: 2}).text, "状态版本未知");
  assert.equal(view(complete, 1).complete, false);
  assert.equal(view(complete, null).text, "等待数据");
  assert.match(view({...complete, legacy_end_state: 2, legacy_end_name: "满充确认中",
    legacy_end_hold_s: 29}).text, new RegExp("29/30 s"));
  for (const code of [5, 6]) assert.equal(view({...complete, legacy_end_state: code}).bad, true);
  const saved = { connection: { version: 3, can1Channel: "PCAN_USBBUS2",
    canbChannel: "PCAN_USBBUS1" }, monitor_tx_rows: [row] };
  const channels = ["PCAN_USBBUS1", "PCAN_USBBUS2"];
  const first = createApp(saved, channels);
  await vm.runInContext("restoreWorkbenchPreferences()", first.context);
  assert.equal(first.nodes["#can1ConnectChannel"].value, "PCAN_USBBUS2");
  assert.equal(first.nodes["#canbConnectChannel"].value, "PCAN_USBBUS1");
  assert.match(first.nodes["#monitorTxRows"].innerHTML, /发给台架/);
  await vm.runInContext('monitorUi.txRows[0].data = "AA BB"; persistMonitorTxRows()', first.context);
  first.nodes["#can1ConnectChannel"].value = "PCAN_USBBUS1";
  first.nodes["#canbConnectChannel"].value = "PCAN_USBBUS2";
  await vm.runInContext("persistConnectionPreferences()", first.context);

  const restarted = createApp(saved, channels);
  await vm.runInContext("restoreWorkbenchPreferences()", restarted.context);
  assert.equal(restarted.nodes["#can1ConnectChannel"].value, "PCAN_USBBUS1");
  assert.equal(restarted.nodes["#canbConnectChannel"].value, "PCAN_USBBUS2");
  assert.match(restarted.nodes["#monitorTxRows"].innerHTML, /AA BB/);

  const connected = { connection: null, monitor_tx_rows: [] };
  const live = createApp(connected, channels);
  live.context.bridge.connect_can = async () => ({ ok: true });
  live.context.bridge.connect_vehicle = async config => {
    assert.equal(config.bitrate, 250000); return { ok: true };
  };
  vm.runInContext("toast = () => {}; setBusConnecting = () => {}; resetChargeTiming = () => {}; poll = async () => {}", live.context);
  live.nodes["#can1ConnectChannel"].value = "PCAN_USBBUS2";
  live.nodes["#canbConnectChannel"].value = "PCAN_USBBUS1";
  await vm.runInContext("toggleMainDockConnection()", live.context);
  assert.equal(connected.connection.can1Channel, "PCAN_USBBUS2");
  live.nodes["#canbConnectBitrate"].value = "250000";
  await vm.runInContext("connectVehicle()", live.context);
  assert.equal(connected.connection.canbBitrate, 250000);
  assert.equal(connected.connection.canbChannel, "PCAN_USBBUS1");
  const legacyRestart = createApp(connected, channels);
  await vm.runInContext("restoreWorkbenchPreferences()", legacyRestart.context);
  assert.equal(legacyRestart.nodes["#canbConnectBitrate"].value, "250000");

  const partlyAvailable = { connection: { version: 3, can1Channel: "PCAN_USBBUS1",
    canbChannel: "PCAN_USBBUS2" }, monitor_tx_rows: [] };
  const oneChannel = createApp(partlyAvailable, ["PCAN_USBBUS1"]);
  await vm.runInContext("restoreWorkbenchPreferences()", oneChannel.context);
  oneChannel.context.bridge.connect_can = async () => ({ ok: true });
  vm.runInContext("toast = () => {}; setBusConnecting = () => {}; resetChargeTiming = () => {}; poll = async () => {}", oneChannel.context);
  await vm.runInContext("toggleMainDockConnection()", oneChannel.context);
  assert.equal(partlyAvailable.connection.canbChannel, "PCAN_USBBUS2");
  vm.runInContext(`populatePcanSelect("#canbConnectChannel", {
    channels: ["PCAN_USBBUS1", "PCAN_USBBUS2"]});`, oneChannel.context);
  assert.equal(oneChannel.nodes["#canbConnectChannel"].value, "PCAN_USBBUS2");

  const migrated = { connection: null, monitor_tx_rows: null };
  const legacy = new Map([
    ["canHostConnectionPreferences", JSON.stringify({ version: 3,
      can1Channel: "PCAN_USBBUS2", canbChannel: "PCAN_USBBUS1" })],
    ["canHostMonitorTransmitRowsV1", JSON.stringify([row])],
  ]);
  const offline = createApp(migrated, [], legacy);
  await vm.runInContext("restoreWorkbenchPreferences()", offline.context);
  assert.equal(migrated.connection.can1Channel, "PCAN_USBBUS2");
  assert.equal(migrated.monitor_tx_rows[0].name, row.name);
  await vm.runInContext("persistConnectionPreferences()", offline.context);
  assert.equal(migrated.connection.can1Channel, "PCAN_USBBUS2");
  vm.runInContext(`populatePcanSelect("#can1ConnectChannel", {
    channels: ["PCAN_USBBUS1", "PCAN_USBBUS2"]});`, offline.context);
  assert.equal(offline.nodes["#can1ConnectChannel"].value, "PCAN_USBBUS2");
}

main().catch(error => { console.error(error); process.exitCode = 1; });
