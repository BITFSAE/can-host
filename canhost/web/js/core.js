/* 框架层：状态、轮询、导航、确认弹窗、状态栏与快捷栏。
 * 页面模块（bms/vehicle/fan/bench/ivt/monitor/telemetry）在其后加载。 */

var $ = (selector) => document.querySelector(selector);
var $$ = (selector) => [...document.querySelectorAll(selector)];
window.$ = $;
window.$$ = $$;

var state = {
  api: null,
  bootstrap: null,
  mainSnapshot: null,
  snapshot: null,
  canbBmsSnapshot: null,
  toolSnapshots: { bench: null, ivt: null, simulator: null },
  vehicleSnapshot: null,
  telemetrySnapshot: null,
  quickSnapshot: null,
  page: "overview",
  cellMode: "voltage",
  frameKind: "all",
  frameSource: "main",
  framePaused: false,
  pollTimer: null,
  pollInFlight: false,
  pendingCommand: null,
  pendingIvtAction: null,
  pendingFanCommand: null,
  pendingFanAction: null,
  pendingMonitorAction: null,
  inputsInitialized: { thresholds: false, switches: false, charge: false },
  dirty: { thresholds: false, switches: false, charge: false, direction: false,
           fan: false, fanCaps: false, batteryFanCaps: false },
  onlyActiveAlarms: false,
  theme: document.documentElement.dataset.theme || "dark",
  uiScale: 1,
  lastZoomWheelAt: 0,
  chargeTiming: { active: false, elapsedMs: 0, lastTickMs: null, averageCurrentA: null, currentSumA: 0, currentSamples: 0, connectionKey: null },
  saveWatch: null,
  cellRefs: null,
  pcanScanInFlight: null,
  busMismatchPrompted: { main: null, vehicle: null },
};
window.state = state;

const UI_SCALE_STEPS = [0.8, 0.9, 1, 1.1, 1.2, 1.3];
const UI_SCALE_DEFAULT = 1.1;

const PAGE_ORDER = ["overview", "cells", "alarms", "control", "vehicle", "fan", "frames", "bench", "ivt", "simulator", "telemetry"];
const TOOL_PAGES = ["bench", "ivt", "simulator"];
const DATA_FRESH_MAX_S = 1.5;
const SLOW_DATA_FRESH_MAX_S = 2.5;
const CONNECTION_PREFS_KEY = "canHostConnectionPreferences";
const THEME_PREFS_KEY = "canHostTheme";
let themePersistQueue = Promise.resolve();

function fmt(value, digits = 1, fallback = "—") {
  return value === null || value === undefined || Number.isNaN(value) ? fallback : Number(value).toFixed(digits);
}
function isFresh(age, limit = DATA_FRESH_MAX_S) {
  const value = Number(age);
  return age != null && Number.isFinite(value) && value >= 0 && value <= limit;
}
function hasDataAge(age) {
  const value = Number(age);
  return age != null && Number.isFinite(value) && value >= 0;
}
function isStaleData(age, limit = DATA_FRESH_MAX_S) {
  return hasDataAge(age) && !isFresh(age, limit);
}
function dataAgeText(age, limit = DATA_FRESH_MAX_S) {
  if (!hasDataAge(age)) return "等待数据";
  return `${isStaleData(age, limit) ? "已过期 · " : ""}${fmt(age, 1)} s 前`;
}
function markStaleData(idOrNode, stale) {
  const node = typeof idOrNode === "string" ? $(idOrNode) : idOrNode;
  if (node) node.classList.toggle("data-stale", !!stale);
}
function text(id, value) { const node = $(id); if (node) node.textContent = value; }
function setClass(idOrNode, className, enabled) {
  const node = typeof idOrNode === "string" ? $(idOrNode) : idOrNode;
  if (node) node.classList.toggle(className, !!enabled);
}

function syncToastHost() {
  const host = $("#toastStack");
  if (!host) return null;
  const openDialogs = $$("dialog[open]");
  const target = openDialogs.length ? openDialogs[openDialogs.length - 1] : document.body;
  if (host.parentElement !== target) target.append(host);
  return host;
}

function toast(message, error = false, duration = null) {
  const node = document.createElement("div");
  node.className = `toast${error ? " error" : ""}`;
  node.setAttribute("role", error ? "alert" : "status");
  node.textContent = message;
  syncToastHost()?.append(node);
  setTimeout(() => node.remove(), duration ?? (error ? 8000 : 3600));
}

function escapeHtml(value) {
  return String(value ?? "—").replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
  }[character]));
}

function cssVar(name, fallback = "") {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}
window.cssVar = cssVar;

function normalizeTheme(value) {
  return value === "light" || value === "dark" ? value : "dark";
}

function syncThemeToggle() {
  const button = $("#themeToggle");
  if (!button) return;
  const label = state.theme === "dark" ? "切换到浅色模式" : "切换到深色模式";
  button.title = label;
  button.setAttribute("aria-label", label);
  button.setAttribute("aria-pressed", String(state.theme === "light"));
}

function applyTheme(theme, { persist = true } = {}) {
  const next = normalizeTheme(theme);
  state.theme = next;
  document.documentElement.dataset.theme = next;
  document.documentElement.style.colorScheme = next;
  if (persist) {
    try { localStorage.setItem(THEME_PREFS_KEY, next); } catch { /* storage may be disabled */ }
  }
  syncThemeToggle();
  if (state.page === "overview") requestAnimationFrame(drawTrend);
  if (state.page === "vehicle") requestAnimationFrame(drawVehicleTrend);
}

async function toggleTheme() {
  const next = state.theme === "dark" ? "light" : "dark";
  const root = document.documentElement;
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!reduced && typeof document.startViewTransition === "function") {
    await document.startViewTransition(() => applyTheme(next)).ready.catch(() => {});
  } else {
    if (!reduced) root.classList.add("theme-transition");
    applyTheme(next);
    if (!reduced) window.setTimeout(() => root.classList.remove("theme-transition"), 220);
  }
  if (!state.api?.set_theme_preference) return;
  try {
    themePersistQueue = themePersistQueue.catch(() => {}).then(async () => {
      const result = await state.api.set_theme_preference(next);
      if (result?.ok === false) throw new Error(result.error || "无法保存外观设置");
    });
    await themePersistQueue;
  } catch (error) {
    toast(`外观已切换，但保存失败：${error}`, true);
  }
}

async function syncThemePreference() {
  syncThemeToggle();
  if (!state.api?.theme_preference) return;
  try {
    const remote = normalizeTheme(await state.api.theme_preference());
    // The backend preference also selects the native window background, so it
    // is authoritative if the two stores ever diverge after a failed write.
    applyTheme(remote);
  } catch (error) {
    applyTheme(state.theme, { persist: false });
  }
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
  applyTheme(document.documentElement.dataset.theme, { persist: false });
  restoreUiScale();
  bindNavigation();
  bindCoreControls();
  bindMonitorControls();
  bindBmsControls();
  bindVehicleControls();
  bindFanControls();
  bindBenchControls();
  bindIvtControls();
  bindTelemetryControls();
  bindTelemetrySimulatorControls();
  buildAlarmMatrix();
  buildVehicleStatics();
  try {
    state.api = await waitForApi();
    await syncThemePreference();
    state.bootstrap = await state.api.bootstrap();
    text("#appVersion", `v${state.bootstrap.version || "—"}`);
    text("#appVersionDate", state.bootstrap.version_date || "—");
    $("#simulationBusButton")?.classList.toggle(
      "hidden",
      state.bootstrap.simulation_enabled !== true && state.bootstrap.vehicle_simulation_enabled !== true
    );
    $("#telemetrySimulatorNav")?.classList.toggle("hidden", state.bootstrap.telemetry_simulator_enabled !== true);
    populateConnectionOptions();
    populateToolChannelOptions();
    populateVehicleOptions();
    restoreConnectionPreferences();
    buildSwitchList();
    buildSwitchStatusList();
    await poll();
    const health = await state.api.mark_frontend_ready();
    if (health?.required && !health.ok) throw new Error(`更新启动确认失败：${health.error || "无法写入状态"}`);
    const updateResult = state.bootstrap.startup_update_result;
    if (updateResult?.ok === false) {
      const logHint = updateResult.log_path ? `；日志：${updateResult.log_path}` : "";
      toast(`${updateResult.message || "上次更新失败"}${logHint}`, true, 12000);
    }
    if (typeof initUpdater === "function") initUpdater();
    await reportStartupUpdate();
  } catch (error) {
    toast(`应用后端未就绪：${error}`, true);
    const fallback = { simulation_enabled: false, channels: ["PCAN_USBBUS1"],
      pcan_scan: { ok: false, automatic: false, channels: ["PCAN_USBBUS1"],
        message: "后端未就绪，暂时显示手动通道列表", error: String(error) }, profiles: [
      { key: "can1", name: "CAN1 · F405 主控 / 从控 / 工具", bitrate: 500000 },
      { key: "canb", name: "CANB · ECU / Chroma · 500 kbit/s", bitrate: 500000 },
    ]};
    populateConnectionOptions(fallback);
    populateToolChannelOptions(fallback);
    restoreConnectionPreferences();
  }
}

/* 启动升级确认：安装包升级和软件内更新都要显示本次改了什么。 */
async function reportStartupUpdate() {
  if (typeof showUpdateResult !== "function" || !state.api?.startup_update_state) return;
  let result = null;
  try {
    result = await state.api.startup_update_state();
  } catch (error) {
    return;
  }
  state.startupUpdate = result || null;
  if (result?.upgraded) showUpdateResult(result);
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
  if (["bench", "ivt", "simulator"].includes(page)) refreshPcanChannels(false);
  schedulePoll(0);
}

function bindBackdropDismissal() {
  $$('dialog[data-backdrop-close]').forEach(dialog => {
    dialog.addEventListener("click", event => {
      if (event.target !== dialog || !dialog.open) return;
      const bounds = dialog.getBoundingClientRect();
      const outside = event.clientX < bounds.left || event.clientX > bounds.right
        || event.clientY < bounds.top || event.clientY > bounds.bottom;
      if (outside) dialog.close("cancel");
    });
  });
}

function bindCoreControls() {
  bindBackdropDismissal();
  $("#themeToggle")?.addEventListener("click", toggleTheme);
  $("#can1BusButton")?.addEventListener("click", toggleMainDockConnection);
  $("#canbBusButton")?.addEventListener("click", toggleVehicleDockConnection);
  $("#simulationBusButton")?.addEventListener("click", toggleSimulationChannels);
  $("#connectionSettingsButton")?.addEventListener("click", () => {
    $("#connectDialog")?.showModal();
    refreshPcanChannels(false);
  });
  $("#refreshPcanChannelsButton")?.addEventListener("click", () => refreshPcanChannels(true));
  $("#can1ConnectChannel")?.addEventListener("change", renderConnectionSettingsMessage);
  $("#canbConnectChannel")?.addEventListener("change", renderConnectionSettingsMessage);
  $("#swapConnectionChannels")?.addEventListener("click", swapConnectionChannels);
  $("#busMismatchSwap")?.addEventListener("click", swapMismatchedBusChannels);
  $("#saveConnectionSettings")?.addEventListener("click", saveConnectionPreferences);
  $("#confirmCheck").addEventListener("change", event => $("#doConfirm").disabled = !event.target.checked);
  $("#confirmDialog").addEventListener("close", () => {
    state.pendingCommand = null;
    state.pendingIvtAction = null;
    state.pendingFanCommand = null;
    state.pendingFanAction = null;
    state.pendingMonitorAction = null;
    $("#confirmCheck").checked = false;
    $("#doConfirm").disabled = true;
    setConfirmModeBadge("待确认");
  });
  $$("dialog").forEach(dialog => dialog.addEventListener("close", () => requestAnimationFrame(syncToastHost)));
  $("#doConfirm").addEventListener("click", sendPendingCommand);
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
  populatePcanSelect("#can1ConnectChannel", data);
  populatePcanSelect("#canbConnectChannel", data);
  renderPcanDiscoveryStatus(data.pcan_scan);
  renderConnectionSettingsMessage();
}

function populateToolChannelOptions(fallback) {
  const data = state.bootstrap || fallback;
  if (!data?.channels) return;
  ["#benchChannelSelect", "#ivtChannelSelect"]
    .forEach(id => populatePcanSelect(id, data));
}

function populatePcanSelect(selector, data) {
  const node = $(selector);
  if (!node) return;
  const previous = node.value;
  const channels = Array.isArray(data?.channels) ? data.channels : [];
  const details = new Map((data?.channel_details || []).map(item => [item.channel, item]));
  if (!channels.length) {
    node.innerHTML = '<option value="">未检测到 PCAN 通道</option>';
    node.disabled = true;
    return;
  }
  node.innerHTML = channels.map(channel => {
    const detail = details.get(channel) || {};
    const label = detail.label || channel;
    return `<option value="${escapeHtml(channel)}" title="${escapeHtml(label)}">${escapeHtml(label)}</option>`;
  }).join("");
  node.disabled = node.closest(".disabled") !== null;
  if (channels.includes(previous)) node.value = previous;
}

function renderPcanDiscoveryStatus(scan) {
  const node = $("#pcanDiscoveryStatus");
  if (!node || !scan) return;
  node.textContent = scan.message || "PCAN 设备状态未知";
  node.classList.toggle("warn", scan.ok !== true || !(scan.channels || []).length);
  node.title = scan.error || "";
}

async function refreshPcanChannels(announce = false) {
  if (!state.api?.refresh_pcan_channels) return null;
  if (state.pcanScanInFlight) return state.pcanScanInFlight;
  const button = $("#refreshPcanChannelsButton");
  if (button) {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = "扫描中…";
  }
  text("#pcanDiscoveryStatus", "正在扫描 PCAN 设备…");
  state.pcanScanInFlight = (async () => {
    try {
      const scan = await state.api.refresh_pcan_channels();
      state.bootstrap = {
        ...(state.bootstrap || {}),
        channels: scan.channels || [],
        channel_details: scan.channel_details || [],
        pcan_scan: scan,
      };
      populateConnectionOptions();
      populateToolChannelOptions();
      if (typeof populateTelemetrySimulatorHardware === "function") populateTelemetrySimulatorHardware();
      if (announce) toast(scan.message || "PCAN 设备扫描完成", scan.ok !== true);
      return scan;
    } catch (error) {
      const scan = { ok: false, automatic: false, channels: [], channel_details: [],
        message: "PCAN 设备扫描失败", error: String(error) };
      state.bootstrap = { ...(state.bootstrap || {}), channels: [], channel_details: [], pcan_scan: scan };
      populateConnectionOptions();
      populateToolChannelOptions();
      if (announce) toast(`PCAN 设备扫描失败：${error}`, true);
      return scan;
    } finally {
      state.pcanScanInFlight = null;
      if (button) {
        button.disabled = false;
        button.removeAttribute("aria-busy");
        button.textContent = "刷新设备";
      }
    }
  })();
  return state.pcanScanInFlight;
}

function restoreConnectionPreferences() {
  let prefs = {};
  try { prefs = JSON.parse(localStorage.getItem(CONNECTION_PREFS_KEY) || "{}"); } catch { /* 使用默认项 */ }
  const options = selector => [...($(selector)?.options || [])].map(option => option.value).filter(Boolean);
  const apply = (selector, value) => {
    const node = $(selector);
    if (node && [...node.options].some(option => option.value === String(value))) node.value = String(value);
  };
  const channels = options("#can1ConnectChannel");
  const automatic = state.bootstrap?.pcan_scan?.automatic === true;
  const can1Saved = prefs.can1Channel || prefs.mainChannel || channels[0] || "";
  let canbSaved = prefs.canbChannel || prefs.vehicleChannel || "";
  const oldPrefs = Number(prefs.version || 0) < 2;
  if (!canbSaved) {
    canbSaved = automatic
      ? channels.find(channel => channel !== can1Saved) || channels[0] || ""
      : channels[0] || "";
  } else if (automatic && oldPrefs && canbSaved === can1Saved && channels.length > 1) {
    canbSaved = channels.find(channel => channel !== can1Saved) || channels[0] || "";
  }
  apply("#can1ConnectChannel", can1Saved);
  apply("#canbConnectChannel", canbSaved);
  renderConnectionSettingsMessage();
}

function persistConnectionPreferences() {
  const prefs = {
    version: 3,
    can1Channel: $("#can1ConnectChannel")?.value || "",
    canbChannel: $("#canbConnectChannel")?.value || "",
  };
  try { localStorage.setItem(CONNECTION_PREFS_KEY, JSON.stringify(prefs)); } catch { /* 本次运行仍保留选择 */ }
  return prefs;
}

function saveConnectionPreferences() {
  const prefs = persistConnectionPreferences();
  $("#connectDialog")?.close();
  const shared = prefs.can1Channel && prefs.can1Channel === prefs.canbChannel;
  toast(shared
    ? "设置已保存；两条总线共用同一通道，连接其中一条会自动断开另一条"
    : "CAN1 / CANB 通道分配已保存");
}

function renderConnectionSettingsMessage() {
  const message = $("#connectionSettingsMessage");
  if (!message) return;
  const can1 = $("#can1ConnectChannel")?.value || "";
  const canb = $("#canbConnectChannel")?.value || "";
  const shared = can1 && can1 === canb;
  message.classList.toggle("warn", !!shared);
  const content = shared
    ? "同一条 PCAN 通道不能同时连接两条总线；连接中点另一条总线按钮会自动切换，也可点中间的交换按钮自动错开。"
    : can1 && canb
      ? ""
      : "请为两条总线选择 PCAN 通道。";
  message.textContent = content;
  message.classList.toggle("hidden", !content);
}

function swapConnectionChannels() {
  const can1 = $("#can1ConnectChannel");
  const canb = $("#canbConnectChannel");
  if (!can1 || !canb || !can1.value || !canb.value) return;
  if (can1.value === canb.value) {
    const alternative = [...canb.options].find(option => option.value && option.value !== can1.value);
    if (alternative) canb.value = alternative.value;
    renderConnectionSettingsMessage();
    return;
  }
  const previous = can1.value;
  can1.value = canb.value;
  canb.value = previous;
  renderConnectionSettingsMessage();
}

function mainConnectionState() {
  return state.mainSnapshot?.connection || state.snapshot?.connection || {};
}

function vehicleConnectionState() {
  return state.quickSnapshot?.vehicle?.connection || state.vehicleSnapshot?.connection || {};
}

function roleButton(role) {
  return {
    can1: $("#can1BusButton"),
    canb: $("#canbBusButton"),
    simulation: $("#simulationBusButton"),
  }[role];
}

async function toggleMainDockConnection() {
  if (!state.api) return toast("应用后端未就绪", true);
  if (roleButton("can1")?.classList.contains("connecting")) return;
  if (mainConnectionState().connected === true && mainConnectionState().mode !== "simulation") return disconnectCan();
  const channel = $("#can1ConnectChannel")?.value;
  if (!channel) return toast("未检测到可选择的 PCAN 通道；请连接设备后刷新", true);
  state.busMismatchPrompted.main = null;
  const vehicle = vehicleConnectionState();
  if (vehicle.connected === true && vehicle.mode === "simulation") {
    await state.api.disconnect_vehicle();
    state.vehicleSnapshot = null;
  }
  resetChargeTiming();
  setBusConnecting("can1", true);
  let result;
  try {
    result = await state.api.connect_can({
      mode: "pcan", bus_profile: "can1",
      channel, bitrate: 500000,
      auto_record: typeof monitorAutoRecordEnabled === "function" ? monitorAutoRecordEnabled() : true,
    });
  } catch (error) {
    return toast(`连接失败：${error}`, true);
  } finally {
    setBusConnecting("can1", false);
  }
  if (!result?.ok) return toast(result?.error || "CAN1 连接失败", true);
  toast(result.notice || "CAN1 已连接");
  if (result.warning) toast(result.warning, true);
  // The status-bar buttons only control connections. Keep the operator's
  // current workspace in place; the monitor remains available from the nav.
  state.frameSource = "main";
  await poll();
}

async function toggleVehicleDockConnection() {
  if (!state.api) return toast("应用后端未就绪", true);
  if (roleButton("canb")?.classList.contains("connecting")) return;
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
  const main = mainConnectionState();
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
  const main = mainConnectionState();
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
  toast("临时调试模拟通道已启动；实车与台架验证请使用实体 PCAN");
  await poll();
}

async function disconnectSimulationChannels() {
  const main = mainConnectionState();
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
  const main = mainConnectionState();
  const vehicle = vehicleConnectionState();
  const sim = simulationState();
  const mainReplay = main.connected === true && main.mode === "replay";
  const states = {
    can1: main.connected === true && main.mode !== "simulation",
    canb: vehicle.connected === true && vehicle.mode !== "simulation",
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
    can1: mainReplay
      ? `${busProfileLabel(main, "CAN1")} 历史回放中，点击停止`
      : states.can1 ? "CAN1 已连接，点击断开" : "按保存设置直接连接 CAN1",
    canb: states.canb ? "CANB 已连接，点击断开" : "按保存设置直接连接 CANB",
    simulation: sim.complete ? "开发模拟通道已启动，点击停止" : sim.running.length ? "模拟通道未完整启动，点击重试" : "启动开发模拟通道",
  };
  Object.entries(titles).forEach(([role, title]) => roleButton(role)?.setAttribute("title", title));
}

async function disconnectCan() {
  if (!state.api) return;
  const replay = mainConnectionState().mode === "replay";
  state.busMismatchPrompted.main = null;
  resetChargeTiming();
  setBusConnecting("can1", true);
  try { await state.api.disconnect_can(); }
  catch (error) { return toast(`断开失败：${error}`, true); }
  finally { setBusConnecting("can1", false); }
  toast(replay ? "历史回放已停止" : "CAN1 已断开");
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
    state.mainSnapshot = await state.api.get_snapshot();
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
    } else if (page === "simulator" && state.api.get_telemetry_simulator_snapshot) {
      await optionalSnapshot(state.api.get_telemetry_simulator_snapshot,
        value => { state.toolSnapshots.simulator = value; });
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
    if (!TOOL_PAGES.includes(page) && state.mainSnapshot?.connection?.connected !== true
        && state.api.get_canb_bms_snapshot) {
      await optionalSnapshot(state.api.get_canb_bms_snapshot, value => { state.canbBmsSnapshot = value; });
    }
    state.snapshot = effectiveBmsSnapshot();
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

function effectiveBmsSnapshot() {
  const main = state.mainSnapshot;
  const canb = state.canbBmsSnapshot;
  if (main?.connection?.connected === true) return main;
  if (canb?.connection?.connected === true) return canb;
  return main || canb;
}

function render() {
  const snap = state.snapshot; if (!snap) return;
  renderConnection();
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
  } else if (state.page === "simulator") {
    renderTelemetrySimulator();
  } else if (state.page === "fan") {
    renderFan();
  } else if (state.page === "frames") {
    renderReplay();
    renderFrames();
  } else if (state.page === "telemetry") {
    renderTelemetry();
  }
}

function renderBusDock() {
  const main = mainConnectionState();
  const vehicle = state.quickSnapshot?.vehicle?.connection
    || state.vehicleSnapshot?.connection || {};
  const bmsData = state.snapshot?.connection || {};
  updateScopeStrips(main, vehicle || {}, bmsData);
  updateBusButtonsEnabled();
}

function updateScopeStrips(main, vehicle, bmsData) {
  const mainConnected = main.connected === true;
  const vehicleConnected = vehicle.connected === true;
  const bmsDataConnected = bmsData.connected === true;
  const mainMismatch = main.bus_mismatch || null;
  const vehicleMismatch = vehicle.bus_mismatch || null;
  renderFrameSourceLabels(main, vehicle);
  let frameSource = state.frameSource || "main";
  const selectedReady = frameSource === "vehicle" ? vehicleConnected : mainConnected;
  if (!selectedReady && (mainConnected || vehicleConnected)) {
    frameSource = mainConnected ? "main" : "vehicle";
    state.frameSource = frameSource;
    $$("#frameSource button").forEach(button => button.classList.toggle("active", button.dataset.source === frameSource));
  }
  $$("#frameSource button").forEach(button => {
    button.disabled = button.dataset.source === "vehicle" ? !vehicleConnected : !mainConnected;
  });

  // 疑似接反时按“不可写”呈现：F405 不接受来自 CANB 的工具命令，
  // 此时保持按钮可用只会让操作者误以为命令能送达。
  const can1Writable = mainConnected && main.mode !== "replay"
    && main.bus_profile === "can1" && !mainMismatch;
  const controlStrip = $("#controlScopeStrip");
  if (controlStrip) controlStrip.hidden = can1Writable;
  document.body.classList.toggle("control-write-locked", !can1Writable);
  $("#page-control")?.classList.toggle("scope-warning", !can1Writable && (mainConnected || vehicleConnected));
  if (!can1Writable) {
    text("#controlScopeTitle", mainMismatch ? "CAN1 写命令不可用 · 总线疑似接反" : "CAN1 写命令不可用");
    text("#controlScopeDetail", mainMismatch
      ? busMismatchDetail("CAN1 连接", mainMismatch)
      : "只有实体 CAN1 连接才能发送 F405 工具命令");
  }

  // 数据页顶部连接提示：未连接时说明需要什么连接，疑似接反时醒目提醒，正常后隐藏。
  const bmsMismatch = state.snapshot === state.mainSnapshot ? mainMismatch : vehicleMismatch;
  const mainWaiting = !bmsDataConnected;
  const cellsWaiting = !(mainConnected && main.bus_profile === "can1");
  setBmsScopeStrip("#overviewScopeStrip", mainWaiting, bmsMismatch, "等待 BMS 数据",
    "点击底部“CAN1”或“CANB”按钮连接后开始监视。");
  setBmsScopeStrip("#cellsScopeStrip", cellsWaiting, mainMismatch, "等待 CAN1 连接",
    "逐串电压与温度只在 CAN1 广播；连接 CAN1 后可查看 138 串电压与 48 路温度。");
  setBmsScopeStrip("#alarmsScopeStrip", mainWaiting, bmsMismatch, "等待 BMS 数据",
    "连接 CAN1 或 CANB 后显示故障码、告警等级与历史记录。");

  const vehicleUsable = vehicleConnected && vehicle.bus_profile === "canb"
    && Number(vehicle.bitrate) === 500000 && !vehicleMismatch;
  updateScopeStrip("#vehicleScopeStrip", vehicleUsable ? { show: false } : {
    show: true, danger: !!vehicleMismatch,
    title: vehicleMismatch ? "总线疑似接反" : "等待 CANB 连接",
    detail: vehicleMismatch
      ? busMismatchDetail("CANB 连接", vehicleMismatch)
      : "点击底部“CANB”按钮连接后查看 ECU / PDM / 赛会能量计等遥测。",
  });

  // 可写判定必须与风扇页 fanConnectionAvailable() 一致：内置模拟通道只提供数据，
  // 命令发不出去，此时仍要显示提示并把写入按钮锁上。
  const fanWritable = vehicleConnected && vehicle.mode === "pcan"
    && vehicle.bus_profile === "canb" && Number(vehicle.bitrate) === 500000;
  const fanStrip = $("#fanScopeStrip");
  if (fanStrip) {
    fanStrip.hidden = fanWritable && !vehicleMismatch;
    fanStrip.classList.toggle("danger", !!vehicleMismatch);
    text("#fanScopeTitle", vehicleMismatch ? "总线疑似接反" : "整车风扇需要 CANB");
    text("#fanScopeDetail", fanWritable && !vehicleMismatch
      ? ""
      : vehicleMismatch
        ? busMismatchDetail("CANB 连接", vehicleMismatch)
        : !vehicleConnected
          ? "点击底部“CANB”直接连接后，才能查看和命令风扇"
          : vehicle.mode !== "pcan"
            ? "当前 CANB 数据来自内置模拟；风扇命令需要实体 CANB"
            : "当前连接不是 CANB 500 kbit/s");
  }
  document.body.classList.toggle("fan-write-locked", !fanWritable);

  maybeNotifyBusMismatch("main", "CAN1 连接", main);
  maybeNotifyBusMismatch("vehicle", "CANB 连接", vehicle);
}

function busProfileLabel(connection, fallback) {
  const profile = connection?.bus_profile;
  if (profile === "canb") return "CANB";
  if (profile === "can1") return "CAN1";
  return fallback;
}

/** Keep a CANB log from masquerading as a live CAN1 stream.  Replay still
 * lives in the main service, but every visible source label follows the
 * profile stored in the recording metadata (or inferred from its frames). */
function renderFrameSourceLabels(main, vehicle) {
  const mainReplay = main?.connected === true && main.mode === "replay";
  const mainBus = busProfileLabel(main, "CAN1");
  const mainLabel = mainReplay ? `回放 ${mainBus}` : "CAN1";
  const mainTitle = mainReplay ? `历史回放 · ${mainBus}` : "CAN1 数据流";
  const mainSourceButton = $('#frameSource button[data-source="main"]');
  if (mainSourceButton) {
    mainSourceButton.textContent = mainLabel;
    mainSourceButton.title = mainTitle;
  }
  const vehicleSourceButton = $('#frameSource button[data-source="vehicle"]');
  if (vehicleSourceButton) {
    vehicleSourceButton.textContent = "CANB";
    vehicleSourceButton.title = "CANB 数据流";
  }
  const dockLabel = $("#can1BusButton b");
  if (dockLabel) dockLabel.textContent = mainLabel;
}

/** 统一驱动 BMS 数据页的顶部提示条：未连接给引导，接反给醒目警示。 */
function setBmsScopeStrip(id, waiting, mismatch, waitTitle, waitDetail) {
  updateScopeStrip(id, waiting || mismatch ? {
    show: true, danger: !!mismatch,
    title: mismatch ? "总线疑似接反" : waitTitle,
    detail: mismatch ? busMismatchDetail("当前 BMS 数据源", mismatch) : waitDetail,
  } : { show: false });
}

function updateScopeStrip(id, { show, danger = false, title = null, detail = null } = {}) {
  const strip = $(id);
  if (!strip) return;
  strip.hidden = !show;
  strip.classList.toggle("danger", danger);
  if (title != null) strip.querySelector("b").textContent = title;
  if (detail != null) strip.querySelector("small").textContent = detail;
}

function busMismatchDetail(scopeLabel, mismatch) {
  const names = { can1: "CAN1", canb: "CANB" };
  const evidence = mismatch.detected === "canb"
    ? "CANB 独有的节点帧（PDM / 风扇 / ECU 等）"
    : "CAN1 从控逐串帧";
  return `${scopeLabel}按${names[mismatch.expected]}工作，却持续收到${evidence}；PCAN 疑似实际接在${names[mismatch.detected]}上，请核对接线。`;
}

/** 疑似接反只弹窗提醒一次；同一连接不再重复打扰，其他弹窗打开时顺延。 */
function maybeNotifyBusMismatch(slot, scopeLabel, connection) {
  const mismatch = connection?.bus_mismatch;
  if (connection?.connected !== true) {
    state.busMismatchPrompted[slot] = null;
    return;
  }
  const connectionKey = [connection.mode, connection.channel, connection.bus_profile,
                         connection.bitrate].join("|");
  let prompted = state.busMismatchPrompted[slot];
  if (!prompted || prompted.connectionKey !== connectionKey) {
    prompted = { connectionKey, shown: false };
    state.busMismatchPrompted[slot] = prompted;
  }
  if (!mismatch || prompted.shown) return;
  if ($("dialog[open]")) return;
  prompted.shown = true;
  text("#busMismatchMessage", busMismatchDetail(scopeLabel, mismatch));
  // 只有两路实体连接同时报告接反（各自听到对侧独有帧）才提供一键交换；
  // 单路接反可能是线插错总线，交换通道分配反而改错配置。
  const main = mainConnectionState();
  const vehicle = vehicleConnectionState();
  const bothSwapped = !!(main.connected === true && main.mode === "pcan" && main.bus_mismatch
    && vehicle.connected === true && vehicle.mode === "pcan" && vehicle.bus_mismatch
    && String(main.channel || "") && String(vehicle.channel || "")
    && String(main.channel) !== String(vehicle.channel));
  $("#busMismatchSwap")?.classList.toggle("hidden", !bothSwapped);
  $("#busMismatchDialog")?.showModal();
}

/** 两路实体连接都报告接反时，按实际占用通道对调分配、保存并重连。 */
async function swapMismatchedBusChannels() {
  if (!state.api) return;
  const button = $("#busMismatchSwap");
  const main = mainConnectionState();
  const vehicle = vehicleConnectionState();
  const can1Channel = String(vehicle.channel || "");
  const canbChannel = String(main.channel || "");
  if (!can1Channel || !canbChannel || can1Channel === canbChannel) return;
  if (button) {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
  }
  try {
    const autoRecord = typeof monitorAutoRecordEnabled === "function" ? monitorAutoRecordEnabled() : true;
    const result = await state.api.swap_mismatched_bus_channels({ auto_record: autoRecord });
    if (!result?.ok) return toast(result?.error || "交换通道失败", true);
    const applySelection = (selector, value) => {
      const node = $(selector);
      if (node && [...node.options].some(option => option.value === value)) node.value = value;
    };
    applySelection("#can1ConnectChannel", result.can1_channel || can1Channel);
    applySelection("#canbConnectChannel", result.canb_channel || canbChannel);
    persistConnectionPreferences();
    $("#busMismatchDialog")?.close();
    state.busMismatchPrompted.main = null;
    state.busMismatchPrompted.vehicle = null;
    resetChargeTiming();
    state.frameSource = "main";
    toast(result.notice || "通道已交换并重新连接");
    if (result.warning) toast(result.warning, true);
  } catch (error) {
    toast(`交换通道失败：${error}`, true);
  } finally {
    if (button) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  }
  await poll();
}

function renderConnection() {
  const connection = mainConnectionState();
  const vehicleConnection = vehicleConnectionState();
  const mainRx = Number(connection.rx_count || 0), mainTx = Number(connection.tx_count || 0);
  const vehicleRx = Number(vehicleConnection.rx_count || 0), vehicleTx = Number(vehicleConnection.tx_count || 0);
  text("#rxCount", (mainRx + vehicleRx).toLocaleString());
  text("#txCount", (mainTx + vehicleTx).toLocaleString());
  const traffic = $(".traffic-group");
  const mainTrafficLabel = connection.mode === "replay"
    ? `回放 ${busProfileLabel(connection, "CAN")}` : "CAN1";
  if (traffic) traffic.title = `${mainTrafficLabel} RX ${mainRx.toLocaleString()} / TX ${mainTx.toLocaleString()} · CANB RX ${vehicleRx.toLocaleString()} / TX ${vehicleTx.toLocaleString()}`;
  const firmware = state.mainSnapshot?.firmware || {};
  const firmwareText = [
    firmware.variant,
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

/** Always-visible quick values: prefer fresh sources, then keep the last valid
 *  sample visible with an explicit stale state.  This strip is diagnostic only;
 *  control paths keep their own strict freshness gates. */
function renderQuickBar() {
  const quick = state.quickSnapshot?.vehicle;
  const main = state.mainSnapshot || {};
  const overview = main.overview || {};
  const mainConnection = main.connection || {};
  const mainSummaryKnown = hasDataAge(mainConnection.summary_age) && overview.voltage_valid !== undefined;
  const mainSummaryFresh = isFresh(mainConnection.summary_age) && overview.voltage_valid !== undefined;
  const vehiclePack = quick?.pack || {};
  const vehiclePackKnown = hasDataAge(vehiclePack.age);
  const vehiclePackFresh = isFresh(vehiclePack.age);
  const useMainHv = mainSummaryFresh && overview.voltage_valid;
  const useMainSoc = mainSummaryFresh && overview.soc_valid;

  const setQuickValue = (id, value, known, fresh, age) => {
    const node = $(id);
    if (!node) return;
    node.textContent = known && value != null ? value : "等待";
    const cell = node.closest(".quick-cell");
    const stale = known && !fresh;
    cell?.classList.toggle("data-stale", stale);
    if (cell) cell.title = known ? dataAgeText(age, 4.0) : "等待数据";
  };
  const choosePackSource = (mainKnown, mainFresh, vehicleKnown, vehicleFresh) => {
    if (mainFresh) return { data: overview, age: mainConnection.summary_age, fresh: true };
    if (vehicleFresh) return { data: vehiclePack, age: vehiclePack.age, fresh: true };
    if (mainKnown && vehicleKnown) {
      return Number(mainConnection.summary_age) <= Number(vehiclePack.age)
        ? { data: overview, age: mainConnection.summary_age, fresh: false }
        : { data: vehiclePack, age: vehiclePack.age, fresh: false };
    }
    if (mainKnown) return { data: overview, age: mainConnection.summary_age, fresh: false };
    if (vehicleKnown) return { data: vehiclePack, age: vehiclePack.age, fresh: false };
    return null;
  };

  const pdm = quick?.pdm || {};
  const lvKnown = hasDataAge(pdm.age) && !pdm.bus_offline;
  const lvFresh = isFresh(pdm.age, 4.0) && !pdm.bus_offline;
  setQuickValue("#quickLvV", fmt(pdm.bus_voltage_v, 1), lvKnown && pdm.bus_voltage_v != null, lvFresh, pdm.age);
  setQuickValue("#quickLvI", fmt(pdm.bus_current_a, 1), lvKnown && pdm.bus_current_a != null, lvFresh, pdm.age);
  setQuickValue("#quickLvP", fmt(pdm.bus_power_w, 0), lvKnown && pdm.bus_power_w != null, lvFresh, pdm.age);

  const vehicleVoltageFresh = vehiclePackFresh && vehiclePack.voltage_valid;
  const mainVoltageKnown = mainSummaryKnown && overview.voltage_valid;
  const vehicleVoltageKnown = vehiclePackKnown && vehiclePack.voltage_valid;
  const voltageSource = choosePackSource(mainVoltageKnown, useMainHv,
    vehicleVoltageKnown, vehicleVoltageFresh);
  setQuickValue("#quickHvV", voltageSource && fmt(voltageSource.data.voltage_v, 1),
    !!voltageSource, voltageSource?.fresh, voltageSource?.age);
  const useMainCurrent = mainSummaryFresh && overview.current_valid;
  const vehicleCurrentFresh = vehiclePackFresh && vehiclePack.current_valid;
  const mainCurrentKnown = mainSummaryKnown && overview.current_valid;
  const vehicleCurrentKnown = vehiclePackKnown && vehiclePack.current_valid;
  const currentSource = choosePackSource(mainCurrentKnown, useMainCurrent,
    vehicleCurrentKnown, vehicleCurrentFresh);
  setQuickValue("#quickHvI", currentSource && fmt(currentSource.data.current_a, 1),
    !!currentSource, currentSource?.fresh, currentSource?.age);
  const vehicleSocFresh = vehiclePackFresh && vehiclePack.soc_valid;
  const mainSocKnown = mainSummaryKnown && overview.soc_valid;
  const vehicleSocKnown = vehiclePackKnown && vehiclePack.soc_valid;
  const socSource = choosePackSource(mainSocKnown, useMainSoc,
    vehicleSocKnown, vehicleSocFresh);
  setQuickValue("#quickSoc", socSource && fmt(socSource.data.soc_pct, 0),
    !!socSource, socSource?.fresh, socSource?.age);

  const sop = quick?.sop || {};
  const sopKnown = hasDataAge(sop.age);
  const sopFresh = isFresh(sop.age, 4.0);
  setQuickValue("#quickSopDis", fmt(sop.discharge_power_kw, 1), sopKnown && sop.discharge_power_kw != null,
    sopFresh, sop.age);
  setQuickValue("#quickSopChg", fmt(sop.charge_power_kw, 1), sopKnown && sop.charge_power_kw != null,
    sopFresh, sop.age);

  const fanKnown = hasDataAge(quick?.fan_age) && quick?.fan_rpm_max != null;
  const fanFresh = isFresh(quick?.fan_age, 4.0);
  setQuickValue("#quickFanRpm", fanKnown ? String(quick.fan_rpm_max) : null,
    fanKnown, fanFresh, quick?.fan_age);
}

/* ---------------- shared confirm dialog ---------------- */

async function sendPendingCommand() {
  if (!state.api) return;
  if (state.pendingMonitorAction) {
    const pending = state.pendingMonitorAction;
    $("#doConfirm").disabled = true;
    try {
      const result = await pending.run();
      if (!result?.ok) throw new Error(result?.error || "CAN 发送失败");
      $("#confirmDialog").close();
      state.pendingMonitorAction = null;
      toast(pending.success || "CAN 发送已执行");
      await poll();
    } catch (error) {
      toast(String(error.message || error), true);
      $("#doConfirm").disabled = false;
      monitorUi.lastTxSignature = "";
      renderMonitorTransmitRows();
    }
    return;
  }
  if (state.pendingIvtAction) {
    const pending = state.pendingIvtAction;
    $("#doConfirm").disabled = true;
    const result = await state.api.configure_ivt_bms_can1(pending.options);
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
      if (pending.name === "fan_calib" && [5, 6].includes(Number(pending.values?.action))) {
        state.dirty.fanCaps = false;
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
    const dirtyMap = { alarm_thresholds: "thresholds", alarm_switches: "switches", charge_config: "charge", current_direction: "direction" };
    if (dirtyMap[command]) state.dirty[dirtyMap[command]] = false;
    watchFlashSave(command, result.ack);
    $("#confirmDialog").close(); toast(result.message || "命令已发送");
    state.pendingCommand = null;
    await poll();
  }
  else { toast(result.error || "发送失败", true); $("#doConfirm").disabled = false; }
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
