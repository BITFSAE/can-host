const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const state = {
  api: null,
  bootstrap: null,
  snapshot: null,
  page: "overview",
  cellMode: "voltage",
  frameKind: "all",
  framePaused: false,
  pausedFrames: [],
  pollTimer: null,
  pollInFlight: false,
  pendingCommand: null,
  inputsInitialized: { thresholds: false, switches: false, charge: false },
  dirty: { thresholds: false, switches: false, charge: false, direction: false, chargerType: false },
  recording: false,
  onlyActiveAlarms: false,
  uiScale: 1,
  lastZoomWheelAt: 0,
  chargeTiming: { active: false, elapsedMs: 0, lastTickMs: null, averageCurrentA: null, currentSumA: 0, currentSamples: 0 },
  saveWatch: null,
};

const PACK_CAPACITY_AH = 16.2;
const UI_SCALE_STEPS = [0.8, 0.9, 1, 1.1, 1.2, 1.3];
const UI_SCALE_DEFAULT = 1.1;
const MACHINE_STATE_CLASSES = { 2: "self-test", 3: "standby", 4: "precharge", 5: "hv-on", 7: "fault" };
const SAVE_KINDS = {
  alarm_thresholds: "config", alarm_switches: "config", charge_config: "config",
  charger_type: "config", current_direction: "direction",
};
const SAVE_LABELS = {
  alarm_thresholds: "告警阈值", alarm_switches: "告警开关", charge_config: "充电请求",
  charger_type: "充电机类型", current_direction: "电流方向",
};
const IMD_FREQUENCY_NOTES = {
  0: "无有效 PWM", 1: "DCP", 2: "DCP", 3: "SST", 4: "设备错误", 5: "接地线错误", 15: "未知频率",
};

const ALARM_RULE_TEXTS = [
  null, null, null, null,
  "采样线开路 · 下一处理周期", "温度线开路 · 下一处理周期",
  "压差 ≥800 mV · 即时", "温差 ≥30 °C · 即时",
  "总压 ≥578.2 V · 480 ms", "总压 ≤415.4 V · 480 ms",
  "风扇/UART/RTC/Flash异常 · 二级", "≤10% 二级 / ≤5% 一级",
  "充电电流 >15.0 A · 270 ms", "放电电流 <−180.0 A · 270 ms",
  "保留位 · 算法未启用", "保留位 · 算法未启用",
  "任一从控数据未就绪 · 即时", "预充超时或失败 · 复位前锁存",
  "CAN/TIM启动失败 · 本次上电锁存", "单体累加与U1差 >100 V · 约1.5 s",
  "离线约360 ms / 状态异常270 ms", "HAL错误或Bus-off · 约3 s无新错误后清除",
  "高压中HV_ACC请求消失 · 事件记录", "充电机反馈 >500 ms · 按在线源定级",
  "20 ms重试25次失败 · 复位前锁存",
  "BMU 1 数据未就绪", "BMU 2 数据未就绪", "BMU 3 数据未就绪",
  "BMU 4 数据未就绪", "BMU 5 数据未就绪", "BMU 6 数据未就绪",
  "IVT U1失联约360 ms",
];

const PAGE_ORDER = ["overview", "cells", "alarms", "frames", "control"];

function fmt(value, digits = 1, fallback = "—") {
  return value === null || value === undefined || Number.isNaN(value) ? fallback : Number(value).toFixed(digits);
}
function text(id, value) { const node = $(id); if (node) node.textContent = value; }
function setClass(id, className, enabled) { const node = $(id); if (node) node.classList.toggle(className, !!enabled); }
function liveFaultState(snapshot) {
  const fault = snapshot?.fault || {};
  const faultFresh = fault.received === true && fault.age != null && fault.age <= 1.5;
  const alarmLevelsFresh = (snapshot?.alarms || []).some(item => item.received === true && item.age != null && item.age <= 1.5);
  return { fault, faultFresh, alarmLevelsFresh, known: faultFresh || alarmLevelsFresh };
}
function toast(message, error = false) {
  const node = document.createElement("div");
  node.className = `toast${error ? " error" : ""}`;
  node.textContent = message;
  $("#toastStack").append(node);
  setTimeout(() => node.remove(), 3600);
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
  try { localStorage.setItem("bmsUiScaleV2", String(nearest)); } catch { /* storage may be disabled */ }
  if (state.page === "overview") setTimeout(drawTrend, 0);
  if (announce) toast(`界面缩放 ${Math.round(nearest * 100)}%`);
}

function restoreUiScale() {
  let saved = UI_SCALE_DEFAULT;
  try { saved = Number(localStorage.getItem("bmsUiScaleV2")) || UI_SCALE_DEFAULT; } catch { /* storage may be disabled */ }
  applyUiScale(saved, false);
}

function stepUiScale(direction) {
  const index = UI_SCALE_STEPS.indexOf(state.uiScale);
  const next = Math.max(0, Math.min(UI_SCALE_STEPS.length - 1, index + direction));
  applyUiScale(UI_SCALE_STEPS[next]);
}

async function waitForApi() {
  if (window.pywebview?.api) return window.pywebview.api;
  await new Promise(resolve => window.addEventListener("pywebviewready", resolve, { once: true }));
  return window.pywebview.api;
}

async function init() {
  restoreUiScale();
  bindNavigation();
  bindControls();
  buildAlarmMatrix();
  try {
    state.api = await waitForApi();
    state.bootstrap = await state.api.bootstrap();
    text("#appVersion", `v${state.bootstrap.version || "—"}`);
    text("#appVersionDate", state.bootstrap.version_date || "—");
    populateConnectionOptions();
    buildSwitchList();
    await poll();
  } catch (error) {
    toast(`应用后端未就绪：${error}`, true);
    populateConnectionOptions({ simulation_enabled: false, channels: ["PCAN_USBBUS1"], profiles: [
      { key: "can1", name: "CAN1 · 主控 / 从控 / 工具", bitrate: 500000 },
      { key: "canb", name: "CANB · IVT / ECU / Chroma", bitrate: 500000 },
    ]});
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
  $$(".nav-item").forEach(node => node.classList.toggle("active", node.dataset.page === page));
  $$(".page").forEach(node => node.classList.toggle("active", node.id === `page-${page}`));
  // Every page owns a different information depth. Keeping the previous page's
  // scroll offset can make a short page appear blank after navigation.
  $("#main").scrollTop = 0;
  if (state.snapshot) render();
  schedulePoll(0);
}

function bindControls() {
  $("#connectButton").addEventListener("click", () => $("#connectDialog").showModal());
  $("#connectMode").addEventListener("change", updateConnectionDialog);
  $("#connectProfile").addEventListener("change", updateConnectionDialog);
  $("#doConnect").addEventListener("click", connectCan);
  $("#disconnectButton").addEventListener("click", disconnectCan);
  $("#onlyAbnormal").addEventListener("change", renderCells);
  $("#frameType").addEventListener("click", event => {
    const button = event.target.closest("button"); if (!button) return;
    state.frameKind = button.dataset.kind;
    $$("#frameType button").forEach(node => node.classList.toggle("active", node === button));
    renderFrames();
  });
  $("#frameSearch").addEventListener("input", renderFrames);
  $("#pauseFrames").addEventListener("change", event => {
    state.framePaused = event.target.checked;
    if (state.framePaused) state.pausedFrames = state.snapshot?.raw_frames || [];
    renderFrames();
  });
  $("#sendThresholds").addEventListener("click", () => confirmCommand(
    "alarm_thresholds",
    { ov_mv: +$("#ovInput").value, uv_mv: +$("#uvInput").value, ot_c: +$("#otInput").value, ut_c: +$("#utInput").value },
    "写入单体告警阈值", `OV ${$("#ovInput").value} mV\nUV ${$("#uvInput").value} mV\nOT ${$("#otInput").value} °C\nUT ${$("#utInput").value} °C`
  ));
  $("#sendSwitches").addEventListener("click", () => {
    const switches = {};
    $$("#switchList input").forEach(input => switches[input.dataset.key] = input.checked);
    confirmCommand("alarm_switches", { switches }, "写入告警开关", "将当前页面的 19 个开关作为一组写入主控。写入后以周期回报值为准。");
  });
  $("#sendChargeConfig").addEventListener("click", () => confirmCommand(
    "charge_config", { voltage_v: +$("#chargeVoltage").value, current_a: +$("#chargeCurrent").value },
    "写入充电请求", `${$("#chargeVoltage").value} V  /  ${$("#chargeCurrent").value} A`
  ));
  $("#syncRtc").addEventListener("click", () => {
    const now = new Date(); const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
    confirmCommand("rtc", { datetime: local }, "RTC 校时", `写入上位机本地时间：${local.replace("T", " ")}`);
  });
  $("#sendCurrentDirection").addEventListener("click", () => {
    const inverted = $("#currentDirection").value === "1";
    confirmCommand("current_direction", { inverted }, "写入电流方向", `明确设置为“${inverted ? "反转" : "正常"}”。此设置保存到 Flash Sector2。`);
  });
  $("#sendChargerType").addEventListener("click", () => {
    const charger_type = +$("#chargerType").value;
    confirmCommand("charger_type", { charger_type }, "切换充电机类型",
      `设置为“${charger_type ? "Chroma · 500 kbit/s" : "Legacy · 250 kbit/s"}”。实体充电按钮决定是否进入充电模式。`);
  });
  $("#clearFaultLog").addEventListener("click", () => {
    confirmCommand("log_clear", {}, "清除 Flash 故障日志",
      "发送独立的三字节确认请求。不会清除当前实时告警，也不会解除故障保持。", true);
  });
  $("#readFlashLog").addEventListener("click", async () => {
    $("#readFlashLog").disabled = true;
    const result = await state.api.read_flash_fault_logs(50);
    $("#readFlashLog").disabled = false;
    if (!result.ok) return toast(result.error || "读取 Flash 日志失败", true);
    toast(`已读取 ${result.records.length} / ${result.count} 条重要故障日志`);
    await poll();
  });
  $("#faultReset").addEventListener("click", () => confirmCommand("fault_reset", {}, "故障保持复位", "只有实时一级、二级告警已清除且 HV_ACC 释放时，主控才会执行复位。", true));
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
  ["#ovInput", "#uvInput", "#otInput", "#utInput"].forEach(id => $(id).addEventListener("input", () => state.dirty.thresholds = true));
  ["#chargeVoltage", "#chargeCurrent"].forEach(id => $(id).addEventListener("input", () => state.dirty.charge = true));
  $("#switchList").addEventListener("change", () => {
    state.dirty.switches = true;
    updateSwitchRowState();
  });
  $("#currentDirection").addEventListener("change", () => state.dirty.direction = true);
  $("#chargerType").addEventListener("change", () => state.dirty.chargerType = true);
  $("#onlyActiveAlarms").addEventListener("change", event => {
    state.onlyActiveAlarms = event.target.checked;
    renderAlarms();
  });
  $("#copyFaultCode").addEventListener("click", async () => {
    const code = $("#faultCode").textContent;
    try {
      await navigator.clipboard.writeText(code);
      toast(`已复制故障码 ${code}`);
    } catch {
      toast("剪贴板不可用，请手动选择复制", true);
    }
  });
  document.addEventListener("visibilitychange", () => schedulePoll(document.hidden ? 1000 : 0));
  window.addEventListener("resize", () => {
    if (state.page === "overview") drawTrend();
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
  const simulationEnabled = data?.simulation_enabled === true;
  const modeField = $("#connectModeField");
  const modeSelect = $("#connectMode");
  if (modeField) modeField.classList.toggle("hidden", !simulationEnabled);
  if (simulationEnabled && modeSelect && !modeSelect.querySelector('option[value="simulation"]')) {
    modeSelect.insertAdjacentHTML("beforeend", '<option value="simulation">内置模拟数据</option>');
  }
  if (!simulationEnabled && modeSelect) modeSelect.value = "pcan";
  $("#connectProfile").innerHTML = data.profiles.map(item => `<option value="${item.key}" data-bitrate="${item.bitrate}">${item.name}</option>`).join("");
  $("#connectChannel").innerHTML = data.channels.map(item => `<option>${item}</option>`).join("");
  updateConnectionDialog();
}

function updateConnectionDialog() {
  const simulation = state.bootstrap?.simulation_enabled === true && $("#connectMode").value === "simulation";
  $("#channelField").classList.toggle("hidden", simulation);
  const option = $("#connectProfile").selectedOptions[0];
  text("#connectBitrate", `${Number(option?.dataset.bitrate || 500000) / 1000} kbit/s`);
}

async function connectCan() {
  if (!state.api) return toast("应用后端未就绪", true);
  const option = $("#connectProfile").selectedOptions[0];
  const simulationEnabled = state.bootstrap?.simulation_enabled === true;
  $("#doConnect").disabled = true; text("#doConnect", "连接中…");
  $("#connectError").classList.add("hidden");
  const result = await state.api.connect_can({
    mode: simulationEnabled ? $("#connectMode").value : "pcan", bus_profile: $("#connectProfile").value,
    channel: $("#connectChannel").value, bitrate: Number(option.dataset.bitrate),
  });
  $("#doConnect").disabled = false; text("#doConnect", "连接");
  if (result.ok) {
    $("#connectDialog").close(); toast(result.connection.mode === "simulation" ? "模拟数据已启动" : "PCAN 已连接"); await poll();
  } else {
    text("#connectError", result.error); $("#connectError").classList.remove("hidden");
  }
}

async function disconnectCan() {
  if (!state.api) return;
  await state.api.disconnect_can();
  $("#connectDialog").close();
  toast("CAN 已断开");
  await poll();
}

function buildSwitchList() {
  const catalog = state.bootstrap?.switch_catalog || [];
  $("#switchList").innerHTML = catalog.map(item =>
    `<label class="switch-row" data-key="${item.key}" title="${item.name} · ${item.code} · ${item.variable}"><i class="switch-dot"></i>`
    + `<span class="switch-label"><b>${item.name}</b><small>${item.code}</small></span>`
    + `<b class="switch-state">等待</b><input type="checkbox" data-key="${item.key}"><span class="switch-track"></span></label>`
  ).join("");
}

/** Refresh the visible on/off state of every alarm switch row on the control page. */
function updateSwitchRowState() {
  const hasReport = Object.keys(state.snapshot?.config?.switches || {}).length > 0;
  $$("#switchList input").forEach(input => {
    const row = input.closest(".switch-row");
    const label = row.querySelector(".switch-state");
    const enabled = input.checked;
    row.classList.toggle("on", enabled);
    if (state.dirty.switches) {
      label.textContent = "待写入";
      row.classList.add("dirty");
    } else if (!hasReport) {
      label.textContent = "等待";
      row.classList.remove("dirty", "on");
    } else {
      label.textContent = enabled ? "启用" : "关闭";
      row.classList.remove("dirty");
    }
  });
}

function buildAlarmMatrix() {
  $("#alarmMatrix").innerHTML = Array.from({ length: 32 }, (_, i) =>
    `<div class="alarm-item" data-alarm="${i}" title="统一故障码 bit ${i}"><span><b>告警项 ${i}</b><small>等待主控状态</small></span><em class="alarm-level">未收到</em></div>`
  ).join("");
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
  } else if (state.page === "frames") {
    renderReplay();
    if (!state.framePaused) renderFrames();
  }
}

function renderConnection(connection) {
  state.recording = !!connection.recording;
  text("#recordButton", state.recording ? "■ 停止记录" : "● 记录数据");
  setClass("#recordButton", "active", state.recording);
  text("#recordingState", state.recording ? `● 正在记录 ${connection.recording.format === "bmslog" ? "BMSLOG" : "CSV"}` : "");
  text("#rxCount", connection.rx_count.toLocaleString()); text("#txCount", connection.tx_count.toLocaleString());
  setClass("#connectButton", "connected", connection.connected);
  setClass("#connectButton", "simulation", connection.mode === "simulation");
  setClass("#connectButton", "replay", connection.mode === "replay");
  const profileNames = { can1: "CAN1", canb: "CANB", canb_legacy: "CANB" };
  const modeName = connection.mode === "simulation" ? "内置模拟数据"
    : connection.mode === "replay" ? "历史回放" : connection.channel || "PCAN";
  $("#connectButton b").textContent = connection.connected ? modeName : "连接设备";
  $("#connectButton small").textContent = connection.connected
    ? `${profileNames[connection.bus_profile] || "CAN"} · ${(connection.bitrate || 500000) / 1000} kbit/s`
    : state.bootstrap?.simulation_enabled === true ? "选择连接方式" : "选择 PCAN 通道";
  $("#disconnectButton").classList.toggle("hidden", !connection.connected);

  const firmware = state.snapshot?.firmware || {};
  const firmwareText = [
    firmware.variant,
    firmware.git,
    firmware.build_date ? `构建 ${firmware.build_date}` : "",
  ].filter(Boolean).join(" · ") || "—";
  const firmwareNode = $("#firmwareIdentity");
  if (firmwareNode) {
    firmwareNode.textContent = firmwareText;
    firmwareNode.title = firmwareText;
  }
}

function renderOverview() {
  const { overview: o, relay, hv, imd, connection } = state.snapshot;
  const faultState = liveFaultState(state.snapshot);
  const overviewFresh = connection.summary_age != null && connection.summary_age <= 1.5;
  const stale = !overviewFresh;
  const stateName = !overviewFresh ? "等待 CAN 数据" : o.state_name || "等待 CAN 数据";
  text("#stateName", stateName);
  const stateNode = $("#stateName");
  if (stateNode) {
    stateNode.className = `machine-value ${!overviewFresh ? "stale" : MACHINE_STATE_CLASSES[o.state] || "unknown"}`;
    stateNode.title = overviewFresh ? `主控状态：${stateName}` : "等待主控状态";
  }
  const descriptions = { 2: "主控正在等待完整从控采样和 IVT U1。", 3: "高压未闭合，可以进行参数配置。", 4: "预充正在进行，配置命令将被主控忽略。", 5: "高压已闭合，优先监视电流、单体与告警。", 7: "故障已锁存，排除实时故障后再请求复位。" };
  text("#stateDescription", stale ? "主控周期状态帧缺失或已经超时。" : descriptions[o.state] || "正在读取主控状态。");
  text("#overviewClock", new Date().toLocaleTimeString("zh-CN", { hour12: false }));
  text("#overviewFaultCode", faultState.faultFresh ? faultState.fault.code_hex : "等待数据");
  text("#alarmLevelName", faultState.known ? o.alarm_level_name || "未知" : "等待数据");
  $("#alarmLevelName").className = !faultState.known || o.alarm_level == null ? "" : o.alarm_level === 1 ? "bad" : o.alarm_level === 2 ? "warn" : "ok";
  text("#packVoltage", fmt(o.voltage_v, 1)); text("#packCurrent", fmt(o.current_a, 1)); text("#packSoc", fmt(o.soc_pct, 0));
  $("#socBar").style.width = `${Math.max(0, Math.min(100, o.soc_pct || 0))}%`;
  const delta = o.max_cell_mv != null && o.min_cell_mv != null ? o.max_cell_mv - o.min_cell_mv : null;
  text("#cellDelta", fmt(delta, 0)); text("#cellExtremes", o.max_cell_mv == null ? "最高 / 最低 —" : `${o.max_cell_mv} / ${o.min_cell_mv} mV`);
  const sumDelta = o.voltage_v != null && o.cell_sum_v != null ? o.voltage_v - o.cell_sum_v : null;
  text("#cellSumDelta", o.cell_sum_v == null ? "单体累加 —" : `单体累加 ${fmt(o.cell_sum_v, 1)} V · 差 ${fmt(sumDelta, 1)} V`);
  const relayFresh = relay.command_age != null && relay.command_age <= 1.5;
  const hvFresh = hv.age != null && hv.age <= 1.5;
  text("#prechargeVoltage", relayFresh ? fmt(relay.precharge_voltage_v, 1) : "等待数据");
  const positive = hvFresh && hv.positive != null ? hv.positive : relayFresh ? relay.positive : null;
  const negative = hvFresh && hv.negative != null ? hv.negative : relayFresh ? relay.negative : null;
  setClass("#positiveRelay", "closed", positive); setClass("#negativeRelay", "closed", negative);
  text("#positiveRelay em", positive == null ? "—" : positive ? "闭合" : "断开");
  text("#negativeRelay em", negative == null ? "—" : negative ? "闭合" : "断开");
  const precharge = hvFresh && hv.precharge != null ? hv.precharge : relayFresh ? relay.precharge : null;
  setClass("#prechargeRelay", "closed", precharge);
  text("#prechargeRelay em", precharge == null ? "—" : precharge ? "闭合" : "断开");
  const hvOutputName = !overviewFresh ? "等待数据" : o.state === 4 ? "预充中" : o.state === 5 || (positive && negative) ? "已接通"
    : o.state === 7 ? "故障保持" : hvFresh ? "已断开" : "等待数据";
  text("#hvOutput", hvOutputName);
  setClass("#hvOutput", "ok", hvOutputName === "已接通");
  setClass("#hvOutput", "bad", hvOutputName === "故障保持");
  text("#hvAcc", !hvFresh ? "等待数据" : hv.hv_acc ? "请求" : "已释放");
  text("#chargeButton", !hvFresh ? "等待数据" : hv.charge_button ? "已按下" : "已释放");
  const safetyAlarm = (state.snapshot.alarms || []).find(item => item.index === 22);
  const safetyEventKnown = faultState.known;
  const safetyEventActive = !!(safetyAlarm?.level || (faultState.faultFresh && safetyAlarm?.in_fault_code));
  text("#safetyEvent", !safetyEventKnown ? "等待数据" : safetyEventActive ? "已触发" : "未触发");
  setClass("#safetyEvent", "bad", safetyEventKnown && safetyEventActive);
  setClass("#safetyEvent", "ok", safetyEventKnown && !safetyEventActive);
  const bmsFaultOutput = state.snapshot.fault?.flags?.bms_output_latched;
  text("#bmsFaultSignal", !faultState.faultFresh ? "等待数据" : bmsFaultOutput ? "已输出" : "未输出");
  setClass("#bmsFaultSignal", "bad", faultState.faultFresh && bmsFaultOutput);
  setClass("#bmsFaultSignal", "ok", faultState.faultFresh && !bmsFaultOutput);
  const prechargeResultName = !hvFresh ? "等待数据" : hv.precharge_result_name || "未发生";
  const prechargeTime = !hvFresh ? "等待数据" : hv.precharge_result === 2 ? `${hv.failure_ms} ms` : hv.success_ms ? `${hv.success_ms} ms` : "—";
  text("#lastPrechargeResult", prechargeResultName);
  text("#prechargeTime", prechargeTime);
  text("#prechargeResult", !hvFresh ? "等待数据" : hv.precharge_result_name || "未发生预充");
  $("#prechargeResult").className = `state-note ${hvFresh && hv.precharge_result === 1 ? "ok" : hvFresh && hv.precharge_result === 2 ? "bad" : ""}`;
  renderThermal(o, relay);
  renderImd(imd);
  drawTrend();
}

function renderThermal(overview, relay) {
  const thermalFresh = relay.thermal_age != null && relay.thermal_age <= 1.5;
  text("#maxTemp", !thermalFresh || overview.max_temp_c == null ? "等待数据" : `${overview.max_temp_c} °C`);
  text("#minTemp", !thermalFresh || overview.min_temp_c == null ? "等待数据" : `${overview.min_temp_c} °C`);
  text("#maxTempNo", !thermalFresh || overview.max_temp_no == null ? "" : `T${overview.max_temp_no}`);
  text("#minTempNo", !thermalFresh || overview.min_temp_no == null ? "" : `T${overview.min_temp_no}`);
  const spread = thermalFresh && overview.max_temp_c != null && overview.min_temp_c != null
    ? overview.max_temp_c - overview.min_temp_c : null;
  text("#tempDelta", spread == null ? "等待数据" : `${spread} °C`);
  text("#fanDuty", !thermalFresh || relay.fan_duty_pct == null ? "等待数据" : `${relay.fan_duty_pct} %`);
  text("#fanRpm", !thermalFresh || relay.fan_rpm == null ? "—" : `${relay.fan_rpm} rpm`);
  text("#coolingTag", !thermalFresh ? "等待数据" : relay.cooling == null ? "目标 —" : relay.cooling ? "目标：请求" : "目标：关闭");
  $("#coolingTag").className = `state-text ${thermalFresh && relay.cooling ? "ok" : ""}`;
  const fanFlags = relay.fan_flags;
  let fanStatus = "等待数据";
  if (thermalFresh && fanFlags != null) {
    const commandOn = (fanFlags & 0x02) !== 0;
    const tachMoving = (fanFlags & 0x04) !== 0;
    const stalled = (fanFlags & 0x10) !== 0;
    fanStatus = stalled ? "停转故障" : commandOn && tachMoving ? "运行" : commandOn ? "等待转速" : "未请求";
  }
  text("#fanStatus", fanStatus);
  $("#fanStatus").className = !thermalFresh ? "" : fanStatus === "停转故障" ? "bad" : fanStatus === "运行" || fanStatus === "未请求" ? "ok" : "";
}

function renderImd(imd) {
  const fresh = imd.age != null && imd.age <= 1.5;
  const status = !fresh ? "等待数据" : imd.status_name || "等待数据";
  text("#imdStatus", status);
  $("#imdStatus").className = !fresh ? "" : imd.status === 0 ? "ok" : "bad";
  $("#imdStatus").title = fresh ? "IMD 综合诊断状态。" : "等待 IMD 数据。";

  const digital = !fresh ? "等待数据" : imd.digital_ok ? "正常" : "故障";
  text("#imdDigital", digital);
  $("#imdDigital").className = !fresh ? "" : imd.digital_ok ? "ok" : "bad";

  const frequency = !fresh || !imd.pwm_signal_ok || imd.frequency_hz == null ? "—" : `${fmt(imd.frequency_hz, 2)} Hz`;
  const frequencyNote = !fresh ? "" : IMD_FREQUENCY_NOTES[imd.frequency_class] || "PWM";
  text("#imdFrequency", frequency);
  text("#imdFrequencyNote", frequencyNote);
  $("#imdFrequency").className = !fresh || !imd.pwm_signal_ok ? "" : "ok";
  $("#imdFrequency").title = fresh ? `IMD 实际 PWM 频率 · ${frequencyNote}` : "等待 IMD 数据。";

  const resistanceMode = imd.frequency_class === 1 || imd.frequency_class === 2;
  const resistance = !fresh || !resistanceMode || !imd.insulation_valid ? "—"
    : imd.resistance_saturated ? "≥65535" : fmt(imd.resistance_kohm, 0);
  text("#imdResistance", resistance);
  text("#imdDuty", !fresh || !imd.pwm_signal_ok || imd.duty_pct == null ? "—" : `${fmt(imd.duty_pct, 1)} %`);
}

function renderModules() {
  const modules = state.snapshot.modules || [];
  const cells = state.snapshot.cells || [], temps = state.snapshot.temps || [];
  $("#moduleAverages").innerHTML = modules.map(item => {
    const cellValues = cells.filter(cell => cell.module === item.no && cell.value != null).map(cell => cell.value);
    const tempValues = temps.filter(temp => temp.module === item.no && temp.value != null).map(temp => temp.value);
    const avgCell = cellValues.length ? cellValues.reduce((sum, value) => sum + value, 0) / cellValues.length : null;
    const avgTemp = tempValues.length ? tempValues.reduce((sum, value) => sum + value, 0) / tempValues.length : null;
    const delta = cellValues.length ? Math.max(...cellValues) - Math.min(...cellValues) : null;
    return `<div class="${item.online ? "online" : "offline"}"><b>M${item.no}</b><span>${fmt(avgCell, 0)} mV</span>`
      + `<span>${fmt(avgTemp, 1)} °C</span><span>${fmt(delta, 0)} mV</span><em>${item.online ? "正常" : "缺失"}</em></div>`;
  }).join("");
  const online = modules.filter(item => item.online).length;
  text("#moduleSummary", `${online}/6 正常`);
  $("#moduleSummary").className = `state-text ${online === 6 ? "ok" : "bad"}`;
}

function durationLabel(totalSeconds) {
  const seconds = Math.max(0, Math.round(totalSeconds || 0));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor(seconds % 3600 / 60);
  const remainder = seconds % 60;
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
}

function renderChargeTiming(overview, connection, fault) {
  const timing = state.chargeTiming;
  if (connection.mode === "replay") {
    text("#chargeTimingState", "历史回放");
    text("#chargeElapsed", "—"); text("#chargeRemaining", "—");
    return;
  }

  const now = Date.now();
  const faultFresh = fault.received === true && fault.age != null && fault.age <= 1.5;
  const summaryFresh = connection.summary_age != null && connection.summary_age <= 1.5;
  if (!faultFresh || !summaryFresh) {
    timing.lastTickMs = null;
    text("#chargeTimingState", "等待数据");
    text("#chargeElapsed", "—"); text("#chargeRemaining", "—");
    return;
  }

  const chargeActive = !!fault.flags?.charge_mode;
  const fresh = true;
  const chargeCurrent = Number(overview.current_a);

  if (chargeActive && !timing.active) {
    timing.active = true;
    timing.elapsedMs = 0;
    timing.lastTickMs = now;
    timing.averageCurrentA = null;
    timing.currentSumA = 0;
    timing.currentSamples = 0;
  } else if (!chargeActive && timing.active) {
    timing.active = false;
    timing.lastTickMs = null;
  }

  if (timing.active) {
    if (fresh && timing.lastTickMs != null) timing.elapsedMs += Math.min(1500, now - timing.lastTickMs);
    timing.lastTickMs = now;
    if (fresh && Number.isFinite(chargeCurrent) && chargeCurrent > 0.05) {
      timing.currentSumA += chargeCurrent;
      timing.currentSamples++;
      timing.averageCurrentA = timing.currentSumA / timing.currentSamples;
    }
  }

  text("#chargeTimingState", chargeActive ? fresh ? "充电计时中" : "状态帧超时" : timing.elapsedMs ? "本次已停止" : "未充电");
  text("#chargeElapsed", durationLabel(timing.elapsedMs / 1000));

  const soc = Number(overview.soc_pct);
  const estimateCurrent = timing.averageCurrentA;
  if (!chargeActive || !fresh || !Number.isFinite(soc) || estimateCurrent == null || estimateCurrent <= .1) {
    text("#chargeRemaining", "—");
    return;
  }

  const remainingAh = PACK_CAPACITY_AH * Math.max(0, 100 - soc) / 100;
  const remainingSeconds = Math.min(7 * 86400, remainingAh / estimateCurrent * 3600);
  if (remainingAh <= .01) {
    text("#chargeRemaining", "已充满");
  } else {
    text("#chargeRemaining", durationLabel(remainingSeconds));
  }
}

const TREND_COLORS = { voltage: "#42c4d5", current: "#f0b429", precharge: "#8ab4f8" };

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

function drawTrend() {
  const canvas = $("#trendCanvas"), trends = state.snapshot?.trends || [];
  if (!canvas || !canvas.clientWidth || !canvas.clientHeight) return;
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth, height = canvas.clientHeight;
  const targetWidth = Math.round(width * ratio), targetHeight = Math.round(height * ratio);
  if (canvas.width !== targetWidth || canvas.height !== targetHeight) {
    canvas.width = targetWidth; canvas.height = targetHeight;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const latest = trends[trends.length - 1] || {};
  text("#trendVoltage", Number.isFinite(latest.voltage) ? `${fmt(latest.voltage, 1)} V` : "—");
  text("#trendCurrent", Number.isFinite(latest.current) ? `${fmt(latest.current, 1)} A` : "—");
  text("#trendPrecharge", Number.isFinite(latest.precharge) ? `${fmt(latest.precharge, 1)} V` : "—");

  const pad = { left: 48, right: 48, top: 12, bottom: 24 };
  const plotWidth = width - pad.left - pad.right, plotHeight = height - pad.top - pad.bottom;
  const axisFont = '10px "SF Mono", "Cascadia Mono", Consolas, monospace';
  const labelColor = "#718087", gridColor = "#223039";

  // Horizontal grid, five bands, plus the left/right axis tick labels.
  const left = trendAxisRange(trends.flatMap(item => [item.voltage, item.precharge]).filter(Number.isFinite), 8);
  const right = trendAxisRange(trends.map(item => item.current).filter(Number.isFinite), 6);
  ctx.font = axisFont;
  for (let row = 0; row <= 4; row++) {
    const y = Math.round(pad.top + plotHeight * row / 4) + .5;
    ctx.strokeStyle = gridColor; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke();
    if (trends.length >= 2) {
      ctx.fillStyle = labelColor;
      if (left && Number.isFinite(left.min)) {
        ctx.textAlign = "right";
        ctx.fillText(`${(left.max - (left.max - left.min) * row / 4).toFixed(1)}`, pad.left - 7, y + 3);
      }
      if (right && Number.isFinite(right.min)) {
        ctx.textAlign = "left";
        ctx.fillText(`${(right.max - (right.max - right.min) * row / 4).toFixed(1)}`, width - pad.right + 7, y + 3);
      }
    }
  }
  ctx.fillStyle = labelColor; ctx.textAlign = "left"; ctx.font = axisFont;
  ctx.fillText("V", 2, pad.top + 3);
  ctx.textAlign = "right"; ctx.fillText("A", width - 2, pad.top + 3);

  if (trends.length < 2) {
    ctx.fillStyle = "#87969c";
    ctx.font = '12px "PingFang SC", "Microsoft YaHei UI", sans-serif';
    ctx.textAlign = "center";
    ctx.fillText("等待状态帧形成曲线", pad.left + plotWidth / 2, pad.top + plotHeight / 2);
    return;
  }

  // Time axis: five evenly spaced labels over the recorded window.
  const t0 = trends[0].t, t1 = trends[trends.length - 1].t;
  const span = Math.max(1, t1 - t0);
  const timeLabel = seconds => {
    const total = Math.max(0, Math.round(seconds));
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
  };
  ctx.font = axisFont; ctx.fillStyle = labelColor; ctx.textAlign = "center";
  for (let col = 0; col <= 4; col++) {
    const x = pad.left + plotWidth * col / 4;
    ctx.strokeStyle = gridColor; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(Math.round(x) + .5, pad.top); ctx.lineTo(Math.round(x) + .5, pad.top + plotHeight); ctx.stroke();
    ctx.fillText(col === 4 ? "现在" : timeLabel(t0 + span * col / 4), x, height - 7);
  }

  const xOf = item => pad.left + plotWidth * (item.t - t0) / span;
  const yLeft = left ? value => pad.top + plotHeight * (left.max - value) / (left.max - left.min) : null;
  const yRight = right ? value => pad.top + plotHeight * (right.max - value) / (right.max - right.min) : null;

  // Dashed zero line on the current axis when the window crosses zero.
  if (right && Number.isFinite(right.min) && right.min < 0 && right.max > 0) {
    const y = Math.round(yRight(0)) + .5;
    ctx.strokeStyle = "#4a5b65"; ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke();
    ctx.setLineDash([]);
  }

  const drawSeries = (key, color, yOf, fill) => {
    const points = [];
    trends.forEach(item => {
      if (!Number.isFinite(item[key])) return;
      points.push({ x: xOf(item), y: yOf(item[key]) });
    });
    if (points.length < 2) return;
    ctx.beginPath();
    points.forEach((point, index) => index ? ctx.lineTo(point.x, point.y) : ctx.moveTo(point.x, point.y));
    ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.lineCap = "round";
    ctx.stroke();
    if (fill) {
      const gradient = ctx.createLinearGradient(0, pad.top, 0, pad.top + plotHeight);
      gradient.addColorStop(0, fill); gradient.addColorStop(1, "rgba(0,0,0,0)");
      ctx.lineTo(points[points.length - 1].x, pad.top + plotHeight);
      ctx.lineTo(points[0].x, pad.top + plotHeight);
      ctx.closePath();
      ctx.fillStyle = gradient; ctx.fill();
    }
    const last = points[points.length - 1];
    ctx.beginPath(); ctx.arc(last.x, last.y, 2.5, 0, Math.PI * 2);
    ctx.fillStyle = color; ctx.fill();
  };

  if (left) {
    drawSeries("precharge", TREND_COLORS.precharge, yLeft, "rgba(138, 180, 248, .10)");
    drawSeries("voltage", TREND_COLORS.voltage, yLeft, "rgba(66, 196, 213, .13)");
  }
  if (right) drawSeries("current", TREND_COLORS.current, yRight, null);
}

function renderAlarmSummary() {
  const alarms = state.snapshot.alarms || [];
  const faultState = liveFaultState(state.snapshot);
  text("#faultCode", faultState.faultFresh ? faultState.fault.code_hex : "等待数据");
  const activeItems = alarms.filter(item => (faultState.alarmLevelsFresh && item.level) || (faultState.faultFresh && item.in_fault_code));
  const activeCount = activeItems.length;
  text("#alarmNavBadge", activeCount); $("#alarmNavBadge").classList.toggle("hidden", activeCount === 0);
  const alarmCountText = activeCount ? `${activeCount} 项` : faultState.known ? "无" : "等待数据";
  text("#alarmActiveCount", alarmCountText);
  text("#alarmPageLevel", faultState.known ? state.snapshot.overview?.alarm_level_name || "未知" : "等待数据");
  $("#alarmPageLevel")?.classList.toggle("bad", faultState.known && state.snapshot.overview?.alarm_level === 1);
  $("#alarmPageLevel")?.classList.toggle("warn", faultState.known && state.snapshot.overview?.alarm_level === 2);
  text("#activeAlarmCount", alarmCountText);
  const active = activeItems.slice(0, 3);
  $("#activeAlarmBrief").className = active.length ? "fault-brief" : "fault-brief empty-state";
  $("#activeAlarmBrief").innerHTML = active.length ? active.map(item => `<div class="brief-alarm ${item.level === 1 ? "lv1" : ""}"><b>${item.name}</b><span>${item.level_name}</span></div>`).join("")
    : faultState.known ? "当前没有活动告警" : "未收到告警状态帧";
}

function alarmRuleText(index, thresholds) {
  if (index === 0) return thresholds.ov_mv == null ? "阈值未回报 · 确认270 ms" : `≥${thresholds.ov_mv} mV · 确认270 ms`;
  if (index === 1) return thresholds.uv_mv == null ? "阈值未回报 · 确认270 ms" : `≤${thresholds.uv_mv} mV · 确认270 ms`;
  if (index === 2) {
    if (thresholds.ot_c == null) return "阈值未回报 · 确认780 ms";
    return `≥${thresholds.ot_c} °C · 充电时${Math.min(thresholds.ot_c, 50)} °C · 780 ms`;
  }
  if (index === 3) {
    if (thresholds.ut_c == null) return "阈值未回报 · 确认780 ms";
    return `≤${thresholds.ut_c} °C · 充电时${Math.max(thresholds.ut_c, 5)} °C · 780 ms`;
  }
  return ALARM_RULE_TEXTS[index] || "按主控当前程序判定";
}

function renderAlarms() {
  const alarms = state.snapshot.alarms || [];
  const faultState = liveFaultState(state.snapshot);
  const thresholds = state.snapshot.config?.thresholds || {};
  alarms.forEach(alarm => {
    const node = $(`[data-alarm="${alarm.index}"]`); if (!node) return;
    const levelReceived = faultState.alarmLevelsFresh;
    const faultCodeActive = faultState.faultFresh && alarm.in_fault_code;
    const active = !!((levelReceived && alarm.level) || faultCodeActive);
    const classes = ["alarm-item"];
    if (levelReceived && alarm.level === 1) classes.push("lv1");
    else if (levelReceived && alarm.level === 2) classes.push("lv2");
    if (faultCodeActive) classes.push("fc");
    if (!levelReceived && !faultCodeActive) classes.push("pending");
    if (alarm.index === 14 || alarm.index === 15) classes.push("reserved");
    if (state.onlyActiveAlarms && !active) classes.push("hidden");
    node.className = classes.join(" ");
    node.querySelector("b").textContent = alarm.name;
    node.querySelector("small").textContent = alarmRuleText(alarm.index, thresholds);
    node.querySelector("em").textContent = alarm.index === 22 && ((levelReceived && alarm.level) || faultCodeActive) ? "事件触发"
      : levelReceived && alarm.level === 1 ? "一级故障"
      : levelReceived && alarm.level === 2 ? "二级告警"
      : faultCodeActive ? "故障码置位"
      : !levelReceived ? "等待等级"
      : alarm.index === 14 || alarm.index === 15 ? "未启用" : "正常";
  });
  const history = state.snapshot.fault_history || [];
  $("#faultHistory").innerHTML = history.length ? history.map(event => `<div class="event-item"><time>${event.time}</time><b>${event.previous} → ${event.code}</b><p>${event.added.length ? `<span class="added">进入：${event.added.join("、")}</span>` : ""}${event.added.length && event.cleared.length ? "<br>" : ""}${event.cleared.length ? `<span class="cleared">清除：${event.cleared.join("、")}</span>` : ""}</p></div>`).join("") : `<div class="empty-state">故障码发生变化后在这里显示进入和清除记录。</div>`;
  const flashRecords = state.snapshot.flash_log_records || [];
  const logClearPending = !!state.snapshot.fault?.flags?.log_clear_pending;
  $("#readFlashLog").disabled = logClearPending;
  $("#clearFaultLog").disabled = logClearPending;
  $("#flashFaultLog").innerHTML = flashRecords.length ? [...flashRecords].reverse().map(event =>
    `<div class="event-item"><time>${event.timestamp}</time><b>${event.fault_code}</b><p>类型 ${event.event_type} · 详情 ${event.event_detail}</p></div>`
  ).join("") : `<div class="empty-state">尚未读取，或 Flash 中没有重要故障日志。</div>`;
  const logInfo = state.snapshot.flash_log_info || {};
  text("#flashLogInfo", logClearPending ? "正在分阶段清除"
    : logInfo.count == null ? "未读取"
    : `主控记录 ${logInfo.count} 条 · 丢弃 ${logInfo.dropped} 条 · 已显示 ${flashRecords.length} 条`);
}

function renderConfig() {
  const config = state.snapshot.config || {}, thresholds = config.thresholds || {}, switches = config.switches || {};
  text("#displayOv", thresholds.ov_mv == null ? "—" : `${thresholds.ov_mv} mV`);
  text("#displayUv", thresholds.uv_mv == null ? "—" : `${thresholds.uv_mv} mV`);
  text("#displayOt", thresholds.ot_c == null ? "—" : `${thresholds.ot_c} °C`);
  text("#displayUt", thresholds.ut_c == null ? "—" : `${thresholds.ut_c} °C`);
  if (Object.keys(thresholds).length) {
    text("#thresholdSync", state.dirty.thresholds ? "页面值待写入" : "已从主控同步");
    $("#thresholdSync").className = `tag ${state.dirty.thresholds ? "warn" : "ok"}`;
    if (!state.dirty.thresholds && (!state.inputsInitialized.thresholds || ![$("#ovInput"), $("#uvInput"), $("#otInput"), $("#utInput")].includes(document.activeElement))) {
      $("#ovInput").value = thresholds.ov_mv; $("#uvInput").value = thresholds.uv_mv;
      $("#otInput").value = thresholds.ot_c; $("#utInput").value = thresholds.ut_c;
      state.inputsInitialized.thresholds = true;
    }
  }
  text("#switchVersion", config.switch_version == null ? "回报 V—" : `回报 V${config.switch_version}`);
  const statusList = $("#switchStatusList");
  if (statusList && state.bootstrap?.switch_catalog) {
    const hasSwitchReport = Object.keys(switches).length > 0;
    const enabledCount = state.bootstrap.switch_catalog.filter(item => switches[item.key]).length;
    text("#switchStatusSummary", hasSwitchReport ? `启用 ${enabledCount} / ${state.bootstrap.switch_catalog.length}` : "等待回报");
    statusList.innerHTML = state.bootstrap.switch_catalog.map(item => {
      const enabled = hasSwitchReport && !!switches[item.key];
      const stateClass = !hasSwitchReport ? "pending" : enabled ? "enabled" : "disabled";
      const stateText = !hasSwitchReport ? "等待" : enabled ? "启用" : "关闭";
      return `<span class="protection-switch ${stateClass}" title="${item.name} · ${item.code} · ${item.variable}">`
        + `<i></i><span class="protection-label"><b>${item.name}</b><small>${item.code}</small></span><b>${stateText}</b></span>`;
    }).join("");
  }
  if (Object.keys(switches).length && !state.dirty.switches && (!state.inputsInitialized.switches || !$("#switchList").contains(document.activeElement))) {
    $$("#switchList input").forEach(input => input.checked = !!switches[input.dataset.key]);
    state.inputsInitialized.switches = true;
  }
  updateSwitchRowState();
  if (config.current_direction_inverted != null && !state.dirty.direction && document.activeElement !== $("#currentDirection")) {
    $("#currentDirection").value = config.current_direction_inverted ? "1" : "0";
  }
}

function watchFlashSave(command, ack) {
  const kind = SAVE_KINDS[command];
  if (!kind) return;
  const flags = ack?.flags || {};
  const pendingKey = kind === "direction" ? "current_direction_save_pending" : "config_save_pending";
  const pending = !!flags[pendingKey];
  state.saveWatch = {
    command, kind, startedAt: Date.now(), ackPending: pending,
    pendingSeen: false, noWrite: ack?.result === 0 && !pending, confirmedAt: null,
  };
}

function renderSaveStatus(runtime) {
  const node = $("#saveStatus");
  if (!node) return;
  const fresh = runtime.age != null && runtime.age <= 1.5;
  const configPending = !!runtime.config_save_pending;
  const directionPending = !!runtime.current_direction_save_pending;
  const pending = configPending || directionPending;
  const watch = state.saveWatch;
  const now = Date.now();
  const watchPending = watch && (watch.kind === "direction" ? directionPending : configPending);
  if (watchPending) watch.pendingSeen = true;
  let label = "等待保存状态";
  let className = "neutral";
  let title = "等待主控确认 Flash 状态。";

  if (!fresh) {
    label = "等待保存状态";
  } else if (!runtime.flash_ready) {
    label = "Flash 离线";
    className = "bad";
    title = "外置 Flash 当前不可用。";
  } else if (watch?.noWrite) {
    label = `${SAVE_LABELS[watch.command]}已接受 · 无待保存`;
    className = "ok";
    title = "当前值无需再次写入，暂无新的保存任务。";
    if (!watch.confirmedAt) watch.confirmedAt = now;
  } else if (watch && watchPending) {
    const overdue = now - watch.startedAt > 5000;
    label = overdue ? "Flash 保存未完成" : "等待 Flash 保存";
    className = overdue ? "bad" : "warn";
    title = overdue ? "写入尚未完成，请检查外置 Flash 或主控状态。"
      : `“${SAVE_LABELS[watch.command]}”写入已接受，正在保存到外置 Flash。`;
  } else if (watch && !watch.noWrite && watch.ackPending && !watch.pendingSeen && now - watch.startedAt < 1000) {
    label = "等待 Flash 保存";
    className = "warn";
    title = `“${SAVE_LABELS[watch.command]}”写入已接受，正在等待保存结果。`;
  } else if (watch && !watch.confirmedAt) {
    watch.confirmedAt = now;
    label = "已保存到 Flash";
    className = "ok";
    title = "主控已确认写入完成。";
    toast(`${SAVE_LABELS[watch.command]}已确认保存到外置 Flash`);
  } else if (watch?.confirmedAt && now - watch.confirmedAt <= 6000) {
    label = "已保存到 Flash";
    className = "ok";
    title = "主控已确认写入完成。";
  } else if (pending) {
    label = "等待 Flash 保存";
    className = "warn";
    title = "有配置正在保存到外置 Flash。";
  } else {
    label = "Flash 就绪 · 无待保存";
    className = "ok";
    title = "外置 Flash 可用，当前没有待保存配置。";
  }

  if (watch?.confirmedAt && now - watch.confirmedAt > 6000) state.saveWatch = null;
  text("#saveStatus", label);
  node.className = `tag ${className}`;
  node.title = title;
}

function renderControls() {
  const { relay, fault, connection, overview } = state.snapshot;
  if (relay.request_voltage_v != null && !state.dirty.charge && (!state.inputsInitialized.charge || ![$("#chargeVoltage"), $("#chargeCurrent")].includes(document.activeElement))) {
    $("#chargeVoltage").value = relay.request_voltage_v; $("#chargeCurrent").value = relay.request_current_a; state.inputsInitialized.charge = true;
  }
  const requestText = relay.request_voltage_v == null || relay.request_current_a == null
    ? "— V / — A"
    : `${fmt(relay.request_voltage_v, 1)} V / ${fmt(relay.request_current_a, 1)} A`;
  text("#chargeRequestEcho", requestText);
  const runtime = state.snapshot.runtime_diag || {};
  renderSaveStatus(runtime);
  if (state.snapshot.config?.charger_type != null && !state.dirty.chargerType && document.activeElement !== $("#chargerType")) {
    $("#chargerType").value = String(state.snapshot.config.charger_type);
  }
  const runtimeFresh = runtime.age != null && runtime.age <= 1.5;
  if (runtimeFresh && runtime.charger_feedback_voltage_v != null && runtime.charger_feedback_current_a != null && runtime.charger_feedback_fresh) {
    text("#chargeFeedbackEcho", `${fmt(runtime.charger_feedback_voltage_v, 1)} V / ${fmt(runtime.charger_feedback_current_a, 1)} A`);
  } else if (runtimeFresh && (runtime.charger_feedback_voltage_v != null || runtime.charger_feedback_current_a != null)) {
    text("#chargeFeedbackEcho", "反馈超时");
  } else {
    text("#chargeFeedbackEcho", "等待数据");
  }
  const charge = fault.flags?.charge_mode;
  const chargeLabel = charge == null ? "模式未知" : charge ? `充电 · ${fault.flags.charger_type}` : "放电 / 待机";
  text("#chargeModeTag", chargeLabel);
  $("#chargeModeTag").className = `tag ${charge == null ? "neutral" : charge ? "warn" : "neutral"}`;
  text("#heroChargeMode", chargeLabel);
  renderRtcReply(state.snapshot.rtc_reply || {});
  const connectedCan1 = connection.connected && connection.bus_profile === "can1";
  const allowedState = [2, 3, 7].includes(overview.state);
  const fresh = connection.summary_age != null && connection.summary_age <= 1.5;
  setClass("#lockConnected", "ok", connectedCan1); setClass("#lockState", "ok", allowedState); setClass("#lockFresh", "ok", fresh);
}

const RTC_STATUS_NAMES = {
  0: "设置成功", 1: "请求帧长度错误", 2: "校验错误",
  3: "日期时间参数非法", 4: "RTC 未就绪", 5: "设置时间或日期失败",
};

function renderRtcReply(reply) {
  if (reply.status == null) return text("#rtcReply", "—");
  const name = RTC_STATUS_NAMES[reply.status] || `未知 ${reply.status}`;
  if (!reply.year) return text("#rtcReply", `${name} · 时间读取失败`);
  const pad = value => String(value).padStart(2, "0");
  text("#rtcReply", `${name} · ${reply.year}-${pad(reply.month)}-${pad(reply.day)} ${pad(reply.hour)}:${pad(reply.minute)}:${pad(reply.second)}`);
}

/** Classify one cell or temperature against the thresholds currently reported by the master. */
function cellStatus(item, voltage, thresholds) {
  if (item.value == null) return "invalid";
  if (voltage) {
    if (thresholds.uv_mv != null && item.value <= thresholds.uv_mv) return "low";
    if (thresholds.ov_mv != null && item.value >= thresholds.ov_mv) return "high";
  } else {
    if (thresholds.ut_c != null && item.value <= thresholds.ut_c) return "low";
    if (thresholds.ot_c != null && item.value >= thresholds.ot_c) return "high";
  }
  return "";
}

function extremes(items) {
  const values = items.filter(item => item.value != null).map(item => item.value);
  return values.length
    ? { max: Math.max(...values), min: Math.min(...values), valid: values.length }
    : { max: null, min: null, valid: 0 };
}

/** Badge counts every cell and temperature the master is not currently reporting as valid. */
function renderCellBadge() {
  const cells = state.snapshot?.cells || [], temps = state.snapshot?.temps || [];
  const missing = cells.filter(item => item.value == null).length + temps.filter(item => item.value == null).length;
  text("#cellNavBadge", missing);
  $("#cellNavBadge").classList.toggle("hidden", missing === 0);
}

function renderCells() {
  if (!state.snapshot) return;
  const cells = state.snapshot.cells || [], temps = state.snapshot.temps || [];
  const cellTotal = extremes(cells), tempTotal = extremes(temps);
  text("#gridMax", cellTotal.max == null ? "—" : `${cellTotal.max} mV`);
  text("#gridMin", cellTotal.min == null ? "—" : `${cellTotal.min} mV`);
  text("#gridDelta", cellTotal.max == null ? "—" : `${cellTotal.max - cellTotal.min} mV`);
  text("#tempGridMax", tempTotal.max == null ? "—" : `${tempTotal.max} °C`);
  text("#tempGridMin", tempTotal.min == null ? "—" : `${tempTotal.min} °C`);
  text("#tempGridDelta", tempTotal.max == null ? "—" : `${fmt(tempTotal.max - tempTotal.min, 1)} °C`);
  const missingCells = cells.length - cellTotal.valid;
  const missingTemps = temps.length - tempTotal.valid;
  const dataIssue = $("#cellDataIssue");
  dataIssue.classList.toggle("hidden", missingCells === 0 && missingTemps === 0);
  dataIssue.textContent = `缺失 ${missingCells} 串 / ${missingTemps} 路`;
  const thresholds = state.snapshot.config.thresholds || {};
  text("#displayOv", thresholds.ov_mv == null ? "—" : `${thresholds.ov_mv} mV`);
  text("#displayUv", thresholds.uv_mv == null ? "—" : `${thresholds.uv_mv} mV`);
  text("#displayOt", thresholds.ot_c == null ? "—" : `${thresholds.ot_c} °C`);
  text("#displayUt", thresholds.ut_c == null ? "—" : `${thresholds.ut_c} °C`);
  const onlyAbnormal = $("#onlyAbnormal").checked;
  const renderItem = (item, voltage, localNo) => {
    const status = cellStatus(item, voltage, thresholds);
    if (onlyAbnormal && !status) return "";
    const label = voltage ? "C" : "T", unit = voltage ? "mV" : "°C";
    return `<div class="cell-item ${status}" title="${item.status} · 最近数据 ${item.age ?? "—"} s">`
      + `<small>${label}${localNo}</small><b>${item.value ?? "—"}<em>${unit}</em></b></div>`;
  };
  const modules = Array.from({ length: 6 }, (_, index) => {
    const cellGroup = cells.filter(item => item.module === index + 1);
    const tempGroup = temps.filter(item => item.module === index + 1);
    const module = state.snapshot.modules[index] || {};
    const cellStats = extremes(cellGroup), tempStats = extremes(tempGroup);
    const cellMarkup = cellGroup.map((item, itemIndex) => renderItem(item, true, itemIndex + 1)).join("");
    const tempMarkup = tempGroup.map((item, itemIndex) => renderItem(item, false, itemIndex + 1)).join("");
    if (onlyAbnormal && !cellMarkup && !tempMarkup) return "";
    const missing = cellGroup.length - cellStats.valid + tempGroup.length - tempStats.valid;
    const moduleIssue = !module.online
      ? `<span class="module-issue">通信中断</span>`
      : missing > 0 ? `<span class="module-issue">缺失 ${missing} 项</span>` : "";
    return `<section class="cell-module"><div class="cell-module-head">`
      + `<strong>BMU ${index + 1}</strong>`
      + moduleIssue
      + `<span class="module-stats"><span>电压 <b>${cellStats.min ?? "—"}–${cellStats.max ?? "—"} mV</b></span>`
      + `<span>压差 <b>${cellStats.max == null ? "—" : cellStats.max - cellStats.min} mV</b></span>`
      + `<span>温度 <b>${tempStats.min == null ? "—" : fmt(tempStats.min, 1)}–${tempStats.max == null ? "—" : fmt(tempStats.max, 1)} °C</b></span></span>`
      + `</div><div class="combined-module-body"><div aria-label="BMU ${index + 1} 电压"><div class="cell-grid voltage-grid">${cellMarkup}</div></div>`
      + `<div aria-label="BMU ${index + 1} 温度"><div class="cell-grid temperature-grid">${tempMarkup}</div></div></div></section>`;
  }).join("");
  $("#cellModules").innerHTML = modules || `<div class="filter-empty"><b>当前没有异常或缺失数据</b><span>138 串电压与 48 路温度均在当前阈值范围内。</span></div>`;
}

function renderFrames() {
  if (!state.snapshot) return;
  const frames = state.framePaused ? state.pausedFrames : state.snapshot.raw_frames;
  const query = $("#frameSearch").value.trim().toLowerCase();
  const filtered = frames.filter(frame => (state.frameKind === "all" || frame.direction === state.frameKind) && (!query || `${frame.id} ${frame.name} ${frame.data}`.toLowerCase().includes(query))).slice(0, 180);
  $("#frameRows").innerHTML = filtered.map(frame => `<tr class="${frame.direction}"><td>${frame.time}</td>`
    + `<td><span class="dir-tag ${frame.direction}">${frame.direction.toUpperCase()}</span></td>`
    + `<td>${frame.id}</td><td>${frame.extended ? "扩展" : "标准"}</td><td>${frame.dlc}</td>`
    + `<td title="${frame.data}">${frame.data}</td><td title="${frame.name}">${frame.name}</td></tr>`).join("");
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

function confirmCommand(name, values, title, message, destructive = false) {
  const conn = state.snapshot?.connection;
  if (!conn?.connected) return toast("请先连接 CAN", true);
  if (conn.mode === "replay") return toast("历史回放为只读，不能发送命令", true);
  if (conn.bus_profile !== "can1") return toast("工具命令只能从 CAN1 发送", true);
  state.pendingCommand = { name, values };
  text("#confirmTitle", title); text("#confirmMessage", message);
  text("#confirmPayload", `通道：${conn.channel || "PCAN"}\n`
    + `总线：CAN1 · ${conn.bitrate ? conn.bitrate / 1000 : 500} kbit/s\n`
    + `主控状态：${state.snapshot.overview.state_name}\n`
    + `告警等级：${state.snapshot.overview.alarm_level_name}\n`
    + `命令：${name}`);
  $("#confirmCheck").checked = false; $("#doConfirm").disabled = true;
  $("#doConfirm").className = destructive ? "danger-button" : "action-button";
  $("#confirmDialog").showModal();
}

async function sendPendingCommand() {
  if (!state.pendingCommand || !state.api) return;
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

document.addEventListener("DOMContentLoaded", init);
