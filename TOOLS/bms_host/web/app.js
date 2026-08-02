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
  pendingCommand: null,
  inputsInitialized: { thresholds: false, switches: false, charge: false },
  dirty: { thresholds: false, switches: false, charge: false, direction: false },
  recording: false,
  projectName: "未命名",
};

const pageMeta = {
  overview: ["LIVE SYSTEM", "运行总览"], cells: ["CELL ARRAY", "单体监视"],
  alarms: ["FAULT & CONFIG", "告警与配置"], control: ["CHARGE & SERVICE", "充电与控制"],
  frames: ["CAN TRAFFIC", "CAN 监视器"],
};

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

async function waitForApi() {
  if (window.pywebview?.api) return window.pywebview.api;
  await new Promise(resolve => window.addEventListener("pywebviewready", resolve, { once: true }));
  return window.pywebview.api;
}

async function init() {
  bindNavigation();
  bindControls();
  buildAlarmMatrix();
  try {
    state.api = await waitForApi();
    state.bootstrap = await state.api.bootstrap();
    populateConnectionOptions();
    buildSwitchList();
    await poll();
    state.pollTimer = setInterval(poll, 250);
  } catch (error) {
    toast(`应用后端未就绪：${error}`, true);
    populateConnectionOptions({ channels: ["PCAN_USBBUS1"], profiles: [
      { key: "can1", name: "CAN1 · 主控 / 从控 / 工具", bitrate: 500000 },
      { key: "canb", name: "CANB · IVT / ECU / Chroma", bitrate: 500000 },
    ]});
  }
  resizeCanvas();
}

function bindNavigation() {
  $("#nav").addEventListener("click", event => {
    const button = event.target.closest(".nav-item");
    if (button) showPage(button.dataset.page);
  });
  $$('[data-goto]').forEach(button => button.addEventListener("click", () => showPage(button.dataset.goto)));
}

function showPage(page) {
  state.page = page;
  $$(".nav-item").forEach(node => node.classList.toggle("active", node.dataset.page === page));
  $$(".page").forEach(node => node.classList.toggle("active", node.id === `page-${page}`));
  text("#pageKicker", pageMeta[page][0]); text("#pageTitle", pageMeta[page][1]);
  if (page === "overview") setTimeout(resizeCanvas, 20);
}

function bindControls() {
  $("#connectButton").addEventListener("click", () => $("#connectDialog").showModal());
  $("#connectMode").addEventListener("change", updateConnectionDialog);
  $("#connectProfile").addEventListener("change", updateConnectionDialog);
  $("#doConnect").addEventListener("click", connectCan);
  $("#disconnectButton").addEventListener("click", disconnectCan);
  $("#cellMode").addEventListener("click", event => {
    const button = event.target.closest("button"); if (!button) return;
    state.cellMode = button.dataset.mode;
    $$("#cellMode button").forEach(node => node.classList.toggle("active", node === button));
    renderCells();
  });
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
  window.addEventListener("resize", resizeCanvas);
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
  $("#switchList").innerHTML = catalog.map(item => `<label class="switch-row"><span>${item.name}</span><input type="checkbox" data-key="${item.key}"><i class="mini-switch"></i></label>`).join("");
}

function buildAlarmMatrix() {
  $("#alarmMatrix").innerHTML = Array.from({ length: 32 }, (_, i) => `<div class="alarm-item" data-alarm="${i}"><i>${String(i).padStart(2, "0")}</i><span><b>告警项 ${i}</b><small>bit ${i}</small></span><em class="alarm-level">未收到</em></div>`).join("");
}

async function poll() {
  if (!state.api) return;
  try {
    state.snapshot = await state.api.get_snapshot();
    render();
  } catch (error) {
    console.error(error);
  }
}

function render() {
  const snap = state.snapshot; if (!snap) return;
  renderConnection(snap.connection);
  renderOverview(); renderModules(); renderAlarms(); renderConfig(); renderControls();
  renderReplay();
  if (state.page === "cells") renderCells();
  if (state.page === "frames" && !state.framePaused) renderFrames();
}

function renderConnection(connection) {
  state.recording = !!connection.recording;
  text("#recordButton", state.recording ? "■ 停止记录" : "● 记录数据");
  text("#rxCount", connection.rx_count.toLocaleString()); text("#txCount", connection.tx_count.toLocaleString());
  setClass("#connectButton", "connected", connection.connected); setClass("#sideLamp", "online", connection.connected);
  const profileNames = { can1: "CAN1", canb: "CANB", canb_legacy: "CANB 250k" };
  text("#sideBus", connection.connected ? `${profileNames[connection.bus_profile]} · ${connection.channel || "模拟"}` : "未连接");
  $("#connectButton b").textContent = connection.connected ? connection.status : "连接设备";
  $("#connectButton small").textContent = connection.connected ? `${profileNames[connection.bus_profile]} · ${(connection.bitrate / 1000)} kbit/s` : "PCAN / 模拟";
  text("#busProfile", `${profileNames[connection.bus_profile] || "CAN"} · ${connection.bitrate ? connection.bitrate / 1000 : 500} kbit/s`);
  text("#summaryAge", connection.summary_age == null ? "—" : `${connection.summary_age.toFixed(1)} s`);
  $("#disconnectButton").classList.toggle("hidden", !connection.connected);
}

function renderOverview() {
  const { overview: o, relay, hv, imd, sop, connection } = state.snapshot;
  text("#stateName", o.state_name || "等待 CAN 数据");
  const stale = connection.summary_age == null || connection.summary_age > 1.5;
  const descriptions = { 2: "主控正在等待完整从控采样和 IVT U1。", 3: "高压未闭合，可以进行参数配置。", 4: "预充正在进行，配置命令将被主控忽略。", 5: "高压已闭合，优先监视电流、单体与告警。", 7: "故障已锁存，排除实时故障后再请求复位。" };
  text("#stateDescription", stale ? "主控周期状态帧缺失或已经超时。" : descriptions[o.state] || "正在读取主控状态。");
  $("#systemBanner").style.setProperty("--banner", o.alarm_level === 1 ? "var(--red)" : o.alarm_level === 2 ? "var(--amber)" : "var(--accent)");
  text("#packVoltage", fmt(o.voltage_v, 1)); text("#packCurrent", fmt(o.current_a, 1)); text("#packSoc", fmt(o.soc_pct, 0));
  $("#socBar").style.width = `${Math.max(0, Math.min(100, o.soc_pct || 0))}%`;
  const delta = o.max_cell_mv != null && o.min_cell_mv != null ? o.max_cell_mv - o.min_cell_mv : null;
  text("#cellDelta", fmt(delta, 0)); text("#cellExtremes", o.max_cell_mv == null ? "最高 / 最低 —" : `${o.max_cell_mv} / ${o.min_cell_mv} mV`);
  const sumDelta = o.voltage_v != null && o.cell_sum_v != null ? o.voltage_v - o.cell_sum_v : null;
  text("#cellSumDelta", o.cell_sum_v == null ? "单体累加 —" : `单体累加 ${fmt(o.cell_sum_v, 1)} V · 差 ${fmt(sumDelta, 1)} V`);
  text("#hvPackVoltage", `${fmt(o.voltage_v, 1)} V`); text("#prechargeVoltage", `${fmt(relay.precharge_voltage_v, 1)} V`);
  const positive = hv.positive ?? relay.positive, negative = hv.negative ?? relay.negative;
  setClass("#positiveRelay", "closed", positive); setClass("#negativeRelay", "closed", negative);
  text("#hvOutput", positive && negative ? "已接通" : "断开"); text("#hvAcc", hv.hv_acc == null ? "—" : hv.hv_acc ? "请求" : "释放");
  text("#chargeButton", hv.charge_button == null ? "—" : hv.charge_button ? "按下" : "释放");
  text("#prechargeTime", hv.precharge_result === 2 ? `${hv.failure_ms} ms` : hv.success_ms ? `${hv.success_ms} ms` : "—");
  text("#prechargeResult", hv.precharge_result_name || "未发生预充");
  $("#prechargeResult").className = `tag ${hv.precharge_result === 1 ? "ok" : hv.precharge_result === 2 ? "bad" : "neutral"}`;
  text("#sopDisCurrent", fmt(sop.discharge_current_a, 1)); text("#sopChgCurrent", fmt(sop.charge_current_a, 1));
  text("#sopDisPower", fmt(sop.discharge_power_kw, 1)); text("#sopChgPower", fmt(sop.charge_power_kw, 1));
  text("#imdStatus", imd.status_name || "等待数据"); text("#imdResistance", fmt(imd.resistance_kohm, 0));
  text("#imdFrequency", imd.frequency_hz == null ? "—" : `${fmt(imd.frequency_hz, 2)} Hz`); text("#imdDuty", imd.duty_pct == null ? "—" : `${fmt(imd.duty_pct, 1)} %`);
  $("#imdStatus").className = `tag ${imd.status === 0 ? "ok" : imd.status == null ? "neutral" : "bad"}`;
  const firmware = state.snapshot.firmware || {};
  text("#firmwareIdentity", firmware.git ? `${firmware.variant} · ${firmware.git}${firmware.dirty ? " · dirty" : ""}` : "固件身份 —");
  drawTrend();
}

function renderModules() {
  const modules = state.snapshot.modules || [];
  $("#moduleRow").innerHTML = modules.map(item => `<div class="module-unit ${item.online ? "online" : ""}"><div><strong>BMU ${item.no}</strong><i></i></div><span>电压 ${item.voltage_frames}/6 · 温度 ${item.temperature_frame ? "1/1" : "0/1"}</span><b>${item.online ? "数据完整" : "数据缺失"}</b></div>`).join("");
  text("#moduleSummary", `${modules.filter(item => item.online).length} / 6 在线`);
}

function renderAlarms() {
  const alarms = state.snapshot.alarms || [];
  text("#faultCode", state.snapshot.fault?.code_hex || "0x00000000");
  let activeCount = 0;
  alarms.forEach(alarm => {
    const node = $(`[data-alarm="${alarm.index}"]`); if (!node) return;
    node.className = `alarm-item ${alarm.level === 1 ? "lv1" : alarm.level === 2 ? "lv2" : ""}`;
    node.querySelector("b").textContent = alarm.name;
    node.querySelector("small").textContent = `bit ${alarm.index}${alarm.in_fault_code ? " · fault_code=1" : ""}`;
    node.querySelector("em").textContent = alarm.level === 1 ? "一级" : alarm.level === 2 ? "二级" : "正常";
    if (alarm.level || alarm.in_fault_code) activeCount++;
  });
  text("#alarmNavBadge", activeCount); $("#alarmNavBadge").classList.toggle("hidden", activeCount === 0);
  const active = alarms.filter(item => item.level || item.in_fault_code).slice(0, 3);
  $("#activeAlarmBrief").className = active.length ? "" : "empty-state";
  $("#activeAlarmBrief").innerHTML = active.length ? active.map(item => `<div class="brief-alarm ${item.level === 1 ? "lv1" : ""}"><b>${item.name}</b><span>${item.level_name}</span></div>`).join("") : "当前没有活动告警";
  const history = state.snapshot.fault_history || [];
  $("#faultHistory").innerHTML = history.length ? history.map(event => `<div class="fault-event"><time>${event.time}</time><b>${event.previous} → ${event.code}</b><p>${event.added.length ? `<span class="added">进入：${event.added.join("、")}</span>` : ""}${event.added.length && event.cleared.length ? "<br>" : ""}${event.cleared.length ? `<span class="cleared">清除：${event.cleared.join("、")}</span>` : ""}</p></div>`).join("") : `<div class="empty-state">故障码发生变化后在这里显示进入和清除记录。</div>`;
  const flashRecords = state.snapshot.flash_log_records || [];
  const logClearPending = !!state.snapshot.fault?.flags?.log_clear_pending;
  $("#readFlashLog").disabled = logClearPending;
  $("#clearFaultLog").disabled = logClearPending;
  $("#flashFaultLog").innerHTML = flashRecords.length ? [...flashRecords].reverse().map(event =>
    `<div class="fault-event"><time>${event.timestamp}</time><b>${event.fault_code}</b><p>类型 ${event.event_type} · 详情 ${event.event_detail}</p></div>`
  ).join("") : `<div class="empty-state">尚未读取，或 Flash 中没有重要故障日志。</div>`;
}

function renderConfig() {
  const config = state.snapshot.config || {}, thresholds = config.thresholds || {}, switches = config.switches || {};
  if (Object.keys(thresholds).length) {
    text("#thresholdSync", "已从主控同步");
    if (!state.dirty.thresholds && (!state.inputsInitialized.thresholds || ![$("#ovInput"), $("#uvInput"), $("#otInput"), $("#utInput")].includes(document.activeElement))) {
      $("#ovInput").value = thresholds.ov_mv; $("#uvInput").value = thresholds.uv_mv;
      $("#otInput").value = thresholds.ot_c; $("#utInput").value = thresholds.ut_c;
      state.inputsInitialized.thresholds = true;
    }
  }
  text("#switchVersion", config.switch_version == null ? "V—" : `V${config.switch_version}`);
  if (Object.keys(switches).length && !state.dirty.switches && (!state.inputsInitialized.switches || !$("#switchList").contains(document.activeElement))) {
    $$("#switchList input").forEach(input => input.checked = !!switches[input.dataset.key]);
    state.inputsInitialized.switches = true;
  }
  if (config.current_direction_inverted != null && !state.dirty.direction && document.activeElement !== $("#currentDirection")) {
    $("#currentDirection").value = config.current_direction_inverted ? "1" : "0";
  }
}

function renderControls() {
  const { relay, fault, connection, overview, ivt } = state.snapshot;
  if (relay.request_voltage_v != null && !state.dirty.charge && (!state.inputsInitialized.charge || ![$("#chargeVoltage"), $("#chargeCurrent")].includes(document.activeElement))) {
    $("#chargeVoltage").value = relay.request_voltage_v; $("#chargeCurrent").value = relay.request_current_a; state.inputsInitialized.charge = true;
  }
  text("#chargeEcho", relay.request_voltage_v == null ? "— V / — A" : `${fmt(relay.request_voltage_v, 1)} V / ${fmt(relay.request_current_a, 1)} A`);
  const runtime = state.snapshot.runtime_diag || {};
  const savePending = runtime.config_save_pending || runtime.current_direction_save_pending;
  text("#saveStatus", runtime.flash_ready == null ? "等待保存状态" : !runtime.flash_ready ? "Flash 离线" : savePending ? "等待 Flash 保存" : "保存队列空");
  $("#saveStatus").className = `tag ${runtime.flash_ready == null ? "neutral" : !runtime.flash_ready ? "bad" : savePending ? "warn" : "ok"}`;
  if (state.snapshot.config?.charger_type != null && document.activeElement !== $("#chargerType")) {
    $("#chargerType").value = String(state.snapshot.config.charger_type);
  }
  if (runtime.charger_feedback_voltage_v != null && runtime.charger_feedback_fresh) {
    text("#chargeEcho", `${fmt(relay.request_voltage_v, 1)} V / ${fmt(relay.request_current_a, 1)} A · 反馈 ${fmt(runtime.charger_feedback_voltage_v, 1)} V / ${fmt(runtime.charger_feedback_current_a, 1)} A`);
  } else if (runtime.charger_feedback_voltage_v != null) {
    text("#chargeEcho", `${fmt(relay.request_voltage_v, 1)} V / ${fmt(relay.request_current_a, 1)} A · 反馈已超时`);
  }
  const charge = fault.flags?.charge_mode;
  text("#chargeModeTag", charge == null ? "模式未知" : charge ? `充电 · ${fault.flags.charger_type}` : "放电 / 待机");
  const connectedCan1 = connection.connected && connection.bus_profile === "can1";
  const allowedState = [2, 3, 7].includes(overview.state);
  const fresh = connection.summary_age != null && connection.summary_age <= 1.5;
  setClass("#lockConnected", "ok", connectedCan1); setClass("#lockState", "ok", allowedState); setClass("#lockFresh", "ok", fresh);
  text("#ivtU1", fmt(ivt.u1_v, 2)); text("#ivtU2", fmt(ivt.u2_v, 2)); text("#ivtCurrent", fmt(ivt.current_a, 2));
  text("#ivtPower", fmt(ivt.power_w, 0)); text("#ivtCharge", fmt(ivt.charge_as, 0)); text("#ivtEnergy", fmt(ivt.energy_wh, 0));
  text("#ivtValidity", ivt.last_channel ? ivt.valid ? "最近通道正常" : `状态异常 ${ivt.status}` : "等待数据");
  $("#ivtValidity").className = `tag ${ivt.last_channel ? ivt.valid ? "ok" : "bad" : "neutral"}`;
}

function renderCells() {
  if (!state.snapshot) return;
  const voltage = state.cellMode === "voltage";
  const items = voltage ? state.snapshot.cells : state.snapshot.temps;
  const groups = Array.from({ length: 6 }, (_, i) => items.filter(item => item.module === i + 1));
  const values = items.filter(item => item.value != null).map(item => item.value);
  const max = values.length ? Math.max(...values) : null, min = values.length ? Math.min(...values) : null;
  const unit = voltage ? "mV" : "°C";
  text("#gridMax", max == null ? "—" : `${max} ${unit}`); text("#gridMin", min == null ? "—" : `${min} ${unit}`);
  text("#gridDelta", max == null ? "—" : `${max - min} ${voltage ? "mV" : "°C"}`);
  const thresholds = state.snapshot.config.thresholds || {};
  const onlyAbnormal = $("#onlyAbnormal").checked;
  $("#cellModules").innerHTML = groups.map((group, index) => {
    const module = state.snapshot.modules[index] || {};
    const cells = group.map(item => {
      let status = "";
      if (item.value == null) status = "invalid";
      else if (voltage && thresholds.uv_mv != null && item.value <= thresholds.uv_mv) status = "low";
      else if (voltage && thresholds.ov_mv != null && item.value >= thresholds.ov_mv) status = "high";
      else if (!voltage && thresholds.ut_c != null && item.value <= thresholds.ut_c) status = "low";
      else if (!voltage && thresholds.ot_c != null && item.value >= thresholds.ot_c) status = "high";
      if (onlyAbnormal && !status) return "";
      return `<div class="cell-item ${status}" title="${item.status} · 最近数据 ${item.age ?? "—"} s"><small>${voltage ? "CELL" : "TEMP"} ${String(item.no).padStart(3, "0")}</small><b>${item.value ?? "—"}<em>${unit}</em></b></div>`;
    }).join("");
    return `<section class="cell-module"><div class="cell-module-head"><div><small>SLAVE MODULE</small><strong>BMU ${index + 1}</strong></div><span class="${module.online ? "online" : ""}">${module.online ? "● 数据完整" : "● 数据缺失"}</span></div><div class="cell-grid">${cells || `<div class="empty-state">本模块没有符合筛选条件的数据</div>`}</div></section>`;
  }).join("");
}

function renderFrames() {
  if (!state.snapshot) return;
  const frames = state.framePaused ? state.pausedFrames : state.snapshot.raw_frames;
  const query = $("#frameSearch").value.trim().toLowerCase();
  const filtered = frames.filter(frame => (state.frameKind === "all" || frame.direction === state.frameKind) && (!query || `${frame.id} ${frame.name} ${frame.data}`.toLowerCase().includes(query))).slice(0, 180);
  $("#frameRows").innerHTML = filtered.map(frame => `<tr><td>${frame.time}</td><td><span class="dir-tag ${frame.direction}">${frame.direction.toUpperCase()}</span></td><td>${frame.id}</td><td>${frame.extended ? "扩展" : "标准"}</td><td>${frame.dlc}</td><td title="${frame.data}">${frame.data}</td><td title="${frame.name}">${frame.name}</td></tr>`).join("");
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

function resizeCanvas() {
  const canvas = $("#trendCanvas"); if (!canvas || !canvas.clientWidth) return;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = canvas.clientWidth * ratio; canvas.height = canvas.clientHeight * ratio;
  drawTrend();
}

function drawTrend() {
  const canvas = $("#trendCanvas"); const trends = state.snapshot?.trends || [];
  if (!canvas || !canvas.width) return;
  const ctx = canvas.getContext("2d"), ratio = window.devicePixelRatio || 1, w = canvas.width / ratio, h = canvas.height / ratio;
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, w, h);
  const pad = { l: 42, r: 42, t: 18, b: 28 }, pw = w - pad.l - pad.r, ph = h - pad.t - pad.b;
  ctx.strokeStyle = "#dfe2da"; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) { const y = pad.t + ph * i / 4; ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke(); }
  if (trends.length < 2) { ctx.fillStyle = "#7d8a8f"; ctx.font = "10px Aptos"; ctx.fillText("等待状态帧形成曲线…", pad.l, pad.t + ph / 2); return; }
  const draw = (key, color, fallbackSpan) => {
    const valid = trends.map(item => item[key]).filter(value => value != null); if (!valid.length) return;
    let min = Math.min(...valid), max = Math.max(...valid); if (max - min < fallbackSpan) { const mid = (max + min) / 2; min = mid - fallbackSpan / 2; max = mid + fallbackSpan / 2; }
    ctx.beginPath(); ctx.strokeStyle = color; ctx.lineWidth = 1.7;
    trends.forEach((item, i) => { if (item[key] == null) return; const x = pad.l + pw * i / Math.max(1, trends.length - 1); const y = pad.t + ph * (max - item[key]) / (max - min); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.stroke(); return [min, max];
  };
  const volts = draw("voltage", "#269aa7", 10), amps = draw("current", "#e28b2d", 10);
  ctx.font = "8px Bahnschrift";
  if (volts) { ctx.fillStyle = "#269aa7"; ctx.fillText(`${volts[1].toFixed(1)} V`, 4, pad.t + 4); ctx.fillText(`${volts[0].toFixed(1)} V`, 4, pad.t + ph); }
  if (amps) { ctx.fillStyle = "#c47725"; ctx.textAlign = "right"; ctx.fillText(`${amps[1].toFixed(1)} A`, w - 4, pad.t + 4); ctx.fillText(`${amps[0].toFixed(1)} A`, w - 4, pad.t + ph); ctx.textAlign = "left"; }
}

function confirmCommand(name, values, title, message, destructive = false) {
  const conn = state.snapshot?.connection;
  if (!conn?.connected) return toast("请先连接 CAN", true);
  if (conn.mode === "replay") return toast("历史回放为只读，不能发送命令", true);
  if (conn.bus_profile !== "can1") return toast("工具命令只能从 CAN1 发送", true);
  state.pendingCommand = { name, values };
  text("#confirmTitle", title); text("#confirmMessage", message);
  text("#confirmPayload", `通道：${conn.channel || "模拟器"}\n总线：CAN1 · 500 kbit/s\n主控状态：${state.snapshot.overview.state_name}\n命令：${name}`);
  $("#confirmCheck").checked = false; $("#doConfirm").disabled = true;
  $("#doConfirm").className = destructive ? "danger-button" : "action-button";
  $("#confirmDialog").showModal();
}

async function sendPendingCommand() {
  if (!state.pendingCommand || !state.api) return;
  $("#doConfirm").disabled = true;
  const result = await state.api.send_command(state.pendingCommand.name, state.pendingCommand.values, true);
  if (result.ok) {
    const dirtyMap = { alarm_thresholds: "thresholds", alarm_switches: "switches", charge_config: "charge", current_direction: "direction" };
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
  state.dirty = { thresholds: true, switches: true, charge: true, direction: true };
  text("#thresholdSync", "工程值待写入");
  if (["voltage", "temperature"].includes(view.cell_mode)) {
    state.cellMode = view.cell_mode;
    $$("#cellMode button").forEach(button => button.classList.toggle("active", button.dataset.mode === state.cellMode));
  }
  $("#onlyAbnormal").checked = !!view.only_abnormal;
  $("#projectDialog").close(); toast("工程已导入：参数只填入界面，尚未连接或写入主控");
}

document.addEventListener("DOMContentLoaded", init);
