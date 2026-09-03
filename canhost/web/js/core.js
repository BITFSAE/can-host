/* 框架层：状态、轮询、导航、确认弹窗、状态栏与快捷栏、CAN 监视器、回放。
 * 页面模块（bms/vehicle/fan/bench/ivt）在其后加载，函数在调用时解析。 */

var $ = (selector) => document.querySelector(selector);
var $$ = (selector) => [...document.querySelectorAll(selector)];
window.$ = $;
window.$$ = $$;

var state = {
  api: null,
  bootstrap: null,
  snapshot: null,
  toolSnapshots: { bench: null, ivt: null },
  vehicleSnapshot: null,
  telemetrySnapshot: null,
  quickSnapshot: null,
  page: "overview",
  cellMode: "voltage",
  frameKind: "all",
  frameSource: "main",
  framePaused: false,
  pausedFrames: [],
  pollTimer: null,
  pollInFlight: false,
  pendingCommand: null,
  pendingIvtAction: null,
  pendingFanCommand: null,
  pendingFanAction: null,
  inputsInitialized: { thresholds: false, switches: false, charge: false },
  dirty: { thresholds: false, switches: false, charge: false, direction: false, chargerType: false, fan: false },
  recording: false,
  onlyActiveAlarms: false,
  uiScale: 1,
  lastZoomWheelAt: 0,
  chargeTiming: { active: false, elapsedMs: 0, lastTickMs: null, averageCurrentA: null, currentSumA: 0, currentSamples: 0, connectionKey: null },
  saveWatch: null,
  cellRefs: null,
  frameRowPool: [],
};
window.state = state;

const UI_SCALE_STEPS = [0.8, 0.9, 1, 1.1, 1.2, 1.3];
const UI_SCALE_DEFAULT = 1.1;

const PAGE_ORDER = ["overview", "cells", "alarms", "control", "vehicle", "fan", "frames", "bench", "ivt", "telemetry"];
const TOOL_PAGES = ["bench", "ivt"];
const DATA_FRESH_MAX_S = 1.5;
const SLOW_DATA_FRESH_MAX_S = 2.5;
const CONNECTION_PREFS_KEY = "canHostConnectionPreferences";

function fmt(value, digits = 1, fallback = "—") {
  return value === null || value === undefined || Number.isNaN(value) ? fallback : Number(value).toFixed(digits);
}
function isFresh(age, limit = DATA_FRESH_MAX_S) { return age != null && age <= limit; }
function text(id, value) { const node = $(id); if (node) node.textContent = value; }
function setClass(idOrNode, className, enabled) {
  const node = typeof idOrNode === "string" ? $(idOrNode) : idOrNode;
  if (node) node.classList.toggle(className, !!enabled);
}

function toast(message, error = false) {
  const node = document.createElement("div");
  node.className = `toast${error ? " error" : ""}`;
  node.textContent = message;
  $("#toastStack").append(node);
  setTimeout(() => node.remove(), 3600);
}

function escapeHtml(value) {
  return String(value ?? "—").replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
  }[character]));
}

function applyUiScale(scale, announce = true) {
  const nearest = UI_SCALE_STEPS.reduce((best, value) =>
    Math.abs(value - scale) < Math.abs(best - scale) ? value : best
  );
  state.uiScale = nearest;
  // CSS zoom is supported by the Edge WebView2 engine and scales the complete
  // interface while recalculating its available layout width and height.
  document.documentElement.style.zoom = String(nearest);
  document.documentElement.style.setProperty("--app-height", `${100 / nearest}vh`);
  try { localStorage.setItem("canHostUiScale", String(nearest)); } catch { /* storage may be disabled */ }
  if (state.page === "overview") setTimeout(drawTrend, 0);
  if (state.page === "vehicle") setTimeout(drawVehicleTrend, 0);
  if (announce) toast(`界面缩放 ${Math.round(nearest * 100)}%`);
}

function restoreUiScale() {
  let saved = UI_SCALE_DEFAULT;
  try { saved = Number(localStorage.getItem("canHostUiScale")) || UI_SCALE_DEFAULT; } catch { /* storage may be disabled */ }
  applyUiScale(saved, false);
}

function stepUiScale(direction) {
  const index = UI_SCALE_STEPS.indexOf(state.uiScale);
  const next = Math.max(0, Math.min(UI_SCALE_STEPS.length - 1, index + direction));
  applyUiScale(UI_SCALE_STEPS[next]);
}

async function waitForApi() {
  // pywebview.api is an empty object before its JS methods have been created.
  // Waiting only for the object itself caused bootstrap() to be called too
  // early, leaving the UI in "waiting" forever. Require a real API method.
  const apiReady = () => typeof window.pywebview?.api?.bootstrap === "function";
  if (apiReady()) return window.pywebview.api;
  return new Promise(resolve => {
    let done = false;
    const check = () => {
      if (apiReady() && !done) {
        done = true;
        resolve(window.pywebview.api);
        return true;
      }
      return false;
    };
    if (check()) return;
    window.addEventListener("pywebviewready", () => check(), { once: true });
    // Keep waiting: in the desktop app pywebview injects its API shortly after
    // the page loads. A timeout here would silently leave the UI without a
    // backend and make Connect appear to do nothing.
    const timer = setInterval(() => {
      if (check()) clearInterval(timer);
    }, 100);
  });
}

async function init() {
  restoreUiScale();
  bindNavigation();
  bindCoreControls();
  bindBmsControls();
  bindVehicleControls();
  bindFanControls();
  bindBenchControls();
  bindIvtControls();
  bindTelemetryControls();
  buildAlarmMatrix();
  buildVehicleStatics();
  try {
    state.api = await waitForApi();
    state.bootstrap = await state.api.bootstrap();
    text("#appVersion", `v${state.bootstrap.version || "—"}`);
    text("#appVersionDate", state.bootstrap.version_date || "—");
    $("#simulationBusButton")?.classList.toggle(
      "hidden",
      state.bootstrap.simulation_enabled !== true && state.bootstrap.vehicle_simulation_enabled !== true
    );
    populateConnectionOptions();
    populateToolChannelOptions();
    populateVehicleOptions();
    restoreConnectionPreferences();
    buildSwitchList();
    buildSwitchStatusList();
    await poll();
    if (typeof initUpdater === "function") initUpdater();
  } catch (error) {
    toast(`应用后端未就绪：${error}`, true);
    const fallback = { simulation_enabled: false, channels: ["PCAN_USBBUS1"], profiles: [
      { key: "can1", name: "CAN1 · F405 主控 / 从控 / 工具", bitrate: 500000 },
      { key: "canb", name: "CANB · ECU / Chroma · 500 kbit/s", bitrate: 500000 },
      { key: "canb_legacy", name: "CANB · Legacy · 250 kbit/s", bitrate: 250000 },
    ]};
    populateConnectionOptions(fallback);
    populateToolChannelOptions(fallback);
    restoreConnectionPreferences();
  }
}

function bindNavigation() {
  $("#nav").addEventListener("click", event => {
    const button = event.target.closest(".nav-item");
    if (button) showPage(button.dataset.page);
  });
  $$('[data-goto]').forEach(button => button.addEventListener("click", () => showPage(button.dataset.goto)));
}

function showPage(page) {
  if (!PAGE_ORDER.includes(page)) return;
  state.page = page;
  const isTool = TOOL_PAGES.includes(page);
  if (isTool) $(".nav-tools")?.setAttribute("open", "");
  $$(".nav-item").forEach(node => node.classList.toggle("active", node.dataset.page === page));
  $$(".page").forEach(node => node.classList.toggle("active", node.id === `page-${page}`));
  document.body.classList.toggle("is-tool-page", isTool);
  // Every page owns a different information depth. Keeping the previous page's
  // scroll offset can make a short page appear blank after navigation.
  $("#main").scrollTop = 0;
  if (state.snapshot) render();
  schedulePoll(0);
}

function bindCoreControls() {
  $("#can1BusButton")?.addEventListener("click", () => toggleMainDockConnection("can1"));
  $("#canbBmsBusButton")?.addEventListener("click", () => toggleMainDockConnection("canb_bms"));
  $("#canbVehicleBusButton")?.addEventListener("click", toggleVehicleDockConnection);
  $("#simulationBusButton")?.addEventListener("click", toggleSimulationChannels);
  $("#connectionSettingsButton")?.addEventListener("click", () => $("#connectDialog")?.showModal());
  $("#saveConnectionSettings")?.addEventListener("click", saveConnectionPreferences);
  $("#frameType").addEventListener("click", event => {
    const button = event.target.closest("button"); if (!button) return;
    state.frameKind = button.dataset.kind;
    $$("#frameType button").forEach(node => node.classList.toggle("active", node === button));
    renderFrames();
  });
  $("#frameSource").addEventListener("click", event => {
    const button = event.target.closest("button"); if (!button) return;
    state.frameSource = button.dataset.source;
    state.framePaused = false;
    $("#pauseFrames").checked = false;
    $$("#frameSource button").forEach(node => node.classList.toggle("active", node === button));
    schedulePoll(0);
    renderFrames();
  });
  $("#frameSearch").addEventListener("input", renderFrames);
  $("#pauseFrames").addEventListener("change", event => {
    state.framePaused = event.target.checked;
    if (state.framePaused) state.pausedFrames = activeFrameList();
    renderFrames();
  });
  $("#confirmCheck").addEventListener("change", event => $("#doConfirm").disabled = !event.target.checked);
  $("#confirmDialog").addEventListener("close", () => {
    state.pendingCommand = null;
    state.pendingIvtAction = null;
    state.pendingFanCommand = null;
    state.pendingFanAction = null;
    $("#confirmCheck").checked = false;
    $("#doConfirm").disabled = true;
    setConfirmModeBadge("待确认");
  });
  $("#doConfirm").addEventListener("click", sendPendingCommand);
  $("#recordButton").addEventListener("click", toggleRecording);
  $("#replayButton").addEventListener("click", openReplay);
  $("#replayPlay").addEventListener("click", toggleReplay);
  $("#replaySpeed").addEventListener("change", event => state.api?.replay_control("speed", +event.target.value));
  $("#replaySeek").addEventListener("change", event => {
    const replay = state.snapshot?.connection?.replay; if (!replay) return;
    state.api?.replay_control("seek", replay.duration * (+event.target.value / 1000));
  });
  document.addEventListener("visibilitychange", () => schedulePoll(document.hidden ? 1000 : 0));
  window.addEventListener("resize", () => {
    if (state.page === "overview") drawTrend();
    if (state.page === "vehicle") drawVehicleTrend();
  });
  window.addEventListener("keydown", event => {
    if ((event.ctrlKey || event.metaKey) && !event.altKey) {
      const zoomIn = event.key === "+" || event.key === "=" || event.code === "Equal" || event.code === "NumpadAdd";
      const zoomOut = event.key === "-" || event.code === "Minus" || event.code === "NumpadSubtract";
      const zoomReset = event.key === "0" || event.code === "Digit0" || event.code === "Numpad0";
      if (zoomIn || zoomOut || zoomReset) {
        event.preventDefault();
        event.stopImmediatePropagation();
        if (zoomReset) applyUiScale(UI_SCALE_DEFAULT);
        else stepUiScale(zoomIn ? 1 : -1);
        return;
      }
    }
    if (!event.altKey || event.ctrlKey || event.shiftKey) return;
    const index = Number(event.key) - 1;
    if (Number.isInteger(index) && PAGE_ORDER[index]) {
      event.preventDefault();
      showPage(PAGE_ORDER[index]);
    }
  }, true);
  window.addEventListener("wheel", event => {
    if (!event.ctrlKey || Math.abs(event.deltaY) < 1) return;
    event.preventDefault();
    const now = performance.now();
    if (now - state.lastZoomWheelAt < 140) return;
    state.lastZoomWheelAt = now;
    stepUiScale(event.deltaY < 0 ? 1 : -1);
  }, { passive: false, capture: true });
}

function populateConnectionOptions(fallback) {
  const data = state.bootstrap || fallback;
  if (!data) return;
  // 模拟入口只在底栏开发按钮出现，连接弹窗只配置真实 PCAN，避免同一模式重复入口。
  const profiles = [...(data.profiles || [])].filter(item => ["canb", "canb_legacy"].includes(item.key));
  $("#connectProfile").innerHTML = profiles.map(item =>
    `<option value="${item.key}" data-mode="${item.mode || "pcan"}" data-bitrate="${item.bitrate}">${item.name}</option>`
  ).join("");
  $("#connectChannel").innerHTML = (data.channels || ["PCAN_USBBUS1"]).map(item => `<option>${item}</option>`).join("");
}

function populateToolChannelOptions(fallback) {
  const data = state.bootstrap || fallback;
  if (!data?.channels) return;
  const options = data.channels.map(item => `<option>${item}</option>`).join("");
  ["#benchChannelSelect", "#ivtChannelSelect", "#vehicleConnectChannel"]
    .forEach(id => { if ($(id)) $(id).innerHTML = options; });
}

function restoreConnectionPreferences() {
  let prefs = {};
  try { prefs = JSON.parse(localStorage.getItem(CONNECTION_PREFS_KEY) || "{}"); } catch { /* 使用默认项 */ }
  const apply = (selector, value) => {
    const node = $(selector);
    if (node && [...node.options].some(option => option.value === String(value))) node.value = String(value);
  };
  apply("#connectChannel", prefs.mainChannel || "PCAN_USBBUS1");
  apply("#connectProfile", prefs.bmsCanbProfile || "canb");
  apply("#vehicleConnectChannel", prefs.vehicleChannel || "PCAN_USBBUS1");
  apply("#vehicleConnectBitrate", prefs.vehicleBitrate || "500000");
}

function saveConnectionPreferences() {
  const prefs = {
    mainChannel: $("#connectChannel")?.value || "PCAN_USBBUS1",
    bmsCanbProfile: $("#connectProfile")?.value || "canb",
    vehicleChannel: $("#vehicleConnectChannel")?.value || "PCAN_USBBUS1",
    vehicleBitrate: $("#vehicleConnectBitrate")?.value || "500000",
  };
  try { localStorage.setItem(CONNECTION_PREFS_KEY, JSON.stringify(prefs)); } catch { /* 本次运行仍保留选择 */ }
  $("#connectDialog")?.close();
  toast("CAN 连接设置已保存");
}

function mainConnectionRole() {
  const main = (state.snapshot || {}).connection || {};
  if (main.connected !== true || main.mode === "simulation") return null;
  if (main.connected === true && ["canb", "canb_legacy"].includes(main.bus_profile)) return "canb_bms";
  return "can1";
}

function vehicleConnectionState() {
  return state.quickSnapshot?.vehicle?.connection || state.vehicleSnapshot?.connection || {};
}

function roleButton(role) {
  return {
    can1: $("#can1BusButton"),
    canb_bms: $("#canbBmsBusButton"),
    canb_vehicle: $("#canbVehicleBusButton"),
    simulation: $("#simulationBusButton"),
  }[role];
}

async function toggleMainDockConnection(role) {
  if (!state.api) return toast("应用后端未就绪", true);
  if (roleButton(role)?.classList.contains("connecting")) return;
  if (mainConnectionRole() === role) return disconnectCan();
  const profileOption = $("#connectProfile")?.selectedOptions?.[0];
  const profile = role === "can1" ? "can1" : (profileOption?.value || "canb");
  const bitrate = role === "can1" ? 500000 : Number(profileOption?.dataset?.bitrate || 500000);
  const vehicle = vehicleConnectionState();
  if (vehicle.connected === true && vehicle.mode === "simulation") {
    await state.api.disconnect_vehicle();
    state.vehicleSnapshot = null;
  }
  resetChargeTiming();
  setBusConnecting(role, true);
  let result;
  try {
    result = await state.api.connect_can({
      mode: "pcan", bus_profile: profile,
      channel: $("#connectChannel")?.value || "PCAN_USBBUS1", bitrate,
    });
  } catch (error) {
    return toast(`连接失败：${error}`, true);
  } finally {
    setBusConnecting(role, false);
  }
  if (!result?.ok) return toast(result?.error || `${role === "can1" ? "CAN1" : "BMS CANB"} 连接失败`, true);
  toast(`${role === "can1" ? "CAN1" : "BMS CANB"} 已连接`);
  await poll();
}

async function toggleVehicleDockConnection() {
  if (!state.api) return toast("应用后端未就绪", true);
  if (roleButton("canb_vehicle")?.classList.contains("connecting")) return;
  const vehicle = vehicleConnectionState();
  if (vehicle.connected === true && vehicle.mode !== "simulation") return disconnectVehicle();
  await connectVehicle();
}

function simulationServices() {
  const services = [];
  if (state.bootstrap?.simulation_enabled === true) services.push("main");
  if (state.bootstrap?.vehicle_simulation_enabled === true) services.push("vehicle");
  return services;
}

function simulationState() {
  const services = simulationServices();
  const main = state.snapshot?.connection || {};
  const vehicle = vehicleConnectionState();
  const running = services.filter(service => service === "main"
    ? main.connected === true && main.mode === "simulation"
    : vehicle.connected === true && vehicle.mode === "simulation");
  return { services, running, complete: services.length > 0 && running.length === services.length };
}

async function toggleSimulationChannels() {
  if (!state.api) return toast("应用后端未就绪", true);
  if (roleButton("simulation")?.classList.contains("connecting")) return;
  const sim = simulationState();
  if (!sim.services.length) return toast("当前版本未启用模拟通道", true);
  if (sim.complete) return disconnectSimulationChannels();
  const main = state.snapshot?.connection || {};
  const vehicle = vehicleConnectionState();
  const realConnected = (main.connected === true && main.mode !== "simulation")
    || (vehicle.connected === true && vehicle.mode !== "simulation");
  if (realConnected) return toast("请先断开真实 CAN 连接，再启动模拟通道", true);

  setBusConnecting("simulation", true);
  const started = [];
  try {
    if (sim.services.includes("main") && !sim.running.includes("main")) {
      const result = await state.api.connect_can({ mode: "simulation", bus_profile: "can1", channel: null, bitrate: 500000 });
      if (!result.ok) throw new Error(result.error || "BMS 模拟通道启动失败");
      started.push("main");
    }
    if (sim.services.includes("vehicle") && !sim.running.includes("vehicle")) {
      const result = await state.api.connect_vehicle({ mode: "simulation", channel: null, bitrate: 500000, bus_profile: "canb" });
      if (!result.ok) throw new Error(result.error || "整车模拟通道启动失败");
      started.push("vehicle");
    }
  } catch (error) {
    if (started.includes("main")) await state.api.disconnect_can();
    if (started.includes("vehicle")) await state.api.disconnect_vehicle();
    toast(String(error.message || error), true);
    await poll();
    return;
  } finally {
    setBusConnecting("simulation", false);
  }
  toast("开发模拟通道已启动");
  await poll();
}

async function disconnectSimulationChannels() {
  const main = state.snapshot?.connection || {};
  const vehicle = vehicleConnectionState();
  if (main.connected === true && main.mode === "simulation") await state.api.disconnect_can();
  if (vehicle.connected === true && vehicle.mode === "simulation") await state.api.disconnect_vehicle();
  state.vehicleSnapshot = null;
  toast("开发模拟通道已停止");
  await poll();
}

function setBusConnecting(role, active) {
  const node = roleButton(role);
  if (node) {
    node.classList.toggle("connecting", active);
    node.setAttribute("aria-busy", String(active));
  }
}

function updateBusButtonsEnabled() {
  const mainRole = mainConnectionRole();
  const vehicle = vehicleConnectionState();
  const sim = simulationState();
  const states = {
    can1: mainRole === "can1",
    canb_bms: mainRole === "canb_bms",
    canb_vehicle: vehicle.connected === true && vehicle.mode !== "simulation",
    simulation: sim.complete,
  };
  Object.entries(states).forEach(([role, active]) => {
    const button = roleButton(role);
    if (!button) return;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  const simulationButton = roleButton("simulation");
  simulationButton?.classList.toggle("partial", sim.running.length > 0 && !sim.complete);
  const titles = {
    can1: states.can1 ? "CAN1 已连接，点击断开" : "按保存设置直接连接 CAN1",
    canb_bms: states.canb_bms ? "BMS CANB 已连接，点击断开" : "按保存设置直接连接 BMS CANB（只读）",
    canb_vehicle: states.canb_vehicle ? "整车 CANB 已连接，点击断开" : "按保存设置直接连接整车 CANB",
    simulation: sim.complete ? "开发模拟通道已启动，点击停止" : sim.running.length ? "模拟通道未完整启动，点击重试" : "启动开发模拟通道",
  };
  Object.entries(titles).forEach(([role, title]) => roleButton(role)?.setAttribute("title", title));
}

async function disconnectCan() {
  if (!state.api) return;
  const role = mainConnectionRole() || "can1";
  resetChargeTiming();
  setBusConnecting(role, true);
  try { await state.api.disconnect_can(); }
  catch (error) { return toast(`断开失败：${error}`, true); }
  finally { setBusConnecting(role, false); }
  toast("BMS 主连接已断开");
  await poll();
}

function schedulePoll(delay) {
  if (!state.api) return;
  if (state.pollTimer !== null) clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(poll, delay);
}

async function poll() {
  if (!state.api || state.pollInFlight) return;
  if (state.pollTimer !== null) clearTimeout(state.pollTimer);
  state.pollTimer = null;
  state.pollInFlight = true;
  try {
    state.snapshot = await state.api.get_snapshot();
    const page = state.page;
    // Optional/side snapshots must never prevent the main BMS snapshot from
    // being rendered. If one of them fails (for example a transient PyWebView
    // bridge error), keep the main page live and retry on the next poll.
    async function optionalSnapshot(loader, assign) {
      try {
        assign(await loader());
      } catch (error) {
        console.warn("可选数据快照获取失败：", error);
      }
    }
    if (page === "bench" && state.api.get_bench_snapshot) {
      await optionalSnapshot(state.api.get_bench_snapshot, value => { state.toolSnapshots.bench = value; });
    } else if (page === "ivt" && state.api.get_ivt_snapshot) {
      await optionalSnapshot(state.api.get_ivt_snapshot, value => { state.toolSnapshots.ivt = value; });
    }
    if ((page === "vehicle" || page === "fan"
         || (page === "frames" && state.frameSource === "vehicle"))
        && state.api.get_vehicle_snapshot) {
      await optionalSnapshot(state.api.get_vehicle_snapshot, value => { state.vehicleSnapshot = value; });
    }
    const telemetryState = state.telemetrySnapshot?.connection?.state;
    const telemetryActive = !!telemetryState && telemetryState !== "disconnected";
    if ((page === "telemetry" || (!TOOL_PAGES.includes(page) && telemetryActive))
        && state.api.get_telemetry_snapshot) {
      await optionalSnapshot(state.api.get_telemetry_snapshot, value => { state.telemetrySnapshot = value; });
    }
    if (!TOOL_PAGES.includes(page) && state.api.get_quick_snapshot) {
      await optionalSnapshot(state.api.get_quick_snapshot, value => { state.quickSnapshot = value; });
    }
    if (state.snapshot) {
      renderChargeTiming(state.snapshot.overview, state.snapshot.connection, state.snapshot.fault || {});
    }
    render();
  } catch (error) {
    console.error(error);
  } finally {
    state.pollInFlight = false;
    const detailedPage = state.page !== "overview";
    schedulePoll(document.hidden ? 1000 : detailedPage ? 500 : 250);
  }
}

function render() {
  const snap = state.snapshot; if (!snap) return;
  renderConnection(snap.connection);
  renderTelemetryBadge();
  renderCellBadge();
  renderAlarmSummary();
  renderBusDock();
  if (state.page === "overview") {
    renderOverview(); renderModules(); renderControls();
  } else if (state.page === "cells") {
    renderCells();
  } else if (state.page === "alarms") {
    renderAlarms(); renderConfig();
  } else if (state.page === "control") {
    renderConfig(); renderControls();
  } else if (state.page === "vehicle") {
    renderVehicle();
  } else if (state.page === "bench") {
    renderBench();
  } else if (state.page === "ivt") {
    renderIvtConfig();
  } else if (state.page === "fan") {
    renderFan();
  } else if (state.page === "frames") {
    renderReplay();
    if (!state.framePaused) renderFrames();
  } else if (state.page === "telemetry") {
    renderTelemetry();
  }
}

function renderBusDock() {
  const main = (state.snapshot || {}).connection || {};
  const vehicle = state.quickSnapshot?.vehicle?.connection
    || state.vehicleSnapshot?.connection || {};
  updateScopeStrips(main, vehicle || {});
  updateBusButtonsEnabled();
}

function updateScopeStrips(main, vehicle) {
  const mainConnected = main.connected === true;
  const vehicleConnected = vehicle.connected === true;
  const framesStrip = $("#framesScopeStrip");
  if (framesStrip) framesStrip.hidden = mainConnected || vehicleConnected;
  let frameSource = state.frameSource || "main";
  const selectedReady = frameSource === "vehicle" ? vehicleConnected : mainConnected;
  if (!selectedReady && (mainConnected || vehicleConnected)) {
    frameSource = mainConnected ? "main" : "vehicle";
    state.frameSource = frameSource;
    $$("#frameSource button").forEach(button => button.classList.toggle("active", button.dataset.source === frameSource));
  }
  const frameReady = frameSource === "vehicle" ? vehicleConnected : mainConnected;
  $$("#frameSource button").forEach(button => {
    button.disabled = button.dataset.source === "vehicle" ? !vehicleConnected : !mainConnected;
  });

  const can1Writable = mainConnected && main.bus_profile === "can1";
  const controlStrip = $("#controlScopeStrip");
  if (controlStrip) controlStrip.hidden = can1Writable;
  document.body.classList.toggle("control-write-locked", !can1Writable);
  $("#page-control")?.classList.toggle("scope-warning", !can1Writable && (mainConnected || vehicleConnected));

  const fanWritable = vehicleConnected && vehicle.bus_profile !== "canb_legacy";
  const fanStrip = $("#fanScopeStrip");
  if (fanStrip) {
    fanStrip.hidden = fanWritable;
    text("#fanScopeDetail", fanWritable
      ? ""
      : vehicleConnected && vehicle.bus_profile === "canb_legacy"
        ? "当前整车连接为 Legacy 250 kbit/s；风扇命令需要整车 CANB 500 kbit/s"
        : "点击底部“整车 CANB”直接连接后，才能查看和命令风扇");
  }
  document.body.classList.toggle("fan-write-locked", !fanWritable);
}

function renderConnection(connection) {
  state.recording = !!connection.recording;
  text("#recordButton", state.recording ? "■ 停止记录" : "● 记录数据");
  setClass("#recordButton", "active", state.recording);
  const vehicleConnection = vehicleConnectionState();
  const mainRx = Number(connection.rx_count || 0), mainTx = Number(connection.tx_count || 0);
  const vehicleRx = Number(vehicleConnection.rx_count || 0), vehicleTx = Number(vehicleConnection.tx_count || 0);
  text("#rxCount", (mainRx + vehicleRx).toLocaleString());
  text("#txCount", (mainTx + vehicleTx).toLocaleString());
  const traffic = $(".traffic-group");
  if (traffic) traffic.title = `BMS RX ${mainRx.toLocaleString()} / TX ${mainTx.toLocaleString()} · 整车 RX ${vehicleRx.toLocaleString()} / TX ${vehicleTx.toLocaleString()}`;
  const firmware = state.snapshot?.firmware || {};
  const firmwareText = [
    firmware.variant,
    firmware.charger_variant && firmware.charger_variant !== "Runtime" ? firmware.charger_variant : "",
    firmware.git,
    firmware.build_date ? `构建 ${firmware.build_date}` : "",
  ].filter(Boolean).join(" · ") || "等待数据";
  const firmwareNode = $("#firmwareIdentity");
  if (firmwareNode) {
    firmwareNode.textContent = firmwareText;
    firmwareNode.title = firmwareText;
  }
  renderQuickBar();
}

/** 确认弹窗顶部的主/总线/操作模式徽标；危险操作明确用红色，避免只说“确认发送”。 */
function setConfirmModeBadge(label, mode) {
  const badge = $("#confirmModeBadge");
  if (badge) {
    badge.textContent = label;
    badge.className = "confirm-mode-badge" + (mode ? " " + mode : "");
  }
}

/** Always-visible quick values: LV from the vehicle connection, HV/SOC from the
 *  main connection with the vehicle 0x4B0 mirror as fallback. */
function renderQuickBar() {
  const quick = state.quickSnapshot?.vehicle;
  const main = state.snapshot || {};
  const overview = main.overview || {};
  const mainConnection = main.connection || {};
  const mainSummaryFresh = isFresh(mainConnection.summary_age) && overview.voltage_valid !== undefined;
  const vehiclePack = quick?.pack || {};
  const vehiclePackFresh = isFresh(vehiclePack.age);
  const useMainHv = mainSummaryFresh && overview.voltage_valid;
  const useMainSoc = mainSummaryFresh && overview.soc_valid;

  const pdm = quick?.pdm || {};
  const lvFresh = isFresh(pdm.age, 4.0) && !pdm.bus_offline;
  text("#quickLvV", lvFresh ? fmt(pdm.bus_voltage_v, 1) : "等待");
  text("#quickLvI", lvFresh ? fmt(pdm.bus_current_a, 1) : "等待");
  text("#quickLvP", lvFresh ? fmt(pdm.bus_power_w, 0) : "等待");

  const vehicleVoltageFresh = vehiclePackFresh && vehiclePack.voltage_valid;
  const voltageSource = useMainHv ? overview : vehicleVoltageFresh ? vehiclePack : null;
  text("#quickHvV", voltageSource ? fmt(voltageSource.voltage_v, 1) : "等待");
  const useMainCurrent = mainSummaryFresh && overview.current_valid;
  const vehicleCurrentFresh = vehiclePackFresh && vehiclePack.current_valid;
  const currentSource = useMainCurrent ? overview : vehicleCurrentFresh ? vehiclePack : null;
  text("#quickHvI", currentSource ? fmt(currentSource.current_a, 1) : "等待");
  const vehicleSocFresh = vehiclePackFresh && vehiclePack.soc_valid;
  const socSource = useMainSoc ? overview : vehicleSocFresh ? vehiclePack : null;
  text("#quickSoc", socSource ? fmt(socSource.soc_pct, 0) : "等待");

  const sop = quick?.sop || {};
  const sopFresh = isFresh(sop.age, 4.0);
  text("#quickSopDis", sopFresh ? fmt(sop.discharge_power_kw, 1) : "等待");
  text("#quickSopChg", sopFresh ? fmt(sop.charge_power_kw, 1) : "等待");

  const fanFresh = isFresh(quick?.fan_age, 4.0);
  text("#quickFanRpm", fanFresh && quick.fan_rpm_max != null ? String(quick.fan_rpm_max) : "等待");
}

/* ---------------- CAN monitor + replay ---------------- */

function activeFrameList() {
  return state.frameSource === "vehicle"
    ? (state.vehicleSnapshot?.raw_frames || [])
    : (state.snapshot?.raw_frames || []);
}

function fillFrameRow(row, frame) {
  row.className = frame.direction;
  row.innerHTML = `<td>${frame.time}</td>`
    + `<td><span class="dir-tag ${frame.direction}">${frame.direction.toUpperCase()}</span></td>`
    + `<td>${frame.id}</td><td>${frame.extended ? "扩展" : "标准"}</td><td>${frame.dlc}</td>`
    + `<td title="${frame.data}">${frame.data}</td><td title="${frame.name}">${frame.name}</td>`;
}

function frameKey(frame) {
  return `${frame.time}|${frame.direction}|${frame.id}|${frame.extended}|${frame.dlc}|${frame.data}|${frame.name}`;
}

/** Reuse table rows keyed by their full content so unchanged rows keep their DOM node
 *  and the native title tooltips on data/name cells stay visible while polling. */
function renderFrames() {
  if (!state.snapshot) return;
  const frames = state.framePaused ? state.pausedFrames : activeFrameList();
  const query = $("#frameSearch").value.trim().toLowerCase();
  const filtered = frames.filter(frame => (state.frameKind === "all" || frame.direction === state.frameKind) && (!query || `${frame.id} ${frame.name} ${frame.data}`.toLowerCase().includes(query))).slice(0, 180);
  const tbody = $("#frameRows");
  const pooled = state.frameRowPool.filter(row => row.isConnected);
  const freeByKey = new Map();
  pooled.forEach(row => {
    const key = row.dataset.key;
    if (!freeByKey.has(key)) freeByKey.set(key, []);
    freeByKey.get(key).push(row);
  });
  const used = new Set();
  const next = filtered.map(frame => {
    const key = frameKey(frame);
    const pool = freeByKey.get(key);
    let row = null;
    if (pool) {
      const candidate = pool.find(item => !used.has(item));
      if (candidate) { row = candidate; used.add(candidate); }
    }
    if (!row) {
      row = document.createElement("tr");
      row.dataset.key = key;
      fillFrameRow(row, frame);
    }
    return row;
  });
  const current = [...tbody.children];
  const sameOrder = current.length === next.length && current.every((row, i) => row === next[i]);
  if (!sameOrder) {
    pooled.forEach(row => { if (!used.has(row)) row.remove(); });
    next.forEach(row => tbody.appendChild(row));
  }
  state.frameRowPool = next;
  $("#frameEmpty").classList.toggle("hidden", filtered.length > 0);
}

function timeLabel(seconds) {
  const value = Math.max(0, Math.floor(seconds || 0));
  const hours = Math.floor(value / 3600), minutes = Math.floor(value % 3600 / 60), secs = value % 60;
  return hours ? `${String(hours).padStart(2,"0")}:${String(minutes).padStart(2,"0")}:${String(secs).padStart(2,"0")}` : `${String(minutes).padStart(2,"0")}:${String(secs).padStart(2,"0")}`;
}

function renderReplay() {
  const connection = state.snapshot?.connection, replay = connection?.replay;
  $("#replayBar").classList.toggle("hidden", !replay);
  if (!replay) return;
  text("#replayFile", connection.channel); text("#replayPosition", `${timeLabel(replay.position)} / ${timeLabel(replay.duration)}`);
  $("#replaySeek").value = replay.duration ? Math.round(replay.position / replay.duration * 1000) : 0;
  $("#replayPlay").textContent = replay.paused ? "▶" : "Ⅱ";
  $("#replaySpeed").value = String(replay.speed);
}

/* ---------------- shared confirm dialog ---------------- */

async function sendPendingCommand() {
  if (!state.api) return;
  if (state.pendingIvtAction) {
    const pending = state.pendingIvtAction;
    $("#doConfirm").disabled = true;
    const result = pending.kind === "configure"
      ? await state.api.configure_ivt_bms_can1(pending.options)
      : await state.api.switch_ivt_bitrate(pending.options);
    if (result.ok) {
      $("#confirmDialog").close();
      state.pendingIvtAction = null;
      toast(result.message || "IVT 操作完成");
      await poll();
    } else {
      toast(result.error || "IVT 操作失败", true);
      $("#doConfirm").disabled = false;
    }
    return;
  }
  if (state.pendingFanAction) {
    // 通用确认动作（例如启动风扇自动扫频）：必须勾选确认后才会执行。
    const pending = state.pendingFanAction;
    $("#doConfirm").disabled = true;
    try {
      await pending.run();
      $("#confirmDialog").close();
      state.pendingFanAction = null;
      await poll();
    } catch (e) {
      toast(`操作失败：${e}`, true);
      $("#doConfirm").disabled = false;
    }
    return;
  }
  if (state.pendingFanCommand) {
    const pending = state.pendingFanCommand;
    $("#doConfirm").disabled = true;
    const result = await state.api.send_fan_command(pending.name, pending.values, true);
    if (result.ok) {
      $("#confirmDialog").close();
      state.pendingFanCommand = null;
      if (["fan_curve", "fan_failsafe", "fan_restore_defaults"].includes(pending.name)) {
        state.dirty.fan = false;
      }
      toast(result.message || "风扇命令已执行");
      await poll();
    } else {
      toast(result.error || "风扇命令失败", true);
      $("#doConfirm").disabled = false;
    }
    return;
  }
  if (!state.pendingCommand) return;
  $("#doConfirm").disabled = true;
  const command = state.pendingCommand.name;
  const result = await state.api.send_command(command, state.pendingCommand.values, true);
  if (result.ok) {
    const dirtyMap = { alarm_thresholds: "thresholds", alarm_switches: "switches", charge_config: "charge", current_direction: "direction", charger_type: "chargerType" };
    if (dirtyMap[command]) state.dirty[dirtyMap[command]] = false;
    watchFlashSave(command, result.ack);
    $("#confirmDialog").close(); toast(result.message || "命令已发送");
    state.pendingCommand = null;
    await poll();
  }
  else { toast(result.error || "发送失败", true); $("#doConfirm").disabled = false; }
}

async function toggleRecording() {
  if (!state.api) return;
  if (state.recording) {
    await state.api.stop_recording(); state.recording = false; text("#recordButton", "● 记录数据"); toast("CAN 数据记录已停止");
  } else {
    const result = await state.api.choose_record_file();
    if (result.ok) { state.recording = true; text("#recordButton", "■ 停止记录"); toast(`正在记录 ${result.format === "bmslog" ? "BMSLOG" : "CSV"}：${result.path}`); }
    else if (!result.cancelled) toast(result.error || "无法开始记录", true);
  }
}

async function openReplay() {
  if (!state.api) return;
  if (state.recording) return toast("请先停止当前数据记录", true);
  const result = await state.api.choose_replay_file();
  if (result.ok) { toast(`已载入 ${result.frames.toLocaleString()} 帧历史记录`); showPage("frames"); await poll(); }
  else if (!result.cancelled) toast(result.error || "历史记录载入失败", true);
}

async function toggleReplay() {
  const replay = state.snapshot?.connection?.replay; if (!replay || !state.api) return;
  await state.api.replay_control(replay.paused ? "play" : "pause"); await poll();
}

/** Shared canvas trend helper: widen a min/max window to a minimum span. */
function trendAxisRange(values, minimumSpan) {
  if (!values.length) return null;
  let min = Math.min(...values), max = Math.max(...values);
  if (max - min < minimumSpan) {
    const mid = (max + min) / 2;
    min = mid - minimumSpan / 2;
    max = mid + minimumSpan / 2;
  }
  return { min, max };
}

document.addEventListener("DOMContentLoaded", init);
