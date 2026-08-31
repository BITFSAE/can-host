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
    populateConnectionOptions();
    populateToolChannelOptions();
    populateVehicleOptions();
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
  $("#connectButton").addEventListener("click", () => $("#connectDialog").showModal());
  $("#connectProfile").addEventListener("change", updateConnectionDialog);
  $("#doConnect").addEventListener("click", connectCan);
  $("#disconnectButton").addEventListener("click", disconnectCan);
  $("#vehicleStatusPill").addEventListener("click", () => $("#vehicleConnectDialog")?.showModal());
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
  const profiles = [...(data.profiles || [])];
  if (data.simulation_enabled === true && !profiles.some(item => item.key === "simulation" || item.mode === "simulation")) {
    profiles.unshift({ key: "simulation", mode: "simulation", name: "内置模拟数据 · CAN1 / 开发测试", bitrate: 500000 });
  }
  $("#connectProfile").innerHTML = profiles.map(item =>
    `<option value="${item.key}" data-mode="${item.mode || (item.key === "simulation" ? "simulation" : "pcan")}" data-bitrate="${item.bitrate}">${item.name}</option>`
  ).join("");
  if (data.simulation_enabled === true && !state.snapshot?.connection?.connected) {
    $("#connectProfile").value = "simulation";
  }
  $("#connectChannel").innerHTML = (data.channels || ["PCAN_USBBUS1"]).map(item => `<option>${item}</option>`).join("");
  updateConnectionDialog();
}

function populateToolChannelOptions(fallback) {
  const data = state.bootstrap || fallback;
  if (!data?.channels) return;
  const options = data.channels.map(item => `<option>${item}</option>`).join("");
  ["#benchChannelSelect", "#ivtChannelSelect", "#vehicleConnectChannel"]
    .forEach(id => { if ($(id)) $(id).innerHTML = options; });
}

function updateConnectionDialog() {
  const option = $("#connectProfile").selectedOptions?.[0] || $("#connectProfile").options?.[0];
  const simulation = option?.dataset?.mode === "simulation" || option?.value === "simulation";
  $("#channelField")?.classList.toggle("hidden", simulation);
  text("#connectBitrate", simulation ? "虚拟 CAN1" : `${Number(option?.dataset?.bitrate || 500000) / 1000} kbit/s`);
}

async function connectCan() {
  if (!state.api) return toast("应用后端未就绪", true);
  const option = $("#connectProfile").selectedOptions?.[0] || $("#connectProfile").options?.[0];
  const mode = option?.dataset?.mode || (option?.value === "simulation" ? "simulation" : "pcan");
  const profileVal = $("#connectProfile").value || "can1";
  resetChargeTiming();
  $("#doConnect").disabled = true; text("#doConnect", "连接中…");
  $("#connectError").classList.add("hidden");
  const result = await state.api.connect_can({
    mode, bus_profile: mode === "simulation" ? "can1" : profileVal,
    channel: mode === "simulation" ? null : $("#connectChannel").value,
    bitrate: Number(option?.dataset?.bitrate || 500000),
  });
  $("#doConnect").disabled = false; text("#doConnect", "连接");
  if (result.ok) {
    $("#connectDialog").close();
    toast(mode === "simulation" ? "BMS 模拟数据已启动（CAN1）" : "BMS 主连接已建立");
    await poll();
  } else {
    text("#connectError", result.error || "连接失败");
    $("#connectError").classList.remove("hidden");
  }
}

async function disconnectCan() {
  if (!state.api) return;
  resetChargeTiming();
  await state.api.disconnect_can();
  $("#connectDialog").close();
  toast("CAN 已断开");
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

function renderConnection(connection) {
  state.recording = !!connection.recording;
  text("#recordButton", state.recording ? "■ 停止记录" : "● 记录数据");
  setClass("#recordButton", "active", state.recording);
  text("#rxCount", (connection.rx_count || 0).toLocaleString());
  text("#txCount", (connection.tx_count || 0).toLocaleString());
  setClass("#connectButton", "connected", connection.connected);
  setClass("#connectButton", "simulation", connection.mode === "simulation");
  setClass("#connectButton", "replay", connection.mode === "replay");
  const profileNames = { can1: "CAN1 · F405", canb: "CANB · 500", canb_legacy: "CANB Legacy · 250" };
  const modeName = connection.mode === "simulation" ? "BMS 模拟数据"
    : connection.mode === "bench" ? "BMS 从控台架"
    : connection.mode === "replay" ? "BMS 历史回放" : `BMS: ${connection.channel || "PCAN"}`;
  $("#connectButton b").textContent = connection.connected ? modeName : "BMS 未连接";
  $("#connectButton small").textContent = connection.connected
    ? connection.mode === "simulation" ? "虚拟 CAN1 · 测试"
      : `${profileNames[connection.bus_profile] || "CAN1"} · ${(connection.bitrate || 500000) / 1000} kbit/s`
    : "选择 CAN1 总线";
  $("#disconnectButton").classList.toggle("hidden", !connection.connected);

  const vehicleConnection = state.quickSnapshot?.vehicle?.connection
    || state.vehicleSnapshot?.connection || {};
  const vehiclePill = $("#vehicleStatusPill");
  if (vehiclePill) {
    const connected = vehicleConnection.connected === true;
    setClass(vehiclePill, "connected", connected);
    setClass(vehiclePill, "simulation", vehicleConnection.mode === "simulation");
    vehiclePill.querySelector("b").textContent = connected
      ? (vehicleConnection.mode === "simulation" ? "整车模拟数据" : `整车: ${vehicleConnection.channel || "PCAN"}`)
      : "整车未连接";
    vehiclePill.querySelector("small").textContent = connected
      ? vehicleConnection.mode === "simulation" ? "虚拟 CANB"
        : `CANB · ${(vehicleConnection.bitrate || 500000) / 1000} kbit/s`
      : "CANB · 500 kbit/s";
  }

  const firmware = state.snapshot?.firmware || {};
  const firmwareText = [
    firmware.variant,
    firmware.charger_variant && firmware.charger_variant !== "Runtime" ? firmware.charger_variant : "",
    firmware.git,
    firmware.build_date ? `构建 ${firmware.build_date}` : "",
  ].filter(Boolean).join(" · ") || "—";
  const firmwareNode = $("#firmwareIdentity");
  if (firmwareNode) {
    firmwareNode.textContent = firmwareText;
    firmwareNode.title = firmwareText;
  }
  renderQuickBar();
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

  const hvFresh = useMainHv || (vehiclePackFresh && vehiclePack.voltage_valid);
  const hvSource = useMainHv ? overview : vehiclePack;
  text("#quickHvV", hvFresh ? fmt(hvSource.voltage_v, 1) : "等待");
  const hvCurrentFresh = (useMainHv && overview.current_valid) || (vehiclePackFresh && vehiclePack.current_valid);
  text("#quickHvI", hvCurrentFresh ? fmt(hvSource.current_a, 1) : "等待");
  const socFresh = useMainSoc || (vehiclePackFresh && vehiclePack.soc_valid);
  text("#quickSoc", socFresh ? fmt(hvSource.soc_pct, 0) : "等待");

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
