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
  projectName: "未命名",
  uiScale: 1,
  lastZoomWheelAt: 0,
  chargeTiming: { active: false, elapsedMs: 0, lastTickMs: null, averageCurrentA: null, currentSumA: 0, currentSamples: 0 },
};

const PACK_CAPACITY_AH = 16.2;
const UI_SCALE_STEPS = [0.8, 0.9, 1, 1.1, 1.2, 1.3];
const UI_SCALE_DEFAULT = 1.1;

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
  "高压时HV_ACC释放 · 事件记录", "充电机反馈 >500 ms · 按在线源定级",
  "20 ms重试25次失败 · 复位前锁存",
  "BMU 1 数据未就绪", "BMU 2 数据未就绪", "BMU 3 数据未就绪",
  "BMU 4 数据未就绪", "BMU 5 数据未就绪", "BMU 6 数据未就绪",
  "IVT U1失联约360 ms",
];

const pageMeta = {
  overview: ["运行总览", "高压、安全、故障与六个模组的实时状态"],
  cells: ["电芯与温度", "138 串电压和 48 路温度同页显示"],
  alarms: ["故障与记录", "实时告警、统一故障码和历史记录"],
  control: ["参数与命令", "所有写入操作集中在本页并逐次确认"],
  frames: ["CAN 监视器", "原始报文、数据记录与历史回放"],
};

const PAGE_ORDER = ["overview", "cells", "alarms", "frames", "control"];

function fmt(value, digits = 1, fallback = "—") {
  return value === null || value === undefined || Number.isNaN(value) ? fallback : Number(value).toFixed(digits);
}
function text(id, value) { const node = $(id); if (node) node.textContent = value; }
function setClass(id, className, enabled) { const node = $(id); if (node) node.classList.toggle(className, !!enabled); }
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
    populateConnectionOptions({ channels: ["PCAN_USBBUS1"], profiles: [
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
  if (!pageMeta[page]) return;
  state.page = page;
  $$(".nav-item").forEach(node => node.classList.toggle("active", node.dataset.page === page));
  $$(".page").forEach(node => node.classList.toggle("active", node.id === `page-${page}`));
  text("#pageTitle", pageMeta[page][0]); text("#pageKicker", pageMeta[page][1]);
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
    confirmCommand("rtc", { datetime: local }, "RTC 校时", `写入 Windows 本地时间：${local.replace("T", " ")}`);
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
  $("#projectButton").addEventListener("click", () => $("#projectDialog").showModal());
  $("#exportProject").addEventListener("click", exportProject);
  $("#importProject").addEventListener("click", importProject);
  ["#ovInput", "#uvInput", "#otInput", "#utInput"].forEach(id => $(id).addEventListener("input", () => state.dirty.thresholds = true));
  ["#chargeVoltage", "#chargeCurrent"].forEach(id => $(id).addEventListener("input", () => state.dirty.charge = true));
  $("#switchList").addEventListener("change", () => state.dirty.switches = true);
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
  $("#connectProfile").innerHTML = data.profiles.map(item => `<option value="${item.key}" data-bitrate="${item.bitrate}">${item.name}</option>`).join("");
  $("#connectChannel").innerHTML = data.channels.map(item => `<option>${item}</option>`).join("");
  updateConnectionDialog();
}

function updateConnectionDialog() {
  const simulation = $("#connectMode").value === "simulation";
  $("#channelField").classList.toggle("hidden", simulation);
  const option = $("#connectProfile").selectedOptions[0];
  text("#connectBitrate", `${Number(option?.dataset.bitrate || 500000) / 1000} kbit/s`);
}

async function connectCan() {
  if (!state.api) return toast("应用后端未就绪", true);
  const option = $("#connectProfile").selectedOptions[0];
  $("#doConnect").disabled = true; text("#doConnect", "连接中…");
  $("#connectError").classList.add("hidden");
  const result = await state.api.connect_can({
    mode: $("#connectMode").value, bus_profile: $("#connectProfile").value,
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
    `<label class="switch-row"><span>${item.name}</span><input type="checkbox" data-key="${item.key}"><i class="switch-track"></i></label>`
  ).join("");
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
  setClass("#connectButton", "connected", connection.connected); setClass("#sideLamp", "online", connection.connected);
  const profileNames = { can1: "CAN1", canb: "CANB", canb_legacy: "CANB 250k" };
  text("#sideBus", connection.connected ? (connection.channel || "内置模拟数据") : "未连接");
  $("#connectButton b").textContent = connection.connected ? connection.status : "连接设备";
  $("#connectButton small").textContent = connection.connected ? `${profileNames[connection.bus_profile]} · ${(connection.bitrate / 1000)} kbit/s` : "PCAN / 模拟";
  text("#busProfile", `${profileNames[connection.bus_profile] || "CAN"} · ${connection.bitrate ? connection.bitrate / 1000 : 500} kbit/s`);
  text("#summaryAge", connection.summary_age == null ? "—" : `${connection.summary_age.toFixed(1)} s`);
  $("#disconnectButton").classList.toggle("hidden", !connection.connected);
}

function renderOverview() {
  const { overview: o, relay, hv, imd, connection } = state.snapshot;
  text("#stateName", o.state_name || "等待 CAN 数据");
  const stale = connection.summary_age == null || connection.summary_age > 1.5;
  const descriptions = { 2: "主控正在等待完整从控采样和 IVT U1。", 3: "高压未闭合，可以进行参数配置。", 4: "预充正在进行，配置命令将被主控忽略。", 5: "高压已闭合，优先监视电流、单体与告警。", 7: "故障已锁存，排除实时故障后再请求复位。" };
  text("#stateDescription", stale ? "主控周期状态帧缺失或已经超时。" : descriptions[o.state] || "正在读取主控状态。");
  const bannerColor = o.alarm_level == null || stale ? "var(--idle)"
    : o.alarm_level === 1 ? "var(--fault)" : o.alarm_level === 2 ? "var(--warn)" : "var(--ok)";
  $("#systemBanner").style.setProperty("--banner", bannerColor);
  text("#overviewClock", new Date().toLocaleTimeString("zh-CN", { hour12: false }));
  text("#overviewFaultCode", state.snapshot.fault?.code_hex || "0x00000000");
  text("#alarmLevelName", o.alarm_level_name || "等待数据");
  text("#packVoltage", fmt(o.voltage_v, 1)); text("#packCurrent", fmt(o.current_a, 1)); text("#packSoc", fmt(o.soc_pct, 0));
  $("#socBar").style.width = `${Math.max(0, Math.min(100, o.soc_pct || 0))}%`;
  const delta = o.max_cell_mv != null && o.min_cell_mv != null ? o.max_cell_mv - o.min_cell_mv : null;
  text("#cellDelta", fmt(delta, 0)); text("#cellExtremes", o.max_cell_mv == null ? "最高 / 最低 —" : `${o.max_cell_mv} / ${o.min_cell_mv} mV`);
  const sumDelta = o.voltage_v != null && o.cell_sum_v != null ? o.voltage_v - o.cell_sum_v : null;
  text("#cellSumDelta", o.cell_sum_v == null ? "单体累加 —" : `单体累加 ${fmt(o.cell_sum_v, 1)} V · 差 ${fmt(sumDelta, 1)} V`);
  text("#hvPackVoltage", `${fmt(o.voltage_v, 1)} V`); text("#prechargeVoltage", fmt(relay.precharge_voltage_v, 1));
  const positive = hv.positive ?? relay.positive, negative = hv.negative ?? relay.negative;
  setClass("#positiveRelay", "closed", positive); setClass("#negativeRelay", "closed", negative);
  text("#positiveRelay em", positive == null ? "—" : positive ? "闭合" : "断开");
  text("#negativeRelay em", negative == null ? "—" : negative ? "闭合" : "断开");
  setClass("#prechargeRelay", "closed", hv.precharge);
  text("#prechargeRelay em", hv.precharge == null ? "—" : hv.precharge ? "闭合" : "断开");
  text("#hvOutput", positive && negative ? "已接通" : "断开"); text("#hvAcc", hv.hv_acc == null ? "—" : hv.hv_acc ? "请求" : "释放");
  text("#chargeButton", hv.charge_button == null ? "—" : hv.charge_button ? "按下" : "释放");
  const safetyAlarm = (state.snapshot.alarms || []).find(item => item.index === 22);
  text("#safetyCircuit", !safetyAlarm ? "—" : safetyAlarm.level || safetyAlarm.in_fault_code ? "断开" : "正常");
  setClass("#safetyCircuit", "bad", !!(safetyAlarm?.level || safetyAlarm?.in_fault_code));
  text("#prechargeTime", hv.precharge_result === 2 ? `${hv.failure_ms} ms` : hv.success_ms ? `${hv.success_ms} ms` : "—");
  text("#prechargeResult", hv.precharge_result_name || "未发生预充");
  $("#prechargeResult").className = `state-note ${hv.precharge_result === 1 ? "ok" : hv.precharge_result === 2 ? "bad" : ""}`;
  text("#relayOutputState", positive || negative || hv.precharge ? "有输出" : "全断开");
  renderThermal(o, relay);
  text("#imdStatus", imd.status_name || "等待数据"); text("#imdResistance", fmt(imd.resistance_kohm, 0));
  text("#imdFrequency", imd.frequency_hz == null ? "—" : `${fmt(imd.frequency_hz, 2)} Hz`); text("#imdDuty", imd.duty_pct == null ? "—" : `${fmt(imd.duty_pct, 1)} %`);
  $("#imdStatus").className = imd.status === 0 ? "ok" : imd.status == null ? "" : "bad";
  const firmware = state.snapshot.firmware || {};
  text("#firmwareIdentity", firmware.git ? `${firmware.variant} · ${firmware.git}${firmware.dirty ? " · dirty" : ""}` : "—");
  renderChargeTiming(o, connection, state.snapshot.fault || {});
  drawTrend();
}

function renderThermal(overview, relay) {
  text("#maxTemp", overview.max_temp_c == null ? "—" : `${overview.max_temp_c} °C`);
  text("#minTemp", overview.min_temp_c == null ? "—" : `${overview.min_temp_c} °C`);
  text("#maxTempNo", overview.max_temp_no == null ? "" : `T${overview.max_temp_no}`);
  text("#minTempNo", overview.min_temp_no == null ? "" : `T${overview.min_temp_no}`);
  const spread = overview.max_temp_c != null && overview.min_temp_c != null ? overview.max_temp_c - overview.min_temp_c : null;
  text("#tempDelta", spread == null ? "—" : `${spread} °C`);
  text("#fanDuty", relay.fan_duty_pct == null ? "—" : `${relay.fan_duty_pct} %`);
  text("#fanRpm", relay.fan_rpm == null ? "—" : `${relay.fan_rpm} rpm`);
  text("#coolingTag", relay.cooling == null ? "冷却 —" : relay.cooling ? "冷却请求" : "冷却关闭");
  $("#coolingTag").className = `state-text ${relay.cooling ? "ok" : ""}`;
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
    text("#chargeExpectedAt", "—"); text("#chargeAverageCurrent", "—");
    text("#chargeEstimateNote", "历史回放不使用本机时间估算充满时刻。");
    return;
  }

  const now = Date.now();
  const chargeActive = !!fault.flags?.charge_mode;
  const fresh = connection.summary_age != null && connection.summary_age <= 1.5;
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
    if (fresh && timing.lastTickMs != null) timing.elapsedMs += Math.min(1000, now - timing.lastTickMs);
    timing.lastTickMs = now;
    if (fresh && Number.isFinite(chargeCurrent) && chargeCurrent > 0.05) {
      timing.currentSumA += chargeCurrent;
      timing.currentSamples++;
      timing.averageCurrentA = timing.currentSumA / timing.currentSamples;
    }
  }

  text("#chargeTimingState", chargeActive ? fresh ? "充电计时中" : "状态帧超时" : timing.elapsedMs ? "本次已停止" : "未充电");
  text("#chargeElapsed", durationLabel(timing.elapsedMs / 1000));
  text("#chargeAverageCurrent", timing.averageCurrentA == null ? "—" : `${fmt(timing.averageCurrentA, 2)} A`);

  const soc = Number(overview.soc_pct);
  const estimateCurrent = timing.averageCurrentA;
  if (!chargeActive || !fresh || !Number.isFinite(soc) || estimateCurrent == null || estimateCurrent <= .1) {
    text("#chargeRemaining", "—"); text("#chargeExpectedAt", "—");
    text("#chargeEstimateNote", chargeActive && estimateCurrent != null && estimateCurrent <= .1
      ? "当前充电电流过低，暂不估算充满时间。"
      : "检测到充电模式后开始计时；预计时间按当前SOC和本次平均电流估算。");
    return;
  }

  const remainingAh = PACK_CAPACITY_AH * Math.max(0, 100 - soc) / 100;
  const remainingSeconds = Math.min(7 * 86400, remainingAh / estimateCurrent * 3600);
  if (remainingAh <= .01) {
    text("#chargeRemaining", "已充满"); text("#chargeExpectedAt", "现在");
  } else {
    text("#chargeRemaining", durationLabel(remainingSeconds));
    const expected = new Date(now + remainingSeconds * 1000);
    const sameDay = expected.toDateString() === new Date(now).toDateString();
    text("#chargeExpectedAt", expected.toLocaleString("zh-CN", sameDay
      ? { hour: "2-digit", minute: "2-digit", hour12: false }
      : { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }));
  }
  text("#chargeEstimateNote", `按 ${PACK_CAPACITY_AH.toFixed(1)} Ah、当前 ${fmt(soc, 0)}% SOC和本次平均电流估算，未计入末段降流。`);
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
  const pad = { left: 54, right: 54, top: 18, bottom: 24 };
  const plotWidth = width - pad.left - pad.right, plotHeight = height - pad.top - pad.bottom;
  ctx.strokeStyle = "#253238"; ctx.lineWidth = 1;
  for (let row = 0; row <= 3; row++) {
    const y = Math.round(pad.top + plotHeight * row / 3) + .5;
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke();
  }
  if (trends.length < 2) {
    ctx.fillStyle = "#87969c"; ctx.font = '12px "Microsoft YaHei UI", sans-serif';
    ctx.fillText("等待状态帧形成曲线", pad.left, pad.top + plotHeight / 2);
    return;
  }
  const drawSeries = (key, color, minimumSpan) => {
    const values = trends.map(item => item[key]).filter(Number.isFinite);
    if (!values.length) return null;
    let min = Math.min(...values), max = Math.max(...values);
    if (max - min < minimumSpan) { const mid = (max + min) / 2; min = mid - minimumSpan / 2; max = mid + minimumSpan / 2; }
    ctx.beginPath(); ctx.strokeStyle = color; ctx.lineWidth = 2;
    let started = false;
    trends.forEach((item, index) => {
      if (!Number.isFinite(item[key])) return;
      const x = pad.left + plotWidth * index / Math.max(1, trends.length - 1);
      const y = pad.top + plotHeight * (max - item[key]) / (max - min);
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    });
    ctx.stroke();
    return { min, max };
  };
  const voltage = drawSeries("voltage", "#42c4d5", 8);
  const current = drawSeries("current", "#f0b429", 6);
  ctx.font = '11px "Cascadia Mono", Consolas, monospace';
  if (voltage) {
    ctx.fillStyle = "#42c4d5"; ctx.textAlign = "left";
    ctx.fillText(`${voltage.max.toFixed(1)} V`, 4, pad.top + 3);
    ctx.fillText(`${voltage.min.toFixed(1)} V`, 4, pad.top + plotHeight);
  }
  if (current) {
    ctx.fillStyle = "#f0b429"; ctx.textAlign = "right";
    ctx.fillText(`${current.max.toFixed(1)} A`, width - 4, pad.top + 3);
    ctx.fillText(`${current.min.toFixed(1)} A`, width - 4, pad.top + plotHeight);
  }
  ctx.fillStyle = "#87969c"; ctx.textAlign = "left";
  ctx.fillText("较早", pad.left, height - 5);
  ctx.textAlign = "right"; ctx.fillText("现在", width - pad.right, height - 5);
}

function renderAlarmSummary() {
  const alarms = state.snapshot.alarms || [];
  text("#faultCode", state.snapshot.fault?.code_hex || "0x00000000");
  const activeItems = alarms.filter(item => item.level || item.in_fault_code);
  const activeCount = activeItems.length;
  text("#alarmNavBadge", activeCount); $("#alarmNavBadge").classList.toggle("hidden", activeCount === 0);
  text("#alarmActiveCount", activeCount ? `${activeCount} 项` : alarms.length ? "无" : "—");
  text("#alarmPageLevel", state.snapshot.overview?.alarm_level_name || "等待数据");
  $("#alarmPageLevel")?.classList.toggle("bad", state.snapshot.overview?.alarm_level === 1);
  $("#alarmPageLevel")?.classList.toggle("warn", state.snapshot.overview?.alarm_level === 2);
  text("#activeAlarmCount", activeCount ? `${activeCount} 项` : alarms.length ? "无" : "—");
  const active = activeItems.slice(0, 3);
  $("#activeAlarmBrief").className = active.length ? "fault-brief" : "fault-brief empty-state";
  $("#activeAlarmBrief").innerHTML = active.length ? active.map(item => `<div class="brief-alarm ${item.level === 1 ? "lv1" : ""}"><b>${item.name}</b><span>${item.level_name}</span></div>`).join("") : "当前没有活动告警";
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
  const thresholds = state.snapshot.config?.thresholds || {};
  alarms.forEach(alarm => {
    const node = $(`[data-alarm="${alarm.index}"]`); if (!node) return;
    const active = !!(alarm.level || alarm.in_fault_code);
    const levelReceived = alarm.received !== false;
    const classes = ["alarm-item"];
    if (alarm.level === 1) classes.push("lv1");
    else if (alarm.level === 2) classes.push("lv2");
    if (alarm.in_fault_code) classes.push("fc");
    if (!levelReceived && !alarm.in_fault_code) classes.push("pending");
    if (alarm.index === 14 || alarm.index === 15) classes.push("reserved");
    if (state.onlyActiveAlarms && !active) classes.push("hidden");
    node.className = classes.join(" ");
    node.querySelector("b").textContent = alarm.name;
    node.querySelector("small").textContent = alarmRuleText(alarm.index, thresholds);
    node.querySelector("em").textContent = alarm.level === 1 ? "一级故障"
      : alarm.level === 2 ? "二级告警"
      : alarm.in_fault_code ? "故障码置位"
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
      return `<span class="protection-switch ${stateClass}"><i></i><span>${item.name}</span><b>${stateText}</b></span>`;
    }).join("");
  }
  if (Object.keys(switches).length && !state.dirty.switches && (!state.inputsInitialized.switches || !$("#switchList").contains(document.activeElement))) {
    $$("#switchList input").forEach(input => input.checked = !!switches[input.dataset.key]);
    state.inputsInitialized.switches = true;
  }
  if (config.current_direction_inverted != null && !state.dirty.direction && document.activeElement !== $("#currentDirection")) {
    $("#currentDirection").value = config.current_direction_inverted ? "1" : "0";
  }
}

function renderControls() {
  const { relay, fault, connection, overview } = state.snapshot;
  if (relay.request_voltage_v != null && !state.dirty.charge && (!state.inputsInitialized.charge || ![$("#chargeVoltage"), $("#chargeCurrent")].includes(document.activeElement))) {
    $("#chargeVoltage").value = relay.request_voltage_v; $("#chargeCurrent").value = relay.request_current_a; state.inputsInitialized.charge = true;
  }
  text("#chargeEcho", relay.request_voltage_v == null ? "— V / — A" : `${fmt(relay.request_voltage_v, 1)} V / ${fmt(relay.request_current_a, 1)} A`);
  const runtime = state.snapshot.runtime_diag || {};
  const savePending = runtime.config_save_pending || runtime.current_direction_save_pending;
  text("#saveStatus", runtime.flash_ready == null ? "等待保存状态" : !runtime.flash_ready ? "Flash 离线" : savePending ? "等待 Flash 保存" : "保存队列空");
  $("#saveStatus").className = `tag ${runtime.flash_ready == null ? "neutral" : !runtime.flash_ready ? "bad" : savePending ? "warn" : "ok"}`;
  if (state.snapshot.config?.charger_type != null && !state.dirty.chargerType && document.activeElement !== $("#chargerType")) {
    $("#chargerType").value = String(state.snapshot.config.charger_type);
  }
  if (runtime.charger_feedback_voltage_v != null && runtime.charger_feedback_fresh) {
    text("#chargeEcho", `${fmt(relay.request_voltage_v, 1)} V / ${fmt(relay.request_current_a, 1)} A · 反馈 ${fmt(runtime.charger_feedback_voltage_v, 1)} V / ${fmt(runtime.charger_feedback_current_a, 1)} A`);
  } else if (runtime.charger_feedback_voltage_v != null) {
    text("#chargeEcho", `${fmt(relay.request_voltage_v, 1)} V / ${fmt(relay.request_current_a, 1)} A · 反馈已超时`);
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
  text("#confirmPayload", `通道：${conn.channel || "内置模拟器"}\n`
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
  const result = await state.api.send_command(state.pendingCommand.name, state.pendingCommand.values, true);
  if (result.ok) {
    const dirtyMap = { alarm_thresholds: "thresholds", alarm_switches: "switches", charge_config: "charge", current_direction: "direction", charger_type: "chargerType" };
    if (dirtyMap[state.pendingCommand.name]) state.dirty[dirtyMap[state.pendingCommand.name]] = false;
    $("#confirmDialog").close(); toast(result.message || "命令已发送");
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

function valueOrNull(selector) {
  const value = $(selector).value;
  return value === "" || !Number.isFinite(Number(value)) ? null : Number(value);
}

function collectProject() {
  const switches = {};
  $$("#switchList input").forEach(input => switches[input.dataset.key] = input.checked);
  const connection = state.snapshot?.connection || {};
  return {
    name: $("#projectName").value.trim() || "未命名工程",
    notes: $("#projectNotes").value,
    connection: {
      bus_profile: connection.bus_profile || $("#connectProfile").value,
      channel: connection.mode === "pcan" ? connection.channel : $("#connectChannel").value,
      bitrate: connection.bitrate || Number($("#connectProfile").selectedOptions[0]?.dataset.bitrate || 500000),
    },
    parameters: {
      thresholds: { ov_mv: valueOrNull("#ovInput"), uv_mv: valueOrNull("#uvInput"), ot_c: valueOrNull("#otInput"), ut_c: valueOrNull("#utInput") },
      switches,
      charge: { voltage_v: valueOrNull("#chargeVoltage"), current_a: valueOrNull("#chargeCurrent") },
      current_direction_inverted: $("#currentDirection").value === "1",
      charger_type: +$("#chargerType").value,
    },
    view: { cell_mode: state.cellMode, only_abnormal: $("#onlyAbnormal").checked },
  };
}

async function exportProject() {
  if (!state.api) return;
  const result = await state.api.export_project(collectProject());
  if (result.ok) {
    state.projectName = $("#projectName").value.trim() || "未命名工程";
    text("#projectNameTop", state.projectName); $("#projectDialog").close(); toast(`工程已导出：${result.path}`);
  } else if (!result.cancelled) toast(result.error || "工程导出失败", true);
}

async function importProject() {
  if (!state.api) return;
  const result = await state.api.import_project();
  if (!result.ok) { if (!result.cancelled) toast(result.error || "工程导入失败", true); return; }
  const project = result.project, connection = project.connection || {}, parameters = project.parameters || {}, view = project.view || {};
  state.projectName = project.name || "未命名工程";
  $("#projectName").value = state.projectName; $("#projectNotes").value = project.notes || ""; text("#projectNameTop", state.projectName);
  if ($(`#connectProfile option[value="${connection.bus_profile}"]`)) $("#connectProfile").value = connection.bus_profile;
  if ($$("#connectChannel option").some(option => option.value === connection.channel)) $("#connectChannel").value = connection.channel;
  updateConnectionDialog();
  const thresholds = parameters.thresholds || {};
  [["#ovInput", thresholds.ov_mv], ["#uvInput", thresholds.uv_mv], ["#otInput", thresholds.ot_c], ["#utInput", thresholds.ut_c]].forEach(([selector, value]) => { if (value != null) $(selector).value = value; });
  const switches = parameters.switches || {};
  $$("#switchList input").forEach(input => { if (input.dataset.key in switches) input.checked = !!switches[input.dataset.key]; });
  const charge = parameters.charge || {};
  if (charge.voltage_v != null) $("#chargeVoltage").value = charge.voltage_v;
  if (charge.current_a != null) $("#chargeCurrent").value = charge.current_a;
  if (typeof parameters.current_direction_inverted === "boolean") $("#currentDirection").value = parameters.current_direction_inverted ? "1" : "0";
  if (parameters.charger_type === 0 || parameters.charger_type === 1) $("#chargerType").value = String(parameters.charger_type);
  state.dirty = { thresholds: true, switches: true, charge: true, direction: true, chargerType: true };
  if (["voltage", "temperature"].includes(view.cell_mode)) {
    state.cellMode = view.cell_mode;
    $$("#cellMode button").forEach(button => button.classList.toggle("active", button.dataset.mode === state.cellMode));
  }
  $("#onlyAbnormal").checked = !!view.only_abnormal;
  $("#projectDialog").close(); toast("工程已导入：参数只填入界面，尚未连接或写入主控");
}

document.addEventListener("DOMContentLoaded", init);
